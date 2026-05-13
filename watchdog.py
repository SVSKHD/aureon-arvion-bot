"""
watchdog.py — Supervisor for main.py with Telegram command interface.

USAGE
    python watchdog.py

This is the new entry point. Don't run main.py directly anymore.

FEATURES
  - Spawns main.py as subprocess
  - Auto-restarts main.py on crash (with backoff)
  - Polls Telegram for commands:
      /status   — current bot state (positions, equity, etc.)
      /restart  — kill + restart main.py
      /halt     — stop main.py until /resume
      /resume   — restart after halt
      /setweekly <amount> — change weekly profit lock target
      /resetweekly — revert weekly target to config.py default
      /help     — list commands
  - Rate limits restarts (max 10/hour, then auto-halt)
  - Reads logs/status.json (written by main.py background thread)

REQUIREMENTS
  - TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in env or .env file
  - main.py must import + call start_status_writer() near startup
  - `requests` library installed (pip install requests)
  - `python-dotenv` library installed (pip install python-dotenv) — optional

ARCHITECTURE
  Watchdog runs in foreground. Two threads:
    1. Main thread: monitors subprocess, restarts on crash
    2. Daemon thread: polls Telegram getUpdates, handles commands
"""

import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# Load .env if python-dotenv is available — same mechanism config.py uses
# ----------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
    _ENV_PATH = PROJECT_ROOT / ".env"
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH)
        _ENV_LOADED = True
    else:
        _ENV_LOADED = False
except ImportError:
    _ENV_LOADED = False

# ----------------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------------

MAIN_SCRIPT = PROJECT_ROOT / "main.py"
STATUS_FILE = PROJECT_ROOT / "logs" / "status.json"
HALT_FLAG = PROJECT_ROOT / "halt.flag"
WATCHDOG_LOG = PROJECT_ROOT / "logs" / "watchdog.log"

IST = timezone(timedelta(hours=5, minutes=30))

# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}" if TG_TOKEN else ""

# ----------------------------------------------------------------------------
# Restart policy
# ----------------------------------------------------------------------------

RESTART_BACKOFF_SEC = [5, 30, 60, 120, 300]
MAX_RESTARTS_PER_HOUR = 10


# ============================================================================
# Logging
# ============================================================================

