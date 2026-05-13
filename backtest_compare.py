"""
backtest_tp_compare.py — Compare TP values for XAUUSD anchor strategy

Runs the same logic as the live bot (anchor, OCO, trail, SL) on historical
M1 data from MT5, for multiple TP_DIST values side by side. Outputs:
  - Console summary table
  - Per-trade CSV: backtest_tp_compare_trades.csv
  - Per-TP summary CSV: backtest_tp_compare_summary.csv

Lock step stays $0.30 across all variants. Lock count scales with TP so the
trail covers the full range up to TP.

Usage:
    python backtest_tp_compare.py

Date range and TP variants are configured at the top of this file.
"""

import csv
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import MetaTrader5 as mt5
import numpy as np

import config


# ----------------------------------------------------------------------------
# Backtest configuration
# ----------------------------------------------------------------------------

# Date range — backtest covers [START, END) in broker time
START_DATE = datetime(2026, 1, 1)
END_DATE   = datetime(2026, 5, 1)   # exclusive

# TP variants to compare (in USD price units)
TP_VARIANTS = [3.0, 6.0, 8.0, 10.0, 15.0]

# Strategy constants (mirror live bot)
LOT_SIZE        = config.LOT_SIZE
TRIGGER_DIST    = config.TRIGGER_DIST
SL_DIST         = config.SL_DIST
LOCK_STEP       = config.LOCK_STEP
ANCHOR_HOUR     = config.ANCHOR_HOUR
EOD_HOUR        = config.EOD_CANCEL_HOUR

# XAUUSD: 1 lot moves $100 per $1 price change → 0.5 lot = $50 per $1
USD_PER_DOLLAR_PER_LOT = 100.0


# ----------------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------------

@dataclass
class TradeResult:
    date: str           # YYYY-MM-DD (broker)
    tp_dist: float
    side: str           # 'LONG', 'SHORT', or 'NONE'
    anchor: float
    entry_price: float  # 0 if no trigger
    exit_price: float   # 0 if no trigger
    exit_reason: str    # 'TP', 'SL', 'TRAIL', 'EOD', 'NO_TRIGGER'
    pnl_price: float    # exit - entry (signed; for LONG: exit-entry, for SHORT: entry-exit)
    pnl_usd: float
    max_favorable: float
    final_lock_idx: int


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def dollar_pnl(price_diff: float) -> float:
    """Convert price movement (USD) to dollar PnL at LOT_SIZE."""
    return price_diff * LOT_SIZE * USD_PER_DOLLAR_PER_LOT


# Match live bot's trailing.py: EPSILON-safe float compare, floor-based idx
EPSILON = 1e-9


def lock_count_for_tp(tp_dist: float) -> int:
    """
    Max lock levels usable before TP. With LOCK_STEP=$0.30 and TP=$3:
      9 levels (indices 0..8) at +0.30, +0.60, +0.90, ..., +2.70 — matches
      live bot's LOCK_STEPS_COUNT=9.
    Formula: ceil(TP/LOCK_STEP) - 1
    """
    import math
    raw = tp_dist / LOCK_STEP
    return int(math.ceil(raw - EPSILON)) - 1


def lock_step_idx(favorable: float, lock_count: int) -> int:
    """
    EXACT mirror of live bot's calculate_lock_step (strategy/trailing.py):
      idx -1 = no lock yet (favorable < $0.30)
      idx  0 = +$0.30 reached → SL moves to entry+$0.30
      idx  1 = +$0.60 reached → SL moves to entry+$0.60
      ...
      idx  N = capped at lock_count - 1
    """
    import math
    if favorable + EPSILON < LOCK_STEP:
        return -1
    steps_reached = int(math.floor((favorable + EPSILON) / LOCK_STEP))
    idx = steps_reached - 1
    return min(idx, lock_count - 1)


# ----------------------------------------------------------------------------
# Per-day simulation
# ----------------------------------------------------------------------------

