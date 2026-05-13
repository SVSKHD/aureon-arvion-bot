"""Order placement, modification, cancellation."""

from dataclasses import dataclass
from typing import Optional, Tuple

import MetaTrader5 as mt5

import config
from core.exceptions import HaltBot
from core.logger import get_logger
from core.mt5_client import get_bot_orders, get_symbol_info

log = get_logger()

# Acceptable retcodes for pending order placement.
# Some brokers return PLACED (10008), others DONE (10009) for the same operation.
# Both signify successful queuing of the pending order.
PENDING_PLACEMENT_OK = {
    mt5.TRADE_RETCODE_DONE,
    mt5.TRADE_RETCODE_PLACED,
}


def _effective_lot_size() -> float:
    """
    Return the per-leg lot size:
      - Dual-bracket OFF → LOT_SIZE (single trade uses full lot)
      - Dual-bracket ON, override set → DUAL_BRACKET_LOT_SIZE
      - Dual-bracket ON, no override → LOT_SIZE (combined exposure = 2×LOT_SIZE)
    """
    if getattr(config, "DUAL_BRACKET_ENABLED", False):
        override = getattr(config, "DUAL_BRACKET_LOT_SIZE", None)
        if override is not None:
            return float(override)
    return float(config.LOT_SIZE)


@dataclass
class OrderLevels:
    """Computed prices for the OCO pair given an anchor."""
    anchor: float
    long_entry: float
    long_sl: float
    long_tp: float
    short_entry: float
    short_sl: float
    short_tp: float


def build_order_prices(anchor_price: float) -> OrderLevels:
    """Compute entry/SL/TP for both sides from anchor."""
    sym = get_symbol_info()
    digits = sym.digits if sym else 2

    long_entry = round(anchor_price + config.TRIGGER_DIST, digits)
    long_sl = round(long_entry - config.SL_DIST, digits)
    long_tp = round(long_entry + config.TP_DIST, digits)

    short_entry = round(anchor_price - config.TRIGGER_DIST, digits)
    short_sl = round(short_entry + config.SL_DIST, digits)
    short_tp = round(short_entry - config.TP_DIST, digits)

    return OrderLevels(
        anchor=anchor_price,
        long_entry=long_entry,
        long_sl=long_sl,
        long_tp=long_tp,
        short_entry=short_entry,
        short_sl=short_sl,
        short_tp=short_tp,
    )


def _place_buy_stop(levels: OrderLevels) -> Optional[int]:
    """Place buy stop only. Returns ticket or None on failure."""
    req = {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": config.SYMBOL,
        "volume": _effective_lot_size(),
        "type": mt5.ORDER_TYPE_BUY_STOP,
        "price": levels.long_entry,
        "sl": levels.long_sl,
        "tp": levels.long_tp,
        "deviation": 20,
        "magic": config.MAGIC,
        "comment": f"{config.COMMENT}_LONG",
        "type_time": mt5.ORDER_TIME_DAY,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(req)
    if res and res.retcode in PENDING_PLACEMENT_OK:
        log.info(f"Buy stop placed: ticket {res.order}")
        return res.order
    log.error(f"Buy stop FAILED: {res}")
    return None


def _place_sell_stop(levels: OrderLevels) -> Optional[int]:
    """Place sell stop only. Returns ticket or None on failure."""
    req = {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": config.SYMBOL,
        "volume": _effective_lot_size(),
        "type": mt5.ORDER_TYPE_SELL_STOP,
        "price": levels.short_entry,
        "sl": levels.short_sl,
        "tp": levels.short_tp,
        "deviation": 20,
        "magic": config.MAGIC,
        "comment": f"{config.COMMENT}_SHORT",
        "type_time": mt5.ORDER_TIME_DAY,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(req)
    if res and res.retcode in PENDING_PLACEMENT_OK:
        log.info(f"Sell stop placed: ticket {res.order}")
        return res.order
    log.error(f"Sell stop FAILED: {res}")
    return None


def place_pending_orders(
    levels: OrderLevels,
    allow_long: bool = True,
    allow_short: bool = True,
) -> Tuple[Optional[int], Optional[int]]:
    """
    Place pending order(s) based on side decision.

    BOTH sides allowed (default): OCO with strict abort-on-partial.
      - Buy fails → don't attempt sell → (None, None)
      - Buy succeeds, sell fails → cancel buy → (None, None)
      - Both succeed → (buy_ticket, sell_ticket)

    Only LONG allowed: place buy stop only → (buy_ticket, None)
    Only SHORT allowed: place sell stop only → (None, sell_ticket)
    Neither allowed: → (None, None)

    Guarantees: never leaves a one-sided OCO exposure UNLESS one-sided was
    explicitly requested via filter (allow_long XOR allow_short).
    """
    if not allow_long and not allow_short:
        log.info("Both sides filtered out — placing nothing.")
        return None, None

    # SHORT-ONLY path
    if not allow_long and allow_short:
        ticket = _place_sell_stop(levels)
        return None, ticket

    # LONG-ONLY path
    if allow_long and not allow_short:
        ticket = _place_buy_stop(levels)
        return ticket, None

    # BOTH (standard OCO) path
    buy_ticket = _place_buy_stop(levels)
    if buy_ticket is None:
        log.error("Aborting OCO (sell not attempted).")
        return None, None

    sell_ticket = _place_sell_stop(levels)
    if sell_ticket is None:
        log.error(f"Cancelling buy {buy_ticket} to avoid one-sided exposure.")
        cancel_ok = cancel_order(buy_ticket)
        if not cancel_ok:
            # Verify: maybe broker already cancelled it
            remaining = get_bot_orders()
            if remaining is None:
                # CRITICAL — cancel failed AND we can't verify state.
                msg = (
                    f"CRITICAL: sell failed, buy cancel failed, "
                    f"AND orders_get failed. Buy ticket {buy_ticket} state UNKNOWN. "
                    f"Halting — manual review required immediately."
                )
                log.error(msg)
                raise HaltBot(msg)
            still_live = any(o.ticket == buy_ticket for o in remaining)
            if still_live:
                msg = (
                    f"CRITICAL: sell stop failed AND buy cancel failed. "
                    f"Buy ticket {buy_ticket} is still LIVE. Halting for manual review."
                )
                log.error(msg)
                raise HaltBot(msg)
            else:
                log.info(
                    f"Buy ticket {buy_ticket} not in orders list — broker already cancelled."
                )
        return None, None

    log.info(f"Sell stop placed: ticket {sell_ticket}")
    return buy_ticket, sell_ticket


def cancel_order(ticket: Optional[int]) -> bool:
    """Cancel a pending order. Returns True on success."""
    if ticket is None:
        return False
    req = {"action": mt5.TRADE_ACTION_REMOVE, "order": ticket}
    res = mt5.order_send(req)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"Cancelled pending order {ticket}")
        return True
    log.warning(f"Failed to cancel order {ticket}: {res}")
    return False


def modify_position_sl(ticket: int, new_sl: float, tp: float) -> bool:
    """
    Update SL (and keep current TP) on an open position.

    Safety: refuses to move SL backward (i.e., away from current price).
    For LONG, new_sl must be greater than current SL.
    For SHORT, new_sl must be less than current SL.
    Initial set (current SL = 0 / not set) is always allowed.
    """
    sym = get_symbol_info()
    digits = sym.digits if sym else 2
    new_sl_rounded = round(new_sl, digits)

    # Read current position state to check direction-aware SL safety
    positions = mt5.positions_get(ticket=ticket)
    if positions:
        p = positions[0]
        current_sl = p.sl
        is_long = p.type == mt5.POSITION_TYPE_BUY
        if current_sl and current_sl > 0:
            if is_long and new_sl_rounded <= current_sl:
                log.warning(
                    f"SL backward-move blocked (LONG {ticket}): "
                    f"new {new_sl_rounded} not greater than current {current_sl}"
                )
                return False
            if (not is_long) and new_sl_rounded >= current_sl:
                log.warning(
                    f"SL backward-move blocked (SHORT {ticket}): "
                    f"new {new_sl_rounded} not less than current {current_sl}"
                )
                return False

    req = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "symbol": config.SYMBOL,
        "sl": new_sl_rounded,
        "tp": round(tp, digits),
    }
    res = mt5.order_send(req)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        return True
    log.warning(f"SL modify failed for position {ticket}: {res}")
    return False


