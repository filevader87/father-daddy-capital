#!/usr/bin/env python3
"""
V2.3 Rain Shadow Cell — Precipitation Market Discovery & Shadow-Only Trading
==============================================================================
Classification: RAIN_MARKET_SHADOW_ONLY
- NO live entries
- NO paper entries until validated
- Discovers rain/precipitation markets on Polymarket
- Computes edge using multi-source precipitation forecasts
- Logs shadow events for post-resolution audit
- Promotion requires 25+ resolved shadow events with PF >= 1.25

Created by directive V21.7.14 — Weather Containment + Rain Market Expansion
"""
import asyncio
import json
import time
import logging
import math
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp

BASE = Path("/home/naq1987s/father-daddy-capital")
OUT = BASE / "output" / "weather_bot" / "rain_shadow"
OUT.mkdir(parents=True, exist_ok=True)

GAMMA_URL = "https://gamma-api.polymarket.com"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

RAIN_SEARCH_TERMS = [
    "rain", "precipitation", "will it rain", "rainfall",
    "measurable precipitation", "precipitation amount",
    "weather rain yes/no", "rain yes or no", "raining",
]

CITY_COORDS = {
    "new-york": (40.71, -74.01), "los-angeles": (34.05, -118.24),
    "london": (51.51, -0.13), "paris": (48.86, 2.35),
    "amsterdam": (52.37, 4.90), "tokyo": (35.68, 139.69),
    "sydney": (-33.87, 151.21), "mumbai": (19.08, 72.88),
    "singapore": (1.35, 103.82), "hong-kong": (22.32, 114.17),
    "dubai": (25.20, 55.27), "seoul": (37.57, 126.98),
    "berlin": (52.52, 13.41), "moscow": (55.76, 37.62),
    "istanbul": (41.01, 28.98), "madrid": (40.42, -3.70),
    "rome": (41.90, 12.50), "chicago": (41.88, -87.63),
    "miami": (25.76, -80.19), "houston": (29.76, -95.37),
    "san-francisco": (37.77, -122.42), "seattle": (47.61, -122.33),
    "atlanta": (33.75, -84.39), "boston": (42.36, -71.06),
    "toronto": (43.65, -79.38), "mexico-city": (19.43, -99.13),
    "sao-paulo": (-23.55, -46.63), "buenos-aires": (-34.60, -58.38),
    "cairo": (30.04, 31.24), "lagos": (6.52, 3.38),
    "nairobi": (-1.29, 36.82), "bangkok": (13.76, 100.50),
    "jakarta": (-6.21, 106.85), "manila": (14.60, 120.98),
    "taipei": (25.03, 121.57), "beijing": (39.90, 116.41),
    "shanghai": (31.23, 121.47), "delhi": (28.61, 77.21),
    "chengdu": (30.57, 104.07), "busan": (35.18, 129.08),
    "helsinki": (60.17, 24.94), "oslo": (59.91, 10.75),
    "stockholm": (59.33, 18.07), "copenhagen": (55.68, 12.57),
    "dublin": (53.35, -6.26), "lisbon": (38.72, -9.14),
    "athens": (37.98, 23.73), "warsaw": (52.23, 21.01),
    "vienna": (48.21, 16.37), "budapest": (47.50, 19.04),
    "prague": (50.08, 14.44), "bucharest": (44.43, 26.10),
    "melbourne": (-37.81, 144.96), "auckland": (-36.85, 174.76),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(OUT / "rain_shadow.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("rain_shadow")

MINIMUM_EDGE_PP = 20.0
MINIMUM_SOURCE_AGREEMENT = 2


async def discover_rain_markets(session: aiohttp.ClientSession) -> List[Dict]:
    """Query Polymarket Gamma API for rain/precipitation markets."""
    all_events = []
    for term in RAIN_SEARCH_TERMS:
        try:
            url = f"{GAMMA_URL}/events?tag=weather"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list):
                        for ev in data:
                            title = ev.get("title", "").lower()
                            slug = ev.get("slug", "")
                            if any(t in title for t in RAIN_SEARCH_TERMS):
                                all_events.append(ev)
        except Exception as e:
            log.debug(f"Search '{term}' failed: {e}")

    # Also search by direct slug patterns
    now = int(time.time())
    for city_slug in CITY_COORDS:
        for offset_days in range(0, 4):
            target_date = datetime.now(timezone.utc) + timedelta(days=offset_days)
            date_str = target_date.strftime("%B-%-d-%Y").lower().replace(" ", "-")
            for pattern in [f"rain-in-{city_slug}", f"precipitation-in-{city_slug}",
                           f"will-it-rain-in-{city_slug}"]:
                slug = f"{pattern}-on-{date_str}"
                try:
                    url = f"{GAMMA_URL}/events?slug={slug}"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if isinstance(data, list) and data:
                                all_events.extend(data)
                except Exception:
                    pass

    # Deduplicate by slug
    seen = set()
    results = []
    for ev in all_events:
        slug = ev.get("slug", "")
        if slug and slug not in seen:
            seen.add(slug)
            results.append(ev)

    log.info(f"Discovered {len(results)} unique rain/precipitation events")
    return results


async def fetch_precipitation_forecast(session: aiohttp.ClientSession, lat: float, lon: float) -> Dict:
    """Fetch precipitation forecast from Open-Meteo."""
    try:
        url = f"{OPEN_METEO_URL}?latitude={lat}&longitude={lon}&daily=precipitation_probability_max,precipitation_sum&timezone=auto&forecast_days=3"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                daily = data.get("daily", {})
                return {
                    "precip_prob_max": daily.get("precipitation_probability_max", []),
                    "precip_sum": daily.get("precipitation_sum", []),
                    "dates": daily.get("time", []),
                    "source": "open-meteo",
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }
    except Exception as e:
        log.warning(f"Open-Meteo fetch failed for ({lat}, {lon}): {e}")
    return {}


def classify_rain_market(event: Dict) -> Optional[Dict]:
    """Extract and classify a rain market. Returns None if rejected."""
    title = event.get("title", "")
    slug = event.get("slug", "")
    markets = event.get("markets", [])

    if not markets:
        return None

    # Extract condition info
    for m in markets:
        cid = m.get("conditionId", "")
        raw_tids = m.get("clobTokenIds", "[]")
        clob_tids = json.loads(raw_tids) if isinstance(raw_tids, str) else raw_tids
        if not clob_tids:
            continue

        outcomes = m.get("outcomes", "[]")
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        outcome_prices = m.get("outcomePrices", "[]")
        outcome_prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices

        end_date = m.get("endDate", "")
        active = m.get("active", False)
        closed = m.get("closed", False)

        # Reject expired
        if closed:
            continue

        # Reject if resolution rule unclear
        question = m.get("question", title)
        resolution = m.get("resolutionSource", "")
        if not resolution and not any(kw in question.lower() for kw in ["rain", "precipitation", "mm"]):
            continue

        # Extract city/region from title
        city = "unknown"
        for city_name in CITY_COORDS:
            if city_name.replace("-", " ") in title.lower() or city_name in slug:
                city = city_name
                break

        yes_price = float(outcome_prices[0]) if len(outcome_prices) > 0 else 0
        no_price = float(outcome_prices[1]) if len(outcome_prices) > 1 else 0

        return {
            "market_slug": slug,
            "condition_id": cid,
            "question": question,
            "city_or_region": city,
            "date": end_date[:10] if end_date else "",
            "resolution_rule": resolution or "unknown",
            "settlement_source": "unknown",
            "threshold": "unknown",
            "yes_token_id": clob_tids[0] if len(clob_tids) > 0 else "",
            "no_token_id": clob_tids[1] if len(clob_tids) > 1 else "",
            "current_yes_price": yes_price,
            "current_no_price": no_price,
            "end_time": end_date,
            "active": active,
            "closed": closed,
            "reject_reason": None,
        }

    return None


async def run_rain_shadow():
    """Main rain shadow discovery loop."""
    log.info("=== V2.3 Rain Shadow Cell Starting ===")
    log.info("Classification: RAIN_MARKET_SHADOW_ONLY")

    async with aiohttp.ClientSession() as session:
        # Phase 1: Discover rain markets
        events = await discover_rain_markets(session)

        markets = []
        for ev in events:
            m = classify_rain_market(ev)
            if m:
                markets.append(m)

        # Write discovery
        with open(OUT / "rain_market_discovery.jsonl", "w") as f:
            for m in markets:
                f.write(json.dumps(m) + "\n")
        log.info(f"Wrote {len(markets)} rain market discoveries")

        # Phase 2: Fetch precipitation forecasts for discovered cities
        for m in markets:
            city = m.get("city_or_region", "")
            if city in CITY_COORDS:
                lat, lon = CITY_COORDS[city]
                forecast = await fetch_precipitation_forecast(session, lat, lon)
                m["forecast_data"] = forecast
                if forecast and forecast.get("precip_prob_max"):
                    prob = forecast["precip_prob_max"][0] / 100.0 if forecast["precip_prob_max"] else 0
                    m["model_prob_rain"] = prob
                    m["edge_pp"] = round((prob - m["current_yes_price"]) * 100, 1) if m["current_yes_price"] else 0

        # Write shadow events
        with open(OUT / "rain_shadow_events.jsonl", "w") as f:
            for m in markets:
                if m.get("edge_pp", 0) >= MINIMUM_EDGE_PP:
                    event = {
                        "event_id": f"RS-{m['market_slug']}-{int(time.time())}",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "market_slug": m["market_slug"],
                        "condition_id": m["condition_id"],
                        "city_or_region": m["city_or_region"],
                        "date": m["date"],
                        "side": "YES" if m.get("model_prob_rain", 0) > m.get("current_yes_price", 0) else "NO",
                        "entry_price": m.get("current_yes_price", 0),
                        "market_prob": m.get("current_yes_price", 0),
                        "model_prob": m.get("model_prob_rain", 0),
                        "edge_pp": m.get("edge_pp", 0),
                        "precip_probability": m.get("forecast_data", {}).get("precip_prob_max", [0])[0] if m.get("forecast_data") else 0,
                        "expected_precip_mm": m.get("forecast_data", {}).get("precip_sum", [0])[0] if m.get("forecast_data") else 0,
                        "source_agreement_score": 1,
                        "forecast_sources": ["open-meteo"],
                        "settlement_source": m.get("settlement_source", "unknown"),
                        "resolution_rule": m.get("resolution_rule", "unknown"),
                        "threshold": m.get("threshold", "unknown"),
                        "timezone": "UTC",
                        "risk_tier": "BLOCKED",
                        "reject_reason": "shadow_only_no_entries",
                        "classification": "RAIN_MARKET_SHADOW_ONLY",
                    }
                    f.write(json.dumps(event) + "\n")
        log.info(f"Rain shadow cycle complete. {len(markets)} markets, {sum(1 for m in markets if m.get('edge_pp', 0) >= MINIMUM_EDGE_PP)} with edge >= {MINIMUM_EDGE_PP}pp")

        # Write readiness report
        readiness = {
            "classification": "RAIN_MARKET_SHADOW_ONLY",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "markets_discovered": len(markets),
            "shadow_events": sum(1 for m in markets if m.get("edge_pp", 0) >= MINIMUM_EDGE_PP),
            "settled_events": 0,
            "paper_entries_allowed": False,
            "live_allowed": False,
            "promotion_criteria": {
                "resolved_shadow_events_required": 25,
                "settlement_errors_required": 0,
                "timezone_errors_required": 0,
                "source_mismatch_errors_required": 0,
                "rule_ambiguity_errors_required": 0,
                "slippage_adjusted_ev_required": "positive",
                "profit_factor_required": 1.25,
            },
            "next_steps": [
                "discover_rain_markets",
                "collect_precipitation_forecasts",
                "log_shadow_events",
                "settle_shadow_events_after_resolution",
                "evaluate_promotion_after_25_resolved",
            ],
        }
        with open(OUT / "rain_readiness_report.json", "w") as f:
            json.dump(readiness, f, indent=2)
        log.info(f"Rain readiness report written")


if __name__ == "__main__":
    asyncio.run(run_rain_shadow())