"""
backtest_rescue.py — Simulate rescue hedge strategy.

Idea: when an open position reaches -$10 adverse (5 from SL), open an
opposite-direction trade to capture the continuation move. Both positions
manage independently with the same TP/SL/lock-step trail logic.

USAGE
    python backtest_rescue.py

OUTPUT
    - Per-trade detail of when rescue fired
    - Distribution: how often rescue helped vs hurt
    - Comparison: baseline vs rescue strategy total PnL
"""

import csv
import sys
from datetime import datetime, timedelta

import MetaTrader5 as mt5
import numpy as np

# Strategy params (must match config.py)
SYMBOL = "XAUUSD"
LOT_SIZE = 0.5
TRIGGER_DIST = 10.0
TP_DIST = 3.0
SL_DIST = 15.0
LOCK_STEP = 0.30
LOCK_STEPS_COUNT = 9
ANCHOR_HOUR = 2

# Rescue trigger: adverse distance from entry that triggers rescue
RESCUE_TRIGGER_ADVERSE = 10.0  # i.e., when LONG is -$10 from entry

# Backtest range
START = datetime(2026, 1, 1)
END = datetime(2026, 4, 30, 23, 59)


def main():
    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}")
        sys.exit(1)

    print(f"Fetching M1 bars {START} → {END}...")
    m1 = mt5.copy_rates_range(SYMBOL, mt5.TIMEFRAME_M1, START, END + timedelta(days=1))
    if m1 is None or len(m1) == 0:
        print(f"M1 fetch failed: {mt5.last_error()}")
        sys.exit(1)
    print(f"  → {len(m1):,} M1 bars\n")

    results = []
    current = START
    while current <= END:
        if current.weekday() < 5:
            anchor_time = current.replace(hour=ANCHOR_HOUR, minute=0, second=0)
            r = simulate_day(m1, anchor_time)
            if r is not None:
                results.append(r)
        current += timedelta(days=1)

    print(f"Simulated {len(results)} trade days\n")

    # Save CSV
    save_csv(results, "rescue_results.csv")

    # Print analysis
    print_per_trade(results)
    print_rescue_analysis(results)
    print_comparison(results)

    mt5.shutdown()


def simulate_day(m1, anchor_time):
    """Simulate one day with potential rescue."""
    anchor_ts = int(anchor_time.timestamp())
    anchor_bar = m1[m1['time'] == anchor_ts]
    if len(anchor_bar) == 0:
        return None
    anchor_price = float(anchor_bar[0]['close'])

    long_entry = round(anchor_price + TRIGGER_DIST, 2)
    short_entry = round(anchor_price - TRIGGER_DIST, 2)

    end_ts = anchor_ts + 22 * 3600
    day_bars = m1[(m1['time'] >= anchor_ts) & (m1['time'] <= end_ts)]
    if len(day_bars) == 0:
        return None

    # Find first trigger
    long_fired = day_bars['high'] >= long_entry
    short_fired = day_bars['low'] <= short_entry
    long_idx = int(np.argmax(long_fired)) if long_fired.any() else -1
    short_idx = int(np.argmax(short_fired)) if short_fired.any() else -1

    if long_idx == -1 and short_idx == -1:
        return {'date': anchor_time.date().isoformat(), 'anchor': anchor_price,
                'side': 'NONE', 'orig_pnl': 0, 'rescue_pnl': 0, 'rescue_fired': False,
                'orig_outcome': 'NO_TRIGGER', 'rescue_outcome': '',
                'combined_pnl_baseline': 0, 'combined_pnl_rescue': 0}

    if long_idx == -1 or (short_idx != -1 and short_idx < long_idx):
        side = 'SHORT'
        entry = short_entry
        fill_idx = short_idx
    else:
        side = 'LONG'
        entry = long_entry
        fill_idx = long_idx

    sl = entry - SL_DIST if side == 'LONG' else entry + SL_DIST
    tp = entry + TP_DIST if side == 'LONG' else entry - TP_DIST

    # Simulate original position with rescue tracking
    post = day_bars[fill_idx:]
    sim = walk_with_rescue(post, side, entry, sl, tp)

    return {
        'date': anchor_time.date().isoformat(),
        'anchor': anchor_price,
        'side': side,
        'entry': entry,
        'orig_outcome': sim['orig_outcome'],
        'orig_exit': sim['orig_exit'],
        'orig_pnl': sim['orig_pnl'],
        'rescue_fired': sim['rescue_fired'],
        'rescue_outcome': sim['rescue_outcome'],
        'rescue_pnl': sim['rescue_pnl'],
        'combined_pnl_baseline': sim['orig_pnl'],
        'combined_pnl_rescue': sim['orig_pnl'] + sim['rescue_pnl'],
        'rescue_entry': sim.get('rescue_entry'),
        'rescue_exit': sim.get('rescue_exit'),
    }


