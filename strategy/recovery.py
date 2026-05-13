"""
Startup recovery — resume in-progress state when the bot starts.

Three recovery scenarios handled:

  1. Existing bot position (e.g., bot crashed mid-trade)
     → Resume trail management from current SL position.

  2. Existing bot pending orders (e.g., crashed after placement)
     → Resume watching for trigger.

  3. Late anchor (started after 02:00 broker today)
     → If today's anchor candle exists AND price still in safe entry zone:
       capture late, place OCO pendings.
     → Otherwise log + skip (wait for tomorrow).

Recovery is CONSERVATIVE: refuses to enter mid-breakout (price already past
trigger). Backtest assumed clean stop-order fills at trigger price; entering
late and dirty would diverge from tested behavior.
"""

import math
from typing import Any, Optional

import MetaTrader5 as mt5

import config
from core.logger import get_logger
from core.mt5_client import (
    get_bot_orders,
    get_bot_positions,
    get_server_time,
    get_tick,
)
from core.start_log import (
    record_late_recovery,
    record_pendings_resumed,
    record_position_resumed,
)
from core.telegram_notifier import TelegramNotifier
from strategy.anchor import capture_anchor_price
from strategy.orders import (
    OrderLevels,
    build_order_prices,
    cancel_bot_pending_orders,
    place_pending_orders,
)
from strategy.trailing import EPSILON

log = get_logger()


def _infer_lock_step_from_sl(side: str, entry: float, sl: float) -> int:
    """
    Reverse-engineer the current lock step from where SL currently sits.

    LONG:  lock_offset = sl - entry  (positive = locked above entry)
    SHORT: lock_offset = entry - sl  (positive = locked below entry)
    """
    if side == "LONG":
        offset = sl - entry
    else:
        offset = entry - sl

    if offset + EPSILON < config.LOCK_STEP:
        return -1

    idx = math.floor((offset + EPSILON) / config.LOCK_STEP) - 1
    return min(idx, config.LOCK_STEPS_COUNT - 1)


def _format_skip_reason(anchor: float, levels: OrderLevels, bid: float, ask: float) -> str:
    """Build a human-readable reason for skipping late anchor."""
    if ask >= levels.long_tp:
        return (
            f"Anchor {anchor:.2f}, long TP {levels.long_tp:.2f} | "
            f"ask {ask:.2f} already past TP (window closed)"
        )
    if bid <= levels.short_tp:
        return (
            f"Anchor {anchor:.2f}, short TP {levels.short_tp:.2f} | "
            f"bid {bid:.2f} already past TP (window closed)"
        )
    if ask >= levels.long_entry:
        return (
            f"Anchor {anchor:.2f}, long trigger {levels.long_entry:.2f} | "
            f"ask {ask:.2f} past trigger (mid-breakout, conservative skip)"
        )
    if bid <= levels.short_entry:
        return (
            f"Anchor {anchor:.2f}, short trigger {levels.short_entry:.2f} | "
            f"bid {bid:.2f} past trigger (mid-breakout, conservative skip)"
        )
    return f"Anchor {anchor:.2f}, price ({bid}/{ask}) out of safe zone"


