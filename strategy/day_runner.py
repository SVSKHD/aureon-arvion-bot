"""Day orchestration: wait for anchor, place orders, manage trade, EOD cleanup."""

import time as time_module
from datetime import datetime, timedelta
from typing import Optional

import MetaTrader5 as mt5

import config
from core.exceptions import HaltBot
from core.logger import get_logger
from core.mt5_client import (
    get_bot_orders,
    get_bot_positions,
    get_server_time,
    get_tick,
    history_deals_with_retry,
)
from core.safety import (
    check_daily_loss,
    check_spread_allowed,
    validate_stop_distance,
    validate_symbol_tradeability,
)
from core.start_log import (
    record_anchor_captured,
    record_day_skipped,
    record_entry_filled,
    record_eod_cleanup,
    record_lock_reached,
    record_orders_placed,
    record_position_closed,
    record_tp_sl_normalized,
)
from core.telegram_notifier import TelegramNotifier
from core.time_utils import get_next_anchor_time, is_weekend, wait_until
from strategy.anchor import capture_anchor_price
from strategy.orders import (
    OrderLevels,
    build_order_prices,
    cancel_bot_pending_orders,
    cancel_order,
    modify_position_sl,
    modify_position_sl_tp,
    place_pending_orders,
)
from strategy.telemetry import send_close_telemetry, send_position_telemetry
from strategy.trailing import calculate_lock_step, calculate_new_sl, should_update_sl

log = get_logger()


def _record_close_to_start_log(ticket: int, side: Optional[str], entry_price: float) -> None:
    """
    Read closing deals from MT5 history and write POSITION_CLOSED to start_log.

    Uses BROKER server time for the history window (MT5 deals are in broker time).
    Falls back to local time only if server time is unavailable.

    Aggregates across multiple closing deals (partial exits / split fills):
      - PnL summed across all matching ENTRY_OUT deals
      - Exit price taken from latest deal (most accurate for stop fills)
      - Reason taken from latest deal
    """
    server_now = get_server_time()
    if server_now is None:
        server_now = datetime.now()  # fallback only
    from_time = server_now - timedelta(days=2)
    deals = history_deals_with_retry(from_time, server_now)

    if deals is None:
        # Query FAILED. Don't pretend close details are "unknown" — be explicit.
        log.error(
            f"history_deals_get FAILED for close logging (ticket {ticket}): "
            f"{mt5.last_error()}. Recording with query-failed marker."
        )
        record_position_closed(
            side=side, ticket=ticket,
            exit_price=None, pnl=None,
            reason="history_query_failed (check MT5 manually for actual close)",
        )
        return

    matching = [
        d for d in deals
        if d.position_id == ticket
        and d.entry == mt5.DEAL_ENTRY_OUT
        and d.symbol == config.SYMBOL
        # NOTE: do NOT filter by magic here. When SL/TP fires server-side,
        # the closing deal is broker-generated and carries magic=0, not our
        # EA's magic. Filtering by magic loses every SL/TP exit's audit.
        # position_id is unique per broker — safe to match on it alone.
    ]
    if not matching:
        record_position_closed(
            side=side, ticket=ticket,
            exit_price=None, pnl=None, reason="unknown (no deal in history)",
        )
        return

    matching.sort(key=lambda d: d.time_msc)
    last = matching[-1]
    total_pnl = sum(d.profit + d.commission + d.swap for d in matching)
    exit_price = last.price

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

    record_position_closed(
        side=side, ticket=ticket,
        exit_price=exit_price, pnl=total_pnl, reason=reason,
    )


