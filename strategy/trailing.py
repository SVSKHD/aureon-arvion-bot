"""
strategy/trailing.py — Trailing-stop logic for the XAUUSD anchor bot.

This file contains TWO trail systems that coexist:

1. LEGACY discrete-step lock
   Functions: calculate_lock_step(), calculate_new_sl(), should_update_sl()
   Side strings: "LONG" / "SHORT"
   Still used by:
     - strategy/rescue.py        (the rescue hedge position)
     - strategy/day_runner.py    (the dual-bracket manager, _manage_one_leg)
   DO NOT DELETE these — removing them breaks rescue.py and dual-bracket.

2. CONTINUOUS trail  (the new system, used by the main single-OCO position)
   Class + functions: TrailState, init_trail_state(), update_trail(),
                      should_log(), should_notify()
   Side strings: "BUY" / "SELL"
   Mode selected by config.TRAIL_MODE:
     "uncapped" — SL trails TRAIL_GAP behind the peak price, no maximum cap,
                  all the way to TP.
     "capped"   — discrete 9-step lock (re-implemented here as _capped_sl so the
                  continuous-trail entry point can fall back to old behavior).

The continuous trail is a strict one-way ratchet: the SL only ever moves in
the favorable direction, never backward.
"""

import math
from dataclasses import dataclass
from typing import Optional

import config


# ============================================================================
# SECTION 1 — LEGACY discrete-step lock
# Used by rescue.py and the dual-bracket manager. Side strings: "LONG"/"SHORT".
# ============================================================================

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


# ============================================================================
# SECTION 2 — CONTINUOUS trail
# Used by the main single-OCO position in day_runner. Side strings: "BUY"/"SELL".
# ============================================================================

# XAUUSD quotes carry 2 decimal places on this broker.
_PRICE_DIGITS = 2


def _round(price: float) -> float:
    return round(price, _PRICE_DIGITS)


@dataclass
class TrailState:
    """Mutable trailing-stop state for a single open position."""
    side: str                      # "BUY" or "SELL"
    entry_price: float
    initial_sl: float
    current_sl: float
    max_favorable_price: float     # best price seen: highest for BUY, lowest for SELL
    be_armed: bool = False         # True once SL has reached breakeven or better
    last_logged_locked: float = 0.0
    last_tg_locked: float = 0.0

    # ---- derived values -------------------------------------------------
    @property
    def favorable_distance(self) -> float:
        """How far price has travelled in our favor from entry (>= 0)."""
        if self.side == "BUY":
            return max(0.0, self.max_favorable_price - self.entry_price)
        return max(0.0, self.entry_price - self.max_favorable_price)

    @property
    def locked_distance(self) -> float:
        """Profit distance the current SL guarantees. Negative until BE armed."""
        if self.side == "BUY":
            return self.current_sl - self.entry_price
        return self.entry_price - self.current_sl

    def locked_usd(self, lot_size: float, contract: float = 100.0) -> float:
        """Locked profit in account currency. Gold contract = 100 oz per lot."""
        return round(self.locked_distance * lot_size * contract, 2)


def init_trail_state(side: str, entry_price: float, initial_sl: float) -> TrailState:
    """Create a fresh TrailState at position open."""
    return TrailState(
        side=side,
        entry_price=entry_price,
        initial_sl=initial_sl,
        current_sl=initial_sl,
        max_favorable_price=entry_price,
    )


def update_trail(state: TrailState, bid: float, ask: float) -> Optional[float]:
    """
    Update the trailing stop given the latest tick.

    Mutates `state` (max_favorable_price, current_sl, be_armed).
    Returns the new SL price if the broker order should be modified,
    or None if nothing changes this tick.
    """
    # 1. Update the peak favorable price using the side-correct quote.
    #    A BUY is exited at the bid; a SELL is exited at the ask.
    if state.side == "BUY":
        if bid > state.max_favorable_price:
            state.max_favorable_price = bid
    else:
        if ask < state.max_favorable_price:
            state.max_favorable_price = ask

    fav = state.favorable_distance

    # 2. Danger zone — not enough favorable movement yet. SL stays at initial.
    if fav < config.TRAIL_BE_TRIGGER:
        return None

    # 3. Candidate SL for the active mode.
    if config.TRAIL_MODE == "uncapped":
        candidate = _uncapped_sl(state)
    else:
        candidate = _capped_sl(state)

    if candidate is None:
        return None
    candidate = _round(candidate)

    # 4. One-way ratchet — only move the SL favorably, and only if the move
    #    is at least TRAIL_MIN_STEP (prevents broker-modify spam every tick).
    if state.side == "BUY":
        if candidate > state.current_sl + config.TRAIL_MIN_STEP - 1e-9:
            state.current_sl = candidate
            state.be_armed = True
            return candidate
    else:
        if candidate < state.current_sl - config.TRAIL_MIN_STEP + 1e-9:
            state.current_sl = candidate
            state.be_armed = True
            return candidate

    return None


def _uncapped_sl(state: TrailState) -> float:
    """
    Continuous trail: SL sits TRAIL_GAP behind the peak price, clamped so it
    never sits worse than breakeven. No maximum — trails all the way to TP.
    """
    if state.side == "BUY":
        return max(state.entry_price, state.max_favorable_price - config.TRAIL_GAP)
    return min(state.entry_price, state.max_favorable_price + config.TRAIL_GAP)


def _capped_sl(state: TrailState) -> Optional[float]:
    """Original discrete 9-step lock (capped at LOCK_STEPS_COUNT)."""
    fav = state.favorable_distance
    step = int(fav / config.LOCK_STEP)
    if step < 1:
        return None
    step = min(step, config.LOCK_STEPS_COUNT)
    locked = (step - 1) * config.LOCK_STEP
    if state.side == "BUY":
        return state.entry_price + locked
    return state.entry_price - locked


# ---- throttle helpers for the day_runner --------------------------------

def should_log(state: TrailState) -> bool:
    """True if locked profit advanced >= TRAIL_LOG_INTERVAL since last log."""
    locked = state.locked_distance
    if locked - state.last_logged_locked >= config.TRAIL_LOG_INTERVAL - 1e-9:
        state.last_logged_locked = locked
        return True
    return False


def should_notify(state: TrailState) -> bool:
    """True if locked profit advanced >= TRAIL_TG_INTERVAL since last Telegram."""
    locked = state.locked_distance
    if locked - state.last_tg_locked >= config.TRAIL_TG_INTERVAL - 1e-9:
        state.last_tg_locked = locked
        return True
    return False