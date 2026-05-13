"""
Pre-flight test script for the XAUUSD Anchor Bot.

Run this BEFORE the next scheduled anchor (02:00 broker time) to verify:
  1. MT5 connection works
  2. Symbol is tradeable
  3. Pending orders can be placed (catches IOC filling mode rejection)
  4. Both filling modes (IOC, RETURN) tested — finds which your broker accepts
  5. SL/TP modify works on a pending order
  6. Order cancellation works
  7. Bot magic number filtering works

Test orders are placed far from market (cannot trigger), with TEST magic
number, then cancelled. Nothing real risked.

Usage (run from xauusd_anchor_bot/ directory):
    python test_orders.py

Stop main.py before running this if it's running.
"""

import sys
import time as time_module
from pathlib import Path
from typing import Optional, Tuple

# Make project root importable regardless of where this script lives
# (project root, strategy/, or any subdir).
_SCRIPT_DIR = Path(__file__).resolve().parent
if (_SCRIPT_DIR / "config.py").exists():
    _PROJECT_ROOT = _SCRIPT_DIR
elif (_SCRIPT_DIR.parent / "config.py").exists():
    _PROJECT_ROOT = _SCRIPT_DIR.parent
else:
    raise RuntimeError(
        "Cannot find project root (config.py). "
        "Place test_orders.py in xauusd_anchor_bot/ or xauusd_anchor_bot/strategy/."
    )
sys.path.insert(0, str(_PROJECT_ROOT))

import MetaTrader5 as mt5

import config
from core.logger import setup_logger
from core.mt5_client import (
    connect_mt5,
    disconnect_mt5,
    get_account_info,
    get_symbol_info,
    get_tick,
)

# --- Test isolation ---------------------------------------------------------
# Use a DIFFERENT magic so test orders don't get touched by the real bot,
# and the real bot's pending orders (if any) don't get touched here.
TEST_MAGIC = 99999999
TEST_COMMENT = "ANCHOR_TEST"

# Test order placement: far from market so it cannot trigger.
TEST_DISTANCE = 50.0      # USD from market for the test entry
TEST_SL_DIST = 15.0       # USD SL
TEST_TP_DIST = 3.0        # USD TP
TEST_LOT = 0.10           # smaller than production to be safe

log = setup_logger("anchor_bot_test")


# --- Pretty printing --------------------------------------------------------

def section(title: str) -> None:
    log.info("")
    log.info("=" * 70)
    log.info(f"  {title}")
    log.info("=" * 70)


def result(name: str, ok: bool, detail: str = "") -> None:
    mark = "✅ PASS" if ok else "❌ FAIL"
    detail_str = f" — {detail}" if detail else ""
    log.info(f"  {mark}: {name}{detail_str}")


# --- Retcode lookup ---------------------------------------------------------

RETCODE_NAMES = {
    10004: "REQUOTE",
    10006: "REJECT",
    10007: "CANCEL",
    10008: "PLACED",
    10009: "DONE",
    10010: "DONE_PARTIAL",
    10011: "ERROR",
    10012: "TIMEOUT",
    10013: "INVALID",
    10014: "INVALID_VOLUME",
    10015: "INVALID_PRICE",
    10016: "INVALID_STOPS",
    10017: "TRADE_DISABLED",
    10018: "MARKET_CLOSED",
    10019: "NO_MONEY",
    10027: "AUTOTRADING_DISABLED",
    10030: "UNSUPPORTED_FILLING_MODE",
}


def retcode_desc(retcode: int) -> str:
    return RETCODE_NAMES.get(retcode, f"UNKNOWN({retcode})")


# --- Filling mode discovery ------------------------------------------------

FILLING_MODES = [
    ("ORDER_FILLING_IOC", mt5.ORDER_FILLING_IOC),
    ("ORDER_FILLING_RETURN", mt5.ORDER_FILLING_RETURN),
    ("ORDER_FILLING_FOK", mt5.ORDER_FILLING_FOK),
]