def manage_active_position(
    buy_ticket: Optional[int],
    sell_ticket: Optional[int],
    levels: OrderLevels,
    tg: TelegramNotifier,
    *,
    recovered_position: Optional[object] = None,
    recovered_side: Optional[str] = None,
    recovered_entry_price: Optional[float] = None,
    recovered_lock_idx: int = -1,
) -> None:
    """
    Monitor pending orders → handle fill, OCO, TP/SL normalize, trail, exit.

    If recovered_position is provided, skip the entry-detection / OCO-cancel /
    TP-SL-normalize phases (those already happened in the previous run) and
    jump straight into trail management.

    Dispatches to manage_dual_bracket if DUAL_BRACKET_ENABLED is True and
    we're NOT in single-position recovery mode.
    """
    # Dispatch to dual-bracket manager when enabled.
    # Recovery of a single side still uses single-OCO flow (resume that one
    # position only — dual-mode recovery from a single ticket is ambiguous).
    if getattr(config, "DUAL_BRACKET_ENABLED", False) and recovered_position is None:
        manage_dual_bracket(buy_ticket, sell_ticket, levels, tg)
        return
    if recovered_position is not None:
        # Recovery path — resume trail directly
        active_position = recovered_position
        active_side = recovered_side
        entry_price = recovered_entry_price
        current_lock_idx = recovered_lock_idx
        opp_cancelled = True
        tp_sl_normalized = True
        log.info(
            f"Recovery path: skipping detect/OCO/normalize; trail from step "
            f"{current_lock_idx + 1} for {active_side} @ {entry_price}"
        )
    else:
        active_position = None
        active_side = None
        entry_price = None
        current_lock_idx = -1
        opp_cancelled = False
        tp_sl_normalized = False

    last_heartbeat_ts = time_module.time()
    heartbeat_interval = max(60, config.TELEGRAM_POSITION_HEARTBEAT_MINUTES * 60)

    # Retry tracking for critical operations
    normalize_attempts = 0
    NORMALIZE_MAX_ATTEMPTS = 3
    opp_cancel_attempts = 0
    OPP_CANCEL_MAX_ATTEMPTS = 10  # ~10 polls = ~20 sec at POLL_SECONDS=2

    # Trail SL modify failure tracking. If broker repeatedly rejects the
    # same SL update (e.g. freeze level, invalid stops), give up rather
    # than loop forever. Reset to 0 on each successful modify.
    trail_modify_failures = 0
    TRAIL_MODIFY_MAX_FAILURES = 5

    if recovered_position is None:
        log.info("Watching for entry trigger...")

    while True:
        try:
            # ---------------------------------------------------------------
            # 1. Detect active position (one-time)
            # ---------------------------------------------------------------
            if active_position is None:
                positions = get_bot_positions()
                if positions is None:
                    # MT5 query failed — don't conclude anything, retry
                    time_module.sleep(config.POLL_SECONDS)
                    continue
                if len(positions) > 1:
                    msg = (
                        f"Multiple bot positions detected at runtime "
                        f"({len(positions)}). Halting for manual review."
                    )
                    log.error(msg)
                    tg.send_message(
                        f"🚨 MULTIPLE POSITIONS DETECTED\n"
                        f"Count: {len(positions)}\n"
                        f"Bot is halting — manual review required."
                    )
                    raise HaltBot(msg)
                if positions:
                    p = positions[0]
                    active_position = p
                    active_side = "LONG" if p.type == mt5.POSITION_TYPE_BUY else "SHORT"
                    entry_price = float(p.price_open)  # actual filled price
                    log.info(
                        f"✓ ENTERED {active_side} @ actual filled price {entry_price}"
                    )

                    # Determine planned price for slippage record
                    planned_price = (
                        levels.long_entry if active_side == "LONG"
                        else levels.short_entry
                    )
                    record_entry_filled(
                        side=active_side,
                        entry_price=entry_price,
                        planned_price=planned_price,
                        ticket=p.ticket,
                    )

                    send_position_telemetry(
                        tg, active_position, active_side, "ENTRY_FILLED",
                        entry_price, lock_step_idx=-1,
                    )

            # ---------------------------------------------------------------
            # 2. OCO: cancel opposite pending — RETRY until success or HALT
            # ---------------------------------------------------------------
            if active_position is not None and not opp_cancelled:
                opp_ticket = None
                if active_side == "LONG":
                    opp_ticket = sell_ticket
                elif active_side == "SHORT":
                    opp_ticket = buy_ticket

                if opp_ticket is None:
                    # Nothing to cancel
                    opp_cancelled = True
                else:
                    if cancel_order(opp_ticket):
                        opp_cancelled = True
                    else:
                        # Cancel failed — verify if still live
                        remaining = get_bot_orders()
                        if remaining is None:
                            # Cannot verify — count as failed attempt, retry
                            opp_cancel_attempts += 1
                            log.warning(
                                f"Opposite cancel verify failed (MT5 query error). "
                                f"Attempt {opp_cancel_attempts}/{OPP_CANCEL_MAX_ATTEMPTS}."
                            )
                        elif not any(o.ticket == opp_ticket for o in remaining):
                            log.info(
                                f"Opposite ticket {opp_ticket} not in orders list — "
                                f"already gone. Marking cancelled."
                            )
                            opp_cancelled = True
                        else:
                            opp_cancel_attempts += 1
                            log.warning(
                                f"Opposite pending {opp_ticket} STILL LIVE. "
                                f"Attempt {opp_cancel_attempts}/{OPP_CANCEL_MAX_ATTEMPTS}."
                            )

                    if not opp_cancelled and opp_cancel_attempts >= OPP_CANCEL_MAX_ATTEMPTS:
                        msg = (
                            f"CRITICAL: opposite pending {opp_ticket} could not be "
                            f"cancelled after {opp_cancel_attempts} attempts. "
                            f"Risk of double-fill. Halting."
                        )
                        log.error(msg)
                        tg.send_message(
                            f"🚨 OPPOSITE CANCEL FAILED\n"
                            f"Ticket: {opp_ticket}\n"
                            f"Attempts: {opp_cancel_attempts}\n"
                            f"Bot halting — manually cancel opposite in MT5."
                        )
                        raise HaltBot(msg)

                # ---- Post-cancel state verification ----
                # When opp_cancelled flips True, immediately re-query both
                # positions AND orders to confirm the post-cancel state is sane.
                if opp_cancelled:
                    verify_positions = get_bot_positions()
                    verify_orders = get_bot_orders()
                    if verify_positions is not None and verify_orders is not None:
                        if len(verify_positions) > 1:
                            msg = (
                                f"Post-cancel verify: {len(verify_positions)} positions "
                                f"detected (expected exactly 1). Halting."
                            )
                            log.error(msg)
                            tg.send_message(
                                f"🚨 POST-CANCEL: MULTIPLE POSITIONS\n"
                                f"Found: {len(verify_positions)}\n"
                                f"Bot halting — manual flatten required."
                            )
                            raise HaltBot(msg)
                        if len(verify_orders) > 0:
                            log.warning(
                                f"Post-cancel verify: {len(verify_orders)} pending(s) "
                                f"still present. Reverting opp_cancelled to retry."
                            )
                            # Broker may not have confirmed cancellation yet.
                            # Don't trust the True flag — try again next loop.
                            opp_cancelled = False
                    else:
                        log.warning(
                            "Post-cancel verify: MT5 query failed. "
                            "Main loop checks will catch any issue."
                        )

                # GATE: do not normalize / trail / manage further until
                # opposite is confirmed cancelled. Just sleep and retry.
                if not opp_cancelled:
                    time_module.sleep(config.POLL_SECONDS)
                    continue

            # ---------------------------------------------------------------
            # 3. Normalize TP/SL from ACTUAL filled price — RETRY up to N times
            # ---------------------------------------------------------------
            if active_position is not None and not tp_sl_normalized:
                if active_side == "LONG":
                    corrected_sl = entry_price - config.SL_DIST
                    corrected_tp = entry_price + config.TP_DIST
                else:
                    corrected_sl = entry_price + config.SL_DIST
                    corrected_tp = entry_price - config.TP_DIST

                ok = modify_position_sl_tp(
                    active_position.ticket, corrected_sl, corrected_tp
                )

                if ok:
                    fresh = mt5.positions_get(ticket=active_position.ticket)
                    if fresh:
                        active_position = fresh[0]
                    tp_sl_normalized = True
                    record_tp_sl_normalized(
                        side=active_side,
                        ticket=active_position.ticket,
                        new_sl=corrected_sl,
                        new_tp=corrected_tp,
                    )
                    send_position_telemetry(
                        tg, active_position, active_side, "TP_SL_NORMALIZED",
                        entry_price, lock_step_idx=-1,
                    )
                else:
                    normalize_attempts += 1
                    if normalize_attempts >= NORMALIZE_MAX_ATTEMPTS:
                        log.error(
                            f"TP/SL normalization failed after {normalize_attempts} attempts — "
                            f"giving up. Pending-order SL/TP remain active."
                        )
                        tg.send_message(
                            f"🚨 TP/SL normalization failed after {normalize_attempts} retries\n"
                            f"Side: {active_side}\n"
                            f"Entry: {entry_price:.2f}\n"
                            f"Trail will still operate from actual filled price,\n"
                            f"but TP/SL may be slightly off (slippage amount).\n"
                            f"Verify position in MT5 manually."
                        )
                        tp_sl_normalized = True  # stop retrying
                    else:
                        log.warning(
                            f"TP/SL normalize attempt {normalize_attempts}/"
                            f"{NORMALIZE_MAX_ATTEMPTS} failed — will retry next loop."
                        )
                        time_module.sleep(2)
                        continue  # skip rest of loop, retry normalize

            # ---------------------------------------------------------------
            # 4. No position yet → check EOD cutoff
            # ---------------------------------------------------------------
            if active_position is None:
                now = get_server_time()
                if now is not None and now.hour >= config.EOD_CANCEL_HOUR:
                    log.info("EOD reached without entry. Cancelling pending orders.")
                    cancelled = cancel_bot_pending_orders()
                    if cancelled is None:
                        msg = (
                            "EOD cleanup: MT5 query failed — cannot verify pendings "
                            "were cancelled. Halting to avoid stale orders carrying over."
                        )
                        log.error(msg)
                        tg.send_message(
                            f"🚨 EOD CLEANUP FAILED\n"
                            f"Cannot verify pendings cancelled.\n"
                            f"Halting — manually cancel any leftover pendings in MT5."
                        )
                        raise HaltBot(msg)
                    record_eod_cleanup(cancelled_count=cancelled)
                    tg.send_message(
                        f"🧹 EOD cleanup\nCancelled {cancelled} pending order(s)."
                    )
                    return

                pendings = get_bot_orders()
                if pendings is None:
                    # Query failed — wait and retry
                    time_module.sleep(config.POLL_SECONDS)
                    continue
                if not pendings:
                    log.warning("No pending orders and no position. Day done.")
                    return

                time_module.sleep(config.POLL_SECONDS)
                continue

            # ---------------------------------------------------------------
            # 5. Position open → check if still alive
            # ---------------------------------------------------------------
            positions = get_bot_positions()
            if positions is None:
                # MT5 query failed — don't conclude close, retry
                time_module.sleep(config.POLL_SECONDS)
                continue
            if len(positions) > 1:
                msg = (
                    f"Multiple bot positions detected during management "
                    f"({len(positions)}). Halting for manual review."
                )
                log.error(msg)
                tg.send_message(
                    f"🚨 MULTIPLE POSITIONS DURING MANAGEMENT\n"
                    f"Count: {len(positions)}\n"
                    f"Active ticket: {active_position.ticket}\n"
                    f"Bot is halting — manual review required."
                )
                raise HaltBot(msg)
            still_open = any(p.ticket == active_position.ticket for p in positions)
            if not still_open:
                _record_close_to_start_log(active_position.ticket, active_side, entry_price)
                send_close_telemetry(
                    tg, active_position.ticket, active_side,
                    entry_price, current_lock_idx,
                )
                log.info(f"✓ Position {active_position.ticket} CLOSED")
                return

            # ---------------------------------------------------------------
            # 6. Trail SL — using ACTUAL filled entry_price
            # ---------------------------------------------------------------
            tick = get_tick()
            if tick is None:
                time_module.sleep(config.POLL_SECONDS)
                continue

            if active_side == "LONG":
                favorable = tick.bid - entry_price
            else:
                favorable = entry_price - tick.ask

            new_idx = calculate_lock_step(favorable)
            if should_update_sl(current_lock_idx, new_idx):
                new_sl = calculate_new_sl(entry_price, active_side, new_idx)
                if new_sl is not None:
                    # Refresh position so we modify with current TP.
                    # If refresh returns empty, position closed mid-update — handle as close.
                    fresh = mt5.positions_get(ticket=active_position.ticket)
                    if not fresh:
                        log.info(
                            f"Position {active_position.ticket} closed during trail update."
                        )
                        _record_close_to_start_log(
                            active_position.ticket, active_side, entry_price
                        )
                        send_close_telemetry(
                            tg, active_position.ticket, active_side,
                            entry_price, current_lock_idx,
                        )
                        return
                    active_position = fresh[0]
                    ok = modify_position_sl(
                        active_position.ticket, new_sl, active_position.tp
                    )
                    if ok:
                        trail_modify_failures = 0  # reset on success
                        current_lock_idx = new_idx
                        lock_offset = config.LOCK_STEP * (new_idx + 1)
                        log.info(
                            f"TRAIL step {new_idx + 1} → SL={new_sl:.2f} "
                            f"(+${lock_offset:.2f} locked)"
                        )
                        record_lock_reached(
                            side=active_side,
                            step=new_idx + 1,
                            locked_amount=lock_offset,
                            new_sl=new_sl,
                            ticket=active_position.ticket,
                        )
                        # Refresh once more for telemetry. Tolerate close race.
                        fresh = mt5.positions_get(ticket=active_position.ticket)
                        if fresh:
                            active_position = fresh[0]
                        send_position_telemetry(
                            tg, active_position, active_side, "LOCK_REACHED",
                            entry_price, lock_step_idx=current_lock_idx,
                        )
                    else:
                        trail_modify_failures += 1
                        log.warning(
                            f"Trail SL modify failed (attempt "
                            f"{trail_modify_failures}/{TRAIL_MODIFY_MAX_FAILURES}). "
                            f"Will retry next poll."
                        )
                        if trail_modify_failures >= TRAIL_MODIFY_MAX_FAILURES:
                            msg = (
                                f"Trail SL modify failed {trail_modify_failures} "
                                f"consecutive times. Possible broker freeze level "
                                f"or invalid stops. Halting — current SL/TP remain "
                                f"active on position {active_position.ticket}."
                            )
                            log.error(msg)
                            tg.send_message(
                                f"🚨 TRAIL MODIFY EXHAUSTED\n"
                                f"Ticket: {active_position.ticket}\n"
                                f"Attempts: {trail_modify_failures}\n"
                                f"Existing SL still active in broker.\n"
                                f"Bot halting — review broker freeze level and SL/TP."
                            )
                            raise HaltBot(msg)

            # ---------------------------------------------------------------
            # 7. Heartbeat — periodic state snapshot while position open
            # ---------------------------------------------------------------
            if config.TELEGRAM_POSITION_HEARTBEAT_ENABLED:
                if time_module.time() - last_heartbeat_ts >= heartbeat_interval:
                    send_position_telemetry(
                        tg, active_position, active_side, "POSITION_HEARTBEAT",
                        entry_price, lock_step_idx=current_lock_idx,
                    )
                    last_heartbeat_ts = time_module.time()

            time_module.sleep(config.POLL_SECONDS)

        except HaltBot:
            # Re-raise — HaltBot must propagate to main() for clean shutdown.
            # Do NOT log here (main loop logs); do NOT sleep (we're exiting).
            raise
        except Exception as e:
            log.error(f"manage_active_position error: {e}", exc_info=True)
            time_module.sleep(config.POLL_SECONDS)


