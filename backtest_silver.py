"""
backtest_silver.py — Anchor strategy backtest for XAGUSD with configurable parameters.

Tests the same anchor breakout logic from the XAUUSD bot applied to silver.
All distances configurable via command-line args so you can iterate quickly.

XAGUSD (silver) specifics:
  - Typical price: $20-$35
  - Contract size: 5000 oz (default; some brokers use different)
  - 1 lot × $1 move = $5,000 per dollar (vs $100 for gold)
  - Daily range: usually $0.30 - $1.50

USAGE
    # Default silver parameters
    python backtest_silver.py

    # Custom configuration
    python backtest_silver.py --trigger 0.15 --tp 0.05 --sl 0.15 --lock 0.01

    # Multi-config sweep (run multiple times)
    python backtest_silver.py --trigger 0.10 --tp 0.03 --sl 0.10
    python backtest_silver.py --trigger 0.15 --tp 0.05 --sl 0.15
    python backtest_silver.py --trigger 0.20 --tp 0.07 --sl 0.20

    # Different symbol name if your broker uses XAGUSD.r or similar
    python backtest_silver.py --symbol XAGUSD.r

OUTPUT
    1. Per-trade table with all outcomes
    2. ATR distribution (M15 ATR observed at each anchor)
    3. Scenario comparison (ATR filter variants)
    4. Weekly P&L breakdown
    5. Saved CSV: backtest_silver_<config_hash>.csv

PARAMETERS to tune
    --trigger    USD distance from anchor for entry (default 0.15)
    --tp         USD take profit from entry (default 0.05)
    --sl         USD stop loss from entry (default 0.15)
    --lock       USD per lock-step (default 0.005)
    --lock-count Number of lock steps (default 9)
    --lot        Lot size (default 0.5)
    --contract   Contract size per lot in oz (default 5000)
    --symbol     MT5 symbol name (default XAGUSD)
    --anchor-hr  Broker hour for anchor (default 2)
    --start      Start date YYYY-MM-DD (default 2025-01-01)
    --end        End date YYYY-MM-DD (default 2025-12-31)
"""

import argparse
import csv
import hashlib
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import MetaTrader5 as mt5
import numpy as np


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    # Symbol & timing
    p.add_argument("--symbol", default="XAGUSD", help="MT5 symbol (default XAGUSD)")
    p.add_argument("--anchor-hr", type=int, default=2, help="Broker hour for anchor (default 2)")
    p.add_argument("--start", default="2025-01-01", help="Start date YYYY-MM-DD")
    p.add_argument("--end", default="2025-12-31", help="End date YYYY-MM-DD")

    # Strategy distances
    p.add_argument("--trigger", type=float, default=0.15,
                   help="USD distance anchor → entry (default 0.15)")
    p.add_argument("--tp", type=float, default=0.05,
                   help="USD distance entry → TP (default 0.05)")
    p.add_argument("--sl", type=float, default=0.15,
                   help="USD distance entry → SL (default 0.15)")
    p.add_argument("--lock", type=float, default=0.005,
                   help="USD per lock step (default 0.005)")
    p.add_argument("--lock-count", type=int, default=9,
                   help="Number of lock steps (default 9)")

    # Size
    p.add_argument("--lot", type=float, default=0.5, help="Lot size (default 0.5)")
    p.add_argument("--contract", type=float, default=5000,
                   help="Contract size in oz per 1.0 lot (default 5000)")

    # ATR
    p.add_argument("--atr-period", type=int, default=14)
    p.add_argument("--atr-tf", default="M15", choices=["M1","M5","M15","M30","H1"])

    return p.parse_args()