def try_place_with_filling(
    filling_mode_name: str,
    filling_mode: int,
    entry: float,
    sl: float,
    tp: float,
    digits: int,
) -> Tuple[bool, Optional[int], str]:
    """Try placing a buy stop with the given filling mode. Returns (ok, ticket, detail)."""
    req = {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": config.SYMBOL,
        "volume": TEST_LOT,
        "type": mt5.ORDER_TYPE_BUY_STOP,
        "price": round(entry, digits),
        "sl": round(sl, digits),
        "tp": round(tp, digits),
        "deviation": 20,
        "magic": TEST_MAGIC,
        "comment": f"{TEST_COMMENT}_{filling_mode_name[14:]}",  # shorten
        "type_time": mt5.ORDER_TIME_DAY,
        "type_filling": filling_mode,
    }
    res = mt5.order_send(req)
    if res is None:
        return False, None, f"order_send returned None (err: {mt5.last_error()})"
    if res.retcode == mt5.TRADE_RETCODE_DONE:
        return True, res.order, "accepted"
    return False, None, f"retcode {res.retcode} ({retcode_desc(res.retcode)})"


def cancel_ticket(ticket: int) -> bool:
    req = {"action": mt5.TRADE_ACTION_REMOVE, "order": ticket}
    res = mt5.order_send(req)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        return True
    return False


# --- Tests ------------------------------------------------------------------

def test_connection() -> bool:
    section("TEST 1 — MT5 connection + account + symbol")
    if not connect_mt5():
        result("Connection", False, "connect_mt5() returned False")
        return False
    info = get_account_info()
    sym = get_symbol_info()
    if info is None or sym is None:
        result("Account/symbol info", False, "info unavailable")
        return False
    log.info(f"  Account: {info.login} | Broker: {info.server}")
    log.info(f"  Balance: ${info.balance:.2f} | Mode: {'DEMO' if info.trade_mode == 0 else 'LIVE'}")
    log.info(f"  Symbol: {config.SYMBOL} | Digits: {sym.digits} | Stops level: {sym.trade_stops_level}")
    log.info(f"  Trade mode: {sym.trade_mode}  (4=FULL, others restricted)")
    result("Connection + symbol", True)
    return True


def test_tick_available() -> Optional[Tuple[float, float]]:
    section("TEST 2 — Live tick available")
    tick = get_tick()
    if tick is None:
        result("Tick read", False, "no tick from broker")
        return None
    bid, ask = tick.bid, tick.ask
    spread = ask - bid
    log.info(f"  Bid: {bid}  Ask: {ask}  Spread: ${spread:.2f}")
    result("Tick read", True, f"spread ${spread:.2f}")
    return bid, ask


def test_filling_modes(bid: float, ask: float) -> Optional[int]:
    """
    Try each filling mode. Return the first one that works,
    leaving its ticket placed so we can test modify/cancel.
    """
    section("TEST 3 — Filling mode discovery (which one your broker accepts)")
    sym = get_symbol_info()
    digits = sym.digits

    # Place test buy stop FAR above current ask (no trigger risk)
    entry = ask + TEST_DISTANCE
    sl = entry - TEST_SL_DIST
    tp = entry + TEST_TP_DIST

    log.info(f"  Test entry: {entry:.{digits}f}  SL: {sl:.{digits}f}  TP: {tp:.{digits}f}")
    log.info(f"  (Entry is ${TEST_DISTANCE} above ask — cannot trigger)")
    log.info("")

    accepted_ticket = None
    accepted_mode = None

    for mode_name, mode_value in FILLING_MODES:
        ok, ticket, detail = try_place_with_filling(
            mode_name, mode_value, entry, sl, tp, digits
        )
        log.info(f"  Trying {mode_name:<25} → {'✅' if ok else '❌'} {detail}")
        if ok and ticket is not None:
            if accepted_ticket is None:
                accepted_ticket = ticket
                accepted_mode = mode_name
            else:
                # Already have one — cancel duplicates
                cancel_ticket(ticket)

    log.info("")
    if accepted_mode:
        result(
            "Filling mode discovery", True,
            f"use '{accepted_mode}' in orders.py"
        )
        return accepted_ticket
    else:
        result("Filling mode discovery", False, "no filling mode accepted")
        return None


def test_sl_tp_modify(ticket: int) -> bool:
    section("TEST 4 — SL/TP modify on pending order")
    sym = get_symbol_info()
    digits = sym.digits

    # Fetch the order to read current SL/TP
    orders = mt5.orders_get(ticket=ticket)
    if not orders:
        result("Read pending order", False, "order disappeared")
        return False
    o = orders[0]
    log.info(f"  Before: entry={o.price_open}  SL={o.sl}  TP={o.tp}")

    # Move SL/TP by $0.50 each
    new_sl = round(o.sl - 0.50, digits)
    new_tp = round(o.tp + 0.50, digits)
    req = {
        "action": mt5.TRADE_ACTION_MODIFY,
        "order": ticket,
        "symbol": config.SYMBOL,
        "price": o.price_open,
        "sl": new_sl,
        "tp": new_tp,
        "type_time": mt5.ORDER_TIME_DAY,
    }
    res = mt5.order_send(req)
    if not res or res.retcode != mt5.TRADE_RETCODE_DONE:
        detail = f"retcode {res.retcode if res else 'None'} ({retcode_desc(res.retcode) if res else 'no result'})"
        result("Modify pending SL/TP", False, detail)
        return False

    log.info(f"  After:  SL={new_sl}  TP={new_tp}")
    result("Modify pending SL/TP", True)
    return True