def walk_with_rescue(bars, side, entry, sl, tp):
    """
    Walk bars, simulating original position with possible rescue.
    Rescue fires when price reaches entry +/- RESCUE_TRIGGER_ADVERSE adverse.
    """
    # Original position state
    orig_step = 0
    orig_sl = sl
    orig_exit = None
    orig_outcome = None
    orig_pnl = 0
    orig_closed_idx = None

    # Rescue position state (opposite direction)
    rescue_fired = False
    rescue_entry = None
    rescue_sl = None
    rescue_tp = None
    rescue_step = 0
    rescue_curr_sl = None
    rescue_exit = None
    rescue_outcome = None
    rescue_pnl = 0
    rescue_fired_idx = None

    for i, bar in enumerate(bars):
        h = float(bar['high'])
        l = float(bar['low'])

        # === ORIGINAL POSITION ===
        if orig_outcome is None:
            if side == 'LONG':
                # Check for rescue trigger (price hit entry - 10)
                if not rescue_fired and l <= entry - RESCUE_TRIGGER_ADVERSE:
                    # Fire rescue SHORT at the trigger price
                    rescue_fired = True
                    rescue_fired_idx = i
                    rescue_entry = round(entry - RESCUE_TRIGGER_ADVERSE, 2)
                    rescue_sl = round(rescue_entry + SL_DIST, 2)
                    rescue_tp = round(rescue_entry - TP_DIST, 2)
                    rescue_curr_sl = rescue_sl

                # TP first (optimistic)
                if h >= tp:
                    orig_outcome = 'TP'
                    orig_exit = tp
                    orig_pnl = round((tp - entry) * LOT_SIZE * 100, 2)
                    orig_closed_idx = i
                else:
                    # Update lock-step
                    fav = max(0.0, h - entry)
                    new_step = min(int(fav / LOCK_STEP), LOCK_STEPS_COUNT)
                    if new_step > orig_step:
                        orig_step = new_step
                        orig_sl = round(entry + (orig_step - 1) * LOCK_STEP, 2)
                    if l <= orig_sl:
                        if orig_step == 0:
                            orig_outcome = 'SL'
                        elif orig_step == 1:
                            orig_outcome = 'BE'
                        else:
                            orig_outcome = 'Trail'
                        orig_exit = orig_sl
                        orig_pnl = round((orig_sl - entry) * LOT_SIZE * 100, 2)
                        orig_closed_idx = i
            else:  # SHORT original
                if not rescue_fired and h >= entry + RESCUE_TRIGGER_ADVERSE:
                    rescue_fired = True
                    rescue_fired_idx = i
                    rescue_entry = round(entry + RESCUE_TRIGGER_ADVERSE, 2)
                    rescue_sl = round(rescue_entry - SL_DIST, 2)
                    rescue_tp = round(rescue_entry + TP_DIST, 2)
                    rescue_curr_sl = rescue_sl

                if l <= tp:
                    orig_outcome = 'TP'
                    orig_exit = tp
                    orig_pnl = round((entry - tp) * LOT_SIZE * 100, 2)
                    orig_closed_idx = i
                else:
                    fav = max(0.0, entry - l)
                    new_step = min(int(fav / LOCK_STEP), LOCK_STEPS_COUNT)
                    if new_step > orig_step:
                        orig_step = new_step
                        orig_sl = round(entry - (orig_step - 1) * LOCK_STEP, 2)
                    if h >= orig_sl:
                        if orig_step == 0:
                            orig_outcome = 'SL'
                        elif orig_step == 1:
                            orig_outcome = 'BE'
                        else:
                            orig_outcome = 'Trail'
                        orig_exit = orig_sl
                        orig_pnl = round((entry - orig_sl) * LOT_SIZE * 100, 2)
                        orig_closed_idx = i

        # === RESCUE POSITION ===
        if rescue_fired and rescue_outcome is None and (rescue_fired_idx is not None and i >= rescue_fired_idx):
            # Rescue is opposite direction
            r_side = 'SHORT' if side == 'LONG' else 'LONG'

            if r_side == 'LONG':
                if h >= rescue_tp:
                    rescue_outcome = 'TP'
                    rescue_exit = rescue_tp
                    rescue_pnl = round((rescue_tp - rescue_entry) * LOT_SIZE * 100, 2)
                else:
                    fav = max(0.0, h - rescue_entry)
                    new_step = min(int(fav / LOCK_STEP), LOCK_STEPS_COUNT)
                    if new_step > rescue_step:
                        rescue_step = new_step
                        rescue_curr_sl = round(rescue_entry + (rescue_step - 1) * LOCK_STEP, 2)
                    if l <= rescue_curr_sl:
                        if rescue_step == 0:
                            rescue_outcome = 'SL'
                        elif rescue_step == 1:
                            rescue_outcome = 'BE'
                        else:
                            rescue_outcome = 'Trail'
                        rescue_exit = rescue_curr_sl
                        rescue_pnl = round((rescue_curr_sl - rescue_entry) * LOT_SIZE * 100, 2)
            else:  # SHORT rescue
                if l <= rescue_tp:
                    rescue_outcome = 'TP'
                    rescue_exit = rescue_tp
                    rescue_pnl = round((rescue_entry - rescue_tp) * LOT_SIZE * 100, 2)
                else:
                    fav = max(0.0, rescue_entry - l)
                    new_step = min(int(fav / LOCK_STEP), LOCK_STEPS_COUNT)
                    if new_step > rescue_step:
                        rescue_step = new_step
                        rescue_curr_sl = round(rescue_entry - (rescue_step - 1) * LOCK_STEP, 2)
                    if h >= rescue_curr_sl:
                        if rescue_step == 0:
                            rescue_outcome = 'SL'
                        elif rescue_step == 1:
                            rescue_outcome = 'BE'
                        else:
                            rescue_outcome = 'Trail'
                        rescue_exit = rescue_curr_sl
                        rescue_pnl = round((rescue_entry - rescue_curr_sl) * LOT_SIZE * 100, 2)

        # Stop simulation if both closed
        if orig_outcome is not None and (not rescue_fired or rescue_outcome is not None):
            break

    # EOD fallback for orig
    if orig_outcome is None:
        last_close = float(bars[-1]['close'])
        orig_outcome = 'EOD'
        orig_exit = last_close
        if side == 'LONG':
            orig_pnl = round((last_close - entry) * LOT_SIZE * 100, 2)
        else:
            orig_pnl = round((entry - last_close) * LOT_SIZE * 100, 2)

    # EOD fallback for rescue
    if rescue_fired and rescue_outcome is None:
        last_close = float(bars[-1]['close'])
        rescue_outcome = 'EOD'
        rescue_exit = last_close
        r_side = 'SHORT' if side == 'LONG' else 'LONG'
        if r_side == 'LONG':
            rescue_pnl = round((last_close - rescue_entry) * LOT_SIZE * 100, 2)
        else:
            rescue_pnl = round((rescue_entry - last_close) * LOT_SIZE * 100, 2)

    return {
        'orig_outcome': orig_outcome, 'orig_exit': orig_exit, 'orig_pnl': orig_pnl,
        'rescue_fired': rescue_fired,
        'rescue_outcome': rescue_outcome or '', 'rescue_exit': rescue_exit,
        'rescue_pnl': rescue_pnl,
        'rescue_entry': rescue_entry,
    }


