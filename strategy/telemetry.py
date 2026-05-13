"""
Rich Telegram telemetry for bot-managed positions.

All telemetry is filtered by SYMBOL + MAGIC. Manual trades or other bots'
positions are never reported.

Five event labels:
  ENTRY_FILLED       — once, immediately when a pending fills into a position
  TP_SL_NORMALIZED   — once, after TP/SL are corrected to actual filled price
  LOCK_REACHED       — each time a new trail lock step is applied
  POSITION_HEARTBEAT — periodic while position is open (configurable interval)
  POSITION_CLOSED    — once, when position closes (TP / SL-Trail / Manual)
"""

from datetime import datetime, timedelta
from typing import Any, Optional

import MetaTrader5 as mt5

import config
from core.logger import get_logger
from core.mt5_client import (
    get_account_info,
    get_server_time,
    get_tick,
    history_deals_with_retry,
)
from core.telegram_notifier import TelegramNotifier

log = get_logger()


# --- Formatting helpers --------------------------------------------------------

def _fmt(v: Optional[float], prec: int = 2) -> str:
    if v is None:
        return "n/a"
    return f"{v:.{prec}f}"


def _fmt_signed(v: Optional[float], prec: int = 2) -> str:
    if v is None:
        return "n/a"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.{prec}f}"


def _server_time_str() -> str:
    """Format broker server time. Uses utcfromtimestamp to match get_server_time()."""
    tick = get_tick()
    if tick is None:
        return "n/a"
    return datetime.utcfromtimestamp(tick.time).strftime("%Y-%m-%d %H:%M:%S")


# --- Live position telemetry ---------------------------------------------------

def send_position_telemetry(
    tg: TelegramNotifier,
    position: Any,
    side: str,
    label: str,
    entry_price: float,
    lock_step_idx: int = -1,
) -> None:
    """
    Send rich telemetry for an open bot-managed position.

    Args:
        tg:             Telegram notifier
        position:       MT5 position object (may have stale SL/TP — we re-fetch)
        side:           "LONG" or "SHORT"
        label:          ENTRY_FILLED | TP_SL_NORMALIZED | LOCK_REACHED | POSITION_HEARTBEAT
        entry_price:    ACTUAL filled price (preserved from position.price_open at detection)
        lock_step_idx:  Current trail lock step (-1 if none applied yet)
    """
    if not tg.enabled:
        return

    try:
        # Sanity: filter by SYMBOL + MAGIC even though caller should pre-filter
        if position.symbol != config.SYMBOL or position.magic != config.MAGIC:
            log.warning(
                f"Telemetry skipped — position {position.ticket} not bot-owned "
                f"(symbol={position.symbol}, magic={position.magic})"
            )
            return

        tick = get_tick()
        acct = get_account_info()

        # Market state
        bid: Optional[float] = None
        ask: Optional[float] = None
        spread: Optional[float] = None
        if tick is not None:
            bid = tick.bid
            ask = tick.ask
            spread = ask - bid

        # Fresh position state (SL/TP may have just been updated)
        sl: Optional[float] = position.sl
        tp: Optional[float] = position.tp
        floating_pnl: Optional[float] = None
        try:
            fresh = mt5.positions_get(ticket=position.ticket)
            if fresh:
                fp = fresh[0]
                sl = fp.sl
                tp = fp.tp
                floating_pnl = fp.profit + fp.swap
        except Exception:
            pass

        # Favorable move from ACTUAL filled entry
        favorable: Optional[float] = None
        if bid is not None and ask is not None:
            if side == "LONG":
                favorable = bid - entry_price
            else:
                favorable = entry_price - ask

        # Lock display
        if lock_step_idx >= 0:
            locked = config.LOCK_STEP * (lock_step_idx + 1)
            lock_step_disp = lock_step_idx + 1
        else:
            locked = 0.0
            lock_step_disp = 0

        # Account snapshot
        balance = acct.balance if acct else None
        equity = acct.equity if acct else None
        margin_level = (
            acct.margin_level if acct and acct.margin_level and acct.margin_level > 0
            else None
        )

        lines = [
            f"📡 POSITION TELEMETRY — {label}",
            f"Symbol: {position.symbol}",
            f"Ticket: {position.ticket}",
            f"Side: {side}",
            f"Lot: {position.volume:.2f}",
            "",
            f"Entry: {entry_price:.2f}",
            f"Bid: {_fmt(bid)}",
            f"Ask: {_fmt(ask)}",
            f"Spread: {_fmt(spread)}",
            "",
            f"Move: {_fmt_signed(favorable)}",
            f"Lock Step: {lock_step_disp}",
            f"Locked: +{locked:.2f}",
            f"SL: {_fmt(sl)}",
            f"TP: {_fmt(tp)}",
            "",
            f"Floating PnL: {_fmt_signed(floating_pnl)}",
            f"Balance: {_fmt(balance)}",
            f"Equity: {_fmt(equity)}",
        ]
        if margin_level is not None:
            lines.append(f"Margin Level: {margin_level:.2f}%")
        lines.append(f"Server Time: {_server_time_str()}")

        tg.send_message("\n".join(lines))

    except Exception as e:
        log.warning(f"Telemetry build error for {label}: {e}")


