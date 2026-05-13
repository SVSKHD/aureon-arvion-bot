"""
Telegram Diagnostic — figures out why telegram messages aren't arriving.

Tests in order:
  1. Config values look valid (token format, chat_id format)
  2. Token works (calls Telegram getMe API)
  3. Chat ID is reachable (sends a test message)

Prints detailed error analysis with specific fix instructions for each failure mode.

Run from project root OR strategy/ folder:
    python test_telegram.py
"""

import sys
from pathlib import Path

# Make project root importable regardless of where this script lives
_SCRIPT_DIR = Path(__file__).resolve().parent
if (_SCRIPT_DIR / "config.py").exists():
    _PROJECT_ROOT = _SCRIPT_DIR
elif (_SCRIPT_DIR.parent / "config.py").exists():
    _PROJECT_ROOT = _SCRIPT_DIR.parent
else:
    raise RuntimeError("Cannot find config.py. Run from project root or strategy/.")
sys.path.insert(0, str(_PROJECT_ROOT))

import requests

import config


def section(title):
    print("")
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def result(name, ok, detail=""):
    mark = "✅ PASS" if ok else "❌ FAIL"
    print(f"  {mark}: {name}" + (f" — {detail}" if detail else ""))


def main():
    print("")
    print("Telegram Diagnostic for XAUUSD Anchor Bot")
    print("")

    # ------------------------------------------------------------------
    # 1. Config sanity check
    # ------------------------------------------------------------------
    section("TEST 1 — Config values")

    if not config.TELEGRAM_ENABLED:
        result("TELEGRAM_ENABLED", False, "set TELEGRAM_ENABLED = True in config.py")
        return
    result("TELEGRAM_ENABLED", True)

    token = config.TELEGRAM_BOT_TOKEN.strip()
    chat_id = str(config.TELEGRAM_CHAT_ID).strip()

    if not token:
        result("TELEGRAM_BOT_TOKEN", False, "empty — get one from @BotFather")
        return

    # Valid token format: "1234567890:ABC-DEF1234ghIkl..." (numbers, colon, ~35 chars)
    if ":" not in token:
        result("Token format", False, "missing colon — should be 'NNNN:AAA...' from BotFather")
        return
    bot_id, secret = token.split(":", 1)
    if not bot_id.isdigit() or len(secret) < 30:
        result("Token format", False, f"looks malformed: bot_id='{bot_id}' secret_len={len(secret)}")
        return
    result("Token format", True, f"bot_id={bot_id}, secret_len={len(secret)}")

    if not chat_id:
        result("TELEGRAM_CHAT_ID", False, "empty — get yours from @userinfobot")
        return

    # Chat ID should be all digits (with optional leading minus for groups)
    cid_check = chat_id[1:] if chat_id.startswith("-") else chat_id
    if not cid_check.isdigit():
        result("Chat ID format", False, f"'{chat_id}' is not numeric — should be a number from @userinfobot")
        return
    result("Chat ID format", True, f"chat_id={chat_id} {'(group)' if chat_id.startswith('-') else '(personal)'}")

    # ------------------------------------------------------------------
    # 2. Token validity — Telegram getMe API
    # ------------------------------------------------------------------
    section("TEST 2 — Token validity (calling Telegram getMe)")

    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
    except requests.RequestException as e:
        result("getMe network", False, str(e))
        print("    Check your internet connection / firewall.")
        return

    if r.status_code == 200:
        bot_info = r.json().get("result", {})
        bot_name = bot_info.get("first_name", "?")
        bot_username = bot_info.get("username", "?")
        result("Token valid", True, f"bot is '@{bot_username}' (name: {bot_name})")
    elif r.status_code == 401:
        result("Token valid", False, "401 Unauthorized — token is wrong or revoked")
        print("    Get a fresh token by messaging @BotFather with /mybots")
        return
    elif r.status_code == 404:
        result("Token valid", False, "404 — token format wrong")
        return
    else:
        result("Token valid", False, f"HTTP {r.status_code}: {r.text[:200]}")
        return

    # ------------------------------------------------------------------
    # 3. Send a real test message
    # ------------------------------------------------------------------
    section("TEST 3 — Send test message to your chat")

    test_msg = (
        "🧪 Telegram Diagnostic Test\n"
        "\n"
        "If you see this message, telegram is working.\n"
        "The bot startup message should arrive here too."
    )

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": test_msg},
            timeout=10,
        )
    except requests.RequestException as e:
        result("Send message network", False, str(e))
        return

    if r.status_code == 200:
        result("Send message", True, "Telegram accepted — check your phone NOW")
        print("")
        print("  → Open Telegram and look for the test message from @" + bot_username)
        print("  → If you SEE the message: telegram is working perfectly.")
        print("    The bot's startup message must be arriving too — check older messages.")
        print("    Or check that you're looking at the right bot (search for @" + bot_username + ").")
        print("")
        print("  → If you DON'T see it: token+chat_id are valid but message went to wrong place.")
        print("    Verify the chat_id matches YOUR user (not someone else's).")
        return

    # Error analysis
    try:
        err = r.json()
        desc = err.get("description", "")
    except Exception:
        desc = r.text[:300]

    if r.status_code == 400 and "chat not found" in desc.lower():
        result("Send message", False, "Chat not found — see fix below")
        print("")
        print("  ❗ This is the most common failure: bot can't message you because")
        print(f"     you have not started a chat with @{bot_username} yet.")
        print("")
        print("  FIX:")
        print(f"     1. Open Telegram → search for @{bot_username}")
        print("     2. Click /start (or send any message)")
        print("     3. Re-run this test")
        return

    if r.status_code == 400 and "wrong" in desc.lower() and "chat" in desc.lower():
        result("Send message", False, f"Chat ID problem: {desc}")
        print("")
        print("  FIX:")
        print("     1. Open Telegram → search for @userinfobot")
        print("     2. Send any message — it replies with your chat ID")
        print("     3. Put that number in TELEGRAM_CHAT_ID in config.py")
        return

    if r.status_code == 403:
        result("Send message", False, "403 Forbidden — bot blocked or never started")
        print("")
        print(f"  FIX: Open Telegram, find @{bot_username}, click 'Start' / send any message.")
        return

    result("Send message", False, f"HTTP {r.status_code}: {desc}")


if __name__ == "__main__":
    main()