def save_csv(results, path):
    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'date', 'anchor', 'side', 'entry', 'orig_outcome', 'orig_exit', 'orig_pnl',
            'rescue_fired', 'rescue_entry', 'rescue_outcome', 'rescue_exit', 'rescue_pnl',
            'combined_pnl_baseline', 'combined_pnl_rescue',
        ])
        writer.writeheader()
        writer.writerows(results)
    print(f"Saved per-trade CSV: {path}\n")


def print_per_trade(results):
    print("=" * 130)
    print("PER-TRADE DETAIL — rescue trigger analysis")
    print("=" * 130)
    print(f"{'Date':<12} {'Side':>5} {'Orig':>6} {'OrigPnL':>9} {'Rescue?':>8} "
          f"{'RescOut':>8} {'RescPnL':>9} {'Baseline':>10} {'WithRescue':>11}")
    print("-" * 130)
    for r in results:
        if r['side'] == 'NONE':
            continue
        print(f"{r['date']:<12} {r['side']:>5} {r['orig_outcome']:>6} "
              f"${r['orig_pnl']:>+8.2f} {'YES' if r['rescue_fired'] else 'no':>8} "
              f"{r.get('rescue_outcome', '') or '—':>8} "
              f"${r.get('rescue_pnl', 0):>+8.2f} "
              f"${r['combined_pnl_baseline']:>+9.2f} "
              f"${r['combined_pnl_rescue']:>+10.2f}")
    print()


