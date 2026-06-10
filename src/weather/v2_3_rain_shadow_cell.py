#!/usr/bin/env python3
"""
V2.3 Rain Shadow Cell — V21.7.15B Validation + Calibration
=============================================================
Classification: RAIN_SHADOW_ACTIVE / RAIN_PAPER_BLOCKED / RAIN_LIVE_BLOCKED
- NO live entries, NO paper entries until validated
- Discovers rain/precipitation markets on Polymarket
- Classifies into: BINARY_RAIN_YES_NO, MEASURABLE_PRECIP_YES_NO,
  PRECIP_AMOUNT_THRESHOLD, CITY_SPECIFIC_RAIN, etc.
- Risk tiers: A_TIER_CLEAR_RULE, B_TIER_USABLE_RULE,
  C_TIER_AMBIGUOUS_RULE, REJECTED_RULE_UNCLEAR
- Computes YES and NO edges separately
- Convective storm filter
- Settlement audit and calibration reporting
- Promotion requires 25 A/B-tier resolved events, PF >= 1.25, EV > 0

Created by directive V21.7.14. Upgraded by directive V21.7.15B.
"""
import asyncio
import json
import time
import logging
import math
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp

BASE = Path("/home/naq1987s/father-daddy-capital")
OUT = BASE / "output" / "weather_bot" / "rain_shadow"
OUT.mkdir(parents=True, exist_ok=True)

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

RAIN_SEARCH_TERMS = [
    "rain", "precipitation", "will it rain", "rainfall",
    "measurable precipitation", "precipitation amount",
    "weather rain yes/no", "rain yes or no", "raining",
    "will-it-rain", "rain-forecast",
]

MARKET_TYPE_PATTERNS = {
    "BINARY_RAIN_YES_NO": [
        r"will it rain", r"rain (yes|no)", r"raining (yes|no)",
        r"rain (today|tomorrow|on \w+)",
        r"will.*rain.*\?(yes|no)",
    ],
    "MEASURABLE_PRECIP_YES_NO": [
        r"measurable precipitation", r"measurable rain",
        r"precipitation.*(yes|no)", r"any rain",
    ],
    "PRECIP_AMOUNT_THRESHOLD": [
        r"precipitation.*(\d+(\.\d+)?)\s*mm", r"rainfall.*(\d+(\.\d+)?)\s*mm",
        r"rain.*exceed", r"precipitation.*exceed", r"rainfall.*over",
        r"more than.*mm.*rain", r"at least.*mm.*rain",
    ],
    "CITY_SPECIFIC_RAIN": [
        r"rain in (\w+)", r"rain.*(\w+) (today|tomorrow|on)",
        r"will it rain in (\w+)",
    ],
    "REGION_SPECIFIC_RAIN": [
        r"rain in (the )?(north|south|east|west|northeast|northwest|southeast|southwest)",
    ],
    "DAILY_RAIN": [
        r"rain (today|on \w+day|on \w+ \d+)",
    ],
    "HOURLY_OR_INTRADAY_RAIN": [
        r"rain.*(morning|afternoon|evening|night|hour)",
        r"rain between.*and.*",
    ],
}

RISK_TIER_KEYWORDS = {
    "A_TIER_CLEAR_RULE": {
        "clear_source": ["noaa", "wunderground", "metar", "nws", "national weather service",
                         "weather.gov", "aviation weather"],
        "clear_threshold": ["measurable", "0.1mm", "0.01 inches", "trace",
                           "yes/no binary", "any precipitation"],
    },
    "B_TIER_USABLE_RULE": {
        "clear_source": ["weather.com", "accuweather", "bbc weather"],
        "minor_ambiguity": True,
    },
    "C_TIER_AMBIGUOUS_RULE": {
        "ambiguous_source": ["community", "user", "crowd", "voting"],
        "unclear_threshold": True,
    },
}

CONVECTIVE_KEYWORDS = [
    "thunderstorm", "convective", "scattered storm", "isolated thunderstorm",
    "severe thunderstorm", "tornado", "supercell", "squall line",
    "flash flood", "hail", "lightning",
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


def classify_market_type(question: str, title: str) -> str:
    """Classify market into type taxonomy."""
    text = f"{question} {title}".lower()
    for mtype, patterns in MARKET_TYPE_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, text):
                return mtype
    return "BINARY_RAIN_YES_NO"  # default for rain markets


