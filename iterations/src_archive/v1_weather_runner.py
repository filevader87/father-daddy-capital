#!/usr/bin/env python3
"""
V1 FDC Weather Bot — Polymarket Temperature Markets
====================================================
Engine: forecast-deviation edge (Open-Meteo + METAR vs PM implied probability)
Markets: Daily highest-temperature in 7 cities
Resolution: Wunderground "Forecast" column at specific ICAO station, whole °C
negRisk: true (different from BTC 5m crypto markets!)
Position sizing: $2 fixed paper, $1 live probe
Entry: forecast probability > market implied by ≥15pp
Exit: binary settlement only (no synthetic TP)
"""

import os
import sys
import json
import time
import math
import csv
import logging
import argparse
import traceback
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, field, asdict

# ─── Paths ───
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src" / "weather"
OUTPUT_DIR = PROJECT_ROOT / "output" / "v1_weather"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Import shared CLOB tools from crypto bot module
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

STATE_FILE = OUTPUT_DIR / "weather_state.json"
TRADES_FILE = OUTPUT_DIR / "weather_trades.jsonl"
FORENSICS_FILE = OUTPUT_DIR / "weather_forensics.jsonl"
EDGE_LOG_FILE = OUTPUT_DIR / "edge_log.jsonl"
CONSOLE_LOG = OUTPUT_DIR / "v1_weather_console.log"

# ─── CLOB / Wallet config (shared with crypto bot) ───
ENV_FILE = Path("/mnt/c/Users/12035/father_daddy_capital/.env")
DERIVED_DW = "[REDACTED_DW]"
EOA = "[REDACTED_EOA]"
USDC_CONTRACT = "[REDACTED_USDC]"
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"

# ─── City config ───
CITIES = {
    "london":    {"lat": 51.5,  "lon": -0.1,   "icao": "EGLC", "skip": False, "best WR": 0.35},
    "seoul":     {"lat": 37.57, "lon": 127.0,  "icao": "RKSI", "skip": False, "best WR": 0.30},
    "taipei":    {"lat": 25.08, "lon": 121.5,  "icao": "RCSS", "skip": True,  "best WR": 0.00},  # backtest loser
    "beijing":   {"lat": 39.9,  "lon": 116.4,  "icao": "ZBAA", "skip": True,  "best WR": 0.00},  # backtest loser
    "wellington":{"lat": -41.3, "lon": 174.8,  "icao": "NZWN", "skip": False, "best WR": 0.35},
    "shanghai":  {"lat": 31.2,  "lon": 121.5,  "icao": "ZSPD", "skip": True,  "best WR": 0.00},  # marginal
    "shenzhen":  {"lat": 22.5,  "lon": 114.1,  "icao": "ZGSZ", "skip": True,  "best WR": 0.00},  # marginal
}

# ─── Logging ───
log = logging.getLogger("v1_weather")
log.setLevel(logging.INFO)
# Only add handlers once
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
    side: str          # "BUY"
    token_id: str
    condition_id: str
    market_slug: str
    shares: float
    entry_price: float  # per share
    cost_usd: float     # total cost
    forecast_temp: float # Open-Meteo forecast at entry
    forecast_prob: float # our derived probability
    market_prob: float   # PM implied probability at entry
    edge_pp: float      # forecast_prob - market_prob in percentage points
    entry_ts: str
    exit_ts: str = ""
    exit_price: float = 0.0
    pnl: float = 0.0
    settled: bool = False
    settlement_temp: Optional[float] = None

@dataclass
class WeatherState:
    """Persistent state for the weather bot."""
    live_enabled: bool = False
    paper_only: bool = True
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    bankroll: float = 20.0  # Paper starting bankroll
    bankroll_actual_usd: float = 0.0  # On-chain USDC (live only)
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
# API FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def load_env():
    """Load .env file for PK."""
    env_vars = {}
    if ENV_FILE.exists():
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip()
    return env_vars

# V21.7.70: Replaced Open-Meteo with wttr.in (free, no key, no rate limits)
_OM_RETRY_AFTER = 0.0  # Kept for compat
_OM_429_COUNT = 0

