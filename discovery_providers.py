#!/usr/bin/env python3
"""V19.7l Discovery Providers — Deterministic slug-based + Gamma API parallel discovery.

The key insight: Polymarket 5m/15m crypto Up/Down markets use deterministic slugs:
  {asset}-updown-{interval}-{unix_timestamp}

Where:
  - asset: btc, eth, sol, xrp, doge (lowercase)
  - interval: 5m, 15m, 1h
  - unix_timestamp: UTC timestamp of the window START

This means we can COMPUTE the exact slug for the next N windows and query directly.

Three providers run in parallel:
  A. SlugProvider (PRIMARY) — computes slugs, queries /events?slug=<slug>
  B. GammaEventsProvider — paginates /events?tag=crypto&active=true
  C. GammaMarketsProvider — paginates /markets?active=true

Tag IDs discovered:
  - btc: 620
  - Bitcoin Prices: 102321
  - Ethereum Prices: 102322
  - XRP: 101267
  - CryptoNewHide: 102165

Two classifier modes:
  - STRICT_EXECUTION: for paper/live trade eligibility
  - DIAGNOSTIC_DISCOVERY: broader acceptance for diagnostic visibility
"""

import json
import math
import os
import re
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

GAMMA = "https://gamma-api.polymarket.com"
UA = "FDC-Discovery/19.7l"

ET_OFFSET = timezone(timedelta(hours=-4))  # EDT (Eastern Daylight Time)
UTC = timezone.utc

# ── Supported assets and intervals ──
SUPPORTED_ASSETS = {
    "BTC": {"name": "Bitcoin", "slug_prefix": "btc"},
    "ETH": {"name": "Ethereum", "slug_prefix": "eth"},
    "SOL": {"name": "Solana", "slug_prefix": "sol"},
    "XRP": {"name": "XRP", "slug_prefix": "xrp"},
}

SUPPORTED_INTERVALS = ["5m", "15m"]

# ── Tag IDs discovered ──
TAG_IDS = {
    "btc": 620,
    "bitcoin_prices": 102321,
    "ethereum_prices": 102322,
    "xrp": 101267,
    "crypto_hide": 102165,
}

# ── Classifier modes ──
STRICT_EXECUTION = "strict_execution"
DIAGNOSTIC_DISCOVERY = "diagnostic_discovery"

# ── Market classification ──
CLASS_ACTIVE_EXECUTABLE = "ACTIVE_EXECUTABLE_NOW"
CLASS_FUTURE_VALID = "FUTURE_VALID_MARKET"
CLASS_EXPIRED = "EXPIRED"
CLASS_CLOSED = "CLOSED"
CLASS_WRONG_INSTRUMENT = "WRONG_INSTRUMENT"
CLASS_AMBIGUOUS = "AMBIGUOUS"

# ── Discovery debug directory ──
DEBUG_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/discovery_debug")
DEBUG_DIR.mkdir(exist_ok=True)


