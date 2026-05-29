#!/usr/bin/env python3
"""
FDC Audit Agent — Daily Review & Parameter Tuning Suggestions (Neural Team Member #3)
=====================================================================================
Runs at end of day (23:59 UTC). Reviews all closed trades, calculates metrics,
and suggests parameter adjustments for V19.2+. Writes to Obsidian vault.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

REPO = Path(__file__).parent
STATE_FILE = REPO / "output" / "v19_paper_state.json"
VAULT_DIR = Path("/mnt/c/Users/12035/Father-Agent-Brain/10-Projects/Father-Daddy-Capital")

def daily_audit():
    """Perform a full daily audit and suggest parameter changes."""
    if not STATE_FILE.exists():
        print("No state file yet")
        return
    
    state = json.loads(STATE_FILE.read_text())
    trades = state.get("trades", [])
    bankroll = state.get("bankroll", 0)
    
    if not trades:
        print("No trades to audit")
        return
    
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    
    # Core metrics
    closed = [t for t in trades if t.get("pnl") is not None]
    wins = [t for t in closed if (t.get("pnl", 0) or 0) > 0]
    losses = [t for t in closed if (t.get("pnl", 0) or 0) <= 0]
    wr = round(len(wins) / len(closed) * 100, 1) if closed else 0
    total_pnl = sum(t.get("pnl", 0) or 0 for t in closed)
    avg_win = sum(t.get("pnl", 0) for t in wins) / len(wins) if wins else 0
    avg_loss = sum(abs(t.get("pnl", 0)) for t in losses) / len(losses) if losses else 0
    
    # Category analysis
    by_tier = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0})
    by_dir = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0})
    by_conf = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0})
    
    for t in closed:
        pnl = t.get("pnl", 0) or 0
        entry = t.get("entry_price", 0.5)
        side = t.get("side", "?")
        conf = t.get("confluence", 0) or 0
        
        tier = "≤8¢" if entry <= 0.08 else "8-20¢" if entry <= 0.20 else "20-50¢" if entry <= 0.50 else "≥50¢"
        conf_bucket = f"{int(conf)}" if conf else "?"
        
        by_tier[tier]["wins" if pnl > 0 else "losses"] += 1
        by_tier[tier]["pnl"] += pnl
        by_dir[side]["wins" if pnl > 0 else "losses"] += 1
        by_dir[side]["pnl"] += pnl
        by_conf[conf_bucket]["wins" if pnl > 0 else "losses"] += 1
        by_conf[conf_bucket]["pnl"] += pnl
    
    # Generate parameter suggestions
    suggestions = []
    
    # 1. Dead zone adjustment
    mid_trades = by_tier.get("8-20¢", {"wins": 0, "losses": 0, "pnl": 0})
    if mid_trades["wins"] + mid_trades["losses"] >= 3:
        mid_wr = mid_trades["wins"] / (mid_trades["wins"] + mid_trades["losses"]) * 100
        if mid_wr >= 70:
            suggestions.append(f"Widen mid-zone: 8-20¢ WR={mid_wr:.0f}% ≥70% — consider lowering MID_ZONE_MIN_CONFLUENCE from 8 to 7")
        elif mid_wr < 50:
            suggestions.append(f"Tighten mid-zone: 8-20¢ WR={mid_wr:.0f}% <50% — consider raising MID_ZONE_MIN_CONFLUENCE or narrowing dead zone")
    
    # 2. Confluence threshold
    low_conf = by_conf.get("6", {"wins": 0, "losses": 0})
    if low_conf["wins"] + low_conf["losses"] >= 3:
        low_wr = low_conf["wins"] / (low_conf["wins"] + low_conf["losses"]) * 100
        if low_wr < 50:
            suggestions.append(f"Raise MIN_CONFLUENCE: conf=6 WR={low_wr:.0f}% <50% — raise from 6 to 7")
    
    # 3. Direction bias
    up_trades = by_dir.get("Up", {"wins": 0, "losses": 0, "pnl": 0})
    down_trades = by_dir.get("Down", {"wins": 0, "losses": 0, "pnl": 0})
    if up_trades["wins"] + up_trades["losses"] >= 3 and down_trades["wins"] + down_trades["losses"] >= 3:
        up_wr = up_trades["wins"] / (up_trades["wins"] + up_trades["losses"]) * 100
        down_wr = down_trades["wins"] / (down_trades["wins"] + down_trades["losses"]) * 100
        if abs(up_wr - down_wr) > 20:
            worse = "UP" if up_wr < down_wr else "DOWN"
            suggestions.append(f"Direction bias: {worse} WR much worse ({min(up_wr, down_wr):.0f}% vs {max(up_wr, down_wr):.0f}%) — consider tighter filters on {worse}")
    
    # 4. Bankroll health
    roi = round((bankroll - 400) / 400 * 100, 1)
    if roi < -10:
        suggestions.append(f"⚠️ CRITICAL: ROI={roi}% — consider reducing position size or pausing trading")
    
    # Write report
    report = {
        "date": today,
        "timestamp": now.isoformat(),
        "bankroll": bankroll,
        "roi_pct": roi,
        "total_trades": len(closed),
        "win_rate": wr,
        "total_pnl": round(total_pnl, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(abs(avg_win / avg_loss), 2) if avg_loss > 0 else float("inf"),
        "by_tier": {k: {"trades": v["wins"]+v["losses"], "wins": v["wins"], "wr": round(v["wins"]/(v["wins"]+v["losses"])*100,1) if v["wins"]+v["losses"] else 0, "pnl": round(v["pnl"],2)} for k,v in by_tier.items()},
        "by_direction": {k: {"trades": v["wins"]+v["losses"], "wins": v["wins"], "wr": round(v["wins"]/(v["wins"]+v["losses"])*100,1) if v["wins"]+v["losses"] else 0, "pnl": round(v["pnl"],2)} for k,v in by_dir.items()},
        "by_confluence": {k: {"trades": v["wins"]+v["losses"], "wins": v["wins"], "wr": round(v["wins"]/(v["wins"]+v["losses"])*100,1) if v["wins"]+v["losses"] else 0, "pnl": round(v["pnl"],2)} for k,v in by_conf.items()},
        "suggestions": suggestions,
    }
    
    write_audit(report)
    return report

def write_audit(report):
    """Write daily audit to Obsidian vault."""
    if not VAULT_DIR.exists():
        VAULT_DIR.mkdir(parents=True, exist_ok=True)
    
    filepath = VAULT_DIR / f"daily-audit-{report['date']}.md"
    
    md = f"""# FDC Daily Audit — {report['date']}