def fetch_open_meteo_forecast(lat: float, lon: float, days: int = 3) -> Optional[Dict]:
    """V21.7.70: Fetch forecast from wttr.in instead of Open-Meteo.

    Returns dict compatible with old Open-Meteo format.
    """
    import urllib.request, json as _json
    url = f"https://wttr.in/{lat},{lon}?format=j1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode())

        weather = data.get("weather", [])
        daily_times = []
        daily_max = []
        daily_min = []
        hourly_times = []
        hourly_temps = []

        for day in weather[:days]:
            daily_times.append(day.get("date", ""))
            daily_max.append(float(day.get("maxtempC", 0)))
            daily_min.append(float(day.get("mintempC", 0)))
            for h in day.get("hourly", []):
                hourly_times.append(h.get("time", ""))
                hourly_temps.append(float(h.get("tempC", 0)) if h.get("tempC") else None)

        return {
            "daily": {
                "time": daily_times,
                "temperature_2m_max": daily_max,
                "temperature_2m_min": daily_min,
            },
            "hourly": {
                "time": hourly_times,
                "temperature_2m": hourly_temps,
            },
        }
    except Exception as e:
        log.warning(f"wttr.in forecast failed: {e}")
        return None

def fetch_metar(icao: str) -> Optional[Dict]:
    """Fetch live METAR observation from Aviation Weather Center."""
    import urllib.request
    url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=json&taf=false"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FDC-Weather-V1/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if data and len(data) > 0:
                return data[0]  # Most recent observation
    except Exception as e:
        log.warning(f"METAR fetch failed for {icao}: {e}")
    return None

def discover_weather_markets(city: str, date_str: str) -> Optional[Dict]:
    """Discover Polymarket weather event for a city+date using Gamma API."""
    import urllib.request
    # Parse date_str like "2026-06-08" into "june-8"
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    month_name = dt.strftime("%B").lower()
    day = dt.day
    slug = f"highest-temperature-in-{city}-on-{month_name}-{day}-2026"
    # Also try just the number (no leading zero on day)
    # API uses lowercase month name
    
    url = f"https://gamma-api.polymarket.com/events?slug={slug}&limit=5"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FDC-Weather-V1/1.0"})
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
        
        # Extract temperature from question
        # "Will the highest temperature in London be 20°C on June 7?"
        import re
        temp_match = re.search(r'be\s+(\d+)°C', question)
        if not temp_match:
            continue
        temp = int(temp_match.group(1))
        
        # Check if "or higher" / "or lower" suffix
        is_threshold = "or higher" in question or "or lower" in question
        
        token_ids = m.get("clobTokenIds", "[]")
        if isinstance(token_ids, str):
            try:
                token_ids = json.loads(token_ids)
            except json.JSONDecodeError:
                continue
        
        condition_id = m.get("conditionId", m.get("condition_id", ""))
        
        buckets.append({
            "question": question,
            "temp": temp,
            "is_threshold": is_threshold,
            "yes_price": yes_price,
            "no_price": no_price,
            "yes_token_id": token_ids[0] if len(token_ids) > 0 else "",
            "no_token_id": token_ids[1] if len(token_ids) > 1 else "",
            "condition_id": condition_id,
            "market_id": m.get("id", ""),
            "volume": float(m.get("volume", 0) or 0),
            "liquidity": float(m.get("liquidity", 0) or 0),
            "neg_risk": True,  # Weather markets use negRisk=true
            "slug": event.get("slug", ""),
        })
    
    return sorted(buckets, key=lambda b: b["temp"])

# ═══════════════════════════════════════════════════════════════
# FORECAST PROBABILITY MODEL
# ═══════════════════════════════════════════════════════════════

def forecast_to_probability(forecast_temp: float, bucket_temp: int,
                             forecast_min: float = None,
                             historical_error_std: float = 1.5) -> float:
    """
    Convert a point forecast to probability of hitting a specific °C bucket.
    
    Uses a Gaussian CDF centered on forecast_temp with spread based on:
    - Historical Open-Meteo error (typically 1-2°C std dev for 1-day forecasts)
    - Wider for 2-3 day forecasts
    - Accounts for "or higher"/"or lower" thresholds
    
    Resolution is whole °C — so P(T=max_temp) ≈ P(bucket_temp - 0.5 < max_temp < bucket_temp + 0.5)
    """
    # Probability that max_temp falls in [bucket_temp - 0.5, bucket_temp + 0.5]
    z_low = (bucket_temp - 0.5 - forecast_temp) / historical_error_std
    z_high = (bucket_temp + 0.5 - forecast_temp) / historical_error_std
    
    from math import erf, sqrt
    def phi(z):
        return 0.5 * (1.0 + erf(z / sqrt(2.0)))
    
    prob = phi(z_high) - phi(z_low)
    return max(0.01, min(0.99, prob))

