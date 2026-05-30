#!/usr/bin/env python3
"""V19.7f Shadow Discovery — logs market discovery every cycle.
No orders. Discovery and scoring only.

Outputs JSON reports to shadow_discovery/ with per-cycle stats.
"""

import json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, '/mnt/c/Users/12035/father_daddy_capital')
import pm_engine_v19_7 as eng

OUT_DIR = Path('/mnt/c/Users/12035/father_daddy_capital/shadow_discovery')
OUT_DIR.mkdir(exist_ok=True)

def shadow_discovery():
    """Run one discovery cycle for all assets. No orders."""
    all_results = []
    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "assets": {},
        "total_valid": 0,
        "total_raw": 0,
        "total_rejected": 0,
        "rejection_reasons": {},
    }
    
    for ak, acfg in eng.ASSETS.items():
        print(f"\n  Discovering {ak} ({acfg['yf']}, {acfg['interval']})...")
        try:
            contracts = eng.discover_contracts(ak)
        except Exception as e:
            print(f"    ERROR: {e}")
            contracts = []
        
        asset_stats = {
            "total_found": len(contracts),
            "markets": [],
        }
        
        for c in contracts:
            entry = {
                "question": c.get("question", "")[:80],
                "conditionId": c.get("conditionId", "")[:20] + "...",
                "asset": c.get("asset", ""),
                "interval": c.get("interval", ""),
                "market_type": c.get("market_type", ""),
                "window": c.get("window", ""),
                "mins_to_expiry": c.get("mins_to_expiry", 0),
                "up_price": round(c.get("up_price", 0), 4),
                "down_price": round(c.get("down_price", 0), 4),
                "spread": round(abs(c.get("up_price", 0) + c.get("down_price", 0) - 1.0), 4),
                "volume": c.get("volume", 0),
                "slug": c.get("slug", ""),
                "end_date": c.get("end_date", ""),
            }
            asset_stats["markets"].append(entry)
            summary["total_valid"] += 1
        
        by_interval = {}
        for m in asset_stats["markets"]:
            iv = m.get("interval", "unknown")
            by_interval[iv] = by_interval.get(iv, 0) + 1
        asset_stats["by_interval"] = by_interval
        
        summary["assets"][ak] = asset_stats
        print(f"    Found {len(contracts)} valid contracts: {by_interval}")
    
    # Save detailed report
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = OUT_DIR / f"shadow_{ts}.json"
    with open(report_path, 'w') as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\n  Report saved: {report_path}")
    
    # Print summary
    print(f"\n{'='*60}")
    print(f"SHADOW DISCOVERY SUMMARY — {summary['timestamp']}")
    print(f"{'='*60}")
    print(f"  Total valid markets: {summary['total_valid']}")
    for ak, stats in summary["assets"].items():
        print(f"  {ak}: {stats['total_found']} contracts — {stats.get('by_interval', {})}")
    print(f"{'='*60}")
    
    return summary

if __name__ == "__main__":
    shadow_discovery()