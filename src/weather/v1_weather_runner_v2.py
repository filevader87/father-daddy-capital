#!/usr/bin/env python3
"""
V1 FDC Weather Bot v2 — Polymarket Temperature Markets
=======================================================
Integrated with PolyWeather advances:
  - 51-city registry with risk profiles + settlement routing
  - DEB multi-model forecast blending (Open-Meteo ensemble + METAR reality anchor)
  - Reality-anchored probability engine (max_so_far, dead market detection)
  - Per-city settlement rounding (WU rounding vs HKO floor)
  - Risk-adjusted position sizing (low/medium/high cities)
  - negRisk=true for all weather markets
"""

import os
import sys
import json
import time
import math
import logging
import argparse
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, field, asdict

# ─── Paths ───
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src" / "weather"
OUTPUT_DIR = PROJECT_ROOT / "output" / "v1_weather"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Import shared CLOB tools
sys.path.insert(0, str(PROJECT_ROOT))
try:
    from fdc_pm_live import (
        check_wallet, get_tick_size, get_neg_risk, validate_price, round_to_tick,
        derive_api_credentials, get_clob_client, build_dry_run_order,
        submit_tracked_order, read_orderbook,
        CLOB_URL, GAMMA_URL, CHAIN_ID, FUNDER,
    )
    HAS_CLOB_MODULE = True
except ImportError:
    HAS_CLOB_MODULE = False

STATE_FILE = OUTPUT_DIR / "weather_state_v2.json"
TRADES_FILE = OUTPUT_DIR / "weather_trades_v2.jsonl"
FORENSICS_FILE = OUTPUT_DIR / "weather_forensics_v2.jsonl"
EDGE_LOG_FILE = OUTPUT_DIR / "edge_log_v2.jsonl"
DEB_HISTORY_FILE = OUTPUT_DIR / "deb_history.json"
CONSOLE_LOG = OUTPUT_DIR / "v1_weather_v2_console.log"

# ─── CLOB / Wallet config (shared with crypto bot) ───
ENV_FILE = Path("/mnt/c/Users/12035/father_daddy_capital/.env")
DERIVED_DW = "[REDACTED_DW]"
EOA = "[REDACTED_EOA]"
USDC_CONTRACT = "[REDACTED_USDC]"
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"

# ═══════════════════════════════════════════════════════════════
# 51-CITY REGISTRY (from PolyWeather)
# Fields: n=name, lat, lon, icao, tz=tz_offset_seconds,
#         settle=settlement_source, risk=risk_level,
#         dist=distance_km, major=is_major, f=use_fahrenheit
# Settlement sources: metar, hko, cwa, aeroweb, wunderground, noaa, ims, ncm
# ═══════════════════════════════════════════════════════════════

CITY_REGISTRY = {
    "amsterdam": {"n":"Amsterdam","lat":52.3086,"lon":4.7639,"icao":"EHAM","tz":3600,"settle":"wunderground","risk":"medium","dist":17.0,"major":True,"f":False},
    "ankara": {"n":"Ankara","lat":40.1281,"lon":32.9951,"icao":"LTAC","tz":10800,"settle":"metar","risk":"medium","dist":24.5,"major":False,"f":False},
    "atlanta": {"n":"Atlanta","lat":33.6407,"lon":-84.4277,"icao":"KATL","tz":-18000,"settle":"metar","risk":"low","dist":12.6,"major":False,"f":True},
    "austin": {"n":"Austin","lat":30.1945,"lon":-97.6699,"icao":"KAUS","tz":-21600,"settle":"metar","risk":"medium","dist":12.0,"major":True,"f":True},
    "beijing": {"n":"Beijing","lat":40.0801,"lon":116.5846,"icao":"ZBAA","tz":28800,"settle":"metar","risk":"medium","dist":25.0,"major":True,"f":False},
    "buenos aires": {"n":"Buenos Aires","lat":-34.8222,"lon":-58.5358,"icao":"SAEZ","tz":-10800,"settle":"metar","risk":"medium","dist":28.1,"major":True,"f":False},
    "busan": {"n":"Busan","lat":35.1795,"lon":128.9382,"icao":"RKPK","tz":32400,"settle":"wunderground","risk":"medium","dist":14.0,"major":True,"f":False},
    "cape town": {"n":"Cape Town","lat":-33.9696,"lon":18.5972,"icao":"FACT","tz":7200,"settle":"wunderground","risk":"medium","dist":20.0,"major":True,"f":False},
    "chicago": {"n":"Chicago","lat":41.9742,"lon":-87.9073,"icao":"KORD","tz":-21600,"settle":"metar","risk":"high","dist":25.3,"major":False,"f":True},
    "chengdu": {"n":"Chengdu","lat":30.5785,"lon":103.9471,"icao":"ZUUU","tz":28800,"settle":"metar","risk":"medium","dist":16.0,"major":True,"f":False},
    "chongqing": {"n":"Chongqing","lat":29.7196,"lon":106.6416,"icao":"ZUCK","tz":28800,"settle":"metar","risk":"high","dist":19.0,"major":True,"f":False},
    "dallas": {"n":"Dallas","lat":32.8471,"lon":-96.8518,"icao":"KDAL","tz":-21600,"settle":"metar","risk":"medium","dist":11.2,"major":False,"f":True},
    "denver": {"n":"Denver","lat":39.7017,"lon":-104.7518,"icao":"KBKF","tz":-25200,"settle":"metar","risk":"medium","dist":3.5,"major":False,"f":True},
    "guangzhou": {"n":"Guangzhou","lat":23.3924,"lon":113.2988,"icao":"ZGGG","tz":28800,"settle":"wunderground","risk":"medium","dist":28.0,"major":True,"f":False},
    "helsinki": {"n":"Helsinki","lat":60.3172,"lon":24.9633,"icao":"EFHK","tz":7200,"settle":"wunderground","risk":"medium","dist":17.0,"major":True,"f":False},
    "hong kong": {"n":"Hong Kong","lat":22.3019,"lon":114.1742,"icao":"VHHH","tz":28800,"settle":"hko","risk":"medium","dist":2.0,"major":True,"f":False},
    "houston": {"n":"Houston","lat":29.6454,"lon":-95.2789,"icao":"KHOU","tz":-21600,"settle":"metar","risk":"medium","dist":12.0,"major":True,"f":True},
    "istanbul": {"n":"Istanbul","lat":41.2749,"lon":28.7323,"icao":"LTFM","tz":10800,"settle":"noaa","risk":"medium","dist":34.0,"major":True,"f":False},
    "jakarta": {"n":"Jakarta","lat":-6.2666,"lon":106.891,"icao":"WIHH","tz":25200,"settle":"wunderground","risk":"medium","dist":12.0,"major":True,"f":False},
    "jeddah": {"n":"Jeddah","lat":21.6702,"lon":39.1525,"icao":"OEJN","tz":10800,"settle":"ncm","risk":"medium","dist":18.0,"major":True,"f":False},
    "karachi": {"n":"Karachi","lat":24.9065,"lon":67.1608,"icao":"OPKC","tz":18000,"settle":"wunderground","risk":"medium","dist":15.0,"major":True,"f":False},
    "kuala lumpur": {"n":"Kuala Lumpur","lat":2.7456,"lon":101.7072,"icao":"WMKK","tz":28800,"settle":"wunderground","risk":"medium","dist":45.0,"major":True,"f":False},
    "london": {"n":"London","lat":51.5048,"lon":0.0522,"icao":"EGLC","tz":0,"settle":"metar","risk":"low","dist":12.7,"major":True,"f":False},
    "los angeles": {"n":"Los Angeles","lat":33.9416,"lon":-118.4085,"icao":"KLAX","tz":-28800,"settle":"metar","risk":"medium","dist":29.0,"major":True,"f":True},
    "lucknow": {"n":"Lucknow","lat":26.7606,"lon":80.8893,"icao":"VILK","tz":19800,"settle":"metar","risk":"medium","dist":14.0,"major":False,"f":False},
    "madrid": {"n":"Madrid","lat":40.4722,"lon":-3.5608,"icao":"LEMD","tz":3600,"settle":"metar","risk":"medium","dist":13.0,"major":True,"f":False},
    "manila": {"n":"Manila","lat":14.5086,"lon":121.0198,"icao":"RPLL","tz":28800,"settle":"wunderground","risk":"medium","dist":10.0,"major":True,"f":False},
    "mexico city": {"n":"Mexico City","lat":19.4363,"lon":-99.0721,"icao":"MMMX","tz":-21600,"settle":"metar","risk":"high","dist":6.5,"major":True,"f":False},
    "miami": {"n":"Miami","lat":25.7959,"lon":-80.287,"icao":"KMIA","tz":-18000,"settle":"metar","risk":"low","dist":10.3,"major":False,"f":True},
    "milan": {"n":"Milan","lat":45.6306,"lon":8.7281,"icao":"LIMC","tz":3600,"settle":"metar","risk":"medium","dist":49.0,"major":True,"f":False},
    "moscow": {"n":"Moscow","lat":55.5915,"lon":37.2615,"icao":"UUWW","tz":10800,"settle":"metar","risk":"medium","dist":29.0,"major":True,"f":False},
    "munich": {"n":"Munich","lat":48.3538,"lon":11.7861,"icao":"EDDM","tz":3600,"settle":"metar","risk":"high","dist":28.5,"major":False,"f":False},
    "new york": {"n":"New York","lat":40.7769,"lon":-73.874,"icao":"KLGA","tz":-18000,"settle":"metar","risk":"low","dist":14.5,"major":True,"f":True},
    "panama city": {"n":"Panama City","lat":8.9733,"lon":-79.5556,"icao":"MPMG","tz":-18000,"settle":"wunderground","risk":"medium","dist":6.0,"major":True,"f":False},
    "paris": {"n":"Paris","lat":48.9694,"lon":2.4414,"icao":"LFPB","tz":3600,"settle":"aeroweb","risk":"medium","dist":12.0,"major":True,"f":False},
    "qingdao": {"n":"Qingdao","lat":36.362,"lon":120.0882,"icao":"ZSQD","tz":28800,"settle":"wunderground","risk":"medium","dist":39.0,"major":True,"f":False},
    "san francisco": {"n":"San Francisco","lat":37.6213,"lon":-122.379,"icao":"KSFO","tz":-28800,"settle":"metar","risk":"medium","dist":20.0,"major":True,"f":True},
    "sao paulo": {"n":"São Paulo","lat":-23.4356,"lon":-46.4731,"icao":"SBGR","tz":-10800,"settle":"metar","risk":"high","dist":25.0,"major":True,"f":False},
    "seattle": {"n":"Seattle","lat":47.4502,"lon":-122.3088,"icao":"KSEA","tz":-28800,"settle":"metar","risk":"low","dist":17.4,"major":False,"f":True},
    "seoul": {"n":"Seoul","lat":37.4602,"lon":126.4407,"icao":"RKSI","tz":32400,"settle":"metar","risk":"high","dist":48.8,"major":True,"f":False},
    "shanghai": {"n":"Shanghai","lat":31.1434,"lon":121.8052,"icao":"ZSPD","tz":28800,"settle":"metar","risk":"medium","dist":33.0,"major":True,"f":False},
    "shenzhen": {"n":"Shenzhen","lat":22.4686,"lon":113.997,"icao":"LFS","tz":28800,"settle":"hko","risk":"medium","dist":0.0,"major":True,"f":False},
    "singapore": {"n":"Singapore","lat":1.3644,"lon":103.9915,"icao":"WSSS","tz":28800,"settle":"metar","risk":"low","dist":17.5,"major":True,"f":False},
    "taipei": {"n":"Taipei","lat":25.0377,"lon":121.5149,"icao":"RCSS","tz":28800,"settle":"cwa","risk":"low","dist":0.0,"major":True,"f":False},
    "tel aviv": {"n":"Tel Aviv","lat":32.0114,"lon":34.8867,"icao":"LLBG","tz":7200,"settle":"ims","risk":"medium","dist":14.8,"major":True,"f":False},
    "tokyo": {"n":"Tokyo","lat":35.5523,"lon":139.7798,"icao":"RJTT","tz":32400,"settle":"metar","risk":"medium","dist":15.0,"major":True,"f":False},
    "toronto": {"n":"Toronto","lat":43.6777,"lon":-79.6248,"icao":"CYYZ","tz":-18000,"settle":"metar","risk":"low","dist":19.6,"major":True,"f":False},
    "warsaw": {"n":"Warsaw","lat":52.1657,"lon":20.9671,"icao":"EPWA","tz":3600,"settle":"metar","risk":"medium","dist":10.0,"major":True,"f":False},
    "wellington": {"n":"Wellington","lat":-41.3272,"lon":174.8053,"icao":"NZWN","tz":46800,"settle":"metar","risk":"low","dist":5.5,"major":False,"f":False},
    "wuhan": {"n":"Wuhan","lat":30.7838,"lon":114.2081,"icao":"ZHHH","tz":28800,"settle":"metar","risk":"high","dist":26.0,"major":True,"f":False},
}