def compute_edge(forecast_temp: float, buckets: List[Dict],
                  error_std: float = 1.5, min_edge_pp: float = 15.0,
                  min_volume: float = 500.0) -> List[Dict]:
    """
    Compute edge for each bucket and return actionable signals.
    Edge = forecast_probability - market_implied_probability (in percentage points)
    """
    signals = []
    
    for b in buckets:
        market_prob = b["yes_price"]
        
        # Skip near-certain or near-impossible markets
        if market_prob < 0.03 or market_prob > 0.97:
            continue
        
        # Skip low-volume markets
        if b["volume"] < min_volume:
            continue
        
        # Compute our probability
        our_prob = forecast_to_probability(forecast_temp, b["temp"],
                                            historical_error_std=error_std)
        
        # Edge in percentage points
        edge_pp = (our_prob - market_prob) * 100.0
        
        # Also check NO side
        no_edge_pp = ((1.0 - our_prob) - b["no_price"]) * 100.0
        
        signal = {
            "city": b["slug"].split("-in-")[1].split("-on-")[0] if "-in-" in b["slug"] else "?",
            "temp": b["temp"],
            "question": b["question"],
            "our_prob": round(our_prob, 4),
            "market_prob": round(market_prob, 4),
            "edge_pp": round(edge_pp, 1),
            "no_edge_pp": round(no_edge_pp, 1),
            "recommended_side": "YES" if edge_pp > no_edge_pp else "NO",
            "best_edge": round(max(edge_pp, no_edge_pp), 1),
            "volume": b["volume"],
            "yes_token_id": b["yes_token_id"],
            "no_token_id": b["no_token_id"],
            "condition_id": b["condition_id"],
            "market_id": b["market_id"],
            "neg_risk": True,
        }
        
        # Only include if edge exceeds minimum
        if signal["best_edge"] >= min_edge_pp:
            signals.append(signal)
    
    # Sort by best edge (descending)
    signals.sort(key=lambda s: s["best_edge"], reverse=True)
    return signals

# ═══════════════════════════════════════════════════════════════
# WALLET & EXECUTION
# ═══════════════════════════════════════════════════════════════

def get_onchain_usdc() -> float:
    """Get USDC balance via CLOB API (V21.7.69: replaced on-chain eth_call which 403'd).

    Uses the py_clob_client get_balance_allowance with sig_type=3 — same method
    the wallet audit uses. Returns pUSD collateral balance.
    """
    try:
        import os
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE if ENV_FILE.exists() else "/mnt/c/Users/12035/father_daddy_capital/.env")

        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams

        proxy = DERIVED_DW
        private_key = os.getenv("PM_WALLET_PRIVATE_KEY", "")
        api_key = os.getenv("PM_API_KEY", "")
        api_secret = os.getenv("PM_API_SECRET", "")
        api_passphrase = os.getenv("PM_API_PASSPHRASE", "")

        if not all([private_key, api_key, api_secret, api_passphrase]):
            log.warning("get_onchain_usdc: missing CLOB credentials")
            return 0.0

        creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=private_key,
            chain_id=137,
            creds=creds,
            signature_type=3,
            funder=proxy,
        )
        bal = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=3))
        balance = int(bal.get("balance", 0)) / 1_000_000
        return round(balance, 2)
    except Exception as e:
        log.warning(f"CLOB balance check failed: {e}")
        return 0.0

def init_clob_client():
    """Initialize CLOB client using shared module (fdc_pm_live).
    Weather markets ALWAYS use negRisk=True."""
    if HAS_CLOB_MODULE:
        client = get_clob_client()
        if client:
            log.info(f"CLOB client initialized via shared module (negRisk=True for weather)")
            return client, None
        else:
            log.warning("Shared CLOB module returned None — falling back to standalone init")
    
    # Fallback standalone init
    try:
        env = load_env()
        pk = env.get("PM_WALLET_PRIVATE_KEY", "").strip()
        if not pk:
            log.error("No PK found in .env")
            return None, None
        
        if not pk.startswith("0x"):
            pk = "0x" + pk
        
        from clob_client import ClobClient
        from clob_client.clob_types import ApiCreds
        
        client = ClobClient(host="https://clob.polymarket.com", key=pk, chain_id=137)
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        
        log.info("CLOB client initialized (standalone fallback)")
        return client, creds
        
    except Exception as e:
        log.error(f"CLOB init failed: {e}")
        traceback.print_exc()
        return None, None

# ═══════════════════════════════════════════════════════════════
# WEATHER BOT MAIN CLASS
# ═══════════════════════════════════════════════════════════════