## Summary
- **Bankroll**: ${report['bankroll']:.2f} | **ROI**: {report['roi_pct']}%
- **Trades**: {report['total_trades']} | **WR**: {report['win_rate']}%
- **PnL**: ${report['total_pnl']:.2f} | **Avg Win**: ${report['avg_win']:.2f} | **Avg Loss**: ${report['avg_loss']:.2f}
- **Profit Factor**: {report['profit_factor']}

## By Price Tier
"""
    for tier in ["≤8¢", "8-20¢", "20-50¢", "≥50¢"]:
        if tier in report["by_tier"]:
            d = report["by_tier"][tier]
            md += f"- **{tier}**: {d['trades']} trades, {d['wr']}% WR, ${d['pnl']:.2f} PnL\n"
    
    md += "\n## By Direction\n"
    for direction in ["Up", "Down"]:
        if direction in report["by_direction"]:
            d = report["by_direction"][direction]
            md += f"- **{direction}**: {d['trades']} trades, {d['wr']}% WR, ${d['pnl']:.2f} PnL\n"
    
    md += "\n## By Confluence\n"
    for conf in sorted(report["by_confluence"].keys()):
        d = report["by_confluence"][conf]
        md += f"- **Conf {conf}**: {d['trades']} trades, {d['wr']}% WR, ${d['pnl']:.2f} PnL\n"
    
    md += "\n## 🧠 Suggested Parameter Changes\n"
    for s in report["suggestions"]:
        md += f"- {s}\n"
    if not report["suggestions"]:
        md += "- No changes needed — current parameters performing well\n"
    
    filepath.write_text(md)
    print(f"  Written to {filepath}")

def main():
    report = daily_audit()
    if report:
        print(f"[{report['date']}] Daily Audit")
        print(f"  Bankroll: ${report['bankroll']:.2f} | ROI: {report['roi_pct']}%")
        print(f"  Trades: {report['total_trades']} | WR: {report['win_rate']}% | PnL: ${report['total_pnl']:.2f}")
        print(f"  Profit Factor: {report['profit_factor']}")
        for s in report["suggestions"]:
            print(f"  💡 {s}")

if __name__ == "__main__":
    main()