def attempt_startup_recovery(tg: TelegramNotifier) -> dict:
    """
    Check for in-progress state on startup. Returns a dict describing the outcome:

      {'mode': 'no_recovery'}                                     # normal startup, wait for next anchor
      {'mode': 'resume_position', 'position', 'side', 'entry_price', 'lock_idx'}
      {'mode': 'resume_pendings', 'buy_ticket', 'sell_ticket', 'levels'}
      {'mode': 'late_anchor',     'buy_ticket', 'sell_ticket', 'levels'}
      {'mode': 'aborted', 'reason'}                              # something dangerous, manual review
    """
    log.info("Startup recovery: checking for in-progress state...")

    # ========================================================================
    # Scenario 1 — Existing bot position
    # ========================================================================
    positions = get_bot_positions()
    if positions is None:
        # MT5 query failed — we DON'T know whether positions exist.
        # Treating this as "no position" and starting trading could place new
        # trades on top of a hidden live position. Refuse to continue.
        msg = "MT5 positions query failed at startup — state unknown. Halting for manual review."
        log.error(msg)
        tg.send_message(
            f"🚨 STARTUP HALT\nCannot read positions from MT5.\n"
            f"State is unknown — bot will not place trades.\n"
            f"Check MT5 connection and restart."
        )
        return {"mode": "aborted", "reason": msg}
    if len(positions) > 1:
        msg = (
            f"Multiple bot positions found ({len(positions)}). "
            f"Manual review required — bot will not place new trades."
        )
        log.error(msg)
        tg.send_message(f"🚨 RECOVERY ABORTED\n{msg}")
        return {"mode": "aborted", "reason": msg}

    if positions:
        p = positions[0]

        # ------------------------------------------------------------------
        # DEEP VERIFY recovered position before resuming management.
        # get_bot_positions filtered by magic+symbol, but we should also
        # confirm volume, comment, and age make sense for our strategy.
        # ------------------------------------------------------------------
        verify_failures = []

        # Volume must match strategy lot (no partial closes expected).
        # Tolerance of 0.001 lots to handle float rounding from broker.
        if abs(p.volume - config.LOT_SIZE) > 0.001:
            verify_failures.append(
                f"volume mismatch: position={p.volume} expected={config.LOT_SIZE}"
            )

        # Comment must start with our COMMENT prefix (we set f"{COMMENT}_LONG"
        # or f"{COMMENT}_SHORT" on placement — broker may truncate but prefix
        # should survive).
        comment = (p.comment or "").strip()
        if not comment.startswith(config.COMMENT):
            verify_failures.append(
                f"comment mismatch: position='{comment}' expected to start with '{config.COMMENT}'"
            )

        # Position type sanity: must be BUY or SELL (not exotic).
        if p.type not in (mt5.POSITION_TYPE_BUY, mt5.POSITION_TYPE_SELL):
            verify_failures.append(f"unexpected position type: {p.type}")

        # Age: position opened more than 48h ago is suspicious for a
        # 1-trade-per-day strategy with EOD cleanup.
        try:
            from datetime import datetime as _dt
            open_time = _dt.utcfromtimestamp(p.time)
            server_now_for_age = get_server_time()
            if server_now_for_age is not None:
                age_hours = (server_now_for_age - open_time).total_seconds() / 3600
                if age_hours > 48:
                    verify_failures.append(
                        f"position age {age_hours:.1f}h exceeds 48h limit "
                        f"(opened {open_time}, server now {server_now_for_age})"
                    )
        except Exception as e:
            log.warning(f"Could not compute position age: {e}")

        if verify_failures:
            msg = (
                f"Recovered position ticket {p.ticket} FAILED deep verification: "
                f"{'; '.join(verify_failures)}. Halting — manual review required."
            )
            log.error(msg)
            tg.send_message(
                f"🚨 RECOVERY ABORTED — position failed deep verify\n"
                f"Ticket: {p.ticket}\n"
                f"Issues:\n  - " + "\n  - ".join(verify_failures) +
                f"\nBot will not manage this position. Manual review required."
            )
            return {"mode": "aborted", "reason": msg}

        side = "LONG" if p.type == mt5.POSITION_TYPE_BUY else "SHORT"
        entry_price = float(p.price_open)
        lock_idx = _infer_lock_step_from_sl(side, entry_price, p.sl)

        log.info(
            f"♻️ Recovering position {p.ticket} | {side} @ {entry_price} | "
            f"vol={p.volume} comment='{p.comment}' | "
            f"SL={p.sl} TP={p.tp} | inferred lock step={lock_idx + 1}"
        )

        # CRITICAL: if bot crashed after entry fill but before OCO cancel,
        # the opposite pending may still be live and could trigger another
        # entry. Check + clear ANY bot pendings before resuming management.
        leftover = get_bot_orders()
        if leftover is None:
            msg = (
                f"Cannot verify pendings state during position recovery (MT5 "
                f"query failed). Risk of leftover opposite pending triggering. "
                f"Halting."
            )
            log.error(msg)
            tg.send_message(f"🚨 RECOVERY ABORTED\n{msg}")
            return {"mode": "aborted", "reason": msg}

        if leftover:
            log.warning(
                f"Found {len(leftover)} leftover pending(s) alongside open position. "
                f"Cancelling before resuming management."
            )
            result = cancel_bot_pending_orders()
            if result is None:
                msg = (
                    "Could not cancel leftover pendings during position recovery. "
                    "Risk of double-fill. Halting."
                )
                log.error(msg)
                tg.send_message(
                    f"🚨 RECOVERY ABORTED\n"
                    f"Found leftover pendings alongside position {p.ticket}\n"
                    f"Cancellation failed — manually cancel in MT5."
                )
                return {"mode": "aborted", "reason": msg}
            log.info(f"Cleared {result} leftover pending(s).")

        record_position_resumed(
            ticket=p.ticket, side=side, entry=entry_price,
            sl=p.sl, tp=p.tp, lock_idx=lock_idx,
        )
        tg.send_message(
            f"♻️ Bot restarted — existing position recovered\n"
            f"Ticket: {p.ticket}\n"
            f"Side: {side}\n"
            f"Entry: {entry_price:.2f}\n"
            f"Current SL: {p.sl}\n"
            f"Current TP: {p.tp}\n"
            f"Lock step inferred: {lock_idx + 1}\n"
            f"Leftover pendings: {len(leftover) if leftover else 0} cancelled\n"
            f"Resuming trail management."
        )
        return {
            "mode": "resume_position",
            "position": p,
            "side": side,
            "entry_price": entry_price,
            "lock_idx": lock_idx,
        }

    # ========================================================================
    # Scenario 2 — Existing pending orders (no position)
    # ========================================================================
    pendings = get_bot_orders()
    if pendings is None:
        msg = "MT5 orders query failed at startup — state unknown. Halting."
        log.error(msg)
        tg.send_message(
            f"🚨 STARTUP HALT\nCannot read pending orders from MT5.\n"
            f"State is unknown — bot will not place trades.\n"
            f"Check MT5 connection and restart."
        )
        return {"mode": "aborted", "reason": msg}
    if pendings:
        buy_ticket: Optional[int] = None
        sell_ticket: Optional[int] = None
        inferred_anchor: Optional[float] = None

        for o in pendings:
            if o.type == mt5.ORDER_TYPE_BUY_STOP:
                buy_ticket = o.ticket
                inferred_anchor = o.price_open - config.TRIGGER_DIST
            elif o.type == mt5.ORDER_TYPE_SELL_STOP:
                sell_ticket = o.ticket
                if inferred_anchor is None:
                    inferred_anchor = o.price_open + config.TRIGGER_DIST

        if inferred_anchor is None:
            log.warning(
                "Bot pendings exist but not buy/sell stops — cancelling and skipping."
            )
            result = cancel_bot_pending_orders()
            if result is None:
                return {"mode": "aborted", "reason": "non-OCO pendings cleanup query failed"}
            return {"mode": "no_recovery"}

        # OCO completeness: require BOTH buy stop AND sell stop.
        # One-sided resume would mean trading directionally — strategy is OCO breakout.
        if buy_ticket is None or sell_ticket is None:
            log.warning(
                f"Incomplete OCO pendings found (buy={buy_ticket}, sell={sell_ticket}). "
                f"Cancelling survivor and skipping recovery."
            )
            result = cancel_bot_pending_orders()
            if result is None:
                return {"mode": "aborted", "reason": "incomplete OCO cleanup query failed"}
            tg.send_message(
                f"⚠️ Incomplete OCO pendings cancelled\n"
                f"Buy ticket: {buy_ticket}\n"
                f"Sell ticket: {sell_ticket}\n"
                f"Strategy needs BOTH sides — waiting for next anchor."
            )
            return {"mode": "no_recovery"}

        # Stale-order check by AGE (>24h since setup) — more robust than
        # comparing dates across timezones. ORDER_TIME_DAY should auto-cancel
        # but not all brokers honor it consistently.
        # Check ALL pendings — if ANY is stale, cancel all and start fresh.
        server_now = get_server_time()
        if server_now is not None:
            from datetime import datetime as _dt
            max_age_hours = 0.0
            for p in pendings:
                p_setup = _dt.utcfromtimestamp(p.time_setup_msc / 1000)
                p_age = (server_now - p_setup).total_seconds() / 3600
                if p_age > max_age_hours:
                    max_age_hours = p_age
            if max_age_hours > 25:
                log.warning(
                    f"At least one pending order is {max_age_hours:.1f}h old (stale). "
                    f"Cancelling ALL and skipping to fresh anchor."
                )
                result = cancel_bot_pending_orders()
                if result is None:
                    return {"mode": "aborted", "reason": "stale cleanup query failed"}
                tg.send_message(
                    f"♻️ Stale pending orders cancelled\n"
                    f"Max age: {max_age_hours:.1f} hours\n"
                    f"Bot will wait for next 02:00 anchor."
                )
                return {"mode": "no_recovery"}

        levels = build_order_prices(inferred_anchor)
        log.info(
            f"♻️ Recovering pendings | buy={buy_ticket} sell={sell_ticket} | "
            f"anchor inferred={inferred_anchor:.2f}"
        )
        record_pendings_resumed(
            buy_ticket=buy_ticket, sell_ticket=sell_ticket, anchor=inferred_anchor,
        )
        tg.send_message(
            f"♻️ Bot restarted — pending orders recovered\n"
            f"Buy ticket: {buy_ticket}\n"
            f"Sell ticket: {sell_ticket}\n"
            f"Inferred anchor: {inferred_anchor:.2f}\n"
            f"Resuming watch for trigger."
        )
        return {
            "mode": "resume_pendings",
            "buy_ticket": buy_ticket,
            "sell_ticket": sell_ticket,
            "levels": levels,
        }

    # ========================================================================
    # Scenario 3 — Late anchor (started today after 02:00 broker)
    # ========================================================================
    server_now = get_server_time()
    if server_now is None:
        log.warning("Cannot check late anchor: server time unavailable.")
        return {"mode": "no_recovery"}

    if server_now.weekday() >= 5:
        log.info(f"Weekend ({server_now.strftime('%A')}). No late recovery.")
        return {"mode": "no_recovery"}

    if server_now.hour < config.ANCHOR_HOUR:
        log.info(
            f"Pre-anchor on startup (broker hour {server_now.hour:02d}). Normal wait."
        )
        return {"mode": "no_recovery"}

    if server_now.hour >= config.EOD_CANCEL_HOUR:
        log.info(
            f"Past EOD ({config.EOD_CANCEL_HOUR}:00 broker). No late recovery."
        )
        return {"mode": "no_recovery"}

    log.info(
        f"Server time {server_now} is past anchor — attempting late capture "
        f"for {server_now.date()}..."
    )

    anchor = capture_anchor_price(server_now.date())
    if anchor is None:
        log.warning("Late anchor capture failed — no recovery.")
        return {"mode": "no_recovery"}

    tick = get_tick()
    if tick is None:
        log.warning("No tick — no late recovery.")
        return {"mode": "no_recovery"}

    levels = build_order_prices(anchor)
    bid = tick.bid
    ask = tick.ask

    # Safe entry zone: bid above short trigger AND ask below long trigger.
    # If either side has already been triggered (price moved past), refuse.
    in_safe_zone = (levels.short_entry < bid) and (ask < levels.long_entry)

    if not in_safe_zone:
        reason = _format_skip_reason(anchor, levels, bid, ask)
        log.warning(f"Late anchor recovery skipped: {reason}")
        record_late_recovery(status="skipped", anchor=anchor, reason=reason)
        tg.send_message(
            f"⏭ Late anchor skipped\n{reason}\nWaiting for next day's anchor."
        )
        return {"mode": "no_recovery"}

    log.info(
        f"✓ Late anchor recovery: anchor=${anchor:.2f}, "
        f"bid={bid} ask={ask} still in safe zone"
    )
    record_late_recovery(status="captured", anchor=anchor)
    tg.send_message(
        f"♻️ Late anchor recovery\n"
        f"Anchor: {anchor:.2f}\n"
        f"Bid/Ask: {bid:.2f} / {ask:.2f}\n"
        f"Price still in safe entry zone.\n"
        f"Placing OCO pendings now (delayed start)."
    )

    buy_ticket, sell_ticket = place_pending_orders(levels)
    if buy_ticket is None or sell_ticket is None:
        log.error("Late OCO placement failed — cancelling survivors.")
        result = cancel_bot_pending_orders()
        if result is None:
            return {"mode": "aborted", "reason": "late OCO cleanup query failed"}
        tg.send_message("❌ Late OCO placement failed. Day skipped.")
        return {"mode": "no_recovery"}

    return {
        "mode": "late_anchor",
        "buy_ticket": buy_ticket,
        "sell_ticket": sell_ticket,
        "levels": levels,
    }