class V1WeatherBot:
    """V1 FDC Weather Bot — forecast-deviation engine for daily temperature markets."""
    
    def __init__(self, paper_only: bool = True, position_size: float = 2.0,
                 max_daily_loss: float = 10.0, max_weekly_loss: float = 20.0,
                 max_daily_trades: int = 10, max_positions: int = 5,
                 min_edge_pp: float = 15.0, scan_interval: int = 900):
        """
        Args:
            paper_only: If True, paper sim. If False, live trading.
            position_size: Fixed dollar amount per position.
            max_daily_loss: Max loss per day (paper or live).
            max_weekly_loss: Max loss per week.
            max_daily_trades: Max trades per day.
            max_positions: Max concurrent open positions.
            min_edge_pp: Minimum edge (our prob - market prob) in percentage points.
            scan_interval: Seconds between scans (default 15 min = 900s).
        """
        self.paper_only = paper_only
        self.position_size = position_size
        self.max_daily_loss = max_daily_loss
        self.max_weekly_loss = max_weekly_loss
        self.max_daily_trades = max_daily_trades
        self.max_positions = max_positions
        self.min_edge_pp = min_edge_pp
        self.scan_interval = scan_interval
        
        # Load state
        self.state = self._load_state()
        self.positions: List[WeatherPosition] = []
        self._load_positions()
        
        # CLOB client (lazy init)
        self.clob_client = None
        self.clob_creds = None
        
        # Scan tracking
        self.cycle_id = 0
        self.scan_count = 0
        self.edge_opportunities = []
        
    def _load_state(self) -> WeatherState:
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE) as f:
                    d = json.load(f)
                return WeatherState(**{k: d.get(k, v) for k, v in asdict(WeatherState()).items()})
            except Exception as e:
                log.warning(f"State load error: {e}, using defaults")
        return WeatherState()
    
    def _save_state(self):
        self.state.timestamp = datetime.now(timezone.utc).isoformat()
        with open(STATE_FILE, "w") as f:
            json.dump(asdict(self.state), f, indent=2, default=str)
        
        # ─── Live promotion readiness ───
        resolved = [p for p in self.positions if p.settled]
        wins = sum(1 for p in resolved if p.pnl and p.pnl > 0)
        losses = len(resolved) - wins
        total_resolved = len(resolved)
        wr = wins / total_resolved if total_resolved > 0 else 0
        total_pnl = sum(p.pnl for p in resolved if p.pnl is not None)
        wins_pnl = sum(p.pnl for p in resolved if p.pnl and p.pnl > 0)
        losses_pnl = abs(sum(p.pnl for p in resolved if p.pnl and p.pnl < 0)) or 0.01
        pf = wins_pnl / losses_pnl if losses_pnl > 0 else float("inf")
        
        MIN_TRADES = 25
        MIN_WR = 0.55
        MIN_PF = 1.25
        MIN_PNL = 25.0
        
        readiness = {
            "timestamp": self.state.timestamp,
            "version": "V2.2",
            "resolved_paper_trades": total_resolved,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wr, 4),
            "profit_factor": round(pf, 2),
            "total_pnl": round(total_pnl, 2),
            "live_blocked": not (
                total_resolved >= MIN_TRADES
                and wr >= MIN_WR
                and pf >= MIN_PF
                and total_pnl >= MIN_PNL
            ),
            "promotion_criteria_met": (
                total_resolved >= MIN_TRADES
                and wr >= MIN_WR
                and pf >= MIN_PF
                and total_pnl >= MIN_PNL
            ),
            "classification": "LIVE_READY" if (
                total_resolved >= MIN_TRADES
                and wr >= MIN_WR
                and pf >= MIN_PF
                and total_pnl >= MIN_PNL
            ) else "PAPER_VALIDATION",
            "gates": {
                "min_resolved_trades": {"required": MIN_TRADES, "actual": total_resolved, "met": total_resolved >= MIN_TRADES},
                "min_win_rate": {"required": MIN_WR, "actual": round(wr, 4), "met": wr >= MIN_WR},
                "min_profit_factor": {"required": MIN_PF, "actual": round(pf, 2), "met": pf >= MIN_PF},
                "min_pnl_usd": {"required": MIN_PNL, "actual": round(total_pnl, 2), "met": total_pnl >= MIN_PNL},
            },
        }
        with open(Path(OUTPUT_DIR) / "v2_2_live_readiness.json", "w") as f:
            json.dump(readiness, f, indent=2, default=str)
    
    def _load_positions(self):
        if TRADES_FILE.exists():
            try:
                with open(TRADES_FILE) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        d = json.loads(line)
                        pos = WeatherPosition(**d)
                        self.positions.append(pos)
                active = [p for p in self.positions if not p.settled]
                self.state.active_positions = len(active)
                log.info(f"Loaded {len(self.positions)} positions ({len(active)} active)")
            except Exception as e:
                log.warning(f"Position load error: {e}")
    
    def _save_position(self, pos: WeatherPosition):
        with open(TRADES_FILE, "a") as f:
            f.write(json.dumps(asdict(pos)) + "\n")
    
    def _check_risk_limits(self) -> Tuple[bool, str]:
        """Check if we can enter a new position."""
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        
        # Reset daily counters
        if self.state.daily_reset != today:
            self.state.daily_loss = 0.0
            self.state.daily_trades = 0
            self.state.daily_reset = today
        
        if self.state.weekly_reset != week_start:
            self.state.weekly_loss = 0.0
            self.state.weekly_reset = week_start
        
        active = [p for p in self.positions if not p.settled]
        
        if len(active) >= self.max_positions:
            return False, f"max_positions={self.max_positions}"
        if self.state.daily_trades >= self.max_daily_trades:
            return False, f"max_daily_trades={self.max_daily_trades}"
        if self.state.daily_loss <= -self.max_daily_loss:
            return False, f"max_daily_loss={self.max_daily_loss}"
        if self.state.weekly_loss <= -self.max_weekly_loss:
            return False, f"max_weekly_loss={self.max_weekly_loss}"
        if self.state.halted:
            return False, f"halted: {self.state.halt_reason}"
        
        return True, "OK"
    
    def scan_markets(self) -> List[Dict]:
        """Scan all active weather markets for edge opportunities."""
        now = datetime.now(timezone.utc)
        signals = []
        
        for city, cfg in CITIES.items():
            if cfg["skip"]:
                continue
            
            # Check today and next 2 days
            for day_offset in range(3):
                target_date = now + timedelta(days=day_offset)
                date_str = target_date.strftime("%Y-%m-%d")
                
                # Fetch forecast
                forecast_data = fetch_open_meteo_forecast(cfg["lat"], cfg["lon"], days=3)
                if not forecast_data or "daily" not in forecast_data:
                    log.debug(f"No forecast data for {city} {date_str}")
                    continue
                
                dates = forecast_data["daily"]["time"]
                max_temps = forecast_data["daily"]["temperature_2m_max"]
                
                if date_str not in dates:
                    log.debug(f"Date {date_str} not in forecast for {city}")
                    continue
                
                idx = dates.index(date_str)
                forecast_max = max_temps[idx]
                
                # Skip day+3+ (high forecast uncertainty) and same-day after 18:00 UTC
                # (resolution happens at midnight local; too late for same-day)
                if day_offset >= 3:
                    continue
                if day_offset == 0 and now.hour >= 18:
                    log.debug(f"Skipping {city} today (after 18:00 UTC, too late)")
                    continue
                
                # Widen error std for further dates
                if day_offset == 0:
                    error_std = 1.2  # Today: narrow
                elif day_offset == 1:
                    error_std = 1.8  # Tomorrow: medium
                else:
                    error_std = 2.5  # Day+2: wide
                
                # Discover Polymarket event
                event = discover_weather_markets(city, date_str)
                if not event:
                    log.debug(f"No PM market for {city} {date_str}")
                    continue
                
                # Parse temperature buckets
                buckets = parse_temperature_markets(event)
                if not buckets:
                    log.debug(f"No buckets for {city} {date_str}")
                    continue
                
                # Compute edge
                edge_signals = compute_edge(forecast_max, buckets,
                                             error_std=error_std,
                                             min_edge_pp=self.min_edge_pp,
                                             min_volume=100.0)
                
                for sig in edge_signals:
                    sig["forecast_max"] = forecast_max
                    sig["date"] = date_str
                    sig["day_offset"] = day_offset
                    sig["error_std"] = error_std
                    sig["city"] = city
                    sig["icao"] = cfg["icao"]
                    sig["slug"] = event.get("slug", f"highest-temperature-in-{city}-on-{datetime.strptime(date_str, '%Y-%m-%d').strftime('%B').lower()}-{datetime.strptime(date_str, '%Y-%m-%d').day}-2026")
                
                signals.extend(edge_signals)
                
                # Log forecast vs market
                self._log_forensics(city, date_str, forecast_max, buckets, error_std)
        
        # Sort by best edge
        signals.sort(key=lambda s: s["best_edge"], reverse=True)
        self.edge_opportunities = signals
        return signals
    
    def _log_forensics(self, city: str, date: str, forecast_max: float,
                        buckets: List[Dict], error_std: float):
        """Log forecast vs market comparison for every scan."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "city": city,
            "date": date,
            "forecast_max": forecast_max,
            "error_std": error_std,
            "buckets": [
                {
                    "temp": b["temp"],
                    "market_yes": b["yes_price"],
                    "market_no": b["no_price"],
                    "volume": b["volume"],
                }
                for b in buckets[:6]  # Top 6 buckets around forecast
            ],
        }
        try:
            with open(FORENSICS_FILE, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass
    
    def enter_position(self, signal: Dict) -> Optional[WeatherPosition]:
        """Execute a trade (paper or live)."""
        # Check risk limits
        ok, reason = self._check_risk_limits()
        if not ok:
            log.info(f"Risk limit: {reason} — skipping {signal['city']} {signal['temp']}°C")
            return None
        
        # Determine side and token
        if signal["recommended_side"] == "YES":
            token_id = signal["yes_token_id"]
            outcome = "YES"
        else:
            token_id = signal["no_token_id"]
            outcome = "NO"
        
        # Determine entry price (use market_prob as estimate)
        if outcome == "YES":
            entry_price = signal["market_prob"]
        else:
            entry_price = 1.0 - signal["market_prob"]
        
        # Calculate shares
        shares = self.position_size / max(entry_price, 0.01)
        
        # Generate trade ID
        trade_id = f"W-{signal['city']}-{signal['date']}-{signal['temp']}C-{outcome}-{int(time.time())}"
        
        pos = WeatherPosition(
            trade_id=trade_id,
            city=signal["city"],
            date=signal["date"],
            bucket_temp=signal["temp"],
            outcome=outcome,
            side="BUY",
            token_id=token_id,
            condition_id=signal["condition_id"],
            market_slug=signal.get("slug", ""),
            shares=round(shares, 2),
            entry_price=round(entry_price, 4),
            cost_usd=self.position_size,
            forecast_temp=signal.get("forecast_max", 0),
            forecast_prob=signal["our_prob"],
            market_prob=signal["market_prob"],
            edge_pp=signal["best_edge"],
            entry_ts=datetime.now(timezone.utc).isoformat(),
        )
        
        if self.paper_only:
            # Paper trade: just record
            log.info(f"PAPER BUY {outcome} {signal['city']} {signal['temp']}°C "
                     f"@ {entry_price:.2f} | edge={signal['best_edge']:.1f}pp | "
                     f"our_prob={signal['our_prob']:.2f} market={signal['market_prob']:.2f}")
        else:
            # Live trade: execute via CLOB with negRisk=True
            log.info(f"LIVE BUY {outcome} {signal['city']} {signal['temp']}°C "
                     f"@ {entry_price:.2f} | edge={signal['best_edge']:.1f}pp | negRisk=True")
            
            if not token_id:
                log.error(f"No token_id for {trade_id} — skipping")
                return None
            
            # Use shared CLOB module for order execution
            if HAS_CLOB_MODULE:
                try:
                    # Weather markets ALWAYS use negRisk=True
                    spec = build_dry_run_order(
                        token_id=token_id,
                        side="BUY",
                        price=entry_price,
                        size=shares,
                    )
                    # Force negRisk=True for weather markets
                    spec.neg_risk = True
                    
                    if not spec.valid:
                        log.error(f"Order validation failed: {spec.errors}")
                        return None
                    
                    result = submit_tracked_order(spec)
                    if "error" in result:
                        log.error(f"Order failed: {result['error']}")
                        return None
                    
                    log.info(f"✅ Order submitted: {result.get('order_id', '?')} | "
                             f"mode={result.get('mode', '?')} | "
                             f"cost=${result.get('cost', 0):.2f}")
                    
                    # Update entry price to actual fill (if different)
                    if result.get("price"):
                        pos.entry_price = float(result["price"])
                        pos.cost_usd = float(result["price"]) * shares
                    
                except Exception as e:
                    log.error(f"Live order exception: {e}")
                    traceback.print_exc()
                    return None
            else:
                log.warning("CLOB module not available — recording as paper (LIVE flag ignored)")
            
            # Log trade
        
        self.positions.append(pos)
        self._save_position(pos)
        self.state.total_trades += 1
        self.state.daily_trades += 1
        self.state.active_positions = len([p for p in self.positions if not p.settled])
        self._save_state()
        
        # Log edge
        edge_entry = {
            "timestamp": pos.entry_ts,
            "trade_id": trade_id,
            "city": signal["city"],
            "date": signal["date"],
            "temp": signal["temp"],
            "side": outcome,
            "our_prob": signal["our_prob"],
            "market_prob": signal["market_prob"],
            "edge_pp": signal["best_edge"],
            "forecast_max": signal.get("forecast_max", 0),
            "paper": self.paper_only,
        }
        try:
            with open(EDGE_LOG_FILE, "a") as f:
                f.write(json.dumps(edge_entry) + "\n")
        except Exception:
            pass
        
        return pos
    
    def settle_positions(self):
        """Check if any positions have resolved and settle them.
        
        Uses Polymarket Gamma API to check resolution via outcomePrices.
        Winning outcome has price=1.0, losing outcome has price=0.0.
        """
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        
        unsettled = [p for p in self.positions if not p.settled]
        if not unsettled:
            return
        
        for pos in unsettled:
            # Only check dates that have passed (with 6h grace for resolution)
            target_dt = datetime.strptime(pos.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if now < target_dt + timedelta(hours=6):
                continue
            
            # Check Polymarket for resolution using slug
            city = pos.city
            dt = datetime.strptime(pos.date, "%Y-%m-%d")
            month_name = dt.strftime("%B").lower()
            day = dt.day
            slug = f"highest-temperature-in-{city}-on-{month_name}-{day}-2026"
            
            try:
                r = requests.get(
                    f"https://gamma-api.polymarket.com/events",
                    params={"slug": slug},
                    timeout=15,
                )
                if r.status_code != 200 or not r.json():
                    continue
                ev = r.json()[0]
            except Exception as e:
                log.warning(f"Settlement API call failed for {slug}: {e}")
                continue
            
            # Find our specific market by condition_id
            pos_cid = pos.condition_id
            for m in ev.get("markets", []):
                mk_cid = m.get("conditionId", m.get("condition_id", ""))
                if mk_cid != pos_cid:
                    continue
                
                closed = m.get("closed", False)
                if not closed:
                    continue  # Not resolved yet
                
                # Get resolution from outcomePrices
                # Closed market: winning outcome price = 1.0, losing = 0.0
                prices_raw = m.get("outcomePrices", "[]")
                outcomes_raw = m.get("outcomes", "[]")
                try:
                    prices = [float(p) for p in (json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw)]
                    outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                except Exception:
                    continue
                
                if len(prices) < 2 or len(outcomes) < 2:
                    continue
                
                # Determine which outcome won
                winning_idx = 0 if prices[0] > prices[1] else 1
                winning_outcome = str(outcomes[winning_idx]).strip().upper()
                our_outcome = pos.outcome.upper()
                
                if our_outcome == winning_outcome:
                    pos.exit_price = 1.0
                    pos.pnl = pos.cost_usd * (1.0 / pos.entry_price - 1.0)
                    pos.settled = True
                    self.state.wins += 1
                    self.state.consecutive_losses = 0
                    log.info(f"✅ WON: {pos.trade_id} {pos.city} {pos.outcome} @ {pos.entry_price} | "
                             f"winning={winning_outcome} | P&L: +${pos.pnl:.2f}")
                else:
                    pos.exit_price = 0.0
                    pos.pnl = -pos.cost_usd
                    pos.settled = True
                    self.state.losses += 1
                    self.state.consecutive_losses += 1
                    self.state.daily_loss += pos.pnl
                    self.state.weekly_loss += pos.pnl
                    log.info(f"❌ LOST: {pos.trade_id} {pos.city} {pos.outcome} @ {pos.entry_price} | "
                             f"winning={winning_outcome} | P&L: ${pos.pnl:.2f}")
                
                pos.exit_ts = now.isoformat()
                self.state.total_pnl += pos.pnl
                self.state.bankroll += pos.pnl
                break
    
    def run_cycle(self):
        """One scan cycle: discover markets, compute edge, enter positions."""
        self.cycle_id += 1
        self.scan_count += 1
        
        log.info(f"{'='*60}")
        log.info(f"V1 Weather Bot — Cycle {self.cycle_id} | Paper={self.paper_only} | "
                 f"Bankroll=${self.state.bankroll:.2f} | Trades={self.state.total_trades}")
        
        # Settle any resolved positions
        self.settle_positions()
        
        # Scan for edge
        signals = self.scan_markets()
        
        if not signals:
            log.info(f"No edge opportunities (scan #{self.scan_count})")
            self._save_state()
            return
        
        log.info(f"Found {len(signals)} edge opportunities:")
        for sig in signals[:5]:
            log.info(f"  {sig['city']:12s} {sig['temp']}°C {sig['recommended_side']:3s} | "
                     f"edge={sig['best_edge']:.1f}pp | our={sig['our_prob']:.2f} "
                     f"market={sig['market_prob']:.2f} | vol=${sig['volume']:,.0f}")
        
        # Enter top opportunities
        entered = 0
        for sig in signals:
            if entered >= 2:  # Max 2 entries per cycle
                break
            pos = self.enter_position(sig)
            if pos:
                entered += 1
        
        self._save_state()
    
    def run(self, max_cycles: int = 0):
        """Main loop."""
        log.info("=" * 60)
        log.info("V1 FDC WEATHER BOT — INITIALIZATION")
        log.info("=" * 60)
        log.info(f"  Mode: {'PAPER' if self.paper_only else 'LIVE'}")
        log.info(f"  Position size: ${self.position_size}")
        log.info(f"  Bankroll: ${self.state.bankroll}")
        log.info(f"  Min edge: {self.min_edge_pp}pp")
        log.info(f"  Scan interval: {self.scan_interval}s")
        log.info(f"  Cities: {[c for c, cfg in CITIES.items() if not cfg['skip']]}")
        
        # Check on-chain balance for live mode
        if not self.paper_only:
            onchain_usdc = get_onchain_usdc()
            self.state.bankroll_actual_usd = onchain_usdc
            log.info(f"  On-chain USDC: ${onchain_usdc}")
        
        self._save_state()
        log.info("✓ Initialization complete")
        log.info(f"\nStarting V1 weather bot (scan every {self.scan_interval}s)...")
        
        cycle = 0
        while True:
            if max_cycles > 0 and cycle >= max_cycles:
                log.info(f"Max cycles ({max_cycles}) reached — stopping")
                break
            
            try:
                self.run_cycle()
            except KeyboardInterrupt:
                log.info("Interrupted — shutting down")
                break
            except Exception as e:
                log.error(f"Cycle error: {e}")
                traceback.print_exc()
            
            cycle += 1
            time.sleep(self.scan_interval)

    def status_report(self):
        """Print a concise status dashboard."""
        # Settle positions first
        self.settle_positions()
        
        active = [p for p in self.positions if not p.settled]
        settled = [p for p in self.positions if p.settled]
        wins = [p for p in settled if p.pnl > 0]
        losses = [p for p in settled if p.pnl <= 0]
        
        wr = (len(wins) / len(settled) * 100) if settled else 0
        total_pnl = sum(p.pnl for p in settled)
        avg_edge = sum(p.edge_pp for p in settled) / len(settled) if settled else 0
        
        print("\n" + "=" * 60)
        print("V1 FDC WEATHER BOT — STATUS")
        print("=" * 60)
        print(f"  Mode:       {'PAPER' if self.paper_only else 'LIVE'}")
        print(f"  Bankroll:   ${self.state.bankroll:.2f}")
        print(f"  Total P&L:  ${total_pnl:+.2f}")
        print(f"  Trades:     {len(settled)} settled / {len(active)} active")
        print(f"  Wins/Loss:  {len(wins)}/{len(losses)} | WR={wr:.1f}%")
        print(f"  Avg Edge:   {avg_edge:.1f}pp")
        print(f"  Halted:     {self.state.halted} {self.state.halt_reason}")
        
        if active:
            print(f"\n  ACTIVE POSITIONS:")
            for p in active:
                days_left = (datetime.strptime(p.date, "%Y-%m-%d").date() - datetime.now(timezone.utc).date()).days
                print(f"    {p.city:12s} {p.bucket_temp}°C {p.outcome:3s} @ {p.entry_price:.3f} | "
                      f"edge={p.edge_pp:.1f}pp | cost=${p.cost_usd:.2f} | "
                      f"forecast={p.forecast_temp}°C | T{days_left}d")
        
        # Current METAR observations for active cities
        print(f"\n  LIVE METAR:")
        for city, cfg in CITIES.items():
            if cfg["skip"]:
                continue
            metar = fetch_metar(cfg["icao"])
            if metar:
                temp_c = metar.get("temp", "?")
                print(f"    {city:12s} ({cfg['icao']}): {temp_c}°C")
            else:
                print(f"    {city:12s} ({cfg['icao']}): NO DATA")
        
        # On-chain balance (live only)
        if not self.paper_only:
            usdc = get_onchain_usdc()
            print(f"\n  On-chain USDC: ${usdc:.2f}")
        
        print("=" * 60)

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="V1 FDC Weather Bot")
    parser.add_argument("--paper", action="store_true", default=True, help="Paper mode (default)")
    parser.add_argument("--live", action="store_true", help="Live mode (REAL MONEY)")
    parser.add_argument("--bankroll", type=float, default=20.0, help="Starting bankroll (paper)")
    parser.add_argument("--position-size", type=float, default=2.0, help="Fixed position size $")
    parser.add_argument("--min-edge", type=float, default=15.0, help="Minimum edge in percentage points")
    parser.add_argument("--scan-interval", type=int, default=900, help="Seconds between scans")
    parser.add_argument("--max-cycles", type=int, default=0, help="Max cycles (0=infinite)")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--status", action="store_true", help="Print status dashboard and exit")
    
    args = parser.parse_args()
    
    bot = V1WeatherBot(
        paper_only=not args.live,
        position_size=args.position_size,
        min_edge_pp=args.min_edge,
        scan_interval=args.scan_interval,
    )
    
    if not args.paper:
        bot.paper_only = False
    
    if args.bankroll:
        bot.state.bankroll = args.bankroll
    
    if args.status:
        bot.status_report()
    elif args.once:
        bot.run_cycle()
    else:
        bot.run(max_cycles=args.max_cycles)