"""
backtest_03_vs_1.py — uncapped 0.30 vs hybrid 0.3->1.0 on XAUUSD.

Two configs walked over the same fills, results printed as two adjacent
side-by-side tables for direct visual comparison.

    UNCAPPED 0.30      - continuous trail, SL rides $0.30 behind peak
    HYBRID 0.3 -> 1.0  - $0.30 trail until max_fav >= $3, then $1.00 trail
                         (ratchet-only; SL never moves backwards on switch)

USAGE
    python backtest_03_vs_1.py
    python backtest_03_vs_1.py --trigger 5 --tp 50 --sl 15 --lot 0.1
    python backtest_03_vs_1.py --switch-at 5.0
    python backtest_03_vs_1.py --start 2025-01-01 --end 2025-05-13

REALISM
    - M1 OHLC, pessimistic intrabar (pre-bar SL checked before trail advance)
    - Spread / slippage NOT modelled. The $0.30 trail in real life sits
      inside the spread; live numbers will be lower than printed here.
"""

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import MetaTrader5 as mt5
import numpy as np


GOLD_CONTRACT = 100.0
BE_TRIGGER = 0.30
COL_W = 48
COL_GAP = "   "
FULL_W = COL_W * 2 + len(COL_GAP)


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--anchor-hr", type=int, default=2)
    p.add_argument("--start", default="2025-01-01")
    p.add_argument("--end", default="2025-12-31")
    p.add_argument("--trigger", type=float, default=5.0)
    p.add_argument("--tp", type=float, default=50.0)
    p.add_argument("--sl", type=float, default=15.0)
    p.add_argument("--lot", type=float, default=0.1)
    p.add_argument("--switch-at", type=float, default=3.0)
    return p.parse_args()


def main():
    a = parse_args()
    usd_per_dollar = a.lot * GOLD_CONTRACT

    print("=" * FULL_W)
    print(f"UNCAPPED 0.30  vs  HYBRID 0.3 -> 1.0   |   {a.symbol}".center(FULL_W))
    print("=" * FULL_W)
    print(f"Range:     {a.start} -> {a.end}    Anchor: {a.anchor_hr:02d}:00 broker")
    print(f"Trigger:   ${a.trigger}    TP: ${a.tp}    SL: ${a.sl}    Lot: {a.lot}")
    print(f"Hybrid:    max_fav >= ${a.switch_at} -> trail widens 0.30 -> 1.00")
    print(f"$ per $1:  ${usd_per_dollar:.2f}    "
          f"TP=${a.tp*usd_per_dollar:.2f}   SL=-${a.sl*usd_per_dollar:.2f}")
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

    configs = [
        {"name": "uncapped_0.30", "label": "UNCAPPED 0.30",
         "subtitle": "continuous trail, gap $0.30",
         "mode": "fixed", "gap": 0.30},
        {"name": "hybrid_0.3->1.0", "label": "HYBRID 0.3 -> 1.0",
         "subtitle": f"$0.30 until +${a.switch_at:.1f}, then $1.00",
         "mode": "hybrid", "gap1": 0.30, "gap2": 1.00, "switch": a.switch_at},
    ]

    results = {c["name"]: [] for c in configs}

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
                for c in configs:
                    trade = walk_position(fill, c, a, usd_per_dollar)
                    results[c["name"]].append(trade)
        current += timedelta(days=1)

    print(f"Trading days simulated: {day_count}    Fills: {fill_count}\n")

    save_detail_csv(results, configs)
    print_two_tables(results, configs)
    print_verdict(results, configs)

    mt5.shutdown()


# ========== CORE BACKTEST LOGIC ==========

