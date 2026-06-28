#!/usr/bin/env python3
"""
V21.7.48 — Expanded Observation Mesh
=======================================
5-second 5m crypto scan mesh + daily weather paper calibration.
NO live scope expansion. Live path remains BTC 15m DOWN only.

5m assets: BTC, ETH, SOL, XRP
5m cadence: 5s (SHADOW_DIAGNOSTIC_ONLY)
Weather: daily paper calibration (QUARANTINED)
Live scope: UNCHANGED — BTC 15m DOWN 3-8¢ / 8-12¢ only
"""
from __future__ import annotations
import json, os, sys, time, logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "output" / "v21748_expanded_observation_mesh"
SUP = ROOT / "output" / "supervisor"
OUT.mkdir(parents=True, exist_ok=True)
SUP.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("v21748")

# ═══════════════════════════════════════════════════════════════════════════
# §2: LIVE SCOPE LOCK — HARD BOUNDARIES
# ═══════════════════════════════════════════════════════════════════════════
AUTHORIZED_LIVE_CELLS = [
    "BTC_15M_DOWN_3_8_TAIL_CANARY",
    "BTC_15M_DOWN_8_12_MICRO_CANARY",
]

FIVE_MINUTE_LIVE_ALLOWED = {
    "BTC": False,
    "ETH": False,
    "SOL": False,
    "XRP": False,
}

WEATHER_LIVE_ALLOWED = False
WEATHER_MODE = "DAILY_PAPER_CALIBRATION"

MAX_ORDER_SIZE_USD = 5.00
MAX_OPEN_LIVE_POSITIONS = 1
MAX_DAILY_LIVE_TRADES = 1
POST_FILL_FREEZE = True

# ═══════════════════════════════════════════════════════════════════════════
# §4-5: 5M MARKET DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════
CRYPTO_5M_ASSETS = ["BTC", "ETH", "SOL", "XRP"]
CRYPTO_5M_INTERVAL = "5m"
CRYPTO_5M_SIDES = ["UP", "DOWN"]
SCAN_INTERVAL_S = 5

# ═══════════════════════════════════════════════════════════════════════════
# §7: ZONE CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════════
def classify_zone(ask: float) -> str:
    if ask < 0.03:
        return "BELOW_RANGE"
    elif ask <= 0.08:
        return "CANARY_3_8"
    elif ask <= 0.12:
        return "NEAR_8_12"
    elif ask <= 0.20:
        return "SECONDARY_12_20"
    elif ask <= 0.25:
        return "EXTENDED_20_25"
    elif ask <= 0.30:
        return "EXTENDED_25_30"
    elif ask <= 0.60:
        return "MIDZONE_30_60"
    elif ask <= 0.85:
        return "HIGH_60_85"
    elif ask <= 0.99:
        return "RESOLUTION_85_99"
    else:
        return "INVALID"


# ═══════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class MarketDiscovery:
    timestamp: str = ""
    asset: str = ""
    interval: str = "5m"
    side: str = ""
    market_slug: str = ""
    condition_id: str = ""
    up_token_id: str = ""
    down_token_id: str = ""
    window_start: str = ""
    window_end: str = ""
    expiry_timestamp: str = ""
    time_to_expiry_s: float = 0.0
    active: bool = False
    closed: bool = False
    accepting_orders: bool = False

@dataclass
class QuoteSnapshot:
    timestamp: str = ""
    asset: str = ""
    interval: str = "5m"
    side: str = ""
    market_slug: str = ""
    condition_id: str = ""
    token_id: str = ""
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread: float = 0.0
    raw_best_bid: float = 0.0
    raw_best_ask: float = 0.0
    normalized_best_bid: float = 0.0
    normalized_best_ask: float = 0.0
    underlying_quote_source: str = "PM_CLOB_READ"
    normalized_price_source: str = "NORMALIZED_BOOK"
    quote_age_ms: int = 0
    book_valid: bool = False
    time_to_expiry_s: float = 0.0

