"""Time helpers — broker-time aware."""

import time as time_module
from datetime import datetime, timedelta

import config
from core.logger import get_logger
from core.mt5_client import get_server_time

log = get_logger()


def is_weekend(dt: datetime) -> bool:
    """Saturday (5) or Sunday (6)."""
    return dt.weekday() >= 5


def get_next_anchor_time() -> datetime:
    """Return next 02:00 broker-time datetime (or fallback to local if MT5 unavailable)."""
    now = get_server_time()
    if now is None:
        now = datetime.now()
    target = now.replace(
        hour=config.ANCHOR_HOUR, minute=0, second=0, microsecond=0
    )
    # If we're already past today's anchor, roll to tomorrow
    if now > target + timedelta(minutes=1):
        target += timedelta(days=1)
    return target


def wait_until(target_dt: datetime) -> None:
    """Block until broker time >= target_dt. Polls with adaptive sleep."""
    while True:
        now = get_server_time()
        if now is None:
            time_module.sleep(2)
            continue
        if now >= target_dt:
            return
        wait_seconds = (target_dt - now).total_seconds()
        # Cap sleep at 30s so we resync time regularly
        sleep_for = min(wait_seconds, 30)
        if sleep_for > 0:
            time_module.sleep(sleep_for)
