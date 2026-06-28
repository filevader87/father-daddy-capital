#!/usr/bin/env python3
"""
V21.7.26 — Scanner Bridge (Canary Integration)
==================================================
Integrates multi-market scanner into the BTC canary watcher.
All zone awareness, strict live-scope enforcement, and audit logging.

LIVE SCOPE — UNCHANGED:
  asset  = BTC
  interval = 15m
  side   = DOWN
  bucket = 3–8¢
  size   = $5
  type   = FAK/FOK

Everything else: observation only. No live expansion.
"""

import json
import time
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import sys
sys.path.insert(0, str(Path(__file__).parent))

from persistent_clob_client import (
    get_pool, http_get_persistent, fetch_books_batch,
    get_pool_stats, close_pool,
)
from book_normalizer import normalize_for_entry

# ─── Paths ───
PROJECT_ROOT = Path("/home/naq1987s/father-daddy-capital")
OUT_DIR = PROJECT_ROOT / "output" / "v21726_scanner_bridge"
SUPERVISOR_DIR = PROJECT_ROOT / "output" / "supervisor"
OUT_DIR.mkdir(parents=True, exist_ok=True)
SUPERVISOR_DIR.mkdir(parents=True, exist_ok=True)

# ─── Constants ───
GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

ASSETS = ["btc", "eth", "sol", "xrp"]
INTERVALS = ["5m", "15m"]

# Zone definitions (§5)
ENTRY_ZONES = {
    "CANARY_3_8":    (0.03, 0.08),   # Live canary candidate (BTC DOWN 15m only)
    "NEAR_8_12":     (0.08, 0.12),   # Observation only — may NOT authorize live
    "SWEeper_92_99": (0.92, 0.99),   # Shadow only — may NOT authorize live
    "MIDZONE_40_60": (0.40, 0.60),   # Observation only — may NOT authorize live
}

# Live-eligible quote sources (§9)
LIVE_QUOTE_SOURCES = {"NORMALIZED_BOOK", "PM_WS_BOOK", "PM_WS_BEST_BID_ASK", "PM_CLOB_READ"}
BLOCKED_QUOTE_SOURCES = {"PM_GAMMA_REST", "PM_REST_FALLBACK", "PM_STALE", "PM_UNAVAILABLE"}

# ─── Timing ───
MAX_QUOTE_AGE_MS = 3000

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('scanner_bridge_v26')

# ─── Market Discovery (persistent pool) ───

_market_cache: Dict[str, dict] = {}
_market_cache_ts: float = 0
_MARKET_CACHE_TTL = 15.0


