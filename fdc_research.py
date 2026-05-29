#!/usr/bin/env python3
"""
FDC Research Agent — Trade Pattern Analysis (Neural Team Member #2)
====================================================================
Triggered on trade resolution. Analyzes historical trade patterns and writes
findings to Obsidian vault for persistent learning.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict, Counter

REPO = Path(__file__).parent
STATE_FILE = REPO / "output" / "v19_paper_state.json"
VAULT_DIR = Path("/mnt/c/Users/12035/Father-Agent-Brain/10-Projects/Father-Daddy-Capital")

def analyze_patterns():
    """Analyze trade history for winning/losing patterns."""
    if not STATE_FILE.exists():
        print("No state file yet")
        return None
    
    state = json.loads(STATE_FILE.read_text())
    trades = state.get("trades", [])
    
    if not trades:
        print("No trades yet")
        return None
    
    analysis = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_trades": len(trades),
        "by_direction": defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0}),
        "by_strategy": defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0}),
        "by_price_tier": defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0}),
        "by_confluence": defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0}),
        "by_side": defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0}),
    }
    
    for t in trades:
        pnl = t.get("pnl", 0) or 0
        is_win = pnl > 0
        
        # Direction
        direction = t.get("strategy", "unknown")
        entry = t.get("entry_price", 0.5)
        side = t.get("side", t.get("action", "").replace("BUY_", ""))
        
        # Price tier
        if entry <= 0.08:
            tier = "ultra_cheap"
        elif entry <= 0.20:
            tier = "cheap"
        elif entry <= 0.50:
            tier = "mid"
        else:
            tier = "expensive"
        
        # Confluence bucket
        conf = t.get("confluence", 0) or 0
        if conf >= 8:
            conf_bucket = "8+"
        elif conf >= 7:
            conf_bucket = "7-8"
        elif conf >= 6:
            conf_bucket = "6-7"
        else:
            conf_bucket = "<6"
        
        analysis["by_strategy"][direction]["wins" if is_win else "losses"] += 1
        analysis["by_strategy"][direction]["pnl"] += pnl
        
        analysis["by_price_tier"][tier]["wins" if is_win else "losses"] += 1
        analysis["by_price_tier"][tier]["pnl"] += pnl
        
        analysis["by_confluence"][conf_bucket]["wins" if is_win else "losses"] += 1
        analysis["by_confluence"][conf_bucket]["pnl"] += pnl
        
        analysis["by_side"][side]["wins" if is_win else "losses"] += 1
        analysis["by_side"][side]["pnl"] += pnl
    
    # Calculate WR per category
    results = {
        "timestamp": analysis["timestamp"],
        "total_trades": len(trades),
        "total_pnl": sum(t.get("pnl", 0) or 0 for t in trades),
    }
    
    wins = sum(1 for t in trades if (t.get("pnl", 0) or 0) > 0)
    results["win_rate"] = round(wins / len(trades) * 100, 1) if trades else 0
    
    for category in ["by_strategy", "by_price_tier", "by_confluence", "by_side"]:
        results[category] = {}
        for key, data in analysis[category].items():
            total = data["wins"] + data["losses"]
            wr = round(data["wins"] / total * 100, 1) if total else 0
            results[category][key] = {
                "trades": total,
                "wins": data["wins"],
                "losses": data["losses"],
                "win_rate": wr,
                "pnl": round(data["pnl"], 2),
            }
    
    # Write to Obsidian
    write_to_vault(results)
    return results

def write_to_vault(results):
    """Write pattern analysis to Obsidian vault."""
    if not VAULT_DIR.exists():
        VAULT_DIR.mkdir(parents=True, exist_ok=True)
    
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    filepath = VAULT_DIR / f"trade-analysis-{ts}.md"
    
    md = f"""# Trade Pattern Analysis — {ts}

## Summary
- **Trades**: {results['total_trades']}
- **Win Rate**: {results['win_rate']}%
- **Total PnL**: ${results['total_pnl']:.2f}

## By Price Tier
| Tier | Trades | Wins | Losses | WR | PnL |
|------|--------|------|--------|-----|-----|
"""
    for tier in ["ultra_cheap", "cheap", "mid", "expensive"]:
        if tier in results["by_price_tier"]:
            d = results["by_price_tier"][tier]
            md += f"| {tier} | {d['trades']} | {d['wins']} | {d['losses']} | {d['win_rate']}% | ${d['pnl']:.2f} |\n"
    
    md += "\n## By Strategy\n"
    for strat, d in sorted(results["by_strategy"].items()):
        md += f"- **{strat}**: {d['trades']} trades, {d['win_rate']}% WR, ${d['pnl']:.2f} PnL\n"
    
    md += "\n## By Confluence\n"
    for conf in ["<6", "6-7", "7-8", "8+"]:
        if conf in results["by_confluence"]:
            d = results["by_confluence"][conf]
            md += f"- **Confluence {conf}**: {d['trades']} trades, {d['win_rate']}% WR, ${d['pnl']:.2f} PnL\n"
    
    md += "\n## By Side (Up/Down)\n"
    for side, d in results["by_side"].items():
        md += f"- **{side}**: {d['trades']} trades, {d['win_rate']}% WR, ${d['pnl']:.2f} PnL\n"
    
    # Key insights
    md += "\n## Key Insights\n"
    best_tier = max(results["by_price_tier"].items(), key=lambda x: x[1]["win_rate"]) if results["by_price_tier"] else ("none", {"win_rate": 0})
    worst_tier = min(results["by_price_tier"].items(), key=lambda x: x[1]["win_rate"]) if results["by_price_tier"] else ("none", {"win_rate": 0})
    md += f"- Best price tier: **{best_tier[0]}** ({best_tier[1]['win_rate']}% WR)\n"
    md += f"- Worst price tier: **{worst_tier[0]}** ({worst_tier[1]['win_rate']}% WR)\n"
    
    if results["by_confluence"]:
        best_conf = max(results["by_confluence"].items(), key=lambda x: x[1]["win_rate"])
        md += f"- Best confluence: **{best_conf[0]}** ({best_conf[1]['win_rate']}% WR)\n"
    
    filepath.write_text(md)
    print(f"  Written to {filepath}")

def main():
    results = analyze_patterns()
    if results:
        print(f"[{results['timestamp']}] Research Report")
        print(f"  Trades: {results['total_trades']} | WR: {results['win_rate']}% | PnL: ${results['total_pnl']:.2f}")
        for tier, d in results["by_price_tier"].items():
            print(f"  {tier}: {d['trades']} trades, {d['win_rate']}% WR, ${d['pnl']:.2f}")

if __name__ == "__main__":
    main()