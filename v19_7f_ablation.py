#!/usr/bin/env python3
"""V19.7f Large Ablation with Wilson Score CI.

Runs MC with enough seeds/cycles to get 300+ trades per zone.
Reports Wilson 95% CI for WR, plus EV, PF, DD, spread, fill estimates.
"""

import sys, os, json, random, math
sys.path.insert(0, '/mnt/c/Users/12035/father_daddy_capital')
import pm_engine_v19_7 as eng
import numpy as np

def wilson_ci(wins, total, z=1.96):
    """Wilson score interval for binomial proportion."""
    if total == 0:
        return 0, 0, 0
    p = wins / total
    denom = 1 + z**2 / total
    center = (p + z**2 / (2 * total)) / denom
    spread = z * math.sqrt((p * (1 - p) + z**2 / (4 * total)) / total) / denom
    return p, max(0, center - spread), min(1, center + spread)

def run_ablation():
    print(f"{'='*70}")
    print(f"V19.7f LARGE ABLATION — Wilson CI, 300+ trades per zone")
    print(f"{'='*70}")
    
    # Hard-mode MC
    eng.HARD_MODE = True
    eng.INITIAL_BANKROLL = 100
    
    SEEDS = 50
    CYCLES = 5000
    
    print(f"\nRunning {SEEDS} seeds × {CYCLES} cycles (hard-mode, ${eng.INITIAL_BANKROLL})...")
    results = eng.mc_backtest(seeds=SEEDS, cycles=CYCLES)
    
    # Collect all journal entries
    journals_dir = eng.JOURNAL_DIR
    all_entries = []
    for seed_r in results:
        journal_path = journals_dir / f"journal_seed_{seed_r['seed']}.json"
        if journal_path.exists():
            with open(journal_path) as f:
                jdata = json.load(f)
            for e in jdata.get("entries", []):
                entry = e.get("entry", e)
                all_entries.append(entry)
    
    print(f"\nTotal journal entries: {len(all_entries)}")
    
    # Classify entries by zone
    zones = {
        "RSI_20_28_UP": lambda e: e.get("rsi", 50) < 28 and e.get("side", e.get("direction", "")).lower() in ("up", "buy"),
        "RSI_28_35_UP": lambda e: 28 <= e.get("rsi", 50) < 35 and e.get("side", e.get("direction", "")).lower() in ("up", "buy"),
        "RSI_55_70_DOWN": lambda e: 55 <= e.get("rsi", 50) < 70 and e.get("side", e.get("direction", "")).lower() in ("down", "sell"),
        "RSI_70_82_DOWN": lambda e: 70 <= e.get("rsi", 50) < 82 and e.get("side", e.get("direction", "")).lower() in ("down", "sell"),
    }
    
    # Also by direction
    directions = {
        "UP": lambda e: e.get("side", e.get("direction", "")).lower() in ("up", "buy"),
        "DOWN": lambda e: e.get("side", e.get("direction", "")).lower() in ("down", "sell"),
    }
    
    # Also by asset
    assets = {
        "BTC": lambda e: e.get("asset", "BTC") == "BTC",
        "ETH": lambda e: e.get("asset", "") == "ETH",
        "SOL": lambda e: e.get("asset", "") == "SOL",
        "XRP": lambda e: e.get("asset", "") == "XRP",
    }
    
    # Also by timeframe
    timeframes = {
        "5m": lambda e: "5" in str(e.get("interval", e.get("timeframe", "5m"))),
        "15m": lambda e: "15" in str(e.get("interval", e.get("timeframe", ""))),
    }
    
    # Also by confidence
    conf_bins = {
        "conf_0.82_0.85": lambda e: 0.82 <= e.get("confidence", 0) < 0.85,
        "conf_0.85_0.90": lambda e: 0.85 <= e.get("confidence", 0) < 0.90,
        "conf_0.90_0.95": lambda e: 0.90 <= e.get("confidence", 0) < 0.95,
        "conf_0.95_1.00": lambda e: 0.95 <= e.get("confidence", 1),
    }
    
    def compute_stats(entries, label):
        if not entries:
            print(f"\n  {label}: 0 trades — INSUFFICIENT DATA")
            return
        
        wins = sum(1 for e in entries if e.get("pnl", e.get("net_pnl", 0)) > 0)
        losses = sum(1 for e in entries if e.get("pnl", e.get("net_pnl", 0)) <= 0)
        total = len(entries)
        wr = wins / total if total > 0 else 0
        
        pnl_list = [e.get("pnl", e.get("net_pnl", 0)) for e in entries]
        entry_prices = [e.get("entry_price", e.get("price", 0)) for e in entries]
        
        total_pnl = sum(pnl_list)
        avg_pnl = total_pnl / total if total > 0 else 0
        avg_entry = np.mean(entry_prices) if entry_prices else 0
        
        gross_win = sum(p for p in pnl_list if p > 0)
        gross_loss = abs(sum(p for p in pnl_list if p < 0))
        pf = gross_win / gross_loss if gross_loss > 0 else float('inf')
        
        # Bankroll DD (cumulative PnL peak-to-trough)
        cum = 0; peak = 0; max_dd = 0
        for p in pnl_list:
            cum += p
            peak = max(peak, cum)
            dd = (peak - cum) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)
        
        # Loss streak
        streak = 0; max_streak = 0
        for p in pnl_list:
            if p <= 0: streak += 1; max_streak = max(max_streak, streak)
            else: streak = 0
        
        # Average spread (entry_price + (1-entry_price) gap from fair)
        spreads = [abs(ep + (e.get("down_price", 1 - ep)) - 1.0) for e, ep in zip(entries, entry_prices) if ep > 0]
        avg_spread = np.mean(spreads) if spreads else 0
        
        # Wilson CI
        p_hat, ci_lo, ci_hi = wilson_ci(wins, total)
        
        print(f"\n  {label}:")
        print(f"    Trades: {total} | Wins: {wins} | Losses: {losses}")
        print(f"    WR: {wr:.1%} | Wilson 95% CI: [{ci_lo:.1%}, {ci_hi:.1%}]")
        print(f"    Avg entry: ${avg_entry:.3f} | Net EV: ${avg_pnl:.3f}/trade")
        print(f"    Total PnL: ${total_pnl:.2f} | PF: {pf:.2f}")
        print(f"    Bankroll DD: {max_dd:.1%} | Loss streak: {max_streak}")
        print(f"    Avg spread: ${avg_spread:.4f}")
        
        sufficient = "✅" if total >= 300 else "⚠️ <300 trades"
        print(f"    Sample size: {sufficient} ({total} trades)")
        
        return {
            "label": label, "trades": total, "wins": wins, "losses": losses,
            "wr": wr, "ci_lo": ci_lo, "ci_hi": ci_hi,
            "avg_entry": avg_entry, "net_ev": avg_pnl,
            "pnl": total_pnl, "pf": pf, "dd": max_dd,
            "loss_streak": max_streak, "avg_spread": avg_spread,
        }
    
    # Report by direction
    print(f"\n{'─'*70}")
    print("BY DIRECTION")
    print(f"{'─'*70}")
    for label, fn in directions.items():
        entries = [e for e in all_entries if fn(e)]
        compute_stats(entries, label)
    
    # Report by RSI zone
    print(f"\n{'─'*70}")
    print("BY RSI ZONE")
    print(f"{'─'*70}")
    for label, fn in zones.items():
        entries = [e for e in all_entries if fn(e)]
        compute_stats(entries, label)
    
    # Report by asset
    print(f"\n{'─'*70}")
    print("BY ASSET")
    print(f"{'─'*70}")
    for label, fn in assets.items():
        entries = [e for e in all_entries if fn(e)]
        compute_stats(entries, label)
    
    # Report by timeframe
    print(f"\n{'─'*70}")
    print("BY TIMEFRAME")
    print(f"{'─'*70}")
    for label, fn in timeframes.items():
        entries = [e for e in all_entries if fn(e)]
        compute_stats(entries, label)
    
    # Report by confidence
    print(f"\n{'─'*70}")
    print("BY CONFIDENCE")
    print(f"{'─'*70}")
    for label, fn in conf_bins.items():
        entries = [e for e in all_entries if fn(e)]
        compute_stats(entries, label)
    
    # Overall
    print(f"\n{'─'*70}")
    print("ALL TRADES")
    print(f"{'─'*70}")
    compute_stats(all_entries, "ALL")
    
    print(f"\n{'='*70}")
    print(f"ABLATION COMPLETE")
    print(f"{'='*70}")

if __name__ == "__main__":
    run_ablation()