def main():
    args = parse_args()

    cfg_hash = hashlib.md5(
        f"{args.trigger}_{args.tp}_{args.sl}_{args.lock}".encode()
    ).hexdigest()[:6]

    print("=" * 80)
    print(f"SILVER ANCHOR BACKTEST  |  symbol={args.symbol}  config_hash={cfg_hash}")
    print("=" * 80)
    print(f"Range:    {args.start} → {args.end}")
    print(f"Anchor:   {args.anchor_hr:02d}:00 broker time")
    print(f"Trigger:  ${args.trigger}  TP: ${args.tp}  SL: ${args.sl}")
    print(f"Lock:     ${args.lock} × {args.lock_count} steps (max lock ${args.lock * (args.lock_count - 1):.3f})")
    print(f"Lot:      {args.lot}  Contract: {args.contract} oz")
    print(f"$ per $1 move: ${args.lot * args.contract:.2f}")
    print(f"TP value: ${args.tp * args.lot * args.contract:.2f}  "
          f"SL value: ${args.sl * args.lot * args.contract:.2f}")
    print("=" * 80)
    print()

    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}")
        sys.exit(1)

    # Verify symbol exists
    info = mt5.symbol_info(args.symbol)
    if info is None:
        print(f"❌ Symbol '{args.symbol}' not found on this broker.")
        print(f"   Try one of: {', '.join(get_silver_variants())}")
        mt5.shutdown()
        sys.exit(1)
    if not info.visible:
        mt5.symbol_select(args.symbol, True)
    print(f"✓ Symbol: {info.name}  digits={info.digits}  point={info.point}")
    print(f"  Spread: {info.spread} points  Trade contract: {info.trade_contract_size}")
    print()

    # Fetch data
    start_dt = datetime.fromisoformat(args.start)
    end_dt = datetime.fromisoformat(args.end + " 23:59")
    print(f"Fetching M1 bars...")
    m1 = mt5.copy_rates_range(args.symbol, mt5.TIMEFRAME_M1, start_dt, end_dt + timedelta(days=1))
    if m1 is None or len(m1) == 0:
        print(f"❌ M1 fetch failed: {mt5.last_error()}")
        mt5.shutdown()
        sys.exit(1)
    print(f"  → {len(m1):,} M1 bars")

    atr_tf = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
              "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
              "H1": mt5.TIMEFRAME_H1}[args.atr_tf]
    print(f"Fetching {args.atr_tf} bars for ATR...")
    atr_start = start_dt - timedelta(days=3)
    atr_bars = mt5.copy_rates_range(args.symbol, atr_tf, atr_start, end_dt + timedelta(days=1))
    if atr_bars is None or len(atr_bars) == 0:
        print(f"⚠️  {args.atr_tf} fetch failed: {mt5.last_error()}")
        atr_bars = np.array([])
    else:
        print(f"  → {len(atr_bars):,} {args.atr_tf} bars")
    print()

    # Simulate day-by-day
    trades = []
    current = start_dt
    while current <= end_dt:
        if current.weekday() < 5:  # Mon-Fri
            anchor_time = current.replace(
                hour=args.anchor_hr, minute=0, second=0, microsecond=0
            )
            t = simulate_day(m1, atr_bars, anchor_time, args)
            if t is not None:
                trades.append(t)
        current += timedelta(days=1)

    print(f"Simulated {len(trades)} trade days\n")

    # Save CSV
    csv_path = f"backtest_silver_{cfg_hash}.csv"
    save_csv(trades, csv_path)

    # Reports
    print_trade_table(trades, args)
    print_sl_analysis(trades)
    print_atr_distribution(trades)
    print_weekly_breakdown(trades)
    print_summary(trades, args)

    mt5.shutdown()


def get_silver_variants():
    return ["XAGUSD", "XAGUSD.r", "XAGUSD#", "XAGUSDm", "SILVER", "XAG/USD"]


def simulate_day(m1, atr_bars, anchor_time, args):
    """Simulate one trading day. Returns trade dict or None."""
    anchor_ts = int(anchor_time.timestamp())

    # Find anchor bar (M1 at anchor_time)
    anchor_bar = m1[m1['time'] == anchor_ts]
    if len(anchor_bar) == 0:
        return None
    anchor_price = float(anchor_bar[0]['close'])

    # Levels
    long_entry = round(anchor_price + args.trigger, args.contract and 4 or 2)
    long_sl = round(long_entry - args.sl, 4)
    long_tp = round(long_entry + args.tp, 4)
    short_entry = round(anchor_price - args.trigger, 4)
    short_sl = round(short_entry + args.sl, 4)
    short_tp = round(short_entry - args.tp, 4)

    # ATR
    atr = calc_atr(atr_bars, anchor_ts, args.atr_period)

    # Day window: anchor → +22h
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
        return {
            'date': anchor_time.date().isoformat(),
            'anchor': anchor_price,
            'atr': atr,
            'side': 'NONE',
            'outcome': 'NO_TRIGGER',
            'pnl_usd': 0.0,
            'entry': None, 'exit': None, 'max_fav': 0.0,
        }

    if long_idx == -1 or (short_idx != -1 and short_idx < long_idx):
        side, entry, sl, tp, fill_idx = 'SHORT', short_entry, short_sl, short_tp, short_idx
    else:
        side, entry, sl, tp, fill_idx = 'LONG', long_entry, long_sl, long_tp, long_idx

    post = day_bars[fill_idx:]
    exit_info = walk_position(post, side, entry, sl, tp, args)

    return {
        'date': anchor_time.date().isoformat(),
        'anchor': anchor_price,
        'atr': atr,
        'side': side,
        'entry': entry,
        'outcome': exit_info['reason'],
        'exit': exit_info['exit_price'],
        'pnl_usd': exit_info['pnl'],
        'max_fav': exit_info['max_fav'],
    }