def simulate_day(
    date_str: str,
    anchor: float,
    bars: np.ndarray,
    tp_dist: float,
) -> TradeResult:
    """
    Simulate one trading day given anchor + M1 bars from 02:00 broker onward.
    Returns the resulting trade (NONE side if no trigger fired before EOD).
    """
    lock_count = lock_count_for_tp(tp_dist)

    long_entry  = anchor + TRIGGER_DIST
    short_entry = anchor - TRIGGER_DIST
    long_sl_0   = long_entry - SL_DIST
    short_sl_0  = short_entry + SL_DIST
    long_tp     = long_entry + tp_dist
    short_tp    = short_entry - tp_dist

    side: Optional[str] = None
    entry_price = 0.0
    current_sl  = 0.0
    current_idx = -1
    max_fav     = 0.0

    last_close = float(bars[-1]['close']) if len(bars) else anchor

    for bar in bars:
        bar_open  = float(bar['open'])
        bar_high  = float(bar['high'])
        bar_low   = float(bar['low'])
        bar_close = float(bar['close'])

        # ---------------- Entry detection ----------------
        if side is None:
            long_hit  = bar_high >= long_entry
            short_hit = bar_low  <= short_entry
            if long_hit and short_hit:
                # Both fire same bar — use bar direction heuristic
                side = 'LONG' if bar_open >= anchor else 'SHORT'
            elif long_hit:
                side = 'LONG'
            elif short_hit:
                side = 'SHORT'

            if side == 'LONG':
                entry_price = long_entry  # stop fills at trigger (assume zero slip)
                current_sl  = long_sl_0
            elif side == 'SHORT':
                entry_price = short_entry
                current_sl  = short_sl_0

            if side is None:
                continue
            # Skip intrabar exit on entry bar (can't determine order without ticks)
            continue

        # ---------------- Trail SL ----------------
        if side == 'LONG':
            favorable = bar_high - entry_price
            if favorable > max_fav:
                max_fav = favorable
            new_idx = lock_step_idx(favorable, lock_count)
            if new_idx > current_idx:
                proposed_sl = entry_price + LOCK_STEP * (new_idx + 1)
                if proposed_sl > current_sl:
                    current_sl = proposed_sl
                    current_idx = new_idx
        else:
            favorable = entry_price - bar_low
            if favorable > max_fav:
                max_fav = favorable
            new_idx = lock_step_idx(favorable, lock_count)
            if new_idx > current_idx:
                proposed_sl = entry_price - LOCK_STEP * (new_idx + 1)
                if proposed_sl < current_sl:
                    current_sl = proposed_sl
                    current_idx = new_idx

        # ---------------- Exit checks ----------------
        if side == 'LONG':
            tp_hit = bar_high >= long_tp
            sl_hit = bar_low  <= current_sl
            if tp_hit and sl_hit:
                # Both within same bar — use direction heuristic
                tp_first = bar_close >= bar_open  # bar up → likely high reached after low
                exit_price = long_tp if tp_first else current_sl
                reason = 'TP' if tp_first else ('TRAIL' if current_idx >= 0 else 'SL')
                return TradeResult(
                    date=date_str, tp_dist=tp_dist, side=side, anchor=anchor,
                    entry_price=entry_price, exit_price=exit_price,
                    exit_reason=reason,
                    pnl_price=exit_price - entry_price,
                    pnl_usd=dollar_pnl(exit_price - entry_price),
                    max_favorable=max_fav, final_lock_idx=current_idx,
                )
            if tp_hit:
                return TradeResult(
                    date=date_str, tp_dist=tp_dist, side=side, anchor=anchor,
                    entry_price=entry_price, exit_price=long_tp,
                    exit_reason='TP',
                    pnl_price=long_tp - entry_price,
                    pnl_usd=dollar_pnl(long_tp - entry_price),
                    max_favorable=max_fav, final_lock_idx=current_idx,
                )
            if sl_hit:
                return TradeResult(
                    date=date_str, tp_dist=tp_dist, side=side, anchor=anchor,
                    entry_price=entry_price, exit_price=current_sl,
                    exit_reason='TRAIL' if current_idx >= 0 else 'SL',
                    pnl_price=current_sl - entry_price,
                    pnl_usd=dollar_pnl(current_sl - entry_price),
                    max_favorable=max_fav, final_lock_idx=current_idx,
                )
        else:  # SHORT
            tp_hit = bar_low  <= short_tp
            sl_hit = bar_high >= current_sl
            if tp_hit and sl_hit:
                tp_first = bar_close <= bar_open  # bar down → likely low reached after high
                exit_price = short_tp if tp_first else current_sl
                reason = 'TP' if tp_first else ('TRAIL' if current_idx >= 0 else 'SL')
                pnl_price = entry_price - exit_price
                return TradeResult(
                    date=date_str, tp_dist=tp_dist, side=side, anchor=anchor,
                    entry_price=entry_price, exit_price=exit_price,
                    exit_reason=reason,
                    pnl_price=pnl_price, pnl_usd=dollar_pnl(pnl_price),
                    max_favorable=max_fav, final_lock_idx=current_idx,
                )
            if tp_hit:
                return TradeResult(
                    date=date_str, tp_dist=tp_dist, side=side, anchor=anchor,
                    entry_price=entry_price, exit_price=short_tp,
                    exit_reason='TP',
                    pnl_price=entry_price - short_tp,
                    pnl_usd=dollar_pnl(entry_price - short_tp),
                    max_favorable=max_fav, final_lock_idx=current_idx,
                )
            if sl_hit:
                pnl_price = entry_price - current_sl
                return TradeResult(
                    date=date_str, tp_dist=tp_dist, side=side, anchor=anchor,
                    entry_price=entry_price, exit_price=current_sl,
                    exit_reason='TRAIL' if current_idx >= 0 else 'SL',
                    pnl_price=pnl_price, pnl_usd=dollar_pnl(pnl_price),
                    max_favorable=max_fav, final_lock_idx=current_idx,
                )

    # EOD reached without TP/SL — close at last close
    if side is None:
        return TradeResult(
            date=date_str, tp_dist=tp_dist, side='NONE', anchor=anchor,
            entry_price=0.0, exit_price=0.0, exit_reason='NO_TRIGGER',
            pnl_price=0.0, pnl_usd=0.0,
            max_favorable=0.0, final_lock_idx=-1,
        )
    if side == 'LONG':
        pnl_price = last_close - entry_price
    else:
        pnl_price = entry_price - last_close
    return TradeResult(
        date=date_str, tp_dist=tp_dist, side=side, anchor=anchor,
        entry_price=entry_price, exit_price=last_close,
        exit_reason='EOD', pnl_price=pnl_price, pnl_usd=dollar_pnl(pnl_price),
        max_favorable=max_fav, final_lock_idx=current_idx,
    )


