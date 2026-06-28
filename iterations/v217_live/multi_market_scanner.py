#!/usr/bin/env python3
"""
V21.7.25 — Multi-Market Concurrent Scanner
=============================================
Scans ALL active PM crypto Up/Down markets concurrently.
Detects entry-zone hits with sub-second cycles.

Architecture:
  - Precise slug discovery: {asset}-updown-{interval}-{epoch_ts}
  - ThreadPoolExecutor for concurrent HTTP (discovery + book fetch)
  - book_normalizer for all price reads (V21.7.24 audit compliant)
  - Entry-zone detection across all buckets
  - Feeds canary watcher for live execution

Classification: V21.7.25_MULTI_MARKET_SCANNER
"""

import os
import sys
import json
import time
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request
import urllib.error

sys.path.insert(0, str(Path(__file__).parent))
from book_normalizer import normalize_for_entry

# ─── Paths ───
PROJECT_ROOT = Path("/home/naq1987s/father-daddy-capital")
OUT_DIR = PROJECT_ROOT / "output" / "v21725_multi_scanner"
SUPERVISOR_DIR = PROJECT_ROOT / "output" / "supervisor"
OUT_DIR.mkdir(parents=True, exist_ok=True)
SUPERVISOR_DIR.mkdir(parents=True, exist_ok=True)

# ─── Environment ───
ENV_PATH = Path("/mnt/c/Users/12035/father_daddy_capital/.env")

def load_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

# ─── Constants ───
GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

# Only intervals that exist on Polymarket
ASSETS = ["btc", "eth", "sol", "xrp"]
INTERVALS = ["5m", "15m"]

