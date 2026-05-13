"""Safety check functions: spread, daily loss, symbol tradeability, stop distance."""

from datetime import datetime
from typing import Optional, Tuple

import MetaTrader5 as mt5

import config
from core.logger import get_logger
from core.mt5_client import (
    get_account_info,
    get_server_time,
    get_spread_usd,
    get_symbol_info,
    history_deals_with_retry,
)

log = get_logger()


def check_spread_allowed() -> Tuple[bool, Optional[float]]:
    """
    Returns (allowed, current_spread_usd).
    allowed=False if spread exceeds MAX_SPREAD_USD or tick unavailable.
    """
    spread = get_spread_usd()
    if spread is None:
        log.warning("Cannot read spread (no tick).")
        return False, None
    if spread > config.MAX_SPREAD_USD:
        return False, spread
    return True, spread


def check_daily_loss() -> Tuple[bool, float]:
    """
    Returns (limit_hit, today_bot_pnl_usd).
    Calculates today's PnL from bot deals only (MAGIC match).

    Uses BROKER server time for the day window — your VPS/Windows time may
    differ from broker time (e.g. IST vs EET), so local 'today' could
    mis-align with broker 'today' and miss/duplicate the loss check.
    """
    info = get_account_info()
    if info is None:
        return False, 0.0

    server_now = get_server_time()
    if server_now is None:
        log.warning("Cannot check daily loss: server time unavailable.")
        return False, 0.0

    today_start = datetime.combine(server_now.date(), datetime.min.time())
    deals = history_deals_with_retry(today_start, server_now)
    if deals is None:
        # Query FAILED. Be safe: assume worst (limit hit) and block trading.
        # Empty result (no deals today) is fine — see check below.
        log.error(
            f"history_deals_get failed: {mt5.last_error()}. "
            f"Blocking trading for safety (cannot verify daily PnL)."
        )
        return True, 0.0
    if len(deals) == 0:
        return False, 0.0

    today_pnl = sum(
        d.profit + d.commission + d.swap
        for d in deals
        if d.magic == config.MAGIC
    )

    if not info.balance:
        return False, today_pnl

    if today_pnl < 0:
        loss_pct = abs(today_pnl) / info.balance * 100
        if loss_pct >= config.MAX_DAILY_LOSS_PCT:
            return True, today_pnl

    return False, today_pnl


def validate_symbol_tradeability() -> bool:
    """Verify the symbol is currently tradeable."""
    sym = get_symbol_info()
    if sym is None:
        log.error("Symbol info unavailable.")
        return False
    if sym.trade_mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
        log.error(f"Trading disabled for {config.SYMBOL}.")
        return False
    return True


def validate_stop_distance(entry: float, sl: float, tp: float) -> bool:
    """
    Warn if SL/TP is closer than broker's minimum stops_level requirement.
    Returns True if OK, False if too close (warning logged either way).
    """
    sym = get_symbol_info()
    if sym is None or sym.point == 0:
        return True

    min_dist_usd = sym.trade_stops_level * sym.point
    if min_dist_usd <= 0:
        return True

    sl_dist = abs(entry - sl)
    tp_dist = abs(entry - tp)

    if sl_dist < min_dist_usd or tp_dist < min_dist_usd:
        log.warning(
            f"Stops may be too close: broker min={min_dist_usd:.2f} USD, "
            f"SL_dist={sl_dist:.2f}, TP_dist={tp_dist:.2f}. Order may be rejected."
        )
        return False
    return True