def walk_position(bars, side, entry, sl, tp, args):
    """Walk bars from fill onward. Apply lock-step trail."""
    dollars_per_dollar = args.lot * args.contract  # USD value of $1 move

    current_step = 0
    current_sl = sl
    max_fav = 0.0

    for bar in bars:
        h = float(bar['high'])
        l = float(bar['low'])

        if side == 'LONG':
            fav = max(0.0, h - entry)
            if fav > max_fav:
                max_fav = fav

            if h >= tp:
                return {'reason': 'TP', 'exit_price': tp,
                        'pnl': round((tp - entry) * dollars_per_dollar, 2),
                        'max_fav': round(max_fav, 4)}

            new_step = min(int(fav / args.lock), args.lock_count)
            if new_step > current_step:
                current_step = new_step
                current_sl = round(entry + (current_step - 1) * args.lock, 4)

            if l <= current_sl:
                if current_step == 0:
                    reason = 'SL'
                elif current_step == 1:
                    reason = 'BE'
                else:
                    reason = 'Trail'
                pnl = round((current_sl - entry) * dollars_per_dollar, 2)
                return {'reason': reason, 'exit_price': current_sl, 'pnl': pnl,
                        'max_fav': round(max_fav, 4)}

        else:  # SHORT
            fav = max(0.0, entry - l)
            if fav > max_fav:
                max_fav = fav

            if l <= tp:
                return {'reason': 'TP', 'exit_price': tp,
                        'pnl': round((entry - tp) * dollars_per_dollar, 2),
                        'max_fav': round(max_fav, 4)}

            new_step = min(int(fav / args.lock), args.lock_count)
            if new_step > current_step:
                current_step = new_step
                current_sl = round(entry - (current_step - 1) * args.lock, 4)

            if h >= current_sl:
                if current_step == 0:
                    reason = 'SL'
                elif current_step == 1:
                    reason = 'BE'
                else:
                    reason = 'Trail'
                pnl = round((entry - current_sl) * dollars_per_dollar, 2)
                return {'reason': reason, 'exit_price': current_sl, 'pnl': pnl,
                        'max_fav': round(max_fav, 4)}

    last_close = float(bars[-1]['close'])
    pnl_per_unit = (last_close - entry) if side == 'LONG' else (entry - last_close)
    return {'reason': 'EOD', 'exit_price': last_close,
            'pnl': round(pnl_per_unit * dollars_per_dollar, 2),
            'max_fav': round(max_fav, 4)}


def calc_atr(bars, anchor_ts, period):
    if len(bars) == 0:
        return None
    prior = bars[bars['time'] < anchor_ts]
    if len(prior) < period + 1:
        return None
    last = prior[-(period + 1):]
    trs = []
    for i in range(1, period + 1):
        h = float(last[i]['high'])
        l = float(last[i]['low'])
        pc = float(last[i - 1]['close'])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return round(sum(trs) / period, 4)


def save_csv(trades, path):
    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'date', 'anchor', 'atr', 'side', 'entry', 'exit',
            'outcome', 'pnl_usd', 'max_fav',
        ])
        writer.writeheader()
        writer.writerows(trades)
    print(f"Saved per-trade CSV: {path}\n")


def print_trade_table(trades, args):
    print("=" * 110)
    print("PER-TRADE DETAIL")
    print("=" * 110)
    print(f"{'Date':<12} {'ATR':>7} {'Anchor':>9} {'Side':>6} {'Out':>6} "
          f"{'Entry':>10} {'Exit':>10} {'PnL':>10} {'MaxFav':>8}")
    print("-" * 110)
    for t in trades:
        atr_s = f"{t['atr']:.3f}" if t['atr'] is not None else "n/a"
        entry_s = f"{t['entry']:.4f}" if t.get('entry') else "—"
        exit_s = f"{t['exit']:.4f}" if t.get('exit') else "—"
        print(f"{t['date']:<12} {atr_s:>7} {t['anchor']:>9.4f} {t['side']:>6} "
              f"{t['outcome']:>6} {entry_s:>10} {exit_s:>10} "
              f"{t['pnl_usd']:>+10.2f} {t.get('max_fav', 0):>8.4f}")


def print_sl_analysis(trades):
    sls = [t for t in trades if t['outcome'] == 'SL']
    print()
    print("=" * 70)
    print(f"SL TRADES ANALYSIS — {len(sls)} losing trades")
    print("=" * 70)
    for t in sls:
        atr_s = f"{t['atr']:.3f}" if t['atr'] else "n/a"
        print(f"  {t['date']} | ATR {atr_s} | {t['side']:>5} "
              f"@ {t.get('entry', 0):.4f} → {t.get('exit', 0):.4f} "
              f"| PnL ${t['pnl_usd']:>+8.2f}")
    print()