# ============================================================================
# DUAL-BRACKET MANAGEMENT
# ----------------------------------------------------------------------------
# Both legs trade independently. Each side has its own state machine for
# entry detection, TP/SL normalization, trailing, and close handling.
# No OCO cancellation — pendings stay live until they fill or EOD cleans up.
# ============================================================================

from dataclasses import dataclass


@dataclass
class _SideState:
    """Per-leg state for dual-bracket management."""
    side: str                          # "LONG" or "SHORT"
    pending_ticket: Optional[int]
    planned_entry: float               # planned trigger price (for slippage record)
    position: Optional[object] = None  # MT5 position object when filled
    entry_price: Optional[float] = None
    tp_sl_normalized: bool = False
    normalize_attempts: int = 0
    current_lock_idx: int = -1
    trail_modify_failures: int = 0
    closed: bool = False               # set True once position closes
    closed_handled: bool = False       # set True once close telemetry sent
    last_heartbeat_ts: float = 0.0


def manage_dual_bracket(
    buy_ticket: Optional[int],
    sell_ticket: Optional[int],
    levels: OrderLevels,
    tg: TelegramNotifier,
) -> None:
    """
    Manage two legs (LONG + SHORT) independently. Both can trade and trail.

    Exit conditions:
      - Both sides have closed positions, OR
      - EOD reached and all pendings cancelled + all open positions closed
    """
    NORMALIZE_MAX_ATTEMPTS = 3
    TRAIL_MODIFY_MAX_FAILURES = 5

    long_state = _SideState(
        side="LONG", pending_ticket=buy_ticket,
        planned_entry=levels.long_entry,
        last_heartbeat_ts=time_module.time(),
    )
    short_state = _SideState(
        side="SHORT", pending_ticket=sell_ticket,
        planned_entry=levels.short_entry,
        last_heartbeat_ts=time_module.time(),
    )
    heartbeat_interval = max(60, config.TELEGRAM_POSITION_HEARTBEAT_MINUTES * 60)

    log.info(
        f"DUAL-BRACKET active | LONG pending={buy_ticket} @ {levels.long_entry} "
        f"| SHORT pending={sell_ticket} @ {levels.short_entry} | "
        f"per-leg lot={_dual_lot_size()}"
    )

    eod_cleanup_done = False

    while True:
        try:
            # ----------------------------------------------------------------
            # Query state
            # ----------------------------------------------------------------
            positions = get_bot_positions()
            orders = get_bot_orders()
            if positions is None or orders is None:
                # MT5 query failed — don't conclude anything, retry
                time_module.sleep(config.POLL_SECONDS)
                continue

            # Multi-side sanity: at most ONE position per side
            long_positions = [p for p in positions if p.type == mt5.POSITION_TYPE_BUY]
            short_positions = [p for p in positions if p.type == mt5.POSITION_TYPE_SELL]
            if len(long_positions) > 1 or len(short_positions) > 1:
                msg = (
                    f"Dual-bracket: multiple positions on same side detected "
                    f"(long={len(long_positions)}, short={len(short_positions)}). Halting."
                )
                log.error(msg)
                tg.send_message(
                    f"🚨 DUAL-BRACKET: SAME-SIDE DUPLICATES\n"
                    f"Long count: {len(long_positions)}\n"
                    f"Short count: {len(short_positions)}\n"
                    f"Halting — manual review required."
                )
                raise HaltBot(msg)

            # ----------------------------------------------------------------
            # Per-side management
            # ----------------------------------------------------------------
            for state, side_positions in (
                (long_state, long_positions),
                (short_state, short_positions),
            ):
                _manage_one_leg(
                    state=state,
                    side_positions=side_positions,
                    levels=levels,
                    tg=tg,
                    NORMALIZE_MAX_ATTEMPTS=NORMALIZE_MAX_ATTEMPTS,
                    TRAIL_MODIFY_MAX_FAILURES=TRAIL_MODIFY_MAX_FAILURES,
                    heartbeat_interval=heartbeat_interval,
                )

            # ----------------------------------------------------------------
            # Exit conditions
            # ----------------------------------------------------------------
            both_closed = long_state.closed and short_state.closed
            long_done = (
                long_state.closed
                or (eod_cleanup_done and long_state.position is None)
            )
            short_done = (
                short_state.closed
                or (eod_cleanup_done and short_state.position is None)
            )
            if both_closed or (long_done and short_done):
                log.info("Dual-bracket: both legs done.")
                return

            # ----------------------------------------------------------------
            # EOD: cancel any remaining bot pendings (positions keep running)
            # ----------------------------------------------------------------
            if not eod_cleanup_done:
                now = get_server_time()
                if now is not None and now.hour >= config.EOD_CANCEL_HOUR:
                    # Only cancel pendings, don't touch live positions
                    remaining_pendings = [
                        o for o in orders
                        if o.ticket in (long_state.pending_ticket, short_state.pending_ticket)
                    ]
                    if remaining_pendings:
                        log.info(
                            f"EOD reached. Cancelling {len(remaining_pendings)} "
                            f"unfilled pending(s). Live positions continue."
                        )
                        for p in remaining_pendings:
                            cancel_order(p.ticket)
                    eod_cleanup_done = True
                    tg.send_message(
                        f"🧹 EOD pending cleanup (dual-bracket)\n"
                        f"Cancelled {len(remaining_pendings)} unfilled pending(s).\n"
                        f"Open positions (if any) continue to trail."
                    )
                    record_eod_cleanup(cancelled_count=len(remaining_pendings))

            time_module.sleep(config.POLL_SECONDS)

        except HaltBot:
            raise
        except Exception as e:
            log.error(f"manage_dual_bracket error: {e}", exc_info=True)
            time_module.sleep(config.POLL_SECONDS)


