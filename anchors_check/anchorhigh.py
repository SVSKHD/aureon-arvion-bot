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
    "trade_id", "direction", "lot", "backfill",
    "entry_price", "entry_server_time", "entry_ist_time",
    "anchor_type", "anchor_price", "anchor_server_time",
    "exit_price", "exit_server_time", "exit_ist_time", "exit_reason",
    "trail_stop_at_exit", "high_water", "low_water",
    "price_distance", "pnl_usd",
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
        "trail_stop": round(trail, 4),
        "high_water": entry_price,
        "low_water": entry_price,
        "status": "OPEN",
    }


def trade_pnl_usd(trade: dict, exit_or_current_price: float,
                  contract_size: float) -> float:
    """Dollar P&L for a paper trade at the given price."""
    if trade["direction"] == "LONG":
        per_unit = exit_or_current_price - trade["entry_price"]
    else:
        per_unit = trade["entry_price"] - exit_or_current_price
    return per_unit * trade["lot"] * contract_size


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
    """Finalize a trade in place."""
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


def check_master_close(latest_pivot: dict, current_bid: float) -> bool:
    """Has price moved MASTER_CLOSE_USD in the direction opposite to what the
    latest pivot implies? LOW expects up → opposite is down. HIGH expects
    down → opposite is up. OPEN counts neither way."""
    if latest_pivot["type"] == "LOW":
        return current_bid <= latest_pivot["price"] - MASTER_CLOSE_USD
    if latest_pivot["type"] == "HIGH":
        return current_bid >= latest_pivot["price"] + MASTER_CLOSE_USD
    return False


def unrealized_pnl(open_trades: list, current_bid: float,
                   contract_size: float) -> float:
    return sum(trade_pnl_usd(t, current_bid, contract_size) for t in open_trades)


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
                    open_trades: list, closed_pnl: float, contract_size: float):
    """One-line live state. Shows the last confirmed pivot, the running
    candidate for the next pivot, the price at which the candidate would
    confirm, plus open-trade count and unrealized + realized P&L."""
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
            f"{pivot_letter} {pivot['price']:.2f}@{pivot['server_time']:%H:%M} "
            f"→ {cand_letter} {candidate['price']:.2f}@{candidate['server_time']:%H:%M} "
            f"swg{swing:+.2f} {flip_label}"
        )
    else:
        pivot_block = (
            f"{pivot_letter} {pivot['price']:.2f}@{pivot['server_time']:%H:%M} "
            f"(no cand)"
        )

    unreal = unrealized_pnl(open_trades, last_price, contract_size)
    trade_block = (
        f"open {len(open_trades)} ${unreal:+.2f}  closed ${closed_pnl:+.2f}"
    )

    from_day_high = last_price - day_high
    from_day_low = last_price - day_low
    day_block = (
        f"day H {day_high:.2f}(Δ{from_day_high:+.2f}) "
        f"L {day_low:.2f}(Δ{from_day_low:+.2f})"
    )

    line = (
        f"\r[{ist:%H:%M:%S}] "
        f"${last_price:.2f}  "
        f"{pivot_block}  | "
        f"{day_block}  | "
        f"{trade_block}"
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

    # Paper-trade state
    open_trades = []
    closed_trades_today = []   # closed during this day, for the day_finalized summary
    anchors_used = set()       # anchor_keys already fired
    next_trade_id = 1
    closed_pnl_total = 0.0     # realized P&L for the session

    print(f"Started tracker for {SYMBOL}")
    print(f"Daily anchor: {ANCHOR_HOUR:02d}:{ANCHOR_MINUTE:02d} broker time")
    print(f"Timeframe   : M1 (1-minute timestamp resolution)")
    print(f"Pivot mode  : ZigZag — flips only on ${REVERSAL_THRESHOLD_USD:.2f} reversal")
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
                    if prev_date in daily_records:
                        r = daily_records[prev_date]
                        print(f"\n>>> Finalized {prev_date}: "
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
                            "trades_closed": len(closed_trades_today),
                            "trades_left_open": len(open_trades),
                            "realized_pnl": round(closed_pnl_total, 2),
                            "last_price_recorded": float(r.get("last_price") or 0),
                        }, get_server_now(SYMBOL), get_ist_now())

                active_anchor = anchor_time
                anchor_data = new_data
                pivots_count = 1
                last_high = None
                last_low = None
                did_backfill = False

                # Fresh day: clear paper-trade state
                open_trades = []
                closed_trades_today = []
                anchors_used = set()
                next_trade_id = 1
                closed_pnl_total = 0.0

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
            if (BACKFILL_TRADES_ON_START
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

            # Daily stats (since 02:00 anchor) — for full report + daily log
            data = compute_high_low(
                SYMBOL, active_anchor, anchor_data, server_now, pip_size,
                rates=rates,
            )

            if data:
                tick = mt5.symbol_info_tick(SYMBOL)
                last_price = tick.bid if tick is not None else data["high"]["price"]
                ist_now = get_ist_now()

                # === Paper-trade engine ===

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

                # Live heartbeat — pivot + candidate + trade summary
                print_heartbeat(
                    pivots[-1], candidate, REVERSAL_THRESHOLD_USD,
                    pip_size, last_price,
                    current_high, current_low,
                    open_trades, closed_pnl_total, contract_size,
                )

            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            log_event("shutdown", {"reason": "keyboard_interrupt"},
                      datetime.utcnow(), get_ist_now())
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