def discover_all_markets(force: bool = False) -> List[dict]:
    """Discover ALL active crypto Up/Down markets using precise slugs + persistent pool."""
    global _market_cache, _market_cache_ts

    now = time.time()
    if not force and _market_cache and (now - _market_cache_ts) < _MARKET_CACHE_TTL:
        return list(_market_cache.values())

    markets = []
    epoch = int(now)
    next_expiries = {
        "5m": ((epoch // 300) + 1) * 300,
        "15m": ((epoch // 900) + 1) * 900,
    }

    def fetch_event(args):
        asset, interval, next_exp = args
        slug = f"{asset}-updown-{interval}-{next_exp}"
        url = f"{GAMMA_HOST}/events?slug={slug}"
        data = http_get_persistent(url, timeout=2.0)
        if not data or not isinstance(data, list) or not data:
            return None
        event = data[0]
        event_markets = event.get("markets", [])
        if not event_markets:
            return None
        m = event_markets[0]
        tokens_str = m.get("clobTokenIds", "[]")
        try:
            tokens = json.loads(tokens_str) if isinstance(tokens_str, str) else tokens_str
        except json.JSONDecodeError:
            return None
        if not tokens or len(tokens) < 2:
            return None
        return {
            "market_id": m.get("id", ""),
            "slug": slug,
            "question": m.get("question", ""),
            "condition_id": m.get("conditionId", ""),
            "neg_risk": m.get("neg_risk", False),
            "active": m.get("active", False),
            "asset": asset.upper(),
            "interval": interval,
            "expiry_ts": next_exp,
            "tte": next_exp - epoch,
            "up_token_id": tokens[0],
            "down_token_id": tokens[1],
        }

    requests = [(a, i, next_expiries[i]) for a in ASSETS for i in INTERVALS]

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_event, req): req for req in requests}
        for future in as_completed(futures):
            result = future.result()
            if result:
                markets.append(result)

    _market_cache = {m["slug"]: m for m in markets}
    _market_cache_ts = now
    log.info(f"Discovered {len(markets)} markets (persistent pool)")
    return markets


def fetch_books_persistent(markets: List[dict], max_workers: int = 8) -> Dict[str, dict]:
    """Fetch and normalize order books using persistent pool + book_normalizer."""
    token_ids = []
    sides = []
    for m in markets:
        for side, tid in [("UP", m.get("up_token_id", "")), ("DOWN", m.get("down_token_id", ""))]:
            if tid:
                token_ids.append(tid)
                sides.append(side)

    # Raw book fetch
    raw_books = fetch_books_batch(token_ids, sides, max_workers=max_workers)

    # Normalize
    quotes = {}
    for tid, book in raw_books.items():
        idx = token_ids.index(tid)
        side = sides[idx]
        norm = normalize_for_entry(book, token_id=tid, side=side)
        if norm.get("is_valid"):
            quotes[tid] = norm

    return quotes


def classify_zone(ask_price: float) -> str:
    """Classify ask price into entry zone."""
    for zone_name, (lo, hi) in ENTRY_ZONES.items():
        if lo <= ask_price <= hi:
            return zone_name
    return "OTHER"


def evaluate_canary_eligibility(market: dict, quote: dict) -> dict:
    """Evaluate BTC 15m DOWN canary entry eligibility per §10.
    
    STRICT: Only BTC DOWN 15m in CANARY_3_8 zone may be live_eligible.
    """
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "live_eligible": False,
        "canary_order_allowed": False,
        "reject_reasons": [],
        "checks": {},
    }

    # ─── Asset/Interval/Side gate (§2, §10) ───
    asset = market.get("asset", "")
    interval = market.get("interval", "")
    side = "DOWN"  # We only evaluate DOWN for canary
    tte = market.get("tte", 0)
    ask = quote.get("best_ask")
    bid = quote.get("best_bid")
    spread = quote.get("spread")
    source = quote.get("price_source", "UNKNOWN")

    result["checks"]["asset"] = asset
    result["checks"]["interval"] = interval
    result["checks"]["side"] = side
    result["checks"]["ask"] = ask
    result["checks"]["bid"] = bid
    result["checks"]["spread"] = spread
    result["checks"]["tte"] = tte
    result["checks"]["zone"] = classify_zone(ask) if ask else "NO_PRICE"
    result["checks"]["quote_source"] = source

    # ─── Hard gate: BTC 15m DOWN only ───
    if asset != "BTC":
        result["reject_reasons"].append(f"asset={asset} not BTC — observation only")
        return result
    if interval != "15m":
        result["reject_reasons"].append(f"interval={interval} not 15m — observation only")
        return result

    # ─── Zone gate (§5) ───
    zone = classify_zone(ask) if ask else "NO_PRICE"
    if zone != "CANARY_3_8":
        result["reject_reasons"].append(f"zone={zone} not CANARY_3_8 — observation only")
        return result

    result["live_eligible"] = True  # Zone is CANARY_3_8

    # ─── Entry bucket gate ───
    if ask is None or not (0.03 <= ask <= 0.08):
        result["reject_reasons"].append(f"ask={ask} outside 3-8¢ bucket")
        result["live_eligible"] = False
        return result

    # ─── Spread gate ───
    if spread is not None and spread > 0.02:
        result["reject_reasons"].append(f"spread={spread:.4f} > 0.02")
        result["live_eligible"] = False

    # ─── TTE gate ───
    if not (180 <= tte <= 900):
        result["reject_reasons"].append(f"tte={tte}s outside 180-900s")
        result["live_eligible"] = False

    # ─── Quote source gate (§9) ───
    if source in BLOCKED_QUOTE_SOURCES:
        result["reject_reasons"].append(f"quote_source={source} blocked for live")
        result["live_eligible"] = False
    elif source not in LIVE_QUOTE_SOURCES:
        result["reject_reasons"].append(f"quote_source={source} not in live sources")
        result["live_eligible"] = False

    # ─── Ask present gate ───
    if ask is None:
        result["reject_reasons"].append("missing down_ask")
        result["live_eligible"] = False

    # ─── Price-path integrity (§3) ───
    # Verify book_normalizer is active (not raw asks[0])
    if quote.get("price_source") != "NORMALIZED_BOOK":
        result["reject_reasons"].append(f"price_path_integrity: source={source} not NORMALIZED_BOOK")
        result["live_eligible"] = False

    result["canary_order_allowed"] = result["live_eligible"]
    return result


# ─── Full Scan Cycle ───

_scan_latency_history: List[float] = []
_MAX_LATENCY_HISTORY = 100


def run_full_scan() -> dict:
    """Run one complete scan: discover → fetch → classify → audit.
    
    Returns full scan result with zone alerts and canary evaluation.
    """
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

    # 2. Fetch books
    fetch_start = time.time()
    quotes = fetch_books_persistent(markets, max_workers=8)
    fetch_ms = (time.time() - fetch_start) * 1000

    # 3. Classify zones for all markets
    zone_alerts = []
    btc_15m_down = None
    btc_15m_down_quote = None

    for m in markets:
        for side in ["UP", "DOWN"]:
            tid = m.get(f"{side.lower()}_token_id", "")
            if not tid or tid not in quotes:
                continue

            q = quotes[tid]
            ask = q.get("best_ask")
            bid = q.get("best_bid")
            spread = q.get("spread")

            if ask is None or bid is None:
                continue

            zone = classify_zone(ask)
            alert = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "asset": m["asset"],
                "interval": m["interval"],
                "side": side,
                "zone": zone,
                "best_ask": round(ask, 4),
                "best_bid": round(bid, 4),
                "spread": round(spread, 4) if spread else None,
                "quote_source": q.get("price_source", "UNKNOWN"),
                "tte": m.get("tte", 0),
                "slug": m.get("slug", ""),
                "token_id": tid[:40],
                "live_eligible": False,  # Set properly below for BTC 15m DOWN
            }

            # Track BTC 15m DOWN for canary evaluation
            if m["asset"] == "BTC" and m["interval"] == "15m" and side == "DOWN":
                btc_15m_down = m
                btc_15m_down_quote = q

            zone_alerts.append(alert)

    # 4. Evaluate canary eligibility for BTC 15m DOWN
    canary_eval = None
    if btc_15m_down and btc_15m_down_quote:
        canary_eval = evaluate_canary_eligibility(btc_15m_down, btc_15m_down_quote)
        # Update zone alert live_eligible
        for a in zone_alerts:
            if a["asset"] == "BTC" and a["interval"] == "15m" and a["side"] == "DOWN":
                a["live_eligible"] = canary_eval.get("live_eligible", False)
    else:
        canary_eval = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "live_eligible": False,
            "canary_order_allowed": False,
            "reject_reasons": ["BTC_15M_DOWN_NOT_FOUND"],
        }

    total_ms = (time.time() - cycle_start) * 1000
    _scan_latency_history.append(total_ms)
    if len(_scan_latency_history) > _MAX_LATENCY_HISTORY:
        _scan_latency_history.pop(0)

    # 5. Build market summaries
    market_summaries = []
    for m in markets:
        summary = {"slug": m["slug"], "asset": m["asset"], "interval": m["interval"], "tte": m.get("tte", 0)}
        for side in ["UP", "DOWN"]:
            tid = m.get(f"{side.lower()}_token_id", "")
            if tid and tid in quotes:
                q = quotes[tid]
                summary[f"{side.lower()}_ask"] = round(q.get("best_ask", 0), 4) if q.get("best_ask") else None
                summary[f"{side.lower()}_bid"] = round(q.get("best_bid", 0), 4) if q.get("best_bid") else None
                summary[f"{side.lower()}_spread"] = round(q.get("spread", 0), 4) if q.get("spread") else None
                summary[f"{side.lower()}_zone"] = classify_zone(q.get("best_ask", 0))
                summary[f"{side.lower()}_source"] = q.get("price_source", "MISSING")
        market_summaries.append(summary)

    # Compute zone counts
    zone_counts = {}
    for a in zone_alerts:
        z = a["zone"]
        zone_counts[z] = zone_counts.get(z, 0) + 1

    # Latency stats
    p50 = sorted(_scan_latency_history)[len(_scan_latency_history) // 2] if _scan_latency_history else 0
    p95_idx = int(len(_scan_latency_history) * 0.95)
    p95 = sorted(_scan_latency_history)[min(p95_idx, len(_scan_latency_history) - 1)] if _scan_latency_history else 0

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "V21.7.26",
        "classification": "V21.7.26_SCANNER_BRIDGE_SCAN_COMPLETE",
        "markets_discovered": len(markets),
        "books_fetched": len(quotes),
        "zone_alerts": len(zone_alerts),
        "zone_counts": zone_counts,
        "discovery_ms": round(discovery_ms, 1),
        "fetch_ms": round(fetch_ms, 1),
        "total_ms": round(total_ms, 1),
        "cycle_p50_ms": round(p50, 1),
        "cycle_p95_ms": round(p95, 1),
        "btc_15m_down_zone": canary_eval.get("checks", {}).get("zone", "UNKNOWN") if canary_eval else "NOT_FOUND",
        "btc_15m_down_ask": canary_eval.get("checks", {}).get("ask") if canary_eval else None,
        "btc_15m_down_bid": canary_eval.get("checks", {}).get("bid") if canary_eval else None,
        "btc_15m_live_eligible": canary_eval.get("live_eligible", False),
        "canary_order_allowed": canary_eval.get("canary_order_allowed", False),
        "canary_reject_reasons": canary_eval.get("reject_reasons", []),
        "market_summaries": market_summaries,
        "canary_eval": canary_eval,
        "pool_stats": get_pool_stats(),
    }

    # ─── Write output files (§11, §13) ───
    _write_audit_files(zone_alerts, canary_eval, result)

    return result