def print_rescue_analysis(results):
    fired = [r for r in results if r.get('rescue_fired')]
    not_fired = [r for r in results if r['side'] != 'NONE' and not r.get('rescue_fired')]

    print("=" * 80)
    print("RESCUE FIRING ANALYSIS")
    print("=" * 80)
    print(f"Total trades:           {sum(1 for r in results if r['side'] != 'NONE')}")
    print(f"Rescue fired:           {len(fired)}")
    print(f"Rescue did NOT fire:    {len(not_fired)}")
    print()

    if fired:
        helps = [r for r in fired if r['combined_pnl_rescue'] > r['combined_pnl_baseline']]
        hurts = [r for r in fired if r['combined_pnl_rescue'] < r['combined_pnl_baseline']]
        same = [r for r in fired if r['combined_pnl_rescue'] == r['combined_pnl_baseline']]

        print(f"When rescue fired ({len(fired)} times):")
        print(f"  Rescue HELPED (saved money):  {len(helps)}")
        print(f"  Rescue HURT  (cost money):    {len(hurts)}")
        print(f"  Rescue neutral:               {len(same)}")
        print()

        if helps:
            print(f"HELPER TRADES (rescue saved money):")
            for r in helps:
                diff = r['combined_pnl_rescue'] - r['combined_pnl_baseline']
                print(f"  {r['date']} {r['side']:>5} {r['orig_outcome']:>5} "
                      f"baseline ${r['combined_pnl_baseline']:>+8.2f} → "
                      f"with rescue ${r['combined_pnl_rescue']:>+8.2f} "
                      f"(save ${diff:>+8.2f})")
            print()

        if hurts:
            print(f"HURTER TRADES (rescue cost money):")
            for r in hurts:
                diff = r['combined_pnl_rescue'] - r['combined_pnl_baseline']
                print(f"  {r['date']} {r['side']:>5} {r['orig_outcome']:>5} "
                      f"baseline ${r['combined_pnl_baseline']:>+8.2f} → "
                      f"with rescue ${r['combined_pnl_rescue']:>+8.2f} "
                      f"(cost ${diff:>+8.2f})")
            print()


def print_comparison(results):
    base_total = sum(r['combined_pnl_baseline'] for r in results)
    rescue_total = sum(r['combined_pnl_rescue'] for r in results)

    print("=" * 80)
    print("FINAL COMPARISON")
    print("=" * 80)
    print(f"Baseline (no rescue):    ${base_total:>+10.2f}")
    print(f"With rescue mechanism:   ${rescue_total:>+10.2f}")
    print(f"Difference:              ${rescue_total - base_total:>+10.2f}")
    print()
    if rescue_total > base_total:
        print("✅ Rescue mechanism IMPROVES net PnL")
    elif rescue_total < base_total:
        print("❌ Rescue mechanism HURTS net PnL")
    else:
        print("➖ Rescue mechanism has no net effect")
    print()


if __name__ == "__main__":
    main()