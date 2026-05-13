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


def wait_until(target_dt: datetime, log_interval_minutes: int = 60) -> None:
    """
    Block until broker time >= target_dt. Polls with adaptive sleep.

    Logs a countdown every `log_interval_minutes` (default 60) so long waits
    (e.g. 21h until next anchor) don't appear as a frozen bot.

    Args:
        target_dt: broker-time datetime to wait for
        log_interval_minutes: how often to log remaining time (default 60min)
    """
    log_interval_sec = log_interval_minutes * 60
    # Set initial last_log so first periodic log fires after one interval
    # (the caller has likely already logged "next anchor in X hr" right before)
    last_log_ts = time_module.time()

    while True:
        now = get_server_time()
        if now is None:
            time_module.sleep(2)
            continue
        if now >= target_dt:
            log.info(f"⏰ Target time reached: {target_dt} (broker). Proceeding.")
            return
        wait_seconds = (target_dt - now).total_seconds()

        # Periodic countdown logging (file + console)
        current_ts = time_module.time()
        if current_ts - last_log_ts >= log_interval_sec:
            hours = int(wait_seconds // 3600)
            minutes = int((wait_seconds % 3600) // 60)
            log.info(
                f"⏳ Waiting for anchor at {target_dt} (broker). "
                f"Remaining: {hours}h {minutes:02d}m | "
                f"Server now: {now.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            last_log_ts = current_ts

        # Cap sleep at 30s so we resync time regularly + check log interval
        sleep_for = min(wait_seconds, 30)
        if sleep_for > 0:
            time_module.sleep(sleep_for)