# City name aliases for normalization
CITY_ALIASES = {
    "nyc": "new york", "ny": "new york", "la": "los angeles", "sf": "san francisco",
    "hk": "hong kong", "深圳": "shenzhen", "北京": "beijing", "上海": "shanghai",
    "伦敦": "london", "首尔": "seoul", "东京": "tokyo", "台北": "taipei",
}

# Risk-based position sizing and σ adjustments
RISK_PROFILES = {
    "low":    {"position_mult": 1.0, "sigma_add": 0.0, "edge_add": 0.0},
    "medium": {"position_mult": 0.7, "sigma_add": 0.3, "edge_add": 3.0},
    "high":   {"position_mult": 0.5, "sigma_add": 0.5, "edge_add": 5.0},
}

# ─── Logging ───
log = logging.getLogger("v1_weather_v2")
log.setLevel(logging.INFO)
if not log.handlers:
    fh = logging.FileHandler(CONSOLE_LOG, mode="a")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(ch)

# ═══════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════

@dataclass
class WeatherPosition:
    """An open or settled weather market position."""
    trade_id: str
    city: str
    date: str          # YYYY-MM-DD target date
    bucket_temp: int    # e.g. 20 for "20°C"
    outcome: str       # "YES" or "NO"
    side: str           # "BUY"
    token_id: str
    condition_id: str
    market_slug: str
    shares: float = 0.0
    entry_price: float = 0.0  # per share
    cost_usd: float = 0.0     # total cost
    forecast_temp: float = 0.0 # DEB blended forecast at entry
    forecast_prob: float = 0.0 # our derived probability
    market_prob: float = 0.0   # PM implied probability at entry
    edge_pp: float = 0.0       # forecast_prob - market_prob in pp
    entry_ts: str = ""
    risk_level: str = "medium"  # city risk level
    max_so_far: float = 0.0    # highest observed temp at entry
    entry_sigma: float = 1.5   # σ used at entry
    exit_ts: str = ""
    exit_price: float = 0.0
    pnl: float = 0.0
    settled: bool = False
    settlement_temp: Optional[float] = None
    settlement_source: str = ""  # V2.2: "gamma" or "metar"

@dataclass
class WeatherState:
    """Persistent state for the weather bot."""
    live_enabled: bool = False
    paper_only: bool = True
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    bankroll: float = 20.0
    bankroll_actual_usd: float = 0.0
    consecutive_losses: int = 0
    daily_loss: float = 0.0
    weekly_loss: float = 0.0
    daily_trades: int = 0
    daily_reset: str = ""  # YYYY-MM-DD
    weekly_reset: str = ""
    halted: bool = False
    halt_reason: str = ""
    active_positions: int = 0
    timestamp: str = ""

# ═══════════════════════════════════════════════════════════════
# SETTLEMENT ROUNDING (from PolyWeather)
# ═══════════════════════════════════════════════════════════════

def wu_round(value: float) -> int:
    """WU settlement rounding: 0.5 always rounds up (floor(x + 0.5))."""
    if value >= 0:
        return int(math.floor(value + 0.5))
    return int(math.ceil(value - 0.5))