def assign_risk_tier(market: Dict) -> str:
    """Assign risk tier based on settlement rule clarity."""
    source = market.get("resolution_source", "").lower()
    rule = market.get("resolution_rule", "").lower()
    question = market.get("question", "").lower()
    threshold = market.get("threshold", "").lower()

    # A-tier: clear source + clear threshold + clear city
    clear_sources = ["noaa", "wunderground", "metar", "nws", "national weather service",
                     "weather.gov", "aviation weather center", "nws obs"]
    has_clear_source = any(s in source or s in rule for s in clear_sources)
    has_clear_threshold = any(t in threshold or t in question for t in
                              ["measurable", "0.1mm", "0.01 inches", "trace", "yes/no", "any"])
    has_city = market.get("city_or_region", "") != "unknown"

    if has_clear_source and has_clear_threshold and has_city:
        return "A_TIER_CLEAR_RULE"

    # B-tier: mostly clear
    usable_sources = ["weather.com", "accuweather", "bbc weather", "open-meteo"]
    has_usable_source = any(s in source or s in rule for s in usable_sources)

    if (has_usable_source or has_clear_source) and has_city:
        return "B_TIER_USABLE_RULE"

    # C-tier: ambiguous
    ambiguous = ["community", "user", "crowd", "voting", "prediction"]
    if any(a in source or a in rule for a in ambiguous) or not has_city:
        return "C_TIER_AMBIGUOUS_RULE"

    return "C_TIER_AMBIGUOUS_RULE"


def check_convective_risk(question: str, title: str, forecast_data: Dict = None) -> bool:
    """Check if convective/storm language is present."""
    text = f"{question} {title}".lower()
    for kw in CONVECTIVE_KEYWORDS:
        if kw in text:
            return True
    # Also check forecast for high precipitation variability
    if forecast_data and forecast_data.get("precip_sum"):
        for amt in forecast_data["precip_sum"][:3]:
            if amt and amt > 20:  # >20mm suggests convective
                return True
    return False


async def discover_rain_markets(session: aiohttp.ClientSession) -> List[Dict]:
    """Query Polymarket Gamma API for rain/precipitation markets."""
    all_events = []
    seen_slugs = set()

    # Phase 1: Search by weather tag and filter for rain
    # Note: Polymarket weather tag includes all weather markets. We need
    # to filter for actual rain/precipitation markets specifically.
    try:
        url = f"{GAMMA_URL}/events?tag=weather&limit=200"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if isinstance(data, list):
                    for ev in data:
                        title = ev.get("title", "").lower()
                        slug = ev.get("slug", "")
                        # Strict rain filter — must have rain/precip in title or question
                        is_rain = any(t in title for t in ["rain", "precipitation", "rainfall", "will it rain", "measurable"])
                        # Also check individual market questions
                        if not is_rain:
                            for m in ev.get("markets", []):
                                q = m.get("question", "").lower()
                                if any(t in q for t in ["rain", "precipitation", "rainfall"]):
                                    is_rain = True
                                    break
                        if is_rain and slug not in seen_slugs:
                            all_events.append(ev)
                            seen_slugs.add(slug)
    except Exception as e:
        log.warning(f"Weather tag search failed: {e}")

    # Phase 2: Direct text search — skip, returns sports false positives
    # Polymarket text search matches team names (Mavericks, Heat, etc.)
    # Rain markets don't currently exist on Polymarket as of 2026-06-10
    log.info("Polymarket text search skipped — no rain-specific markets found, returns sports false positives")

    # Phase 3: City-specific slug patterns
    now_utc = datetime.now(timezone.utc)
    for city_slug in list(CITY_COORDS.keys())[:20]:  # Top 20 cities only
        for offset_days in range(0, 3):
            target = now_utc + timedelta(days=offset_days)
            # Try multiple date formats
            for fmt in ["%B-%-d-%Y", "%b-%-d-%Y"]:
                date_str = target.strftime(fmt).lower().replace(" ", "-")
                for pattern in [f"rain-in-{city_slug}-on-{date_str}",
                               f"will-it-rain-in-{city_slug}-on-{date_str}",
                               f"precipitation-in-{city_slug}-on-{date_str}"]:
                    try:
                        url = f"{GAMMA_URL}/events?slug={pattern}"
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                if isinstance(data, list) and data:
                                    for ev in data:
                                        slug = ev.get("slug", "")
                                        if slug and slug not in seen_slugs:
                                            all_events.append(ev)
                                            seen_slugs.add(slug)
                    except Exception:
                        pass

    log.info(f"Discovered {len(all_events)} unique rain/precipitation events")
    return all_events