def _write_audit_files(zone_alerts: list, canary_eval: dict, result: dict):
    """Write all required output files (§11, §12, §13)."""

    # Zone alerts JSONL (§11)
    with open(OUT_DIR / "zone_alerts.jsonl", "a") as f:
        for a in zone_alerts:
            f.write(json.dumps(a, default=str) + "\n")

    # Missed canary signals (§11)
    # A missed canary = BTC DOWN 15m in 3-8¢, spread ≤ 0.02, fresh quote, TTE valid
    # but canary_order_allowed = False
    if canary_eval and not canary_eval.get("canary_order_allowed", False):
        checks = canary_eval.get("checks", {})
        ask = checks.get("ask")
        zone = checks.get("zone", "")
        # Only log as missed if in CANARY zone with valid price
        if ask is not None and zone == "CANARY_3_8":
            missed = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "classification": "MISSED_CANARY_SIGNAL",
                "market_slug": checks.get("interval", ""),
                "down_ask": ask,
                "down_bid": checks.get("bid"),
                "spread": checks.get("spread"),
                "zone": zone,
                "tte": checks.get("tte"),
                "reject_reasons": canary_eval.get("reject_reasons", []),
            }
            with open(OUT_DIR / "missed_canary_signals.jsonl", "a") as f:
                f.write(json.dumps(missed, default=str) + "\n")

    # Scanner performance report (§13)
    perf = {
        "timestamp": result["timestamp"],
        "discovery_ms": result["discovery_ms"],
        "fetch_ms": result["fetch_ms"],
        "total_ms": result["total_ms"],
        "cycle_p50_ms": result["cycle_p50_ms"],
        "cycle_p95_ms": result["cycle_p95_ms"],
        "markets_discovered": result["markets_discovered"],
        "books_fetched": result["books_fetched"],
        "pool_stats": result.get("pool_stats", {}),
    }
    with open(OUT_DIR / "scanner_performance_report.json", "w") as f:
        json.dump(perf, f, indent=2, default=str)

    # Canary bridge report (§13)
    bridge = {
        "timestamp": result["timestamp"],
        "canary_eval": canary_eval,
        "btc_15m_down_zone": result["btc_15m_down_zone"],
        "btc_15m_down_ask": result["btc_15m_down_ask"],
        "btc_15m_down_bid": result["btc_15m_down_bid"],
        "btc_15m_live_eligible": result["btc_15m_live_eligible"],
        "canary_order_allowed": result["canary_order_allowed"],
        "canary_reject_reasons": result["canary_reject_reasons"],
    }
    with open(OUT_DIR / "canary_bridge_report.json", "w") as f:
        json.dump(bridge, f, indent=2, default=str)

    # Supervisor status (§12)
    supervisor = {
        "timestamp": result["timestamp"],
        "version": "V21.7.26",
        "classification": "V21.7.26_SCANNER_BRIDGE_RUNNING",
        "multi_market_scanner_running": True,
        "scanner_cycle_p50_ms": result["cycle_p50_ms"],
        "scanner_cycle_p95_ms": result["cycle_p95_ms"],
        "markets_discovered": result["markets_discovered"],
        "books_fetched": result["books_fetched"],
        "zone_counts": result["zone_counts"],
        "btc_15m_down_zone": result["btc_15m_down_zone"],
        "btc_15m_down_ask": result["btc_15m_down_ask"],
        "btc_15m_down_bid": result["btc_15m_down_bid"],
        "btc_15m_live_eligible": result["btc_15m_live_eligible"],
        "canary_bridge_status": "ARMED" if result["btc_15m_live_eligible"] else "OBSERVING",
        "canary_order_allowed": result["canary_order_allowed"],
        "canary_reject_reasons": result["canary_reject_reasons"],
        "total_ms": result["total_ms"],
    }
    with open(SUPERVISOR_DIR / "v21726_scanner_bridge_status.json", "w") as f:
        json.dump(supervisor, f, indent=2, default=str)

    # Persistent session report (§13)
    session_report = {
        "timestamp": result["timestamp"],
        "pool_stats": get_pool_stats(),
    }
    with open(OUT_DIR / "persistent_session_report.json", "w") as f:
        json.dump(session_report, f, indent=2, default=str)