# --- Close telemetry -----------------------------------------------------------

def send_close_telemetry(
    tg: TelegramNotifier,
    ticket: int,
    side: Optional[str],
    entry_price: float,
    lock_step_idx: int,
) -> None:
    """
    Send POSITION_CLOSED telemetry by reading the closing deal from history.
    Reports exit price, final PnL, close reason, and lock state at close.
    """
    if not tg.enabled:
        return

    try:
        server_now = get_server_time()
        if server_now is None:
            server_now = datetime.now()  # fallback only
        from_time = server_now - timedelta(days=2)
        deals = history_deals_with_retry(from_time, server_now)

        if deals is None:
            # Query failed — send an explicit "query failed" message rather
            # than misleading "unknown close" details.
            log.error(
                f"history_deals_get FAILED for close telemetry (ticket {ticket}). "
                f"Telegram message will reflect query failure."
            )
            tg.send_message(
                f"❌ POSITION CLOSED — but history query FAILED\n\n"
                f"Ticket: {ticket}\n"
                f"Side: {side}\n"
                f"Entry: ${entry_price:.2f}\n\n"
                f"⚠️ Could not read exit price / PnL from broker history.\n"
                f"Check MT5 terminal manually for actual close details."
            )
            return

        matching = [
            d for d in deals
            if d.position_id == ticket
            and d.entry == mt5.DEAL_ENTRY_OUT
            and d.symbol == config.SYMBOL
            # NOTE: do NOT filter by magic. Broker-initiated SL/TP closes
            # have magic=0, not our EA magic. Position_id is unique per broker.
        ]

        exit_price: Optional[float] = None
        pnl: Optional[float] = None
        reason = "unknown"
        volume = 0.0

        if matching:
            matching.sort(key=lambda d: d.time_msc)
            last = matching[-1]
            exit_price = last.price
            pnl = sum(d.profit + d.commission + d.swap for d in matching)
            volume = sum(d.volume for d in matching)
            rc = last.reason
            if rc == mt5.DEAL_REASON_TP:
                reason = "TP"
            elif rc == mt5.DEAL_REASON_SL:
                reason = "SL / Trail"
            elif rc == mt5.DEAL_REASON_CLIENT:
                reason = "Manual"
            elif rc == mt5.DEAL_REASON_EXPERT:
                reason = "Expert/EA"
            else:
                reason = f"code {rc}"
            if len(matching) > 1:
                reason = f"{reason} ({len(matching)} partial exits)"

        # Move at close
        move: Optional[float] = None
        if exit_price is not None and side is not None:
            if side == "LONG":
                move = exit_price - entry_price
            else:
                move = entry_price - exit_price

        if lock_step_idx >= 0:
            locked = config.LOCK_STEP * (lock_step_idx + 1)
            lock_step_disp = lock_step_idx + 1
        else:
            locked = 0.0
            lock_step_disp = 0

        acct = get_account_info()
        balance = acct.balance if acct else None
        equity = acct.equity if acct else None

        lot_str = f"{volume:.2f}" if volume else f"{config.LOT_SIZE:.2f}"

        lines = [
            "📡 POSITION TELEMETRY — POSITION_CLOSED",
            f"Symbol: {config.SYMBOL}",
            f"Ticket: {ticket}",
            f"Side: {side}",
            f"Lot: {lot_str}",
            "",
            f"Entry: {entry_price:.2f}",
            f"Exit: {_fmt(exit_price)}",
            f"Move at close: {_fmt_signed(move)}",
            f"Final Lock Step: {lock_step_disp}",
            f"Locked: +{locked:.2f}",
            "",
            f"Final PnL: {_fmt_signed(pnl)}",
            f"Reason: {reason}",
            f"Balance: {_fmt(balance)}",
            f"Equity: {_fmt(equity)}",
            "",
            f"Server Time: {_server_time_str()}",
        ]
        tg.send_message("\n".join(lines))

    except Exception as e:
        log.warning(f"Close telemetry error: {e}")