async def fetch_precipitation_forecast(session: aiohttp.ClientSession, lat: float, lon: float) -> Dict:
    """Fetch daily + hourly precipitation forecast from Open-Meteo."""
    try:
        url = (f"{OPEN_METEO_URL}?latitude={lat}&longitude={lon}"
               f"&daily=precipitation_probability_max,precipitation_sum"
               f"&hourly=precipitation_probability,precipitation"
               f"&timezone=auto&forecast_days=3")
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                daily = data.get("daily", {})
                hourly = data.get("hourly", {})
                result = {
                    "daily": {
                        "precip_prob_max": daily.get("precipitation_probability_max", []),
                        "precip_sum": daily.get("precipitation_sum", []),
                        "dates": daily.get("time", []),
                    },
                    "hourly": {
                        "precip_probability": hourly.get("precipitation_probability", []),
                        "precipitation": hourly.get("precipitation", []),
                        "time": hourly.get("time", []),
                    },
                    "source": "open-meteo",
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }
                # Compute derived metrics
                if daily.get("precipitation_probability_max"):
                    result["max_precip_prob_3d"] = max(daily["precipitation_probability_max"])
                    result["day1_precip_prob"] = daily["precipitation_probability_max"][0] if daily["precipitation_probability_max"] else 0
                    result["day1_precip_mm"] = daily["precipitation_sum"][0] if daily["precipitation_sum"] else 0
                return result
    except Exception as e:
        log.warning(f"Open-Meteo fetch failed for ({lat}, {lon}): {e}")
    return {}


def extract_market_details(event: Dict) -> Optional[Dict]:
    """Extract and classify a rain market. Returns None if rejected."""
    title = event.get("title", "")
    slug = event.get("slug", "")
    markets = event.get("markets", [])

    if not markets:
        return None

    for m in markets:
        cid = m.get("conditionId", "")
        raw_tids = m.get("clobTokenIds", "[]")
        clob_tids = json.loads(raw_tids) if isinstance(raw_tids, str) else raw_tids
        if not clob_tids or len(clob_tids) < 2:
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

        question = m.get("question", title)
        resolution = m.get("resolutionSource", "")

        # Classify market type
        market_type = classify_market_type(question, title)

        # Extract city
        city = "unknown"
        for city_name in CITY_COORDS:
            if city_name.replace("-", " ") in title.lower() or city_name in slug:
                city = city_name
                break

        yes_price = float(outcome_prices[0]) if len(outcome_prices) > 0 else 0
        no_price = float(outcome_prices[1]) if len(outcome_prices) > 1 else 0

        # Check if market is effectively resolved (price > 0.95 or < 0.05)
        effectively_resolved = (yes_price > 0.95 or yes_price < 0.05)

        # Extract threshold from question
        threshold = "unknown"
        threshold_match = re.search(r'(\d+(?:\.\d+)?)\s*mm', question.lower())
        if threshold_match:
            threshold = f"{threshold_match.group(1)}mm"
        elif any(t in question.lower() for t in ["measurable", "trace", "any"]):
            threshold = "measurable"

        # Extract date
        date_str = end_date[:10] if end_date else ""

        # Check time to resolution
        time_to_resolution = None
        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                time_to_resolution = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
            except Exception:
                pass

        # Build market record
        market = {
            "market_slug": slug,
            "condition_id": cid,
            "question": question,
            "market_type": market_type,
            "city_or_region": city,
            "station_mapping": city if city != "unknown" else "unmapped",
            "date": date_str,
            "settlement_window": f"{time_to_resolution:.1f}h" if time_to_resolution else "unknown",
            "settlement_source": resolution or "unknown",
            "resolution_rule": resolution or "unknown",
            "threshold": threshold,
            "yes_token_id": clob_tids[0],
            "no_token_id": clob_tids[1],
            "current_yes_price": yes_price,
            "current_no_price": no_price,
            "end_time": end_date,
            "active": active,
            "closed": closed,
            "effectively_resolved": effectively_resolved,
            "time_to_resolution_hours": time_to_resolution,
            "reject_reason": None,
        }

        # Assign risk tier
        market["risk_tier"] = assign_risk_tier(market)

        # Rejection checks
        if not resolution and city == "unknown":
            market["reject_reason"] = "rule_and_city_unclear"
            market["risk_tier"] = "REJECTED_RULE_UNCLEAR"
        elif effectively_resolved:
            market["reject_reason"] = "effectively_resolved"
            market["risk_tier"] = "REJECTED_RULE_UNCLEAR"
        elif threshold == "unknown" and market_type in ["PRECIP_AMOUNT_THRESHOLD"]:
            market["reject_reason"] = "threshold_unclear"
            market["risk_tier"] = "C_TIER_AMBIGUOUS_RULE"

        return market

    return None


