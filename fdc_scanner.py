#!/usr/bin/env python3
"""
FDC Scanner Agent — Signal Discovery (Neural Team Member #1)
=============================================================
Runs every 5 min via cronjob. Parses V19.2 scanner output and extracts qualified signals.
Writes structured signal data to output/signals.json for the Research Agent.
"""

import json
import os
import sys
import re
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).parent
SIGNALS_FILE = REPO / "output" / "signals.json"
TRADES_FILE = REPO / "output" / "v19_paper_state.json"

def parse_signals():
    """Parse the last scan output from the paper trade log."""
    # Read current state for trade info
    if TRADES_FILE.exists():
        state = json.loads(TRADES_FILE.read_text())
    else:
        state = {"trades": [], "positions": {}, "bankroll": 0}
    
    signals = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "bankroll": state.get("bankroll", 0),
        "open_positions": len(state.get("positions", {})),
        "total_trades": len(state.get("trades", [])),
        "daily_losses": state.get("daily_losses", 0),
        "daily_loss_amount": state.get("daily_loss_amount", 0),
        "recent_trades": [],
        "open_positions_detail": [],
        "win_rate": 0,
        "total_pnl": state.get("total_pnl", 0),
    }
    
    # Calculate WR
    closed = [t for t in state.get("trades", []) if t.get("pnl") is not None]
    if closed:
        wins = [t for t in closed if t.get("pnl", 0) > 0]
        signals["win_rate"] = round(len(wins) / len(closed) * 100, 1)
    
    # Recent trades (last 5)
    for t in state.get("trades", [])[-5:]:
        signals["recent_trades"].append({
            "id": t.get("id"),
            "action": t.get("action"),
            "entry_price": t.get("entry_price"),
            "side": t.get("side"),
            "strategy": t.get("strategy"),
            "pnl": t.get("pnl"),
            "confluence": t.get("confluence"),
        })
    
    # Open positions
    for k, v in state.get("positions", {}).items():
        signals["open_positions_detail"].append({
            "market": k,
            "side": v.get("side"),
            "entry_price": v.get("entry_price"),
            "strategy": v.get("strategy"),
            "confluence": v.get("confluence"),
        })
    
    # Save
    SIGNALS_FILE.parent.mkdir(exist_ok=True)
    SIGNALS_FILE.write_text(json.dumps(signals, indent=2))
    return signals

def main():
    signals = parse_signals()
    print(f"[{signals['timestamp']}] Scanner Report")
    print(f"  Bankroll: ${signals['bankroll']:.2f}")
    print(f"  Open: {signals['open_positions']} | Trades: {signals['total_trades']} | WR: {signals['win_rate']}%")
    print(f"  Daily losses: {signals['daily_losses']} (${signals['daily_loss_amount']:.2f})")
    if signals["recent_trades"]:
        lt = signals['recent_trades'][-1]
        ep = lt['entry_price']
        pnl = lt.get('pnl')
        ep_str = f"{ep*100:.0f}¢" if ep is not None else "N/A"
        pnl_str = f"${pnl:+.2f}" if pnl is not None else "pending"
        print(f"  Last trade: {lt['action']} @ {ep_str} | PnL: {pnl_str}")

if __name__ == "__main__":
    main()