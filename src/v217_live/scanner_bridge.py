#!/usr/bin/env python3
"""
V21.7.25 — Scanner → Canary Integration Bridge
===================================================
Provides fast multi-market data to the canary watcher.
The watcher can call get_btc_15m_market_and_quote() to get
discovery + quote in one concurrent pass instead of 2 sequential HTTP calls.

Also exposes get_all_market_alerts() for cross-market monitoring.

Classification: V21.7.25_SCANNER_BRIDGE
"""

import sys
import time
import logging
from pathlib import Path
from typing import Optional, Dict, List
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))
from multi_market_scanner import (
    discover_all_markets,
    fetch_books_concurrent,
    detect_entry_zones,
    _market_cache,
    _market_cache_ts,
)

log = logging.getLogger('scanner_bridge')


def get_btc_15m_market_and_quote() -> Optional[Dict]:
    """Get BTC 15m market data + quote using the multi-market scanner.
    
    Returns dict with:
      - market: {market_id, slug, condition_id, down_token_id, up_token_id, tte, ...}
      - quote: {best_ask, best_bid, spread, price_source, is_valid, ...}
      - scan_ms: total time
    
    Falls back to None if not found.
    """
    scan_start = time.time()
    
    # 1. Discover all markets
    markets = discover_all_markets()
    
    # 2. Find BTC 15m
    btc_15m = None
    for m in markets:
        if m["asset"] == "BTC" and m["interval"] == "15m":
            btc_15m = m
            break
    
    if not btc_15m:
        log.warning("BTC 15m market not found in scanner data")
        return None
    
    # 3. Fetch books for BTC 15m tokens
    quotes = fetch_books_concurrent([btc_15m], max_workers=2)
    
    down_tid = btc_15m.get("down_token_id", "")
    up_tid = btc_15m.get("up_token_id", "")
    
    down_quote = quotes.get(down_tid)
    
    scan_ms = (time.time() - scan_start) * 1000
    
    result = {
        "market": {
            "market_id": btc_15m.get("market_id", ""),
            "slug": btc_15m.get("slug", ""),
            "condition_id": btc_15m.get("condition_id", ""),
            "neg_risk": btc_15m.get("neg_risk", False),
            "active": btc_15m.get("active", False),
            "down_token_id": down_tid,
            "up_token_id": up_tid,
            "tte": btc_15m.get("tte", 0),
            "expiry_ts": btc_15m.get("expiry_ts", 0),
        },
        "quote": down_quote,
        "scan_ms": round(scan_ms, 1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    
    return result


def get_all_market_alerts() -> Dict:
    """Run a full scan and return market data + alerts.
    
    Returns:
      - markets: list of all market price summaries
      - alerts: list of entry zone hits
      - live_alerts: list of live-eligible alerts
      - scan_ms: total scan time
    """
    scan_start = time.time()
    
    markets = discover_all_markets()
    if not markets:
        return {
            "status": "NO_MARKETS",
            "markets": [],
            "alerts": [],
            "live_alerts": [],
            "scan_ms": 0,
        }
    
    quotes = fetch_books_concurrent(markets, max_workers=8)
    alerts = detect_entry_zones(markets, quotes)
    
    scan_ms = (time.time() - scan_start) * 1000
    
    # Build market summaries
    market_summaries = []
    for m in markets:
        summary = {"slug": m["slug"], "asset": m["asset"], "interval": m["interval"], "tte": m.get("tte", 0)}
        for side in ["UP", "DOWN"]:
            tid = m.get(f"{side.lower()}_token_id", "")
            if tid and tid in quotes:
                q = quotes[tid]
                summary[f"{side.lower()}_ask"] = q.get("best_ask")
                summary[f"{side.lower()}_bid"] = q.get("best_bid")
                summary[f"{side.lower()}_spread"] = q.get("spread")
        market_summaries.append(summary)
    
    live_alerts = [a for a in alerts if a.get("live_eligible")]
    
    return {
        "status": "OK",
        "markets": market_summaries,
        "alerts": alerts,
        "live_alerts": live_alerts,
        "scan_ms": round(scan_ms, 1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Scanner Bridge Test")
    parser.add_argument("--btc15m", action="store_true", help="Get BTC 15m market + quote")
    parser.add_argument("--all", action="store_true", help="Get all market alerts")
    args = parser.parse_args()
    
    import json
    
    if args.btc15m:
        result = get_btc_15m_market_and_quote()
        if result:
            print(f"BTC 15m Market: {result['market']['slug']}")
            print(f"  TTE: {result['market']['tte']}s")
            print(f"  DOWN ask: {result['quote'].get('best_ask')}")
            print(f"  DOWN bid: {result['quote'].get('best_bid')}")
            print(f"  DOWN spread: {result['quote'].get('spread')}")
            print(f"  Source: {result['quote'].get('price_source')}")
            print(f"  Scan time: {result['scan_ms']}ms")
        else:
            print("BTC 15m market not found")
    
    elif args.all:
        result = get_all_market_alerts()
        print(f"Markets: {len(result['markets'])}")
        print(f"Alerts: {len(result['alerts'])}")
        print(f"Live eligible: {len(result['live_alerts'])}")
        print(f"Scan time: {result['scan_ms']}ms")
        for m in result['markets']:
            da = m.get('down_ask', '-')
            db = m.get('down_bid', '-')
            print(f"  {m['slug'][:40]} DN={da}/{db} tte={m['tte']}s")
    
    else:
        print("Usage: --btc15m or --all")