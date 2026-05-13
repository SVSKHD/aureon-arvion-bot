"""Daily anchor capture: read open of 02:00 M5 bar in broker time."""

from datetime import date
from typing import Optional

import MetaTrader5 as mt5
import pandas as pd

import config
from core.logger import get_logger

log = get_logger()


def get_anchor_bar(target_date: date) -> Optional[dict]:
    """
    Fetch the ANCHOR_HOUR:00 M5 candle for target_date (in broker server time).
    Returns the bar as a dict or None if not found.

    Uses copy_rates_from_pos() which fetches the most recent N bars without
    needing a datetime argument — eliminates local-vs-server timezone bugs.
    Pandas decodes the bars' Unix timestamps as naive UTC datetimes, which
    line up with broker time on MT5 (MT5 stores server time as Unix-style
    seconds). target_date must also be a broker-server-time date.
    """
    # 288 M5 bars = 24 hours of data — always enough to cover today's anchor
    # and yesterday's for boundary safety.
    bars = mt5.copy_rates_from_pos(
        config.SYMBOL, mt5.TIMEFRAME_M5, 0, 24 * 12
    )
    if bars is None or len(bars) == 0:
        log.error("Failed to fetch M5 bars from MT5.")
        return None

    df = pd.DataFrame(bars)
    df["time"] = pd.to_datetime(df["time"], unit="s")

    # Debug log: show the time range of fetched bars so timezone issues are obvious
    earliest = df["time"].min()
    latest = df["time"].max()
    log.info(
        f"Fetched {len(df)} M5 bars | range: {earliest} → {latest} (broker time)"
    )

    match = df[
        (df["time"].dt.date == target_date)
        & (df["time"].dt.hour == config.ANCHOR_HOUR)
        & (df["time"].dt.minute == 0)
    ]

    if match.empty:
        log.warning(
            f"No {config.ANCHOR_HOUR:02d}:00 M5 candle for {target_date} in "
            f"fetched data (range {earliest} → {latest})."
        )
        return None

    return match.iloc[0].to_dict()


def capture_anchor_price(target_date: date) -> Optional[float]:
    """Return open price of today's ANCHOR_HOUR:00 anchor candle (or None)."""
    bar = get_anchor_bar(target_date)
    if bar is None:
        return None
    anchor = float(bar["open"])
    log.info(f"Anchor captured: ${anchor:.2f} at {bar['time']}")
    return anchor