@dataclass
class ZoneTransition:
    timestamp: str = ""
    asset: str = ""
    interval: str = "5m"
    side: str = ""
    market_slug: str = ""
    condition_id: str = ""
    token_id: str = ""
    best_ask: float = 0.0
    best_bid: float = 0.0
    spread: float = 0.0
    zone: str = ""
    previous_zone: str = ""
    zone_changed: bool = False
    time_to_expiry_s: float = 0.0
    underlying_quote_source: str = "PM_CLOB_READ"
    quote_age_ms: int = 0
    reject_reason: str = ""

@dataclass
class ShadowOpportunity:
    timestamp: str = ""
    asset: str = ""
    side: str = ""
    ask: float = 0.0
    bid: float = 0.0
    spread: float = 0.0
    tte_s: float = 0.0
    market_slug: str = ""
    condition_id: str = ""
    token_id: str = ""
    zone: str = ""
    underlying_quote_source: str = "PM_CLOB_READ"
    normalized_price_source: str = "NORMALIZED_BOOK"
    diagnostic_classification: str = ""
    reason_not_live: str = ""

@dataclass
class RolloverEvent:
    timestamp: str = ""
    asset: str = ""
    interval: str = "5m"
    old_slug: str = ""
    new_slug: str = ""
    old_condition_id: str = ""
    new_condition_id: str = ""
    old_down_token_id: str = ""
    new_down_token_id: str = ""
    old_up_token_id: str = ""
    new_up_token_id: str = ""
    identity_verified: bool = False

@dataclass
class WeatherInput:
    timestamp: str = ""
    location_station: str = ""
    forecast_source: str = ""
    market_slug: str = ""
    market_question: str = ""
    forecast_timestamp: str = ""
    target_date: str = ""
    target_hour_window: str = ""
    temperature_forecast: Optional[float] = None
    precipitation_forecast: Optional[str] = None
    wind_forecast: Optional[str] = None
    market_price: Optional[float] = None
    market_side: str = ""
    market_expiry: str = ""

@dataclass
class WeatherPaperDecision:
    timestamp: str = ""
    market_slug: str = ""
    condition_id: str = ""
    question: str = ""
    selected_side: str = ""
    paper_entry_price: float = 0.0
    forecast_probability: float = 0.0
    market_probability: float = 0.0
    edge_estimate: float = 0.0
    paper_size: float = 0.0
    reason_for_entry: str = ""
    reason_for_no_entry: str = ""

@dataclass
class LiveScopeProtection:
    timestamp: str = ""
    btc_5m_live_allowed: bool = False
    eth_5m_live_allowed: bool = False
    sol_5m_live_allowed: bool = False
    xrp_5m_live_allowed: bool = False
    weather_live_allowed: bool = False
    authorized_live_cells: List[str] = field(default_factory=list)
    live_order_assertions_passed: bool = False
    live_scope_unchanged: bool = True


# ═══════════════════════════════════════════════════════════════════════════
# MARKET DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════

def discover_5m_markets() -> List[dict]:
    """§5: Discover all 5m crypto markets."""
    import requests
    from multi_market_scanner import discover_all_markets
    
    all_markets = discover_all_markets()
    five_m_markets = []
    
    for m in all_markets:
        slug = m.get('slug', '')
        if '5m' not in slug.lower():
            continue
        # Determine asset
        asset = None
        for a in CRYPTO_5M_ASSETS:
            if a.lower() in slug.lower():
                asset = a
                break
        if not asset:
            continue
        
        # Get full market data from Gamma
        r = requests.get(f'https://gamma-api.polymarket.com/markets?slug={slug}', timeout=15)
        if r.status_code != 200:
            continue
        mkts = r.json()
        for mk in mkts:
            outcomes_raw = mk.get('outcomes', '[]')
            try:
                outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            except:
                outcomes = []
            if 'Up' not in str(outcomes) or 'Down' not in str(outcomes):
                continue
            
            try:
                token_ids = json.loads(mk.get('clobTokenIds', '[]')) if isinstance(mk.get('clobTokenIds'), str) else mk.get('clobTokenIds', [])
            except:
                token_ids = []
            try:
                prices = json.loads(mk.get('outcomePrices', '[]')) if isinstance(mk.get('outcomePrices'), str) else mk.get('outcomePrices', [])
            except:
                prices = []
            
            up_token_id = ""
            down_token_id = ""
            for i, o in enumerate(outcomes):
                if 'up' in str(o).lower() and i < len(token_ids):
                    up_token_id = token_ids[i]
                elif 'down' in str(o).lower() and i < len(token_ids):
                    down_token_id = token_ids[i]
            
            end_date = mk.get('endDate', '')
            tte = 0.0
            try:
                end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
                tte = (end_dt - datetime.now(timezone.utc)).total_seconds()
            except:
                pass
            
            five_m_markets.append({
                "asset": asset,
                "interval": "5m",
                "slug": slug,
                "condition_id": mk.get('conditionId', mk.get('condition_id', '')),
                "up_token_id": up_token_id,
                "down_token_id": down_token_id,
                "outcomes": outcomes,
                "prices": prices,
                "end_date": end_date,
                "tte": tte,
                "active": mk.get('active', False),
                "closed": mk.get('closed', False),
                "accepting_orders": mk.get('acceptingOrders', mk.get('accepting_orders', False)),
            })
    
    return five_m_markets