def find_fill(m1, anchor_time, a):
    """Capture anchor and find which side fills first."""
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
    """Walk one fill forward under one trail config."""
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

        # 1. Pre-bar SL check
        if side == "BUY":
            if l <= current_sl:
                return _close(base, side, entry, current_sl, max_fav, usd_per_dollar)
        else:
            if h >= current_sl:
                return _close(base, side, entry, current_sl, max_fav, usd_per_dollar)

        # 2. Update peak
        if side == "BUY":
            if h > max_fav_price:
                max_fav_price = h
        else:
            if l < max_fav_price:
                max_fav_price = l
        max_fav = (max_fav_price - entry) if side == "BUY" else (entry - max_fav_price)

        # 3. Advance trail (ratchet only)
        new_sl = _trail_sl(cfg, side, entry, max_fav_price, max_fav)
        if new_sl is not None:
            if side == "BUY" and new_sl > current_sl:
                current_sl = new_sl
            elif side == "SELL" and new_sl < current_sl:
                current_sl = new_sl

        # 4. TP check
        if side == "BUY":
            if h >= tp:
                return _close(base, side, entry, tp, max_fav, usd_per_dollar, forced="TP")
        else:
            if l <= tp:
                return _close(base, side, entry, tp, max_fav, usd_per_dollar, forced="TP")

    last = float(bars[-1]["close"])
    return _close(base, side, entry, last, max_fav, usd_per_dollar, forced="EOD")


def _trail_sl(cfg, side, entry, max_fav_price, max_fav):
    if max_fav < BE_TRIGGER:
        return None
    mode = cfg["mode"]
    if mode == "fixed":
        gap = cfg["gap"]
    elif mode == "hybrid":
        gap = cfg["gap1"] if max_fav < cfg["switch"] else cfg["gap2"]
    else:
        return None
    if side == "BUY":
        return round(max(entry, max_fav_price - gap), 2)
    return round(min(entry, max_fav_price + gap), 2)


def _close(base, side, entry, exit_price, max_fav, usd_per_dollar, forced=None):
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
        if pnl_dist < -1e-6:
            outcome = "SL"
        elif abs(pnl_dist) <= 1e-6:
            outcome = "BE"
        else:
            outcome = "Trail"
    return {**base, "outcome": outcome, "entry": entry, "exit": exit_price,
            "pnl": pnl, "max_fav": round(max_fav, 2)}


def _stats(trades):
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


# ========== SIDE-BY-SIDE TABLE FORMATTING ==========

def _line(label, value_str):
    """Build a label/value line exactly COL_W chars wide."""
    text = f"  {label}"
    spaces = COL_W - len(text) - len(value_str)
    if spaces < 1:
        spaces = 1
    return text + " " * spaces + value_str


def _center(text):
    return text.center(COL_W)


def _bar(ch="="):
    return ch * COL_W


def _summary_block(cfg, s):
    L = []
    L.append(_bar("="))
    L.append(_center(cfg["label"]))
    L.append(_center(f"({cfg['subtitle']})"))
    L.append(_bar("="))
    L.append(_line("Trades", f"{s['n']}"))
    L.append(_line("Win Rate", f"{s['win_rate']:.1f}%"))
    L.append(" " * COL_W)
    L.append(_line("TP Hits", f"{s['tps']}"))
    L.append(_line("Trail Wins", f"{s['trails']}"))
    L.append(_line("BE Exits", f"{s['bes']}"))
    L.append(_line("SL Hits", f"{s['sls']}"))
    L.append(_line("EOD Exits", f"{s['eods']}"))
    L.append(" " * COL_W)
    L.append(_line("Avg Win", f"${s['avg_win']:+.2f}"))
    L.append(_line("Avg Loss", f"${s['avg_loss']:+.2f}"))
    L.append(_line("Biggest Win", f"${s['biggest_win']:+.2f}"))
    L.append(_line("Max DrawDown", f"${s['max_dd']:.2f}"))
    L.append(_bar("-"))
    L.append(_line("TOTAL P&L", f"${s['total']:+.2f}"))
    L.append(_bar("="))
    return L


def _monthly_block(label, trades):
    L = [_center(f"-- {label} MONTHLY --"), " " * COL_W]
    monthly = defaultdict(list)
    for t in trades:
        d = datetime.fromisoformat(t["date"])
        monthly[f"{d.year}-{d.month:02d}"].append(t)
    for m in sorted(monthly):
        mt = monthly[m]
        pnl = sum(t["pnl"] for t in mt)
        sls = sum(1 for t in mt if t["outcome"] == "SL")
        L.append(f"  {m}   n={len(mt):>2}   SL={sls}   ${pnl:>+9.2f}".ljust(COL_W))
    return L