# ----------------------------------------------------------------------------
# Data fetch + loop
# ----------------------------------------------------------------------------

def fetch_m1(start: datetime, end: datetime) -> Optional[np.ndarray]:
    """Fetch M1 bars in [start, end) — broker time. Returns structured array."""
    bars = mt5.copy_rates_range(config.SYMBOL, mt5.TIMEFRAME_M1, start, end)
    if bars is None or len(bars) == 0:
        print(f"  No data for {start.date()} ({mt5.last_error()})")
        return None
    return bars


def find_anchor_price(m1_bars: np.ndarray, date: datetime) -> Optional[float]:
    """
    Find the M5 candle at 02:00 broker on the given date.
    M5 close at 02:05 = M1 close of bar at 02:04.
    We use the M1 bar at exactly 02:04 to mimic live bot's M5-close anchor.
    """
    anchor_time = datetime(date.year, date.month, date.day, ANCHOR_HOUR, 4).timestamp()
    matches = m1_bars[m1_bars['time'] == int(anchor_time)]
    if len(matches) == 0:
        # Fallback: bar at 02:00 (use close)
        anchor_time = datetime(date.year, date.month, date.day, ANCHOR_HOUR, 0).timestamp()
        matches = m1_bars[m1_bars['time'] == int(anchor_time)]
    if len(matches) == 0:
        return None
    return float(matches[0]['close'])


def run_backtest() -> List[TradeResult]:
    """Walk every day in the range. For each, simulate all TP variants."""
    print(f"Backtest range: {START_DATE.date()} → {END_DATE.date()}")
    print(f"TP variants: {TP_VARIANTS}")
    print(f"Symbol: {config.SYMBOL}  Lot: {LOT_SIZE}")
    print(f"Trigger: ±${TRIGGER_DIST}  SL: ±${SL_DIST}  Lock step: ${LOCK_STEP}")
    print()

    all_results: List[TradeResult] = []
    day = START_DATE
    while day < END_DATE:
        if day.weekday() >= 5:  # Saturday=5, Sunday=6
            day += timedelta(days=1)
            continue

        # Fetch M1 for the full day (02:00 → next day 02:00 broker)
        day_start = datetime(day.year, day.month, day.day, ANCHOR_HOUR, 0)
        day_end   = day_start + timedelta(hours=21)  # to 23:00 broker
        m1 = fetch_m1(day_start, day_end + timedelta(minutes=5))
        if m1 is None or len(m1) < 10:
            day += timedelta(days=1)
            continue

        anchor = find_anchor_price(m1, day)
        if anchor is None:
            print(f"  {day.date()}: no anchor candle, skipping")
            day += timedelta(days=1)
            continue

        # Bars for the trade window: from 02:05 (right after anchor) to 23:00
        sim_start = int(datetime(day.year, day.month, day.day, ANCHOR_HOUR, 5).timestamp())
        sim_end   = int(datetime(day.year, day.month, day.day, EOD_HOUR, 0).timestamp())
        bars = m1[(m1['time'] >= sim_start) & (m1['time'] < sim_end)]
        if len(bars) < 10:
            day += timedelta(days=1)
            continue

        date_str = day.strftime('%Y-%m-%d')
        for tp in TP_VARIANTS:
            res = simulate_day(date_str, anchor, bars, tp)
            all_results.append(res)

        day += timedelta(days=1)

    return all_results


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------