# ─── Persistent Scanner ───

_scanner_state = {
    "running": True,
    "scan_count": 0,
    "last_scan_ms": 0,
    "total_alerts": 0,
    "live_eligible_seen": 0,
    "missed_canary_count": 0,
}

SCAN_INTERVAL = 3.0


def run_persistent_scanner(interval: float = SCAN_INTERVAL):
    """Run scanner bridge in a loop."""
    log.info(f"V21.7.26 Scanner Bridge starting (interval={interval}s)")
    log.info(f"Live scope: BTC DOWN 15m 3-8¢ only. All other markets: observation only.")
    log.info(f"Zones: {list(ENTRY_ZONES.keys())}")

    while _scanner_state["running"]:
        try:
            result = run_full_scan()
            _scanner_state["scan_count"] += 1
            _scanner_state["last_scan_ms"] = result.get("total_ms", 0)
            _scanner_state["total_alerts"] += result.get("zone_alerts", 0)
            if result.get("btc_15m_live_eligible"):
                _scanner_state["live_eligible_seen"] += 1
                log.warning(f"🎯 CANARY ELIGIBLE: BTC 15m DOWN ask={result['btc_15m_down_ask']} "
                           f"zone={result['btc_15m_down_zone']} tte={result.get('canary_eval', {}).get('checks', {}).get('tte')}s")
            else:
                zone = result.get("btc_15m_down_zone", "?")
                ask = result.get("btc_15m_down_ask", "?")
                reject = "; ".join(result.get("canary_reject_reasons", []))
                log.info(f"Scan: {result['markets_discovered']} mkts, {result['books_fetched']} books, "
                         f"{result['total_ms']:.0f}ms | BTC 15m DN: zone={zone} ask={ask} reject=[{reject}]")
        except Exception as e:
            log.error(f"Scan error: {e}")
            import traceback
            log.error(traceback.format_exc())

        time.sleep(interval)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="V21.7.26 Scanner Bridge")
    parser.add_argument("--run", action="store_true", help="Run persistent scanner bridge")
    parser.add_argument("--scan", action="store_true", help="Single scan and exit")
    parser.add_argument("--interval", type=float, default=3.0, help="Scan interval in seconds")
    args = parser.parse_args()

    if args.scan:
        result = run_full_scan()
        # Print summary
        summary = {k: v for k, v in result.items()
                   if k not in ("market_summaries", "canary_eval", "pool_stats")}
        print(json.dumps(summary, indent=2, default=str))
        close_pool()
    elif args.run:
        try:
            run_persistent_scanner(args.interval)
        except KeyboardInterrupt:
            log.info("Scanner bridge shutting down")
            close_pool()
    else:
        print("V21.7.26 Scanner Bridge")
        print("  Live scope: BTC DOWN 15m 3-8¢ ONLY")
        print("  All other markets: observation only")
        print()
        print("Usage:")
        print("  --scan       Single scan and exit")
        print("  --run        Run persistent scanner bridge")
        print("  --interval N Scan interval in seconds (default: 3)")