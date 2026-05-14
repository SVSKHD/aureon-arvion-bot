"""
backtest_trailing.py — Compare trailing-stop mechanisms on XAUUSD.

Runs the anchor-breakout strategy over historical M1 data and walks every
filled trade forward under SEVERAL trailing-stop configurations at once, so
you can see — side by side — which mechanism and which gap size makes the
most money with acceptable drawdown.

Configurations compared in one pass:
    - capped          : original discrete 9-step lock (max +$2.70 favorable)
    - uncapped gap=X  : continuous trail, SL rides X behind peak, no cap
                        (X swept over several values)

USAGE
    # Defaults: $5 trigger, $50 TP, $15 SL, 0.1 lot, full 2025
    python backtest_trailing.py

    # Match your current live setup
    python backtest_trailing.py --trigger 5 --tp 3 --sl 15 --lot 0.1

    # Wide TP so the trail mechanism dominates the result
    python backtest_trailing.py --trigger 5 --tp 50 --sl 15 --lot 0.1

    # No effective TP — pure trail
    python backtest_trailing.py --tp 9999

    # Custom date range / symbol
    python backtest_trailing.py --start 2025-01-01 --end 2025-05-13 --symbol XAUUSD

WHAT TO LOOK FOR
    - Highest TOTAL P&L with a MAX DRAWDOWN you can stomach
    - A gap that is not so tight it gets noise-stopped (look at avg win
      and the TP/Trail/SL split)
    - Compare every uncapped row against the 'capped' baseline row

NOTE ON REALISM
    - M1 OHLC only; intrabar tick order unknown.
    - Pessimistic convention: each bar is checked for a stop-out against
      the PRE-BAR SL first, before the trail is allowed to advance.
    - Spread/slippage NOT modelled. Real results will be lower. Treat the
      comparison between configs as more reliable than the absolute P&L.
"""

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import MetaTrader5 as mt5
import numpy as np


GOLD_CONTRACT = 100.0   # oz per 1.0 lot on XAUUSD
BE_TRIGGER = 0.30       # favorable $ before breakeven is armed (both modes)
LOCK_STEP = 0.30        # discrete step size for the capped mode
LOCK_STEPS = 9          # discrete step count for the capped mode

# Trail gaps swept for the uncapped mode
GAP_SWEEP = [0.30, 0.50, 0.75, 1.00, 1.50, 2.00, 3.00]


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--anchor-hr", type=int, default=2, help="Broker hour for anchor")
    p.add_argument("--start", default="2025-01-01")
    p.add_argument("--end", default="2025-12-31")
    p.add_argument("--trigger", type=float, default=5.0, help="Anchor->entry distance $")
    p.add_argument("--tp", type=float, default=50.0, help="Entry->TP distance $")
    p.add_argument("--sl", type=float, default=15.0, help="Entry->SL distance $")
    p.add_argument("--lot", type=float, default=0.1, help="Lot size")
    return p.parse_args()


def main():
    a = parse_args()
    usd_per_dollar = a.lot * GOLD_CONTRACT   # account $ per $1 price move

    print("=" * 78)
    print(f"TRAILING-STOP BACKTEST  |  {a.symbol}")
    print("=" * 78)
    print(f"Range:    {a.start} -> {a.end}")
    print(f"Anchor:   {a.anchor_hr:02d}:00 broker")
    print(f"Trigger:  ${a.trigger}   TP: ${a.tp}   SL: ${a.sl}   Lot: {a.lot}")
    print(f"$ per $1 move: ${usd_per_dollar:.2f}   "
          f"TP=${a.tp*usd_per_dollar:.2f}  SL=-${a.sl*usd_per_dollar:.2f}")
    print("=" * 78)
    print()

    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}")
        sys.exit(1)

    info = mt5.symbol_info(a.symbol)
    if info is None:
        print(f"Symbol '{a.symbol}' not found.")
        mt5.shutdown(); sys.exit(1)
    if not info.visible:
        mt5.symbol_select(a.symbol, True)

    start_dt = datetime.fromisoformat(a.start)
    end_dt = datetime.fromisoformat(a.end + " 23:59")
    print("Fetching M1 bars...")
    m1 = mt5.copy_rates_range(a.symbol, mt5.TIMEFRAME_M1, start_dt,
                              end_dt + timedelta(days=1))
    if m1 is None or len(m1) == 0:
        print(f"M1 fetch failed: {mt5.last_error()}")
        mt5.shutdown(); sys.exit(1)
    print(f"  -> {len(m1):,} bars\n")

    # Build the list of trail configs to compare
    configs = [{"name": "capped", "mode": "capped"}]
    for g in GAP_SWEEP:
        configs.append({"name": f"uncapped {g:.2f}", "mode": "uncapped", "gap": g})

    # results[config_name] = list of trade dicts
    results = {c["name"]: [] for c in configs}

    # Walk every trading day
    current = start_dt
    day_count = 0
    fill_count = 0
    while current <= end_dt:
        if current.weekday() < 5:
            anchor_time = current.replace(hour=a.anchor_hr, minute=0,
                                          second=0, microsecond=0)
            fill = find_fill(m1, anchor_time, a)
            if fill is not None:
                day_count += 1
                if fill["side"] != "NONE":
                    fill_count += 1
                # Walk the same fill under every trail config
                for c in configs:
                    trade = walk_position(fill, c, a, usd_per_dollar)
                    results[c["name"]].append(trade)
                else:
                    pass
            else:
                day_count += 0
        current += timedelta(days=1)

    print(f"Trading days simulated: {day_count}   Fills: {fill_count}\n")

    # Save per-trade CSV for the capped baseline + best uncapped
    save_detail_csv(results, configs)

    # Comparison table
    print_comparison(results, configs, usd_per_dollar)

    # Detail on the winning config
    print_winner_detail(results, configs, usd_per_dollar)

    mt5.shutdown()