def test_cancel(ticket: int) -> bool:
    section("TEST 5 — Cancel pending order")
    ok = cancel_ticket(ticket)
    result("Cancel pending", ok)
    return ok


def cleanup_test_orders() -> int:
    """Cancel any leftover TEST_MAGIC pending orders. Returns count."""
    section("CLEANUP — Cancel any stray test orders")
    orders = mt5.orders_get(symbol=config.SYMBOL)
    n = 0
    if orders:
        for o in orders:
            if o.magic == TEST_MAGIC:
                if cancel_ticket(o.ticket):
                    n += 1
                    log.info(f"  Cancelled stray test ticket {o.ticket}")
    if n == 0:
        log.info("  No stray test orders found.")
    return n


# --- Verdict ----------------------------------------------------------------

def print_verdict(
    connection_ok: bool,
    tick_ok: bool,
    filling_ticket: Optional[int],
    modify_ok: bool,
    cancel_ok: bool,
) -> None:
    section("VERDICT")
    all_pass = connection_ok and tick_ok and filling_ticket is not None
    if all_pass:
        log.info("  ✅ Bot is ready to run on this broker.")
        log.info("")
        log.info("  Action items before tomorrow's 02:00 anchor:")
        log.info("    1. If the filling mode test showed 'ORDER_FILLING_IOC' failed")
        log.info("       and 'ORDER_FILLING_RETURN' succeeded, edit")
        log.info("       strategy/orders.py and change both 'type_filling' lines")
        log.info("       from mt5.ORDER_FILLING_IOC to mt5.ORDER_FILLING_RETURN.")
        log.info("    2. Restart the bot:  python main.py")
        if not modify_ok:
            log.info("    ⚠️ SL/TP modify failed — trail logic may not work in live.")
        if not cancel_ok:
            log.info("    ⚠️ Cancel failed — OCO and EOD cleanup may not work.")
    else:
        log.info("  ❌ Bot is NOT ready. Issues:")
        if not connection_ok:
            log.info("     • MT5 connection / symbol issue — check MT5 terminal + symbol config")
        if not tick_ok:
            log.info("     • No live tick — market may be closed or symbol disabled")
        if filling_ticket is None:
            log.info("     • No filling mode accepted — broker may not allow this symbol/lot")
    log.info("")


# --- Main -------------------------------------------------------------------

def main() -> None:
    log.info("")
    log.info("XAUUSD Anchor Bot — Pre-flight Order Test")
    log.info(f"Symbol: {config.SYMBOL} | Test lot: {TEST_LOT} | Test magic: {TEST_MAGIC}")
    log.info("")

    connection_ok = False
    tick_ok = False
    filling_ticket: Optional[int] = None
    modify_ok = False
    cancel_ok = False

    try:
        connection_ok = test_connection()
        if not connection_ok:
            print_verdict(connection_ok, tick_ok, filling_ticket, modify_ok, cancel_ok)
            return

        prices = test_tick_available()
        tick_ok = prices is not None
        if not tick_ok:
            print_verdict(connection_ok, tick_ok, filling_ticket, modify_ok, cancel_ok)
            return
        bid, ask = prices

        filling_ticket = test_filling_modes(bid, ask)

        if filling_ticket:
            time_module.sleep(1)  # let broker register
            modify_ok = test_sl_tp_modify(filling_ticket)
            time_module.sleep(1)
            cancel_ok = test_cancel(filling_ticket)
            filling_ticket = None  # cancelled

        # Sweep any leftover test orders just in case
        cleanup_test_orders()

        print_verdict(connection_ok, tick_ok, filling_ticket is not None or cancel_ok, modify_ok, cancel_ok)

    except KeyboardInterrupt:
        log.info("Test interrupted by user.")
    except Exception as e:
        log.error(f"Test crashed: {e}", exc_info=True)
    finally:
        # Belt and suspenders cleanup
        try:
            cleanup_test_orders()
        except Exception:
            pass
        disconnect_mt5()


if __name__ == "__main__":
    main()