def _topwins_block(label, trades, n=5):
    L = [_center(f"-- {label} TOP {n} WINS --"), " " * COL_W]
    top = sorted(trades, key=lambda t: t["pnl"], reverse=True)[:n]
    for t in top:
        L.append(f"  {t['date']} {t['side']:>4}  mf=${t['max_fav']:>5.2f}  "
                 f"${t['pnl']:>+8.2f}".ljust(COL_W))
    return L


def _sl_block(label, trades):
    sls = [t for t in trades if t["outcome"] == "SL"]
    L = [_center(f"-- {label} SL TRADES ({len(sls)}) --"), " " * COL_W]
    if not sls:
        L.append(_center("(none)"))
        return L
    for t in sls:
        L.append(f"  {t['date']} {t['side']:>4}  entry {t['entry']:.2f}  "
                 f"${t['pnl']:>+8.2f}".ljust(COL_W))
    return L


def _print_pair(left, right):
    """Print two column blocks side by side, padded to equal length."""
    n = max(len(left), len(right))
    while len(left) < n:
        left.append(" " * COL_W)
    while len(right) < n:
        right.append(" " * COL_W)
    for l, r in zip(left, right):
        print(l.ljust(COL_W)[:COL_W] + COL_GAP + r.ljust(COL_W)[:COL_W])


def print_two_tables(results, configs):
    s_left = _stats(results[configs[0]["name"]])
    s_right = _stats(results[configs[1]["name"]])

    _print_pair(_summary_block(configs[0], s_left),
                _summary_block(configs[1], s_right))
    print()

    tl = [t for t in results[configs[0]["name"]] if t["side"] != "NONE"]
    tr = [t for t in results[configs[1]["name"]] if t["side"] != "NONE"]

    _print_pair(_monthly_block(configs[0]["label"], tl),
                _monthly_block(configs[1]["label"], tr))
    print()

    _print_pair(_topwins_block(configs[0]["label"], tl),
                _topwins_block(configs[1]["label"], tr))
    print()

    _print_pair(_sl_block(configs[0]["label"], tl),
                _sl_block(configs[1]["label"], tr))
    print()


def print_verdict(results, configs):
    s_left = _stats(results[configs[0]["name"]])
    s_right = _stats(results[configs[1]["name"]])
    diff = s_right["total"] - s_left["total"]

    # How often did hybrid actually diverge from uncapped (i.e. how often
    # did the phase-2 ($1.00) trail get to matter)?
    lt = {t["date"]: t for t in results[configs[0]["name"]] if t["side"] != "NONE"}
    rt = {t["date"]: t for t in results[configs[1]["name"]] if t["side"] != "NONE"}
    divergent = 0
    div_pnl_diff = 0.0
    for d, t in lt.items():
        if d in rt and abs(t["pnl"] - rt[d]["pnl"]) > 0.01:
            divergent += 1
            div_pnl_diff += rt[d]["pnl"] - t["pnl"]

    print("=" * FULL_W)
    print("VERDICT".center(FULL_W))
    print("=" * FULL_W)
    print(f"  {configs[0]['label']:<22}  TOTAL  ${s_left['total']:>+10.2f}")
    print(f"  {configs[1]['label']:<22}  TOTAL  ${s_right['total']:>+10.2f}")
    print(f"  Difference (hybrid - uncapped):    ${diff:>+10.2f}")
    if abs(diff) < 0.01:
        print("  Winner: TIE")
    else:
        winner = configs[1]['label'] if diff > 0 else configs[0]['label']
        print(f"  Winner: {winner}  by ${abs(diff):.2f}")
    print()
    print(f"  Trades where hybrid behaved differently: {divergent} / {len(lt)} "
          f"({divergent/max(len(lt),1)*100:.1f}%)")
    print(f"  P&L delta on those divergent trades:  ${div_pnl_diff:>+10.2f}")
    print(f"  -> on {len(lt)-divergent} trades the two configs were identical")
    print("=" * FULL_W)
    print()


def save_detail_csv(results, configs):
    path = "backtest_03_vs_1_detail.csv"
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