def find_fill(m1, anchor_time, a):
    """Capture anchor, find which side fills first. Returns fill dict or None."""
    anchor_ts = int(anchor_time.timestamp())
    anchor_bar = m1[m1["time"] == anchor_ts]
    if len(anchor_bar) == 0:
        return None
    anchor = float(anchor_bar[0]["close"])

    long_entry = round(anchor + a.trigger, 2)
    short_entry = round(anchor - a.trigger, 2)

    end_ts = anchor_ts + 22 * 3600
    day = m1[(m1["time"] >= anchor_ts) & (m1["time"] <= end_ts)]
    if len(day) == 0:
        return None

    long_hit = day["high"] >= long_entry
    short_hit = day["low"] <= short_entry
    li = int(np.argmax(long_hit)) if long_hit.any() else -1
    si = int(np.argmax(short_hit)) if short_hit.any() else -1

    if li == -1 and si == -1:
        return {"date": anchor_time.date().isoformat(), "anchor": anchor,
                "side": "NONE", "bars": None}

    if li == -1 or (si != -1 and si < li):
        side, entry, idx = "SELL", short_entry, si
        initial_sl = round(entry + a.sl, 2)
        tp = round(entry - a.tp, 2)
    else:
        side, entry, idx = "BUY", long_entry, li
        initial_sl = round(entry - a.sl, 2)
        tp = round(entry + a.tp, 2)

    return {
        "date": anchor_time.date().isoformat(),
        "anchor": anchor, "side": side, "entry": entry,
        "initial_sl": initial_sl, "tp": tp,
        "bars": day[idx:],
    }


def walk_position(fill, cfg, a, usd_per_dollar):
    """Walk one fill forward under one trail config. Returns trade result dict."""
    base = {"date": fill["date"], "side": fill["side"], "config": cfg["name"]}
    if fill["side"] == "NONE":
        return {**base, "outcome": "NO_TRIGGER", "pnl": 0.0, "max_fav": 0.0}

    side = fill["side"]
    entry = fill["entry"]
    tp = fill["tp"]
    bars = fill["bars"]

    current_sl = fill["initial_sl"]
    max_fav_price = entry
    max_fav = 0.0

    for bar in bars:
        h = float(bar["high"])
        l = float(bar["low"])

        # 1. Stop-out check against the PRE-BAR SL (pessimistic).
        if side == "BUY":
            if l <= current_sl:
                return _close(base, side, entry, current_sl, max_fav,
                              current_sl, usd_per_dollar)
        else:
            if h >= current_sl:
                return _close(base, side, entry, current_sl, max_fav,
                              current_sl, usd_per_dollar)

        # 2. Update peak favorable price.
        if side == "BUY":
            if h > max_fav_price:
                max_fav_price = h
        else:
            if l < max_fav_price:
                max_fav_price = l
        max_fav = (max_fav_price - entry) if side == "BUY" else (entry - max_fav_price)

        # 3. Advance the trail (ratchet only).
        new_sl = _trail_sl(cfg, side, entry, max_fav_price, max_fav)
        if new_sl is not None:
            if side == "BUY" and new_sl > current_sl:
                current_sl = new_sl
            elif side == "SELL" and new_sl < current_sl:
                current_sl = new_sl

        # 4. TP check against this bar's favorable extreme.
        if side == "BUY":
            if h >= tp:
                return _close(base, side, entry, tp, max_fav, tp, usd_per_dollar,
                              forced="TP")
        else:
            if l <= tp:
                return _close(base, side, entry, tp, max_fav, tp, usd_per_dollar,
                              forced="TP")

    # End of window — close at last bar's close.
    last = float(bars[-1]["close"])
    return _close(base, side, entry, last, max_fav, current_sl, usd_per_dollar,
                  forced="EOD")