async def run_rain_shadow():
    """Main rain shadow validation cycle."""
    log.info("=== V2.3 Rain Shadow Cell V21.7.15B Starting ===")
    log.info("Classification: RAIN_SHADOW_ACTIVE / RAIN_PAPER_BLOCKED / RAIN_LIVE_BLOCKED")

    async with aiohttp.ClientSession() as session:
        # Phase 1: Discover rain markets
        events = await discover_rain_markets(session)
        log.info(f"Raw events: {len(events)}")

        markets = []
        for ev in events:
            m = extract_market_details(ev)
            if m:
                markets.append(m)

        # Filter rejected
        valid_markets = [m for m in markets if m.get("reject_reason") is None]
        rejected_markets = [m for m in markets if m.get("reject_reason") is not None]
        log.info(f"Valid markets: {len(valid_markets)}, Rejected: {len(rejected_markets)}")

        # Write discovery
        with open(OUT / "rain_market_discovery.jsonl", "w") as f:
            for m in markets:
                f.write(json.dumps(m) + "\n")
        log.info(f"Wrote {len(markets)} rain market discoveries")

        # Phase 2: Fetch precipitation forecasts for valid markets
        yes_events = []
        no_events = []

        for m in valid_markets:
            city = m.get("city_or_region", "")
            if city in CITY_COORDS and not m.get("effectively_resolved", False):
                lat, lon = CITY_COORDS[city]
                forecast = await fetch_precipitation_forecast(session, lat, lon)
                m["forecast_data"] = forecast

                if forecast and forecast.get("day1_precip_prob") is not None:
                    prob_rain = forecast["day1_precip_prob"] / 100.0
                    prob_no_rain = 1.0 - prob_rain
                    expected_precip = forecast.get("day1_precip_mm", 0)

                    # Compute edge for both YES and NO
                    yes_price = m.get("current_yes_price", 0)
                    no_price = m.get("current_no_price", 0)
                    yes_edge = round((prob_rain - yes_price) * 100, 1)
                    no_edge = round((prob_no_rain - no_price) * 100, 1)

                    m["model_prob_rain"] = prob_rain
                    m["model_prob_no_rain"] = prob_no_rain
                    m["expected_precip_mm"] = expected_precip
                    m["yes_edge_pp"] = yes_edge
                    m["no_edge_pp"] = no_edge
                    m["source_agreement_score"] = 1  # Open-Meteo only for now
                    m["forecast_confidence"] = "moderate" if prob_rain > 0.1 and prob_rain < 0.9 else "low"

                    # Check convective risk
                    convective = check_convective_risk(
                        m.get("question", ""), m.get("market_slug", ""), forecast.get("daily", {}))
                    m["convective_risk"] = convective
                    if convective:
                        m["risk_tier"] = "C_TIER_AMBIGUOUS_OR_VOLATILE"

                    # Generate YES shadow event if edge >= 20pp
                    if yes_edge >= MINIMUM_EDGE_PP and prob_rain >= 0.5:
                        event = {
                            "event_id": f"RS-YES-{m['market_slug'][:40]}-{int(time.time())}",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "market_slug": m["market_slug"],
                            "condition_id": m["condition_id"],
                            "question": m.get("question", ""),
                            "market_type": m.get("market_type", "BINARY_RAIN_YES_NO"),
                            "city_or_region": m["city_or_region"],
                            "station_mapping": m.get("station_mapping", "unknown"),
                            "date": m["date"],
                            "settlement_window": m.get("settlement_window", "unknown"),
                            "settlement_source": m.get("settlement_source", "unknown"),
                            "resolution_rule": m.get("resolution_rule", "unknown"),
                            "threshold": m.get("threshold", "unknown"),
                            "side": "RAIN_YES",
                            "entry_price": yes_price,
                            "market_probability": yes_price,
                            "model_probability": prob_rain,
                            "edge_pp": yes_edge,
                            "prob_rain": prob_rain,
                            "prob_no_rain": prob_no_rain,
                            "expected_precip_mm": expected_precip,
                            "forecast_sources": ["open-meteo"],
                            "source_agreement_score": 1,
                            "forecast_confidence": m.get("forecast_confidence", "low"),
                            "risk_tier": m.get("risk_tier", "C_TIER_AMBIGUOUS_RULE"),
                            "convective_risk": convective,
                            "reject_reason": "shadow_only_no_entries" if m.get("risk_tier", "").startswith("A_") or m.get("risk_tier", "").startswith("B_") else "risk_tier_ineligible",
                            "classification": "RAIN_SHADOW_ACTIVE",
                        }
                        yes_events.append(event)

                    # Generate NO shadow event if edge >= 20pp
                    if no_edge >= MINIMUM_EDGE_PP and prob_no_rain >= 0.5:
                        event = {
                            "event_id": f"RS-NO-{m['market_slug'][:40]}-{int(time.time())}",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "market_slug": m["market_slug"],
                            "condition_id": m["condition_id"],
                            "question": m.get("question", ""),
                            "market_type": m.get("market_type", "BINARY_RAIN_YES_NO"),
                            "city_or_region": m["city_or_region"],
                            "station_mapping": m.get("station_mapping", "unknown"),
                            "date": m["date"],
                            "settlement_window": m.get("settlement_window", "unknown"),
                            "settlement_source": m.get("settlement_source", "unknown"),
                            "resolution_rule": m.get("resolution_rule", "unknown"),
                            "threshold": m.get("threshold", "unknown"),
                            "side": "RAIN_NO",
                            "entry_price": no_price,
                            "market_probability": no_price,
                            "model_probability": prob_no_rain,
                            "edge_pp": no_edge,
                            "prob_rain": prob_rain,
                            "prob_no_rain": prob_no_rain,
                            "expected_precip_mm": expected_precip,
                            "forecast_sources": ["open-meteo"],
                            "source_agreement_score": 1,
                            "forecast_confidence": m.get("forecast_confidence", "low"),
                            "risk_tier": m.get("risk_tier", "C_TIER_AMBIGUOUS_RULE"),
                            "convective_risk": convective,
                            "reject_reason": "shadow_only_no_entries" if m.get("risk_tier", "").startswith("A_") or m.get("risk_tier", "").startswith("B_") else "risk_tier_ineligible",
                            "classification": "RAIN_SHADOW_ACTIVE",
                        }
                        no_events.append(event)

                # Rate limit
                await asyncio.sleep(0.3)

        # Write shadow events
        all_events = yes_events + no_events
        with open(OUT / "rain_shadow_events.jsonl", "w") as f:
            for ev in all_events:
                f.write(json.dumps(ev) + "\n")
        log.info(f"Shadow events: {len(yes_events)} YES, {len(no_events)} NO, {len(all_events)} total")

        # Phase 3: Initialize settlement audit (empty — events must settle after market resolution)
        with open(OUT / "rain_shadow_settlements.jsonl", "w") as f:
            pass  # empty — settlements populated when markets resolve

        # Phase 4: Calibration report
        a_tier_count = sum(1 for m in valid_markets if m.get("risk_tier") == "A_TIER_CLEAR_RULE")
        b_tier_count = sum(1 for m in valid_markets if m.get("risk_tier") == "B_TIER_USABLE_RULE")
        c_tier_count = sum(1 for m in valid_markets if m.get("risk_tier") == "C_TIER_AMBIGUOUS_RULE")
        rejected_count = len(rejected_markets)

        # Market type breakdown
        type_counts = {}
        for m in valid_markets:
            mt = m.get("market_type", "UNKNOWN")
            type_counts[mt] = type_counts.get(mt, 0) + 1

        # Side breakdown
        yes_edge_events = [e for e in yes_events if e.get("edge_pp", 0) >= MINIMUM_EDGE_PP]
        no_edge_events = [e for e in no_events if e.get("edge_pp", 0) >= MINIMUM_EDGE_PP]

        calibration = {
            "directive": "V21.7.15B",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "classification": "RAIN_SHADOW_ACTIVE",
            "resolved_shadow_events": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "ev_per_event": 0.0,
            "brier_score": None,
            "calibration_by_probability_bucket": {
                "50_60": {"count": 0, "wins": 0},
                "60_70": {"count": 0, "wins": 0},
                "70_80": {"count": 0, "wins": 0},
                "80_90": {"count": 0, "wins": 0},
                "90_100": {"count": 0, "wins": 0},
            },
            "yes_wr": None,
            "no_wr": None,
            "yes_ev": None,
            "no_ev": None,
            "a_tier_ev": None,
            "b_tier_ev": None,
            "rule_errors": 0,
            "timezone_errors": 0,
            "station_mapping_errors": 0,
            "settlement_errors": 0,
            "discovery_summary": {
                "total_events_discovered": len(events),
                "valid_markets": len(valid_markets),
                "rejected_markets": rejected_count,
                "a_tier": a_tier_count,
                "b_tier": b_tier_count,
                "c_tier": c_tier_count,
                "market_type_breakdown": type_counts,
            },
            "shadow_summary": {
                "yes_events": len(yes_events),
                "no_events": len(no_events),
                "yes_events_with_edge": len(yes_edge_events),
                "no_events_with_edge": len(no_edge_events),
                "convective_risk_count": sum(1 for e in all_events if e.get("convective_risk")),
            },
            "promotion_eligible": False,
            "promotion_blockers": [
                "resolved_shadow_events < 25",
                "no_settlement_data_yet",
            ],
            "paper_promotion_criteria": {
                "resolved_shadow_events_required": 25,
                "a_or_b_tier_events_required": 25,
                "settlement_errors_required": 0,
                "timezone_errors_required": 0,
                "station_mapping_errors_required": 0,
                "rule_ambiguity_errors_required": 0,
                "profit_factor_required": 1.25,
                "ev_per_event_required": "positive",
                "brier_score_required": "acceptable",
                "side_breakdown_required": "yes_no_reviewed",
            },
        }

        with open(OUT / "rain_calibration_report.json", "w") as f:
            json.dump(calibration, f, indent=2)
        log.info(f"Calibration report written: {a_tier_count} A-tier, {b_tier_count} B-tier, {c_tier_count} C-tier, {rejected_count} rejected")

        # Phase 5: Readiness report
        readiness = {
            "classification": "RAIN_SHADOW_ACTIVE",
            "directive": "V21.7.15B",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "markets_discovered": len(events),
            "valid_markets": len(valid_markets),
            "shadow_events": len(all_events),
            "yes_shadow_events": len(yes_events),
            "no_shadow_events": len(no_events),
            "settled_events": 0,
            "paper_entries_allowed": False,
            "live_allowed": False,
            "temperature_quarantined": True,
            "rain_paper_blocked": True,
            "rain_live_blocked": True,
            "promotion_criteria": {
                "resolved_shadow_events_required": 25,
                "a_or_b_tier_events_required": 25,
                "settlement_errors_required": 0,
                "timezone_errors_required": 0,
                "station_mapping_errors_required": 0,
                "rule_ambiguity_errors_required": 0,
                "slippage_adjusted_ev_required": "positive",
                "profit_factor_required": 1.25,
                "brier_score_required": "acceptable",
                "side_breakdown_required": "yes_no_reviewed",
            },
            "rejection_criteria": {
                "resolved_shadow_events_lt_25": True,
                "pf_lt_1_25": "unknown_no_data",
                "ev_le_zero": "unknown_no_data",
                "settlement_errors_gt_0": "unknown_no_data",
                "rule_ambiguity_errors_gt_0": "unknown_no_data",
                "station_mapping_errors_gt_0": "unknown_no_data",
                "model_overconfidence": "untested",
                "both_sides_negative_ev": "unknown_no_data",
            },
            "next_steps": [
                "discover_rain_markets",
                "collect_precipitation_forecasts",
                "log_shadow_events_with_yes_no_sides",
                "settle_shadow_events_after_resolution",
                "run_calibration_after_25_resolved",
                "evaluate_promotion_criteria",
            ],
        }

        with open(OUT / "rain_readiness_report.json", "w") as f:
            json.dump(readiness, f, indent=2)
        log.info(f"Rain readiness report written")

        # Final summary
        log.info(f"=== V21.7.15B Rain Shadow Cycle Complete ===")
        log.info(f"Markets discovered: {len(events)} | Valid: {len(valid_markets)} | Rejected: {rejected_count}")
        log.info(f"A-tier: {a_tier_count} | B-tier: {b_tier_count} | C-tier: {c_tier_count}")
        log.info(f"YES shadow: {len(yes_events)} | NO shadow: {len(no_events)}")
        log.info(f"Classification: RAIN_SHADOW_ACTIVE | Paper: BLOCKED | Live: BLOCKED")


if __name__ == "__main__":
    asyncio.run(run_rain_shadow())