def print_atr_distribution(trades):
    atrs = [t['atr'] for t in trades if t['atr'] is not None]
    if not atrs:
        return
    print("=" * 70)
    print("ATR DISTRIBUTION")
    print("=" * 70)
    print(f"  Min:    {min(atrs):.3f}")
    print(f"  Max:    {max(atrs):.3f}")
    print(f"  Mean:   {sum(atrs)/len(atrs):.3f}")
    print(f"  Median: {sorted(atrs)[len(atrs)//2]:.3f}")
    print()


def print_weekly_breakdown(trades):
    print("=" * 70)
    print("WEEKLY BREAKDOWN")
    print("=" * 70)
    weekly = defaultdict(list)
    for t in trades:
        d = datetime.fromisoformat(t['date'])
        iso = d.isocalendar()
        weekly[f"{iso[0]}-W{iso[1]:02d}"].append(t)
    print(f"{'Week':<12} {'Trades':>7} {'Wins':>6} {'SLs':>4} {'PnL':>10}")
    print("-" * 70)
    losing_weeks = 0
    for w in sorted(weekly.keys()):
        wt = weekly[w]
        wins = sum(1 for t in wt if t['pnl_usd'] > 0)
        sls = sum(1 for t in wt if t['outcome'] == 'SL')
        pnl = sum(t['pnl_usd'] for t in wt)
        flag = " ← LOSING WEEK" if pnl < 0 else ""
        if pnl < 0:
            losing_weeks += 1
        print(f"{w:<12} {len(wt):>7} {wins:>6} {sls:>4} ${pnl:>+9.2f}{flag}")
    print("-" * 70)
    print(f"Losing weeks: {losing_weeks} of {len(weekly)} ({losing_weeks/len(weekly)*100:.1f}%)")
    print()


def print_summary(trades, args):
    tr = [t for t in trades if t['side'] != 'NONE']
    total_pnl = sum(t['pnl_usd'] for t in tr)
    wins = sum(1 for t in tr if t['pnl_usd'] > 0)
    tps = sum(1 for t in tr if t['outcome'] == 'TP')
    trails = sum(1 for t in tr if t['outcome'] == 'Trail')
    bes = sum(1 for t in tr if t['outcome'] == 'BE')
    sls = sum(1 for t in tr if t['outcome'] == 'SL')
    no_triggers = sum(1 for t in trades if t['side'] == 'NONE')

    win_pnl = sum(t['pnl_usd'] for t in tr if t['pnl_usd'] > 0)
    loss_pnl = sum(t['pnl_usd'] for t in tr if t['pnl_usd'] < 0)
    avg_win = (win_pnl / wins) if wins else 0
    avg_loss = (loss_pnl / sls) if sls else 0

    days_simulated = len(trades)

    print("=" * 70)
    print(f"FINAL SUMMARY — {args.symbol}")
    print("=" * 70)
    print(f"Config:           trigger=${args.trigger}  tp=${args.tp}  sl=${args.sl}  lock=${args.lock}")
    print(f"Days simulated:   {days_simulated}")
    print(f"No-trigger days:  {no_triggers}")
    print(f"Trades:           {len(tr)}")
    print(f"  TPs:            {tps}")
    print(f"  Trails:         {trails}")
    print(f"  BEs:            {bes}")
    print(f"  SLs:            {sls}  ← losses")
    print(f"Win rate:         {wins/len(tr)*100:.1f}% ({wins}/{len(tr)})")
    print(f"Avg win:          ${avg_win:+.2f}")
    print(f"Avg loss:         ${avg_loss:+.2f}")
    if sls and avg_loss != 0:
        print(f"R:R per trade:    {abs(avg_win/avg_loss):.2f}:1")
    print(f"TOTAL P&L:        ${total_pnl:+.2f}")
    if days_simulated:
        annualized = total_pnl * (252 / days_simulated)
        print(f"Annualized:       ${annualized:+.2f}")
    print("=" * 70)

    # Tuning guidance
    if sls == 0:
        print("⚠️  Zero SLs — SL distance may be too wide. Consider tightening SL.")
    elif sls / len(tr) > 0.15:
        print("⚠️  SL rate > 15%. SL distance may be too tight, or trigger too aggressive.")
    if tps == 0:
        print("⚠️  Zero TPs — TP distance may be too far. Consider tightening TP.")
    if no_triggers / days_simulated > 0.3:
        print(f"⚠️  {no_triggers/days_simulated*100:.0f}% of days had no trigger — trigger distance may be too wide.")


if __name__ == "__main__":
    main()