def log(msg: str) -> None:
    """Log to console and watchdog.log."""
    ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    line = f"{ts} | {msg}"
    print(line, flush=True)
    try:
        WATCHDOG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(WATCHDOG_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ============================================================================
# Telegram helpers
# ============================================================================

def tg_send(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"{TG_API}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception as e:
        log(f"Telegram send failed: {e}")


# ============================================================================
# Watchdog state
# ============================================================================

class Watchdog:
    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.process_lock = threading.Lock()
        self.shutdown_requested = False
        self.restart_history: deque = deque(maxlen=100)
        self.session_restart_count = 0

    # ------------------------------------------------------------------
    # Subprocess management
    # ------------------------------------------------------------------

    def start_main(self) -> None:
        with self.process_lock:
            if self.process is not None and self.process.poll() is None:
                log("start_main: already running, no-op")
                return
            cmd = [sys.executable, "-u", str(MAIN_SCRIPT)]
            log(f"Starting: {' '.join(cmd)}")
            self.process = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT))
            log(f"main.py started (PID {self.process.pid})")

    def kill_main(self) -> int | None:
        """Terminate main.py, return exit code (or None if not running)."""
        with self.process_lock:
            if self.process is None or self.process.poll() is not None:
                return None
            log(f"Terminating main.py (PID {self.process.pid})")
            self.process.terminate()
            try:
                rc = self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                log("Terminate timeout, killing")
                self.process.kill()
                rc = self.process.wait(timeout=5)
            log(f"main.py exited (code {rc})")
            return rc

    # ------------------------------------------------------------------
    # Status reader
    # ------------------------------------------------------------------

    def read_status(self) -> dict | None:
        if not STATUS_FILE.exists():
            return None
        try:
            with open(STATUS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            return {"error": f"could not read status.json: {e}"}

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def handle_command(self, raw_cmd: str) -> None:
        parts = raw_cmd.split()
        cmd = parts[0].lower().lstrip("/")
        args = parts[1:]
        log(f"Command received: /{cmd} {' '.join(args)}")

        if cmd == "status":
            self._cmd_status()
        elif cmd == "restart":
            self._cmd_restart()
        elif cmd == "halt":
            self._cmd_halt()
        elif cmd == "resume":
            self._cmd_resume()
        elif cmd == "setweekly":
            self._cmd_setweekly(args)
        elif cmd == "resetweekly":
            self._cmd_resetweekly()
        elif cmd in ("help", "start"):
            self._cmd_help()
        else:
            tg_send(f"❓ Unknown command: /{cmd}\nSend /help for available commands.")

    def _cmd_status(self) -> None:
        status = self.read_status()
        if not status:
            tg_send(
                "📊 Bot state: NO STATUS FILE\n"
                "main.py may be starting, halted, or crashed.\n"
                f"Subprocess alive: {self.process is not None and self.process.poll() is None}"
            )
            return

        # Build status report
        lines = ["📊 BOT STATUS"]
        lines.append(f"⏱ Updated: {status.get('updated_at_ist', '?')}")
        lines.append(f"⚙️ State: {status.get('state', '?')}")

        if "account" in status:
            lines.append(
                f"💰 Equity: ${status.get('equity', 0):.2f}  "
                f"Bal: ${status.get('balance', 0):.2f}"
            )

        lines.append(
            f"📈 Positions: {status.get('positions_count', 0)}  "
            f"Pendings: {status.get('orders_count', 0)}"
        )

        if status.get("active_position"):
            p = status["active_position"]
            lines.append(
                f"\n📍 OPEN POSITION ({p['side']})\n"
                f"   Entry: {p['entry']}\n"
                f"   Current: {p.get('current', '?')}\n"
                f"   SL: {p['sl']}  TP: {p['tp']}\n"
                f"   Floating PnL: ${p.get('floating_pnl', 0):.2f}"
            )

        if status.get("rescue_position"):
            p = status["rescue_position"]
            lines.append(
                f"\n🆘 RESCUE POSITION ({p['side']})\n"
                f"   Entry: {p['entry']}\n"
                f"   Current: {p.get('current', '?')}\n"
                f"   SL: {p['sl']}  TP: {p['tp']}\n"
                f"   Floating PnL: ${p.get('floating_pnl', 0):.2f}"
            )

        if status.get("pending_orders"):
            for o in status["pending_orders"]:
                lines.append(f"   ⏸ {o['type']} @ {o['price']}")

        if "bid" in status:
            lines.append(
                f"\n💱 Bid {status['bid']}  Ask {status['ask']}  "
                f"Spread ${status.get('spread_usd', 0):.2f}"
            )

        wl = status.get("weekly_lock")
        if wl and wl.get("enabled"):
            if wl.get("locked"):
                lines.append(
                    f"\n🔒 WEEKLY LOCK ACTIVE ({wl.get('iso_week', '?')})\n"
                    f"   Weekly PnL: ${wl.get('weekly_pnl', 0):.2f}\n"
                    f"   Target:     ${wl.get('target', 0):.2f}\n"
                    f"   Bot resumes next Monday."
                )
            else:
                lines.append(
                    f"\n💰 Weekly PnL: ${wl.get('weekly_pnl', 0):.2f} of "
                    f"${wl.get('target', 0):.2f} target\n"
                    f"   ${wl.get('remaining_to_lock', 0):.2f} until lock"
                )

        lines.append(f"\n♻️ Restarts this session: {self.session_restart_count}")

        if HALT_FLAG.exists():
            lines.append("🛑 HALTED (use /resume)")

        tg_send("\n".join(lines))

    def _cmd_restart(self) -> None:
        tg_send("♻️ /restart received — restarting main.py...")
        self.kill_main()
        time.sleep(2)
        self.start_main()
        self.session_restart_count += 1
        tg_send("✓ main.py restarted")

    def _cmd_halt(self) -> None:
        tg_send("🛑 /halt received — stopping main.py. Send /resume to restart.")
        HALT_FLAG.touch()
        self.kill_main()

    def _cmd_resume(self) -> None:
        if HALT_FLAG.exists():
            HALT_FLAG.unlink()
        tg_send("▶️ /resume received — starting main.py...")
        self.start_main()
        tg_send("✓ main.py running")

    def _cmd_setweekly(self, args: list) -> None:
        if not args:
            tg_send(
                "Usage: /setweekly <amount>\n"
                "Example: /setweekly 250\n\n"
                "Sets the weekly profit lock target in USD.\n"
                "Once weekly bot PnL hits this, no more trades until Monday.\n"
                "Backtest sweet spot: $200-$250.\n"
                "Use /resetweekly to revert to config.py default."
            )
            return
        try:
            new_value = float(args[0])
        except ValueError:
            tg_send(f"❌ Invalid amount: '{args[0]}'. Must be a number.")
            return
        if new_value < 0:
            tg_send("❌ Amount must be >= 0.")
            return
        if new_value > 100000:
            tg_send("❌ Amount too large. Sanity check: provide a USD value, e.g. 250.")
            return

        # Write runtime override JSON directly (no Python import needed)
        runtime_path = PROJECT_ROOT / "runtime_config.json"
        data = {}
        if runtime_path.exists():
            try:
                with open(runtime_path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass
        old_value = data.get("WEEKLY_PROFIT_LOCK_USD")
        data["WEEKLY_PROFIT_LOCK_USD"] = new_value
        tmp = runtime_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(runtime_path)

        old_str = f"${old_value:.2f}" if old_value is not None else "config default"
        tg_send(
            f"✓ Weekly profit lock target updated\n"
            f"Old: {old_str}\n"
            f"New: ${new_value:.2f}\n\n"
            f"Effect: next anchor check uses the new target.\n"
            f"No restart needed."
        )
        log(f"setweekly: {old_str} → ${new_value:.2f}")

    def _cmd_resetweekly(self) -> None:
        """Remove runtime override, fall back to config.py default."""
        runtime_path = PROJECT_ROOT / "runtime_config.json"
        if not runtime_path.exists():
            tg_send("ℹ️ No runtime override active. Using config.py default.")
            return
        try:
            with open(runtime_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            tg_send(f"❌ Could not read runtime config: {e}")
            return
        if "WEEKLY_PROFIT_LOCK_USD" not in data:
            tg_send("ℹ️ No weekly override active. Using config.py default.")
            return
        old_value = data.pop("WEEKLY_PROFIT_LOCK_USD")
        tmp = runtime_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(runtime_path)
        tg_send(
            f"✓ Weekly target reset\n"
            f"Was: ${old_value:.2f}\n"
            f"Now: config.py default (restart-time value)"
        )
        log(f"resetweekly: ${old_value:.2f} → config default")

    def _cmd_help(self) -> None:
        tg_send(
            "🤖 Watchdog commands:\n\n"
            "📊 STATE\n"
            "/status — bot state, position, equity, weekly P&L\n\n"
            "♻️ CONTROL\n"
            "/restart — kill + restart main.py\n"
            "/halt — stop main.py (won't auto-restart)\n"
            "/resume — restart after halt\n\n"
            "💰 WEEKLY LOCK\n"
            "/setweekly <amount> — set weekly profit target (e.g. /setweekly 250)\n"
            "/resetweekly — revert to config.py default\n\n"
            "/help — this list"
        )

    # ------------------------------------------------------------------
    # Telegram polling
    # ------------------------------------------------------------------

    def poll_telegram_loop(self) -> None:
        if not TG_TOKEN or not TG_CHAT_ID:
            log("Telegram polling disabled (no credentials)")
            return
        offset = 0
        log("Telegram command poller started")
        while not self.shutdown_requested:
            try:
                resp = requests.get(
                    f"{TG_API}/getUpdates",
                    params={"offset": offset + 1, "timeout": 30},
                    timeout=35,
                ).json()
                if not resp.get("ok"):
                    time.sleep(5)
                    continue
                for upd in resp.get("result", []):
                    offset = upd["update_id"]
                    msg = upd.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    text = (msg.get("text") or "").strip()
                    if chat_id != TG_CHAT_ID:
                        continue  # ignore other chats
                    if text.startswith("/"):
                        self.handle_command(text)
            except requests.exceptions.Timeout:
                continue
            except Exception as e:
                log(f"Telegram poll error: {e}")
                time.sleep(5)

    # ------------------------------------------------------------------
    # Subprocess monitor (main loop)
    # ------------------------------------------------------------------

    def monitor_loop(self) -> None:
        log("Watchdog monitor loop started")
        while not self.shutdown_requested:
            # Halt flag → don't run main.py
            if HALT_FLAG.exists():
                if self.process is not None and self.process.poll() is None:
                    log("Halt flag set, killing main.py")
                    self.kill_main()
                time.sleep(5)
                continue

            with self.process_lock:
                alive = self.process is not None and self.process.poll() is None

            if alive:
                time.sleep(5)
                continue

            # main.py is not running — restart needed
            if self.process is not None:
                rc = self.process.returncode
                log(f"main.py exited with code {rc}")
                tg_send(f"⚠️ main.py exited (code {rc}). Restarting...")
                self.session_restart_count += 1
                self.restart_history.append(time.time())

            # Restart rate limiting
            cutoff = time.time() - 3600
            recent = [t for t in self.restart_history if t > cutoff]
            if len(recent) > MAX_RESTARTS_PER_HOUR:
                msg = (
                    f"🚨 main.py restarted {len(recent)} times in 1h. "
                    f"Auto-halting. Send /resume after investigating."
                )
                log(msg)
                tg_send(msg)
                HALT_FLAG.touch()
                continue

            # Backoff before restart
            idx = min(len(recent), len(RESTART_BACKOFF_SEC) - 1)
            delay = RESTART_BACKOFF_SEC[idx]
            log(f"Backoff {delay}s before restart attempt {len(recent) + 1}")
            time.sleep(delay)

            self.start_main()

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self) -> None:
        log("=" * 60)
        log("Watchdog starting")
        log(f"Project root: {PROJECT_ROOT}")
        log(f"Main script: {MAIN_SCRIPT}")
        log(f"Status file: {STATUS_FILE}")
        log(f"Halt flag: {HALT_FLAG}")
        log(f".env file: {'LOADED' if _ENV_LOADED else 'NOT FOUND (or python-dotenv missing)'}")
        log("=" * 60)

        # ----- TELEGRAM SELF-TEST -----
        if not TG_TOKEN:
            log("❌ TELEGRAM_BOT_TOKEN env var is EMPTY.")
            log("   Telegram commands will NOT work.")
            log("   Fix: set TELEGRAM_BOT_TOKEN in .env file or via setx, then restart watchdog.")
        elif not TG_CHAT_ID:
            log("❌ TELEGRAM_CHAT_ID env var is EMPTY.")
            log("   Telegram commands will NOT work.")
        else:
            # Mask token for log (show last 4 chars only)
            token_hint = f"...{TG_TOKEN[-4:]}" if len(TG_TOKEN) > 4 else "***"
            log(f"✓ TELEGRAM_BOT_TOKEN: {token_hint}  TELEGRAM_CHAT_ID: {TG_CHAT_ID}")
            log("Testing Telegram connection (getMe)...")
            try:
                resp = requests.get(f"{TG_API}/getMe", timeout=10).json()
                if resp.get("ok"):
                    bot_name = resp["result"].get("username", "?")
                    log(f"✓ Telegram bot reachable: @{bot_name}")
                else:
                    log(f"❌ Telegram getMe failed: {resp}")
                    log("   Token may be invalid or revoked.")
                    log("   Get a new one from @BotFather and update .env.")
            except Exception as e:
                log(f"❌ Telegram API unreachable: {e}")
                log("   Check internet connection.")

            # Test message send to confirm chat_id is correct
            log("Sending test message to chat...")
            try:
                resp = requests.post(
                    f"{TG_API}/sendMessage",
                    json={"chat_id": TG_CHAT_ID,
                          "text": "🟢 Watchdog starting — Telegram commands ready"},
                    timeout=10,
                ).json()
                if resp.get("ok"):
                    log(f"✓ Test message sent to chat {TG_CHAT_ID}")
                else:
                    log(f"❌ sendMessage failed: {resp}")
                    log("   Verify TELEGRAM_CHAT_ID is correct (try @userinfobot in your chat).")
            except Exception as e:
                log(f"❌ sendMessage error: {e}")

        # Telegram polling thread (only if both credentials present)
        if TG_TOKEN and TG_CHAT_ID:
            t = threading.Thread(target=self.poll_telegram_loop, daemon=True)
            t.start()
        else:
            log("⚠️ Telegram polling thread NOT started (missing credentials)")

        # Initial start
        if HALT_FLAG.exists():
            log(f"Halt flag present, not starting main.py. Send /resume to start.")
            tg_send(
                "🛑 Watchdog started but main.py is HALTED.\n"
                "Send /resume to start trading."
            )
        else:
            self.start_main()

        try:
            self.monitor_loop()
        except KeyboardInterrupt:
            log("KeyboardInterrupt — shutting down")
            self.shutdown_requested = True
            self.kill_main()
            tg_send("👋 Watchdog stopped (Ctrl+C)")
            log("Watchdog shutdown complete")


if __name__ == "__main__":
    Watchdog().run()