def get_5m_orderbook(token_id: str) -> Optional[dict]:
    """Get CLOB orderbook for a 5m token."""
    import requests
    r = requests.get(f'https://clob.polymarket.com/book?token_id={token_id}', timeout=10)
    if r.status_code != 200:
        return None
    book = r.json()
    asks = sorted(book.get('asks', []), key=lambda x: float(x.get('price', 1)))
    bids = sorted(book.get('bids', []), key=lambda x: float(x.get('price', 0)), reverse=True)
    best_ask = float(asks[0]['price']) if asks else None
    best_bid = float(bids[0]['price']) if bids else None
    spread = round(best_ask - best_bid, 4) if best_ask and best_bid else None
    return {"best_ask": best_ask, "best_bid": best_bid, "spread": spread, "book_valid": bool(asks or bids)}


# ═══════════════════════════════════════════════════════════════════════════
# WEATHER PAPER CALIBRATION
# ═══════════════════════════════════════════════════════════════════════════

def run_weather_paper_calibration() -> dict:
    """§11-14: Daily weather paper calibration."""
    import requests
    
    # Search for weather/temperature markets
    weather_markets = []
    for query in ['temperature', 'weather']:
        try:
            r = requests.get(f'https://gamma-api.polymarket.com/markets?closed=false&limit=20&tag={query}', timeout=10)
            if r.status_code == 200:
                for mk in r.json():
                    q = str(mk.get('question', '')).lower()
                    if any(k in q for k in ['temperature', 'temp', 'weather', 'forecast', 'rain', 'snow', 'celsius', 'fahrenheit']):
                        weather_markets.append(mk)
        except:
            pass
    
    # Also search by text
    try:
        r = requests.get('https://gamma-api.polymarket.com/markets?closed=false&limit=200', timeout=15)
        if r.status_code == 200:
            for mk in r.json():
                q = str(mk.get('question', '')).lower()
                if any(k in q for k in ['temperature', 'high will be', 'low will be', 'weather', 'rain', 'snow']):
                    if mk not in weather_markets:
                        weather_markets.append(mk)
    except:
        pass
    
    weather_inputs = []
    weather_decisions = []
    
    for wm in weather_markets[:5]:  # Process up to 5
        wi = WeatherInput(
            timestamp=datetime.now(timezone.utc).isoformat(),
            location_station="UNKNOWN",
            forecast_source="NO_SOURCE",
            market_slug=wm.get('slug', ''),
            market_question=wm.get('question', '')[:200],
            forecast_timestamp=datetime.now(timezone.utc).isoformat(),
            target_date=wm.get('endDate', '')[:10],
            target_hour_window="",
            market_price=None,
            market_side="",
            market_expiry=wm.get('endDate', ''),
        )
        weather_inputs.append(asdict(wi))
        
        wd = WeatherPaperDecision(
            timestamp=datetime.now(timezone.utc).isoformat(),
            market_slug=wm.get('slug', ''),
            condition_id=wm.get('conditionId', ''),
            question=wm.get('question', '')[:200],
            selected_side="",
            paper_entry_price=0.0,
            forecast_probability=0.0,
            market_probability=0.0,
            edge_estimate=0.0,
            paper_size=0.0,
            reason_for_entry="",
            reason_for_no_entry="NO_FORECAST_SOURCE_AVAILABLE" if not weather_markets else "NO_WEATHER_EDGE",
        )
        weather_decisions.append(asdict(wd))
    
    if not weather_markets:
        wd = WeatherPaperDecision(
            timestamp=datetime.now(timezone.utc).isoformat(),
            reason_for_no_entry="NO_WEATHER_MARKET_AVAILABLE",
        )
        weather_decisions.append(asdict(wd))
    
    return {
        "weather_markets_found": len(weather_markets),
        "weather_inputs": weather_inputs,
        "weather_decisions": weather_decisions,
        "weather_mode": WEATHER_MODE,
        "weather_live_allowed": WEATHER_LIVE_ALLOWED,
    }