def _trail_sl(cfg, side, entry, max_fav_price, max_fav):
    """Candidate SL for the given trail config, or None if not armed yet."""
    if max_fav < BE_TRIGGER:
        return None
    if cfg["mode"] == "uncapped":
        gap = cfg["gap"]
        if side == "BUY":
            return round(max(entry, max_fav_price - gap), 2)
        return round(min(entry, max_fav_price + gap), 2)
    # capped
    step = int(max_fav / LOCK_STEP)
    if step < 1:
        return None
    step = min(step, LOCK_STEPS)
    locked = (step - 1) * LOCK_STEP
    if side == "BUY":
        return round(entry + locked, 2)
    return round(entry - locked, 2)


def _close(base, side, entry, exit_price, max_fav, ref_sl, usd_per_dollar,
           forced=None):
    """Build a closed-trade dict, classifying the outcome."""
    if side == "BUY":
        pnl_dist = exit_price - entry
    else:
        pnl_dist = entry - exit_price
    pnl = round(pnl_dist * usd_per_dollar, 2)

    if forced == "TP":
        outcome = "TP"
    elif forced == "EOD":
        outcome = "EOD"
    else:
        # closed by SL touch — classify by how much was locked
        if pnl_dist < -1e-6:
            outcome = "SL"
        elif abs(pnl_dist) <= 1e-6:
            outcome = "BE"
        else:
            outcome = "Trail"
    return {**base, "outcome": outcome, "entry": entry, "exit": exit_price,
            "pnl": pnl, "max_fav": round(max_fav, 2)}


def _stats(trades, usd_per_dollar):
    """Aggregate stats for one config's trade list."""
    fills = [t for t in trades if t["side"] != "NONE"]
    if not fills:
        return None
    total = sum(t["pnl"] for t in fills)
    wins = [t for t in fills if t["pnl"] > 0]
    losses = [t for t in fills if t["pnl"] < 0]
    tps = sum(1 for t in fills if t["outcome"] == "TP")
    trails = sum(1 for t in fills if t["outcome"] == "Trail")
    bes = sum(1 for t in fills if t["outcome"] == "BE")
    sls = sum(1 for t in fills if t["outcome"] == "SL")
    eods = sum(1 for t in fills if t["outcome"] == "EOD")

    # running max drawdown on cumulative PnL
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in fills:
        cum += t["pnl"]
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    return {
        "n": len(fills), "total": total,
        "win_rate": len(wins) / len(fills) * 100,
        "tps": tps, "trails": trails, "bes": bes, "sls": sls, "eods": eods,
        "avg_win": (sum(t["pnl"] for t in wins) / len(wins)) if wins else 0.0,
        "avg_loss": (sum(t["pnl"] for t in losses) / len(losses)) if losses else 0.0,
        "biggest_win": max((t["pnl"] for t in fills), default=0.0),
        "max_dd": max_dd,
    }


