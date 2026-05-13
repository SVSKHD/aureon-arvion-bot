"""
Pre-trade filters that decide which OCO sides to allow on a given day.

Currently:
  - ATR-based directional filter (volatility → side bias)

Future additions can extend SideDecision composition (multiple filters AND'd).
"""

from dataclasses import dataclass
from typing import Optional

import MetaTrader5 as mt5

import config
from core.logger import get_logger

log = get_logger()


@dataclass
class SideDecision:
    """Which OCO sides to place pending orders for."""
    allow_long: bool
    allow_short: bool
    reason: str
    atr_value: Optional[float] = None  # observed ATR, for telemetry

    @classmethod
    def both(cls, reason: str = "no filter active", atr: Optional[float] = None) -> "SideDecision":
        return cls(True, True, reason, atr)

    @classmethod
    def short_only(cls, reason: str, atr: Optional[float] = None) -> "SideDecision":
        return cls(False, True, reason, atr)

    @classmethod
    def long_only(cls, reason: str, atr: Optional[float] = None) -> "SideDecision":
        return cls(True, False, reason, atr)

    @classmethod
    def none(cls, reason: str, atr: Optional[float] = None) -> "SideDecision":
        return cls(False, False, reason, atr)


# ----------------------------------------------------------------------------
# ATR
# ----------------------------------------------------------------------------

_TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
}


def _resolve_timeframe() -> int:
    tf_str = getattr(config, "ATR_TIMEFRAME", "M15")
    return _TIMEFRAME_MAP.get(tf_str, mt5.TIMEFRAME_M15)


def calculate_atr(period: int = None) -> Optional[float]:
    """
    Calculate ATR (Average True Range) on the configured timeframe.

    Uses (period+1) most recent bars (previous close needed for TR calc).
    Simple Moving Average of TR (not Wilder smoothing — simpler, sufficient).
    Returns ATR in price units (USD for XAUUSD), or None if data unavailable.
    """
    if period is None:
        period = getattr(config, "ATR_PERIOD", 14)
    tf = _resolve_timeframe()

    bars = mt5.copy_rates_from_pos(config.SYMBOL, tf, 0, period + 1)
    if bars is None or len(bars) < period + 1:
        log.warning(
            f"ATR: insufficient bars ({len(bars) if bars is not None else 0} "
            f"available, need {period + 1})"
        )
        return None

    trs = []
    for i in range(1, period + 1):
        high = float(bars[i]['high'])
        low = float(bars[i]['low'])
        prev_close = float(bars[i - 1]['close'])
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        trs.append(tr)
    return sum(trs) / period


def get_side_decision() -> SideDecision:
    """
    Top-level filter decision. Currently uses ATR only.

    Always calculates ATR (for observability). Only ACTS on it if
    ATR_FILTER_ENABLED is True.
    """
    period = getattr(config, "ATR_PERIOD", 14)
    tf_str = getattr(config, "ATR_TIMEFRAME", "M15")
    atr = calculate_atr(period)

    if atr is None:
        log.warning("ATR unavailable — allowing both sides")
        return SideDecision.both("ATR unavailable", atr=None)

    log.info(f"ATR({period}) on {tf_str}: ${atr:.2f}")

    if not getattr(config, "ATR_FILTER_ENABLED", False):
        return SideDecision.both(
            f"filter disabled (ATR observed: ${atr:.2f})",
            atr=atr,
        )

    high_th = getattr(config, "ATR_HIGH_THRESHOLD", None)
    low_th = getattr(config, "ATR_LOW_THRESHOLD", None)

    if high_th is not None and atr > high_th:
        return SideDecision.short_only(
            f"ATR ${atr:.2f} > high threshold ${high_th:.2f} → SHORT only",
            atr=atr,
        )
    if low_th is not None and atr < low_th:
        return SideDecision.long_only(
            f"ATR ${atr:.2f} < low threshold ${low_th:.2f} → LONG only",
            atr=atr,
        )
    return SideDecision.both(
        f"ATR ${atr:.2f} in mid range (${low_th}–${high_th}) → both sides",
        atr=atr,
    )