def summarize_by_month(results: List[TradeResult]) -> None:
    """Pivot table: rows = year-month, columns = TP variants, cells = monthly PnL."""
    months = sorted(set(r.date[:7] for r in results))  # 'YYYY-MM'
    if not months:
        return

    print()
    print("=" * 100)
    print("MONTHLY PnL BREAKDOWN")
    print("=" * 100)

    # Header
    header = f"{'Month':<10}"
    for tp in TP_VARIANTS:
        header += f" | {'TP $' + str(tp):>10}"
    header += f" | {'Trades':>7}"
    print(header)
    print("-" * len(header))

    # Per-month rows
    monthly_csv_rows = []
    for month in months:
        row = f"{month:<10}"
        month_results = [r for r in results if r.date.startswith(month)]
        row_data = {'month': month}
        for tp in TP_VARIANTS:
            tp_results = [r for r in month_results if r.tp_dist == tp]
            month_pnl = sum(r.pnl_usd for r in tp_results)
            n_tp = sum(1 for r in tp_results if r.exit_reason == 'TP')
            n_sl = sum(1 for r in tp_results if r.exit_reason == 'SL')
            row += f" | ${month_pnl:>9.2f}"
            row_data[f'tp_{tp}_pnl'] = round(month_pnl, 2)
            row_data[f'tp_{tp}_tps'] = n_tp
            row_data[f'tp_{tp}_sls'] = n_sl
        # Trade count is same across all TPs (same days)
        days_in_month = len(set(r.date for r in month_results))
        triggered = len([r for r in month_results
                         if r.tp_dist == TP_VARIANTS[0] and r.side != 'NONE'])
        row += f" | {triggered}/{days_in_month}"
        row_data['triggered'] = triggered
        row_data['days'] = days_in_month
        monthly_csv_rows.append(row_data)
        print(row)

    # Totals row
    total_row = f"{'TOTAL':<10}"
    for tp in TP_VARIANTS:
        tp_results = [r for r in results if r.tp_dist == tp]
        total_pnl = sum(r.pnl_usd for r in tp_results)
        total_row += f" | ${total_pnl:>9.2f}"
    total_days = len(set(r.date for r in results))
    total_triggered = len([r for r in results
                           if r.tp_dist == TP_VARIANTS[0] and r.side != 'NONE'])
    total_row += f" | {total_triggered}/{total_days}"
    print("-" * len(header))
    print(total_row)
    print("=" * 100)

    # Per-TP outcome breakdown by month
    print()
    print("MONTHLY OUTCOMES (TP-hits / Trail-exits / Full-SLs)")
    print("=" * 100)
    outcome_header = f"{'Month':<10}"
    for tp in TP_VARIANTS:
        outcome_header += f" | {'TP$' + str(tp):>14}"
    print(outcome_header)
    print("-" * len(outcome_header))
    for month in months:
        row = f"{month:<10}"
        month_results = [r for r in results if r.date.startswith(month)]
        for tp in TP_VARIANTS:
            tp_results = [r for r in month_results if r.tp_dist == tp]
            n_tp = sum(1 for r in tp_results if r.exit_reason == 'TP')
            n_trail = sum(1 for r in tp_results if r.exit_reason == 'TRAIL')
            n_sl = sum(1 for r in tp_results if r.exit_reason == 'SL')
            row += f" | {n_tp:>3}/{n_trail:>3}/{n_sl:>3}    "
        print(row)
    print("=" * 100)

    # Write monthly CSV
    if monthly_csv_rows:
        with open('backtest_tp_compare_monthly.csv', 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(monthly_csv_rows[0].keys()))
            w.writeheader()
            w.writerows(monthly_csv_rows)
        print()
        print("Monthly breakdown written to backtest_tp_compare_monthly.csv")


