"""
Rescue hedge logic.

When the original position reaches RESCUE_TRIGGER_ADVERSE (default $10) adverse,
open an opposite-direction market position to capture continued momentum.
Both positions then manage independently with same TP/SL/lock-step trail.

Backtest result (Jan-Apr 2026, 68 trades):
  - Without rescue: +$3,465
  - With rescue: +$3,885
  - 8 rescue fires: 6 helped, 2 neutral, 0 hurt

USAGE in day_runner.py
    rescue_mgr = RescueManager(active_side, entry_price, levels, tg)
    while True:
        # ... existing tick polling ...
        if active_position_closed:
            rescue_mgr.original_has_closed()
        rescue_mgr.tick(active_position, tick)
        if not active_position and rescue_mgr.is_done():
            break

REQUIREMENTS
  - Hedging account (not netting)
  - config.RESCUE_ENABLED = True
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import MetaTrader5 as mt5

import config
from core.logger import get_logger
from core.start_log import (
    record_rescue_triggered,
    record_rescue_filled,
    record_rescue_closed,
)
from core.telegram_notifier import TelegramNotifier
from strategy.orders import (
    OrderLevels,
    open_rescue_market_position,
    modify_position_sl,
)
from strategy.trailing import calculate_lock_step, calculate_new_sl, should_update_sl

log = get_logger()


# ----------------------------------------------------------------------------
# State machine for rescue position
# ----------------------------------------------------------------------------

@dataclass
class RescueState:
    """Tracks a single rescue position from trigger through close."""
    # Static at construction
    parent_side: str        # 'LONG' or 'SHORT' (the original)
    rescue_side: str        # opposite of parent
    trigger_price: float    # price at which rescue fired
    entry_planned: float    # what we asked for
    parent_entry: float     # original position's entry

    # Set when rescue fills
    ticket: Optional[int] = None
    entry_actual: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    initial_sl: Optional[float] = None

    # Runtime tracking
    current_sl: Optional[float] = None
    lock_idx: int = -1  # -1 = no lock, 0 = first lock step, etc.
    max_fav: float = 0.0
    filled: bool = False
    closed: bool = False
    close_reason: Optional[str] = None
    close_price: Optional[float] = None
    close_pnl: Optional[float] = None


# ----------------------------------------------------------------------------
# RescueManager
# ----------------------------------------------------------------------------

class RescueManager:
    """
    Manages the optional rescue hedge alongside an original position.

    Call .tick(active_position, tick) on every iteration of the trail loop.
    Call .original_has_closed() when the original position closes.
    Call .is_done() to know if the rescue is finished (so the outer loop can exit).
    """

    def __init__(
        self,
        active_side: str,
        active_entry: float,
        levels: OrderLevels,
        tg: TelegramNotifier,
    ):
        self.tg = tg
        self.levels = levels
        self.enabled = bool(getattr(config, "RESCUE_ENABLED", False))
        self.trigger_dist = float(getattr(config, "RESCUE_TRIGGER_ADVERSE", 10.0))
        self.rescue_lot = getattr(config, "RESCUE_LOT_SIZE", None)
        if self.rescue_lot is None:
            self.rescue_lot = config.LOT_SIZE

        self.parent_side = active_side
        self.parent_entry = active_entry
        self.rescue_side = "SHORT" if active_side == "LONG" else "LONG"

        # State: None = not yet fired
        self.state: Optional[RescueState] = None

        # External signal: original has closed (we keep managing rescue)
        self._original_closed = False

        if not self.enabled:
            log.info("RescueManager: disabled by config")

    # ----- Public API -----

    def original_has_closed(self) -> None:
        """Notify manager that the original position has closed."""
        self._original_closed = True
        if self.state is not None and not self.state.closed:
            log.info("Rescue still open after original closed — continuing to manage.")

    def is_done(self) -> bool:
        """True if rescue is finished (or never fired)."""
        if not self.enabled:
            return True
        if self.state is None:
            # If original is closed AND rescue never fired, we're done
            return self._original_closed
        return self.state.closed

    def tick(self, active_position: Optional[object], tick_data) -> None:
        """
        Called every loop iteration. Drives rescue state forward:
          1. If not fired yet and original still open: check trigger
          2. If fired but not filled: poll for fill
          3. If filled and open: manage trail, detect close
        """
        if not self.enabled:
            return
        if tick_data is None:
            return

        # Phase 1: trigger check (only if rescue not yet placed AND original still active)
        if self.state is None and active_position is not None:
            self._maybe_trigger(active_position, tick_data)
            return

        if self.state is None:
            return  # never triggered, no further action

        # Phase 2: fill detection (rescue placed, not yet confirmed filled)
        if not self.state.filled:
            self._poll_for_fill()
            return

        # Phase 3: manage open rescue (trail + close detection)
        if not self.state.closed:
            self._manage_open_rescue(tick_data)

    # ----- Phase 1: trigger -----

    def _maybe_trigger(self, active_position, tick) -> None:
        """Check if adverse threshold reached. If yes, open rescue."""
        if self.parent_side == "LONG":
            # LONG goes adverse when price drops below entry
            current = float(tick.bid)
            adverse = self.parent_entry - current
        else:
            current = float(tick.ask)
            adverse = current - self.parent_entry

        if adverse < self.trigger_dist:
            return

        log.info(
            f"🆘 RESCUE TRIGGER: {self.parent_side} adverse ${adverse:.2f} "
            f">= ${self.trigger_dist:.2f}. Opening {self.rescue_side} market order."
        )

        # Calculate rescue levels using parent's entry +/- trigger_dist as conceptual entry
        if self.rescue_side == "SHORT":
            rescue_entry_planned = round(self.parent_entry - self.trigger_dist, 2)
            rescue_sl = round(rescue_entry_planned + config.SL_DIST, 2)
            rescue_tp = round(rescue_entry_planned - config.TP_DIST, 2)
        else:  # rescue LONG (parent was SHORT)
            rescue_entry_planned = round(self.parent_entry + self.trigger_dist, 2)
            rescue_sl = round(rescue_entry_planned - config.SL_DIST, 2)
            rescue_tp = round(rescue_entry_planned + config.TP_DIST, 2)

        record_rescue_triggered(
            parent_side=self.parent_side,
            parent_entry=self.parent_entry,
            adverse_usd=round(adverse, 2),
            rescue_side=self.rescue_side,
            rescue_entry_planned=rescue_entry_planned,
            rescue_sl=rescue_sl,
            rescue_tp=rescue_tp,
        )

        self.tg.send_message(
            f"🆘 RESCUE TRIGGERED\n"
            f"Parent: {self.parent_side} @ {self.parent_entry}\n"
            f"Adverse: ${adverse:.2f}\n"
            f"Opening {self.rescue_side} {self.rescue_lot} lot\n"
            f"Planned entry: {rescue_entry_planned}\n"
            f"SL: {rescue_sl}  TP: {rescue_tp}"
        )

        # Open market order
        ticket = open_rescue_market_position(
            side=self.rescue_side,
            lot=self.rescue_lot,
            sl=rescue_sl,
            tp=rescue_tp,
        )

        if ticket is None:
            log.error("Rescue market order placement FAILED. Aborting rescue.")
            self.tg.send_message(
                "❌ Rescue order placement FAILED.\n"
                "Original position continues normally."
            )
            # Disable for this position cycle
            self.enabled = False
            return

        self.state = RescueState(
            parent_side=self.parent_side,
            rescue_side=self.rescue_side,
            trigger_price=current,
            entry_planned=rescue_entry_planned,
            parent_entry=self.parent_entry,
            ticket=ticket,
            sl=rescue_sl,
            tp=rescue_tp,
            initial_sl=rescue_sl,
            current_sl=rescue_sl,
        )

        log.info(f"Rescue placed: ticket {ticket}")

    # ----- Phase 2: fill confirmation -----

    def _poll_for_fill(self) -> None:
        """Confirm the rescue position is live and capture actual fill price."""
        positions = mt5.positions_get(ticket=self.state.ticket)
        if positions is None or len(positions) == 0:
            return  # not yet visible; check next tick

        p = positions[0]
        self.state.entry_actual = float(p.price_open)
        self.state.filled = True
        slippage = self.state.entry_actual - self.state.entry_planned
        if self.rescue_side == "SHORT":
            slippage = -slippage  # for SHORT, lower fill = positive slippage

        record_rescue_filled(
            ticket=self.state.ticket,
            entry_actual=self.state.entry_actual,
            entry_planned=self.state.entry_planned,
            slippage=round(slippage, 2),
            sl=self.state.sl,
            tp=self.state.tp,
        )

        log.info(
            f"Rescue FILLED: ticket {self.state.ticket} @ "
            f"{self.state.entry_actual} (slippage ${slippage:+.2f})"
        )
        self.tg.send_message(
            f"✓ Rescue filled\n"
            f"Side: {self.rescue_side}\n"
            f"Entry: {self.state.entry_actual}\n"
            f"Slippage: ${slippage:+.2f}\n"
            f"SL: {self.state.sl}  TP: {self.state.tp}"
        )

    # ----- Phase 3: trail + close detection -----

    def _manage_open_rescue(self, tick) -> None:
        """Manage trail and detect close on the open rescue position."""
        positions = mt5.positions_get(ticket=self.state.ticket)

        # If position no longer exists in MT5, it was closed (by SL/TP or manual)
        if positions is None or len(positions) == 0:
            self._handle_rescue_closed()
            return

        # Update trail based on current price
        if self.rescue_side == "LONG":
            current = float(tick.bid)  # LONG closes at bid
            fav = current - self.state.entry_actual
        else:
            current = float(tick.ask)  # SHORT closes at ask
            fav = self.state.entry_actual - current

        if fav > self.state.max_fav:
            self.state.max_fav = fav

        new_lock_idx = calculate_lock_step(self.state.max_fav)
        if new_lock_idx > self.state.lock_idx and should_update_sl(
            new_lock_idx, self.state.current_sl, self.state.entry_actual, self.rescue_side
        ):
            self.state.lock_idx = new_lock_idx
            new_sl = calculate_new_sl(new_lock_idx, self.state.entry_actual, self.rescue_side)
            if modify_position_sl(self.state.ticket, new_sl):
                self.state.current_sl = new_sl
                log.info(
                    f"Rescue LOCK STEP {new_lock_idx + 1}: "
                    f"SL → {new_sl} (fav ${self.state.max_fav:.2f})"
                )
                self.tg.send_message(
                    f"🔒 Rescue lock step {new_lock_idx + 1}/{config.LOCK_STEPS_COUNT}\n"
                    f"Locked: ${self.state.max_fav:.2f} favorable\n"
                    f"New SL: {new_sl}"
                )

    def _handle_rescue_closed(self) -> None:
        """Rescue position no longer in MT5 — query history for close details."""
        from datetime import timedelta as _td
        from strategy.day_runner import _record_close_to_start_log  # late import to avoid cycle
        from core.mt5_client import get_server_time

        server_now = get_server_time()
        if server_now is None:
            server_now = datetime.now()
        from_time = server_now - _td(days=2)

        deals = mt5.history_deals_get(from_time, server_now)
        exit_price = None
        pnl = None
        reason = "unknown"

        if deals is not None:
            for d in deals:
                if (d.position_id == self.state.ticket
                        and d.entry == mt5.DEAL_ENTRY_OUT
                        and d.symbol == config.SYMBOL):
                    exit_price = float(d.price)
                    pnl = float(d.profit) + float(d.commission) + float(d.swap)
                    # Infer reason from deal reason code
                    if d.reason == mt5.DEAL_REASON_TP:
                        reason = "TP"
                    elif d.reason == mt5.DEAL_REASON_SL:
                        # Distinguish trail vs original SL
                        if self.state.lock_idx >= 1:
                            reason = "Trail"
                        elif self.state.lock_idx == 0:
                            reason = "BE"
                        else:
                            reason = "SL"
                    else:
                        reason = f"reason_code_{d.reason}"
                    break

        self.state.closed = True
        self.state.close_reason = reason
        self.state.close_price = exit_price
        self.state.close_pnl = pnl

        record_rescue_closed(
            ticket=self.state.ticket,
            reason=reason,
            exit_price=exit_price,
            pnl=pnl,
            max_fav=round(self.state.max_fav, 2),
            lock_idx=self.state.lock_idx,
        )

        emoji = "✅" if (pnl or 0) >= 0 else "❌"
        log.info(
            f"Rescue CLOSED: {self.state.ticket} | {reason} | "
            f"exit {exit_price} | PnL ${pnl}"
        )
        self.tg.send_message(
            f"{emoji} RESCUE CLOSED\n"
            f"Side: {self.rescue_side}\n"
            f"Entry: {self.state.entry_actual}\n"
            f"Exit: {exit_price}\n"
            f"Reason: {reason}\n"
            f"PnL: ${pnl:.2f}\n"
            f"Max Fav: ${self.state.max_fav:.2f}\n"
            f"Lock step reached: {self.state.lock_idx + 1 if self.state.lock_idx >= 0 else 'none'}"
        )