# Entry zones (price buckets for asks)
ENTRY_ZONES = {
    "CANARY_3_8":    (0.03, 0.08),   # Live-eligible canary
    "NEAR_8_12":     (0.08, 0.12),   # Approaching canary
    "SWEeper_92_99": (0.92, 0.99),   # Near-resolution sweeper
    "MIDZONE_40_60": (0.40, 0.60),   # Coin-flip
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('multi_scanner')

# ─── HTTP ───

def http_get(url: str, timeout: float = 5.0) -> Optional[dict]:
    """Fast HTTP GET with timeout."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "FDC/21.7.25",
            "Accept": "application/json",
        })
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError):
        return None


# ─── Market Discovery (concurrent, precise slugs) ───

_market_cache: Dict[str, dict] = {}
_market_cache_ts: float = 0
_MARKET_CACHE_TTL = 15.0  # seconds — shorter for fresh TTE


def discover_all_markets(force: bool = False) -> List[dict]:
    """Discover ALL active crypto Up/Down markets using precise slug format.
    
    PM slug format: {asset}-updown-{interval}-{epoch_ts}
    e.g. btc-updown-15m-1781205300
    
    This avoids false matches from non-crypto markets.
    """
    global _market_cache, _market_cache_ts
    
    now = time.time()
    if not force and _market_cache and (now - _market_cache_ts) < _MARKET_CACHE_TTL:
        return list(_market_cache.values())
    
    markets = []
    epoch = int(now)
    
    # Compute next expiry for each interval
    next_expiries = {
        "5m": ((epoch // 300) + 1) * 300,
        "15m": ((epoch // 900) + 1) * 900,
    }
    
    def fetch_event(args):
        asset, interval, next_exp = args
        slug = f"{asset}-updown-{interval}-{next_exp}"
        url = f"{GAMMA_HOST}/events?slug={slug}"
        data = http_get(url, timeout=5.0)
        if not data or not isinstance(data, list) or not data:
            return None
        
        event = data[0]
        event_markets = event.get("markets", [])
        if not event_markets:
            return None
        
        m = event_markets[0]  # Single market per event
        tokens_str = m.get("clobTokenIds", "[]")
        try:
            tokens = json.loads(tokens_str) if isinstance(tokens_str, str) else tokens_str
        except json.JSONDecodeError:
            return None
        if not tokens or len(tokens) < 2:
            return None
        
        condition_id = m.get("conditionId", "")
        neg_risk = m.get("neg_risk", False)
        active = m.get("active", False)
        question = m.get("question", "")
        
        return {
            "market_id": m.get("id", ""),
            "slug": slug,
            "question": question,
            "condition_id": condition_id,
            "neg_risk": neg_risk,
            "active": active,
            "asset": asset.upper(),
            "interval": interval,
            "expiry_ts": next_exp,
            "tte": next_exp - epoch,
            "up_token_id": tokens[0],
            "down_token_id": tokens[1],
        }
    
    # Build all requests
    requests = []
    for asset in ASSETS:
        for interval in INTERVALS:
            next_exp = next_expiries[interval]
            requests.append((asset, interval, next_exp))
    
    # Fetch all concurrently
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_event, req): req for req in requests}
        for future in as_completed(futures):
            result = future.result()
            if result:
                markets.append(result)
    
    _market_cache = {m["slug"]: m for m in markets}
    _market_cache_ts = now
    log.info(f"Discovered {len(markets)} crypto Up/Down markets")
    return markets


def fetch_books_concurrent(markets: List[dict], max_workers: int = 8) -> Dict[str, dict]:
    """Fetch order books for ALL tokens concurrently. Returns token_id -> normalized quote."""
    quotes = {}
    token_map = {}
    
    for m in markets:
        for side, tid in [("UP", m.get("up_token_id", "")), ("DOWN", m.get("down_token_id", ""))]:
            if tid:
                token_map[tid] = (m, side)
    
    def fetch_one(tid: str) -> Tuple[str, Optional[dict]]:
        url = f"{CLOB_HOST}/book?token_id={tid}"
        data = http_get(url, timeout=3.0)
        if not data:
            return tid, None
        side = token_map.get(tid, (None, "UNKNOWN"))[1]
        norm = normalize_for_entry(data, token_id=tid, side=side)
        return tid, norm
    
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_one, tid): tid for tid in token_map}
        for future in as_completed(futures):
            tid = futures[future]
            try:
                _, norm = future.result()
                if norm and norm.get("is_valid"):
                    quotes[tid] = norm
            except Exception:
                pass
    
    return quotes


def detect_entry_zones(markets: List[dict], quotes: Dict[str, dict]) -> List[dict]:
    """Detect which markets are in entry zones."""
    alerts = []
    
    for m in markets:
        for side in ["UP", "DOWN"]:
            tid = m.get(f"{side.lower()}_token_id", "")
            if not tid or tid not in quotes:
                continue
            
            q = quotes[tid]
            best_ask = q.get("best_ask")
            best_bid = q.get("best_bid")
            spread = q.get("spread")
            midpoint = q.get("midpoint")
            
            if best_ask is None or best_bid is None:
                continue
            
            for zone_name, (lo, hi) in ENTRY_ZONES.items():
                if lo <= best_ask <= hi:
                    alert = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "asset": m["asset"],
                        "interval": m["interval"],
                        "side": side,
                        "zone": zone_name,
                        "zone_bounds": [lo, hi],
                        "best_ask": round(best_ask, 4),
                        "best_bid": round(best_bid, 4),
                        "spread": round(spread, 4) if spread else None,
                        "midpoint": round(midpoint, 4) if midpoint else None,
                        "book_valid": q.get("is_valid", False),
                        "price_source": q.get("price_source", "UNKNOWN"),
                        "tte": m.get("tte", 0),
                        "slug": m.get("slug", ""),
                        "condition_id": m.get("condition_id", ""),
                        "token_id": tid[:40],
                        "live_eligible": (
                            m["asset"] == "BTC"
                            and m["interval"] in ("5m", "15m")
                            and side == "DOWN"
                            and zone_name == "CANARY_3_8"
                            and 180 <= m.get("tte", 0) <= 900
                        ),
                    }
                    alerts.append(alert)
    
    return alerts


def run_scan_cycle() -> dict:
    """Run one complete scan cycle: discover → fetch → detect. Target: <1s."""
    cycle_start = time.time()
    
    # 1. Discover markets
    markets = discover_all_markets()
    discovery_ms = (time.time() - cycle_start) * 1000
    
    if not markets:
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "NO_MARKETS",
            "discovery_ms": round(discovery_ms, 1),
        }
    
    # 2. Fetch all books concurrently
    fetch_start = time.time()
    quotes = fetch_books_concurrent(markets, max_workers=8)
    fetch_ms = (time.time() - fetch_start) * 1000
    
    # 3. Detect entry zones
    alerts = detect_entry_zones(markets, quotes)
    
    total_ms = (time.time() - cycle_start) * 1000
    
    # 4. Build per-market summary
    market_prices = []
    for m in markets:
        mkt = {"slug": m["slug"], "asset": m["asset"], "interval": m["interval"], "tte": m.get("tte", 0)}
        for side in ["UP", "DOWN"]:
            tid = m.get(f"{side.lower()}_token_id", "")
            if tid and tid in quotes:
                q = quotes[tid]
                mkt[f"{side.lower()}_ask"] = round(q.get("best_ask", 0), 4) if q.get("best_ask") else None
                mkt[f"{side.lower()}_bid"] = round(q.get("best_bid", 0), 4) if q.get("best_bid") else None
                mkt[f"{side.lower()}_spread"] = round(q.get("spread", 0), 4) if q.get("spread") else None
                mkt[f"{side.lower()}_source"] = q.get("price_source", "MISSING")
        market_prices.append(mkt)
    
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "V21.7.25",
        "classification": "MULTI_MARKET_SCAN_COMPLETE",
        "markets_discovered": len(markets),
        "books_fetched": len(quotes),
        "books_invalid": len(market_prices) * 2 - len(quotes),
        "alerts": len(alerts),
        "live_eligible_alerts": sum(1 for a in alerts if a.get("live_eligible")),
        "discovery_ms": round(discovery_ms, 1),
        "fetch_ms": round(fetch_ms, 1),
        "total_ms": round(total_ms, 1),
        "market_prices": market_prices,
        "entry_zone_alerts": alerts,
    }
    
    return result


def scan_and_report() -> dict:
    """Run scan and write output files."""
    result = run_scan_cycle()
    
    # Write scan result (without full alerts to keep file small)
    summary = {k: v for k, v in result.items() if k != "market_prices"}
    # Truncate alerts in summary for readability
    summary["entry_zone_alerts"] = [
        {k: v for k, v in a.items() if k in (
            "timestamp", "asset", "interval", "side", "zone",
            "best_ask", "best_bid", "spread", "tte", "live_eligible"
        )} for a in result.get("entry_zone_alerts", [])
    ]
    
    with open(OUT_DIR / "scan_result.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)
    
    # Write market prices (compact JSONL)
    with open(OUT_DIR / "market_prices.jsonl", "w") as f:
        for mkt in result.get("market_prices", []):
            f.write(json.dumps(mkt, default=str) + "\n")
    
    # Write alerts (if any)
    alerts = result.get("entry_zone_alerts", [])
    if alerts:
        with open(OUT_DIR / "entry_zone_alerts.jsonl", "a") as f:
            for a in alerts:
                f.write(json.dumps(a, default=str) + "\n")
    
    # Write supervisor status
    status = {
        "timestamp": result["timestamp"],
        "version": "V21.7.25",
        "classification": "V21.7.25_MULTI_SCANNER_RUNNING",
        "markets_discovered": result["markets_discovered"],
        "books_fetched": result["books_fetched"],
        "alerts": result["alerts"],
        "live_eligible_alerts": result["live_eligible_alerts"],
        "total_ms": result["total_ms"],
    }
    with open(SUPERVISOR_DIR / "v21725_multi_scanner_status.json", "w") as f:
        json.dump(status, f, indent=2, default=str)
    
    # Log summary
    live = [a for a in alerts if a.get("live_eligible")]
    log.info(f"Scan: {result['markets_discovered']} mkts, {result['books_fetched']} books, "
             f"{len(alerts)} alerts ({len(live)} LIVE), {result['total_ms']:.0f}ms")
    
    if live:
        for a in live:
            log.warning(f"🎯 LIVE ELIGIBLE: {a['asset']}/{a['interval']}/{a['side']} "
                       f"ask={a['best_ask']} spread={a['spread']} tte={a['tte']}s "
                       f"zone={a['zone']}")
    
    return result


# ─── Persistent Scanner ───

_scanner_state = {
    "running": True,
    "scan_count": 0,
    "last_scan_ms": 0,
    "total_alerts": 0,
    "live_eligible_seen": 0,
}

SCAN_INTERVAL = 3.0  # seconds between scans


def run_persistent_scanner(interval: float = SCAN_INTERVAL):
    """Run scanner in a loop, writing results each cycle."""
    log.info(f"V21.7.25 Multi-Market Scanner starting (interval={interval}s)")
    log.info(f"Assets: {[a.upper() for a in ASSETS]} | Intervals: {INTERVALS}")
    log.info(f"Entry zones: {list(ENTRY_ZONES.keys())}")
    
    while _scanner_state["running"]:
        try:
            result = scan_and_report()
            _scanner_state["scan_count"] += 1
            _scanner_state["last_scan_ms"] = result.get("total_ms", 0)
            _scanner_state["total_alerts"] += result.get("alerts", 0)
            _scanner_state["live_eligible_seen"] += result.get("live_eligible_alerts", 0)
        except Exception as e:
            log.error(f"Scan error: {e}")
        
        time.sleep(interval)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="V21.7.25 Multi-Market Scanner")
    parser.add_argument("--run", action="store_true", help="Run persistent scanner")
    parser.add_argument("--scan", action="store_true", help="Single scan and exit")
    parser.add_argument("--interval", type=float, default=3.0, help="Scan interval in seconds")
    args = parser.parse_args()
    
    if args.scan:
        result = scan_and_report()
        print(json.dumps({k: v for k, v in result.items() if k != "market_prices"}, indent=2, default=str))
    elif args.run:
        run_persistent_scanner(args.interval)
    else:
        print("V21.7.25 Multi-Market Scanner")
        print(f"  Assets: {[a.upper() for a in ASSETS]}")
        print(f"  Intervals: {INTERVALS}")
        print(f"  Entry zones: {list(ENTRY_ZONES.keys())}")
        print()
        print("Usage:")
        print("  --scan       Single scan and exit")
        print("  --run        Run persistent scanner")
        print("  --interval N Scan interval in seconds (default: 3)")