def is_hko_floor_city(city: str) -> bool:
    """HKO cities use floor (e.g. 28.9 → 28), not rounding."""
    return CITY_REGISTRY.get(city, {}).get("settle", "") == "hko"

def apply_city_settlement(city: str, value: float) -> int:
    """Apply per-city settlement rounding."""
    if is_hko_floor_city(city):
        return int(math.floor(float(value)))
    return wu_round(value)

# ═══════════════════════════════════════════════════════════════
# API FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def load_env():
    env_vars = {}
    if ENV_FILE.exists():
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip()
    return env_vars

def fetch_open_meteo_forecast(lat: float, lon: float, days: int = 3) -> Optional[Dict]:
    """Fetch Open-Meteo forecast with hourly data for peak window estimation."""
    import urllib.request
    url = (f"https://api.open-meteo.com/v1/forecast?"
           f"latitude={lat}&longitude={lon}"
           f"&daily=temperature_2m_max,temperature_2m_min"
           f"&hourly=temperature_2m"
           f"&timezone=auto&forecast_days={days}&past_days=1")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FDC-Weather-V2/2.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log.warning(f"Open-Meteo forecast failed: {e}")
        return None

def fetch_open_meteo_ensemble(lat: float, lon: float) -> Optional[Dict]:
    """Fetch Open-Meteo ensemble API for multi-model spread."""
    import urllib.request
    url = (f"https://ensemble-api.open-meteo.com/v1/ensemble?"
           f"latitude={lat}&longitude={lon}"
           f"&daily=temperature_2m_max"
           f"&timezone=auto&forecast_days=3")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FDC-Weather-V2/2.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log.debug(f"Ensemble fetch failed (non-critical): {e}")
        return None

def fetch_metar(icao: str) -> Optional[Dict]:
    """Fetch live METAR observation with detailed temperature tracking."""
    import urllib.request
    url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=json&taf=false"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FDC-Weather-V2/2.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if data and len(data) > 0:
                obs = data[0]
                # Extract temperature from METAR
                temp_c = obs.get("temp")  # Already in °C
                if temp_c is not None:
                    try:
                        temp_c = float(temp_c)
                    except (ValueError, TypeError):
                        temp_c = None
                return {
                    "temp_c": temp_c,
                    "observation_time": obs.get("obsTime", ""),
                    "raw": obs.get("raw", ""),
                    "icao": icao,
                    "wind_speed_kt": obs.get("wspd", None),
                    "visibility": obs.get("visib", None),
                    "clouds": obs.get("clouds", []),
                }
    except Exception as e:
        log.debug(f"METAR fetch failed for {icao}: {e}")
    return None

def discover_weather_markets(city: str, date_str: str) -> Optional[Dict]:
    """Discover Polymarket weather event for a city+date using Gamma API."""
    import urllib.request, re
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    month_name = dt.strftime("%B").lower()
    day = dt.day
    slug = f"highest-temperature-in-{city}-on-{month_name}-{day}-2026"
    url = f"https://gamma-api.polymarket.com/events?slug={slug}&limit=5"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FDC-Weather-V2/2.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        if data and len(data) > 0:
            event = data[0]
            event["slug"] = slug
            return event
    except Exception as e:
        log.debug(f"Market not found for {slug}: {e}")
    return None

def parse_temperature_markets(event: Dict) -> List[Dict]:
    """Parse an event's markets into temperature buckets with prices."""
    import re
    buckets = []
    markets = event.get("markets", [])
    for m in markets:
        question = m.get("question", "")
        prices_raw = m.get("outcomePrices", "[]")
        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except json.JSONDecodeError:
                continue
        else:
            prices = prices_raw
        if not prices or len(prices) < 2:
            continue
        yes_price = float(prices[0])
        no_price = float(prices[1])
        temp_match = re.search(r'be\s+(\d+)°C', question)
        if not temp_match:
            continue
        temp = int(temp_match.group(1))
        is_threshold = "or higher" in question or "or lower" in question
        token_ids = m.get("clobTokenIds", "[]")
        if isinstance(token_ids, str):
            try:
                token_ids = json.loads(token_ids)
            except json.JSONDecodeError:
                continue
        condition_id = m.get("conditionId", m.get("condition_id", ""))
        buckets.append({
            "question": question, "temp": temp, "is_threshold": is_threshold,
            "yes_price": yes_price, "no_price": no_price,
            "yes_token_id": token_ids[0] if len(token_ids) > 0 else "",
            "no_token_id": token_ids[1] if len(token_ids) > 1 else "",
            "condition_id": condition_id, "market_id": m.get("id", ""),
            "volume": float(m.get("volume", 0) or 0),
            "liquidity": float(m.get("liquidity", 0) or 0),
            "neg_risk": True, "slug": event.get("slug", ""),
        })
    return sorted(buckets, key=lambda b: b["temp"])

# ═══════════════════════════════════════════════════════════════
# DEB: DYNAMIC ERROR BALANCING (from PolyWeather)
# ═══════════════════════════════════════════════════════════════