def modify_position_sl_tp(ticket: int, new_sl: float, new_tp: float) -> bool:
    """
    Modify BOTH SL and TP for an active position.

    Used immediately after a pending order fills so that TP/SL are anchored to
    the ACTUAL filled price (position.price_open), not the planned pending price.
    MT5 fills can slip; this normalization keeps backtest assumptions intact.
    """
    sym = get_symbol_info()
    if sym is None:
        log.warning("Cannot modify SL/TP: symbol_info unavailable")
        return False
    digits = sym.digits
    req = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "symbol": config.SYMBOL,
        "sl": round(new_sl, digits),
        "tp": round(new_tp, digits),
        "magic": config.MAGIC,
        "comment": f"{config.COMMENT}_NORMALIZE",
    }
    res = mt5.order_send(req)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(
            f"Normalized TP/SL from filled price | "
            f"ticket={ticket} sl={round(new_sl, digits)} tp={round(new_tp, digits)}"
        )
        return True
    # retcode 10025 = TRADE_RETCODE_NO_CHANGES — MT5 reports the position
    # already has the requested SL/TP. This happens when fill price equals
    # the pending trigger exactly (zero slippage), so the pending-order SL/TP
    # ARE the post-fill SL/TP. Treat as success.
    if res and res.retcode == 10025:
        log.info(
            f"TP/SL already at requested values (zero slippage) | "
            f"ticket={ticket} sl={round(new_sl, digits)} tp={round(new_tp, digits)} "
            f"— treating as normalized."
        )
        return True
    log.warning(f"Failed to normalize TP/SL for {ticket}: {res}")
    return False


def cancel_bot_pending_orders() -> Optional[int]:
    """
    Cancel ALL bot-owned pending orders.

    Returns:
      int  — number cancelled (0 if no orders existed, N if all N cancelled)
      None — Either MT5 query failed, OR partial cancellation (some still live).
             Caller MUST treat None as UNKNOWN/UNSAFE and HaltBot.
    """
    orders = get_bot_orders()
    if orders is None:
        log.error("Cannot cancel pendings: MT5 orders query failed. State unknown.")
        return None
    cancelled = 0
    for order in orders:
        if cancel_order(order.ticket):
            cancelled += 1
    if cancelled < len(orders):
        log.error(
            f"PARTIAL cancellation: {cancelled}/{len(orders)} cancelled. "
            f"{len(orders) - cancelled} pending(s) MAY STILL BE LIVE. "
            f"Returning None to force caller to halt."
        )
        return None
    return cancelled