# anchor_high_low_tracker.py
#
# Live tracker for 02:00 broker-time anchor with ZigZag-style swing pivots
# AND a paper-trading engine layered on top. The "anchor" is the last
# confirmed pivot point. It flips only when price reverses by
# REVERSAL_THRESHOLD_USD from the running extreme.
#
# Paper trade rules:
#   - When the latest pivot is LOW and price moves +TRADE_TRIGGER_USD above it,
#     place a LONG (paper).
#   - When the latest pivot is HIGH and price moves -TRADE_TRIGGER_USD below it,
#     place a SHORT.
#   - Each trade has its own TRAIL_STOP_USD trailing stop.
#   - If price moves MASTER_CLOSE_USD against the latest pivot's direction,
#     close every open trade regardless of which pivot spawned it.
#
# Daily anchor stats persist to anchor_daily_log.csv.
# Every closed trade appends to paper_trades.csv with full entry/exit details.
#
# Changes vs original:
#   - M5 → M1 timeframe (timestamps precise to ±1 min instead of ±5 min)
#   - Anchor price cached once per day (was re-fetched every second)
#   - Broker time read from MT5 epoch correctly (was sensitive to local TZ)
#   - Datetimes passed back to MT5 are tagged UTC so the wrapper doesn't
#     silently reinterpret them through the local machine's timezone
#   - Weekend skip in anchor calc (was crashing Mon morning before 02:00)
#   - Anchor candle timestamp verified (catches data gaps)
#   - Main loop wrapped in try/except with retry (was dying on any blip)
#   - Daily CSV log (one row per anchor date, updates in place during the day)
#   - Live one-line heartbeat showing current bid + delta vs anchor (overwrites
#     in place; full multi-line report only on new extreme)

import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import MetaTrader5 as mt5


SYMBOL = "XAUUSD"
TIMEFRAME = mt5.TIMEFRAME_M1

ANCHOR_HOUR = 2
ANCHOR_MINUTE = 0

POLL_SECONDS = 1
RETRY_SECONDS = 5  # sleep after a transient error before retrying
HEARTBEAT_WIDTH = 200  # padding for the live one-liner so old chars get cleared

# ZigZag pivot threshold. The anchor (= last confirmed pivot) only flips when
# price reverses by this many dollars from the running extreme. Larger value
# = fewer, more meaningful pivots; smaller = more responsive but noisier.
# Typical: $1–$5 for XAUUSD, $0.05–$0.20 for XAGUSD.
REVERSAL_THRESHOLD_USD = 2.0

# === Paper-trade strategy ===
# Each confirmed pivot can place ONE trade when price moves TRADE_TRIGGER_USD
# away from it in the anchor's direction (LOW→LONG, HIGH→SHORT). The trade has
# a TRAIL_STOP_USD trailing stop. A move of MASTER_CLOSE_USD against the most
# recent pivot closes every open trade regardless of direction.
TRADE_TRIGGER_USD = 10.0
TRAIL_STOP_USD = 0.30
MASTER_CLOSE_USD = 5.0
LOT_SIZE = 0.5

# === Ladder strategy (the active one) ===
# Pyramiding ladder: from a bidirectional anchor (the 2am open initially), each
# $10 favourable move from the latest entry fires a new position. The 'lock' is
# the conceptual safety floor for the whole ladder — execution happens when
# price reverses MASTER_CLOSE_BUFFER_USD past the latest entry, which closes
# every position in the ladder at that price. The master-close price then
# becomes the new bidirectional anchor, and the opposite ladder can begin.
STRATEGY_MODE = "ladder"        # "ladder" (active) or "pivot" (old engine, kept for reference)
LADDER_STEP_USD = 10.0          # distance between rungs and between anchor and first entry
MASTER_CLOSE_BUFFER_USD = 5.0   # how far past the latest entry triggers all-close

# === Ladder safety caps ===
# Hard limits on ladder growth. Without these, a weekend gap or news spike that
# jumps price past several rung triggers between two polls would pyramid the
# ladder in a single iteration of the entry loop. MAX_LADDER_RUNGS is the real
# teeth (an absolute ceiling on concurrent rungs); MAX_RUNGS_PER_TICK just
# smooths the build rate so one poll can't slam on many rungs at once;
# MAX_EXPOSURE_LOTS is a belt-and-suspenders lot ceiling that stays correct
# even if LOT_SIZE changes. NOTE: these bound *exposure during a runaway
# move* — they are NOT gap protection. A gap that jumps THROUGH the master
# close still fills every open rung at the gapped price; the only real defence
# against that is a hard max-loss-per-cycle stop, which this strategy does not
# yet have. See the realism notes where master close is handled.
MAX_RUNGS_PER_TICK = 2          # most new rungs that may fire in one poll iteration
MAX_LADDER_RUNGS = 8            # absolute ceiling on concurrent open rungs in a ladder
MAX_EXPOSURE_LOTS = 4.0         # absolute ceiling on total open lots (== MAX_LADDER_RUNGS * LOT_SIZE here)

# When the tracker boots mid-day, replay historical M1 bars through the trade
# engine to synthesise the paper trades that would have fired between 02:00
# and now. Each backfilled trade is tagged `backfill=True` in the events log
# and CSV so it's separable from live forward-going trades. Set to False if
# you only want strictly-live behaviour (cleaner for parallel-bot comparison).
BACKFILL_TRADES_ON_START = True

IST = ZoneInfo("Asia/Kolkata")

# Persistent files — symbol-prefixed so two tracker instances (e.g., one for
# XAUUSD and one for XAGUSD) running in parallel never collide on writes.
SYMBOL_PREFIX = SYMBOL.lower()
LOG_PATH = f"{SYMBOL_PREFIX}_anchor_daily_log.csv"
TRADE_LOG_PATH = f"{SYMBOL_PREFIX}_paper_trades.csv"
EVENT_LOG_PATH = f"{SYMBOL_PREFIX}_events.jsonl"

# Old (non-prefixed) paths from earlier versions — auto-migrated at startup
LEGACY_PATHS = {
    "anchor_daily_log.csv": LOG_PATH,
    "paper_trades.csv": TRADE_LOG_PATH,
}
LOG_FIELDS = [
    "date",
    "anchor_price",
    "anchor_server_time",
    "high_price",
    "high_server_time",
    "high_dollars",
    "high_pips",
    "low_price",
    "low_server_time",
    "low_dollars",
    "low_pips",
    "last_price",
    "last_price_dollars",
    "candles_read",
    "last_update_server",
    "last_update_ist",
]


def connect_mt5():
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    if not mt5.symbol_select(SYMBOL, True):
        raise RuntimeError(f"Symbol select failed: {SYMBOL}")


def get_ist_now():
    return datetime.now(IST)


def broker_dt(epoch: int) -> datetime:
    """Convert an MT5 epoch (broker wall-clock as Unix seconds) to a naive
    datetime matching what MetaTrader displays. Independent of local TZ."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)


def to_epoch(dt: datetime) -> int:
    """Inverse of broker_dt: naive broker datetime → Unix epoch the same way
    MT5 reports it. Tagging UTC avoids local-TZ interpretation by .timestamp()."""
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def to_mt5_dt(dt: datetime) -> datetime:
    """Tag a naive broker-time datetime as UTC before passing to MT5.
    The MT5 Python wrapper converts naive datetimes using the local machine
    TZ, which silently breaks anchor lookups on any machine that isn't on
    the broker's timezone. Tagging UTC matches how broker_dt() reads back."""
    return dt.replace(tzinfo=timezone.utc)