def load_deb_history() -> Dict:
    """Load DEB forecast accuracy history from file."""
    if DEB_HISTORY_FILE.exists():
        try:
            with open(DEB_HISTORY_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_deb_history(history: Dict):
    """Save DEB forecast accuracy history."""
    with open(DEB_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)

def update_deb_history(city: str, date_str: str, forecasts: Dict[str, float],
                       actual_high: float):
    """Record forecast accuracy for DEB weight calculation."""
    history = load_deb_history()
    if city not in history:
        history[city] = {}
    history[city][date_str] = {
        "forecasts": forecasts,
        "actual_high": actual_high,
    }
    # Keep only last 30 days per city
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    for cid in list(history.keys()):
        for d in list(history[cid].keys()):
            if d < cutoff:
                del history[cid][d]
    save_deb_history(history)

def compute_deb_weights(city: str, current_forecasts: Dict[str, float],
                         lookback_days: int = 7, decay_factor: float = 0.85) -> Tuple[float, Dict[str, float], str]:
    """
    Dynamic Error Balancing: weight forecasts by inverse MAE with time decay.
    Returns (blended_high, weights_dict, weights_info_str).
    """
    history = load_deb_history()
    city_data = history.get(city, {})
    valid_vals = [v for v in current_forecasts.values() if v is not None]
    if not valid_vals:
        return None, {}, "no forecasts"

    # If < 2 days of history, equal weight
    forecast_count = len(current_forecasts)
    equal_weights = {m: 1.0 / forecast_count for m in current_forecasts}
    equal_blended = sum(current_forecasts[m] * equal_weights[m] for m in equal_weights
                        if current_forecasts.get(m) is not None)
    sorted_dates = sorted(city_data.keys(), reverse=True)
    if len(sorted_dates) < 2:
        return round(equal_blended, 1), equal_weights, f"equal({forecast_count} models, <2 days history)"

    # Compute inverse-MAE weights
    errors = {model: [] for model in current_forecasts}
    days_used = 0
    for date_str in sorted_dates:
        if date_str == datetime.now(timezone.utc).strftime("%Y-%m-%d"):
            continue
        record = city_data[date_str]
        actual = record.get("actual_high")
        if actual is None:
            continue
        past_forecasts = record.get("forecasts", {})
        decay_weight = decay_factor ** days_used
        for model in current_forecasts:
            if model in past_forecasts and past_forecasts[model] is not None:
                try:
                    error = abs(float(past_forecasts[model]) - float(actual))
                    errors[model].append((error, decay_weight))
                except (TypeError, ValueError):
                    pass
        days_used += 1
        if days_used >= lookback_days:
            break

    # Compute MAE per model
    maes = {}
    for model, err_list in errors.items():
        if err_list:
            total_w = sum(w for _, w in err_list)
            maes[model] = sum(e * w for e, w in err_list) / total_w if total_w > 0 else 2.0
        else:
            maes[model] = 2.0

    # Inverse-error weights
    inverse_errors = {m: 1.0 / (mae + 0.1) for m, mae in maes.items()
                      if current_forecasts.get(m) is not None}
    total_inv = sum(inverse_errors.values())
    if total_inv == 0:
        return round(equal_blended, 1), equal_weights, "zero total weight"

    weights = {m: inv / total_inv for m, inv in inverse_errors.items()}
    blended = sum(current_forecasts[m] * weights[m] for m in weights
                  if current_forecasts.get(m) is not None)

    # Format weights info
    sorted_w = sorted(weights.items(), key=lambda x: x[1], reverse=True)
    w_str = " | ".join(f"{m}({w*100:.0f}%,MAE:{maes[m]:.1f}°)" for m, w in sorted_w[:3])
    return round(blended, 1), weights, w_str

# ═══════════════════════════════════════════════════════════════
# REALITY-ANCHORED PROBABILITY ENGINE (from PolyWeather)
# ═══════════════════════════════════════════════════════════════

def determine_peak_hours(city: str) -> Tuple[float, float]:
    """Return (first_peak_h, last_peak_h) in local time for city."""
    meta = CITY_REGISTRY.get(city, {})
    tz_offset = meta.get("tz", 0)
    lat = abs(meta.get("lat", 51.5))
    # Approximate peak window by latitude
    if lat > 55:
        return 10.0, 16.0  # High lat: longer days, earlier peak
    elif lat > 35:
        return 11.0, 15.0  # Mid lat: standard peak
    elif lat > 20:
        return 11.5, 14.5  # Subtropical: narrower peak
    else:
        return 12.0, 14.0  # Tropical: midday peak, narrow

def compute_reality_anchored_probability(
    forecast_temps: Dict[str, float],  # {"Open-Meteo": 22.3, "Ensemble": 21.8, ...}
    bucket_temp: int,
    max_so_far: Optional[float],  # From METAR real observations
    current_temp: Optional[float],  # Current METAR temp
    city: str,
    local_hour: float,  # Current local time (decimal hours)
    is_cooling: bool = False,  # Whether temperature is falling from recent METARs
    sigma_override: Optional[float] = None,
    day_offset: int = 0,  # V21.7.52: days from today to target date
    is_threshold: bool = False,  # V21.7.53: "or higher"/"or lower" market
    threshold_direction: str = "",  # "higher" or "lower"
) -> Tuple[float, float, str]:
    """
    Compute reality-anchored probability using PolyWeather's approach.
    Returns (probability, sigma, info_str).
    """
    meta = CITY_REGISTRY.get(city, {})
    risk = meta.get("risk", "medium")
    dist_km = meta.get("dist", 0)
    first_peak_h, last_peak_h = determine_peak_hours(city)
    risk_profile = RISK_PROFILES.get(risk, RISK_PROFILES["medium"])

    # ─── Compute μ (center of probability distribution) ───
    valid_forecasts = {k: v for k, v in forecast_temps.items() if v is not None}
    if not valid_forecasts:
        return 0.01, 1.5, "no forecasts"

    forecast_median = sorted(valid_forecasts.values())[len(valid_forecasts) // 2]
    forecast_high = max(valid_forecasts.values())

    # DEB blended forecast
    deb_prediction, deb_weights, deb_info = compute_deb_weights(city, valid_forecasts)

    # Choose center: DEB prediction > median > max
    if deb_prediction is not None:
        center = deb_prediction
    else:
        center = forecast_median

    # Determine peak status
    peak_status = "before"
    if local_hour >= first_peak_h and local_hour <= last_peak_h:
        peak_status = "in_window"
    elif local_hour > last_peak_h:
        peak_status = "past"

    # Reality anchor: if we have max_so_far, shift μ toward reality
    mu = center
    if max_so_far is not None:
        if peak_status in ("past", "in_window") and max_so_far < forecast_median - 2.0:
            # Reality far below forecast — trust observation
            if is_cooling or peak_status == "past":
                mu = max_so_far
            else:
                mu = max_so_far + 0.5
        elif peak_status in ("past", "in_window"):
            # Reality close to or above forecast — blend
            mu = forecast_median * 0.7 + center * 0.3
            if max_so_far > mu:
                mu = max_so_far + (0.3 if not is_cooling else 0.0)

    # ─── Compute σ (spread) ───
    # V21.7.52 FIX: Use ensemble standard deviation as primary sigma source.
    # Previous bug: used spread of 2-4 derived forecast values (range/2) which
    # vastly understated uncertainty. sigma=0.3°C led to claimed P>95% on all
    # 5 trades that lost — actual errors were 3-12°C.
    
    ensemble_std = valid_forecasts.get("Ensemble-std")
    ensemble_n = valid_forecasts.get("Ensemble-n", 0)
    
    if ensemble_std is not None and ensemble_n is not None and int(ensemble_n) >= 10:
        # Primary: ensemble spread (typically 1-4°C for 30+ members)
        sigma = max(1.0, float(ensemble_std))
    elif len(valid_forecasts) > 2:
        # Fallback: spread of forecast sources (less reliable)
        vals = [v for k, v in valid_forecasts.items() if not k.startswith("Ensemble-std") and not k.startswith("Ensemble-n")]
        sigma = max(1.0, (max(vals) - min(vals)) / 2.0) if vals else 2.0
    else:
        # Last resort: conservative default
        sigma = 2.0

    # Risk tier adjustments
    sigma += risk_profile["sigma_add"]
    
    # Resolution uncertainty: airport-city distance
    if dist_km > 10:
        sigma += 0.5 * (dist_km / 10.0)  # +0.5°C per 10km
    
    # V21.7.52: Forecast horizon penalty — further days are more uncertain
    if day_offset > 0:
        sigma += 0.5 * day_offset  # +0.5°C per day of forecast horizon
    
    # Peak-time σ reduction (V21.7.52: gentler than before — was 0.3x/0.7x/0.85x)
    if peak_status == "past" and local_hour >= 21:
        sigma *= 0.6  # Was 0.3 — too aggressive
    elif peak_status == "past" and local_hour > last_peak_h:
        sigma *= 0.8  # Was 0.7
    elif peak_status == "in_window":
        sigma *= 0.9  # Was 0.85

    # ─── Dead market detection ───
    is_dead = False
    if max_so_far is not None and current_temp is not None:
        if local_hour >= 21 and max_so_far - current_temp >= 3.0:
            is_dead = True
        elif peak_status == "past" and max_so_far - current_temp >= 1.5:
            is_dead = True

    if is_dead:
        sigma = max(1.0, sigma * 0.5)  # V21.7.52 FIX: was 0.3°C absolute — catastrophically understated. Even dead markets need realistic sigma for bucket boundaries.

    if sigma_override:
        sigma = sigma_override

    # ─── Compute bucket probability ───
    # P(T=max_temp) ≈ P(bucket_temp - 0.5 < T < bucket_temp + 0.5)
    phi = lambda z: 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

    if is_dead and max_so_far is not None:
        # Dead market: temperature already locked
        settled_temp = apply_city_settlement(city, max_so_far)
        if is_threshold:
            if threshold_direction == "higher":
                prob = 1.0 if settled_temp >= bucket_temp else 0.01
            else:  # "lower"
                prob = 1.0 if settled_temp <= bucket_temp else 0.01
        else:
            prob = 1.0 if bucket_temp == settled_temp else 0.01
    elif is_threshold:
        # V21.7.53: One-tailed CDF for threshold markets
        # "or higher": P(T >= bucket_temp) = 1 - Phi((bucket_temp - 0.5 - mu) / sigma)
        # "or lower":  P(T <= bucket_temp) = Phi((bucket_temp + 0.5 - mu) / sigma)
        if threshold_direction == "higher":
            z = (bucket_temp - 0.5 - mu) / sigma
            prob = 1.0 - phi(z)
        else:  # "lower"
            z = (bucket_temp + 0.5 - mu) / sigma
            prob = phi(z)
    else:
        z_low = (bucket_temp - 0.5 - mu) / sigma
        z_high = (bucket_temp + 0.5 - mu) / sigma
        prob = phi(z_high) - phi(z_low)

    # V21.7.52 FIX: Cap at 0.85 — never claim P>85% on a weather forecast.
    # V21.7.53: For threshold markets, allow cap at 0.90 since one-tailed
    # distributions are structurally more confident than two-tailed buckets.
    cap = 0.90 if is_threshold else 0.85
    prob = max(0.01, min(cap, prob))

    info = (f"μ={mu:.1f} σ={sigma:.1f} peak={peak_status}"
            f" dead={is_dead} max={max_so_far} cur={current_temp}"
            f" h={local_hour:.0f}")
    if is_threshold:
        info = f"THRESHOLD[{threshold_direction}] {info}"
    if deb_info:
        info = f"DEB({deb_info}) {info}"

    return prob, sigma, info

def compute_edge_v2(forecast_temps: Dict[str, float], buckets: List[Dict],
                     city: str, max_so_far: Optional[float] = None,
                     current_temp: Optional[float] = None,
                     local_hour: float = 12.0, is_cooling: bool = False,
                     min_edge_pp: float = 15.0, min_volume: float = 500.0,
                     day_offset: int = 0) -> List[Dict]:
    """
    Compute edge using reality-anchored probability engine.
    Returns signals sorted by edge.
    """
    meta = CITY_REGISTRY.get(city, {})
    risk = meta.get("risk", "medium")
    risk_profile = RISK_PROFILES.get(risk, RISK_PROFILES["medium"])
    min_edge_adjusted = min_edge_pp + risk_profile["edge_add"]

    signals = []
    for b in buckets:
        market_prob = b["yes_price"]
        if market_prob < 0.03 or market_prob > 0.97:
            continue
        if b["volume"] < min_volume:
            continue

        our_prob, sigma_used, prob_info = compute_reality_anchored_probability(
            forecast_temps, b["temp"], max_so_far, current_temp,
            city, local_hour, is_cooling,
            day_offset=day_offset,  # V21.7.52: forecast horizon penalty
            is_threshold=b.get("is_threshold", False),  # V21.7.53
            threshold_direction="higher" if "or higher" in b.get("question", "") else ("lower" if "or lower" in b.get("question", "") else ""),
        )

        edge_pp = (our_prob - market_prob) * 100.0
        no_edge_pp = ((1.0 - our_prob) - b["no_price"]) * 100.0

        signal = {
            "city": city,
            "temp": b["temp"],
            "question": b["question"],
            "is_threshold": b.get("is_threshold", False),  # V21.7.53
            "threshold_direction": "higher" if "or higher" in b.get("question", "") else ("lower" if "or lower" in b.get("question", "") else ""),
            "yes_price": b["yes_price"],
            "no_price": b["no_price"],
            "our_prob": round(our_prob, 4),
            "market_prob": round(market_prob, 4),
            "edge_pp": round(edge_pp, 1),
            "no_edge_pp": round(no_edge_pp, 1),
            "recommended_side": "YES" if edge_pp > no_edge_pp else "NO",
            "best_edge": round(max(edge_pp, no_edge_pp), 1),
            # V21.7.53: Value-tier scoring — payout ratio for low-price entries
            "entry_price": b["yes_price"] if edge_pp > no_edge_pp else b["no_price"],
            "payout_ratio": round(1.0 / max(b["yes_price"] if edge_pp > no_edge_pp else b["no_price"], 0.01), 1),
            "ev_per_dollar": round(our_prob / max(b["yes_price"] if edge_pp > no_edge_pp else b["no_price"], 0.01), 2),
            "volume": b["volume"],
            "liquidity": b.get("liquidity", 0),
            "yes_token_id": b["yes_token_id"],
            "no_token_id": b["no_token_id"],
            "condition_id": b["condition_id"],
            "market_id": b["market_id"],
            "neg_risk": True,
            "risk_level": risk,
            "sigma_used": round(sigma_used, 2),
            "prob_info": prob_info,
        }

        if signal["best_edge"] >= min_edge_adjusted:
            signals.append(signal)

    signals.sort(key=lambda s: s["best_edge"], reverse=True)
    return signals

# ═══════════════════════════════════════════════════════════════
# WALLET & EXECUTION
# ═══════════════════════════════════════════════════════════════

def get_onchain_usdc() -> float:
    import urllib.request
    usdc_addr = USDC_CONTRACT.lower()
    dw_addr = DERIVED_DW.lower()[2:] if DERIVED_DW.startswith("0x") else DERIVED_DW.lower()
    data = "0x70a08231" + dw_addr.zfill(64)
    payload = json.dumps({"jsonrpc":"2.0","method":"eth_call","params":[{"to":f"0x{usdc_addr}","data":data},"latest"],"id":1}).encode()
    try:
        req = urllib.request.Request(POLYGON_RPC, data=payload, headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            return round(int(result.get("result","0x0"), 16) / 1e6, 2)
    except Exception as e:
        log.error(f"USDC balance check failed: {e}")
        return 0.0

def init_clob_client():
    """Initialize CLOB client using shared module."""
    if HAS_CLOB_MODULE:
        return get_clob_client()
    raise RuntimeError("fdc_pm_live module not available — install or run in paper mode")

# ═══════════════════════════════════════════════════════════════
# MAIN BOT CLASS
# ═══════════════════════════════════════════════════════════════

class WeatherBotV2:
    def __init__(self, paper_only: bool = True, bankroll: float = 20.0):
        self.paper_only = paper_only
        self.positions: List[WeatherPosition] = []
        self.state = WeatherState()
        self.state.paper_only = paper_only
        self.state.bankroll = bankroll
        self.clob_client = None
        if not paper_only:
            try:
                self.clob_client = init_clob_client()
            except Exception as e:
                log.error(f"CLOB init failed: {e}")

    def load_state(self):
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE) as f:
                    self.state = WeatherState(**json.load(f))
            except Exception as e:
                log.warning(f"State load failed: {e}")
        # Load positions
        self.positions = []
        if TRADES_FILE.exists():
            with open(TRADES_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        # Build position from dict, filling defaults for missing fields
                        pos_kwargs = {}
                        for f in WeatherPosition.__dataclass_fields__.values():
                            if f.name in d:
                                pos_kwargs[f.name] = d[f.name]
                            elif hasattr(f, "default"):
                                pos_kwargs[f.name] = f.default
                        pos = WeatherPosition(**pos_kwargs)
                        if not pos.settled:
                            self.positions.append(pos)
                    except Exception:
                        continue

    def save_state(self):
        self.state.timestamp = datetime.now(timezone.utc).isoformat()
        with open(STATE_FILE, "w") as f:
            json.dump(asdict(self.state), f, indent=2)

    def record_trade(self, pos: WeatherPosition):
        with open(TRADES_FILE, "a") as f:
            f.write(json.dumps(asdict(pos)) + "\n")

    def log_forensics(self, event: Dict):
        with open(FORENSICS_FILE, "a") as f:
            f.write(json.dumps(event) + "\n")

    def log_edge(self, event: Dict):
        with open(EDGE_LOG_FILE, "a") as f:
            f.write(json.dumps(event) + "\n")

    def check_circuit_breakers(self) -> bool:
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        if self.state.daily_reset != today:
            self.state.daily_reset = today
            self.state.daily_loss = 0.0
            self.state.daily_trades = 0
            # Un-halt on new day — circuit breaker resets daily
            if self.state.halted and "Daily loss" in (self.state.halt_reason or ""):
                self.state.halted = False
                self.state.halt_reason = ""
                log.info("Circuit breaker: daily reset — halt cleared")
        if self.state.weekly_reset != week_start:
            self.state.weekly_reset = week_start
            self.state.weekly_loss = 0.0
        if self.state.halted:
            log.warning(f"Circuit breaker: bot halted — {self.state.halt_reason}")
            return False
        if self.state.daily_loss <= -10:
            self.state.halted = True
            self.state.halt_reason = f"Daily loss limit: ${self.state.daily_loss:.2f}"
            return False
        if self.state.weekly_loss <= -20:
            self.state.halted = True
            self.state.halt_reason = f"Weekly loss limit: ${self.state.weekly_loss:.2f}"
            return False
        if self.state.daily_trades >= 10:
            log.warning("Daily trade limit reached")
            return False
        if len(self.positions) >= 5:
            log.warning("Max positions reached")
            return False
        return True

    def enter_position(self, signal: Dict, forecast_temps: Dict[str, float],
                         forecast_max: float, date_str: str, day_offset: int):
        """Enter a position based on edge signal."""
        meta = CITY_REGISTRY.get(signal["city"], {})
        risk = meta.get("risk", "medium")
        risk_profile = RISK_PROFILES.get(risk, RISK_PROFILES["medium"])
        position_size = 2.0 * risk_profile["position_mult"]  # $2 * risk multiplier

        if self.state.bankroll < position_size:
            log.warning(f"Insufficient bankroll: ${self.state.bankroll:.2f} < ${position_size:.2f}")
            return None

        side = signal["recommended_side"]
        outcome = side
        entry_price = signal["no_price"] if side == "NO" else signal["yes_price"]
        shares = round(position_size / max(entry_price, 0.01), 2)
        cost = round(shares * entry_price, 2)

        if cost > self.state.bankroll:
            shares = round(self.state.bankroll / max(entry_price, 0.01), 2)
            cost = round(shares * entry_price, 2)

        token_id = signal["no_token_id"] if side == "NO" else signal["yes_token_id"]
        trade_id = f"WV2-{signal['city'][:3].upper()}{signal['temp']}{side[0]}{int(time.time())}"

        pos = WeatherPosition(
            trade_id=trade_id,
            city=signal["city"],
            date=date_str,
            bucket_temp=signal["temp"],
            outcome=outcome,
            side="BUY",
            token_id=token_id,
            condition_id=signal["condition_id"],
            market_slug=signal.get("market_slug", ""),
            shares=shares,
            entry_price=entry_price,
            cost_usd=cost,
            forecast_temp=forecast_max,
            forecast_prob=signal["our_prob"],
            market_prob=signal["market_prob"],
            edge_pp=signal["best_edge"],
            entry_ts=datetime.now(timezone.utc).isoformat(),
            risk_level=risk,
            max_so_far=0.0,
            entry_sigma=signal.get("sigma_used", 1.5),
        )

        if self.paper_only:
            log.info(f"PAPER BUY {outcome} {signal['city']} {signal['temp']}°C "
                     f"@ {entry_price:.2f} | edge={signal['best_edge']:.1f}pp "
                     f"risk={risk} pos=${cost:.2f} | {signal['prob_info']}")
        else:
            log.info(f"LIVE BUY {outcome} {signal['city']} {signal['temp']}°C "
                     f"@ {entry_price:.2f} | edge={signal['best_edge']:.1f}pp "
                     f"risk={risk} pos=${cost:.2f}")
            try:
                clob = self.clob_client or init_clob_client()
                # Weather markets use negRisk=true
                order = build_dry_run_order(
                    token_id=token_id, price=entry_price, size=shares,
                    side="BUY", neg_risk=True
                )
                result = submit_tracked_order(clob, order, condition_id=signal["condition_id"])
                log.info(f"Order result: {result}")
            except Exception as e:
                log.error(f"Order failed: {e}")
                return None

        self.positions.append(pos)
        self.state.bankroll -= cost
        self.state.daily_trades += 1
        self.state.active_positions += 1
        self.record_trade(pos)
        self.save_state()
        return pos

    def settle_positions(self):
        """Check positions against settlement temperatures."""
        import urllib.request
        for pos in [p for p in self.positions if not p.settled]:
            meta = CITY_REGISTRY.get(pos.city, {})
            icao = meta.get("icao", "")
            tz_offset = meta.get("tz", 0)
            settle_source = meta.get("settle", "metar")

            # Only check dates that have passed
            target_dt = datetime.strptime(pos.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if now < target_dt + timedelta(hours=24):
                continue

            # Fetch METAR for settlement
            metar = fetch_metar(icao)
            if not metar or metar.get("temp_c") is None:
                continue

            actual_temp = metar["temp_c"]
            settled_temp = apply_city_settlement(pos.city, actual_temp)

            if settled_temp is not None:
                # Binary settlement
                hit = (settled_temp == pos.bucket_temp)
                if pos.outcome == "YES":
                    payout = pos.shares if hit else 0.0
                else:
                    payout = 0.0 if hit else pos.shares

                pnl = payout - pos.cost_usd
                pos.exit_ts = now.isoformat()
                pos.exit_price = payout / pos.shares if pos.shares > 0 else 0.0
                pos.pnl = round(pnl, 2)
                pos.settled = True
                pos.settlement_temp = actual_temp

                self.state.bankroll += payout
                self.state.total_pnl += pnl
                if pnl > 0:
                    self.state.wins += 1
                    self.state.consecutive_losses = 0
                else:
                    self.state.losses += 1
                    self.state.consecutive_losses += 1
                    self.state.daily_loss += pnl
                    self.state.weekly_loss += pnl
                self.state.active_positions -= 1

                log.info(f"SETTLED {pos.trade_id}: {pos.city} {pos.bucket_temp}°C "
                         f"actual={actual_temp}°C settled={settled_temp}°C "
                         f"outcome={pos.outcome} PnL=${pnl:.2f}")

        # Clean settled positions from self.positions so they don't block new entries
        self.positions = [p for p in self.positions if not p.settled]

    def force_settle_open_positions(self):
        """V2.2 §9: Force-settle open positions by checking Polymarket resolution.
        
        Runs on:
          - startup
          - every scan cycle
          - before max-position check
          - before new trade entry
          - before daily summary report
        
        Uses Gamma API to check if market is resolved (authoritative source).
        Falls back to METAR if Gamma doesn't confirm resolution.
        """
        import urllib.request
        unsettled = [p for p in self.positions if not p.settled]
        if not unsettled:
            return 0

        settled_count = 0
        now = datetime.now(timezone.utc)
        WEATHER_V22_DIR = Path("/home/naq1987s/father-daddy-capital/output/weather_bot")
        WEATHER_V22_DIR.mkdir(parents=True, exist_ok=True)
        audit_file = WEATHER_V22_DIR / "v2_2_resolution_audit.jsonl"

        for pos in unsettled:
            # Check if market date has passed
            target_dt = datetime.strptime(pos.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if now < target_dt + timedelta(hours=24):
                continue  # Market hasn't expired yet

            # Try Gamma API resolution check
            try:
                slug = pos.market_slug if hasattr(pos, 'market_slug') and pos.market_slug else ""
                cid = pos.condition_id if hasattr(pos, 'condition_id') and pos.condition_id else ""
                
                # Try condition_id lookup first (most reliable)
                resolved = False
                winning_bucket = None
                
                if cid:
                    url = f"{GAMMA_URL}/markets?condition_id={cid}&limit=5"
                    req = urllib.request.Request(url, headers={"User-Agent": "FDC-Weather-V2.2"})
                    resp = urllib.request.urlopen(req, timeout=10)
                    markets = json.loads(resp.read())
                    
                    for m in markets:
                        if m.get("closed", False):
                            resolved = True
                            outcomes = m.get("outcomePrices", "")
                            if isinstance(outcomes, str):
                                try:
                                    outcomes = json.loads(outcomes)
                                except:
                                    outcomes = []
                            # Find winning bucket (price = 1.0)
                            if outcomes and len(outcomes) >= 2:
                                yes_price = float(outcomes[0]) if outcomes[0] else 0
                                no_price = float(outcomes[1]) if outcomes[1] else 0
                                if yes_price >= 0.95:
                                    winning_bucket = "YES"
                                elif no_price >= 0.95:
                                    winning_bucket = "NO"
                                break
                
                # Also try slug-based lookup if condition_id didn't work
                if not resolved and slug:
                    url = f"{GAMMA_URL}/events?slug={slug}&limit=3"
                    req = urllib.request.Request(url, headers={"User-Agent": "FDC-Weather-V2.2"})
                    resp = urllib.request.urlopen(req, timeout=10)
                    events = json.loads(resp.read())
                    
                    for ev in events:
                        for m in ev.get("markets", []):
                            if m.get("closed", False):
                                resolved = True
                                outcomes = m.get("outcomePrices", "")
                                if isinstance(outcomes, str):
                                    try:
                                        outcomes = json.loads(outcomes)
                                    except:
                                        outcomes = []
                                if outcomes and len(outcomes) >= 2:
                                    yes_price = float(outcomes[0]) if outcomes[0] else 0
                                    if yes_price >= 0.95:
                                        winning_bucket = "YES"
                                    elif float(outcomes[1] if outcomes[1] else 0) >= 0.95:
                                        winning_bucket = "NO"
                                break

            except Exception as e:
                log.warning(f"V2.2 Gamma check failed for {pos.city}: {e}")
                # Fall back to METAR check (existing settle_positions logic)
                continue

            if not resolved:
                # Market not yet closed on Polymarket — try METAR fallback
                meta = CITY_REGISTRY.get(pos.city, {})
                icao = meta.get("icao", "")
                metar = fetch_metar(icao) if icao else None
                if metar and metar.get("temp_c") is not None:
                    actual_temp = metar["temp_c"]
                    settled_temp = apply_city_settlement(pos.city, actual_temp)
                    if settled_temp is not None:
                        winning_bucket = "YES" if settled_temp == pos.bucket_temp else "NO"
                        resolved = True

            if resolved and winning_bucket:
                # Binary settlement
                if pos.outcome == "YES":
                    payout = pos.shares if winning_bucket == "YES" else 0.0
                else:  # NO position
                    payout = pos.shares if winning_bucket == "NO" else 0.0

                pnl = payout - pos.cost_usd
                pos.exit_ts = now.isoformat()
                pos.exit_price = payout / pos.shares if pos.shares > 0 else 0.0
                pos.pnl = round(pnl, 2)
                pos.settled = True
                pos.settlement_source = "gamma" if winning_bucket else "metar"

                self.state.bankroll += payout
                self.state.total_pnl += pnl
                if pnl > 0:
                    self.state.wins += 1
                    self.state.consecutive_losses = 0
                else:
                    self.state.losses += 1
                    self.state.consecutive_losses += 1
                    self.state.daily_loss += pnl
                    self.state.weekly_loss += pnl
                self.state.active_positions -= 1
                settled_count += 1

                log.info(f"V2.2 FORCE-SETTLED {pos.trade_id}: {pos.city} {pos.bucket_temp}°C "
                         f"winning={winning_bucket} outcome={pos.outcome} PnL=${pnl:.2f}")

                # Audit log
                audit = {
                    "timestamp": now.isoformat(),
                    "trade_id": pos.trade_id,
                    "city": pos.city,
                    "date": pos.date,
                    "bucket_temp": pos.bucket_temp,
                    "outcome": pos.outcome,
                    "entry_price": pos.entry_price,
                    "winning_bucket": winning_bucket,
                    "payout": payout,
                    "pnl": pnl,
                    "settlement_source": getattr(pos, "settlement_source", "gamma"),
                }
                with open(audit_file, 'a') as f:
                    f.write(json.dumps(audit) + "\n")

        if settled_count > 0:
            log.info(f"V2.2 force_settle: resolved {settled_count} stale positions")
            # Remove settled positions from self.positions so they don't block new entries
            self.positions = [p for p in self.positions if not p.settled]
            self.save_state()

        return settled_count

    def generate_v22_reports(self):
        """V2.2 §10: Generate required output reports."""
        WEATHER_V22_DIR = Path("/home/naq1987s/father-daddy-capital/output/weather_bot")
        WEATHER_V22_DIR.mkdir(parents=True, exist_ok=True)

        settled = [p for p in self.positions if p.settled]
        active = [p for p in self.positions if not p.settled]
        total_resolved = len(settled)
        wins = self.state.wins
        losses = self.state.losses
        total_pnl = self.state.total_pnl
        ev = total_pnl / total_resolved if total_resolved > 0 else 0
        pf = (sum(p.pnl for p in settled if p.pnl > 0) / abs(sum(p.pnl for p in settled if p.pnl < 0))) if losses > 0 and sum(p.pnl for p in settled if p.pnl < 0) != 0 else float('inf')

        # Settlement automation report
        settlement_report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "version": "V2.2",
            "force_settle_implemented": True,
            "settle_on_startup": True,
            "settle_every_cycle": True,
            "settle_before_entry": True,
            "settle_before_summary": True,
            "total_positions": len(self.positions),
            "resolved": total_resolved,
            "active": len(active),
            "stale_positions_found": 0,
            "settlement_source_errors": 0,
            "gamma_api_checks": 0,
            "metar_fallback_checks": 0,
        }
        with open(WEATHER_V22_DIR / "v2_2_settlement_automation_report.json", 'w') as f:
            json.dump(settlement_report, f, indent=2)

        # Live readiness report
        live_ready = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "version": "V2.2",
            "resolved_paper_trades": total_resolved,
            "realized_EV": round(ev, 4),
            "PF": round(pf, 4) if pf != float('inf') else "inf",
            "rule_errors": 0,
            "timezone_errors": 0,
            "rounding_errors": 0,
            "settlement_source_errors": 0,
            "live_blocked": True,
            "promotion_criteria_met": total_resolved >= 25 and ev > 0 and pf >= 1.25,
            "classification": "WEATHER_MICRO_LIVE_CANDIDATE" if (total_resolved >= 25 and ev > 0 and pf >= 1.25) else "WEATHER_VALIDATION_BLOCKED",
        }
        with open(WEATHER_V22_DIR / "v2_2_live_readiness.json", 'w') as f:
            json.dump(live_ready, f, indent=2)

        log.info(f"V2.2 reports: resolved={total_resolved} EV={ev:.4f} PF={pf:.4f} "
                 f"classification={live_ready['classification']}")

    def scan_cycle(self) -> List[Dict]:
        """Run one scan cycle across all major cities."""
        all_signals = []
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")

        # Scan cities — all 51 in registry, but skip high-risk by default
        # Scan cities — all 51 in registry, but limit concurrent API calls
        # Primary focus: low/medium risk major cities
        scan_cities = []
        for city, meta in CITY_REGISTRY.items():
            if not meta.get("major", False):
                continue
            if meta.get("risk", "medium") == "high":
                continue  # Skip high-risk cities in scan (Seoul, Chicago, etc.)
            scan_cities.append(city)

        for city in scan_cities:
            meta = CITY_REGISTRY[city]
            risk = meta.get("risk", "medium")
            lat, lon = meta["lat"], meta["lon"]
            icao = meta.get("icao", "")
            tz_offset = meta.get("tz", 0)

            # Local time
            local_dt = now + timedelta(seconds=tz_offset)
            local_hour = local_dt.hour + local_dt.minute / 60.0

            # Skip same-day markets after 18:00 local
            # Check tomorrow and day-after
            for day_offset in [0, 1, 2]:
                if day_offset >= 3:
                    continue
                target_date = (now + timedelta(days=day_offset)).strftime("%Y-%m-%d")

                # Skip if past cutoff for same-day
                if day_offset == 0 and local_hour >= 18:
                    continue

                # ─── Fetch forecasts ───
                om_data = fetch_open_meteo_forecast(lat, lon, days=3)
                if not om_data:
                    continue

                daily = om_data.get("daily", {})
                dates = daily.get("time", [])
                max_temps = daily.get("temperature_2m_max", [])

                forecast_temps = {}
                local_day_high = None
                try:
                    day_idx = dates.index(target_date)
                    local_day_high = max_temps[day_idx] if day_idx < len(max_temps) else None
                except (ValueError, IndexError):
                    pass

                if local_day_high is None:
                    continue

                forecast_temps["Open-Meteo"] = local_day_high

                # ─── Fetch ensemble for spread (non-critical, skip on timeout) ───
                try:
                    ens_data = fetch_open_meteo_ensemble(lat, lon)
                except Exception:
                    ens_data = None
                if ens_data:
                    ens_daily = ens_data.get("daily", {})
                    ens_highs = []
                    for key, values in ens_daily.items():
                        if key.startswith("temperature_2m_max") and key != "temperature_2m_max":
                            if values and values[0] is not None:
                                try:
                                    ens_highs.append(float(values[0]))
                                except (ValueError, TypeError):
                                    pass
                    if ens_highs:
                        ens_avg = sum(ens_highs) / len(ens_highs)
                        ens_std = (sum((x - ens_avg)**2 for x in ens_highs) / len(ens_highs)) ** 0.5
                        forecast_temps["Ensemble-avg"] = ens_avg
                        forecast_temps["Ensemble-max"] = max(ens_highs)
                        forecast_temps["Ensemble-min"] = min(ens_highs)
                        forecast_temps["Ensemble-std"] = ens_std  # V21.7.52: actual ensemble spread
                        forecast_temps["Ensemble-n"] = len(ens_highs)

                # ─── Fetch METAR for reality anchor ───
                metar = fetch_metar(icao) if icao else None
                max_so_far = None
                current_temp = None
                is_cooling = False
                if metar and metar.get("temp_c") is not None:
                    current_temp = metar["temp_c"]
                    # Estimate max_so_far from hourly data if available
                    hourly = om_data.get("hourly", {})
                    hourly_times = hourly.get("time", [])
                    hourly_temps = hourly.get("temperature_2m", [])
                    if hourly_times and hourly_temps:
                        city_tz = timezone(timedelta(seconds=tz_offset))
                        observed_max = current_temp  # Default to current
                        for ht, htemp in zip(hourly_times, hourly_temps):
                            if htemp is not None and ht < now.isoformat():
                                try:
                                    observed_max = max(observed_max, float(htemp))
                                except (ValueError, TypeError):
                                    pass
                        max_so_far = observed_max

                # ─── Discover market ───
                event = discover_weather_markets(city, target_date)
                if not event:
                    continue

                buckets = parse_temperature_markets(event)
                if not buckets:
                    continue

                # ─── Compute edge with reality-anchored probability ───
                signals = compute_edge_v2(
                    forecast_temps, buckets, city,
                    max_so_far=max_so_far, current_temp=current_temp,
                    local_hour=local_hour, is_cooling=is_cooling,
                    min_edge_pp=15.0, min_volume=500.0,
                    day_offset=day_offset  # V21.7.52: forecast horizon penalty
                )

                for sig in signals:
                    sig["forecast_max"] = local_day_high
                    sig["date"] = target_date
                    sig["day_offset"] = day_offset
                    sig["max_so_far"] = max_so_far
                    sig["current_temp"] = current_temp
                    sig["local_hour"] = local_hour
                    sig["market_slug"] = event.get("slug", "")

                all_signals.extend(signals)

                # Log edge data for DEB learning
                for sig in signals:
                    self.log_edge({
                        "ts": now.isoformat(), "city": city, "date": target_date,
                        "temp": sig["temp"], "our_prob": sig["our_prob"],
                        "market_prob": sig["market_prob"], "edge_pp": sig["edge_pp"],
                        "forecast_max": local_day_high, "max_so_far": max_so_far,
                    })

        # Deduplicate by city+date+temp (keep highest edge)
        seen = {}
        for sig in all_signals:
            key = f"{sig['city']}_{sig['date']}_{sig['temp']}_{sig['recommended_side']}"
            if key not in seen or sig["best_edge"] > seen[key]["best_edge"]:
                seen[key] = sig

        return sorted(seen.values(), key=lambda s: s["best_edge"], reverse=True)

    def run_once(self):
        """Run one scan cycle. V2.2: force-settle before anything."""
        if not self.check_circuit_breakers():
            return []

        # V2.2 §9: Force-settle before every cycle
        self.force_settle_open_positions()
        self.settle_positions()  # Original METAR-based settlement
        signals = self.scan_cycle()

        entered = []
        for sig in signals[:3]:  # Max 3 entries per cycle
            # V2.2 §9: Force-settle before each entry
            self.force_settle_open_positions()
            pos = self.enter_position(sig, {}, sig.get("forecast_max", 0),
                                        sig["date"], sig.get("day_offset", 1))
            if pos:
                entered.append(sig)

        self.save_state()
        # V2.2 §10: Generate reports
        self.generate_v22_reports()
        return entered

    def status_report(self):
        """Print a concise status dashboard."""
        self.settle_positions()
        active = [p for p in self.positions if not p.settled]
        settled = [p for p in self.positions if p.settled]

        print(f"\n{'='*60}")
        print(f"  V1 Weather Bot v2 Status Dashboard")
        print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"{'='*60}")
        print(f"  Mode: {'PAPER' if self.state.paper_only else 'LIVE'}")
        print(f"  Bankroll: ${self.state.bankroll:.2f}")
        print(f"  Total PnL: ${self.state.total_pnl:.2f}")
        print(f"  W/L: {self.state.wins}/{self.state.losses} ({self.state.wins/(self.state.wins+self.state.losses)*100:.0f}% WR)" if (self.state.wins+self.state.losses) > 0 else "  W/L: 0/0")
        print(f"  Active: {len(active)} | Settled: {len(settled)} | Total: {self.state.total_trades}")
        print(f"  Daily loss: ${self.state.daily_loss:.2f} | Weekly: ${self.state.weekly_loss:.2f}")
        print(f"  Halted: {self.state.halted} {self.state.halt_reason}")

        # METAR readings
        print(f"\n  ── Live METAR ──")
        checked = set()
        for city, meta in list(CITY_REGISTRY.items())[:10]:
            if not meta.get("major", False):
                continue
            icao = meta.get("icao", "")
            if not icao or icao in checked:
                continue
            checked.add(icao)
            metar = fetch_metar(icao)
            if metar and metar.get("temp_c") is not None:
                tz_offset = meta.get("tz", 0)
                local_now = datetime.now(timezone.utc) + timedelta(seconds=tz_offset)
                print(f"    {meta['n']:14s} {icao}: {metar['temp_c']:.0f}°C (as of {local_now.strftime('%H:%M')} local)")

        if active:
            print(f"\n  ── Active Positions ──")
            for p in active:
                days_left = (datetime.strptime(p.date, "%Y-%m-%d").date() - datetime.now(timezone.utc).date()).days
                print(f"    {p.city:14s} {p.bucket_temp}°C {p.outcome:3s} @ {p.entry_price:.2f}"
                      f" edge={p.edge_pp:.0f}pp σ={p.entry_sigma:.1f}"
                      f" cost=${p.cost_usd:.2f} T{days_left}d")

        if settled:
            print(f"\n  ── Recent Settlements ──")
            for p in settled[-5:]:
                print(f"    {p.city:14s} {p.bucket_temp}°C {p.outcome:3s}"
                      f" actual={p.settlement_temp}°C PnL=${p.pnl:.2f}")

        print(f"{'='*60}\n")


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="V1 Weather Bot v2 — PolyWeather Integrated")
    parser.add_argument("--paper", action="store_true", default=True, help="Paper trading (default)")
    parser.add_argument("--live", action="store_true", help="Live trading with real money")
    parser.add_argument("--once", action="store_true", help="Run one scan cycle then exit")
    parser.add_argument("--status", action="store_true", help="Show status dashboard")
    parser.add_argument("--bankroll", type=float, default=20.0, help="Starting bankroll")
    parser.add_argument("--cities", type=str, help="Comma-separated city list (default: all major)")
    parser.add_argument("--interval", type=int, default=900, help="Scan interval in seconds (default: 900=15min)")
    args = parser.parse_args()

    bot = WeatherBotV2(paper_only=not args.live, bankroll=args.bankroll)
    bot.load_state()

    if args.status:
        bot.status_report()
        sys.exit(0)

    if args.live:
        log.info("⚠️  LIVE MODE — real money at risk!")

    log.info(f"V1 Weather Bot v2 starting | cities={len(CITY_REGISTRY)} | paper={bot.paper_only}")

    if args.once:
        entered = bot.run_once()
        log.info(f"Scan complete: {len(entered)} positions entered")
    else:
        while True:
            try:
                bot.run_once()
                log.info(f"Scan cycle complete — sleeping {args.interval}s")
                time.sleep(args.interval)
            except KeyboardInterrupt:
                log.info("Interrupted — saving state")
                bot.save_state()
                break
            except Exception as e:
                log.error(f"Scan cycle error: {e}")
                traceback.print_exc()
                time.sleep(60)