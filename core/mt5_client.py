"""MT5 connection and centralized query wrappers."""

import time
from datetime import datetime
from typing import Any, List, Optional

import MetaTrader5 as mt5

import config
from core.logger import get_logger

log = get_logger()


def history_deals_with_retry(
    from_time: datetime,
    to_time: datetime,
    retries: int = 3,
    delay_sec: float = 1.0,
) -> Optional[Any]:
    """
    Wrapper around mt5.history_deals_get with transient-failure retry.

    Returns:
      tuple of deal objects (possibly empty) on success
      None if ALL retries failed — caller should treat as unknown state

    Empty tuple (no matching deals) is returned immediately; only None
    triggers retry. This way we don't slow down legitimate "no deals yet"
    cases by retrying on them.
    """
    for attempt in range(1, retries + 1):
        deals = mt5.history_deals_get(from_time, to_time)
        if deals is not None:
            if attempt > 1:
                log.info(f"history_deals_get succeeded on retry {attempt}/{retries}")
            return deals
        if attempt < retries:
            log.warning(
                f"history_deals_get returned None (attempt {attempt}/{retries}): "
                f"{mt5.last_error()}. Retrying in {delay_sec}s..."
            )
            time.sleep(delay_sec)
    log.error(
        f"history_deals_get FAILED after {retries} retries: {mt5.last_error()}"
    )
    return None


def connect_mt5() -> bool:
    """Initialize MT5 and validate symbol. Returns True on success."""
    if not mt5.initialize():
        log.error(f"MT5 initialize failed: {mt5.last_error()}")
        return False

    info = mt5.account_info()
    if info is None:
        log.error("Not logged in to MT5 terminal.")
        mt5.shutdown()
        return False

    sym = mt5.symbol_info(config.SYMBOL)
    if sym is None:
        log.error(
            f"Symbol '{config.SYMBOL}' not found. "
            f"Check your broker's ticker (e.g. XAUUSDm, GOLD)."
        )
        mt5.shutdown()
        return False

    if not sym.visible:
        log.warning(f"Adding {config.SYMBOL} to Market Watch.")
        if not mt5.symbol_select(config.SYMBOL, True):
            log.error(f"Failed to select symbol {config.SYMBOL}")
            mt5.shutdown()
            return False
        sym = mt5.symbol_info(config.SYMBOL)

    mode_map = {0: "DEMO", 1: "CONTEST", 2: "LIVE"}
    mode = mode_map.get(info.trade_mode, f"UNKNOWN({info.trade_mode})")

    log.info(f"Connected | Account: {info.login} | Broker: {info.server}")
    log.info(f"Balance: ${info.balance:.2f} | Equity: ${info.equity:.2f} | Mode: {mode}")
    log.info(
        f"Symbol: {config.SYMBOL} | Digits: {sym.digits} | Point: {sym.point} | "
        f"Stops level (min dist): {sym.trade_stops_level} pts"
    )
    return True


def disconnect_mt5() -> None:
    mt5.shutdown()
    log.info("Disconnected from MT5.")


def get_server_time() -> Optional[datetime]:
    """
    Return current broker server time (via latest tick).

    Uses utcfromtimestamp() so the returned datetime represents broker time
    consistently with how pandas decodes mt5.copy_rates_from*() output.
    Both produce naive datetimes that represent broker server time.
    """
    tick = mt5.symbol_info_tick(config.SYMBOL)
    if tick is None:
        return None
    return datetime.utcfromtimestamp(tick.time)


def get_symbol_info() -> Optional[Any]:
    return mt5.symbol_info(config.SYMBOL)


def get_tick() -> Optional[Any]:
    return mt5.symbol_info_tick(config.SYMBOL)


def get_spread_usd() -> Optional[float]:
    """Return current spread in USD price units."""
    tick = get_tick()
    if tick is None:
        return None
    return tick.ask - tick.bid


def get_account_info() -> Optional[Any]:
    return mt5.account_info()


def get_bot_positions() -> Optional[List[Any]]:
    """
    Return open bot positions (filtered by MAGIC + SYMBOL).

    Returns None if the MT5 query itself failed (call this distinct from
    an empty list which means "no positions"). Callers should treat None
    as "unknown — retry next iteration" rather than "no position".
    """
    raw = mt5.positions_get(symbol=config.SYMBOL)
    if raw is None:
        log.warning(f"positions_get failed: {mt5.last_error()}")
        return None
    return [p for p in raw if p.magic == config.MAGIC]


def get_bot_orders() -> Optional[List[Any]]:
    """
    Return pending bot orders (filtered by MAGIC + SYMBOL).

    Returns None on MT5 query failure (distinct from empty list).
    """
    raw = mt5.orders_get(symbol=config.SYMBOL)
    if raw is None:
        log.warning(f"orders_get failed: {mt5.last_error()}")
        return None
    return [o for o in raw if o.magic == config.MAGIC]
