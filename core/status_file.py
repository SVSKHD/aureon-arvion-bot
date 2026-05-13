"""
Periodic status snapshot for external watchdog/monitor processes.

Background thread writes logs/status.json every N seconds with current
bot state: account, positions, pendings, last event. The watchdog process
reads this file to respond to /status commands from Telegram.
"""

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import MetaTrader5 as mt5

import config
from core.logger import get_logger

log = get_logger()

IST = timezone(timedelta(hours=5, minutes=30))
_PROJECT_ROOT = Path(config.__file__).resolve().parent
STATUS_PATH = _PROJECT_ROOT / "logs" / "status.json"

_stop_event = threading.Event()


def _gather_status() -> dict:
    """Snapshot current bot state from MT5."""
    snap = {
        "updated_at_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "updated_at_unix": int(time.time()),
        "state": "running",
        "symbol": config.SYMBOL,
        "magic": config.MAGIC,
        "lot_size": config.LOT_SIZE,
        "tp_dist": config.TP_DIST,
        "sl_dist": config.SL_DIST,
    }
    try:
        info = mt5.account_info()
        if info is not None:
            snap["account"] = info.login
            snap["broker"] = info.server
            snap["balance"] = float(info.balance)
            snap["equity"] = float(info.equity)
            snap["currency"] = info.currency
            snap["margin_level"] = float(info.margin_level) if info.margin else 0.0

        positions = mt5.positions_get(symbol=config.SYMBOL) or ()
        bot_positions = [p for p in positions if p.magic == config.MAGIC]
        snap["positions_count"] = len(bot_positions)
        # Distinguish original vs rescue by comment suffix
        if bot_positions:
            originals = [p for p in bot_positions if "_RESCUE" not in (p.comment or "")]
            rescues = [p for p in bot_positions if "_RESCUE" in (p.comment or "")]
            if originals:
                p = originals[0]
                snap["active_position"] = {
                    "ticket": int(p.ticket),
                    "side": "LONG" if p.type == mt5.POSITION_TYPE_BUY else "SHORT",
                    "entry": float(p.price_open),
                    "current": float(p.price_current),
                    "sl": float(p.sl),
                    "tp": float(p.tp),
                    "volume": float(p.volume),
                    "floating_pnl": float(p.profit),
                    "swap": float(p.swap),
                }
            if rescues:
                p = rescues[0]
                snap["rescue_position"] = {
                    "ticket": int(p.ticket),
                    "side": "LONG" if p.type == mt5.POSITION_TYPE_BUY else "SHORT",
                    "entry": float(p.price_open),
                    "current": float(p.price_current),
                    "sl": float(p.sl),
                    "tp": float(p.tp),
                    "volume": float(p.volume),
                    "floating_pnl": float(p.profit),
                }

        orders = mt5.orders_get(symbol=config.SYMBOL) or ()
        bot_orders = [o for o in orders if o.magic == config.MAGIC]
        snap["orders_count"] = len(bot_orders)
        if bot_orders:
            snap["pending_orders"] = [
                {
                    "ticket": int(o.ticket),
                    "type": "BUY_STOP" if o.type == mt5.ORDER_TYPE_BUY_STOP else "SELL_STOP",
                    "price": float(o.price_open),
                    "sl": float(o.sl),
                    "tp": float(o.tp),
                }
                for o in bot_orders
            ]

        tick = mt5.symbol_info_tick(config.SYMBOL)
        if tick is not None:
            snap["bid"] = float(tick.bid)
            snap["ask"] = float(tick.ask)
            snap["spread_usd"] = round(float(tick.ask - tick.bid), 2)
            from datetime import datetime as _dt
            snap["server_time"] = _dt.utcfromtimestamp(tick.time).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        # Weekly profit lock state (if enabled)
        if getattr(config, "WEEKLY_PROFIT_LOCK_ENABLED", False):
            try:
                from core.start_log import get_weekly_pnl
                from core.runtime_config import get as rt_get
                from datetime import datetime as _dt
                server_now = _dt.utcfromtimestamp(tick.time) if tick else _dt.utcnow()
                weekly_pnl = get_weekly_pnl(server_now)
                target = float(rt_get("WEEKLY_PROFIT_LOCK_USD", 0.0))
                iso = server_now.isocalendar()
                snap["weekly_lock"] = {
                    "enabled": True,
                    "iso_week": f"{iso[0]}-W{iso[1]:02d}",
                    "weekly_pnl": weekly_pnl,
                    "target": target,
                    "locked": weekly_pnl >= target,
                    "remaining_to_lock": max(0.0, round(target - weekly_pnl, 2)),
                }
            except Exception as e:
                snap["weekly_lock"] = {"enabled": True, "error": str(e)}
        else:
            snap["weekly_lock"] = {"enabled": False}
    except Exception as e:
        snap["error"] = str(e)
    return snap


def write_status() -> None:
    """Write snapshot to status.json (atomic via rename)."""
    try:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        snap = _gather_status()
        tmp = STATUS_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=2)
        tmp.replace(STATUS_PATH)
    except Exception as e:
        log.warning(f"Failed to write status.json: {e}")


def _loop(interval_sec: int) -> None:
    while not _stop_event.is_set():
        write_status()
        _stop_event.wait(interval_sec)


def start_status_writer(interval_sec: int = 30) -> None:
    """Start background daemon thread writing status.json every N seconds."""
    t = threading.Thread(target=_loop, args=(interval_sec,), daemon=True)
    t.start()
    log.info(f"Status writer started (every {interval_sec}s → {STATUS_PATH.name})")


def stop_status_writer() -> None:
    _stop_event.set()