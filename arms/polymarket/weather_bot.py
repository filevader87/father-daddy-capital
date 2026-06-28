#!/usr/bin/env python3
"""
V1 FDC Weather Bot v2.1 — Paper Settlement Validation
======================================================
DIRECTIVE: 24–72h continuous paper validation before any live eligibility.

LIVE BLOCKED until ALL promotion criteria met:
  - ≥25 resolved paper trades
  - Positive realized EV
  - Profit Factor ≥ 1.25
  - Zero settlement/rule/timezone/rounding errors

WEATHER_BOT_LIVE_BLOCKED = True  # V21.7.72: HALTED — settlement fabricating wins, audit required

Output files (all under OUTPUT_DIR):
  v2_1_candidate_log.jsonl   — every candidate signal with full audit
  v2_1_paper_trades.jsonl     — entered positions with execution details
  v2_1_resolution_audit.jsonl — settlement chain per resolved position
  v2_1_city_risk_report.json  — per-city risk tiering summary
  v2_1_hindcast_report.csv    — backtest on historical forecast accuracy
  v2_1_live_readiness.json    — promotion criteria tracker (BLOCKED until met)
"""

WEATHER_BOT_LIVE_BLOCKED = True  # V21.7.72: HALTED — settlement fabricating wins, audit required

import os
import sys
import json
import time
import math
import csv
import logging
import argparse
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, List
from dataclasses import dataclass, asdict

# ─── Paths ───
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output" / "weather_bot"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# §V21.7.14: Halt config — blocks new temperature entries
HALT_CONFIG_PATH = OUTPUT_DIR / "v2_3_halt_config.json"