def _dual_lot_size() -> float:
    """Per-leg lot size for dual mode."""
    override = getattr(config, "DUAL_BRACKET_LOT_SIZE", None)
    return float(override) if override is not None else float(config.LOT_SIZE)


def _manage_one_leg(
    *,
    state: _SideState,
    side_positions: list,
    levels: OrderLevels,
    tg: TelegramNotifier,
    NORMALIZE_MAX_ATTEMPTS: int,
    TRAIL_MODIFY_MAX_FAILURES: int,
    heartbeat_interval: int,
) -> None:
    """
    Run one full management cycle for a single leg. Updates `state` in place.
    Mirrors the single-OCO flow phases for one side only:
      detect → normalize → trail/heartbeat → close handling
    """
    # ------------------------------------------------------------------
    # 1. If this leg already finished, do nothing further
    # ------------------------------------------------------------------
    if state.closed and state.closed_handled:
        return

    # ------------------------------------------------------------------
    # 2. Detect this leg's position (first appearance)
    # ------------------------------------------------------------------
    if state.position is None and side_positions:
        p = side_positions[0]
        state.position = p
        state.entry_price = float(p.price_open)
        log.info(
            f"✓ DUAL leg ENTERED {state.side} @ filled {state.entry_price}"
        )
        record_entry_filled(
            side=state.side,
            entry_price=state.entry_price,
            planned_price=state.planned_entry,
            ticket=p.ticket,
        )
        send_position_telemetry(
            tg, state.position, state.side, "ENTRY_FILLED",
            state.entry_price, lock_step_idx=-1,
        )

    # ------------------------------------------------------------------
    # 3. If position previously existed but now gone → it closed
    # ------------------------------------------------------------------
    if state.position is not None and not side_positions and not state.closed:
        log.info(f"✓ DUAL leg {state.side} position CLOSED")
        _record_close_to_start_log(
            state.position.ticket, state.side, state.entry_price or 0.0
        )
        send_close_telemetry(
            tg, state.position.ticket, state.side,
            state.entry_price or 0.0, state.current_lock_idx,
        )
        state.closed = True
        state.closed_handled = True
        return

    # No position yet → nothing more to do this cycle for this leg
    if state.position is None:
        return

    # Refresh position object with latest from this cycle's query
    if side_positions:
        state.position = side_positions[0]

    # ------------------------------------------------------------------
    # 4. Normalize TP/SL from actual filled price (retry-bounded)
    # ------------------------------------------------------------------
    if not state.tp_sl_normalized:
        if state.side == "LONG":
            corrected_sl = state.entry_price - config.SL_DIST
            corrected_tp = state.entry_price + config.TP_DIST
        else:
            corrected_sl = state.entry_price + config.SL_DIST
            corrected_tp = state.entry_price - config.TP_DIST

        ok = modify_position_sl_tp(state.position.ticket, corrected_sl, corrected_tp)
        if ok:
            state.tp_sl_normalized = True
            fresh = mt5.positions_get(ticket=state.position.ticket)
            if fresh:
                state.position = fresh[0]
            record_tp_sl_normalized(
                side=state.side, ticket=state.position.ticket,
                new_sl=corrected_sl, new_tp=corrected_tp,
            )
            send_position_telemetry(
                tg, state.position, state.side, "TP_SL_NORMALIZED",
                state.entry_price, lock_step_idx=-1,
            )
        else:
            state.normalize_attempts += 1
            if state.normalize_attempts >= NORMALIZE_MAX_ATTEMPTS:
                log.error(
                    f"DUAL {state.side} normalize failed after "
                    f"{state.normalize_attempts} attempts. Giving up for this leg."
                )
                tg.send_message(
                    f"🚨 DUAL {state.side} TP/SL normalize failed\n"
                    f"Trail will operate from filled price; broker SL/TP slightly off."
                )
                state.tp_sl_normalized = True
            else:
                log.warning(
                    f"DUAL {state.side} normalize {state.normalize_attempts}/"
                    f"{NORMALIZE_MAX_ATTEMPTS} failed. Retry next cycle."
                )
                return  # try again next cycle for this leg

    # ------------------------------------------------------------------
    # 5. Trail SL
    # ------------------------------------------------------------------
    tick = get_tick()
    if tick is None:
        return
    current_price = tick.bid if state.side == "LONG" else tick.ask
    favorable = (
        current_price - state.entry_price
        if state.side == "LONG"
        else state.entry_price - current_price
    )

    new_idx = calculate_lock_step(favorable)
    if should_update_sl(state.current_lock_idx, new_idx):
        new_sl = calculate_new_sl(state.entry_price, state.side, new_idx)
        if new_sl is not None:
            fresh = mt5.positions_get(ticket=state.position.ticket)
            if not fresh:
                # Closed mid-trail
                log.info(f"DUAL {state.side} closed during trail update.")
                _record_close_to_start_log(
                    state.position.ticket, state.side, state.entry_price
                )
                send_close_telemetry(
                    tg, state.position.ticket, state.side,
                    state.entry_price, state.current_lock_idx,
                )
                state.closed = True
                state.closed_handled = True
                return
            state.position = fresh[0]
            ok = modify_position_sl(
                state.position.ticket, new_sl, state.position.tp
            )
            if ok:
                state.trail_modify_failures = 0
                state.current_lock_idx = new_idx
                lock_offset = config.LOCK_STEP * (new_idx + 1)
                log.info(
                    f"DUAL {state.side} TRAIL step {new_idx + 1} → "
                    f"SL={new_sl:.2f} (+${lock_offset:.2f} locked)"
                )
                record_lock_reached(
                    side=state.side, step=new_idx + 1,
                    locked_amount=lock_offset, new_sl=new_sl,
                    ticket=state.position.ticket,
                )
                fresh = mt5.positions_get(ticket=state.position.ticket)
                if fresh:
                    state.position = fresh[0]
                send_position_telemetry(
                    tg, state.position, state.side, "LOCK_REACHED",
                    state.entry_price, lock_step_idx=state.current_lock_idx,
                )
            else:
                state.trail_modify_failures += 1
                log.warning(
                    f"DUAL {state.side} trail modify failed "
                    f"({state.trail_modify_failures}/{TRAIL_MODIFY_MAX_FAILURES})"
                )
                if state.trail_modify_failures >= TRAIL_MODIFY_MAX_FAILURES:
                    msg = (
                        f"DUAL {state.side} trail modify exhausted. "
                        f"Existing SL active. Halting."
                    )
                    log.error(msg)
                    tg.send_message(
                        f"🚨 DUAL {state.side} TRAIL MODIFY EXHAUSTED\n"
                        f"Ticket: {state.position.ticket}\n"
                        f"Existing SL still active. Bot halting."
                    )
                    raise HaltBot(msg)

    # ------------------------------------------------------------------
    # 6. Heartbeat
    # ------------------------------------------------------------------
    if config.TELEGRAM_POSITION_HEARTBEAT_ENABLED:
        if time_module.time() - state.last_heartbeat_ts >= heartbeat_interval:
            send_position_telemetry(
                tg, state.position, state.side, "POSITION_HEARTBEAT",
                state.entry_price, lock_step_idx=state.current_lock_idx,
            )
            state.last_heartbeat_ts = time_module.time()