def get_server_now(symbol: str) -> datetime:
    """Current broker wall-clock time as a naive datetime."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise RuntimeError(f"No tick data for {symbol}")
    return broker_dt(tick.time)


def get_current_anchor_time(server_now: datetime) -> datetime:
    """Most recent 02:00 anchor in broker time. Steps back over weekends so we
    never request a candle on Sat/Sun (when no bar exists)."""
    anchor = server_now.replace(
        hour=ANCHOR_HOUR,
        minute=ANCHOR_MINUTE,
        second=0,
        microsecond=0,
    )

    if server_now < anchor:
        anchor -= timedelta(days=1)

    while anchor.weekday() >= 5:  # Sat=5, Sun=6
        anchor -= timedelta(days=1)

    return anchor


def get_pip_size(symbol: str) -> float:
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"Symbol info not found: {symbol}")
    if info.digits in (3, 5):
        return info.point * 10
    return info.point


def fetch_anchor(symbol: str, anchor_time: datetime):
    """Fetch the anchor M1 candle. Called once per day, cached by run()."""
    rates = mt5.copy_rates_from(symbol, TIMEFRAME, to_mt5_dt(anchor_time), 1)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"Anchor candle not found at {anchor_time}")

    candle = rates[0]
    candle_dt = broker_dt(candle["time"])
    if candle_dt != anchor_time:
        raise RuntimeError(
            f"Anchor candle mismatch: wanted {anchor_time}, got {candle_dt}"
        )

    return {
        "price": float(candle["open"]),
        "server_time": candle_dt,
    }


def compute_high_low(symbol, anchor_time, anchor_data, server_now, pip_size,
                     rates=None):
    """Compute running high/low using cached anchor. Optionally accepts
    pre-fetched rates to avoid a duplicate MT5 call per poll."""
    if rates is None:
        rates = mt5.copy_rates_range(
            symbol, TIMEFRAME, to_mt5_dt(anchor_time), to_mt5_dt(server_now)
        )
    if rates is None or len(rates) == 0:
        return None

    anchor_price = anchor_data["price"]

    highest = max(rates, key=lambda x: x["high"])
    lowest = min(rates, key=lambda x: x["low"])

    high_price = float(highest["high"])
    low_price = float(lowest["low"])

    ist_now = get_ist_now()

    return {
        "symbol": symbol,
        "pip_size": pip_size,

        "anchor": {
            "price": anchor_price,
            "server_time": anchor_data["server_time"],
            "detected_ist_time": ist_now,
        },

        "high": {
            "price": high_price,
            "server_time": broker_dt(highest["time"]),
            "detected_ist_time": ist_now,
            "pips_moved": round((high_price - anchor_price) / pip_size, 2),
        },

        "low": {
            "price": low_price,
            "server_time": broker_dt(lowest["time"]),
            "detected_ist_time": ist_now,
            "pips_moved": round((anchor_price - low_price) / pip_size, 2),
        },

        "candles_read": len(rates),
        "checked_server_time": server_now,
        "checked_ist_time": ist_now,
    }


def detect_pivots(rates, initial_pivot: dict, reversal_threshold: float):
    """ZigZag-style pivot detection. Processes M1 bars in time order with one
    confirmed pivot at a time and a running 'candidate' in the opposite
    direction. The candidate becomes a confirmed pivot when price retraces
    from it by reversal_threshold dollars.

    Returns (pivots, candidate) where:
      pivots    = list of confirmed pivots, starting with initial_pivot
      candidate = the running extreme in the direction we're currently
                  tracking, or None if no bar has moved past the last pivot yet

    The candidate is the "would-be next pivot" — useful for the live display
    so the user can see where the next flip would land if price reverses now."""
    pivots = [initial_pivot]
    candidate = None
    if rates is None or len(rates) == 0:
        return pivots, candidate

    sorted_rates = sorted(rates, key=lambda b: b["time"])
    last_pivot_ts = to_epoch(initial_pivot["server_time"])

    for bar in sorted_rates:
        if bar["time"] <= last_pivot_ts:
            continue

        bar_time = broker_dt(bar["time"])
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        last_type = pivots[-1]["type"]

        if last_type in ("LOW", "OPEN"):
            # Tracking upward — candidate is a HIGH
            if candidate is None or bar_high > candidate["price"]:
                candidate = {"price": bar_high, "server_time": bar_time, "type": "HIGH"}
            # Confirm if this bar's low retraced from candidate by threshold
            if bar_low <= candidate["price"] - reversal_threshold:
                pivots.append(candidate)
                last_pivot_ts = to_epoch(candidate["server_time"])
                # Start the next (LOW) candidate at this bar's low
                candidate = {"price": bar_low, "server_time": bar_time, "type": "LOW"}
        else:  # last_type == "HIGH"
            # Tracking downward — candidate is a LOW
            if candidate is None or bar_low < candidate["price"]:
                candidate = {"price": bar_low, "server_time": bar_time, "type": "LOW"}
            if bar_high >= candidate["price"] + reversal_threshold:
                pivots.append(candidate)
                last_pivot_ts = to_epoch(candidate["server_time"])
                candidate = {"price": bar_high, "server_time": bar_time, "type": "HIGH"}

    return pivots, candidate


# === Paper-trading engine ===

TRADE_FIELDS = [
    "trade_id", "cycle_id", "session_date",
    "direction", "lot", "backfill",
    "entry_price", "entry_server_time", "entry_ist_time",
    "anchor_type", "anchor_price", "anchor_server_time",
    "trigger_reason",
    "exit_price", "exit_server_time", "exit_ist_time", "exit_reason",
    "duration_seconds",
    "trail_stop_at_exit", "high_water", "low_water",
    "mfe_price", "mae_price", "mfe_pips", "mae_pips",
    "price_distance", "pips_moved",
    "pnl_usd",
]


def anchor_key(anchor: dict) -> str:
    """Unique key for an anchor instance — prevents re-entering from the same
    pivot more than once."""
    return f"{anchor['type']}_{anchor['server_time'].isoformat()}"


def make_paper_trade(trade_id: int, direction: str, entry_price: float,
                     server_time: datetime, ist_time: datetime,
                     anchor: dict, lot: float) -> dict:
    """Construct an OPEN paper trade. Trail stop starts TRAIL_STOP_USD behind
    the entry; water marks track the favourable excursion for trail updates."""
    if direction == "LONG":
        trail = entry_price - TRAIL_STOP_USD
    else:
        trail = entry_price + TRAIL_STOP_USD
    return {
        "trade_id": trade_id,
        "cycle_id": None,            # filled in by ladder engine for cycle clustering
        "session_date": None,        # filled in from the active 2am anchor date
        "direction": direction,
        "lot": lot,
        "backfill": False,
        "entry_price": round(entry_price, 4),
        "entry_server_time": server_time,
        "entry_ist_time": ist_time,
        "anchor": anchor,  # full anchor dict, for context
        "anchor_type": anchor["type"],
        "anchor_price": anchor["price"],
        "anchor_server_time": anchor["server_time"],
        "trigger_reason": None,      # FIRST_RUNG | LADDER_CONTINUATION | PIVOT_BREAK
        "trail_stop": round(trail, 4),
        "high_water": entry_price,
        "low_water": entry_price,
        "status": "OPEN",
    }


def trade_pnl_usd(trade: dict, exit_or_current_price: float,
                  contract_size: float) -> float:
    """Dollar P&L for a paper trade. Tries MT5's broker-aware order_calc_profit
    first (which knows the real contract specs, currency conversion, and tick
    value for this broker/account). Falls back to manual contract-size math if
    MT5 is unavailable (e.g. during unit tests or if the symbol isn't in MarketWatch)."""
    pnl = order_calc_pnl(trade["direction"], SYMBOL, trade["lot"],
                         trade["entry_price"], exit_or_current_price)
    if pnl is not None:
        return pnl
    # Manual fallback — assumes USD-denominated symbol with given contract size
    if trade["direction"] == "LONG":
        per_unit = exit_or_current_price - trade["entry_price"]
    else:
        per_unit = trade["entry_price"] - exit_or_current_price
    return per_unit * trade["lot"] * contract_size


def order_calc_pnl(direction: str, symbol: str, lot: float,
                   entry: float, exit_price: float) -> float | None:
    """Use MT5's broker-aware profit calculation. Returns the profit in account
    currency, or None if the call fails (caller falls back to manual math).

    Why this matters: contract_size × price_diff × lot only works for USD-quoted
    symbols on USD accounts. order_calc_profit handles XAUUSD on EUR accounts,
    XAGUSD with non-standard contract specs, etc., and returns the same number
    the broker would actually book."""
    if not hasattr(mt5, "order_calc_profit"):
        return None
    action = mt5.ORDER_TYPE_BUY if direction == "LONG" else mt5.ORDER_TYPE_SELL
    try:
        result = mt5.order_calc_profit(action, symbol, lot, entry, exit_price)
        if result is None:
            # MT5 returns None on failure; check last_error for diagnostics
            return None
        return float(result)
    except Exception:
        return None


def check_paper_entry(latest_pivot: dict, current_bid: float,
                      anchors_used: set, next_id: int,
                      server_now: datetime, ist_now: datetime) -> tuple:
    """If the latest pivot hasn't fired yet AND price has moved TRADE_TRIGGER_USD
    in the anchor's direction, build the trade. Returns (trade_or_None, next_id)."""
    key = anchor_key(latest_pivot)
    if key in anchors_used:
        return None, next_id
    if latest_pivot["type"] == "LOW":
        trigger = latest_pivot["price"] + TRADE_TRIGGER_USD
        if current_bid >= trigger:
            trade = make_paper_trade(next_id, "LONG", trigger, server_now,
                                     ist_now, latest_pivot, LOT_SIZE)
            anchors_used.add(key)
            return trade, next_id + 1
    elif latest_pivot["type"] == "HIGH":
        trigger = latest_pivot["price"] - TRADE_TRIGGER_USD
        if current_bid <= trigger:
            trade = make_paper_trade(next_id, "SHORT", trigger, server_now,
                                     ist_now, latest_pivot, LOT_SIZE)
            anchors_used.add(key)
            return trade, next_id + 1
    return None, next_id


def update_trail(trade: dict, current_bid: float) -> None:
    """Advance the trail stop in the favourable direction only."""
    if trade["direction"] == "LONG":
        if current_bid > trade["high_water"]:
            trade["high_water"] = current_bid
            new_trail = round(current_bid - TRAIL_STOP_USD, 4)
            if new_trail > trade["trail_stop"]:
                trade["trail_stop"] = new_trail
    else:  # SHORT
        if current_bid < trade["low_water"]:
            trade["low_water"] = current_bid
            new_trail = round(current_bid + TRAIL_STOP_USD, 4)
            if new_trail < trade["trail_stop"]:
                trade["trail_stop"] = new_trail


def trail_hit(trade: dict, current_bid: float) -> bool:
    if trade["direction"] == "LONG":
        return current_bid <= trade["trail_stop"]
    return current_bid >= trade["trail_stop"]


def close_paper_trade(trade: dict, exit_price: float, reason: str,
                      server_now: datetime, ist_now: datetime,
                      contract_size: float) -> None:
    """Finalize a trade in place. Computes price_distance, pips, MFE/MAE
    excursions, duration, and dollar P&L (via broker-aware order_calc_profit
    when MT5 is connected, fallback to manual math otherwise)."""
    trade["status"] = "CLOSED"
    trade["exit_price"] = round(exit_price, 4)
    trade["exit_server_time"] = server_now
    trade["exit_ist_time"] = ist_now
    trade["exit_reason"] = reason
    if trade["direction"] == "LONG":
        trade["price_distance"] = round(exit_price - trade["entry_price"], 4)
    else:
        trade["price_distance"] = round(trade["entry_price"] - exit_price, 4)
    trade["pnl_usd"] = round(trade_pnl_usd(trade, exit_price, contract_size), 2)

    # Pips: signed distance / pip_size. For XAUUSD pip_size=0.01 → $1 move = 100 pips.
    pip_size = get_pip_size(SYMBOL)
    if pip_size > 0:
        trade["pips_moved"] = round(trade["price_distance"] / pip_size, 1)
    else:
        trade["pips_moved"] = 0.0
    trade["trail_stop_at_exit"] = trade.get("trail_stop", "")

    # MFE / MAE — derived from the live-updated watermarks.
    # MFE = how far in our favour price went at its best  (potential we could've captured)
    # MAE = how far against us price went at its worst    (paper drawdown we absorbed)
    if trade["direction"] == "LONG":
        favourable = trade["high_water"] - trade["entry_price"]
        adverse = trade["entry_price"] - trade["low_water"]
    else:  # SHORT
        favourable = trade["entry_price"] - trade["low_water"]
        adverse = trade["high_water"] - trade["entry_price"]
    trade["mfe_price"] = round(max(0.0, favourable), 4)
    trade["mae_price"] = round(max(0.0, adverse), 4)
    if pip_size > 0:
        trade["mfe_pips"] = round(trade["mfe_price"] / pip_size, 1)
        trade["mae_pips"] = round(trade["mae_price"] / pip_size, 1)
    else:
        trade["mfe_pips"] = 0.0
        trade["mae_pips"] = 0.0

    # Duration in seconds — straightforward but convenient for downstream analysis
    try:
        delta = (trade["exit_server_time"] - trade["entry_server_time"]).total_seconds()
        trade["duration_seconds"] = round(delta, 1)
    except Exception:
        trade["duration_seconds"] = 0.0


def check_master_close(latest_pivot: dict, current_bid: float) -> bool:
    """Has price moved MASTER_CLOSE_USD in the direction opposite to what the
    latest pivot implies? LOW expects up → opposite is down. HIGH expects
    down → opposite is up. OPEN counts neither way."""
    if latest_pivot["type"] == "LOW":
        return current_bid <= latest_pivot["price"] - MASTER_CLOSE_USD
    if latest_pivot["type"] == "HIGH":
        return current_bid >= latest_pivot["price"] + MASTER_CLOSE_USD
    return False


# === Ladder strategy helpers ===

def make_ladder_trade(trade_id: int, direction: str, entry_price: float,
                      ladder_anchor: float, server_time: datetime,
                      ist_time: datetime, lot: float, ladder_position: int) -> dict:
    """Construct a ladder trade. The 'lock_price' starts at the entry (BE) and
    cascades to each subsequent entry's price as more rungs fire. Master close
    is what actually closes the trade — at latest_entry ∓ MASTER_CLOSE_BUFFER_USD."""
    anchor_stub = {
        "type": "LADDER_LONG" if direction == "LONG" else "LADDER_SHORT",
        "price": ladder_anchor,
        "server_time": server_time,
    }
    trade = make_paper_trade(trade_id, direction, entry_price, server_time,
                             ist_time, anchor_stub, lot)
    trade["lock_price"] = round(entry_price, 4)
    trade["ladder_position"] = ladder_position
    return trade


def cascade_locks(open_trades: list, new_latest_entry: float) -> None:
    """When a new rung fires, lift (LONG) or lower (SHORT) every open trade's
    lock_price to the new latest-entry level. Lock is conceptual — actual exit
    happens at master close. Returns nothing; mutates in place."""
    for t in open_trades:
        if t["direction"] == "LONG":
            if new_latest_entry > t["lock_price"]:
                t["lock_price"] = round(new_latest_entry, 4)
        else:
            if new_latest_entry < t["lock_price"]:
                t["lock_price"] = round(new_latest_entry, 4)


def check_ladder_master_close(direction: str, latest_entry: float,
                              current_bid: float) -> bool:
    """LONG: trigger when bid ≤ latest_entry − buffer.  SHORT: ≥ latest + buffer."""
    if direction == "LONG":
        return current_bid <= latest_entry - MASTER_CLOSE_BUFFER_USD
    if direction == "SHORT":
        return current_bid >= latest_entry + MASTER_CLOSE_BUFFER_USD
    return False


def ladder_master_close_price(direction: str, latest_entry: float) -> float:
    """The exact price at which master close fires (used as fill price for all
    positions, AND as the new anchor for the opposite ladder)."""
    if direction == "LONG":
        return round(latest_entry - MASTER_CLOSE_BUFFER_USD, 4)
    return round(latest_entry + MASTER_CLOSE_BUFFER_USD, 4)


def ladder_next_entry(direction: str, latest_entry: float) -> float:
    """The price at which the next rung fires (latest + 10 for LONG, − 10 for SHORT)."""
    if direction == "LONG":
        return round(latest_entry + LADDER_STEP_USD, 4)
    return round(latest_entry - LADDER_STEP_USD, 4)


def bidirectional_trigger(anchor: float, current_bid: float):
    """When there's no active ladder, decide if price has crossed ±LADDER_STEP_USD
    from the anchor. Returns ('LONG', trigger_price) or ('SHORT', trigger_price)
    or (None, None)."""
    if current_bid >= anchor + LADDER_STEP_USD:
        return "LONG", round(anchor + LADDER_STEP_USD, 4)
    if current_bid <= anchor - LADDER_STEP_USD:
        return "SHORT", round(anchor - LADDER_STEP_USD, 4)
    return None, None


def unrealized_pnl(open_trades: list, current_bid: float,
                   contract_size: float) -> float:
    return sum(trade_pnl_usd(t, current_bid, contract_size) for t in open_trades)


def daily_summary(closed_trades: list, risk_stats: dict | None = None) -> dict:
    """Aggregate the day's closed trades into a single summary dict.
    Used both for end-of-day printout and for the day_finalized JSONL event.

    risk_stats (optional) merges in DD/exposure/rung peaks captured by the
    live engine — those aren't derivable from closed trades alone."""
    if not closed_trades:
        s = {
            "trade_count": 0,
            "wins": 0,
            "losses": 0,
            "breakeven": 0,
            "win_rate_pct": 0.0,
            "total_pips_moved": 0.0,
            "total_pnl_usd": 0.0,
            "best_trade_pnl": 0.0,
            "worst_trade_pnl": 0.0,
            "longs": 0,
            "shorts": 0,
            "by_exit_reason": {},
        }
    else:
        pnls = [float(t.get("pnl_usd", 0)) for t in closed_trades]
        pips = [float(t.get("pips_moved", 0)) for t in closed_trades]
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        breakeven = sum(1 for p in pnls if p == 0)
        longs = sum(1 for t in closed_trades if t.get("direction") == "LONG")
        shorts = sum(1 for t in closed_trades if t.get("direction") == "SHORT")
        by_reason = {}
        for t in closed_trades:
            r = t.get("exit_reason", "?")
            by_reason[r] = by_reason.get(r, 0) + 1
        s = {
            "trade_count": len(closed_trades),
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "win_rate_pct": round(100.0 * wins / len(closed_trades), 1),
            "total_pips_moved": round(sum(pips), 1),
            "total_pnl_usd": round(sum(pnls), 2),
            "best_trade_pnl": round(max(pnls), 2),
            "worst_trade_pnl": round(min(pnls), 2),
            "longs": longs,
            "shorts": shorts,
            "by_exit_reason": by_reason,
        }
    if risk_stats:
        s.update({
            "max_floating_dd": round(float(risk_stats.get("max_floating_dd", 0.0)), 2),
            "max_rungs": int(risk_stats.get("max_rungs", 0)),
            "max_exposure_lots": float(risk_stats.get("max_exposure_lots", 0.0)),
        })
    return s


def print_daily_summary(date_str: str, closed_trades: list,
                        ladder_cycles: int = 0,
                        carryover_trades: list | None = None,
                        current_price: float | None = None,
                        contract_size: float = 100.0,
                        risk_stats: dict | None = None):
    """Pretty-print the day's results to console. Called at day rollover AND on
    shutdown so you can read it at a glance without grepping the JSONL.

    carryover_trades / current_price let the summary show what's still OPEN at
    the boundary — important for a 24/7 bot where positions can span days.
    risk_stats surfaces max DD / peak exposure / peak rungs from the live engine."""
    s = daily_summary(closed_trades, risk_stats=risk_stats)
    print()
    print("=" * 64)
    print(f"  DAILY SUMMARY  —  {date_str}  ({SYMBOL})")
    print("=" * 64)
    print(f"  Trades closed   : {s['trade_count']}  "
          f"({s['longs']} LONG / {s['shorts']} SHORT)")
    print(f"  Wins / Losses   : {s['wins']} / {s['losses']}  "
          f"(win rate {s['win_rate_pct']}%)")
    print(f"  Total pips      : {s['total_pips_moved']:+.1f}")
    print(f"  Realized P&L    : ${s['total_pnl_usd']:+.2f}")
    if s['trade_count'] > 0:
        print(f"  Best / Worst    : ${s['best_trade_pnl']:+.2f}  /  "
              f"${s['worst_trade_pnl']:+.2f}")
    if s["by_exit_reason"]:
        reasons = ", ".join(f"{k}={v}" for k, v in s["by_exit_reason"].items())
        print(f"  Exit reasons    : {reasons}")
    if ladder_cycles > 0:
        print(f"  Ladder cycles   : {ladder_cycles}")

    # Risk telemetry (only present if risk_stats supplied)
    if risk_stats:
        print(f"  Max floating DD : ${s.get('max_floating_dd', 0.0):+.2f}")
        print(f"  Peak rungs      : {s.get('max_rungs', 0)}")
        print(f"  Peak exposure   : {s.get('max_exposure_lots', 0.0):g} lot")

    # Carried-over positions — open across the boundary
    if carryover_trades and current_price is not None:
        unreal = sum(trade_pnl_usd(t, current_price, contract_size)
                     for t in carryover_trades)
        ids = ", ".join(f"#{t['trade_id']}" for t in carryover_trades)
        print(f"  Carry-over open : {len(carryover_trades)} trade(s)  "
              f"{ids}  unrealized ${unreal:+.2f}")
    print("=" * 64)


def backfill_paper_trades(pivots: list, bars, contract_size: float):
    """Replay historical M1 bars through the trade engine to synthesise trades
    that would have fired between 02:00 and 'now'. Called once when the tracker
    boots mid-day and finds existing pivots.

    Model — bar-close tick: each M1 bar is treated as one 'tick' at its close
    price. This is identical to how the live loop treats each 1-second poll,
    just at 60x coarser granularity. No high/low same-bar reconstruction
    because that introduces artifacts (a $0.30 trail vs $0.60/min bar range
    would stop every trade out in its entry bar from intra-bar noise alone).
    Live behaviour is preserved; only the sampling rate differs.

    Returns (closed_trades, open_trades, anchors_used, next_trade_id, total_pnl).
    All synthesised trades are tagged `backfill=True`.
    """
    open_trades = []
    closed = []
    anchors_used = set()
    trade_id = 1
    total_pnl = 0.0

    pivots_sorted = sorted(pivots, key=lambda p: p["server_time"])
    pivot_cursor = 0
    latest_pivot = None

    for bar in bars:
        bar_time = broker_dt(int(bar["time"]))
        price = float(bar["close"])
        bar_ist = bar_time.replace(tzinfo=timezone.utc).astimezone(IST)

        # 1. Promote pivots whose server_time has elapsed by this bar's close
        while (pivot_cursor < len(pivots_sorted)
               and pivots_sorted[pivot_cursor]["server_time"] <= bar_time):
            latest_pivot = pivots_sorted[pivot_cursor]
            pivot_cursor += 1

        if latest_pivot is None:
            continue

        # 2. Entry check from latest pivot (same call the live loop uses)
        new_trade, trade_id = check_paper_entry(
            latest_pivot, price, anchors_used, trade_id, bar_time, bar_ist,
        )
        if new_trade is not None:
            new_trade["backfill"] = True
            open_trades.append(new_trade)

        # 3. Update trails on every open trade
        for t in open_trades:
            update_trail(t, price)

        # 4. Trail-stop exits
        survivors = []
        for t in open_trades:
            if trail_hit(t, price):
                close_paper_trade(t, t["trail_stop"], "TRAIL_HIT",
                                  bar_time, bar_ist, contract_size)
                closed.append(t)
                total_pnl += t["pnl_usd"]
            else:
                survivors.append(t)
        open_trades = survivors

        # 5. Master close — opposite move from latest pivot
        if open_trades and check_master_close(latest_pivot, price):
            for t in list(open_trades):
                close_paper_trade(t, price, "MASTER_CLOSE",
                                  bar_time, bar_ist, contract_size)
                closed.append(t)
                total_pnl += t["pnl_usd"]
            open_trades = []

    return closed, open_trades, anchors_used, trade_id, total_pnl


def backfill_ladder(bars, initial_anchor: float, contract_size: float):
    """Replay historical M1 bars through the LADDER engine to synthesise the
    ladder cycles that would have fired between 02:00 and 'now'. Uses bar-close
    as the tick price, matching the live polling model and avoiding the
    intra-bar noise that would falsely trigger master closes.

    Returns a state dict with closed trades, open trades, anchor/direction/
    latest-entry to roll into the live engine, and cycle/PnL totals.

    All synthesised trades are tagged `backfill=True`."""
    open_trades = []
    closed_trades = []
    trade_id = 1
    direction = None
    latest_entry = None
    anchor = initial_anchor
    realized_pnl = 0.0
    cycles = 0

    for bar in bars:
        server_time = broker_dt(int(bar["time"]))
        # Convert broker UTC to IST for the trade row's display-side timestamp
        bar_ist = (server_time.replace(tzinfo=timezone.utc).astimezone(IST))
        price = float(bar["close"])

        # === Master close (resolves before entries — same as live) ===
        if (direction is not None
                and latest_entry is not None
                and check_ladder_master_close(direction, latest_entry, price)):
            # Realistic fill: close at the bar-close price that breached the
            # threshold, NOT the theoretical latest_entry ∓ buffer. On a calm
            # bar the two are within cents; on a gap bar the theoretical price
            # would massively understate the loss. The new anchor is the same
            # actual price, so the next cycle starts from where this one really
            # ended (matters when a gap closes the cycle far past the buffer).
            theoretical_close = ladder_master_close_price(direction, latest_entry)
            close_price = price
            for t in list(open_trades):
                close_paper_trade(t, close_price, "MASTER_CLOSE",
                                  server_time, bar_ist, contract_size)
                closed_trades.append(t)
                realized_pnl += t["pnl_usd"]
            open_trades = []
            anchor = close_price
            direction = None
            latest_entry = None
            cycles += 1

        # === Entries (loop in case price gap fired multiple rungs in one bar) ===
        # Capped: a single gap bar can't pyramid the ladder past MAX_LADDER_RUNGS
        # / MAX_EXPOSURE_LOTS, and no more than MAX_RUNGS_PER_TICK rungs fire per
        # bar (mirrors the live engine's per-poll cap so backfill ≈ live).
        rungs_fired_this_bar = 0
        fired = True
        while fired:
            fired = False
            if len(open_trades) >= MAX_LADDER_RUNGS:
                break
            if len(open_trades) * LOT_SIZE >= MAX_EXPOSURE_LOTS:
                break
            if rungs_fired_this_bar >= MAX_RUNGS_PER_TICK:
                break
            if direction is None:
                d, trigger = bidirectional_trigger(anchor, price)
                if d is not None:
                    nt = make_ladder_trade(trade_id, d, trigger, anchor,
                                           server_time, bar_ist, LOT_SIZE, 1)
                    nt["backfill"] = True
                    open_trades.append(nt)
                    direction = d
                    latest_entry = trigger
                    trade_id += 1
                    rungs_fired_this_bar += 1
                    fired = True
            else:
                nxt = ladder_next_entry(direction, latest_entry)
                should_fire = (
                    (direction == "LONG" and price >= nxt)
                    or
                    (direction == "SHORT" and price <= nxt)
                )
                if should_fire:
                    rung = len(open_trades) + 1
                    nt = make_ladder_trade(trade_id, direction, nxt, anchor,
                                           server_time, bar_ist, LOT_SIZE, rung)
                    nt["backfill"] = True
                    open_trades.append(nt)
                    cascade_locks(open_trades, nxt)
                    latest_entry = nxt
                    trade_id += 1
                    rungs_fired_this_bar += 1
                    fired = True

    return {
        "open_trades": open_trades,
        "closed_trades": closed_trades,
        "realized_pnl": round(realized_pnl, 2),
        "trade_id": trade_id,
        "cycles": cycles,
        "ladder_anchor": anchor,
        "ladder_direction": direction,
        "ladder_latest_entry": latest_entry,
    }


def append_trade_log(path: str, trade: dict):
    """Append one closed trade as a row. Header is written if file is new."""
    is_new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        if is_new:
            writer.writeheader()
        row = {k: trade.get(k, "") for k in TRADE_FIELDS}
        # Format datetimes for CSV friendliness
        for k in ("entry_server_time", "entry_ist_time",
                  "exit_server_time", "exit_ist_time", "anchor_server_time"):
            if isinstance(row.get(k), datetime):
                row[k] = row[k].isoformat(sep=" ")
        writer.writerow(row)


def ensure_trade_log_header(path: str):
    """Create the trade log with just the header row if it doesn't exist yet.
    This makes the file visible at startup — important so the operator can
    confirm output paths are correct without having to wait for the first
    trade to close. Subsequent append_trade_log() calls add rows as trades fire."""
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
            writer.writeheader()


# === Event log (JSONL) ===
# Every meaningful state change appends one JSON object per line. Use this for
# audit, debugging, and reconciliation against any parallel bot. Append-only,
# never rewritten, so concurrent writes from two processes (different SYMBOL
# prefixes) won't corrupt each other.

def log_event(event_type: str, data: dict,
              server_now: datetime, ist_now: datetime):
    """Append one event to EVENT_LOG_PATH. Soft-fail on write errors so a disk
    hiccup doesn't kill the tracker."""
    event = {
        "ts_server": server_now.isoformat(sep=" "),
        "ts_ist": ist_now.isoformat(sep=" "),
        "event": event_type,
        "data": data,
    }
    try:
        with open(EVENT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except Exception as e:
        print(f"[event log write failed: {e}]")


def load_recent_events(path: str, n: int = 100) -> list:
    """Return the last `n` parsed events from the JSONL file, oldest first."""
    if not os.path.exists(path):
        return []
    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events[-n:]


def verify_continuity(events: list, today_iso: str) -> str:
    """Look at the event log and report whether the previous day was properly
    finalized. Returned string is printed in the startup banner."""
    if not events:
        return "first run — no prior event history"
    finalized = [e for e in events if e["event"] == "day_finalized"]
    if not finalized:
        return f"no day_finalized events yet ({len(events)} prior events)"
    last = finalized[-1]
    last_date = last["data"].get("prev_date", "?")
    last_pnl = last["data"].get("realized_pnl", 0)
    trades = last["data"].get("trades_closed", 0)
    return (f"last finalized: {last_date}  "
            f"({trades} trade(s), realized ${last_pnl:+.2f})")


def migrate_legacy_paths():
    """Rename legacy (non-symbol-prefixed) files to the new prefixed paths so
    prior runs aren't orphaned. Idempotent — does nothing if nothing to move."""
    for old, new in LEGACY_PATHS.items():
        if os.path.exists(old) and not os.path.exists(new):
            os.rename(old, new)
            print(f"Migrated legacy file: {old} → {new}")


def load_log(path: str) -> dict:
    """Read existing daily records into a dict keyed by date string.
    Returns empty dict if the file doesn't exist yet."""
    if not os.path.exists(path):
        return {}
    records = {}
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            records[row["date"]] = row
    return records


def save_log(path: str, records: dict):
    """Atomically write all records to CSV (write to .tmp, then rename)."""
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        writer.writeheader()
        for date in sorted(records.keys()):
            writer.writerow(records[date])
    os.replace(tmp, path)


def build_record(data: dict, last_price: float) -> dict:
    """Flatten the live data into a CSV-friendly row for the day."""
    anchor = data["anchor"]
    high = data["high"]
    low = data["low"]
    return {
        "date": anchor["server_time"].date().isoformat(),
        "anchor_price": anchor["price"],
        "anchor_server_time": anchor["server_time"].isoformat(sep=" "),
        "high_price": high["price"],
        "high_server_time": high["server_time"].isoformat(sep=" "),
        "high_dollars": round(high["price"] - anchor["price"], 2),
        "high_pips": high["pips_moved"],
        "low_price": low["price"],
        "low_server_time": low["server_time"].isoformat(sep=" "),
        "low_dollars": round(low["price"] - anchor["price"], 2),
        "low_pips": low["pips_moved"],
        "last_price": last_price,
        "last_price_dollars": round(last_price - anchor["price"], 2),
        "candles_read": data["candles_read"],
        "last_update_server": data["checked_server_time"].isoformat(sep=" "),
        "last_update_ist": data["checked_ist_time"].isoformat(sep=" "),
    }


def print_heartbeat(pivot: dict, candidate: dict | None, threshold: float,
                    pip_size: float, last_price: float,
                    day_high: float, day_low: float,
                    open_trades: list, closed_pnl: float, contract_size: float,
                    ladder_anchor: float | None = None,
                    ladder_direction: str | None = None,
                    ladder_latest_entry: float | None = None,
                    ladder_open: list | None = None,
                    ladder_closed_pnl: float = 0.0,
                    ladder_cycles: int = 0,
                    max_floating_dd: float = 0.0):
    """One-line live state. Shows last pivot + running candidate + flip price
    (observation), plus ladder state (active strategy) or legacy trade block."""
    ist = get_ist_now()
    pivot_letter = pivot["type"][0]

    if candidate is not None:
        cand_letter = candidate["type"][0]
        swing = candidate["price"] - pivot["price"]
        if candidate["type"] == "HIGH":
            flip_at = candidate["price"] - threshold
            flip_label = f"flip≤{flip_at:.2f}"
        else:
            flip_at = candidate["price"] + threshold
            flip_label = f"flip≥{flip_at:.2f}"
        pivot_block = (
            f"pvt {pivot_letter}{pivot['price']:.2f} → {cand_letter}{candidate['price']:.2f} "
            f"swg{swing:+.2f}"
        )
    else:
        pivot_block = f"pvt {pivot_letter}{pivot['price']:.2f}"

    if STRATEGY_MODE == "ladder":
        if ladder_anchor is None:
            ladder_block = "LDR no anchor"
        elif ladder_direction is None:
            up_trig = ladder_anchor + LADDER_STEP_USD
            dn_trig = ladder_anchor - LADDER_STEP_USD
            ladder_block = (
                f"LDR idle anch ${ladder_anchor:.2f} "
                f"L≥${up_trig:.2f} S≤${dn_trig:.2f}"
            )
        else:
            n = len(ladder_open or [])
            next_rung = ladder_next_entry(ladder_direction, ladder_latest_entry)
            close_at = ladder_master_close_price(ladder_direction, ladder_latest_entry)
            unreal = sum(trade_pnl_usd(t, last_price, contract_size) for t in (ladder_open or []))
            total_lot = n * LOT_SIZE
            ladder_block = (
                f"LDR {ladder_direction}#{n} ({total_lot:g}lot) anc ${ladder_anchor:.2f} "
                f"lst ${ladder_latest_entry:.2f} next ${next_rung:.2f} "
                f"cls ${close_at:.2f} ur${unreal:+.2f} dd${max_floating_dd:+.2f}"
            )
        if ladder_cycles > 0:
            ladder_block += f" cyc{ladder_cycles}"
        trade_block = f"{ladder_block} | tot ${ladder_closed_pnl:+.2f}"
    else:
        unreal = unrealized_pnl(open_trades, last_price, contract_size)
        trade_block = (
            f"open {len(open_trades)} ${unreal:+.2f}  closed ${closed_pnl:+.2f}"
        )

    from_day_high = last_price - day_high
    from_day_low = last_price - day_low
    day_block = (
        f"day H{day_high:.2f}Δ{from_day_high:+.2f} L{day_low:.2f}Δ{from_day_low:+.2f}"
    )

    line = (
        f"\r[{ist:%H:%M:%S}] "
        f"${last_price:.2f}  "
        f"{pivot_block} | "
        f"{trade_block} | "
        f"{day_block}"
    )
    sys.stdout.write(line.ljust(HEARTBEAT_WIDTH))
    sys.stdout.flush()


def print_report(data):
    print("\n========== 2 AM Anchor High/Low ==========")
    print(f"Symbol           : {data['symbol']}")
    print(f"Pip Size         : {data['pip_size']}")
    print("------------------------------------------")

    print(f"Anchor Price     : {data['anchor']['price']}")
    print(f"Anchor Server    : {data['anchor']['server_time']}")
    print(f"Anchor IST Log   : {data['anchor']['detected_ist_time']}")

    print("------------------------------------------")
    print(f"High Price       : {data['high']['price']}")
    print(f"High Server      : {data['high']['server_time']}")
    print(f"High IST Detected: {data['high']['detected_ist_time']}")
    print(f"High Pips Moved  : {data['high']['pips_moved']}")

    print("------------------------------------------")
    print(f"Low Price        : {data['low']['price']}")
    print(f"Low Server       : {data['low']['server_time']}")
    print(f"Low IST Detected : {data['low']['detected_ist_time']}")
    print(f"Low Pips Moved   : {data['low']['pips_moved']}")

    print("------------------------------------------")
    print(f"Candles Read     : {data['candles_read']}")
    print(f"Checked Server   : {data['checked_server_time']}")
    print(f"Checked IST      : {data['checked_ist_time']}")


def run():
    connect_mt5()

    migrate_legacy_paths()
    ensure_trade_log_header(TRADE_LOG_PATH)

    pip_size = get_pip_size(SYMBOL)
    info = mt5.symbol_info(SYMBOL)
    contract_size = float(info.trade_contract_size) if info else 100.0
    daily_records = load_log(LOG_PATH)
    recent_events = load_recent_events(EVENT_LOG_PATH)
    today_iso = datetime.utcnow().date().isoformat()
    continuity = verify_continuity(recent_events, today_iso)

    active_anchor = None
    anchor_data = None
    pivots_count = 0
    last_high = None
    last_low = None
    did_backfill = False  # set True after the one-shot historical backfill

    # Paper-trade state (pivot mode — legacy)
    open_trades = []
    closed_trades_today = []   # closed during this day, for the day_finalized summary
    anchors_used = set()       # anchor_keys already fired
    next_trade_id = 1
    closed_pnl_total = 0.0     # realized P&L for the session

    # Ladder state (active mode)
    ladder_anchor = None           # current bidirectional reference price
    ladder_direction = None        # None, "LONG", or "SHORT"
    ladder_latest_entry = None     # price of most recently filed rung
    ladder_open = []               # open ladder trades
    ladder_trade_id = 1
    ladder_closed_today = []       # ladder trades closed today
    ladder_closed_pnl = 0.0        # realized P&L from ladder
    ladder_cycles = 0              # how many master-close → flip events
    ladder_backfilled = False      # one-shot historical replay completed
    current_cycle_id = None        # set at first entry of each cycle; same for all rungs in that cycle

    # Risk telemetry (resets daily at 2am along with other daily counters)
    max_floating_dd = 0.0          # most-negative unrealized P&L seen today
    max_exposure_lots = 0.0        # peak total lots held at any tick
    max_rungs_today = 0            # deepest the ladder reached today

    print(f"Started tracker for {SYMBOL}")
    print(f"Daily anchor: {ANCHOR_HOUR:02d}:{ANCHOR_MINUTE:02d} broker time")
    print(f"Timeframe   : M1 (1-minute timestamp resolution)")
    print(f"Pivot mode  : ZigZag — flips only on ${REVERSAL_THRESHOLD_USD:.2f} reversal (observation only in ladder mode)")
    print(f"Strategy    : {STRATEGY_MODE.upper()}")
    if STRATEGY_MODE == "ladder":
        print(f"Ladder      : step ${LADDER_STEP_USD:.2f}  master-close buffer ${MASTER_CLOSE_BUFFER_USD:.2f}  "
              f"lot {LOT_SIZE} (contract {contract_size:g} → ${LOT_SIZE * contract_size:.0f}/$1)")
    else:
        print(f"Paper trade : trigger ${TRADE_TRIGGER_USD:.2f} / trail ${TRAIL_STOP_USD:.2f} "
              f"/ master close ${MASTER_CLOSE_USD:.2f} / lot {LOT_SIZE} "
              f"(contract {contract_size:g} → ${LOT_SIZE * contract_size:.0f}/$1 move)")
    print(f"Files       : {LOG_PATH}, {TRADE_LOG_PATH}, {EVENT_LOG_PATH}")
    print(f"Daily log   : {len(daily_records)} record(s) loaded")
    print(f"Continuity  : {continuity}")

    log_event("startup", {
        "symbol": SYMBOL,
        "contract_size": contract_size,
        "config": {
            "reversal_threshold_usd": REVERSAL_THRESHOLD_USD,
            "trade_trigger_usd": TRADE_TRIGGER_USD,
            "trail_stop_usd": TRAIL_STOP_USD,
            "master_close_usd": MASTER_CLOSE_USD,
            "lot_size": LOT_SIZE,
        },
        "daily_records_loaded": len(daily_records),
        "continuity_note": continuity,
    }, datetime.utcnow(), get_ist_now())

    while True:
        try:
            server_now = get_server_now(SYMBOL)
            anchor_time = get_current_anchor_time(server_now)

            # New day → cache the daily anchor and reset the running anchor to it.
            if active_anchor != anchor_time:
                new_data = fetch_anchor(SYMBOL, anchor_time)

                if active_anchor is not None:
                    prev_date = active_anchor.date().isoformat()

                    # Print daily summary (separate for each strategy mode)
                    all_closed = (closed_trades_today
                                  if STRATEGY_MODE == "pivot"
                                  else ladder_closed_today)
                    carryover = (open_trades
                                 if STRATEGY_MODE == "pivot"
                                 else ladder_open)
                    # Best estimate of price at the rollover instant
                    rollover_price = anchor_data["price"] if anchor_data else 0
                    try:
                        tick = mt5.symbol_info_tick(SYMBOL)
                        if tick is not None:
                            rollover_price = tick.bid
                    except Exception:
                        pass
                    risk_stats_dict = {
                        "max_floating_dd": max_floating_dd,
                        "max_rungs": max_rungs_today,
                        "max_exposure_lots": max_exposure_lots,
                    }
                    print_daily_summary(prev_date, all_closed, ladder_cycles,
                                        carryover_trades=carryover,
                                        current_price=rollover_price,
                                        contract_size=contract_size,
                                        risk_stats=risk_stats_dict)

                    summary_dict = daily_summary(all_closed, risk_stats=risk_stats_dict)

                    if prev_date in daily_records:
                        r = daily_records[prev_date]
                        print(f">>> Finalized {prev_date}: "
                              f"anchor={r['anchor_price']} "
                              f"high=+${r['high_dollars']} "
                              f"low=${r['low_dollars']}")
                        # Comprehensive day_finalized event for audit
                        log_event("day_finalized", {
                            "prev_date": prev_date,
                            "anchor_price": float(r["anchor_price"]),
                            "day_high_price": float(r["high_price"]),
                            "day_low_price": float(r["low_price"]),
                            "high_dollars_from_anchor": float(r["high_dollars"]),
                            "low_dollars_from_anchor": float(r["low_dollars"]),
                            "high_server_time": r["high_server_time"],
                            "low_server_time": r["low_server_time"],
                            "candles_read": int(r.get("candles_read") or 0),
                            "pivots_count": pivots_count,
                            "trades_closed": len(all_closed),
                            "trades_left_open": (len(open_trades)
                                                 if STRATEGY_MODE == "pivot"
                                                 else len(ladder_open)),
                            "realized_pnl": (round(closed_pnl_total, 2)
                                             if STRATEGY_MODE == "pivot"
                                             else round(ladder_closed_pnl, 2)),
                            "last_price_recorded": float(r.get("last_price") or 0),
                            "strategy_mode": STRATEGY_MODE,
                            "ladder_cycles": ladder_cycles,
                            "summary": summary_dict,
                        }, get_server_now(SYMBOL), get_ist_now())

                active_anchor = anchor_time
                anchor_data = new_data
                pivots_count = 1
                last_high = None
                last_low = None
                did_backfill = False

                # === 24/7 BOUNDARY HANDLING ===
                # The bot runs continuously across the 2am rollover. Open trades
                # and live ladder state CARRY OVER — only the per-day reporting
                # counters reset. Force-closing at 2am would orphan real risk;
                # the ladder closes itself naturally via master close.

                # Pivot mode: reset daily counters; open_trades / anchors_used persist
                closed_trades_today = []
                closed_pnl_total = 0.0
                # open_trades — CARRY OVER (do not clear)
                # anchors_used — CARRY OVER (old pivots stay marked-as-used)
                # next_trade_id — CARRY OVER (don't recycle IDs)

                # Ladder mode: reset daily counters; ladder state persists
                ladder_closed_today = []
                ladder_closed_pnl = 0.0
                ladder_cycles = 0
                # ladder_open, ladder_direction, ladder_latest_entry,
                # ladder_trade_id — ALL CARRY OVER

                # Risk telemetry resets daily (max-DD-per-day is the audit metric)
                max_floating_dd = 0.0
                max_exposure_lots = 0.0
                max_rungs_today = 0

                # Re-arm ladder backfill for the new day (rare: only relevant
                # if the tracker was restarted across midnight, which the
                # ladder_backfilled flag would normally prevent)
                ladder_backfilled = True  # do not re-backfill within a session

                # Only reset the bidirectional anchor if NO ladder is active.
                # If a ladder is mid-flight, its anchor stays put until the
                # ladder closes naturally; this morning's 2am open is just a
                # daily marker in that case.
                if ladder_direction is None and not ladder_open:
                    ladder_anchor = anchor_data["price"]
                    anchor_note = f"ladder anchor reset to 2am open ${ladder_anchor:.2f}"
                else:
                    anchor_note = (
                        f"ladder CARRIES OVER  {ladder_direction} #{len(ladder_open)} "
                        f"anchor ${ladder_anchor:.2f}  latest ${ladder_latest_entry:.2f}"
                    )
                print(f"    [{anchor_note}]")
                log_event("day_boundary", {
                    "new_date": active_anchor.date().isoformat(),
                    "ladder_carryover": ladder_direction is not None or bool(ladder_open),
                    "ladder_direction": ladder_direction,
                    "ladder_anchor": ladder_anchor,
                    "ladder_latest_entry": ladder_latest_entry,
                    "ladder_open_count": len(ladder_open),
                    "ladder_open_trade_ids": [t["trade_id"] for t in ladder_open],
                    "pivot_open_count": len(open_trades),
                    "pivot_open_trade_ids": [t["trade_id"] for t in open_trades],
                    "anchor_note": anchor_note,
                }, get_server_now(SYMBOL), get_ist_now())

                print("\n========== New Daily Anchor ==========")
                print(f"Anchor Server Time : {active_anchor}")
                print(f"Anchor Price       : {anchor_data['price']}")
                print(f"Anchor Type        : OPEN")
                print(f"Detected IST Time  : {get_ist_now()}")

                log_event("daily_anchor_set", {
                    "date": active_anchor.date().isoformat(),
                    "anchor_server_time": active_anchor.isoformat(sep=" "),
                    "anchor_price": anchor_data["price"],
                    "anchor_type": "OPEN",
                }, active_anchor, get_ist_now())

            # Single MT5 fetch per poll; reused for pivots AND daily stats
            rates = mt5.copy_rates_range(
                SYMBOL, TIMEFRAME,
                to_mt5_dt(active_anchor), to_mt5_dt(server_now),
            )

            # ZigZag pivot detection — anchor only flips on confirmed reversals
            open_pivot = {
                "price": anchor_data["price"],
                "server_time": anchor_data["server_time"],
                "type": "OPEN",
            }
            pivots, candidate = detect_pivots(
                rates, open_pivot, REVERSAL_THRESHOLD_USD,
            )

            # Announce any pivots that confirmed since the last poll
            if len(pivots) > pivots_count:
                for p in pivots[pivots_count:]:
                    prev = pivots[pivots.index(p) - 1] if pivots.index(p) > 0 else None
                    if last_high is not None:
                        print()
                    line = (
                        f">>> PIVOT {p['type']:<4} CONFIRMED  "
                        f"${p['price']:.2f} @ {p['server_time']}"
                    )
                    swing = None
                    if prev is not None:
                        swing = p["price"] - prev["price"]
                        line += (
                            f"  (swing from {prev['type']} ${prev['price']:.2f}@"
                            f"{prev['server_time']:%H:%M}: {swing:+.2f})"
                        )
                    line += f"  [IST {get_ist_now():%H:%M:%S}]"
                    print(line)

                    log_event("pivot_confirmed", {
                        "type": p["type"],
                        "price": p["price"],
                        "server_time": p["server_time"].isoformat(sep=" "),
                        "prev_type": prev["type"] if prev else None,
                        "prev_price": prev["price"] if prev else None,
                        "prev_server_time": (prev["server_time"].isoformat(sep=" ")
                                             if prev else None),
                        "swing_from_prev": round(swing, 4) if swing is not None else None,
                        "pivot_index": pivots.index(p),
                    }, server_now, get_ist_now())
                    last_high = None
                pivots_count = len(pivots)

            # === One-shot historical backfill ===
            # Runs once per session start when the tracker boots mid-day and
            # finds existing pivots that elapsed before we were watching.
            # Synthesises the trades that WOULD have fired between 02:00 and
            # now so the audit log isn't empty just because we started late.
            if (STRATEGY_MODE == "pivot"
                    and BACKFILL_TRADES_ON_START
                    and not did_backfill
                    and len(pivots) > 1
                    and rates is not None):
                bf_closed, bf_open, bf_used, bf_next_id, bf_pnl = (
                    backfill_paper_trades(pivots, rates, contract_size)
                )

                print(f"\n>>> BACKFILL: replayed {len(rates)} bars across "
                      f"{len(pivots)} pivots → {len(bf_closed)} closed trades, "
                      f"{len(bf_open)} still open, realized ${bf_pnl:+.2f}")

                # Persist each synthesised trade to CSV + events log
                for t in bf_closed:
                    append_trade_log(TRADE_LOG_PATH, t)
                    print(
                        f"    backfill #{t['trade_id']} {t['direction']:<5} "
                        f"entry ${t['entry_price']:.2f} @ {t['entry_server_time']} "
                        f"→ exit ${t['exit_price']:.2f} @ {t['exit_server_time']}  "
                        f"reason {t['exit_reason']}  PnL ${t['pnl_usd']:+.2f}"
                    )
                    log_event("trade_entry", {
                        "trade_id": t["trade_id"],
                        "direction": t["direction"],
                        "lot": t["lot"],
                        "entry_price": t["entry_price"],
                        "anchor_type": t["anchor_type"],
                        "anchor_price": t["anchor_price"],
                        "anchor_server_time": t["anchor_server_time"].isoformat(sep=" "),
                        "entry_server_time": t["entry_server_time"].isoformat(sep=" "),
                        "backfill": True,
                    }, t["entry_server_time"], get_ist_now())
                    log_event("trade_exit", {
                        "trade_id": t["trade_id"],
                        "direction": t["direction"],
                        "lot": t["lot"],
                        "entry_price": t["entry_price"],
                        "exit_price": t["exit_price"],
                        "exit_server_time": t["exit_server_time"].isoformat(sep=" "),
                        "reason": t["exit_reason"],
                        "price_distance": t["price_distance"],
                        "pnl_usd": t["pnl_usd"],
                        "high_water": t["high_water"],
                        "low_water": t["low_water"],
                        "anchor_type": t["anchor_type"],
                        "anchor_price": t["anchor_price"],
                        "backfill": True,
                    }, t["exit_server_time"], get_ist_now())

                # For any still-open backfilled trades, log entry only
                for t in bf_open:
                    print(
                        f"    backfill #{t['trade_id']} {t['direction']:<5} "
                        f"entry ${t['entry_price']:.2f} @ {t['entry_server_time']}  "
                        f"STILL OPEN  trail ${t['trail_stop']:.2f}"
                    )
                    log_event("trade_entry", {
                        "trade_id": t["trade_id"],
                        "direction": t["direction"],
                        "lot": t["lot"],
                        "entry_price": t["entry_price"],
                        "anchor_type": t["anchor_type"],
                        "anchor_price": t["anchor_price"],
                        "anchor_server_time": t["anchor_server_time"].isoformat(sep=" "),
                        "entry_server_time": t["entry_server_time"].isoformat(sep=" "),
                        "trail_stop": t["trail_stop"],
                        "backfill": True,
                    }, t["entry_server_time"], get_ist_now())

                # Roll the synthesised state into the live engine
                open_trades = bf_open
                closed_trades_today = list(bf_closed)
                anchors_used = bf_used
                next_trade_id = bf_next_id
                closed_pnl_total = bf_pnl

                log_event("backfill_complete", {
                    "bars_replayed": len(rates),
                    "pivots_total": len(pivots),
                    "trades_closed": len(bf_closed),
                    "trades_still_open": len(bf_open),
                    "realized_pnl": round(bf_pnl, 2),
                    "anchors_consumed": len(bf_used),
                }, server_now, get_ist_now())

                did_backfill = True
                last_high = None

            # === Ladder backfill (one-shot historical replay for ladder mode) ===
            if (STRATEGY_MODE == "ladder"
                    and BACKFILL_TRADES_ON_START
                    and not ladder_backfilled
                    and rates is not None
                    and len(rates) > 0):
                seed_anchor = ladder_anchor or anchor_data["price"]
                bf = backfill_ladder(rates, seed_anchor, contract_size)

                # Roll synthesised state into the live engine
                ladder_open = bf["open_trades"]
                ladder_closed_today = list(bf["closed_trades"])
                ladder_closed_pnl = bf["realized_pnl"]
                ladder_trade_id = bf["trade_id"]
                ladder_cycles = bf["cycles"]
                ladder_anchor = bf["ladder_anchor"]
                ladder_direction = bf["ladder_direction"]
                ladder_latest_entry = bf["ladder_latest_entry"]

                if last_high is not None:
                    print()
                print(
                    f">>> LADDER BACKFILL  replayed {len(rates)} bars  "
                    f"→ {len(ladder_closed_today)} closed trades  "
                    f"{len(ladder_open)} still open  "
                    f"{ladder_cycles} cycle(s)  realized ${ladder_closed_pnl:+.2f}"
                )

                # Persist each synthesised closed trade to CSV + events
                for t in ladder_closed_today:
                    append_trade_log(TRADE_LOG_PATH, t)
                    print(
                        f"    backfill #{t['trade_id']} {t['direction']:<5} rung "
                        f"{t.get('ladder_position', '?')} entry ${t['entry_price']:.2f} "
                        f"@ {t['entry_server_time']} → exit ${t['exit_price']:.2f} "
                        f"@ {t['exit_server_time']}  PnL ${t['pnl_usd']:+.2f}"
                    )
                    log_event("trade_entry", {
                        "trade_id": t["trade_id"],
                        "direction": t["direction"],
                        "lot": t["lot"],
                        "entry_price": t["entry_price"],
                        "ladder_anchor": t["anchor_price"],
                        "ladder_position": t.get("ladder_position"),
                        "entry_server_time": t["entry_server_time"].isoformat(sep=" "),
                        "backfill": True,
                        "strategy": "ladder",
                    }, t["entry_server_time"], get_ist_now())
                    log_event("trade_exit", {
                        "trade_id": t["trade_id"],
                        "direction": t["direction"],
                        "lot": t["lot"],
                        "entry_price": t["entry_price"],
                        "exit_price": t["exit_price"],
                        "exit_server_time": t["exit_server_time"].isoformat(sep=" "),
                        "reason": t["exit_reason"],
                        "price_distance": t["price_distance"],
                        "pips_moved": t.get("pips_moved", 0),
                        "pnl_usd": t["pnl_usd"],
                        "ladder_position": t.get("ladder_position"),
                        "backfill": True,
                    }, t["exit_server_time"], get_ist_now())

                # Open carry-over from backfill: log entry only, will close live
                for t in ladder_open:
                    print(
                        f"    backfill #{t['trade_id']} {t['direction']:<5} rung "
                        f"{t.get('ladder_position', '?')} entry ${t['entry_price']:.2f}  "
                        f"STILL OPEN  lock ${t['lock_price']:.2f}"
                    )
                    log_event("trade_entry", {
                        "trade_id": t["trade_id"],
                        "direction": t["direction"],
                        "lot": t["lot"],
                        "entry_price": t["entry_price"],
                        "ladder_anchor": t["anchor_price"],
                        "ladder_position": t.get("ladder_position"),
                        "entry_server_time": t["entry_server_time"].isoformat(sep=" "),
                        "lock_price": t["lock_price"],
                        "backfill": True,
                        "strategy": "ladder",
                    }, t["entry_server_time"], get_ist_now())

                log_event("ladder_backfill_complete", {
                    "bars_replayed": len(rates),
                    "trades_closed": len(ladder_closed_today),
                    "trades_still_open": len(ladder_open),
                    "cycles": ladder_cycles,
                    "realized_pnl": ladder_closed_pnl,
                    "final_anchor": ladder_anchor,
                    "final_direction": ladder_direction,
                    "final_latest_entry": ladder_latest_entry,
                }, server_now, get_ist_now())

                ladder_backfilled = True
                last_high = None
            # Daily stats (since 02:00 anchor) — for full report + daily log
            data = compute_high_low(
                SYMBOL, active_anchor, anchor_data, server_now, pip_size,
                rates=rates,
            )

            if data:
                tick = mt5.symbol_info_tick(SYMBOL)
                last_price = tick.bid if tick is not None else data["high"]["price"]
                ist_now = get_ist_now()

                # === Paper-trade engine (pivot mode, legacy) ===
                if STRATEGY_MODE == "pivot":
                    latest_pivot = pivots[-1]

                    # 1. Check for a new entry from the latest pivot
                    new_trade, next_trade_id = check_paper_entry(
                        latest_pivot, last_price, anchors_used, next_trade_id,
                        server_now, ist_now,
                    )
                    if new_trade is not None:
                        open_trades.append(new_trade)
                        if last_high is not None:
                            print()
                        print(
                            f">>> ENTRY  #{new_trade['trade_id']} {new_trade['direction']:<5}  "
                            f"${new_trade['entry_price']:.2f}  "
                            f"(from {latest_pivot['type']} ${latest_pivot['price']:.2f}, "
                            f"+${TRADE_TRIGGER_USD:.2f}), trail ${new_trade['trail_stop']:.2f}, "
                            f"lot {LOT_SIZE}  [IST {ist_now:%H:%M:%S}]"
                        )
                        log_event("trade_entry", {
                            "trade_id": new_trade["trade_id"],
                            "direction": new_trade["direction"],
                            "lot": new_trade["lot"],
                            "entry_price": new_trade["entry_price"],
                            "anchor_type": latest_pivot["type"],
                            "anchor_price": latest_pivot["price"],
                            "anchor_server_time": latest_pivot["server_time"].isoformat(sep=" "),
                            "initial_trail_stop": new_trade["trail_stop"],
                            "bid_at_trigger": last_price,
                        }, server_now, ist_now)
                        last_high = None

                    # 2. Advance trails for every open trade
                    for t in open_trades:
                        update_trail(t, last_price)

                    # 3. Check for trail-stop hits → close those trades
                    still_open = []
                    for t in open_trades:
                        if trail_hit(t, last_price):
                            close_paper_trade(t, t["trail_stop"], "TRAIL_HIT",
                                              server_now, ist_now, contract_size)
                            closed_pnl_total += t["pnl_usd"]
                            closed_trades_today.append(t)
                            append_trade_log(TRADE_LOG_PATH, t)
                            if last_high is not None:
                                print()
                            print(
                                f">>> EXIT   #{t['trade_id']} {t['direction']:<5}  "
                                f"${t['exit_price']:.2f}  reason TRAIL  "
                                f"price_move ${t['price_distance']:+.2f}  "
                                f"PnL ${t['pnl_usd']:+.2f}  "
                                f"[IST {ist_now:%H:%M:%S}]"
                            )
                            log_event("trade_exit", {
                                "trade_id": t["trade_id"],
                                "direction": t["direction"],
                                "lot": t["lot"],
                                "entry_price": t["entry_price"],
                                "exit_price": t["exit_price"],
                                "reason": "TRAIL_HIT",
                                "price_distance": t["price_distance"],
                                "pnl_usd": t["pnl_usd"],
                                "high_water": t["high_water"],
                                "low_water": t["low_water"],
                                "anchor_type": t["anchor_type"],
                                "anchor_price": t["anchor_price"],
                            }, server_now, ist_now)
                            last_high = None
                        else:
                            still_open.append(t)
                    open_trades = still_open

                    # 4. Master close — $5 opposite move from latest pivot
                    if open_trades and check_master_close(latest_pivot, last_price):
                        if last_high is not None:
                            print()
                        print(
                            f">>> MASTER CLOSE — ${MASTER_CLOSE_USD:.2f} opposite move from "
                            f"{latest_pivot['type']} ${latest_pivot['price']:.2f}, "
                            f"bid ${last_price:.2f}. Closing {len(open_trades)} trade(s)."
                        )
                        master_close_trade_ids = []
                        master_close_total_pnl = 0.0
                        for t in list(open_trades):
                            close_paper_trade(t, last_price, "MASTER_CLOSE",
                                              server_now, ist_now, contract_size)
                            closed_pnl_total += t["pnl_usd"]
                            closed_trades_today.append(t)
                            master_close_trade_ids.append(t["trade_id"])
                            master_close_total_pnl += t["pnl_usd"]
                            append_trade_log(TRADE_LOG_PATH, t)
                            print(
                                f"    #{t['trade_id']} {t['direction']:<5} closed @ "
                                f"${t['exit_price']:.2f}  PnL ${t['pnl_usd']:+.2f}"
                            )
                        log_event("master_close", {
                            "latest_pivot_type": latest_pivot["type"],
                            "latest_pivot_price": latest_pivot["price"],
                            "bid_at_close": last_price,
                            "threshold_usd": MASTER_CLOSE_USD,
                            "closed_trade_ids": master_close_trade_ids,
                            "total_pnl_usd": round(master_close_total_pnl, 2),
                        }, server_now, ist_now)
                        open_trades = []
                        last_high = None

                # === Ladder engine (active mode) ===
                elif STRATEGY_MODE == "ladder":
                    if ladder_anchor is None:
                        ladder_anchor = anchor_data["price"]

                    # 1. Master close check (fires before new entries — single tick can't
                    #    both flip and re-enter; the flip just sets the new anchor and
                    #    waits for price to break ±$10 from there)
                    if (ladder_direction is not None
                            and ladder_latest_entry is not None
                            and check_ladder_master_close(
                                ladder_direction, ladder_latest_entry, last_price)):
                        # Realistic fill: close every rung at the actual bid that
                        # tripped the threshold (last_price), NOT the theoretical
                        # latest_entry ∓ buffer. In calm 1s polling the two differ
                        # by cents; through a gap the theoretical price would hide
                        # most of the loss (a gap-down past a LONG master close
                        # fills you at the gapped price, not the buffer level).
                        # Logging both makes the slippage auditable. The new
                        # anchor is also the actual fill, so the next cycle starts
                        # from where this one truly ended — important after a gap.
                        theoretical_close = ladder_master_close_price(
                            ladder_direction, ladder_latest_entry)
                        close_price = last_price
                        close_slippage = round(close_price - theoretical_close, 4)

                        if last_high is not None:
                            print()
                        print(
                            f">>> MASTER CLOSE  {ladder_direction} ladder "
                            f"({len(ladder_open)} rung(s))  "
                            f"latest entry ${ladder_latest_entry:.2f}  "
                            f"close @ ${close_price:.2f} "
                            f"(theo ${theoretical_close:.2f}, slip ${close_slippage:+.2f})  "
                            f"[IST {ist_now:%H:%M:%S}]"
                        )

                        closed_ids = []
                        cycle_pnl = 0.0
                        for t in list(ladder_open):
                            close_paper_trade(t, close_price, "MASTER_CLOSE",
                                              server_now, ist_now, contract_size)
                            ladder_closed_pnl += t["pnl_usd"]
                            ladder_closed_today.append(t)
                            cycle_pnl += t["pnl_usd"]
                            closed_ids.append(t["trade_id"])
                            append_trade_log(TRADE_LOG_PATH, t)
                            print(
                                f"    #{t['trade_id']} {t['direction']:<5} rung {t['ladder_position']} "
                                f"entry ${t['entry_price']:.2f} → exit ${t['exit_price']:.2f}  "
                                f"PnL ${t['pnl_usd']:+.2f}"
                            )
                            log_event("trade_exit", {
                                "trade_id": t["trade_id"],
                                "direction": t["direction"],
                                "ladder_position": t["ladder_position"],
                                "entry_price": t["entry_price"],
                                "exit_price": t["exit_price"],
                                "reason": "MASTER_CLOSE",
                                "price_distance": t["price_distance"],
                                "pnl_usd": t["pnl_usd"],
                                "lock_price": t["lock_price"],
                                "ladder_anchor": t["anchor_price"],
                            }, server_now, ist_now)

                        old_anchor = ladder_anchor
                        old_direction = ladder_direction
                        prev_latest = ladder_latest_entry

                        # The flip — the ACTUAL fill price becomes the new
                        # bidirectional anchor (see realism note above).
                        ladder_anchor = close_price
                        ladder_direction = None
                        ladder_latest_entry = None
                        ladder_open = []
                        ladder_cycles += 1
                        current_cycle_id = None  # next entry will mint a fresh cycle_id

                        log_event("master_close", {
                            "strategy": "ladder",
                            "direction_closed": old_direction,
                            "latest_entry": prev_latest,
                            "close_price": close_price,
                            "theoretical_close": theoretical_close,
                            "close_slippage_usd": close_slippage,
                            "rungs_closed": len(closed_ids),
                            "closed_trade_ids": closed_ids,
                            "cycle_pnl_usd": round(cycle_pnl, 2),
                            "old_anchor": old_anchor,
                            "new_anchor": ladder_anchor,
                            "cycle_number": ladder_cycles,
                        }, server_now, ist_now)
                        print(
                            f">>> ANCHOR FLIP  new anchor ${ladder_anchor:.2f}  "
                            f"(cycle #{ladder_cycles}, cycle PnL ${cycle_pnl:+.2f}, "
                            f"day total ${ladder_closed_pnl:+.2f})"
                        )
                        last_high = None

                    # 2. Entry checks — may fire multiple rungs if price has run far.
                    #    Capped three ways so a gap between polls can't pyramid the
                    #    ladder uncontrollably: MAX_RUNGS_PER_TICK throttles the
                    #    build rate per poll, MAX_LADDER_RUNGS / MAX_EXPOSURE_LOTS
                    #    are absolute ceilings. When a cap blocks an entry that
                    #    would otherwise have fired, it's logged once per tick as a
                    #    ladder_cap_hit event (a gap fingerprint worth auditing).
                    rungs_fired_this_tick = 0
                    cap_hit_logged = False
                    fired_this_tick = True
                    while fired_this_tick:
                        fired_this_tick = False

                        # --- Safety caps: stop firing rungs once any limit is hit ---
                        _rungs_now = len(ladder_open)
                        _cap_reason = None
                        if _rungs_now >= MAX_LADDER_RUNGS:
                            _cap_reason = "max_ladder_rungs"
                        elif _rungs_now * LOT_SIZE >= MAX_EXPOSURE_LOTS:
                            _cap_reason = "max_exposure_lots"
                        elif rungs_fired_this_tick >= MAX_RUNGS_PER_TICK:
                            _cap_reason = "max_rungs_per_tick"
                        if _cap_reason is not None:
                            # Only log if price actually WANTS another rung right
                            # now — otherwise the cap isn't really "biting".
                            if ladder_direction is not None and ladder_latest_entry is not None:
                                _nxt = ladder_next_entry(ladder_direction, ladder_latest_entry)
                                _wants_more = (
                                    (ladder_direction == "LONG" and last_price >= _nxt)
                                    or
                                    (ladder_direction == "SHORT" and last_price <= _nxt)
                                )
                            else:
                                _wants_more = False
                            if _wants_more and not cap_hit_logged:
                                if last_high is not None:
                                    print()
                                print(
                                    f">>> LADDER CAP  {_cap_reason} hit "
                                    f"({_rungs_now} rung(s), {_rungs_now * LOT_SIZE:g} lot)  "
                                    f"— further entries blocked this poll  "
                                    f"[IST {ist_now:%H:%M:%S}]"
                                )
                                log_event("ladder_cap_hit", {
                                    "reason": _cap_reason,
                                    "rungs_open": _rungs_now,
                                    "exposure_lots": _rungs_now * LOT_SIZE,
                                    "rungs_fired_this_tick": rungs_fired_this_tick,
                                    "direction": ladder_direction,
                                    "latest_entry": ladder_latest_entry,
                                    "bid": last_price,
                                    "max_ladder_rungs": MAX_LADDER_RUNGS,
                                    "max_exposure_lots": MAX_EXPOSURE_LOTS,
                                    "max_rungs_per_tick": MAX_RUNGS_PER_TICK,
                                }, server_now, ist_now)
                                cap_hit_logged = True
                                last_high = None
                            break

                        if ladder_direction is None:
                            # Bidirectional from anchor
                            direction, trigger = bidirectional_trigger(
                                ladder_anchor, last_price)
                            if direction is not None:
                                # Mint a new cycle_id for this fresh ladder cycle
                                session_date = active_anchor.date().isoformat()
                                current_cycle_id = (
                                    f"{session_date}_C{ladder_cycles + 1:02d}"
                                )

                                nt = make_ladder_trade(
                                    ladder_trade_id, direction, trigger,
                                    ladder_anchor, server_now, ist_now,
                                    LOT_SIZE, 1,
                                )
                                nt["cycle_id"] = current_cycle_id
                                nt["session_date"] = session_date
                                nt["trigger_reason"] = "FIRST_RUNG"
                                ladder_open.append(nt)
                                ladder_direction = direction
                                ladder_latest_entry = trigger
                                ladder_trade_id += 1
                                rungs_fired_this_tick += 1
                                fired_this_tick = True

                                if last_high is not None:
                                    print()
                                print(
                                    f">>> LADDER ENTRY  #{nt['trade_id']} {direction:<5} "
                                    f"rung 1 @ ${trigger:.2f}  lot {LOT_SIZE}  "
                                    f"(anchor ${ladder_anchor:.2f}, ±${LADDER_STEP_USD:.0f})  "
                                    f"lock ${nt['lock_price']:.2f}  "
                                    f"master close @ ${trigger - MASTER_CLOSE_BUFFER_USD if direction == 'LONG' else trigger + MASTER_CLOSE_BUFFER_USD:.2f}  "
                                    f"cycle {current_cycle_id}  "
                                    f"[IST {ist_now:%H:%M:%S}]"
                                )
                                log_event("trade_entry", {
                                    "trade_id": nt["trade_id"],
                                    "cycle_id": current_cycle_id,
                                    "session_date": session_date,
                                    "direction": direction,
                                    "lot": LOT_SIZE,
                                    "entry_price": trigger,
                                    "ladder_anchor": ladder_anchor,
                                    "ladder_position": 1,
                                    "trigger_reason": "FIRST_RUNG",
                                    "lock_price": nt["lock_price"],
                                    "bid_at_trigger": last_price,
                                    "strategy": "ladder",
                                }, server_now, ist_now)
                                last_high = None

                        else:
                            # Continuing ladder — next rung at latest ± $10
                            next_trigger = ladder_next_entry(
                                ladder_direction, ladder_latest_entry)
                            should_fire = (
                                (ladder_direction == "LONG" and last_price >= next_trigger)
                                or
                                (ladder_direction == "SHORT" and last_price <= next_trigger)
                            )
                            if should_fire:
                                rung_n = len(ladder_open) + 1
                                session_date = active_anchor.date().isoformat()
                                nt = make_ladder_trade(
                                    ladder_trade_id, ladder_direction, next_trigger,
                                    ladder_anchor, server_now, ist_now,
                                    LOT_SIZE, rung_n,
                                )
                                nt["cycle_id"] = current_cycle_id
                                nt["session_date"] = session_date
                                nt["trigger_reason"] = "LADDER_CONTINUATION"
                                ladder_open.append(nt)

                                # Cascade existing locks to the new latest entry
                                cascade_locks(ladder_open, next_trigger)
                                ladder_latest_entry = next_trigger
                                ladder_trade_id += 1
                                rungs_fired_this_tick += 1
                                fired_this_tick = True

                                close_at = (next_trigger - MASTER_CLOSE_BUFFER_USD
                                            if ladder_direction == "LONG"
                                            else next_trigger + MASTER_CLOSE_BUFFER_USD)
                                if last_high is not None:
                                    print()
                                print(
                                    f">>> LADDER ENTRY  #{nt['trade_id']} {ladder_direction:<5} "
                                    f"rung {rung_n} @ ${next_trigger:.2f}  lot {LOT_SIZE}  "
                                    f"(total exposure {rung_n * LOT_SIZE:g} lot)  "
                                    f"all locks → ${next_trigger:.2f}  "
                                    f"master close @ ${close_at:.2f}  "
                                    f"cycle {current_cycle_id}  "
                                    f"[IST {ist_now:%H:%M:%S}]"
                                )
                                log_event("trade_entry", {
                                    "trade_id": nt["trade_id"],
                                    "cycle_id": current_cycle_id,
                                    "session_date": session_date,
                                    "direction": ladder_direction,
                                    "lot": LOT_SIZE,
                                    "entry_price": next_trigger,
                                    "ladder_anchor": ladder_anchor,
                                    "ladder_position": rung_n,
                                    "trigger_reason": "LADDER_CONTINUATION",
                                    "lock_price": nt["lock_price"],
                                    "bid_at_trigger": last_price,
                                    "strategy": "ladder",
                                    "cascaded_locks_to": next_trigger,
                                }, server_now, ist_now)
                                log_event("ladder_lock_cascade", {
                                    "new_lock_price": next_trigger,
                                    "rungs_affected": [t["trade_id"] for t in ladder_open],
                                    "direction": ladder_direction,
                                }, server_now, ist_now)
                                last_high = None

                    # === Per-tick risk + watermark telemetry ===
                    # Watermarks must update every tick — the ladder engine
                    # doesn't call update_trail() (no individual trail stops),
                    # so without this block high_water/low_water freeze at entry
                    # and MFE/MAE come out as zeros. BUG FIX from the v1 CSV.
                    if ladder_open:
                        for t in ladder_open:
                            if last_price > t["high_water"]:
                                t["high_water"] = last_price
                            if last_price < t["low_water"]:
                                t["low_water"] = last_price

                        floating = unrealized_pnl(ladder_open, last_price, contract_size)
                        if floating < max_floating_dd:
                            max_floating_dd = floating
                        rungs_now = len(ladder_open)
                        if rungs_now > max_rungs_today:
                            max_rungs_today = rungs_now
                        exposure_now = rungs_now * LOT_SIZE
                        if exposure_now > max_exposure_lots:
                            max_exposure_lots = exposure_now

                current_high = data["high"]["price"]
                current_low = data["low"]["price"]

                if current_high != last_high or current_low != last_low:
                    if last_high is not None:
                        print()
                    print_report(data)
                    last_high = current_high
                    last_low = current_low

                    record = build_record(data, last_price)
                    daily_records[record["date"]] = record
                    save_log(LOG_PATH, daily_records)

                # Live heartbeat — pivot + candidate + ladder + day H/L
                print_heartbeat(
                    pivots[-1], candidate, REVERSAL_THRESHOLD_USD,
                    pip_size, last_price,
                    current_high, current_low,
                    open_trades, closed_pnl_total, contract_size,
                    ladder_anchor=ladder_anchor,
                    ladder_direction=ladder_direction,
                    ladder_latest_entry=ladder_latest_entry,
                    ladder_open=ladder_open,
                    ladder_closed_pnl=ladder_closed_pnl,
                    ladder_cycles=ladder_cycles,
                    max_floating_dd=max_floating_dd,
                )

            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            today_str = (active_anchor.date().isoformat()
                         if active_anchor else "today")
            all_closed = (closed_trades_today
                          if STRATEGY_MODE == "pivot"
                          else ladder_closed_today)
            carryover = (open_trades
                         if STRATEGY_MODE == "pivot"
                         else ladder_open)
            shutdown_price = 0
            try:
                tick = mt5.symbol_info_tick(SYMBOL)
                if tick is not None:
                    shutdown_price = tick.bid
            except Exception:
                pass
            risk_stats_dict = {
                "max_floating_dd": max_floating_dd,
                "max_rungs": max_rungs_today,
                "max_exposure_lots": max_exposure_lots,
            }
            print_daily_summary(today_str, all_closed, ladder_cycles,
                                carryover_trades=carryover,
                                current_price=shutdown_price,
                                contract_size=contract_size,
                                risk_stats=risk_stats_dict)
            log_event("shutdown", {
                "reason": "keyboard_interrupt",
                "summary": daily_summary(all_closed, risk_stats=risk_stats_dict),
                "ladder_cycles": ladder_cycles,
                "carryover_open_count": len(carryover),
                "carryover_trade_ids": [t["trade_id"] for t in carryover],
            }, datetime.utcnow(), get_ist_now())
            raise
        except Exception as e:
            print(f"\n[{get_ist_now():%H:%M:%S}] tracker error: {e}")
            log_event("error", {
                "message": str(e),
                "type": type(e).__name__,
            }, datetime.utcnow(), get_ist_now())
            time.sleep(RETRY_SECONDS)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nStopped manually.")
    finally:
        mt5.shutdown()