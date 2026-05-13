"""
XAUUSD Anchor Strategy Bot — entry point.

Run with:
    python main.py

Make sure:
  - MT5 terminal is running and logged in (preferably DEMO first)
  - Algorithmic trading is enabled in MT5
  - config.py is set for your broker
"""

import os
import time as time_module

import MetaTrader5 as mt5

import config
from core.exceptions import HaltBot
from core.logger import setup_logger
from core.mt5_client import connect_mt5, disconnect_mt5, get_account_info
from core.start_log import (
    get_recent_halt_within,
    record_bot_started,
    record_bot_stopped,
)
from core.status_file import start_status_writer
from core.telegram_notifier import TelegramNotifier
from strategy.day_runner import manage_active_position, run_day
from strategy.orders import build_order_prices
from strategy.recovery import attempt_startup_recovery


def main() -> None:
    log = setup_logger()
    tg = TelegramNotifier(log=log)

    log.info("=" * 70)
    log.info("XAUUSD Anchor Bot starting")
    log.info(f"Symbol={config.SYMBOL} | Lot={config.LOT_SIZE} | Magic={config.MAGIC}")
    log.info(
        f"Anchor={config.ANCHOR_HOUR}:00 | Trigger=±${config.TRIGGER_DIST} "
        f"| TP=±${config.TP_DIST} | SL=±${config.SL_DIST}"
    )
    log.info(
        f"Lock step=${config.LOCK_STEP} × {config.LOCK_STEPS_COUNT} levels "
        f"({config.LOCK_STEP * config.LOCK_STEPS_COUNT:.2f} max)"
    )
    log.info(
        f"Safety: max_spread=${config.MAX_SPREAD_USD}, "
        f"max_daily_loss={config.MAX_DAILY_LOSS_PCT}%, EOD_cancel={config.EOD_CANCEL_HOUR}:00"
    )
    log.info("=" * 70)

    # ----- Recent-halt detection (anti-watchdog-loop guard) -----
    # If the bot HaltBot'd recently, refuse to restart blindly. The same
    # broker condition could trigger an infinite halt loop. User must
    # acknowledge by setting ANCHOR_BOT_FORCE_RESUME=1 in env.
    recent_halt = get_recent_halt_within(hours=1.0)
    if recent_halt is not None:
        force_resume = os.environ.get("ANCHOR_BOT_FORCE_RESUME", "").strip() == "1"
        halt_reason = recent_halt.get("reason", "unknown")
        halt_time = recent_halt.get("ist_time", "unknown")
        if not force_resume:
            log.error(
                f"Recent halt detected at {halt_time}: {halt_reason}"
            )
            log.error(
                "Refusing to auto-restart to prevent watchdog halt-loop."
            )
            log.error(
                "Verify the broker state is OK, then either:"
            )
            log.error(
                "  1. Set environment variable ANCHOR_BOT_FORCE_RESUME=1 and restart, OR"
            )
            log.error(
                "  2. Wait 1 hour for the cooldown to expire."
            )
            tg.send_message(
                f"🛑 STARTUP BLOCKED\n"
                f"Recent halt at {halt_time}\n"
                f"Reason: {halt_reason}\n\n"
                f"To resume:\n"
                f"  - Verify broker state in MT5\n"
                f"  - Set ANCHOR_BOT_FORCE_RESUME=1\n"
                f"  - Restart bot"
            )
            return
        log.warning(
            f"Recent halt detected ({halt_reason} at {halt_time}) but "
            f"ANCHOR_BOT_FORCE_RESUME=1 — proceeding."
        )
        tg.send_message(
            f"⚠️ Restarting after recent halt\n"
            f"Halt reason: {halt_reason}\n"
            f"Force-resumed by env var."
        )

    if not connect_mt5():
        tg.send_message("❌ MT5 connection failed. Bot not started.")
        return

    info = get_account_info()
    if info is not None:
        mode_map = {0: "DEMO", 1: "CONTEST", 2: "LIVE"}
        mode = mode_map.get(info.trade_mode, "?")
        # ----- Startup sanity summary -----
        startup_positions = mt5.positions_get(symbol=config.SYMBOL) or ()
        startup_orders = mt5.orders_get(symbol=config.SYMBOL) or ()
        bot_positions = [p for p in startup_positions if p.magic == config.MAGIC]
        bot_orders = [o for o in startup_orders if o.magic == config.MAGIC]
        other_positions = [p for p in startup_positions if p.magic != config.MAGIC]
        other_orders = [o for o in startup_orders if o.magic != config.MAGIC]
        server_now_str = "n/a"
        try:
            tick = mt5.symbol_info_tick(config.SYMBOL)
            if tick:
                from datetime import datetime as _dt
                server_now_str = _dt.utcfromtimestamp(tick.time).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

        log.info("=" * 60)
        log.info(f"STARTUP SANITY SUMMARY")
        log.info(f"  Account: {info.login}  Broker: {info.server}")
        log.info(f"  Equity: ${info.equity:.2f}  Balance: ${info.balance:.2f}")
        log.info(f"  Symbol: {config.SYMBOL}  Lot: {config.LOT_SIZE:.2f}")
        log.info(f"  Server time: {server_now_str}")
        log.info(f"  Bot positions: {len(bot_positions)}  Bot pendings: {len(bot_orders)}")
        log.info(f"  Non-bot positions (other magics): {len(other_positions)}")
        log.info(f"  Non-bot pendings (other magics): {len(other_orders)}")
        log.info("=" * 60)

        record_bot_started(
            account=info.login,
            broker=info.server,
            mode=mode,
            symbol=config.SYMBOL,
            lot=config.LOT_SIZE,
        )
        # Background thread writes logs/status.json every 30s. The watchdog
        # supervisor reads this file to respond to /status commands.
        start_status_writer(interval_sec=30)
        tg.send_message(
            f"🚀 XAUUSD Anchor Bot started\n"
            f"Account: {info.login}\n"
            f"Broker: {info.server}\n"
            f"Server time: {server_now_str}\n"
            f"Equity: ${info.equity:.2f}\n"
            f"Symbol: {config.SYMBOL}  Lot: {config.LOT_SIZE:.2f}\n"
            f"Bot positions: {len(bot_positions)}  pendings: {len(bot_orders)}\n"
            f"Other positions: {len(other_positions)}  pendings: {len(other_orders)}"
        )

    try:
        # ====================================================================
        # STARTUP RECOVERY
        # ====================================================================
        recovery = attempt_startup_recovery(tg)
        mode = recovery.get("mode", "no_recovery")

        if mode == "resume_position":
            # Recovered an existing open position → resume trail directly
            entry = recovery["entry_price"]
            dummy_levels = build_order_prices(entry)  # placeholder
            manage_active_position(
                None, None, dummy_levels, tg,
                recovered_position=recovery["position"],
                recovered_side=recovery["side"],
                recovered_entry_price=entry,
                recovered_lock_idx=recovery["lock_idx"],
            )
            log.info("Recovered position handled. Resuming daily loop.")
        elif mode == "resume_pendings":
            manage_active_position(
                recovery["buy_ticket"], recovery["sell_ticket"],
                recovery["levels"], tg,
            )
            log.info("Recovered pendings handled. Resuming daily loop.")
        elif mode == "late_anchor":
            manage_active_position(
                recovery["buy_ticket"], recovery["sell_ticket"],
                recovery["levels"], tg,
            )
            log.info("Late anchor day handled. Resuming daily loop.")
        elif mode == "aborted":
            log.error(
                f"Recovery aborted: {recovery.get('reason')}. "
                f"Bot will EXIT — manual review required before restart."
            )
            record_bot_stopped(reason=f"recovery_aborted: {recovery.get('reason')}")
            tg.send_message(
                f"🛑 BOT EXITING\n"
                f"Recovery aborted: {recovery.get('reason')}\n"
                f"Resolve the issue in MT5, then restart."
            )
            return  # exit main() — do NOT enter the daily loop

        # ====================================================================
        # NORMAL DAILY LOOP
        # ====================================================================
        while True:
            try:
                run_day(tg)
            except HaltBot as e:
                # Critical state detected at runtime — exit cleanly
                log.error(f"Bot halted: {e}", exc_info=True)
                record_bot_stopped(reason=f"halt: {e}")
                tg.send_message(
                    f"🛑 BOT HALTED AT RUNTIME\n"
                    f"Reason: {e}\n"
                    f"Resolve in MT5 and restart."
                )
                break
            except Exception as e:
                log.error(f"run_day failed: {e}", exc_info=True)
                tg.send_message(f"⚠️ run_day error (recovering): {e}")
            log.info("Day cycle complete. Sleeping 60s before next cycle.")
            time_module.sleep(60)
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
        record_bot_stopped(reason="keyboard_interrupt")
        tg.send_message("📴 Bot stopped by user.")
    finally:
        disconnect_mt5()


if __name__ == "__main__":
    main()