#!/usr/bin/env python3
"""V19.7f Large Ablation with Wilson Score CI.

Uses MC journal entries (nested format) for per-zone, per-direction, per-asset stats.
Reports Wilson 95% CI for WR, EV, PF, DD.
"""

import sys, os, json, math
sys.path.insert(0, '/mnt/c/Users/12035/father_daddy_capital')
import pm_engine_v19_7 as eng
import numpy as np

def wilson_ci(wins, total, z=1.96):
    if total == 0:
        return 0, 0, 0
    p = wins / total
    denom = 1 + z**2 / total
    center = (p + z**2 / (2 * total)) / denom
    spread = z * math.sqrt((p * (1 - p) + z**2 / (4 * total)) / total) / denom
    return p, max(0, center - spread), min(1, center + spread)

def run_ablation():
    print(f"{'='*70}")
    print(f"V19.7f LARGE ABLATION — 50s × 5000c, Wilson CI")
    print(f"{'='*70}")
    
    eng.HARD_MODE = True
    eng.INITIAL_BANKROLL = 100
    
    SEEDS = 50
    CYCLES = 5000
    
    print(f"\nRunning {SEEDS} seeds × {CYCLES} cycles (hard-mode, ${eng.INITIAL_BANKROLL})...")
    results = eng.mc_backtest(seeds=SEEDS, cycles=CYCLES)
    
    # Collect all journal entries
    journals = sorted(eng.JOURNAL_DIR.glob("journal_*.json"))
    all_entries = []
    for jp in journals:
        try:
            with open(jp) as f:
                jdata = json.load(f)
            for e in jdata.get("entries", []):
                all_entries.append(e)
        except: pass
    
    print(f"\nTotal journal entries: {len(all_entries)}")
    
    if not all_entries:
        print("No entries found. Using MC results for aggregate stats only.")
        # Use MC seed results for aggregate
        all_wr = [r['win_rate'] for r in results]
        all_pnl = [r['pnl'] for r in results]
        all_dd = [r['drawdown'] for r in results]
        all_pf = [r['profit_factor'] for r in results if r['profit_factor'] > 0]
        all_trades = [r['trades'] for r in results]
        print(f"\n  Aggregate (from MC seeds):")
        print(f"    Avg WR: {np.mean(all_wr):.1f}% | Avg PnL: ${np.mean(all_pnl):.2f}")
        print(f"    Avg DD: {np.mean(all_dd):.1f}% | Avg PF: {np.mean(all_pf):.2f}")
        print(f"    Avg Trades: {np.mean(all_trades):.1f}")
        return
    
    # Parse entries — nested format: entry.side, entry.rsi, exit.pnl, exit.won
    parsed = []
    for e in all_entries:
        ent = e.get("entry", e)
        ext = e.get("exit", {})
        flt = e.get("filters", {})
        parsed.append({
            "side": ent.get("side", "").lower(),
            "direction": ent.get("direction", "").lower(),
            "rsi": ent.get("rsi", 50),
            "confidence": ent.get("confidence", 0),
            "contract_price": ent.get("contract_price", 0),
            "bet": ent.get("bet", 0),
            "entry_price": ent.get("entry_price", 0),
            "regime": ent.get("regime", ""),
            "win_prob_model": ent.get("win_prob_model", 0),
            "pnl": ext.get("pnl", 0),
            "won": ext.get("won", False),
            "exit_type": ext.get("exit_type", ""),
            "asset": ent.get("asset", "BTC"),
            "cycle": e.get("cycle", 0),
            "seed": e.get("seed", 0),
        })
    
    # Classify
    classifiers = {
        # By direction
        "UP": lambda p: p["side"] in ("up", "buy") or p["direction"] == "up",
        "DOWN": lambda p: p["side"] in ("down", "sell") or p["direction"] == "down",
        # By RSI zone
        "RSI_20_28_UP": lambda p: p["rsi"] < 28 and (p["side"] in ("up","buy") or p["direction"]=="up"),
        "RSI_28_35_UP": lambda p: 28 <= p["rsi"] < 35 and (p["side"] in ("up","buy") or p["direction"]=="up"),
        "RSI_55_70_DOWN": lambda p: 55 <= p["rsi"] < 70 and (p["side"] in ("down","sell") or p["direction"]=="down"),
        "RSI_70_82_DOWN": lambda p: 70 <= p["rsi"] < 82 and (p["side"] in ("down","sell") or p["direction"]=="down"),
        # By asset
        "BTC": lambda p: p["asset"] == "BTC",
        "ETH": lambda p: p["asset"] == "ETH",
        "SOL": lambda p: p["asset"] == "SOL",
        "XRP": lambda p: p["asset"] == "XRP",
        # By confidence
        "conf_0.82_0.85": lambda p: 0.82 <= p["confidence"] < 0.85,
        "conf_0.85_0.90": lambda p: 0.85 <= p["confidence"] < 0.90,
        "conf_0.90_0.95": lambda p: 0.90 <= p["confidence"] < 0.95,
        "conf_0.95_1.00": lambda p: p["confidence"] >= 0.95,
    }
    
    def compute_stats(entries, label):
        if not entries:
            print(f"\n  {label}: 0 trades — INSUFFICIENT DATA ⚠️")
            return None
        
        wins = sum(1 for e in entries if e["won"])
        total = len(entries)
        wr = wins / total
        
        pnl_list = [e["pnl"] for e in entries]
        entry_prices = [e["contract_price"] for e in entries]
        
        total_pnl = sum(pnl_list)
        avg_pnl = total_pnl / total
        avg_entry = np.mean(entry_prices)
        
        gross_win = sum(p for p in pnl_list if p > 0)
        gross_loss = abs(sum(p for p in pnl_list if p < 0))
        pf = gross_win / gross_loss if gross_loss > 0 else float('inf')
        
        # Bankroll DD (cumulative PnL)
        cum = 0; peak = 0; max_dd = 0
        for p in pnl_list:
            cum += p; peak = max(peak, cum)
            dd = (peak - cum) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)
        
        # Loss streak
        streak = 0; max_streak = 0
        for p in pnl_list:
            if p <= 0: streak += 1; max_streak = max(max_streak, streak)
            else: streak = 0
        
        # Wilson CI
        p_hat, ci_lo, ci_hi = wilson_ci(wins, total)
        
        sufficient = "✅" if total >= 300 else f"⚠️ <300 trades"
        
        print(f"\n  {label}:")
        print(f"    Trades: {total} | Wins: {wins} | Losses: {total - wins}")
        print(f"    WR: {wr:.1%} | Wilson 95% CI: [{ci_lo:.1%}, {ci_hi:.1%}]")
        print(f"    Avg entry: ${avg_entry:.3f} | Net EV: ${avg_pnl:+.4f}/trade")
        print(f"    Gross win: ${gross_win:.2f} | Gross loss: ${gross_loss:.2f} | PF: {pf:.2f}")
        print(f"    Bankroll DD: {max_dd:.1%} | Loss streak: {max_streak}")
        print(f"    Sample: {sufficient} ({total} trades)")
        
        return {
            "label": label, "trades": total, "wins": wins, "losses": total - wins,
            "wr": wr, "ci_lo": ci_lo, "ci_hi": ci_hi,
            "avg_entry": avg_entry, "net_ev": avg_pnl,
            "pnl": total_pnl, "pf": pf, "dd": max_dd,
            "loss_streak": max_streak, "sufficient": total >= 300,
        }
    
    sections = [
        ("BY DIRECTION", ["UP", "DOWN"]),
        ("BY RSI ZONE", ["RSI_20_28_UP", "RSI_28_35_UP", "RSI_55_70_DOWN", "RSI_70_82_DOWN"]),
        ("BY ASSET", ["BTC", "ETH", "SOL", "XRP"]),
        ("BY CONFIDENCE", ["conf_0.82_0.85", "conf_0.85_0.90", "conf_0.90_0.95", "conf_0.95_1.00"]),
    ]
    
    report = {}
    for title, labels in sections:
        print(f"\n{'─'*70}")
        print(f"{title}")
        print(f"{'─'*70}")
        for label in labels:
            fn = classifiers[label]
            matching = [e for e in parsed if fn(e)]
            report[label] = compute_stats(matching, label)
    
    # ALL
    print(f"\n{'─'*70}")
    print(f"ALL TRADES")
    print(f"{'─'*70}")
    report["ALL"] = compute_stats(parsed, "ALL")
    
    # Save report
    report_path = "/mnt/c/Users/12035/father_daddy_capital/v19_7f_ablation_report.json"
    with open(report_path, 'w') as f:
        json.dump({k: v for k, v in report.items() if v is not None}, f, indent=2, default=str)
    print(f"\nReport saved: {report_path}")
    print(f"{'='*70}")

if __name__ == "__main__":
    run_ablation()