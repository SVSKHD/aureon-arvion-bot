"""
emergency_flatten.py — MANUAL emergency utility

USAGE
  python emergency_flatten.py

Closes ALL bot-magic positions at market and cancels ALL bot-magic
pending orders for the configured SYMBOL. Use this when:
  - Bot has HaltBot'd and you want a clean state
  - Duplicate positions detected and you want to wipe and restart
  - Pre-deployment dry run to confirm cleanup works

SAFETY
  - Filters strictly by MAGIC + SYMBOL — will NOT touch your manual trades
  - Confirms each action before executing (interactive prompt)
  - Run this BEFORE restarting main.py if you've HaltBot'd with leftover state

This is intentionally NOT called automatically by the bot. Auto-flatten on
uncertain state is itself a way accounts blow up. Manual review + manual
invocation is safer.
"""

import sys

import MetaTrader5 as mt5

import config


def main() -> int:
    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}", file=sys.stderr)
        return 1

    info = mt5.account_info()
    if info is None:
        print("Not logged in to MT5", file=sys.stderr)
        mt5.shutdown()
        return 1

    print(f"Account: {info.login}  Broker: {info.server}  Equity: ${info.equity:.2f}")
    print(f"Symbol filter: {config.SYMBOL}  Magic filter: {config.MAGIC}")
    print()

    # Collect bot-owned items
    all_positions = mt5.positions_get(symbol=config.SYMBOL) or ()
    all_orders = mt5.orders_get(symbol=config.SYMBOL) or ()
    bot_positions = [p for p in all_positions if p.magic == config.MAGIC]
    bot_orders = [o for o in all_orders if o.magic == config.MAGIC]

    print(f"Bot positions to flatten: {len(bot_positions)}")
    for p in bot_positions:
        side = "LONG" if p.type == mt5.POSITION_TYPE_BUY else "SHORT"
        print(f"  - Ticket {p.ticket}  {side}  vol={p.volume}  open={p.price_open}  pnl=${p.profit:.2f}")
    print(f"Bot pendings to cancel: {len(bot_orders)}")
    for o in bot_orders:
        print(f"  - Ticket {o.ticket}  type={o.type}  price={o.price_open}")
    print()

    if not bot_positions and not bot_orders:
        print("Nothing to do — no bot positions or pendings found.")
        mt5.shutdown()
        return 0

    answer = input("Proceed with flatten? Type 'YES' to confirm: ").strip()
    if answer != "YES":
        print("Aborted. No action taken.")
        mt5.shutdown()
        return 0

    # Close positions at market
    closed = 0
    failed_close = 0
    for p in bot_positions:
        # Close = opposite-side market order for same volume
        tick = mt5.symbol_info_tick(config.SYMBOL)
        if tick is None:
            print(f"  No tick for {p.ticket} — skipping")
            failed_close += 1
            continue
        if p.type == mt5.POSITION_TYPE_BUY:
            close_type = mt5.ORDER_TYPE_SELL
            close_price = tick.bid
        else:
            close_type = mt5.ORDER_TYPE_BUY
            close_price = tick.ask
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": p.ticket,
            "symbol": config.SYMBOL,
            "volume": p.volume,
            "type": close_type,
            "price": close_price,
            "deviation": 50,
            "magic": config.MAGIC,
            "comment": "EMERGENCY_FLATTEN",
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(req)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"  ✓ Closed {p.ticket}")
            closed += 1
        else:
            print(f"  ✗ FAILED to close {p.ticket}: {res}")
            failed_close += 1

    # Cancel pendings
    cancelled = 0
    failed_cancel = 0
    for o in bot_orders:
        req = {"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket}
        res = mt5.order_send(req)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"  ✓ Cancelled pending {o.ticket}")
            cancelled += 1
        else:
            print(f"  ✗ FAILED to cancel {o.ticket}: {res}")
            failed_cancel += 1

    print()
    print(f"Closed: {closed}/{len(bot_positions)}   Cancelled: {cancelled}/{len(bot_orders)}")

    # ----- Final verification: re-query to confirm clean state -----
    # Don't trust individual order_send return codes alone. Broker may
    # accept the send but the order/position could still survive.
    print()
    print("Verifying final state by re-querying broker...")

    final_positions = mt5.positions_get(symbol=config.SYMBOL) or ()
    final_orders = mt5.orders_get(symbol=config.SYMBOL) or ()
    leftover_positions = [p for p in final_positions if p.magic == config.MAGIC]
    leftover_orders = [o for o in final_orders if o.magic == config.MAGIC]

    if leftover_positions or leftover_orders:
        print(f"❌ NOT CLEAN — items still present:")
        for p in leftover_positions:
            side = "LONG" if p.type == mt5.POSITION_TYPE_BUY else "SHORT"
            print(f"   Position {p.ticket}  {side}  vol={p.volume}  pnl=${p.profit:.2f}")
        for o in leftover_orders:
            print(f"   Pending {o.ticket}  type={o.type}  price={o.price_open}")
        print()
        print("⚠️  Manual intervention required in MT5 terminal.")
        print("    DO NOT restart the bot until this is resolved.")
        mt5.shutdown()
        return 2  # distinct exit code for partial flatten

    if failed_close or failed_cancel:
        # We had send-level failures but final state is clean.
        # Probably broker auto-cancelled the items we couldn't.
        print("⚠️  Some send-level failures but final state is clean.")
        print(f"   close failures: {failed_close}  cancel failures: {failed_cancel}")
        mt5.shutdown()
        return 0

    print("✓ Confirmed clean state — no bot positions or pendings remain.")
    mt5.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