def run_day(tg: TelegramNotifier) -> None:
    """Single day cycle: wait for anchor → checks → orders → manage."""
    target = get_next_anchor_time()
    now = get_server_time()
    if now is not None:
        hours_wait = (target - now).total_seconds() / 3600
        log.info(f"Next anchor: {target} (broker time). Waiting {hours_wait:.2f} hr...")
    else:
        log.info(f"Next anchor: {target}. Server time unavailable for ETA.")

    wait_until(target + timedelta(seconds=config.ANCHOR_WAIT_BUFFER))

    now = get_server_time()
    if now is None:
        log.error("Server time still unavailable. Skipping day.")
        time_module.sleep(60)
        return

    if is_weekend(now):
        log.info(f"Weekend ({now.strftime('%A')}). Sleeping 1 hour.")
        record_day_skipped("weekend", weekday=now.strftime("%A"))
        time_module.sleep(3600)
        return

    if not validate_symbol_tradeability():
        tg.send_message("⚠️ Symbol not tradeable today — skipping.")
        record_day_skipped("symbol_not_tradeable")
        time_module.sleep(3600)
        return

    limit_hit, today_pnl = check_daily_loss()
    if limit_hit:
        log.warning(f"Daily loss limit hit (PnL ${today_pnl:.2f}). Skipping day.")
        tg.send_message(
            f"🛑 Daily loss limit hit\n"
            f"PnL: ${today_pnl:.2f}\n"
            f"Limit: {config.MAX_DAILY_LOSS_PCT}% of balance"
        )
        record_day_skipped("daily_loss_limit", pnl=today_pnl,
                           limit_pct=config.MAX_DAILY_LOSS_PCT)
        time_module.sleep(3600)
        return

    anchor = capture_anchor_price(now.date())
    if anchor is None:
        log.warning("Anchor unavailable. Skipping day.")
        record_day_skipped("anchor_unavailable", date=str(now.date()))
        time_module.sleep(60)
        return

    spread_ok, spread = check_spread_allowed()
    if not spread_ok:
        spread_str = f"{spread:.2f}" if spread is not None else "n/a"
        log.warning(f"Spread ${spread_str} > max ${config.MAX_SPREAD_USD}. Skipping.")
        tg.send_message(
            f"⚠️ Day skipped: spread too high\n"
            f"Spread: ${spread_str}\n"
            f"Max allowed: ${config.MAX_SPREAD_USD}"
        )
        record_day_skipped("spread_too_high", spread=spread,
                           max_allowed=config.MAX_SPREAD_USD)
        return

    levels = build_order_prices(anchor)
    long_stops_ok = validate_stop_distance(levels.long_entry, levels.long_sl, levels.long_tp)
    short_stops_ok = validate_stop_distance(levels.short_entry, levels.short_sl, levels.short_tp)
    if not (long_stops_ok and short_stops_ok):
        log.error(
            f"Stop distance check failed (long_ok={long_stops_ok}, short_ok={short_stops_ok}). "
            f"Skipping day to avoid broker rejection."
        )
        tg.send_message(
            f"⚠️ Day skipped: stops too close to broker minimum\n"
            f"Long ok: {long_stops_ok}, Short ok: {short_stops_ok}"
        )
        record_day_skipped(
            "stops_too_close",
            long_ok=long_stops_ok,
            short_ok=short_stops_ok,
        )
        return

    log.info(
        f"  LONG  entry: {levels.long_entry}  SL: {levels.long_sl}  TP: {levels.long_tp}"
    )
    log.info(
        f"  SHORT entry: {levels.short_entry}  SL: {levels.short_sl}  TP: {levels.short_tp}"
    )

    # Anchor candle time is approximately now's date at ANCHOR_HOUR:00
    anchor_time = now.replace(hour=config.ANCHOR_HOUR, minute=0, second=0, microsecond=0)
    record_anchor_captured(anchor, anchor_time, levels)

    # ----- Pre-trade filter (ATR-based directional bias) -----
    from strategy.filters import get_side_decision
    decision = get_side_decision()
    atr_str = f"${decision.atr_value:.2f}" if decision.atr_value is not None else "n/a"
    log.info(
        f"Filter decision: long={decision.allow_long}, short={decision.allow_short} "
        f"| ATR={atr_str} | reason: {decision.reason}"
    )

    tg.send_message(
        f"📌 Anchor captured\n"
        f"Date: {now.date()}\n"
        f"Anchor: {anchor:.2f}\n"
        f"Buy Stop: {levels.long_entry}\n"
        f"Sell Stop: {levels.short_entry}\n"
        f"ATR({config.ATR_PERIOD}) {config.ATR_TIMEFRAME}: {atr_str}\n"
        f"Filter: {decision.reason}"
    )

    # If filter blocks both sides, skip the day entirely
    if not decision.allow_long and not decision.allow_short:
        log.info(f"Filter blocked both sides — skipping day. Reason: {decision.reason}")
        tg.send_message(
            f"🚫 Day skipped by filter\n"
            f"Reason: {decision.reason}"
        )
        record_day_skipped("filter_blocked_all",
                           atr=decision.atr_value, reason=decision.reason)
        return

    buy_ticket, sell_ticket = place_pending_orders(
        levels,
        allow_long=decision.allow_long,
        allow_short=decision.allow_short,
    )

    # Validation: what we expected to place must have succeeded.
    expected_buy  = decision.allow_long
    expected_sell = decision.allow_short
    buy_ok  = (not expected_buy)  or (buy_ticket is not None)
    sell_ok = (not expected_sell) or (sell_ticket is not None)

    if not buy_ok or not sell_ok:
        log.error(
            f"Order placement incomplete vs. filter decision. "
            f"expected_buy={expected_buy} got_buy={buy_ticket} | "
            f"expected_sell={expected_sell} got_sell={sell_ticket}. "
            f"Cancelling any accepted bot pendings and skipping day."
        )
        cancelled = cancel_bot_pending_orders()
        if cancelled is None:
            msg = (
                "Order placement incomplete AND cleanup query failed — cannot verify "
                "pending state. Halting to avoid one-sided exposure."
            )
            log.error(msg)
            tg.send_message(f"🚨 {msg}")
            raise HaltBot(msg)
        tg.send_message(
            f"❌ Order placement incomplete — day skipped\n"
            f"Filter wanted: long={expected_buy}, short={expected_sell}\n"
            f"Got: buy={buy_ticket}, sell={sell_ticket}\n"
            f"Action: cancelled {cancelled} bot pending(s)"
        )
        record_day_skipped("placement_incomplete",
                           buy_ticket=buy_ticket, sell_ticket=sell_ticket,
                           filter_long=expected_buy, filter_short=expected_sell)
        return

    record_orders_placed(buy_ticket, sell_ticket)
    sides_placed = []
    if buy_ticket is not None:
        sides_placed.append(f"LONG buy={buy_ticket}")
    if sell_ticket is not None:
        sides_placed.append(f"SHORT sell={sell_ticket}")
    tg.send_message(
        f"📥 Pending orders placed\n"
        + "\n".join(sides_placed)
    )

    manage_active_position(buy_ticket, sell_ticket, levels, tg)