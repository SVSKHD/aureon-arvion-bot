"""Trailing stop / progressive lock logic. Pure functions — no MT5 calls."""

import math
from typing import Optional

import config


# Float epsilon to absorb broker-price subtraction drift.
# Example: 4642.50 + 0.90 - 4642.50 = 0.89999999999963620
# Without EPSILON, int(0.899.../0.30)-1 = 1, missing the +0.90 lock entirely.
EPSILON = 1e-9


def calculate_lock_step(favorable_move: float) -> int:
    """
    Given how far price has moved favorably from entry (in USD),
    return the highest lock step index achieved (0-indexed).

    Returns:
        -1 = no lock yet (move < LOCK_STEP)
         0 = +0.30 reached
         1 = +0.60 reached
         2 = +0.90 reached
         ...
         8 = +2.70 reached (capped at LOCK_STEPS_COUNT - 1)
    """
    if favorable_move + EPSILON < config.LOCK_STEP:
        return -1

    steps_reached = math.floor((favorable_move + EPSILON) / config.LOCK_STEP)
    idx = steps_reached - 1
    return min(idx, config.LOCK_STEPS_COUNT - 1)


def calculate_new_sl(entry: float, side: str, lock_step_idx: int) -> Optional[float]:
    """
    Given lock step index, return the new SL price.

    LONG:  SL = entry + lock_offset  (locks profit above entry)
    SHORT: SL = entry - lock_offset  (locks profit below entry)
    Returns None if lock_step_idx < 0 or unknown side.
    """
    if lock_step_idx < 0:
        return None

    lock_offset = config.LOCK_STEP * (lock_step_idx + 1)

    if side == "LONG":
        return entry + lock_offset
    if side == "SHORT":
        return entry - lock_offset
    return None


def should_update_sl(current_idx: int, new_idx: int) -> bool:
    """Only update SL if a NEW higher lock step has been reached (never backwards)."""
    return new_idx > current_idx