def _get(url, timeout=15):
    """HTTP GET with retry."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except Exception:
            if attempt == 2:
                return None
            time.sleep(0.5 * (attempt + 1))
    return None


# ══════════════════════════════════════════════════════════════════════════════
# PROVIDER A: SlugProvider — Deterministic slug computation
# ══════════════════════════════════════════════════════════════════════════════

def compute_window_timestamps(interval_minutes=5, look_ahead_windows=3):
    """Compute UTC unix timestamps for the next N windows of the given interval.
    
    5m markets use 5-min ET boundaries: :00, :05, :10, :15, :20, :25, :30, :35, :40, :45, :50, :55
    15m markets use 15-min ET boundaries: :00, :15, :30, :45
    """
    now_utc = datetime.now(UTC)
    now_et = now_utc.astimezone(ET_OFFSET)
    
    # Compute current 5-min boundary in ET
    if interval_minutes == 5:
        # Round UP to next 5-min boundary
        minute = now_et.minute
        next_boundary = math.ceil(minute / 5) * 5
        if next_boundary >= 60:
            base_time = now_et.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        else:
            base_time = now_et.replace(minute=next_boundary, second=0, microsecond=0)
        # Also include current window (might still be active)
        current_boundary = math.floor(minute / 5) * 5
        current_window = now_et.replace(minute=current_boundary, second=0, microsecond=0)
    elif interval_minutes == 15:
        minute = now_et.minute
        next_boundary = math.ceil(minute / 15) * 15
        if next_boundary >= 60:
            base_time = now_et.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        else:
            base_time = now_et.replace(minute=next_boundary, second=0, microsecond=0)
        current_boundary = math.floor(minute / 15) * 15
        current_window = now_et.replace(minute=current_boundary, second=0, microsecond=0)
    else:
        return []
    
    timestamps = []
    # Include current window (might still have time left)
    ts_utc = current_window.astimezone(UTC)
    timestamps.append(int(ts_utc.timestamp()))
    
    # Add next N windows
    for i in range(1, look_ahead_windows + 1):
        future_window = current_window + timedelta(minutes=interval_minutes * i)
        ts_utc = future_window.astimezone(UTC)
        timestamps.append(int(ts_utc.timestamp()))
    
    # Deduplicate and sort
    timestamps = sorted(set(timestamps))
    return timestamps


def slug_provider(asset_key="BTC", interval="5m", look_ahead=3):
    """Provider A: Compute deterministic slugs and query Gamma API.
    
    Returns list of market dicts with full metadata, plus provider_name.
    """
    asset_cfg = SUPPORTED_ASSETS.get(asset_key)
    if not asset_cfg:
        return {"provider": "SlugProvider", "markets": [], "raw_count": 0, "error": f"unknown asset {asset_key}"}
    
    prefix = asset_cfg["slug_prefix"]
    slug_interval = interval  # "5m" or "15m"
    interval_min = int(interval.rstrip("m"))
    
    timestamps = compute_window_timestamps(interval_min, look_ahead)
    markets = []
    raw_count = 0
    
    for ts in timestamps:
        slug = f"{prefix}-updown-{slug_interval}-{ts}"
        url = f"{GAMMA}/events?slug={slug}"
        data = _get(url)
        if data is None:
            continue
        raw_count += 1
        
        if isinstance(data, list) and len(data) > 0:
            event = data[0]
            for m in event.get("markets", []):
                m["_provider"] = "SlugProvider"
                m["_slug"] = slug
                m["_event_title"] = event.get("title", "")
                m["_event_slug"] = event.get("slug", "")
                markets.append(m)
    
    return {
        "provider": "SlugProvider",
        "markets": markets,
        "raw_count": raw_count,
        "deduped_count": len(markets),
    }


# ══════════════════════════════════════════════════════════════════════════════
# PROVIDER B: GammaEventsProvider — Paginated event search
# ══════════════════════════════════════════════════════════════════════════════

def gamma_events_provider(max_pages=3, page_size=100):
    """Provider B: Fetch active crypto events from Gamma API.
    
    Searches /events?active=true&closed=false with pagination.
    """
    markets = []
    raw_count = 0
    
    for offset in range(0, max_pages * page_size, page_size):
        url = f"{GAMMA}/events?active=true&closed=false&limit={page_size}&offset={offset}&order=volume&ascending=false"
        data = _get(url)
        if data is None or not isinstance(data, list) or len(data) == 0:
            break
        raw_count += len(data)
        for e in data:
            for m in e.get("markets", []):
                m["_provider"] = "GammaEventsProvider"
                m["_event_title"] = e.get("title", "")
                m["_event_slug"] = e.get("slug", "")
                markets.append(m)
        if len(data) < page_size:
            break
    
    return {
        "provider": "GammaEventsProvider",
        "markets": markets,
        "raw_count": raw_count,
        "deduped_count": len(markets),
    }


# ══════════════════════════════════════════════════════════════════════════════
# PROVIDER C: GammaMarketsProvider — Direct market search
# ══════════════════════════════════════════════════════════════════════════════

def gamma_markets_provider(max_pages=3, page_size=500):
    """Provider C: Fetch active markets directly from Gamma API.
    
    Searches /markets?active=true&closed=false with pagination.
    """
    markets = []
    raw_count = 0
    seen = set()
    
    for offset in range(0, max_pages * page_size, page_size):
        url = f"{GAMMA}/markets?active=true&closed=false&limit={page_size}&offset={offset}&order=volume&ascending=false"
        data = _get(url)
        if data is None or not isinstance(data, list) or len(data) == 0:
            break
        raw_count += len(data)
        for m in data:
            cid = m.get("conditionId", "")
            if cid in seen:
                continue
            seen.add(cid)
            m["_provider"] = "GammaMarketsProvider"
            markets.append(m)
        if len(data) < page_size:
            break
    
    return {
        "provider": "GammaMarketsProvider",
        "markets": markets,
        "raw_count": raw_count,
        "deduped_count": len(markets),
    }


# ══════════════════════════════════════════════════════════════════════════════
# PROVIDER D: TagExplorer — Tag-filtered event queries
# ══════════════════════════════════════════════════════════════════════════════

def tag_explorer(tag_ids=None, max_pages=2):
    """Provider D: Fetch events by tag IDs.
    
    Searches /events?tag_id=<ID>&active=true for crypto-related tags.
    """
    if tag_ids is None:
        tag_ids = list(TAG_IDS.values())
    
    markets = []
    raw_count = 0
    
    for tag_id in tag_ids:
        for offset in range(0, max_pages * 100, 100):
            url = f"{GAMMA}/events?tag_id={tag_id}&active=true&closed=false&limit=100&offset={offset}"
            data = _get(url)
            if data is None or not isinstance(data, list) or len(data) == 0:
                break
            raw_count += len(data)
            for e in data:
                for m in e.get("markets", []):
                    m["_provider"] = "TagExplorer"
                    m["_tag_id"] = tag_id
                    m["_event_title"] = e.get("title", "")
                    markets.append(m)
            if len(data) < 100:
                break
    
    return {
        "provider": "TagExplorer",
        "markets": markets,
        "raw_count": raw_count,
        "deduped_count": len(markets),
    }


# ══════════════════════════════════════════════════════════════════════════════
# TIME WINDOW EXTRACTION — Enhanced for V19.7l
# ══════════════════════════════════════════════════════════════════════════════

def extract_time_window_v2(text):
    """V19.7l: Enhanced time window extraction supporting all formats.
    
    Handles:
    - "9:25AM-9:30AM ET" (compact)
    - "9:25 AM-9:30 AM ET" (spaced)
    - "9:25AM–9:30AM ET" (en-dash)
    - "9:25 AM – 9:30 AM ET" (spaced en-dash)
    - "1:20PM-1:25PM ET"
    - "BTC Up or Down 15m" / "5m" / "15 min" / "15 Minutes"
    - "Bitcoin Up or Down - 5 Minutes" / "15 Minutes"
    - slug contains "5m" or "15m"
    """
    if not text:
        return None
    
    t = text.strip()
    
    # Format 1: Time range with optional spaces and dash types
    # Matches: 9:25AM-9:30AM, 9:25 AM-9:30 AM, 9:25AM–9:30AM, 1:20PM-1:25PM
    m = re.search(
        r'(\d{1,2}):(\d{2})\s*(AM|PM)\s*[-–—]\s*(\d{1,2}):(\d{2})\s*(AM|PM)\s*(?:ET|UTC|EST|EDT)?',
        t, re.I
    )
    if m:
        try:
            sh, sm, sap = int(m.group(1)), int(m.group(2)), m.group(3).upper()
            eh, em, eap = int(m.group(4)), int(m.group(5)), m.group(6).upper()
            if sap == "PM" and sh != 12: sh += 12
            if sap == "AM" and sh == 12: sh = 0
            if eap == "PM" and eh != 12: eh += 12
            if eap == "AM" and eh == 12: eh = 0
            start_min = sh * 60 + sm
            end_min = eh * 60 + em
            if end_min <= start_min: end_min += 24 * 60
            duration = end_min - start_min
            if duration <= 5: return "5m"
            elif duration <= 15: return "15m"
            elif duration <= 60: return "60m"
            else: return f"{duration}m"
        except:
            pass
        return m.group(0).replace(" ", "")
    
    # Format 2: Duration string "5m" / "15m" / "5 min" / "15 Minutes" / "5 minute"
    m = re.search(r'(\d+)\s*(?:min|minute|minutes|m)\b', t, re.I)
    if m:
        dur = int(m.group(1))
        if dur <= 5: return "5m"
        elif dur <= 15: return "15m"
        elif dur <= 60: return "60m"
        return f"{dur}m"
    
    # Format 3: Slug-based interval (e.g., "btc-updown-5m-1780164300")
    m = re.search(r'updown[-_](\d+)m[-_]', t, re.I)
    if m:
        dur = int(m.group(1))
        if dur <= 5: return "5m"
        elif dur <= 15: return "15m"
        return f"{dur}m"
    
    # Format 4: Single time "3:25PM ET"
    m = re.search(r'(\d{1,2})(AM|PM)\s*(?:ET|UTC)', t, re.I)
    if m:
        return m.group(0).replace(" ", "")
    
    return None


def detect_asset_v2(text):
    """V19.7l: Enhanced asset detection from question, title, slug."""
    t = (text or "").lower()
    # Check supported assets
    asset_patterns = {
        "BTC": ["bitcoin", "btc"],
        "ETH": ["ethereum", "eth up", "eth down", "eth-updown", "eth-up", "-eth-"],
        "SOL": ["solana", "sol up", "sol down", "sol-updown", "-sol-"],
        "XRP": ["xrp"],
    }
    for asset, patterns in asset_patterns.items():
        if any(p in t for p in patterns):
            return asset
    return None


# ══════════════════════════════════════════════════════════════════════════════
# CLASSIFIER — STRICT + Diagnostic modes
# ══════════════════════════════════════════════════════════════════════════════

def classify_market_v2(market, mode=STRICT_EXECUTION):
    """V19.7l: Full market classification with strict/diagnostic modes.
    
    Returns dict:
        valid: bool (for execution mode)
        asset: str or None
        interval: str or None (5m/15m)
        direction: str or None (up/down)
        reason: str (rejection reason if not valid)
        market_type: str
        classification: str (ACTIVE_EXECUTABLE_NOW / FUTURE_VALID / EXPIRED / etc.)
        diagnostic_accept: bool (whether diagnostic mode would accept it)
        mins_to_expiry: float
        mins_to_start: float
        starts_at: str
        ends_at: str
    """
    now_utc = datetime.now(UTC)
    result = {
        "valid": False, "asset": None, "interval": None, "direction": None,
        "reason": None, "market_type": "other", "classification": CLASS_AMBIGUOUS,
        "diagnostic_accept": False, "mins_to_expiry": None, "mins_to_start": None,
        "starts_at": None, "ends_at": None,
    }
    
    # Combine all text fields for detection
    question = market.get("question", "") or market.get("title", "")
    slug = market.get("slug", "") or market.get("_event_slug", "")
    event_title = market.get("_event_title", "")
    combined = f"{question} {slug} {event_title}"
    
    if not question and not slug:
        result["reason"] = "no_question"
        return result
    
    result["asset"] = detect_asset_v2(combined)
    
    # Check close/expired status
    closed = market.get("closed", False)
    end_date = market.get("endDate", "")
    start_date = market.get("startDate", "")
    
    if end_date:
        try:
            ed = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
            result["ends_at"] = end_date
            result["mins_to_expiry"] = (ed - now_utc).total_seconds() / 60
        except:
            pass
    
    if start_date:
        try:
            sd = datetime.fromisoformat(str(start_date).replace("Z", "+00:00"))
            result["starts_at"] = start_date
            result["mins_to_start"] = (sd - now_utc).total_seconds() / 60
        except:
            pass
    
    # Classification: EXPIRED
    if result["mins_to_expiry"] is not None and result["mins_to_expiry"] < -1:
        result["classification"] = CLASS_EXPIRED
        result["reason"] = "expired"
        if mode == DIAGNOSTIC_DISCOVERY:
            result["diagnostic_accept"] = True
        return result
    
    # Classification: CLOSED
    if closed and (result["mins_to_expiry"] is None or result["mins_to_expiry"] < 0):
        result["classification"] = CLASS_CLOSED
        result["reason"] = "closed"
        if mode == DIAGNOSTIC_DISCOVERY:
            result["diagnostic_accept"] = True
        return result
    
    # Detect interval
    window = extract_time_window_v2(question) or extract_time_window_v2(slug) or extract_time_window_v2(event_title)
    if window:
        dur_str = window.rstrip("m")
        if dur_str.isdigit():
            dur = int(dur_str)
            if dur <= 5:
                result["interval"] = "5m"
            elif dur <= 15:
                result["interval"] = "15m"
    
    # Check if Up/Down market
    q = question.lower()
    has_up_down = ("up" in q and "down" in q) or ("updown" in q) or ("up or down" in combined.lower())
    has_strike = "$" in q
    has_range = "between" in q and "$" in q
    
    if not has_up_down:
        # In diagnostic mode, also accept if event title/slug indicates up/down
        if mode == DIAGNOSTIC_DISCOVERY and "updown" in slug.lower():
            has_up_down = True
            result["diagnostic_accept"] = True
        else:
            result["classification"] = CLASS_WRONG_INSTRUMENT
            result["reason"] = "no_up_down"
            return result
    
    if has_strike and not has_up_down:
        result["classification"] = CLASS_WRONG_INSTRUMENT
        result["reason"] = "strike_price"
        return result
    
    if has_range:
        result["classification"] = CLASS_WRONG_INSTRUMENT
        result["reason"] = "ladder"
        return result
    
    # Block daily/weekly/monthly
    if not window:
        if any(w in q for w in ["daily", "today", "tonight"]):
            result["reason"] = "daily"
            result["market_type"] = "daily"
            return result
        if any(w in q for w in ["weekly", "this week"]):
            result["reason"] = "weekly"
            result["market_type"] = "weekly"
            return result
        if any(w in q for w in ["monthly", "this month"]):
            result["reason"] = "monthly"
            result["market_type"] = "monthly"
            return result
    
    # Determine direction from outcomes
    outcomes = market.get("outcomes", [])
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except:
            outcomes = []
    if isinstance(outcomes, list) and len(outcomes) >= 2:
        o0 = str(outcomes[0]).lower()
        if "up" in o0:
            result["direction"] = "up_first"
        elif "down" in o0:
            result["direction"] = "down_first"
    
    # Future market classification
    if result["mins_to_start"] is not None and result["mins_to_start"] > 2:
        result["classification"] = CLASS_FUTURE_VALID
        result["valid"] = False  # Not executable NOW
        result["reason"] = "future_market"
        result["market_type"] = f"{result['interval'] or 'unknown'}_binary"
        return result
    
    # Final: is this a valid executable market?
    if result["asset"] and result["interval"] in ("5m", "15m") and has_up_down:
        if not closed and result["mins_to_expiry"] is not None and result["mins_to_expiry"] > 0:
            result["valid"] = True
            result["classification"] = CLASS_ACTIVE_EXECUTABLE
            result["reason"] = None
            result["market_type"] = f"{result['interval']}_binary"
        elif not closed:
            result["classification"] = CLASS_FUTURE_VALID
            result["reason"] = "future_or_ambiguous_timing"
            result["market_type"] = f"{result['interval'] or 'unknown'}_binary"
    elif mode == DIAGNOSTIC_DISCOVERY:
        result["diagnostic_accept"] = True
        if not result["reason"]:
            result["reason"] = "diagnostic_only"
    
    return result


# ══════════════════════════════════════════════════════════════════════════════
# PARALLEL DISCOVERY — Run all providers and merge
# ══════════════════════════════════════════════════════════════════════════════

def discover_markets(asset_key=None, interval=None, look_ahead=3):
    """Run all discovery providers in parallel and return classified results.
    
    Returns dict with:
        - providers: list of provider results
        - valid: list of valid executable markets
        - future: list of future valid markets
        - diagnostic: list of diagnostic-only markets
        - all_raw: all raw markets found
        - deduped: deduplicated market dict
        - bug_detections: list of DISCOVERY_ROUTE_BUG findings
        - comparison: provider comparison report
    """
    results = {
        "providers": [],
        "valid": [],
        "future": [],
        "diagnostic": [],
        "all_raw": [],
        "deduped": {},
        "bug_detections": [],
        "comparison": {},
        "timestamp": datetime.now(UTC).isoformat(),
    }
    
    seen_cids = set()
    
    # ── Provider A: SlugProvider (PRIMARY) ──
    assets = [asset_key] if asset_key else list(SUPPORTED_ASSETS.keys())
    intervals = [interval] if interval else SUPPORTED_INTERVALS
    
    for ak in assets:
        for iv in intervals:
            slug_result = slug_provider(ak, iv, look_ahead)
            results["providers"].append({**slug_result, "asset": ak, "interval": iv})
            for m in slug_result["markets"]:
                cid = m.get("conditionId", "")
                if cid and cid not in seen_cids:
                    seen_cids.add(cid)
                    results["deduped"][cid] = m
                    results["all_raw"].append(m)
    
    # ── Provider B: GammaEventsProvider (1 page for speed) ──
    events_result = gamma_events_provider(max_pages=1, page_size=100)
    results["providers"].append(events_result)
    for m in events_result["markets"]:
        cid = m.get("conditionId", "")
        if cid and cid not in seen_cids:
            seen_cids.add(cid)
            results["deduped"][cid] = m
            results["all_raw"].append(m)
    
    # ── Provider C: GammaMarketsProvider (1 page for speed) ──
    markets_result = gamma_markets_provider(max_pages=1, page_size=100)
    results["providers"].append(markets_result)
    for m in markets_result["markets"]:
        cid = m.get("conditionId", "")
        if cid and cid not in seen_cids:
            seen_cids.add(cid)
            results["deduped"][cid] = m
            results["all_raw"].append(m)
    
    # ── Provider D: TagExplorer ──
    tag_result = tag_explorer(max_pages=1)
    results["providers"].append(tag_result)
    for m in tag_result["markets"]:
        cid = m.get("conditionId", "")
        if cid and cid not in seen_cids:
            seen_cids.add(cid)
            results["deduped"][cid] = m
            results["all_raw"].append(m)
    
    # ── Classify all markets ──
    for cid, m in results["deduped"].items():
        strict = classify_market_v2(m, STRICT_EXECUTION)
        diag = classify_market_v2(m, DIAGNOSTIC_DISCOVERY)
        
        m["_strict"] = strict
        m["_diagnostic"] = diag
        
        # Build contract dict for valid markets
        if strict["valid"]:
            prices_raw = m.get("outcomePrices", "[]")
            try:
                prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            except:
                prices = []
            
            outcomes_raw = m.get("outcomes", "[]")
            try:
                outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            except:
                outcomes = []
            
            up_i, down_i = 0, 1
            if isinstance(outcomes, list) and len(outcomes) >= 2:
                o0 = str(outcomes[0]).lower()
                if "down" in o0 or "no" in o0:
                    up_i, down_i = 1, 0
            
            tokens_raw = m.get("clobTokenIds", "[]")
            try:
                tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
            except:
                tokens = []
            
            contract = {
                "question": m.get("question", ""),
                "conditionId": cid,
                "clobTokenIds": tokens,
                "up_price": float(prices[up_i]) if len(prices) > up_i else 0,
                "down_price": float(prices[down_i]) if len(prices) > down_i else 0,
                "volume": float(m.get("volume", 0)),
                "slug": m.get("_event_slug", m.get("slug", "")),
                "end_date": m.get("endDate", ""),
                "window": strict["interval"],
                "mins_to_expiry": round(strict.get("mins_to_expiry", 0), 1),
                "asset": strict["asset"],
                "interval": strict["interval"],
                "market_type": strict["market_type"],
                "direction_order": strict.get("direction"),
                "_provider": m.get("_provider", ""),
            }
            results["valid"].append(contract)
        
        elif strict["classification"] == CLASS_FUTURE_VALID:
            future_info = {
                "question": m.get("question", ""),
                "asset": strict["asset"],
                "interval": strict["interval"],
                "starts_at": strict.get("starts_at", ""),
                "ends_at": strict.get("ends_at", ""),
                "mins_to_start": round(strict.get("mins_to_start", 0), 1),
                "mins_to_expiry": round(strict.get("mins_to_expiry", 0), 1),
                "conditionId": cid,
                "_provider": m.get("_provider", ""),
            }
            results["future"].append(future_info)
        
        if diag.get("diagnostic_accept") and not strict["valid"]:
            results["diagnostic"].append({
                "question": m.get("question", ""),
                "asset": diag["asset"],
                "interval": diag["interval"],
                "classification": diag["classification"],
                "reason": diag["reason"],
                "_provider": m.get("_provider", ""),
            })
    
    # ── Provider comparison report ──
    slug_valids = sum(1 for m in results["valid"] if m.get("_provider") == "SlugProvider")
    events_valids = sum(1 for m in results["valid"] if m.get("_provider") == "GammaEventsProvider")
    markets_valids = sum(1 for m in results["valid"] if m.get("_provider") == "GammaMarketsProvider")
    tag_valids = sum(1 for m in results["valid"] if m.get("_provider") == "TagExplorer")
    
    results["comparison"] = {
        "SlugProvider": {"valid": slug_valids},
        "GammaEventsProvider": {"valid": events_valids},
        "GammaMarketsProvider": {"valid": markets_valids},
        "TagExplorer": {"valid": tag_valids},
    }
    
    # ── Bug detection ──
    slug_has_candidates = any(
        p["provider"] == "SlugProvider" and len(p["markets"]) > 0
        for p in results["providers"]
    )
    events_has_updown = any(
        p["provider"] == "GammaEventsProvider" and
        any("up or down" in (m.get("question", "") + m.get("_event_title", "")).lower() for m in p["markets"])
        for p in results["providers"]
    )
    markets_has_updown = any(
        p["provider"] == "GammaMarketsProvider" and
        any("up or down" in m.get("question", "").lower() for m in p["markets"])
        for p in results["providers"]
    )
    
    if slug_has_candidates and not events_has_updown and not markets_has_updown:
        results["bug_detections"].append("DISCOVERY_ROUTE_BUG = API_PAGE_MISMATCH")
    elif slug_has_candidates and events_has_updown and not markets_has_updown:
        results["bug_detections"].append("DISCOVERY_ROUTE_BUG = MARKETS_ENDPOINT_MISSING_SHORT_INTERVALS")
    
    # If both providers return 0 crypto Up/Down
    if not slug_has_candidates and not events_has_updown and not markets_has_updown:
        # Check if it's genuinely no markets or classifier too strict
        any_crypto = any(
            any(detect_asset_v2(m.get("question", "") + m.get("_event_title", "")) for m in p["markets"])
            for p in results["providers"] if p["markets"]
        )
        if any_crypto:
            results["bug_detections"].append("DISCOVERY_ROUTE_BUG = CLASSIFIER_TOO_STRICT")
        else:
            results["bug_detections"].append("DISCOVERY_ROUTE_BUG = NO_SHORT_INTERVAL_MARKETS_VISIBLE_TO_API")
    
    if not results["bug_detections"]:
        results["bug_detections"].append("NONE")
    
    return results


def save_discovery_report(results, cycle_num=None):
    """Save discovery report to disk."""
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    
    # Build compact report
    report = {
        "timestamp": results["timestamp"],
        "valid_count": len(results["valid"]),
        "future_count": len(results["future"]),
        "diagnostic_count": len(results["diagnostic"]),
        "total_raw": len(results["all_raw"]),
        "total_deduped": len(results["deduped"]),
        "bug_detections": results["bug_detections"],
        "comparison": results["comparison"],
        "cycle": cycle_num,
    }
    
    # Add valid market summaries
    report["valid_markets"] = [
        {"q": m["question"][:60], "asset": m["asset"], "interval": m["interval"],
         "mins_to_expiry": m["mins_to_expiry"], "cid": m["conditionId"][:20],
         "up": m["up_price"], "down": m["down_price"]}
        for m in results["valid"]
    ]
    
    report["future_markets"] = [
        {"q": m["question"][:60], "asset": m["asset"], "interval": m["interval"],
         "mins_to_start": m["mins_to_start"], "mins_to_expiry": m["mins_to_expiry"]}
        for m in results["future"]
    ]
    
    # Per-provider details
    for p in results["providers"]:
        pname = p["provider"]
        if pname not in report["comparison"]:
            report["comparison"][pname] = {}
        report["comparison"][pname]["raw_count"] = p.get("raw_count", 0)
        report["comparison"][pname]["deduped_count"] = p.get("deduped_count", 0)
    
    # Provider-level rejections
    if results["all_raw"]:
        rejection_counts = defaultdict(int)
        for m in results["all_raw"][:200]:
            cl = m.get("_strict", {})
            reason = cl.get("reason", "unknown")
            rejection_counts[reason] += 1
        report["rejection_reason_counts"] = dict(rejection_counts)
    
    # Bug detections
    report["bug_detections"] = results["bug_detections"]
    
    # Save latest.json
    latest_path = DEBUG_DIR / "latest.json"
    with open(str(latest_path), "w") as f:
        json.dump(report, f, indent=2, default=str)
    
    # Append to history.jsonl
    history_path = DEBUG_DIR / "history.jsonl"
    with open(str(history_path), "a") as f:
        f.write(json.dumps(report, default=str) + "\n")
    
    return report


def maybe_raw_dump(results, consecutive_zero_cycles):
    """If valid_count == 0 for 3+ consecutive cycles, dump raw samples."""
    if consecutive_zero_cycles < 3 or not results["all_raw"]:
        return None
    
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dump_path = DEBUG_DIR / f"raw_zero_valid_dump_{ts}.json"
    
    # First 100 markets with all detected fields
    samples = []
    for m in results["all_raw"][:100]:
        question = m.get("question", "")
        slug = m.get("slug", "") or m.get("_event_slug", "")
        event_title = m.get("_event_title", "")
        
        sample = {
            "question": question,
            "title": m.get("title", ""),
            "slug": slug,
            "eventTitle": event_title,
            "seriesSlug": m.get("events", [{}])[0].get("slug", "") if isinstance(m.get("events"), list) else "",
            "active": m.get("active"),
            "closed": m.get("closed"),
            "endDate": m.get("endDate"),
            "startDate": m.get("startDate"),
            "outcomes": m.get("outcomes"),
            "clobTokenIds_present": bool(m.get("clobTokenIds")),
            "conditionId": m.get("conditionId", "")[:30],
            "outcomePrices": m.get("outcomePrices"),
            "acceptingOrders": m.get("acceptingOrders"),
            "provider": m.get("_provider"),
            "detected_asset": detect_asset_v2(f"{question} {slug} {event_title}"),
            "extracted_window": extract_time_window_v2(question) or extract_time_window_v2(slug),
            "classifier_rejection": m.get("_strict", {}).get("reason"),
        }
        samples.append(sample)
    
    with open(str(dump_path), "w") as f:
        json.dump(samples, f, indent=2, default=str)
    
    return str(dump_path)


# ══════════════════════════════════════════════════════════════════════════════
# PMXT REPLAY MODE — Diagnostic only, does not count toward live readiness
# ══════════════════════════════════════════════════════════════════════════════

def pmxt_replay_discover(parquet_dir="pmxt_data/", max_files=3):
    """DISCOVERY_ONLY_REPLAY: Ingest PMXT historical parquet files for classification testing.
    
    This does NOT count toward live readiness — purely diagnostic.
    """
    import pyarrow.parquet as pq
    
    replay_markets = []
    parquet_dir = Path(parquet_dir)
    
    for pf in sorted(parquet_dir.glob("*.parquet"))[:max_files]:
        try:
            pf_size = pf.stat().st_size
            if pf_size < 100_000:  # Skip stubs
                continue
            table = pq.read_table(str(pf), columns=["condition_id", "asset_id", "timestamp_received"])
            df = table.to_pandas()
            unique_conditions = df["condition_id"].unique()
            for cid in unique_conditions[:20]:
                cid_str = str(cid)[:30] if cid else ""
                subset = df[df["condition_id"] == cid]
                if len(subset) > 0:
                    replay_markets.append({
                        "conditionId": cid_str,
                        "source": "pmxt_replay",
                        "data_points": len(subset),
                        "first_timestamp": str(subset["timestamp_received"].iloc[0]),
                        "last_timestamp": str(subset["timestamp_received"].iloc[-1]),
                    })
        except Exception as ex:
            replay_markets.append({"error": str(ex), "file": pf.name})
    
    return {
        "mode": "DISCOVERY_ONLY_REPLAY",
        "count": len(replay_markets),
        "markets": replay_markets,
        "counts_toward_readiness": False,
    }


# ══════════════════════════════════════════════════════════════════════════════
# TESTS
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("V19.7l DISCOVERY PROVIDERS — TEST RUN")
    print("=" * 60)
    
    results = discover_markets()
    report = save_discovery_report(results, cycle_num=0)
    
    print(f"\nValid markets: {len(results['valid'])}")
    print(f"Future markets: {len(results['future'])}")
    print(f"Diagnostic: {len(results['diagnostic'])}")
    print(f"Bugs: {results['bug_detections']}")
    print(f"\nComparison:")
    for pname, stats in report["comparison"].items():
        print(f"  {pname}: raw={stats.get('raw_count','?')} deduped={stats.get('deduped_count','?')} valid={stats.get('valid',0)}")
    
    for m in results["valid"][:5]:
        print(f"  VALID: {m['asset']} {m['interval']} | {m['question'][:50]} | mins={m['mins_to_expiry']:.0f}")
    
    for m in results["future"][:5]:
        print(f"  FUTURE: {m['asset']} {m['interval']} | {m['question'][:50]} | starts_in={m['mins_to_start']:.0f}m")
    
    # Test time window extraction
    print(f"\nTime window tests:")
    test_cases = [
        "Bitcoin Up or Down - May 30, 9:25AM-9:30AM ET",
        "BTC Up or Down - May 30, 9:25AM–9:30AM ET",
        "Bitcoin Up or Down - 5 Minutes",
        "XRP Up or Down 15m",
        "btc-updown-5m-1780164300",
        "Bitcoin Up or Down - 15 Min",
        "Ethereum Up or Down - May 30, 1:20PM-1:25PM ET",
    ]
    for tc in test_cases:
        w = extract_time_window_v2(tc)
        print(f"  '{tc[:40]}...' → {w}")
    
    print(f"\nReport saved to: {DEBUG_DIR / 'latest.json'}")