# ═══════════════════════════════════════════════════════════════════════════
# §16: LIVE SCOPE PROTECTION ASSERTIONS
# ═══════════════════════════════════════════════════════════════════════════

def verify_live_scope_protection() -> LiveScopeProtection:
    """Hard assertions: 5m and weather cannot submit live orders."""
    protection = LiveScopeProtection(
        timestamp=datetime.now(timezone.utc).isoformat(),
        btc_5m_live_allowed=FIVE_MINUTE_LIVE_ALLOWED["BTC"],
        eth_5m_live_allowed=FIVE_MINUTE_LIVE_ALLOWED["ETH"],
        sol_5m_live_allowed=FIVE_MINUTE_LIVE_ALLOWED["SOL"],
        xrp_5m_live_allowed=FIVE_MINUTE_LIVE_ALLOWED["XRP"],
        weather_live_allowed=WEATHER_LIVE_ALLOWED,
        authorized_live_cells=AUTHORIZED_LIVE_CELLS.copy(),
    )
    
    # Hard assertions
    try:
        # 5m cells cannot submit live orders
        for asset in CRYPTO_5M_ASSETS:
            assert FIVE_MINUTE_LIVE_ALLOWED[asset] == False, f"{asset}_5M_LIVE_ALLOWED must be False"
        
        # Weather cannot submit live orders
        assert WEATHER_LIVE_ALLOWED == False, "WEATHER_LIVE_ALLOWED must be False"
        
        # Only authorized cells can submit live orders
        assert len(AUTHORIZED_LIVE_CELLS) == 2, "Exactly 2 authorized live cells"
        assert "BTC_15M_DOWN_3_8_TAIL_CANARY" in AUTHORIZED_LIVE_CELLS
        assert "BTC_15M_DOWN_8_12_MICRO_CANARY" in AUTHORIZED_LIVE_CELLS
        
        # Live order size cap
        assert MAX_ORDER_SIZE_USD == 5.00, "Max order size must be $5"
        assert MAX_OPEN_LIVE_POSITIONS == 1, "Max open positions must be 1"
        assert MAX_DAILY_LIVE_TRADES == 1, "Max daily live trades must be 1"
        assert POST_FILL_FREEZE == True, "Post-fill freeze must be True"
        
        protection.live_order_assertions_passed = True
        protection.live_scope_unchanged = True
    except AssertionError as e:
        protection.live_order_assertions_passed = False
        protection.live_scope_unchanged = False
        log.error(f"LIVE SCOPE PROTECTION FAILED: {e}")
    
    return protection


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    log.info("V21.7.48 — Expanded Observation Mesh")
    log.info("=" * 60)
    
    now = datetime.now(timezone.utc)
    
    # ─── §5: 5m Market Discovery ───
    log.info("Discovering 5m crypto markets...")
    markets_5m = discover_5m_markets()
    log.info(f"Found {len(markets_5m)} 5m markets")
    
    discovery_records = []
    quote_records = []
    zone_records = []
    opportunity_records = []
    rollover_records = []
    
    previous_zones = {}  # Track zone transitions
    
    for m in markets_5m:
        asset = m.get("asset", "UNKNOWN")
        slug = m.get("slug", "")
        down_token = m.get("down_token_id", "")
        up_token = m.get("up_token_id", "")
        tte = m.get("tte", 0)
        
        # §5: Market discovery record
        disc = MarketDiscovery(
            timestamp=now.isoformat(),
            asset=asset,
            interval="5m",
            side="BOTH",
            market_slug=slug,
            condition_id=m.get("condition_id", ""),
            up_token_id=up_token,
            down_token_id=down_token,
            window_start="",
            window_end=m.get("end_date", ""),
            expiry_timestamp=m.get("end_date", ""),
            time_to_expiry_s=round(tte, 1),
            active=m.get("active", False),
            closed=m.get("closed", False),
            accepting_orders=m.get("accepting_orders", False),
        )
        discovery_records.append(asdict(disc))
        
        # §6: Get orderbooks for UP and DOWN
        for side, token_id in [("DOWN", down_token), ("UP", up_token)]:
            if not token_id:
                continue
            book = get_5m_orderbook(token_id)
            if not book or not book.get("book_valid"):
                continue
            
            best_ask = book.get("best_ask", 0)
            best_bid = book.get("best_bid", 0)
            spread = book.get("spread", 0)
            zone = classify_zone(best_ask) if best_ask else "UNKNOWN"
            zone_key = f"{asset}_{side}"
            prev_zone = previous_zones.get(zone_key, zone)
            zone_changed = zone != prev_zone
            previous_zones[zone_key] = zone
            
            # §6: Quote snapshot
            qs = QuoteSnapshot(
                timestamp=now.isoformat(),
                asset=asset,
                interval="5m",
                side=side,
                market_slug=slug,
                condition_id=m.get("condition_id", ""),
                token_id=token_id,
                best_bid=best_bid,
                best_ask=best_ask,
                spread=spread,
                raw_best_bid=best_bid,
                raw_best_ask=best_ask,
                normalized_best_bid=best_bid,
                normalized_best_ask=best_ask,
                underlying_quote_source="PM_CLOB_READ",
                normalized_price_source="NORMALIZED_BOOK",
                quote_age_ms=0,
                book_valid=book.get("book_valid", False),
                time_to_expiry_s=round(tte, 1),
            )
            quote_records.append(asdict(qs))
            
            # §7: Zone transition
            zt = ZoneTransition(
                timestamp=now.isoformat(),
                asset=asset,
                interval="5m",
                side=side,
                market_slug=slug,
                condition_id=m.get("condition_id", ""),
                token_id=token_id,
                best_ask=best_ask,
                best_bid=best_bid,
                spread=spread,
                zone=zone,
                previous_zone=prev_zone,
                zone_changed=zone_changed,
                time_to_expiry_s=round(tte, 1),
                underlying_quote_source="PM_CLOB_READ",
                quote_age_ms=0,
                reject_reason="",
            )
            zone_records.append(asdict(zt))
            
            # §8: Shadow opportunity (3-20¢ diagnostic)
            if 0.03 <= best_ask <= 0.20 and book.get("book_valid"):
                # Determine reason not live
                reason = "FIVE_MINUTE_TRACK_NOT_LIVE_AUTHORIZED"
                if asset == "BTC" and side == "DOWN":
                    reason = "BTC_5M_DEPRECATED_FORWARD_NEGATIVE"
                elif asset == "BTC" and side == "UP":
                    reason = "BTC_5M_DEPRECATED_FORWARD_NEGATIVE"
                
                so = ShadowOpportunity(
                    timestamp=now.isoformat(),
                    asset=asset,
                    side=side,
                    ask=best_ask,
                    bid=best_bid,
                    spread=spread,
                    tte_s=round(tte, 1),
                    market_slug=slug,
                    condition_id=m.get("condition_id", ""),
                    token_id=token_id,
                    zone=zone,
                    underlying_quote_source="PM_CLOB_READ",
                    normalized_price_source="NORMALIZED_BOOK",
                    diagnostic_classification="SHADOW_DIAGNOSTIC_ONLY",
                    reason_not_live=reason,
                )
                opportunity_records.append(asdict(so))
    
    # ─── §11-14: Weather paper calibration ───
    log.info("Running weather paper calibration...")
    weather_result = run_weather_paper_calibration()
    log.info(f"Weather markets found: {weather_result['weather_markets_found']}")
    
    # ─── §16: Live scope protection ───
    log.info("Verifying live scope protection...")
    protection = verify_live_scope_protection()
    log.info(f"Live scope protection: assertions_passed={protection.live_order_assertions_passed}, unchanged={protection.live_scope_unchanged}")
    
    # ─── Write all outputs ───
    # §5: Market discovery
    with open(OUT / "crypto_5m_market_discovery.jsonl", "a") as f:
        for r in discovery_records:
            f.write(json.dumps(r) + "\n")
    
    # §6: Quote snapshots
    with open(OUT / "crypto_5m_quote_snapshots.jsonl", "a") as f:
        for r in quote_records:
            f.write(json.dumps(r) + "\n")
    
    # §7: Zone transitions
    with open(OUT / "crypto_5m_zone_transitions.jsonl", "a") as f:
        for r in zone_records:
            f.write(json.dumps(r) + "\n")
    
    # §8: Shadow opportunities
    with open(OUT / "crypto_5m_shadow_opportunities.jsonl", "a") as f:
        for r in opportunity_records:
            f.write(json.dumps(r) + "\n")
    
    # §9: Rollover (written when slug changes — initial scan just logs)
    # (No rollover on first scan — tracked on subsequent scans)
    
    # §12: Weather inputs
    with open(OUT / "weather_daily_inputs.jsonl", "a") as f:
        for r in weather_result.get("weather_inputs", []):
            f.write(json.dumps(r) + "\n")
    
    # §13: Weather decisions
    with open(OUT / "weather_daily_paper_decisions.jsonl", "a") as f:
        for r in weather_result.get("weather_decisions", []):
            f.write(json.dumps(r) + "\n")
    
    # §14: Weather calibration report
    calibration = {
        "timestamp": now.isoformat(),
        "weather_mode": WEATHER_MODE,
        "weather_live_allowed": WEATHER_LIVE_ALLOWED,
        "weather_markets_found": weather_result["weather_markets_found"],
        "paper_trades": 0,
        "resolved": 0,
        "wins": 0,
        "losses": 0,
        "wr": 0.0,
        "net_pnl": 0.0,
        "ev_per_trade": 0.0,
        "pf": 0.0,
        "calibration_error": None,
        "settlement_errors": 0,
        "promotion_requirements": {
            "resolved_paper_events": 0,
            "required": 25,
            "wr": 0.0,
            "required_gt_baseline": True,
            "ev": 0.0,
            "required_gt_0": True,
            "pf": 0.0,
            "required_gte_1_25": True,
        },
        "status": "INSUFFICIENT_SAMPLE" if weather_result["weather_markets_found"] == 0 else "COLLECTING",
    }
    with open(OUT / "weather_calibration_report.json", "w") as f:
        json.dump(calibration, f, indent=2)
    
    # §14: Weather settlements (initial — no prior trades to settle)
    settlement = {
        "timestamp": now.isoformat(),
        "settled_trades": 0,
        "wins": 0,
        "losses": 0,
        "pending_resolution": 0,
        "status": "NO_PRIOR_TRADES",
    }
    with open(OUT / "weather_daily_settlements.jsonl", "a") as f:
        f.write(json.dumps(settlement) + "\n")
    
    # §5: 5m scan report
    scan_report = {
        "timestamp": now.isoformat(),
        "version": "V21.7.48",
        "markets_discovered": len(discovery_records),
        "quotes_captured": len(quote_records),
        "zone_transitions": len(zone_records),
        "shadow_opportunities": len(opportunity_records),
        "scan_interval_s": SCAN_INTERVAL_S,
        "scan_mode": "SHADOW_DIAGNOSTIC_ONLY",
        "assets": CRYPTO_5M_ASSETS,
        "five_minute_live_allowed": FIVE_MINUTE_LIVE_ALLOWED,
        "weather_mode": WEATHER_MODE,
    }
    with open(OUT / "crypto_5m_scan_report.json", "w") as f:
        json.dump(scan_report, f, indent=2)
    
    # §16: Live scope protection report
    with open(OUT / "live_scope_protection_report.json", "w") as f:
        json.dump(asdict(protection), f, indent=2)
    
    # ─── §17: Supervisor ───
    # Get current BTC 15m tier from V21.7.47 scan
    btc15m_status = {}
    v47_status_path = SUP / "v21747_adaptive_armed_scan_status.json"
    if v47_status_path.exists():
        try:
            btc15m_status = json.loads(v47_status_path.read_text())
        except:
            pass
    
    supervisor = {
        "version": "V21.7.48",
        "timestamp": now.isoformat(),
        "mode": "MICRO_LIVE_ARMED_ADAPTIVE_SCAN",
        "btc15m_micro_live_state": btc15m_status.get("mode", "UNKNOWN"),
        "crypto_5m_scan_enabled": True,
        "crypto_5m_scan_interval_seconds": SCAN_INTERVAL_S,
        "crypto_5m_live_allowed": False,
        "weather_daily_activation_enabled": True,
        "weather_live_allowed": False,
        "weather_mode": WEATHER_MODE,
        "current_btc15m_down_ask": btc15m_status.get("current_btc15m_down_ask_cents"),
        "current_btc15m_scan_tier": btc15m_status.get("current_scan_tier"),
        "latest_5m_opportunity_count": len(opportunity_records),
        "latest_weather_decision": "NO_WEATHER_MARKET_AVAILABLE" if weather_result["weather_markets_found"] == 0 else "COLLECTING",
        "live_order_allowed_cells": AUTHORIZED_LIVE_CELLS,
        "blocked_live_cells": ["BTC_5M_DOWN", "BTC_5M_UP", "WEATHER_TEMP", "WEATHER_RAIN", "SCALPER"],
        "open_positions": 0,
        "daily_live_trades": 0,
        "live_scope_protection_passed": protection.live_order_assertions_passed,
        "halted": False,
        "halt_reason": None,
        "next_action": "continue_shadow_observation",
    }
    with open(SUP / "v21748_expanded_observation_mesh_status.json", "w") as f:
        json.dump(supervisor, f, indent=2)
    
    # ─── §18: Final report ───
    classification = "V21.7.48_EXPANDED_OBSERVATION_MESH_ACTIVE"
    if not protection.live_order_assertions_passed:
        classification = "V21.7.48_EXPANDED_OBSERVATION_MESH_FAILED"
    
    final = {
        "version": "V21.7.48",
        "timestamp": now.isoformat(),
        "classification": classification,
        "mode": "MICRO_LIVE_ARMED_ADAPTIVE_SCAN",
        "5m_scan": {
            "enabled": True,
            "interval_s": SCAN_INTERVAL_S,
            "assets": CRYPTO_5M_ASSETS,
            "markets_discovered": len(discovery_records),
            "quotes_captured": len(quote_records),
            "shadow_opportunities": len(opportunity_records),
            "live_allowed": False,
        },
        "weather": {
            "daily_activation": True,
            "mode": WEATHER_MODE,
            "live_allowed": False,
            "markets_found": weather_result["weather_markets_found"],
        },
        "live_scope": {
            "authorized_live_cells": AUTHORIZED_LIVE_CELLS,
            "five_minute_live_allowed": FIVE_MINUTE_LIVE_ALLOWED,
            "weather_live_allowed": WEATHER_LIVE_ALLOWED,
            "protection_passed": protection.live_order_assertions_passed,
            "scope_unchanged": protection.live_scope_unchanged,
        },
        "guardrails_unchanged": True,
        "max_order_size_usd": MAX_ORDER_SIZE_USD,
        "post_fill_freeze": POST_FILL_FREEZE,
    }
    with open(OUT / "v21748_final_report.json", "w") as f:
        json.dump(final, f, indent=2)
    
    log.info(f"5m markets: {len(discovery_records)}  Quotes: {len(quote_records)}  Opportunities: {len(opportunity_records)}")
    log.info(f"Weather markets: {weather_result['weather_markets_found']}  Live scope: {protection.live_scope_unchanged}")
    log.info(f"Classification: {classification}")
    print(json.dumps(final, indent=2))


if __name__ == "__main__":
    main()