def summarize(results: List[TradeResult]) -> None:
    """Print summary table and write CSVs."""
    by_tp = {}
    for r in results:
        by_tp.setdefault(r.tp_dist, []).append(r)

    print()
    print("=" * 100)
    print(f"{'TP':>6} | {'Days':>5} | {'Trig':>5} | {'TP':>4} | {'Trail':>5} | "
          f"{'SL':>4} | {'EOD':>4} | {'WinRt':>6} | {'PnL$':>10} | {'AvgPnL':>8} | "
          f"{'MaxDD':>8} | {'Avg+Fav':>8}")
    print("-" * 100)

    summary_rows = []
    for tp in TP_VARIANTS:
        rs = by_tp.get(tp, [])
        days = len(rs)
        triggered = [r for r in rs if r.side != 'NONE']
        n_trig = len(triggered)
        n_tp   = sum(1 for r in triggered if r.exit_reason == 'TP')
        n_trail = sum(1 for r in triggered if r.exit_reason == 'TRAIL')
        n_sl   = sum(1 for r in triggered if r.exit_reason == 'SL')
        n_eod  = sum(1 for r in triggered if r.exit_reason == 'EOD')
        pnls   = [r.pnl_usd for r in triggered]
        total_pnl = sum(pnls)
        avg_pnl   = total_pnl / n_trig if n_trig else 0.0
        wins = [p for p in pnls if p > 0]
        win_rt = (len(wins) / n_trig * 100) if n_trig else 0.0
        # Equity curve max drawdown
        eq = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnls:
            eq += p
            if eq > peak:
                peak = eq
            dd = eq - peak
            if dd < max_dd:
                max_dd = dd
        avg_max_fav = (sum(r.max_favorable for r in triggered) / n_trig) if n_trig else 0.0

        print(f"${tp:>5.1f} | {days:>5} | {n_trig:>5} | {n_tp:>4} | {n_trail:>5} | "
              f"{n_sl:>4} | {n_eod:>4} | {win_rt:>5.1f}% | ${total_pnl:>9.2f} | "
              f"${avg_pnl:>7.2f} | ${max_dd:>7.2f} | ${avg_max_fav:>7.2f}")

        summary_rows.append({
            'tp_dist': tp,
            'days_total': days,
            'triggered': n_trig,
            'tp_hits': n_tp,
            'trail_exits': n_trail,
            'sl_hits': n_sl,
            'eod_exits': n_eod,
            'win_rate_pct': round(win_rt, 2),
            'total_pnl_usd': round(total_pnl, 2),
            'avg_pnl_usd': round(avg_pnl, 2),
            'max_drawdown_usd': round(max_dd, 2),
            'avg_max_favorable': round(avg_max_fav, 2),
        })

    print("=" * 100)
    print()
    print("Legend:")
    print("  Trig   = day with anchor + breakout fired")
    print("  TP     = exited at full take profit")
    print("  Trail  = exited at trailed lock-step SL (small win or breakeven)")
    print("  SL    = exited at original SL (full loss -$750)")
    print("  EOD    = position open at 23:00 broker (closed at last bar)")
    print("  Avg+Fav = avg maximum favorable move per triggered trade")
    print()

    # Write CSVs
    with open('backtest_tp_compare_summary.csv', 'w', newline='') as f:
        if summary_rows:
            w = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
            w.writeheader()
            w.writerows(summary_rows)
    print(f"Summary written to backtest_tp_compare_summary.csv")

    with open('backtest_tp_compare_trades.csv', 'w', newline='') as f:
        if results:
            w = csv.DictWriter(f, fieldnames=list(asdict(results[0]).keys()))
            w.writeheader()
            for r in results:
                w.writerow(asdict(r))
    print(f"Per-trade detail written to backtest_tp_compare_trades.csv")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> int:
    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}", file=sys.stderr)
        return 1

    info = mt5.account_info()
    if info is None:
        print("Not logged in to MT5", file=sys.stderr)
        mt5.shutdown()
        return 1

    print(f"Account: {info.login}  Broker: {info.server}")
    print()

    # Ensure symbol is visible
    if not mt5.symbol_select(config.SYMBOL, True):
        print(f"Cannot select {config.SYMBOL}", file=sys.stderr)
        mt5.shutdown()
        return 1

    results = run_backtest()
    summarize(results)
    summarize_by_month(results)

    mt5.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())