def print_comparison(results, configs, usd_per_dollar):
    print("=" * 100)
    print("COMPARISON  —  all configs, same trades, same entries")
    print("=" * 100)
    hdr = (f"{'Config':<16}{'Trades':>7}{'Win%':>7}{'TP':>5}{'Trail':>7}"
           f"{'BE':>5}{'SL':>5}{'AvgWin':>9}{'AvgLoss':>9}{'BigWin':>9}"
           f"{'MaxDD':>9}{'TOTAL':>11}")
    print(hdr)
    print("-" * 100)

    rows = []
    for c in configs:
        s = _stats(results[c["name"]], usd_per_dollar)
        if s is None:
            continue
        rows.append((c["name"], s))
        print(f"{c['name']:<16}{s['n']:>7}{s['win_rate']:>6.1f}%"
              f"{s['tps']:>5}{s['trails']:>7}{s['bes']:>5}{s['sls']:>5}"
              f"{s['avg_win']:>+9.2f}{s['avg_loss']:>+9.2f}{s['biggest_win']:>+9.2f}"
              f"{s['max_dd']:>9.2f}{s['total']:>+11.2f}")
    print("-" * 100)

    # Highlight best by total, and best by total/maxDD ratio
    best_total = max(rows, key=lambda r: r[1]["total"])
    best_ratio = max(rows, key=lambda r: (r[1]["total"] / r[1]["max_dd"])
                     if r[1]["max_dd"] > 0 else r[1]["total"])
    print(f"\nHighest TOTAL P&L:        {best_total[0]:<16} "
          f"${best_total[1]['total']:+.2f}  (MaxDD ${best_total[1]['max_dd']:.2f})")
    print(f"Best P&L / MaxDD ratio:   {best_ratio[0]:<16} "
          f"${best_ratio[1]['total']:+.2f}  (MaxDD ${best_ratio[1]['max_dd']:.2f}, "
          f"ratio {best_ratio[1]['total']/max(best_ratio[1]['max_dd'],1e-9):.1f})")

    cap = next((r for r in rows if r[0] == "capped"), None)
    if cap:
        print(f"\nvs capped baseline (${cap[1]['total']:+.2f}):")
        for name, s in rows:
            if name == "capped":
                continue
            diff = s["total"] - cap[1]["total"]
            sign = "+" if diff >= 0 else ""
            print(f"  {name:<16} {sign}{diff:>+10.2f}   "
                  f"({'BETTER' if diff > 0 else 'worse'})")
    print()


def print_winner_detail(results, configs, usd_per_dollar):
    """Monthly + weekly breakdown for the highest-P&L config."""
    rows = [(c["name"], _stats(results[c["name"]], usd_per_dollar))
            for c in configs]
    rows = [r for r in rows if r[1] is not None]
    winner = max(rows, key=lambda r: r[1]["total"])[0]
    trades = [t for t in results[winner] if t["side"] != "NONE"]

    print("=" * 78)
    print(f"WINNER DETAIL  —  {winner}")
    print("=" * 78)

    # Monthly
    monthly = defaultdict(list)
    for t in trades:
        d = datetime.fromisoformat(t["date"])
        monthly[f"{d.year}-{d.month:02d}"].append(t)
    print("Monthly:")
    for m in sorted(monthly):
        mt = monthly[m]
        pnl = sum(t["pnl"] for t in mt)
        sls = sum(1 for t in mt if t["outcome"] == "SL")
        print(f"  {m}  trades {len(mt):>3}  SLs {sls}  ${pnl:>+10.2f}")

    # Weekly losing-week count
    weekly = defaultdict(float)
    for t in trades:
        d = datetime.fromisoformat(t["date"])
        iso = d.isocalendar()
        weekly[f"{iso[0]}-W{iso[1]:02d}"] += t["pnl"]
    losing = sum(1 for v in weekly.values() if v < 0)
    print(f"\nWeeks: {len(weekly)}   Losing weeks: {losing} "
          f"({losing/len(weekly)*100:.1f}%)")

    # Biggest wins
    top = sorted(trades, key=lambda t: t["pnl"], reverse=True)[:5]
    print("\nTop 5 wins:")
    for t in top:
        print(f"  {t['date']}  {t['side']:>4}  {t['outcome']:>6}  "
              f"max_fav ${t['max_fav']:>6.2f}  pnl ${t['pnl']:>+9.2f}")

    # SL list
    sls = [t for t in trades if t["outcome"] == "SL"]
    print(f"\nSL trades ({len(sls)}):")
    for t in sls:
        print(f"  {t['date']}  {t['side']:>4}  entry {t['entry']:.2f}  "
              f"pnl ${t['pnl']:>+9.2f}")
    print()


def save_detail_csv(results, configs):
    """Save per-trade rows for the capped baseline and every uncapped config."""
    path = "backtest_trailing_detail.csv"
    fieldnames = ["config", "date", "side", "outcome", "entry", "exit",
                  "pnl", "max_fav"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for c in configs:
            for t in results[c["name"]]:
                if t["side"] != "NONE":
                    w.writerow(t)
    print(f"Saved per-trade detail: {path}\n")


if __name__ == "__main__":
    main()