"""
Structured Start Log — separate from the verbose bot.log.

Writes one JSON object per line (JSONL) to logs/start_log.jsonl.
Every event records both broker SERVER time AND your local IST time so
nothing is ambiguous.

Designed to be easy to:
  - Read at a glance (one line per event, chronological)
  - Parse programmatically (standard JSON per line)
  - Search (`grep ANCHOR_CAPTURED logs/start_log.jsonl`)
  - Append-only (crash-safe, no rewrites)

Events recorded:
  BOT_STARTED          Bot connected to MT5
  BOT_STOPPED          User-initiated shutdown
  ANCHOR_CAPTURED      Anchor candle open price captured
  ORDERS_PLACED        OCO pendings successfully placed
  DAY_SKIPPED          Day skipped (with reason)
  ENTRY_FILLED         Pending order triggered into position
  TP_SL_NORMALIZED     SL/TP corrected from actual filled price
  LOCK_REACHED         Trail step ratcheted up
  POSITION_CLOSED      Position exited (TP / Trail-SL / Manual)
  EOD_CLEANUP          End-of-day pending cancel
  LATE_RECOVERY        Late anchor recovery (captured or skipped)
  POSITION_RESUMED     Bot restart found existing position
  PENDINGS_RESUMED     Bot restart found existing pendings

Example record:
  {"ist_time": "2026-05-13 04:30:15 IST", "server_time": "2026-05-13 02:00:15",
   "event": "ANCHOR_CAPTURED", "anchor_price": 4749.78, "buy_stop": 4759.78,
   "sell_stop": 4739.78, ...}
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import config
from core.logger import get_logger
from core.mt5_client import get_server_time

log = get_logger()

# IST timezone (UTC+5:30) — independent of OS clock setting
IST = timezone(timedelta(hours=5, minutes=30))

# Resolve start log path relative to project root (where config.py lives)
_PROJECT_ROOT = Path(config.__file__).resolve().parent
START_LOG_PATH = _PROJECT_ROOT / "logs" / "start_log.jsonl"


def _write(event_type: str, data: dict) -> None:
    """Append one JSON record to the start log. Never raises."""
    try:
        path = Path(START_LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)

        ist_now = datetime.now(IST)
        srv_now = get_server_time()

        record = {
            "ist_time": ist_now.strftime("%Y-%m-%d %H:%M:%S IST"),
            "server_time": (
                srv_now.strftime("%Y-%m-%d %H:%M:%S") if srv_now else None
            ),
            "event": event_type,
            **data,
        }

        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning(f"Failed to write start log [{event_type}]: {e}")


# ============================================================================
# PUBLIC RECORD FUNCTIONS — call these from anywhere in the bot
# ============================================================================

def record_bot_started(account: int, broker: str, mode: str, symbol: str, lot: float) -> None:
    _write("BOT_STARTED", {
        "account": account,
        "broker": broker,
        "mode": mode,
        "symbol": symbol,
        "lot": lot,
    })


def record_bot_stopped(reason: str = "user") -> None:
    _write("BOT_STOPPED", {"reason": reason})


def get_recent_halt_within(hours: float) -> Optional[dict]:
    """
    Scan the tail of start_log.jsonl for the most recent BOT_STOPPED record
    with a halt-related reason (anything other than "user"). Returns the
    record dict if it's within the last `hours`, else None.

    Used at startup to break watchdog auto-restart loops: if the bot
    halted very recently due to a critical condition, restarting blindly
    risks repeating the same dangerous state. The user must acknowledge
    via env var ANCHOR_BOT_FORCE_RESUME=1 to proceed.
    """
    if not START_LOG_PATH.exists():
        return None
    try:
        # Read last ~50 lines (cheap) and find most recent BOT_STOPPED
        with open(START_LOG_PATH, "rb") as f:
            # Seek near end
            try:
                f.seek(-50 * 500, 2)  # ~25KB from end
            except OSError:
                f.seek(0)
            tail = f.read().decode("utf-8", errors="replace")
        lines = [ln for ln in tail.splitlines() if ln.strip()]
        # Walk backwards
        for ln in reversed(lines):
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if rec.get("event") != "BOT_STOPPED":
                continue
            reason = rec.get("reason", "")
            if reason == "user" or not reason:
                # Clean stop — not a halt
                return None
            # Compare IST timestamp
            ist_str = rec.get("ist_time")
            if not ist_str:
                continue
            try:
                rec_time = datetime.fromisoformat(ist_str)
            except ValueError:
                continue
            now_ist = datetime.now(IST)
            age = (now_ist - rec_time).total_seconds() / 3600
            if age <= hours:
                return rec
            return None  # too old
    except Exception as e:
        log.warning(f"Could not read recent halt state: {e}")
    return None


def record_anchor_captured(anchor_price: float, anchor_time: Any, levels: Any) -> None:
    _write("ANCHOR_CAPTURED", {
        "anchor_time_broker": str(anchor_time),
        "anchor_price": float(anchor_price),
        "buy_stop": levels.long_entry,
        "sell_stop": levels.short_entry,
        "buy_sl": levels.long_sl,
        "buy_tp": levels.long_tp,
        "sell_sl": levels.short_sl,
        "sell_tp": levels.short_tp,
    })


def record_orders_placed(buy_ticket: Optional[int], sell_ticket: Optional[int]) -> None:
    _write("ORDERS_PLACED", {
        "buy_ticket": buy_ticket,
        "sell_ticket": sell_ticket,
    })


def record_day_skipped(reason: str, **extra) -> None:
    _write("DAY_SKIPPED", {"reason": reason, **extra})


def record_entry_filled(side: str, entry_price: float, planned_price: float, ticket: int) -> None:
    if side == "LONG":
        slip = entry_price - planned_price
    else:
        slip = planned_price - entry_price
    _write("ENTRY_FILLED", {
        "side": side,
        "ticket": ticket,
        "planned_price": planned_price,
        "entry_price": entry_price,
        "slippage": round(slip, 5),
    })


def record_tp_sl_normalized(side: str, ticket: int, new_sl: float, new_tp: float) -> None:
    _write("TP_SL_NORMALIZED", {
        "side": side,
        "ticket": ticket,
        "new_sl": new_sl,
        "new_tp": new_tp,
    })


def record_lock_reached(side: str, step: int, locked_amount: float, new_sl: float, ticket: int) -> None:
    _write("LOCK_REACHED", {
        "side": side,
        "ticket": ticket,
        "step": step,
        "locked_amount": locked_amount,
        "new_sl": new_sl,
    })


def record_position_closed(side: Optional[str], ticket: int,
                            exit_price: Optional[float], pnl: Optional[float],
                            reason: str) -> None:
    _write("POSITION_CLOSED", {
        "side": side,
        "ticket": ticket,
        "exit_price": exit_price,
        "pnl": pnl,
        "reason": reason,
    })


def record_eod_cleanup(cancelled_count: int = 0) -> None:
    _write("EOD_CLEANUP", {"cancelled_count": cancelled_count})


def record_late_recovery(status: str, anchor: float, reason: Optional[str] = None) -> None:
    """status: 'captured' or 'skipped'"""
    data = {"status": status, "anchor": anchor}
    if reason:
        data["reason"] = reason
    _write("LATE_RECOVERY", data)


def record_position_resumed(ticket: int, side: str, entry: float,
                             sl: float, tp: float, lock_idx: int) -> None:
    _write("POSITION_RESUMED", {
        "ticket": ticket,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "lock_step": lock_idx + 1,
    })


def record_pendings_resumed(buy_ticket: Optional[int], sell_ticket: Optional[int], anchor: float) -> None:
    _write("PENDINGS_RESUMED", {
        "buy_ticket": buy_ticket,
        "sell_ticket": sell_ticket,
        "anchor_inferred": anchor,
    })


def get_weekly_pnl(server_now: datetime) -> float:
    """
    Sum the PnL of POSITION_CLOSED events for the CURRENT ISO week.

    Uses server_now's ISO week (Monday-Sunday). A trade is counted if its
    server_time falls in the same (year, week) as server_now.

    Returns total PnL in USD. Returns 0.0 if log file missing or no closes
    this week. Trades with pnl=null (history lookup failed) are skipped.

    Used by weekly profit lock to decide whether to skip a trading day.
    """
    if not _LOG_PATH.exists():
        return 0.0

    target_iso = server_now.isocalendar()
    target_year = target_iso[0]
    target_week = target_iso[1]

    total = 0.0
    try:
        with open(_LOG_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except Exception:
                    continue
                if evt.get("event") != "POSITION_CLOSED":
                    continue
                pnl = evt.get("pnl")
                if pnl is None:
                    continue
                stime = evt.get("server_time")
                if not stime:
                    continue
                try:
                    dt = datetime.strptime(stime, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                iso = dt.isocalendar()
                if iso[0] == target_year and iso[1] == target_week:
                    total += float(pnl)
    except Exception as e:
        log.warning(f"get_weekly_pnl failed: {e}")
        return 0.0

    return round(total, 2)



def record_rescue_triggered(**kwargs) -> None:
    """RESCUE_TRIGGERED — adverse threshold reached, market order placed."""
    _write_event("RESCUE_TRIGGERED", **kwargs)


def record_rescue_filled(**kwargs) -> None:
    """RESCUE_FILLED — rescue market order filled, position open."""
    _write_event("RESCUE_FILLED", **kwargs)


def record_rescue_closed(**kwargs) -> None:
    """RESCUE_CLOSED — rescue position closed (TP/SL/Trail/BE)."""
    _write_event("RESCUE_CLOSED", **kwargs)