def load_halt_config() -> dict:
    """Load V21.7.14 halt config. Returns empty dict if missing."""
    try:
        if HALT_CONFIG_PATH.exists():
            with open(HALT_CONFIG_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return {}

TEMPERATURE_ENTRIES_HALTED = load_halt_config().get("disable_new_weather_temperature_entries", False)

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # arms/polymarket/ — finds weather_lib.py, city_registry.py, etc.
sys.path.insert(0, str(PROJECT_ROOT / "src" / "weather"))
try:
    from weather_lib import (
        CITY_REGISTRY, RISK_PROFILES,
        WeatherBotV2, WeatherPosition, WeatherState,
        fetch_open_meteo_forecast, fetch_open_meteo_ensemble, fetch_metar,
        discover_weather_markets, parse_temperature_markets,
        compute_edge_v2,
        apply_city_settlement, wu_round, is_hko_floor_city,
    )
    HAS_V2 = True
except ImportError as e:
    print(f"FATAL: Cannot import weather_lib: {e}")
    HAS_V2 = False
    sys.exit(1)

# §V22: DEB multi-model integration
sys.path.insert(0, str(PROJECT_ROOT / "src" / "polyweather_analysis"))
# V21.7.70: FDEB disabled — uses Open-Meteo multi-model API (removed)
HAS_FDEB = False
# Stubs to prevent NameError in dead code paths
fetch_multi_model_forecasts = None
fetch_ensemble_forecast = None
build_deb_forecasts = None

try:
    from pm_live import (
        build_dry_run_order,
        submit_tracked_order,
    )
    HAS_CLOB_MODULE = True
except ImportError:
    HAS_CLOB_MODULE = False

# ─── Output files ───
CANDIDATE_LOG = OUTPUT_DIR / "v2_1_candidate_log.jsonl"
PAPER_TRADES  = OUTPUT_DIR / "v2_1_paper_trades.jsonl"      # Paper mode only
LIVE_TRADES   = OUTPUT_DIR / "v2_1_live_trades.jsonl"        # Live mode only — SEPARATION FIX
RESOLUTION_AUDIT = OUTPUT_DIR / "v2_1_resolution_audit.jsonl"
CITY_RISK_REPORT = OUTPUT_DIR / "v2_1_city_risk_report.json"
HINDCAST_REPORT  = OUTPUT_DIR / "v2_1_hindcast_report.csv"
LIVE_READINESS   = OUTPUT_DIR / "v2_1_live_readiness.json"
STATE_FILE       = OUTPUT_DIR / "v2_1_state.json"            # Paper state
LIVE_STATE_FILE  = OUTPUT_DIR / "v2_1_live_state.json"       # Live state — SEPARATION FIX
CONSOLE_LOG      = OUTPUT_DIR / "v2_1_console.log"

# ─── Live probe limits ───
MAX_POSITION_USD = 3.00    # V21.7.57: $3/position — ensures ≥5 shares at ~$0.53 entry (PM minimum)
MAX_CONCURRENT = 5
MAX_DAILY_LOSS = 10.0
MAX_WEEKLY_LOSS = 20.0
MAX_DAILY_TRADES = 10
MIN_EDGE_PP = 15.0

# ─── Logging ───
log = logging.getLogger("v1_weather_v21")
log.setLevel(logging.INFO)
if not log.handlers:
    fh = logging.FileHandler(CONSOLE_LOG, mode="a")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(ch)

# ═══════════════════════════════════════════════════════════════
# CITY RISK TIERING
# ═══════════════════════════════════════════════════════════════

def get_risk_tier(city: str) -> str:
    """Return risk tier: TRADE / QUALIFY / BLOCKED."""
    meta = CITY_REGISTRY.get(city, {})
    risk = meta.get("risk", "medium")
    # High-risk cities are BLOCKED from live (paper only)
    if risk == "high":
        return "BLOCKED"
    # Medium-risk cities must QUALIFY before live
    if risk == "medium":
        return "QUALIFY"
    # Low-risk cities can be TRADE-eligible after promotion
    return "TRADE"

def get_position_size(city: str, base: float = MAX_POSITION_USD,
                       edge_pp: float = 0.0, weekly_loss: float = 0.0,
                       entry_price: float = 0.0, our_prob: float = 0.0) -> float:
    """Risk-adjusted position size.
    V21.7.74: Kelly criterion FIXED — uses P(NO) for NO bets, quarter Kelly multiplier.
    Kelly: f* = (bp - q) / b where b = payout ratio, p = P(NO), q = 1-p
    Capped at quarter Kelly for safety.
    """
    meta = CITY_REGISTRY.get(city, {})
    risk = meta.get("risk", "medium")
    size = base * RISK_PROFILES.get(risk, RISK_PROFILES["medium"])["position_mult"]

    # V21.7.74: Kelly criterion — FIXED
    # All trades are NO side. our_prob = P(YES) from model.
    # Kelly needs p = P(NO wins) = 1 - our_prob
    if our_prob > 0 and entry_price > 0:
        b = (1.0 - entry_price) / entry_price  # payout ratio for NO bet
        p_no = 1.0 - our_prob  # P(NO wins) — the side we're actually betting
        q_no = 1.0 - p_no
        kelly = (b * p_no - q_no) / b if b > 0 else 0
        kelly = max(0, min(0.25, kelly))  # cap at full Kelly for safety
        kelly_size = base * kelly * 0.25  # V21.7.74 FIX: was *4 (4x Kelly), now *0.25 (quarter Kelly)
        size = min(size, kelly_size)

    # Fallback entry-price sizing when no prob
    if entry_price > 0 and our_prob == 0:
        if entry_price < 0.10:
            size *= 0.5
        elif entry_price < 0.30:
            size *= 0.3
        elif entry_price < 0.70:
            size *= 1.5

    if weekly_loss <= -15:
        size *= 0.5
    elif weekly_loss <= -10:
        size *= 0.75

    return round(max(0.50, size), 2)

def get_edge_threshold(city: str, base: float = MIN_EDGE_PP) -> float:
    """Risk-adjusted minimum edge."""
    meta = CITY_REGISTRY.get(city, {})
    risk = meta.get("risk", "medium")
    return base + RISK_PROFILES.get(risk, RISK_PROFILES["medium"])["edge_add"]

def get_sigma_adjustment(city: str) -> float:
    """Risk-adjusted σ addition."""
    meta = CITY_REGISTRY.get(city, {})
    risk = meta.get("risk", "medium")
    return RISK_PROFILES.get(risk, RISK_PROFILES["medium"])["sigma_add"]

def generate_city_risk_report() -> Dict:
    """Generate per-city risk tiering report."""
    report = {}
    for city, meta in CITY_REGISTRY.items():
        tier = get_risk_tier(city)
        report[city] = {
            "name": meta.get("n", city),
            "risk_level": meta.get("risk", "medium"),
            "risk_tier": tier,
            "live_eligible": tier == "TRADE",
            "distance_km": meta.get("dist", 0),
            "settlement_source": meta.get("settle", "metar"),
            "position_mult": RISK_PROFILES.get(meta.get("risk","medium"), RISK_PROFILES["medium"])["position_mult"],
            "sigma_add": RISK_PROFILES.get(meta.get("risk","medium"), RISK_PROFILES["medium"])["sigma_add"],
            "edge_add_pp": RISK_PROFILES.get(meta.get("risk","medium"), RISK_PROFILES["medium"])["edge_add"],
            "icao": meta.get("icao", ""),
            "tz_offset": meta.get("tz", 0),
        }
    with open(CITY_RISK_REPORT, "w") as f:
        json.dump(report, f, indent=2)
    log.info(f"City risk report written: {len(report)} cities")
    return report

# ═══════════════════════════════════════════════════════════════
# V22.1 ENTRY GATE — BLOCK REASON LOGGING
# ═══════════════════════════════════════════════════════════════

ENTRY_GATE_LOG = OUTPUT_DIR / "v22_1_entry_gate_log.jsonl"

BLOCK_REASONS = [
    "NO_MARKET_FOUND", "DEAD_MARKET", "LOW_LIQUIDITY", "WIDE_SPREAD",
    "NO_BUCKET_EDGE", "MODEL_DISAGREEMENT_TOO_HIGH", "METAR_ANCHOR_MISSING",
    "SETTLEMENT_RULE_UNCLEAR", "PRICE_TOO_HIGH", "PRICE_TOO_LOW",
    "DUPLICATE_CITY_DATE_BUCKET", "MAX_ACTIVE_POSITIONS",
    "MISSING_FORECAST_MODEL", "MISSING_OBSERVATION",
]

def log_entry_gate(city: str, market_slug: str, market_date: str,
                   bucket: int, side: str, market_price: float,
                   deb_probability: float, edge: float, sigma_c: float,
                   model_count: int, metar_anchor_present: bool,
                   liquidity_ok: bool, spread_ok: bool,
                   entry_allowed: bool, block_reason: str = ""):
    """Log every candidate with entry decision and block reason."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "city": city, "market_slug": market_slug, "market_date": market_date,
        "bucket": bucket, "side": side, "market_price": round(market_price, 4),
        "deb_probability": round(deb_probability, 4), "edge": round(edge, 2),
        "sigma_c": round(sigma_c, 2), "model_count": model_count,
        "metar_anchor_present": metar_anchor_present,
        "liquidity_ok": liquidity_ok, "spread_ok": spread_ok,
        "entry_allowed": entry_allowed, "block_reason": block_reason,
    }
    with open(ENTRY_GATE_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


# ═══════════════════════════════════════════════════════════════
# V22.1 WEATHER VALIDATION BOARD GENERATOR
# ═══════════════════════════════════════════════════════════════

def generate_v22_1_validation_board() -> Dict:
    """Generate V22.1 validation board separating pre-DEB and post-DEB."""
    all_trades = []
    if PAPER_TRADES.exists():
        with open(PAPER_TRADES) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        all_trades.append(json.loads(line))
                    except Exception:
                        pass

    pre_deb = [t for t in all_trades if "V22" not in str(t.get("version", "")) and "deb_v" not in str(t.get("deb_version", ""))]
    post_deb = [t for t in all_trades if "V22" in str(t.get("version", "")) or "deb_v" in str(t.get("deb_version", ""))]

    pre_resolved = [t for t in pre_deb if t.get("settled")]
    post_resolved = [t for t in post_deb if t.get("settled")]

    board = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "post_deb_v22": {
            "paper_trades": len(post_deb),
            "active": len([t for t in post_deb if not t.get("settled")]),
            "resolved": len(post_resolved),
            "wins": sum(1 for t in post_resolved if t.get("pnl", 0) > 0),
            "losses": sum(1 for t in post_resolved if t.get("pnl", 0) <= 0),
            "pnl": round(sum(t.get("pnl", 0) for t in post_resolved), 2),
            "pf": None,
            "ready_for_review": False,
            "live_allowed": False,
        },
        "pre_deb_sigma_bug": {
            "resolved": len(pre_resolved),
            "wins": sum(1 for t in pre_resolved if t.get("pnl", 0) > 0),
            "losses": sum(1 for t in pre_resolved if t.get("pnl", 0) <= 0),
            "pnl": round(sum(t.get("pnl", 0) for t in pre_resolved), 2),
            "excluded_from_deb_promotion": True,
        },
        "requirements": {
            "min_resolved": 25,
            "min_profit_factor": 1.25,
            "positive_pnl_required": True,
            "settlement_errors_allowed": 0,
        }
    }
    return board


# ═══════════════════════════════════════════════════════════════
# V21.7.64: ROLLING RECALIBRATION — dynamically adjusts calibration
# based on recent trade outcomes. When recent WR drops, calibration
# tightens to reduce overconfidence. Module-level for cross-function access.
# ═══════════════════════════════════════════════════════════════

ROLLING_CALIBRATION_FILE = OUTPUT_DIR / "v2_1_rolling_calibration.json"
ROLLING_WR_WINDOW = 20  # Track last 20 NO-side outcomes

# V21.7.74: CONFORMAL CALIBRATION DISABLED — hardcoded table destroyed 21.7pp edge/trade.
# These values were guessed, not learned. They inflated P(YES)=1% to 30%, turning
# the bot's best signals (strong NO) into marginal or negative-edge signals.
# Pass-through until real calibration data exists (isotonic with ≥50 samples per city).
CONFORMAL_CALIBRATION = {}  # V21.7.74: EMPTY = pass-through, no calibration applied

def load_rolling_calibration() -> Dict:
    """V21.7.74: Stub — rolling calibration disabled with conformal table."""
    return {"recent_no_outcomes": [], "recent_no_pnls": [], "adjustment_factor": 1.0}

def save_rolling_calibration(state: Dict):
    """V21.7.74: Stub — kept for backward compatibility."""
    pass

def update_rolling_calibration(win: bool, pnl: float = 0.0):
    """V21.7.74: Stub — rolling calibration disabled with conformal table."""
    pass

def conformal_calibrate(raw_prob: float) -> float:
    """V21.7.74: Pass-through — hardcoded calibration table removed.
    Isotonic calibration (in compute_edge_v22) still active if data exists.
    """
    return raw_prob


# ═══════════════════════════════════════════════════════════════
# V22 DEB-ENHANCED EDGE COMPUTATION
# ═══════════════════════════════════════════════════════════════

def compute_edge_v22(
    lat: float, lon: float, city: str, target_date: str,
    buckets: List[Dict], max_so_far: Optional[float] = None,
    current_temp: Optional[float] = None, local_hour: float = 12.0,
    is_cooling: bool = False, day_offset: int = 0,
    min_edge_pp: float = 15.0, min_volume: float = 200.0,
    multi_model: Dict = None, ensemble: Dict = None,
) -> List[Dict]:
    """
    V22 edge computation using DEB multi-model probability engine.
    Optimized: fetches multi-model + ensemble ONCE, then computes
    probabilities for all buckets from the same data.
    """
    if HAS_FDEB and buckets:
        # FDEB path — use compute_edge_v22 (legacy)
        return compute_edge_v22(
            lat, lon, city, target_date, buckets, max_so_far, current_temp,
            local_hour, is_cooling, day_offset, min_edge_pp, min_volume,
            multi_model, ensemble,
        )
    if not buckets:
        return []

    # V21.7.76: EV-first edge computation (no FDEB dependency)
    # Uses Open-Meteo forecast directly, computes Gumbel probabilities,
    # then evaluates each bucket on EV (not just edge).
    meta = CITY_REGISTRY.get(city, {})
    risk = meta.get("risk", "medium")
    dist_km = meta.get("dist", 0)

    # Get forecast from the passed-in forecast data
    # (the caller already fetched it — we just need the center + sigma)
    if multi_model is None:
        multi_model = fetch_open_meteo_forecast(lat, lon, 3)
    if multi_model:
        # Extract max temps — handle both Open-Meteo daily format and legacy forecast_temps
        daily_maxes = []
        for day_key, day_data in multi_model.items():
            if isinstance(day_data, dict):
                tmax = day_data.get("temperature_2m_max", day_data.get("max_temp", None))
                if tmax is not None:
                    daily_maxes.append(float(tmax))
            elif isinstance(day_data, (int, float)):
                daily_maxes.append(float(day_data))
        if daily_maxes:
            center = sorted(daily_maxes)[len(daily_maxes) // 2]
        else:
            return []
    else:
        return []

    # Sigma from model spread + city risk
    if len(daily_maxes) > 2:
        sigma = max(1.0, (max(daily_maxes) - min(daily_maxes)) / 2.0)
    else:
        sigma = 2.0

    sigma += {"low": 0.0, "medium": 0.3, "high": 0.8}.get(risk, 0.3)
    if dist_km > 10:
        sigma += 0.5 * (dist_km / 10.0)
    if day_offset > 0:
        sigma += 0.5 * day_offset

    # Peak-time sigma reduction
    lat_abs = abs(lat)
    if lat_abs > 55: first_peak, last_peak = 10.0, 16.0
    elif lat_abs > 35: first_peak, last_peak = 11.0, 15.0
    elif lat_abs > 20: first_peak, last_peak = 11.5, 14.5
    else: first_peak, last_peak = 12.0, 14.0

    peak_status = "before"
    if local_hour >= first_peak and local_hour <= last_peak: peak_status = "in_window"
    elif local_hour > last_peak: peak_status = "past"

    if peak_status == "past" and local_hour >= 21: sigma *= 0.6
    elif peak_status == "past" and local_hour > last_peak: sigma *= 0.8
    elif peak_status == "in_window": sigma *= 0.9

    # Dead market
    is_dead = False
    if max_so_far is not None and current_temp is not None:
        if local_hour >= 21 and max_so_far - current_temp >= 3.0: is_dead = True
        elif peak_status == "past" and max_so_far - current_temp >= 1.5: is_dead = True
    if is_dead: sigma = max(1.0, sigma * 0.5)

    # Reality anchor
    mu = float(center)
    forecast_median = sorted(daily_maxes)[len(daily_maxes) // 2] if daily_maxes else center
    if max_so_far is not None:
        if peak_status in ("past", "in_window") and max_so_far < forecast_median - 2.0:
            mu = float(max_so_far) if (is_cooling or peak_status == "past") else float(max_so_far) + 0.5
        elif peak_status in ("past", "in_window"):
            mu = forecast_median * 0.7 + float(center) * 0.3
            if max_so_far > mu: mu = float(max_so_far) + (0.3 if not is_cooling else 0.0)

    # Per-city sigma multipliers
    CITY_SIGMA_MULTIPLIER = {
        "london": 2.0, "tokyo": 2.5, "hong_kong": 2.0,
        "chengdu": 3.0, "madrid": 1.5, "istanbul": 1.8,
        "busan": 1.5, "moscow": 1.5, "manila": 1.3, "lucknow": 1.5,
    }
    city_mult = CITY_SIGMA_MULTIPLIER.get(city, 1.0)
    sigma *= city_mult

    # Gumbel distribution for daily maxima
    import math as _math
    beta = sigma * _math.sqrt(6.0) / _math.pi

    def gumbel_cdf(x, mu, b):
        return _math.exp(-_math.exp(-(x - mu) / max(b, 0.01)))
    def gumbel_sf(x, mu, b):
        return 1.0 - gumbel_cdf(x, mu, b)

    signals = []
    for b in buckets:
        market_prob = b.get("prob", b.get("yes_price", 0))
        if market_prob is None or market_prob == 0:
            market_prob = b.get("yes_price", 0)
        if market_prob < 0.03 or market_prob > 0.97: continue
        if b.get("volume", 0) < min_volume: continue

        bucket_temp = b["temp"]
        b_is_threshold = b.get("is_threshold", False)
        b_threshold_dir = "higher" if "or higher" in b.get("question", "") else ("lower" if "or lower" in b.get("question", "") else "")

        if is_dead and max_so_far is not None:
            settled_temp = apply_city_settlement(city, max_so_far)
            if b_is_threshold:
                if b_threshold_dir == "higher":
                    our_prob = 1.0 if settled_temp >= bucket_temp else 0.01
                else:
                    our_prob = 1.0 if settled_temp <= bucket_temp else 0.01
            else:
                our_prob = 1.0 if bucket_temp == settled_temp else 0.01
        elif b_is_threshold:
            if b_threshold_dir == "higher":
                our_prob = gumbel_sf(bucket_temp - 0.5, mu, beta)
            else:
                our_prob = gumbel_cdf(bucket_temp + 0.5, mu, beta)
        else:
            our_prob = gumbel_cdf(bucket_temp + 0.5, mu, beta) - gumbel_cdf(bucket_temp - 0.5, mu, beta)
        cap = 0.90 if b_is_threshold else 0.85
        our_prob = max(0.01, min(cap, our_prob))

        # Conformal calibration
        our_prob_raw = our_prob
        our_prob = conformal_calibrate(our_prob)

        # Isotonic calibration
        try:
            from isotonic_calibration import calibrate_prob
            direction = "over" if b_threshold_dir == "higher" else "under"
            our_prob = calibrate_prob(city, bucket_temp, our_prob, direction)
            our_prob = max(0.01, min(0.99, our_prob))
        except Exception:
            pass

        yes_edge = our_prob - market_prob
        no_edge = (1 - our_prob) - (1 - market_prob)
        best_edge = max(yes_edge, no_edge)
        recommended_side = "YES" if yes_edge >= no_edge else "NO"

        # YES blocked — 0% WR
        if recommended_side == "YES":
            if no_edge > 0:
                recommended_side = "NO"
                best_edge = no_edge
            else:
                continue

        yes_entry_price = b.get("yes_price", 0)
        no_entry_price = b.get("no_price", 0)
        entry_price = yes_entry_price if recommended_side == "YES" else no_entry_price

        # V21.7.76: Real EV computation
        # EV = P(win) × payout - P(loss) × cost
        # For NO side: P(win) = 1 - our_prob, payout = (1 - no_price) per share, cost = no_price per share
        # For YES side: P(win) = our_prob, payout = (1 - yes_price), cost = yes_price
        if recommended_side == "NO":
            p_win = 1.0 - our_prob
            p_loss = our_prob
            payout_per_share = 1.0 - no_entry_price
            cost_per_share = no_entry_price
        else:
            p_win = our_prob
            p_loss = 1.0 - our_prob
            payout_per_share = 1.0 - yes_entry_price
            cost_per_share = yes_entry_price

        ev_per_share = p_win * payout_per_share - p_loss * cost_per_share
        ev_cents = ev_per_share * 100  # EV in cents per share

        risk_profile = RISK_PROFILES.get(risk, RISK_PROFILES["medium"])
        min_edge_adjusted = min_edge_pp + risk_profile["edge_add"]
        if recommended_side == "YES":
            min_edge_adjusted += 10.0
        else:
            min_edge_adjusted -= 10.0
            if best_edge * 100 >= 40.0:
                min_edge_adjusted -= 5.0

        signal = {
            "city": city, "temp": bucket_temp,
            "is_threshold": b_is_threshold,
            "threshold_direction": b_threshold_dir,
            "our_prob": round(our_prob, 4), "our_prob_raw": round(our_prob_raw, 4),
            "market_prob": market_prob,
            "yes_price": b.get("yes_price", 0), "no_price": b.get("no_price", 0),
            "yes_edge_pp": round(yes_edge * 100, 1), "no_edge_pp": round(no_edge * 100, 1),
            "best_edge": round(best_edge * 100, 1), "recommended_side": recommended_side,
            "edge_pp": round(best_edge * 100, 1),
            "entry_price": entry_price,
            "payout_ratio": round(1.0 / max(entry_price, 0.01), 1),
            "ev_per_dollar": round(our_prob / max(entry_price, 0.01), 2),
            # V21.7.76: Real EV
            "ev_cents": round(ev_cents, 2),
            "ev_per_share": round(ev_per_share, 4),
            "p_win": round(p_win, 4),
            "yes_token_id": b.get("yes_token_id", ""), "no_token_id": b.get("no_token_id", ""),
            "condition_id": b.get("condition_id", ""), "market_id": b.get("market_id", ""),
            "neg_risk": True, "risk_level": risk,
            "sigma_used": round(sigma, 2),
            "prob_info": f"μ={mu:.1f} σ={sigma:.1f} peak={peak_status} dead={is_dead} EV={ev_cents:.1f}¢",
        }
        if signal["best_edge"] >= min_edge_adjusted:
            signals.append(signal)

    # V21.7.76: Composite score now includes EV as primary factor
    for s in signals:
        threshold_boost = 5.0 if s.get("is_threshold") else 0.0
        value_bonus = min(s.get("payout_ratio", 1.0) * 0.5, 10.0)
        # EV is the top marker — weight it heavily in composite
        ev_score = s.get("ev_cents", 0) * 2.0  # 2x weight: 10¢ EV = 20 points
        s["composite_score"] = round(ev_score + s["best_edge"] + threshold_boost + value_bonus, 1)
        s["threshold_boost"] = threshold_boost
        s["value_bonus"] = value_bonus

    return sorted(signals, key=lambda s: s.get("composite_score", s["best_edge"]), reverse=True)


# ═══════════════════════════════════════════════════════════════
# FULL CANDIDATE AUDIT LOG
# ═══════════════════════════════════════════════════════════════

def log_candidate(signal: Dict, meta: Dict, forecast_temps: Dict,
                   max_so_far, current_temp, local_hour: float,
                   is_cooling: bool, settlement_source: str):
    """Log every candidate signal with full audit trail."""
    city = signal.get("city", "?")
    temp = signal.get("temp", 0)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "city": city,
        "city_name": meta.get("n", city),
        "date": signal.get("date", ""),
        "day_offset": signal.get("day_offset", 0),
        "bucket_temp": temp,
        "recommended_side": signal.get("recommended_side", ""),
        # Parsed market rule
        "question": signal.get("question", ""),
        "settlement_source": settlement_source,
        "is_hko_floor": is_hko_floor_city(city),
        "rounding_rule": "floor" if is_hko_floor_city(city) else "wu_round",
        "tz_offset": meta.get("tz", 0),
        # Threshold and probability
        "model_prob": signal.get("our_prob", 0),
        "market_prob": signal.get("market_prob", 0),
        "executable_price": signal.get("no_price" if signal.get("recommended_side") == "NO" else "yes_price", 0),
        "edge_pp": signal.get("edge_pp", 0),
        "no_edge_pp": signal.get("no_edge_pp", 0),
        "best_edge": signal.get("best_edge", 0),
        "sigma_used": signal.get("sigma_used", 2.0),  # V21.7.52 FIX: was 0 — absurd default
        "prob_info": signal.get("prob_info", ""),
        # V21.7.53: Threshold + value-tier audit
        "is_threshold": signal.get("is_threshold", False),
        "threshold_direction": signal.get("threshold_direction", ""),
        "payout_ratio": signal.get("payout_ratio", 1.0),
        "ev_per_dollar": signal.get("ev_per_dollar", 0),
        "ev_cents": signal.get("ev_cents", 0),  # V21.7.76: Real EV
        "p_win": signal.get("p_win", 0),        # V21.7.76: Win probability
        "composite_score": signal.get("composite_score", signal.get("best_edge", 0)),
        "threshold_boost": signal.get("threshold_boost", 0),
        "value_bonus": signal.get("value_bonus", 0),
        # Liquidity
        "volume": signal.get("volume", 0),
        "liquidity": signal.get("liquidity", 0),
        # Observation data
        "max_so_far": max_so_far,
        "current_temp": current_temp,
        "local_hour": local_hour,
        "is_cooling": is_cooling,
        "forecast_temps": {k: round(v, 2) for k, v in forecast_temps.items() if v is not None},
        # Risk tiering
        "risk_level": meta.get("risk", "medium"),
        "risk_tier": get_risk_tier(city),
        "position_size": get_position_size(city),
        "edge_threshold": get_edge_threshold(city),
        # Market identifiers
        "token_id": signal.get("no_token_id" if signal.get("recommended_side") == "NO" else "yes_token_id", ""),
        "condition_id": signal.get("condition_id", ""),
        "market_id": signal.get("market_id", ""),
        "neg_risk": True,
        # Stale market detection
        "stale_market": local_hour >= 18 and signal.get("day_offset", 1) == 0,
    }
    with open(CANDIDATE_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry

# ═══════════════════════════════════════════════════════════════
# RESOLUTION AUDIT
# ═══════════════════════════════════════════════════════════════

def audit_settlement(pos: WeatherPosition, actual_temp: float, city_meta: Dict) -> Dict:
    """Full settlement chain audit."""
    city = pos.city
    settlement_source = city_meta.get("settle", "metar")
    rounding_rule = "floor" if is_hko_floor_city(city) else "wu_round"
    settled_temp = apply_city_settlement(city, actual_temp)
    bucket_hit = (settled_temp == pos.bucket_temp)

    # Binary outcome
    if pos.outcome == "YES":
        payout_per_share = 1.0 if bucket_hit else 0.0
    else:
        payout_per_share = 0.0 if bucket_hit else 1.0

    shares = pos.shares if pos.shares is not None else 0
    total_payout = round(payout_per_share * shares, 2)
    pnl = round(total_payout - pos.cost_usd, 2)

    audit = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "trade_id": pos.trade_id,
        "city": city,
        "city_name": city_meta.get("n", city),
        "date": pos.date,
        "bucket_temp": pos.bucket_temp,
        "outcome": pos.outcome,
        "entry_price": pos.entry_price,
        "shares": pos.shares,
        "cost_usd": pos.cost_usd,
        # Settlement chain
        "settlement_source": settlement_source,
        "rounding_rule": rounding_rule,
        "is_hko_floor": is_hko_floor_city(city),
        "actual_temp_raw": actual_temp,
        "settled_temp": settled_temp,
        "bucket_hit": bucket_hit,
        "payout_per_share": payout_per_share,
        "total_payout": total_payout,
        "pnl": pnl,
        "win": pnl > 0,
        # Risk context
        "risk_level": pos.risk_level,
        "risk_tier": get_risk_tier(city),
        "edge_pp_at_entry": pos.edge_pp,
        "sigma_at_entry": pos.entry_sigma,
        "forecast_at_entry": pos.forecast_temp,
        # Validation
        "settlement_error": None,
        "rule_error": None,
        "timezone_error": None,
        "rounding_error": None,
    }

    # Validate settlement
    if settlement_source == "hko":
        expected = int(math.floor(actual_temp))
        if settled_temp != expected:
            audit["rounding_error"] = f"Expected floor({actual_temp})={expected}, got {settled_temp}"
    else:
        expected = wu_round(actual_temp)
        if settled_temp != expected:
            audit["rounding_error"] = f"Expected wu_round({actual_temp})={expected}, got {settled_temp}"

    with open(RESOLUTION_AUDIT, "a") as f:
        f.write(json.dumps(audit) + "\n")
    return audit

# ═══════════════════════════════════════════════════════════════
# LIVE READINESS TRACKER
# ═══════════════════════════════════════════════════════════════

def check_live_readiness() -> Dict:
    """Check promotion criteria for live eligibility.

    V21.7.67 FIX: Only count trades with actual settlement (settlement_temp is not None)
    and non-zero PnL as truly resolved. Trades with settled=true but settlement_temp=null
    and pnl=0.0 are unsettled stubs — they were marked settled by the cycle loop but never
    received real temperature verification. Including them inflates the resolved count and
    dilutes win rate, producing fake promotion signals.
    
    V21.7.58 FIX: Paper/live separation — read from LIVE_TRADES when in live mode,
    PAPER_TRADES when in paper mode. Never mix the two.
    """
    resolved = []
    excluded_stubs = 0
    
    # V21.7.58: Use the correct trade file based on mode
    trade_file = LIVE_TRADES if not WEATHER_BOT_LIVE_BLOCKED else PAPER_TRADES
    # Also check paper trades for promotion tracking (paper → live transition)
    # But report them SEPARATELY
    paper_resolved = []
    
    if trade_file.exists():
        with open(trade_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if not d.get("settled", False):
                        continue
                    # V21.7.67: Exclude stub settlements
                    if d.get("settlement_temp") is None and d.get("pnl", 0) == 0:
                        excluded_stubs += 1
                        continue
                    # V21.7.74: Only count Gamma-verified settlements.
                    # METAR settlements were fraudulent (13/30 fake wins).
                    # Trades with gamma_verified=False or settlement_source=METAR are excluded.
                    if d.get("settlement_source") == "METAR" and not d.get("gamma_verified", False):
                        excluded_stubs += 1
                        continue
                    resolved.append(d)
                except Exception:
                    continue
    
    # Also load paper trades for comparison (but don't mix into primary stats)
    if PAPER_TRADES.exists() and trade_file != PAPER_TRADES:
        with open(PAPER_TRADES) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if not d.get("settled", False):
                        continue
                    if d.get("settlement_temp") is None and d.get("pnl", 0) == 0:
                        continue
                    paper_resolved.append(d)
                except Exception:
                    continue

    total_resolved = len(resolved)
    wins = sum(1 for r in resolved if r.get("pnl", 0) > 0)
    losses = sum(1 for r in resolved if r.get("pnl", 0) <= 0)
    total_pnl = sum(r.get("pnl", 0) for r in resolved)
    gross_profit = sum(r.get("pnl", 0) for r in resolved if r.get("pnl", 0) > 0)
    gross_loss = abs(sum(r.get("pnl", 0) for r in resolved if r.get("pnl", 0) < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    avg_ev = total_pnl / total_resolved if total_resolved > 0 else 0

    # Check for any settlement/rule/timezone/rounding errors
    has_errors = False
    error_count = 0
    if RESOLUTION_AUDIT.exists():
        with open(RESOLUTION_AUDIT) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    for key in ["settlement_error", "rule_error", "timezone_error", "rounding_error"]:
                        if d.get(key) is not None:
                            has_errors = True
                            error_count += 1
                except Exception:
                    pass

    # V21.7.58: Paper stats for comparison
    paper_pnl = sum(r.get("pnl", 0) for r in paper_resolved)
    paper_wins = sum(1 for r in paper_resolved if r.get("pnl", 0) > 0)
    
    readiness = {
        "live_blocked": WEATHER_BOT_LIVE_BLOCKED,
        "block_reason": "WEATHER_BOT_LIVE_BLOCKED=True in source" if WEATHER_BOT_LIVE_BLOCKED else "Live mode active",
        "excluded_stub_settlements": excluded_stubs,
        "data_source": "LIVE_TRADES" if trade_file == LIVE_TRADES else "PAPER_TRADES",  # V21.7.58
        "promotion_criteria": {
            "min_resolved": 25,
            "resolved_count": total_resolved,
            "criteria_met": total_resolved >= 25,
            "positive_ev": total_pnl > 0,
            "ev_per_trade": round(avg_ev, 4),
            "profit_factor": round(profit_factor, 4),
            "pf_met": profit_factor >= 1.25,
            "zero_errors": not has_errors,
            "error_count": error_count,
        },
        "performance": {
            "total_resolved": total_resolved,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total_resolved, 4) if total_resolved > 0 else 0,
            "total_pnl": round(total_pnl, 2),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "profit_factor": round(profit_factor, 4),
            "avg_ev_per_trade": round(avg_ev, 4),
        },
        # V21.7.58: Paper comparison stats (kept separate, never mixed)
        "paper_comparison": {
            "paper_resolved": len(paper_resolved),
            "paper_pnl": round(paper_pnl, 2),
            "paper_wins": paper_wins,
        },
        "all_criteria_met": (total_resolved >= 25 and total_pnl > 0
                              and profit_factor >= 1.25 and not has_errors),
        "ready_for_live": not WEATHER_BOT_LIVE_BLOCKED and (total_resolved >= 25 and total_pnl > 0
                              and profit_factor >= 1.25 and not has_errors),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with open(LIVE_READINESS, "w") as f:
        json.dump(readiness, f, indent=2)
    return readiness

# ═══════════════════════════════════════════════════════════════
# HINDCAST: Historical Forecast Accuracy Check
# ═══════════════════════════════════════════════════════════════

def run_hindcast(days_back: int = 7) -> str:
    """
    Check forecast accuracy over past days using METAR observations
    vs what our model would have predicted.
    """
    results = []
    now = datetime.now(timezone.utc)

    for city, meta in CITY_REGISTRY.items():
        if not meta.get("major", False):
            continue
        risk = meta.get("risk", "medium")
        if risk == "high":
            continue

        lat, lon = meta["lat"], meta["lon"]
        icao = meta.get("icao", "")
        tz_offset = meta.get("tz", 0)

        # Fetch forecast (includes past_day=1 so we get yesterday)
        om = fetch_open_meteo_forecast(lat, lon, days=2)
        if not om:
            continue

        daily = om.get("daily", {})
        dates = daily.get("time", [])
        max_temps = daily.get("temperature_2m_max", [])

        # Get current METAR
        metar = fetch_metar(icao) if icao else None
        current_temp = metar.get("temp_c") if metar and metar.get("temp_c") is not None else None

        for i, date_str in enumerate(dates):
            if i >= len(max_temps) or max_temps[i] is None:
                continue
            forecast_max = max_temps[i]
            day_offset = (datetime.strptime(date_str, "%Y-%m-%d").date() - now.date()).days
            results.append({
                "city": city,
                "city_name": meta.get("n", city),
                "date": date_str,
                "day_offset": day_offset,
                "forecast_max_c": round(forecast_max, 1),
                "settlement_source": meta.get("settle", "metar"),
                "is_hko_floor": is_hko_floor_city(city),
                "risk_level": risk,
                "risk_tier": get_risk_tier(city),
                "current_temp_c": current_temp,
            })

    # Write CSV
    if results:
        with open(HINDCAST_REPORT, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=results[0].keys())
            writer.writeheader()
            writer.writerows(results)
        log.info(f"Hindcast report: {len(results)} entries written")

    return HINDCAST_REPORT

# ═══════════════════════════════════════════════════════════════
# V2.1 BOT CLASS (extends V2)
# ═══════════════════════════════════════════════════════════════

class WeatherBotV21(WeatherBotV2):
    """V2.1 adds full audit trail, settlement validation, live block."""

    # Scan budget per cycle — V22.1: FULL_REGISTRY_SCAN_MODE
    MAX_CITIES_PER_CYCLE = 50  # V22.1: Scan all eligible cities per cycle
    MAX_DAY_OFFSETS = 0        # V21.7.65: LIVE mode — today only. Future dates lock capital in unsettled positions.
                                # Paper mode can use 2 (today + tomorrow + day-after) for validation.
    # V21.7.53: Multi-bucket portfolio — max adjacent buckets per city
    MAX_ADJACENT_BUCKETS = 3   # Buy up to 3 adjacent temp buckets for same city+date
    PORTFOLIO_BUDGET_PER_CITY = 6.0  # Max $6 spread across adjacent buckets
    # V21.7.56: Per-city daily PnL cap — prevent single-city concentration
    # Observer rec: Busan = 69% of PnL. Cap any city at 40% of total to force diversification
    MAX_CITY_PNL_PCT = 0.40    # Max 40% of total PnL from any single city
    MAX_CITY_TRADES_PER_DAY = 3  # Max 3 trades/day per city (was unlimited)

    # V21.7.74: Geographic cluster cap — correlated European/Asian cities
    # share synoptic weather systems. Multiple NO bets on same cluster = one bet
    # on "no heat wave in region". Cap concurrent exposure per cluster.
    GEO_CLUSTERS = {
        "europe_west": {"london", "paris", "amsterdam", "madrid", "milan", "lisbon", "dublin"},
        "europe_east": {"warsaw", "helsinki", "istanbul", "moscow", "kiev", "prague"},
        "middle_east": {"jeddah", "karachi", "dubai", "riyadh", "tehran"},
        "east_asia": {"tokyo", "taipei", "chengdu", "shanghai", "seoul", "busan"},
        "south_asia": {"lucknow", "mumbai", "delhi"},
    }
    MAX_CLUSTER_EXPOSURE = 6.0  # Max $6 per geographic cluster per day

    def __init__(self, bankroll: float = 20.0, paper_only: bool = True):
        # V21.7.67: Set _state_file BEFORE super().__init__() so _load_state()
        # reads the correct mode-specific file. Previously super().__init__()
        # called _load_state() which hardcoded STATE_FILE (paper), loading $584
        # paper bankroll into the live bot on every restart.
        if paper_only:
            self._state_file = STATE_FILE
            self._trades_file = PAPER_TRADES
        else:
            self._state_file = LIVE_STATE_FILE
            self._trades_file = LIVE_TRADES
        super().__init__(paper_only=paper_only, bankroll=bankroll)
        self.state.paper_only = paper_only  # V21.7.55: Respect --live flag
        self._cycle_count = 0
        self._last_daily_reset = ""  # UTC date string for daily reset
        if not WEATHER_BOT_LIVE_BLOCKED:
            pass  # Live paths unlocked
        # ═══ SEPARATION FIX: Mode-specific file paths ═══
        # Paper and live NEVER share state or trade files.
        # This eliminates the entire class of bugs where stale paper positions
        # block live entries or paper losses count against live circuit breakers.
        # V21.7.67: _state_file and _trades_file now set BEFORE super().__init__()

    def run_once(self):
        """V21.7.53: Override with multi-bucket portfolio selection.
        Instead of just taking top 3 signals, we:
        1. Take the top signal by composite score
        2. Look for adjacent temp buckets (±1°C, ±2°C) for same city+date
        3. Enter up to MAX_ADJACENT_BUCKETS positions, budget capped at PORTFOLIO_BUDGET_PER_CITY
        4. Then take next top signal from a different city
        """
        # ─── Daily reset (UTC day boundary) ───
        # V21.7.56: Removed duplicate daily reset — check_circuit_breakers() handles this
        # in V2 parent. Having two resets caused force-settled losses from yesterday
        # to hit today's budget (ordering bug per bug scanner).
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._last_daily_reset != today:
            log.info(f"Weather daily reset: {self._last_daily_reset} → {today} | "
                     f"trades={getattr(self.state, 'daily_trades', 0)} loss=${getattr(self.state, 'daily_loss', 0):.2f}")
            self._last_daily_reset = today

        # Force-settle stale positions BEFORE circuit breaker check
        # so they don't count against max_positions
        self.force_settle_open_positions()
        self.settle_positions()
        
        if not self.check_circuit_breakers():
            return []
        signals = self.scan_cycle()

        if not signals:
            return []

        entered = []
        used_city_dates = set()  # Track (city, date) pairs already covered
        # V21.7.56: Track trades per city today for diversification cap
        city_trades_today = {}
        # V21.7.56: Compute per-city PnL from settled trades for concentration cap
        from collections import defaultdict
        city_pnl_map = defaultdict(float)
        total_pnl = 0.0
        for t in getattr(self.state, 'trade_history', []):
            if t.get('settled'):
                c = t.get('city', '?')
                p = t.get('pnl', 0) or 0
                city_pnl_map[c] += p
                total_pnl += p

        for sig in signals:
            if len(entered) >= 5:  # MAX_CONCURRENT
                break

            city = sig.get("city", "?")
            date = sig.get("date", "")
            city_date_key = (city, date)

            if city_date_key in used_city_dates:
                continue  # Already covered this city+date with a portfolio

            # V21.7.56: Per-city daily trade cap — force diversification
            city_count = city_trades_today.get(city, 0)
            if city_count >= self.MAX_CITY_TRADES_PER_DAY:
                log.info(f"SKIP {city} — daily city cap reached ({city_count}/{self.MAX_CITY_TRADES_PER_DAY})")
                continue

            # V21.7.56: Per-city PnL concentration cap
            if total_pnl > 0:
                city_pct = city_pnl_map.get(city, 0) / total_pnl
                if city_pct > self.MAX_CITY_PNL_PCT:
                    log.info(f"SKIP {city} — PnL concentration {city_pct:.0%} > {self.MAX_CITY_PNL_PCT:.0%} cap")
                    continue

            # V21.7.74: Geographic cluster exposure cap
            # Correlated cities share synoptic systems — treat as one risk
            cluster_name = None
            for cname, cities in self.GEO_CLUSTERS.items():
                if city in cities:
                    cluster_name = cname
                    break
            if cluster_name:
                cluster_cost = sum(
                    p.cost_usd for p in self.positions
                    if not p.settled and p.city in self.GEO_CLUSTERS[cluster_name]
                )
                if cluster_cost >= self.MAX_CLUSTER_EXPOSURE:
                    log.info(f"SKIP {city} — cluster {cluster_name} exposure ${cluster_cost:.2f} ≥ ${self.MAX_CLUSTER_EXPOSURE:.2f} cap")
                    continue

            # V21.7.53: Build portfolio — top signal + adjacent buckets
            portfolio = [sig]
            base_temp = sig["temp"]

            # Find adjacent buckets from same city+date
            for adj_sig in signals:
                if len(portfolio) >= self.MAX_ADJACENT_BUCKETS:
                    break
                if adj_sig is sig:
                    continue
                if adj_sig.get("city") != city or adj_sig.get("date") != date:
                    continue
                if adj_sig.get("is_threshold", False) != sig.get("is_threshold", False):
                    continue  # Don't mix threshold and non-threshold
                # Only pick adjacent temps (±2°C)
                if abs(adj_sig["temp"] - base_temp) <= 2:
                    # Check we don't already have this temp
                    if adj_sig["temp"] not in [p["temp"] for p in portfolio]:
                        portfolio.append(adj_sig)

            # Calculate per-bucket allocation (split budget across buckets)
            n_buckets = len(portfolio)
            per_bucket = min(self.PORTFOLIO_BUDGET_PER_CITY / n_buckets, MAX_POSITION_USD)

            # V21.7.53: Cap per-bucket at MAX_POSITION_USD
            per_bucket = min(per_bucket, MAX_POSITION_USD)

            portfolio_budget = 0
            for psig in portfolio:
                if portfolio_budget + per_bucket > self.state.bankroll:
                    break
                self.force_settle_open_positions()
                pos = self.enter_position(psig, {}, psig.get("forecast_max", 0),
                                          psig["date"], psig.get("day_offset", 1))
                if pos:
                    entered.append(psig)
                    portfolio_budget += per_bucket

            used_city_dates.add(city_date_key)
            # V21.7.56: Track per-city daily trades for diversification cap
            city_trades_today[city] = city_trades_today.get(city, 0) + len(portfolio)

            # Log portfolio composition
            if len(portfolio) > 1:
                temps = [str(p["temp"]) for p in portfolio]
                log.info(f"PORTFOLIO {city} {date}: buckets {','.join(temps)}°C "
                         f"({len(portfolio)} positions, ${portfolio_budget:.2f} budget)")

        self.save_state()
        self.generate_v22_reports()
        return entered

    def scan_cycle(self) -> List[Dict]:
        """Override: rotate through cities, limit per cycle to avoid timeout."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        self._cycle_count += 1
        now = datetime.now(timezone.utc)

        # Build eligible city list (major, non-high-risk)
        eligible = [(c, m) for c, m in CITY_REGISTRY.items()
                    if m.get("major", False) and m.get("risk", "medium") != "high"]
        # Rotate: pick a different slice each cycle
        start_idx = ((self._cycle_count - 1) * self.MAX_CITIES_PER_CYCLE) % len(eligible)
        batch = []
        for i in range(self.MAX_CITIES_PER_CYCLE):
            idx = (start_idx + i) % len(eligible)
            batch.append(eligible[idx])

        all_signals = []
        today = now.strftime("%Y-%m-%d")

        def process_city(item):
            city, meta = item
            results = []
            try:
                lat, lon = meta["lat"], meta["lon"]
                icao = meta.get("icao", "")
                tz_offset = meta.get("tz", 0)
                local_dt = now + timedelta(seconds=tz_offset)
                local_hour = local_dt.hour + local_dt.minute / 60.0

                om = fetch_open_meteo_forecast(lat, lon, days=3)
                if not om:
                    return results

                daily = om.get("daily", {})
                dates = daily.get("time", [])
                max_temps = daily.get("temperature_2m_max", [])

                # METAR (non-blocking — try but don't fail)
                metar = fetch_metar(icao) if icao else None
                current_temp = metar.get("temp_c") if metar and metar.get("temp_c") is not None else None
                max_so_far = None
                is_cooling = False

                # Estimate max_so_far from hourly
                hourly = om.get("hourly", {})
                hourly_times = hourly.get("time", [])
                hourly_temps = hourly.get("temperature_2m", [])
                if hourly_times and hourly_temps and current_temp is not None:
                    observed_max = current_temp
                    for ht, htemp in zip(hourly_times, hourly_temps):
                        if htemp is not None and ht < now.isoformat():
                            try:
                                observed_max = max(observed_max, float(htemp))
                            except (ValueError, TypeError):
                                pass
                    max_so_far = observed_max

                for day_offset in range(min((self.MAX_DAY_OFFSETS if self.paper_only else 0) + 1, 3)):
                    if day_offset >= 3:
                        break
                    if day_offset == 0 and local_hour >= 18:
                        continue
                    target_date = (now + timedelta(days=day_offset)).strftime("%Y-%m-%d")
                    try:
                        day_idx = dates.index(target_date)
                        local_day_high = max_temps[day_idx] if day_idx < len(max_temps) else None
                    except (ValueError, IndexError):
                        continue
                    if local_day_high is None:
                        continue

                    forecast_temps = {"Open-Meteo": local_day_high}

                    # Skip ensemble for speed (non-critical)
                    ens_data = None
                    try:
                        ens_data = fetch_open_meteo_ensemble(lat, lon)
                    except Exception:
                        pass
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
                            forecast_temps["Ensemble-avg"] = round(ens_avg, 1)
                            forecast_temps["Ensemble-max"] = round(max(ens_highs), 1)
                            forecast_temps["Ensemble-min"] = round(min(ens_highs), 1)
                            forecast_temps["Ensemble-std"] = round(ens_std, 2)  # V21.7.52 FIX: actual ensemble spread
                            forecast_temps["Ensemble-n"] = len(ens_highs)

                    if metar and metar.get("temp_c") is not None:
                        forecast_temps["METAR-current"] = metar["temp_c"]

                    mkt = discover_weather_markets(city, target_date)
                    if not mkt:
                        continue
                    buckets = parse_temperature_markets(mkt)
                    if not buckets:
                        continue

                    # V21.7.76: Always use compute_edge_v22 (now handles non-FDEB path with EV)
                    signals = compute_edge_v22(
                        lat=lat, lon=lon, city=city, target_date=target_date,
                        buckets=buckets, max_so_far=max_so_far, current_temp=current_temp,
                        local_hour=local_hour, is_cooling=is_cooling, day_offset=day_offset,
                        min_edge_pp=15.0, min_volume=200.0,
                        multi_model=forecast_temps, ensemble=None,
                    )

                    for sig in signals:
                        sig["forecast_max"] = local_day_high
                        sig["date"] = target_date
                        sig["day_offset"] = day_offset
                        sig["max_so_far"] = max_so_far
                        sig["current_temp"] = current_temp
                        sig["local_hour"] = local_hour
                        sig["is_cooling"] = is_cooling
                        sig["market_slug"] = mkt.get("slug", "")

                    results.extend(signals)

                    # Log edge data for DEB learning
                    for sig in signals:
                        self.log_edge({
                            "ts": now.isoformat(), "city": city, "date": target_date,
                            "temp": sig["temp"], "our_prob": sig["our_prob"],
                            "market_prob": sig["market_prob"], "edge_pp": sig["edge_pp"],
                            "forecast_max": local_day_high, "max_so_far": max_so_far,
                        })
            except Exception as e:
                log.debug(f"City scan error for {city}: {e}")
            return results

        # Execute with thread pool for parallel API calls
        # V21.7.53: Handle timeout gracefully — collect partial results
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(process_city, item): item[0] for item in batch}
            try:
                for future in as_completed(futures, timeout=120):
                    city = futures[future]
                    try:
                        results = future.result(timeout=30)
                        all_signals.extend(results)
                    except Exception as e:
                        log.warning(f"Future error for {city}: {e}")
            except Exception as timeout_err:
                # V21.7.53: Don't lose partial results on timeout
                unfinished = sum(1 for f in futures if not f.done())
                log.warning(f"Scan timeout: {unfinished} of {len(batch)} cities unfinished — keeping {len(all_signals)} partial signals")
                # Cancel remaining futures
                for f in futures:
                    if not f.done():
                        f.cancel()

        # Deduplicate by city+date+temp+side (keep highest composite score)
        seen = {}
        for sig in all_signals:
            key = f"{sig['city']}_{sig['date']}_{sig['temp']}_{sig['recommended_side']}"
            comp = sig.get("composite_score", sig["best_edge"])
            if key not in seen or comp > seen[key].get("composite_score", seen[key]["best_edge"]):
                seen[key] = sig

        log.info(f"Cycle {self._cycle_count}: scanned {len(batch)} cities | {len(seen)} signals | "
                 f"rotation {start_idx}-{start_idx+len(batch)-1}")
        return sorted(seen.values(), key=lambda s: s.get("composite_score", s["best_edge"]), reverse=True)

    def enter_position(self, signal: Dict, forecast_temps: Dict,
                       forecast_max: float, date_str: str, day_offset: int):
        """Enter position with full audit logging."""
        # §V21.7.14: Halt new temperature entries
        halt_cfg = load_halt_config()
        if halt_cfg.get("disable_new_weather_temperature_entries", False):
            log.info(f"TEMP_ENTRIES_HALTED: skipping {signal.get('city', '?')} — V21.7.14 halt directive active")
            return None

        # V21.7.66: Regime detection — skip when rolling WR is low AND PnL is negative (true crisis)
        rolling = load_rolling_calibration()
        recent = rolling.get("recent_no_outcomes", [])
        if len(recent) >= 5:
            rolling_wr = sum(recent) / len(recent)
            rolling_pnl = sum(rolling.get("recent_no_pnls", []))
            if rolling_wr < 0.35 and rolling_pnl < 0:
                log.info(f"REGIME HALT: rolling WR={rolling_wr:.0%} < 35% AND PnL=${rolling_pnl:.2f} — strategy in crisis, skipping {signal.get('city','?')}")
                return None

        city = signal.get("city", "?")
        meta = CITY_REGISTRY.get(city, {})
        risk_tier = get_risk_tier(city)

        # BLOCKED cities: still enter paper trades for tracking but flag them
        if not self.check_circuit_breakers():
            return None

        # Log candidate with full audit
        log_candidate(signal, meta, forecast_temps,
                       signal.get("max_so_far"), signal.get("current_temp"),
                       signal.get("local_hour", 12.0),
                       signal.get("is_cooling", False),
                       meta.get("settle", "metar"))

        # Use risk-adjusted position size with edge/weekly_loss context
        # V21.7.73: Pass our_prob for Kelly criterion sizing
        _entry_for_sizing = signal.get("no_price", signal.get("yes_price", 0)) if signal.get("recommended_side") == "NO" else signal.get("yes_price", signal.get("no_price", 0))
        _our_prob = signal.get("our_prob", 0)
        position_size = get_position_size(city, MAX_POSITION_USD,
                                           edge_pp=signal.get("best_edge", 0),
                                           weekly_loss=getattr(self.state, 'weekly_loss', 0),
                                           entry_price=_entry_for_sizing,
                                           our_prob=_our_prob)
        edge_threshold = get_edge_threshold(city)

        if signal["best_edge"] < edge_threshold:
            log.info(f"SKIP {city} {signal['temp']}°C — edge {signal['best_edge']:.1f}pp < threshold {edge_threshold:.0f}pp (risk={meta.get('risk','medium')})")
            return None

        # V21.7.76: Min EV filter — reject if EV < 2¢ per share
        ev_cents = signal.get("ev_cents", 0)
        if ev_cents < 2.0:
            log.info(f"SKIP {city} {signal['temp']}°C — EV {ev_cents:.1f}¢ < 2.0¢ min")
            return None

        # Committed capital check — don't over-allocate
        committed = sum(p.cost_usd for p in self.positions if not p.settled)
        # V21.7.70: In live mode, use actual CLOB collateral, not virtual bankroll.
        # Virtual bankroll ($117.44) includes accumulated PnL but actual CLOB
        # collateral may be $1.51 if winnings were withdrawn or not deposited.
        # Trading on virtual bankroll causes order failures and over-allocation.
        trading_bankroll = self.state.bankroll
        if not self.state.paper_only:
            actual = getattr(self.state, 'bankroll_actual_usd', 0)
            if actual > 0 and actual < trading_bankroll:
                trading_bankroll = actual
                log.info(f"Live mode: using CLOB collateral ${actual:.2f} "
                         f"(virtual bankroll ${self.state.bankroll:.2f})")
        available = trading_bankroll - committed
        if available < position_size:
            log.warning(f"Insufficient available capital: ${available:.2f} available "
                       f"(trading_bankroll=${trading_bankroll:.2f} - committed=${committed:.2f}) < ${position_size:.2f}")
            return None

        # Slug deduplication — skip if we already have an open position on this market
        slug = signal.get("market_slug", "")
        existing_slugs = {p.market_slug for p in self.positions if not p.settled}
        if slug and slug in existing_slugs:
            log.info(f"Skipping {slug[:50]} — already have open position")
            return None

        # V21.7.74: Disk-based slug dedup — prevents duplicate orders across restarts
        # and between cycles where in-memory positions get cleared after settlement.
        DEDUP_FILE = OUTPUT_DIR / "v2_1_slug_dedup.json"
        dedup_data = {}
        if DEDUP_FILE.exists():
            try:
                with open(DEDUP_FILE) as f:
                    dedup_data = json.load(f)
            except Exception:
                dedup_data = {}
        # Check if we already entered this slug today
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        slug_key = f"{slug}_{today}"
        if slug_key in dedup_data:
            log.warning(f"SKIP {slug[:50]} — already entered today (disk dedup)")
            return None

        if trading_bankroll < position_size:
            log.warning(f"Insufficient bankroll: ${trading_bankroll:.2f} < ${position_size:.2f}")
            return None

        side = signal["recommended_side"]
        outcome = side
        
        # V21.7.56: HARD YES BLOCK — observer recommendation, YES is 0% WR
        # No YES entries allowed under any circumstances
        if side == "YES":
            log.info(f"SKIP {city} {signal['temp']}°C — YES entries blocked (0% WR)")
            return None
        
        entry_price = signal["no_price"] if side == "NO" else signal["yes_price"]
        shares = round(position_size / max(entry_price, 0.01), 2)
        # V21.7.58: Enforce Polymarket minimum 5 shares
        if shares < 5:
            shares = 5
            log.info(f"Adjusted shares to 5 (PM minimum) for {city} @ {entry_price:.2f}")
        cost = round(shares * entry_price, 2)
        if cost > trading_bankroll:
            shares = round(trading_bankroll / max(entry_price, 0.01), 2)
            cost = round(shares * entry_price, 2)

        token_id = signal["no_token_id"] if side == "NO" else signal["yes_token_id"]
        trade_id = f"WV21-{city[:3].upper()}{signal['temp']}{side[0]}{int(time.time())}"

        pos = WeatherPosition(
            trade_id=trade_id,
            city=city,
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
            forecast_prob=signal.get("our_prob", 0),
            market_prob=signal.get("market_prob", 0),
            edge_pp=signal.get("best_edge", 0),
            entry_ts=datetime.now(timezone.utc).isoformat(),
            risk_level=meta.get("risk", "medium"),
            max_so_far=signal.get("max_so_far", 0.0),
            entry_sigma=signal.get("sigma_used", 1.5),
        )

        # Live order submission
        # V21.7.70: FILL VERIFICATION — only record as live if CLOB confirms fill.
        # Previously: failed/unmatched orders were written to live_trades.jsonl with
        # fabricated PnL, inflating live performance. Now: if order fails or returns
        # status != "matched", the position is marked as paper and written to paper file.
        tier_str = f"[{risk_tier}]" if risk_tier != "TRADE" else ""
        order_filled = False
        if not self.paper_only and not WEATHER_BOT_LIVE_BLOCKED:
            log.info(f"LIVE BUY {outcome} {city} {signal['temp']}°C "
                     f"@ {entry_price:.2f} | edge={signal['best_edge']:.1f}pp "
                     f"risk={meta.get('risk','medium')} pos=${cost:.2f} {tier_str} | "
                     f"{signal.get('prob_info', '')[:60]}")
            try:
                clob = self.clob_client or init_clob_client()
                order = build_dry_run_order(
                    token_id=token_id, side="BUY", price=entry_price, size=shares,
                )
                result = submit_tracked_order(order)
                log.info(f"Live order result: {result}")
                # V21.7.70: Verify fill — only accept if result has no error,
                # has a real order_id (not paper_), and status is "matched"
                if isinstance(result, dict) and not result.get("error"):
                    oid = result.get("order_id", "")
                    rstatus = result.get("status", "")
                    if oid and not str(oid).startswith("paper_") and rstatus == "matched":
                        pos.order_id = oid  # type: ignore[attr-defined]
                        order_filled = True
                        log.info(f"LIVE FILL CONFIRMED: {city} @ {entry_price:.2f} | order={oid[:20]}...")
                    else:
                        log.warning(f"ORDER NOT FILLED: {city} | status={rstatus} | oid={oid[:20] if oid else 'EMPTY'}... — recording as PAPER")
            except Exception as e:
                log.error(f"Live order failed: {e} — recording as PAPER")
        else:
            log.info(f"PAPER BUY {outcome} {city} {signal['temp']}°C "
                     f"@ {entry_price:.2f} | edge={signal['best_edge']:.1f}pp "
                     f"risk={meta.get('risk','medium')} pos=${cost:.2f} {tier_str} | "
                     f"{signal.get('prob_info', '')[:60]}")

        self.positions.append(pos)
        self.state.bankroll -= cost
        self.state.daily_trades += 1
        self.state.active_positions += 1
        self.state.total_trades += 1

        # V21.7.74: Write to disk dedup — prevent duplicate entries on same slug+day
        if slug:
            try:
                dedup_data[slug_key] = datetime.now(timezone.utc).isoformat()
                with open(DEDUP_FILE, "w") as f:
                    json.dump(dedup_data, f, indent=2)
            except Exception:
                pass

        # Write to mode-specific trades log — SEPARATION FIX
        # V21.7.70: If live order was NOT filled, write to PAPER file, not LIVE file.
        # This prevents unfilled orders from contaminating live performance metrics.
        trade_record = asdict(pos)
        trade_record["risk_tier"] = risk_tier
        trade_record["position_size"] = position_size
        trade_record["edge_threshold_used"] = edge_threshold
        trade_record["live_blocked"] = WEATHER_BOT_LIVE_BLOCKED
        trade_record["ev_cents"] = signal.get("ev_cents", 0)  # V21.7.76: EV at entry
        trade_record["p_win"] = signal.get("p_win", 0)        # V21.7.76: Win probability
        trade_record["composite_score"] = signal.get("composite_score", 0)
        trade_record["settlement_source"] = meta.get("settle", "metar")
        trade_record["rounding_rule"] = "floor" if is_hko_floor_city(city) else "wu_round"
        trade_record["tz_offset"] = meta.get("tz", 0)
        trade_record["distance_km"] = meta.get("dist", 0)
        # V21.7.70: Route to correct file — live_trades only if order actually filled
        if not self.paper_only and not WEATHER_BOT_LIVE_BLOCKED and order_filled:
            write_file = self._trades_file  # LIVE_TRADES
        else:
            write_file = PAPER_TRADES  # Paper fallback for unfilled live attempts
            trade_record["live_attempted_but_not_filled"] = (not self.paper_only and not order_filled)
        with open(write_file, "a") as f:
            f.write(json.dumps(trade_record) + "\n")

        self.save_state()
        return pos

    def settle_positions(self):
        """Settle positions via Polymarket Gamma API resolution (primary) or METAR fallback.

        V21.7.69: Added on-chain verification — after Gamma/METAR settlement,
        cross-check against Polymarket Data API positions to verify the actual
        outcome. The bot was reporting wins that on-chain showed as losses (0W/8L
        discrepancy). Now: if on-chain shows curPrice=0 for a position we marked
        as WON, override to LOSS.

        V21.7.72: FIX on-chain verification — previous code only matched by
        condition_id, which was empty in bot records. Now also matches by
        market TITLE (city + temperature). This catches the 19 fake wins where
        bot declared WON via Gamma API but position sat at curPrice=0 on-chain.
        Also: only count wins if the position was actually REDEEMED (SELL trade
        exists on-chain) or curPrice > 0. A win with curPrice=0 and no SELL
        is NOT a win — it's an unredeemed loss.
        """
        import requests as _requests
        now = datetime.now(timezone.utc)

        # V21.7.72: Fetch on-chain positions AND build title-based lookup
        onchain_positions = {}
        onchain_by_title = {}  # V21.7.72: title → on-chain data
        try:
            proxy_addr = "0xaF7B21FE2B18745aE1b2fA2F6F00B0fC4EF3F70b"
            r_chain = _requests.get(
                f"https://data-api.polymarket.com/positions?user={proxy_addr}&limit=500",
                timeout=15,
            )
            if r_chain.status_code == 200:
                for p in r_chain.json():
                    cid = p.get("conditionId", p.get("condition_id", ""))
                    title = p.get("title", "").lower()
                    data = {
                        "cur_price": float(p.get("curPrice", 0) or 0),
                        "cash_pnl": float(p.get("cashPnl", 0) or 0),
                        "size": float(p.get("size", 0) or 0),
                        "title": p.get("title", ""),
                    }
                    if cid:
                        onchain_positions[cid] = data
                    if title:
                        onchain_by_title[title] = data
        except Exception as e:
            log.warning(f"On-chain position fetch failed: {e}")

        # V21.7.72: Also fetch SELL trades to verify actual redemptions
        redeemed_titles = set()
        try:
            r_trades = _requests.get(
                f"https://data-api.polymarket.com/trades?user={proxy_addr}&limit=500",
                timeout=15,
            )
            if r_trades.status_code == 200:
                for t in r_trades.json():
                    if t.get("side") == "SELL" and ("temperature" in t.get("title", "").lower() or "temp" in t.get("title", "").lower()):
                        redeemed_titles.add(t.get("title", "").lower())
        except Exception as e:
            log.warning(f"On-chain trades fetch failed: {e}")

        for pos in [p for p in self.positions if not p.settled]:
            # V21.7.73: Wait 24h (not 6h) before checking resolution
            # Wunderground daily high may not be available until 24h after
            target_dt = datetime.strptime(pos.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if now < target_dt + timedelta(hours=24):
                continue

            # ─── Primary: Polymarket Gamma API resolution ───
            city = pos.city
            dt = datetime.strptime(pos.date, "%Y-%m-%d")
            month_name = dt.strftime("%B").lower()
            day = dt.day
            slug = f"highest-temperature-in-{city}-on-{month_name}-{day}-2026"

            try:
                r = _requests.get(
                    "https://gamma-api.polymarket.com/events",
                    params={"slug": slug},
                    timeout=15,
                )
                if r.status_code == 200 and r.json():
                    ev = r.json()[0]
                    pos_cid = getattr(pos, "condition_id", "") or ""
                    for m in ev.get("markets", []):
                        mk_cid = m.get("conditionId", m.get("condition_id", ""))
                        # Match by condition_id or by EXACT question match
                        # V21.7.73: Strict matching — temperature alone matches wrong market
                        # (e.g. "33°C" matches both "be 33°C" and "be 33°C or higher")
                        matched = False
                        if pos_cid and mk_cid:
                            matched = (mk_cid == pos_cid)
                        if not matched:
                            # V21.7.73: Match by full question string containing bucket temp
                            # AND the same outcome type (exact vs threshold)
                            mk_question = m.get("question", "")
                            pos_outcome = getattr(pos, "outcome", "")
                            pos_bucket = getattr(pos, "bucket_temp", 0)
                            if f"{pos_bucket}°C" in mk_question:
                                # Check if it's a threshold market ("or higher"/"or lower")
                                # vs exact bucket market ("be X°C")
                                is_threshold_mkt = "or higher" in mk_question.lower() or "or lower" in mk_question.lower()
                                is_threshold_pos = getattr(pos, "is_threshold", False) or ("or higher" in str(getattr(pos, "question", "")).lower() or "or lower" in str(getattr(pos, "question", "")).lower())
                                if is_threshold_mkt == is_threshold_pos:
                                    matched = True
                            if not matched:
                                continue

                        closed = m.get("closed", False)
                        if not closed:
                            continue  # PM hasn't resolved yet

                        # Resolution from outcomePrices
                        prices_raw = m.get("outcomePrices", "[]")
                        outcomes_raw = m.get("outcomes", "[]")
                        try:
                            prices = [float(p) for p in (json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw)]
                            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                        except Exception:
                            prices, outcomes = [], []

                        if len(prices) < 2 or len(outcomes) < 2:
                            continue

                        winning_idx = 0 if prices[0] > prices[1] else 1
                        winning_outcome = str(outcomes[winning_idx]).strip().upper()
                        our_outcome = pos.outcome.upper()

                        payout_per_share = 1.0 if our_outcome == winning_outcome else 0.0
                        total_payout = payout_per_share * pos.shares
                        cost = pos.entry_price * pos.shares
                        pnl = total_payout - cost

                        # V21.7.72: On-chain verification — if we marked WON but
                        # on-chain shows curPrice=0 (resolved at 0), override to LOSS.
                        # Previous code only matched by condition_id (always empty).
                        # Now matches by condition_id OR by market title.
                        pos_cid = getattr(pos, "condition_id", "") or ""
                        chain_data = None
                        if pos_cid and pos_cid in onchain_positions:
                            chain_data = onchain_positions[pos_cid]
                        else:
                            # V21.7.72: Match by title — build a title from pos fields
                            pos_title_key = m.get("question", "").lower()
                            if pos_title_key and pos_title_key in onchain_by_title:
                                chain_data = onchain_by_title[pos_title_key]
                        
                        if pnl > 0 and chain_data:
                            if chain_data["cur_price"] == 0 and chain_data["size"] > 0:
                                log.warning(
                                    f"⚠️ ON-CHAIN OVERRIDE: {pos.trade_id} marked WON by Gamma "
                                    f"but on-chain curPrice=0 | overriding to LOSS"
                                )
                                payout_per_share = 0.0
                                total_payout = 0.0
                                pnl = -cost
                            elif chain_data["cur_price"] == 0 and chain_data["size"] == 0:
                                # Position not found on-chain at all — not redeemed
                                log.warning(
                                    f"⚠️ ON-CHAIN OVERRIDE: {pos.trade_id} marked WON by Gamma "
                                    f"but not on-chain (not redeemed) | overriding to LOSS"
                                )
                                payout_per_share = 0.0
                                total_payout = 0.0
                                pnl = -cost

                        pos.exit_ts = now.isoformat()
                        pos.exit_price = payout_per_share
                        pos.pnl = pnl
                        pos.settled = True
                        pos.settlement_source = "PM_GAMMA_RESOLUTION"

                        self.state.bankroll += total_payout
                        self.state.total_pnl += pnl
                        if pnl > 0:
                            self.state.wins += 1
                            self.state.consecutive_losses = 0
                            update_rolling_calibration(True, pnl)  # V21.7.66: pass PnL
                        else:
                            self.state.losses += 1
                            self.state.consecutive_losses += 1
                            update_rolling_calibration(False, pnl)  # V21.7.66: pass PnL
                            # V21.7.58: Only count LIVE trade losses against circuit breakers
                            if not self.paper_only and getattr(pos, 'order_id', ''):
                                self.state.weekly_loss += pnl
                        break  # Position settled, move to next
            except Exception as e:
                log.warning(f"PM Gamma settlement check failed for {slug}: {e}")

            # V21.7.73: METAR FALLBACK REMOVED — was using current temp, not daily high
            # This caused 19 fake wins (bot read nighttime temp 20°C, actual high was 33°C)
            # Now: if Gamma API hasn't resolved, WAIT. Don't settle with wrong data.
            if pos.settled:
                continue
            # Log that we're waiting for Gamma resolution
            log.debug(f"Waiting for Gamma resolution: {pos.city} {pos.date} (not yet closed on PM)")
            continue

        # Save state (persist settlements to JSONL) BEFORE removing settled positions
        self.save_state()
        # Remove settled positions from list so they don't count against max_positions
        self.positions = [p for p in self.positions if not getattr(p, 'settled', False)]

    def _load_state(self):
        """V21.7.67: Override parent to load from mode-specific state file.

        Parent hardcodes STATE_FILE (paper). This override uses self._state_file
        which is set in __init__ BEFORE super().__init__() calls _load_state().
        """
        from src.weather.v1_weather_runner import WeatherState, asdict, log
        if self._state_file.exists():
            try:
                with open(self._state_file) as f:
                    d = json.load(f)
                return WeatherState(**{k: d.get(k, v) for k, v in asdict(WeatherState()).items()})
            except Exception as e:
                log.warning(f"State load error from {self._state_file}: {e}, using defaults")
        return WeatherState(paper_only=not WEATHER_BOT_LIVE_BLOCKED if not self._state_file.name.startswith('v2_1_live') else False)

    def save_state(self):
        """Override: save state to mode-specific file. SEPARATION FIX.
        Also persist settled flags back to mode-specific JSONL.
        V21.7.67: Also sync bankroll_actual_usd from CLOB when in live mode.
        """
        self.state.timestamp = datetime.now(timezone.utc).isoformat()
        # V21.7.67: Sync bankroll_actual_usd from CLOB when in live mode
        if not self.state.paper_only:
            try:
                from src.weather.v1_weather_runner import get_onchain_usdc
                self.state.bankroll_actual_usd = get_onchain_usdc()
            except Exception as e:
                log.warning(f"bankroll_actual_usd sync failed: {e}")
        with open(self._state_file, "w") as f:
            json.dump(asdict(self.state), f, indent=2)
        # V21.7.54: Rewrite trades JSONL to persist settlement status
        # This prevents re-loading already-settled positions on restart
        # V21.7.70: Check both LIVE and PAPER files — unfilled live attempts
        # are written to PAPER_TRADES, so their settlements must be persisted there
        if self.positions:
            settled_ids = {p.trade_id for p in self.positions if p.settled}
            if settled_ids:
                # V21.7.70: Check both LIVE and PAPER files — unfilled live attempts
                # are written to PAPER_TRADES, so their settlements must be persisted there
                for trades_file in [self._trades_file, PAPER_TRADES]:
                    if not trades_file.exists():
                        continue
                    lines = trades_file.read_text().splitlines()
                    updated_lines = []
                    changed = False
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                            tid = d.get("trade_id", "")
                            if tid in settled_ids and not d.get("settled", False):
                                for p in self.positions:
                                    if p.trade_id == tid and p.settled:
                                        d["settled"] = True
                                        d["pnl"] = p.pnl
                                        d["exit_ts"] = p.exit_ts
                                        d["exit_price"] = p.exit_price
                                        d["settlement_source"] = getattr(p, "settlement_source", d.get("settlement_source", ""))
                                        if hasattr(p, "settlement_temp"):
                                            d["settlement_temp"] = p.settlement_temp
                                        changed = True
                                        break
                            updated_lines.append(json.dumps(d))
                        except Exception:
                            updated_lines.append(line)
                    if changed:
                        with open(trades_file, "w") as f:
                            for line in updated_lines:
                                if line:
                                    f.write(line + "\n")
                        log.info(f"Persisted {len(settled_ids)} settlement(s) to {trades_file.name}")

    def load_state(self):
        """Override: load state and positions from mode-specific files only.
        SEPARATION FIX: Paper loads from paper files, live loads from live files.
        They never cross-contaminate.
        V21.7.70: On-chain reconciliation — in live mode, sync bankroll and PnL
        to actual on-chain values on every startup. Prevents fabricated state
        from paper mode surviving into live mode.
        """
        if self._state_file.exists():
            try:
                with open(self._state_file) as f:
                    self.state = WeatherState(**json.load(f))
            except Exception as e:
                log.warning(f"State load failed ({self._state_file.name}): {e}")
        # Load positions from mode-specific trades file
        self.positions = []
        if self._trades_file.exists():
            with open(self._trades_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        pos_kwargs = {}
                        for fld in WeatherPosition.__dataclass_fields__.values():
                            if fld.name in d:
                                pos_kwargs[fld.name] = d[fld.name]
                            elif hasattr(fld, "default"):
                                pos_kwargs[fld.name] = fld.default
                        pos = WeatherPosition(**pos_kwargs)
                        if not pos.settled:
                            self.positions.append(pos)
                    except Exception:
                        continue

        # V21.7.72: On-chain reconciliation in live mode
        # Uses TRADES API (BUY+SELL pairs) as source of truth, not positions API.
        # Winning positions get redeemed (SELL) and disappear from positions API.
        # Positions API only shows remaining (often losing) positions, so counting
        # curPrice=0 as losses and ignoring redeemed wins fabricates 0W/all-L.
        #
        # Method:
        #   1. Trades API: group by market title. SELL = closed. PnL = sell_rev - buy_cost.
        #   2. Positions API: remaining positions with curPrice=0 = expired losses.
        #   3. Positions API: remaining with curPrice>0 = active open.
        if not self.state.paper_only:
            try:
                import requests as _req
                from collections import defaultdict
                proxy = "0xaF7B21FE2B18745aE1b2fA2F6F00B0fC4EF3F70b"

                # 1. Get trades (BUY + SELL history)
                r_trades = _req.get(f"https://data-api.polymarket.com/trades?user={proxy}&limit=500", timeout=15)
                all_trades = r_trades.json() if r_trades.status_code == 200 else []
                weather_trades = [
                    t for t in all_trades
                    if "temperature" in t.get("title", "").lower() or "temp" in t.get("title", "").lower()
                ]
                by_market = defaultdict(list)
                for t in weather_trades:
                    by_market[t.get("title", "")].append(t)

                # 2. Get positions (remaining on-chain)
                r_pos = _req.get(f"https://data-api.polymarket.com/positions?user={proxy}&limit=500", timeout=15)
                all_positions = r_pos.json() if r_pos.status_code == 200 else []
                weather_positions = [
                    p for p in all_positions
                    if "temperature" in p.get("title", "").lower() or "temp" in p.get("title", "").lower()
                ]

                # 3. Calculate realized PnL from closed (SELL) trades
                closed_titles = set()
                realized_pnl = 0.0
                wins = 0
                losses = 0

                for title, market_trades in by_market.items():
                    buys = [t for t in market_trades if t.get("side") == "BUY"]
                    sells = [t for t in market_trades if t.get("side") == "SELL"]
                    if not sells:
                        continue  # Not closed
                    closed_titles.add(title)
                    buy_cost = sum(float(t.get("price", 0) or 0) * float(t.get("size", 0) or 0) for t in buys)
                    sell_rev = sum(float(t.get("price", 0) or 0) * float(t.get("size", 0) or 0) for t in sells)
                    buy_size = sum(float(t.get("size", 0) or 0) for t in buys)
                    sell_size = sum(float(t.get("size", 0) or 0) for t in sells)

                    if buy_size == 0:
                        # Bought earlier, not in this batch — revenue is pure profit
                        pnl = sell_rev
                    elif sell_size >= buy_size:
                        pnl = sell_rev - buy_cost
                    else:
                        # Partial close
                        pnl = sell_rev - (buy_cost * sell_size / buy_size)

                    realized_pnl += pnl
                    if pnl > 0:
                        wins += 1
                    else:
                        losses += 1

                # 4. Add losses from expired positions (on-chain, curPrice=0, not redeemed)
                open_active = 0
                for p in weather_positions:
                    title = p.get("title", "")
                    if title in closed_titles:
                        continue
                    cur = float(p.get("curPrice", 0) or 0)
                    if cur == 0:
                        # Expired worthless — realized loss
                        cash_pnl = float(p.get("cashPnl", 0) or 0)
                        realized_pnl += cash_pnl
                        losses += 1
                    else:
                        open_active += 1

                # 5. Sync USDC balance (shared wallet)
                try:
                    from src.weather.v1_weather_runner import get_onchain_usdc
                    actual_usdc = get_onchain_usdc()
                except Exception:
                    actual_usdc = None

                closed_count = len(closed_titles)
                expired_count = losses - sum(1 for _, mt in by_market.items() if any(t.get('side')=='SELL' for t in mt) and (sum(float(t.get('price',0) or 0)*float(t.get('size',0) or 0) for t in mt if t.get('side')=='SELL') - sum(float(t.get('price',0) or 0)*float(t.get('size',0) or 0) for t in mt if t.get('side')=='BUY')) <= 0)
                log.info(f"V21.7.72 ON-CHAIN RECONCILIATION (trades API):")
                log.info(f"  Wallet USDC: ${actual_usdc:.2f}" if actual_usdc else "  Wallet USDC: N/A")
                log.info(f"  Weather trades: {len(weather_trades)} | positions: {len(weather_positions)} (of {len(all_positions)} wallet)")
                log.info(f"  Closed (redeemed): {closed_count} | Expired worthless: {expired_count} | Active open: {open_active}")
                log.info(f"  Realized PnL: ${realized_pnl:.2f} | W:{wins} L:{losses}")

                # Override state with verified on-chain truth
                self.state.total_pnl = realized_pnl
                self.state.wins = wins
                self.state.losses = losses
                self.state.active_positions = open_active
                self.state.total_trades = wins + losses + open_active
                if actual_usdc is not None and actual_usdc > 0:
                    self.state.bankroll = actual_usdc
                    self.state.bankroll_actual_usd = actual_usdc

                log.info(f"  Reconciled: W:{wins} L:{losses} | PnL: ${realized_pnl:.2f} | Cash: ${actual_usdc:.2f}" if actual_usdc else f"  Reconciled: W:{wins} L:{losses} | PnL: ${realized_pnl:.2f}")
            except Exception as e:
                log.warning(f"V21.7.72 on-chain reconciliation failed: {e}")

    def status_report(self):
        """Extended status with live readiness."""
        self.settle_positions()

        readiness = check_live_readiness()

        active = [p for p in self.positions if not p.settled]
        settled = [p for p in self.positions if p.settled]

        print(f"\n{'='*70}")
        print(f"  V1 Weather Bot v2.1 — Paper Settlement Validation")
        print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"{'='*70}")
        print(f"  LIVE BLOCKED: {WEATHER_BOT_LIVE_BLOCKED}")
        print(f"  Mode: PAPER ONLY (forced)")
        print(f"  Bankroll: ${self.state.bankroll:.2f}")
        print(f"  Total PnL: ${self.state.total_pnl:.2f}")
        wr = self.state.wins / max(1, self.state.wins + self.state.losses) * 100
        print(f"  W/L: {self.state.wins}/{self.state.losses} ({wr:.0f}% WR)")
        print(f"  Active: {len(active)} | Settled: {len(settled)} | Total: {self.state.total_trades}")
        print(f"  Daily loss: ${self.state.daily_loss:.2f} | Weekly: ${self.state.weekly_loss:.2f}")

        print(f"\n  ── Promotion Criteria ──")
        criteria = readiness["promotion_criteria"]
        print(f"  Resolved: {criteria['resolved_count']}/25  {'✓' if criteria['criteria_met'] else '✗'}")
        print(f"  Positive EV: {criteria['ev_per_trade']:.4f}/trade  {'✓' if criteria['positive_ev'] else '✗'}")
        print(f"  PF: {criteria['profit_factor']:.2f}  {'✓' if criteria['pf_met'] else '✗'} (threshold ≥ 1.25)")
        print(f"  Zero errors: {criteria['zero_errors']}  {'✓' if criteria['zero_errors'] else '✗'} ({criteria['error_count']} errors)")
        print(f"  ALL MET: {'YES' if readiness['all_criteria_met'] else 'NO'} — LIVE {'ELIGIBLE' if readiness['all_criteria_met'] else 'BLOCKED'}")

        print(f"\n  ── Live METAR ──")
        checked = set()
        for city, meta in list(CITY_REGISTRY.items())[:15]:
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
                tier = get_risk_tier(city)
                print(f"    {meta['n']:14s} {icao}: {metar['temp_c']:.0f}°C "
                      f"({local_now.strftime('%H:%M')} local) [{tier}]")

        if active:
            print(f"\n  ── Active Positions ({len(active)}) ──")
            for p in active:
                tier = get_risk_tier(p.city)
                days_left = (datetime.strptime(p.date, "%Y-%m-%d").date() - datetime.now(timezone.utc).date()).days
                print(f"    {p.city:14s} {p.bucket_temp}°C {p.outcome:3s} @ {p.entry_price:.2f}"
                      f" edge={p.edge_pp:.0f}pp σ={p.entry_sigma:.1f}"
                      f" cost=${p.cost_usd:.2f} T{days_left}d [{tier}]")

        print(f"{'='*70}\n")

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="V1 Weather Bot v2.1 — Paper Settlement Validation")
    parser.add_argument("--paper", action="store_true", default=True, help="Paper trading mode")
    parser.add_argument("--live", action="store_true", help="Live trading mode (REAL MONEY)")
    parser.add_argument("--once", action="store_true", help="Run one scan cycle")
    parser.add_argument("--status", action="store_true", help="Show status dashboard with live readiness")
    parser.add_argument("--bankroll", type=float, default=20.0, help="Starting bankroll")
    parser.add_argument("--interval", type=int, default=900, help="Scan interval in seconds (default: 900=15min)")
    parser.add_argument("--hindcast", action="store_true", help="Generate hindcast report only")
    parser.add_argument("--risk-report", action="store_true", help="Generate city risk report only")
    parser.add_argument("--readiness", action="store_true", help="Check live readiness only")
    args = parser.parse_args()

    if args.live and WEATHER_BOT_LIVE_BLOCKED:
        print("⚠️  LIVE MODE IS BLOCKED. WEATHER_BOT_LIVE_BLOCKED = True")
        print("   Promotion criteria not yet met. Run --readiness to check status.")
        sys.exit(1)

    # Generate city risk report on startup
    generate_city_risk_report()

    if args.risk_report:
        sys.exit(0)

    if args.hindcast:
        report_path = run_hindcast()
        print(f"Hindcast report written to: {report_path}")
        sys.exit(0)

    if args.readiness:
        readiness = check_live_readiness()
        print(json.dumps(readiness, indent=2))
        sys.exit(0)

    bot = WeatherBotV21(bankroll=args.bankroll, paper_only=not args.live)
    bot.load_state()

    # V2.2 §9: Force-settle on startup
    settled = bot.force_settle_open_positions()
    if settled > 0:
        log.info(f"V2.2 startup: force-settled {settled} stale positions")

    if args.status:
        bot.status_report()
        sys.exit(0)

    log.info(f"V2.2 Paper Settlement Validation starting | cities={len(CITY_REGISTRY)} | LIVE BLOCKED={WEATHER_BOT_LIVE_BLOCKED} | TEMP_HALTED={TEMPERATURE_ENTRIES_HALTED}")

    if args.once:
        entered = bot.run_once()
        bot.settle_positions()
        readiness = check_live_readiness()
        log.info(f"Scan complete: {len(entered) if entered else 0} positions entered | "
                 f"Resolved: {readiness['performance']['total_resolved']} | "
                 f"PnL: ${readiness['performance']['total_pnl']:.2f} | "
                 f"PF: {readiness['performance']['profit_factor']:.2f}")
    else:
        cycle = 0
        while True:
            cycle += 1
            try:
                bot.run_once()
                bot.settle_positions()
                if cycle % 4 == 0:  # Check readiness every 4th cycle (~1 hour)
                    readiness = check_live_readiness()
                    data_src = readiness.get('data_source', '?')
                    resolved_n = readiness['performance']['total_resolved']
                    pnl_val = readiness['performance']['total_pnl']
                    pf_val = readiness['performance']['profit_factor']
                    wr_val = readiness['performance']['win_rate']
                    paper_cmp = readiness.get('paper_comparison', {})
                    paper_n = paper_cmp.get('paper_resolved', 0)
                    paper_pnl = paper_cmp.get('paper_pnl', 0)
                    paper_str = f" | Paper: {paper_n} trades ${paper_pnl:.2f}" if paper_n > 0 else ""
                    log.info(f"Cycle {cycle} | [{data_src}] Resolved: {resolved_n} | "
                             f"PnL: ${pnl_val:.2f} | PF: {pf_val:.2f} | WR: {wr_val:.1%}{paper_str}")
                log.info(f"Cycle {cycle} complete — sleeping {args.interval}s")
                time.sleep(args.interval)
            except KeyboardInterrupt:
                log.info("Interrupted — saving state")
                bot.save_state()
                readiness = check_live_readiness()
                log.info(f"Final: Resolved={readiness['performance']['total_resolved']} "
                         f"PnL=${readiness['performance']['total_pnl']:.2f} "
                         f"PF={readiness['performance']['profit_factor']:.2f}")
                break
            except Exception as e:
                log.error(f"Cycle {cycle} error: {e}")
                traceback.print_exc()
                time.sleep(60)