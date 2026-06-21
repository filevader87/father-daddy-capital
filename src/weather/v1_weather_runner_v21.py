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

WEATHER_BOT_LIVE_BLOCKED = True

Output files (all under OUTPUT_DIR):
  v2_1_candidate_log.jsonl   — every candidate signal with full audit
  v2_1_paper_trades.jsonl     — entered positions with execution details
  v2_1_resolution_audit.jsonl — settlement chain per resolved position
  v2_1_city_risk_report.json  — per-city risk tiering summary
  v2_1_hindcast_report.csv    — backtest on historical forecast accuracy
  v2_1_live_readiness.json    — promotion criteria tracker (BLOCKED until met)
"""

WEATHER_BOT_LIVE_BLOCKED = True

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
from typing import Optional, Dict, List, Tuple
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

TEMPERATURE_ENTRIES_HALTED = load_halt_config().get("disable_new_weather_temperature_entries", True)

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
# §MCP: Add v217_live to path for MCP bridge
sys.path.insert(0, str(PROJECT_ROOT / "src" / "v217_live"))
try:
    from v1_weather_runner_v2 import (
        CITY_REGISTRY, CITY_ALIASES, RISK_PROFILES,
        WeatherBotV2, WeatherPosition, WeatherState,
        fetch_open_meteo_forecast, fetch_open_meteo_ensemble, fetch_metar,
        discover_weather_markets, parse_temperature_markets,
        compute_reality_anchored_probability, compute_edge_v2,
        determine_peak_hours, apply_city_settlement, wu_round, is_hko_floor_city,
        compute_deb_weights, load_deb_history, save_deb_history,
    )
    HAS_V2 = True
except ImportError as e:
    print(f"FATAL: Cannot import v1_weather_runner_v2: {e}")
    HAS_V2 = False
    sys.exit(1)

# §V22: DEB multi-model integration
sys.path.insert(0, str(PROJECT_ROOT / "src" / "polyweather_analysis"))
try:
    from fdeb_integration import deb_enhanced_probability, fetch_multi_model_forecasts, fetch_ensemble_forecast, build_deb_forecasts, record_actual_high
    HAS_FDEB = True
except ImportError as e:
    print(f"WARNING: fdeb_integration not available: {e}")
    HAS_FDEB = False

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

# ─── Output files ───
CANDIDATE_LOG = OUTPUT_DIR / "v2_1_candidate_log.jsonl"
PAPER_TRADES  = OUTPUT_DIR / "v2_1_paper_trades.jsonl"
RESOLUTION_AUDIT = OUTPUT_DIR / "v2_1_resolution_audit.jsonl"
CITY_RISK_REPORT = OUTPUT_DIR / "v2_1_city_risk_report.json"
HINDCAST_REPORT  = OUTPUT_DIR / "v2_1_hindcast_report.csv"
LIVE_READINESS   = OUTPUT_DIR / "v2_1_live_readiness.json"
STATE_FILE       = OUTPUT_DIR / "v2_1_state.json"
CONSOLE_LOG      = OUTPUT_DIR / "v2_1_console.log"

# ─── Live probe limits ───
MAX_POSITION_USD = 2.00    # Paper: $2/position
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

def get_position_size(city: str, base: float = MAX_POSITION_USD) -> float:
    """Risk-adjusted position size."""
    meta = CITY_REGISTRY.get(city, {})
    risk = meta.get("risk", "medium")
    return base * RISK_PROFILES.get(risk, RISK_PROFILES["medium"])["position_mult"]

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
                    except:
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
    if not HAS_FDEB or not buckets:
        return []

    meta = CITY_REGISTRY.get(city, {})
    risk = meta.get("risk", "medium")
    dist_km = meta.get("dist", 0)

    # Fetch multi-model + ensemble ONCE for this city (if not provided)
    if multi_model is None:
        multi_model = fetch_multi_model_forecasts(lat, lon, forecast_days=3)
    if ensemble is None:
        ensemble = fetch_ensemble_forecast(lat, lon, forecast_days=3)

    # Build forecast dict for target date
    forecasts = build_deb_forecasts(multi_model, ensemble, target_date)
    if not forecasts:
        return []

    deb_input = {k: v for k, v in forecasts.items() if not k.startswith("Ensemble-")}
    if not deb_input:
        return []

    # Run DEB prediction ONCE for this city
    from deb_algorithm import calculate_deb_prediction, bootstrap_recent_daily_history_if_missing
    try:
        bootstrap_recent_daily_history_if_missing(city, lookback_days=14)
    except Exception:
        pass

    try:
        deb_result = calculate_deb_prediction(
            city_name=city, current_forecasts=deb_input,
            lookback_days=7, decay_factor=0.85,
            bias_lookback_days=30, bias_min_samples=3,
        )
    except Exception:
        valid = [v for v in deb_input.values() if v is not None]
        center = sorted(valid)[len(valid) // 2] if valid else 20.0
        deb_result = {"prediction": center, "raw_prediction": center, "version": "fallback", "weights_info": "", "bias_adjustment": 0, "bias_samples": 0}

    center = deb_result.get("prediction") or deb_result.get("raw_prediction")
    if center is None:
        valid = [v for v in deb_input.values() if v is not None]
        center = sorted(valid)[len(valid) // 2] if valid else 20.0

    # Sigma from ensemble
    ensemble_std = forecasts.get("Ensemble-std")
    ensemble_n = int(forecasts.get("Ensemble-n", 0))
    if ensemble_std is not None and ensemble_n >= 10:
        sigma = max(1.0, float(ensemble_std))
    elif len(deb_input) > 2:
        vals = [v for v in deb_input.values() if v is not None]
        sigma = max(1.0, (max(vals) - min(vals)) / 2.0) if vals else 2.0
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
    forecast_median = sorted([v for v in deb_input.values() if v is not None])[len(deb_input) // 2] if deb_input else center
    if max_so_far is not None:
        if peak_status in ("past", "in_window") and max_so_far < forecast_median - 2.0:
            mu = float(max_so_far) if (is_cooling or peak_status == "past") else float(max_so_far) + 0.5
        elif peak_status in ("past", "in_window"):
            mu = forecast_median * 0.7 + float(center) * 0.3
            if max_so_far > mu: mu = float(max_so_far) + (0.3 if not is_cooling else 0.0)

    # Compute probability per bucket
    from settlement_rounding import apply_city_settlement
    import math
    phi = lambda z: 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

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
            # V21.7.53: One-tailed CDF for threshold markets
            if b_threshold_dir == "higher":
                z = (bucket_temp - 0.5 - mu) / sigma
                our_prob = 1.0 - phi(z)
            else:
                z = (bucket_temp + 0.5 - mu) / sigma
                our_prob = phi(z)
        else:
            z_low = (bucket_temp - 0.5 - mu) / sigma
            z_high = (bucket_temp + 0.5 - mu) / sigma
            our_prob = phi(z_high) - phi(z_low)
        cap = 0.90 if b_is_threshold else 0.85
        our_prob = max(0.01, min(cap, our_prob))

        yes_edge = our_prob - market_prob
        no_edge = (1 - our_prob) - (1 - market_prob)
        best_edge = max(yes_edge, no_edge)
        recommended_side = "YES" if yes_edge >= no_edge else "NO"

        min_edge_adjusted = min_edge_pp + RISK_PROFILES.get(risk, RISK_PROFILES["medium"])["edge_add"]

        signal = {
            "city": city, "temp": bucket_temp,
            "is_threshold": b_is_threshold,  # V21.7.53
            "threshold_direction": b_threshold_dir,
            "our_prob": round(our_prob, 4), "market_prob": market_prob,
            "yes_price": b.get("yes_price", 0), "no_price": b.get("no_price", 0),
            "yes_edge_pp": round(yes_edge * 100, 1), "no_edge_pp": round(no_edge * 100, 1),
            "best_edge": round(best_edge * 100, 1), "recommended_side": recommended_side,
            "edge_pp": round(best_edge * 100, 1),
            # V21.7.53: Value-tier scoring
            "entry_price": b.get("yes_price", 0) if recommended_side == "YES" else b.get("no_price", 0),
            "payout_ratio": round(1.0 / max(b.get("yes_price", 0) if recommended_side == "YES" else b.get("no_price", 0), 0.01), 1),
            "ev_per_dollar": round(our_prob / max(b.get("yes_price", 0) if recommended_side == "YES" else b.get("no_price", 0), 0.01), 2),
            "yes_token_id": b.get("yes_token_id", ""), "no_token_id": b.get("no_token_id", ""),
            "condition_id": b.get("condition_id", ""), "market_id": b.get("market_id", ""),
            "neg_risk": True, "risk_level": risk,
            "sigma_used": round(sigma, 2),
            "prob_info": f"μ={mu:.1f} σ={sigma:.1f} peak={peak_status} dead={is_dead} DEB[{deb_result.get('version','?')}] models={len(deb_input)} ens_n={ensemble_n}",
            "deb_version": deb_result.get("version", "unknown"),
            "deb_prediction": deb_result.get("prediction"),
            "deb_bias_adjustment": deb_result.get("bias_adjustment", 0),
            "deb_bias_samples": deb_result.get("bias_samples", 0),
        }
        if signal["best_edge"] >= min_edge_adjusted:
            signals.append(signal)

    # V21.7.53: Composite ranking score — edge + value-tier + threshold boost
    # Instead of sorting purely by edge_pp, we compute a composite score:
    #   composite = edge_pp + threshold_boost + value_bonus
    # where:
    #   threshold_boost = 5.0 if threshold market (one-tailed, structurally easier)
    #   value_bonus = min(payout_ratio * 0.5, 10.0) — rewards low-price entries
    # This biases toward badatmath-style 2-15¢ entries with high payout ratios
    for s in signals:
        threshold_boost = 5.0 if s.get("is_threshold") else 0.0
        value_bonus = min(s.get("payout_ratio", 1.0) * 0.5, 10.0)
        s["composite_score"] = round(s["best_edge"] + threshold_boost + value_bonus, 1)
        s["threshold_boost"] = threshold_boost
        s["value_bonus"] = value_bonus

    return sorted(signals, key=lambda s: s["composite_score"], reverse=True)


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
    """Check promotion criteria for live eligibility."""
    resolved = []
    if PAPER_TRADES.exists():
        with open(PAPER_TRADES) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if d.get("settled", False):
                        resolved.append(d)
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

    readiness = {
        "live_blocked": WEATHER_BOT_LIVE_BLOCKED,
        "block_reason": "WEATHER_BOT_LIVE_BLOCKED=True in source",
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
        "all_criteria_met": (total_resolved >= 25 and total_pnl > 0
                              and profit_factor >= 1.25 and not has_errors),
        "ready_for_live": False,  # Always False while WEATHER_BOT_LIVE_BLOCKED=True
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
    MAX_DAY_OFFSETS = 2        # Only check today + tomorrow
    # V21.7.53: Multi-bucket portfolio — max adjacent buckets per city
    MAX_ADJACENT_BUCKETS = 3   # Buy up to 3 adjacent temp buckets for same city+date
    PORTFOLIO_BUDGET_PER_CITY = 6.0  # Max $6 spread across adjacent buckets

    def __init__(self, bankroll: float = 20.0):
        super().__init__(paper_only=True, bankroll=bankroll)
        self.state.paper_only = True  # FORCE paper mode
        self._cycle_count = 0
        if not WEATHER_BOT_LIVE_BLOCKED:
            pass
        # Override output paths for V2.1
        self._state_file = STATE_FILE

    def run_once(self):
        """V21.7.53: Override with multi-bucket portfolio selection.
        Instead of just taking top 3 signals, we:
        1. Take the top signal by composite score
        2. Look for adjacent temp buckets (±1°C, ±2°C) for same city+date
        3. Enter up to MAX_ADJACENT_BUCKETS positions, budget capped at PORTFOLIO_BUDGET_PER_CITY
        4. Then take next top signal from a different city
        """
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

        for sig in signals:
            if len(entered) >= 5:  # MAX_CONCURRENT
                break

            city = sig.get("city", "?")
            date = sig.get("date", "")
            city_date_key = (city, date)

            if city_date_key in used_city_dates:
                continue  # Already covered this city+date with a portfolio

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

                for day_offset in range(min(self.MAX_DAY_OFFSETS + 1, 3)):
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

                    signals = compute_edge_v2(forecast_temps, buckets, city,
                                              max_so_far=max_so_far, current_temp=current_temp,
                                              local_hour=local_hour, is_cooling=is_cooling,
                                              min_edge_pp=15.0, min_volume=200.0)

                    # §V22: DEB-enhanced edge computation (replaces above when available)
                    if HAS_FDEB:
                        # Fetch multi-model + ensemble ONCE for this city+day
                        mm = fetch_multi_model_forecasts(lat, lon, forecast_days=3)
                        ens = fetch_ensemble_forecast(lat, lon, forecast_days=3)
                        deb_signals = compute_edge_v22(
                            lat=lat, lon=lon, city=city, target_date=target_date,
                            buckets=buckets, max_so_far=max_so_far, current_temp=current_temp,
                            local_hour=local_hour, is_cooling=is_cooling, day_offset=day_offset,
                            min_edge_pp=15.0, min_volume=200.0,
                            multi_model=mm, ensemble=ens,
                        )
                        if deb_signals:
                            signals = deb_signals  # Override with DEB-enhanced signals

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
        if halt_cfg.get("disable_new_weather_temperature_entries", True):
            log.info(f"TEMP_ENTRIES_HALTED: skipping {signal.get('city', '?')} — V21.7.14 halt directive active")
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

        # Use risk-adjusted position size
        position_size = get_position_size(city, MAX_POSITION_USD)
        edge_threshold = get_edge_threshold(city)

        if signal["best_edge"] < edge_threshold:
            log.info(f"SKIP {city} {signal['temp']}°C — edge {signal['best_edge']:.1f}pp < threshold {edge_threshold:.0f}pp (risk={meta.get('risk','medium')})")
            return None

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

        # Paper trade: record with full audit
        tier_str = f"[{risk_tier}]" if risk_tier != "TRADE" else ""
        log.info(f"PAPER BUY {outcome} {city} {signal['temp']}°C "
                 f"@ {entry_price:.2f} | edge={signal['best_edge']:.1f}pp "
                 f"risk={meta.get('risk','medium')} pos=${cost:.2f} {tier_str} | "
                 f"{signal.get('prob_info', '')[:60]}")

        self.positions.append(pos)
        self.state.bankroll -= cost
        self.state.daily_trades += 1
        self.state.active_positions += 1
        self.state.total_trades += 1

        # Write to paper trades log
        trade_record = asdict(pos)
        trade_record["risk_tier"] = risk_tier
        trade_record["position_size"] = position_size
        trade_record["edge_threshold_used"] = edge_threshold
        trade_record["live_blocked"] = WEATHER_BOT_LIVE_BLOCKED
        trade_record["settlement_source"] = meta.get("settle", "metar")
        trade_record["rounding_rule"] = "floor" if is_hko_floor_city(city) else "wu_round"
        trade_record["tz_offset"] = meta.get("tz", 0)
        trade_record["distance_km"] = meta.get("dist", 0)
        with open(PAPER_TRADES, "a") as f:
            f.write(json.dumps(trade_record) + "\n")

        self.save_state()
        return pos

    def settle_positions(self):
        """Settle positions via Polymarket Gamma API resolution (primary) or METAR fallback."""
        import requests as _requests
        now = datetime.now(timezone.utc)

        for pos in [p for p in self.positions if not p.settled]:
            # Check if enough time has passed for PM to resolve (6h grace)
            target_dt = datetime.strptime(pos.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if now < target_dt + timedelta(hours=6):
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
                        # Match by condition_id or by temperature bucket
                        matched = False
                        if pos_cid and mk_cid:
                            matched = (mk_cid == pos_cid)
                        if not matched:
                            import re
                            temp_match = re.search(r'(\d+)°C', m.get("question", ""))
                            if temp_match and hasattr(pos, "bucket_temp"):
                                matched = (int(temp_match.group(1)) == pos.bucket_temp)
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
                        else:
                            self.state.losses += 1
                            self.state.consecutive_losses += 1
                            # Don't count settlement losses against daily circuit breaker
                            self.state.weekly_loss += pnl
                        self.state.active_positions -= 1

                        log.info(f"✅ PM SETTLED {pos.trade_id}: {pos.city} {pos.outcome} @ {pos.entry_price} | "
                                 f"winning={winning_outcome} | PnL=${pnl:.2f}")
                        break  # Position settled, move to next
            except Exception as e:
                log.warning(f"PM Gamma settlement check failed for {slug}: {e}")

            # If not settled by PM, try METAR fallback
            if pos.settled:
                continue

            meta = CITY_REGISTRY.get(pos.city, {})
            icao = meta.get("icao", "")
            metar = fetch_metar(icao) if icao else None
            if not metar or metar.get("temp_c") is None:
                continue

            actual_temp = metar["temp_c"]
            audit = audit_settlement(pos, actual_temp, meta)
            settled_temp = audit["settled_temp"]
            bucket_hit = audit["bucket_hit"]
            payout_per_share = audit["payout_per_share"]
            total_payout = audit["total_payout"]
            pnl = audit["pnl"]

            pos.exit_ts = now.isoformat()
            pos.exit_price = payout_per_share
            pos.pnl = pnl
            pos.settled = True
            pos.settlement_temp = actual_temp
            pos.settlement_source = "METAR"

            self.state.bankroll += total_payout
            self.state.total_pnl += pnl
            if pnl > 0:
                self.state.wins += 1
                self.state.consecutive_losses = 0
            else:
                self.state.losses += 1
                self.state.consecutive_losses += 1
                # Don't count settlement losses against daily circuit breaker
                self.state.weekly_loss += pnl
            self.state.active_positions -= 1

            log.info(f"METAR SETTLED {pos.trade_id}: {pos.city} {pos.bucket_temp}°C "
                     f"actual={actual_temp}°C settled={settled_temp}°C "
                     f"hit={bucket_hit} PnL=${pnl:.2f}")

        # Remove settled positions from list so they don't count against max_positions
        self.positions = [p for p in self.positions if not getattr(p, 'settled', False)]
        self.save_state()

    def save_state(self):
        """Override state file path for V2.1."""
        self.state.timestamp = datetime.now(timezone.utc).isoformat()
        with open(STATE_FILE, "w") as f:
            json.dump(asdict(self.state), f, indent=2)

    def load_state(self):
        """Override state file path for V2.1."""
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE) as f:
                    self.state = WeatherState(**json.load(f))
            except Exception as e:
                log.warning(f"State load failed: {e}")
        # Load positions from paper trades
        self.positions = []
        if PAPER_TRADES.exists():
            with open(PAPER_TRADES) as f:
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
    parser.add_argument("--paper", action="store_true", default=True, help="Paper trading (forced)")
    parser.add_argument("--live", action="store_true", help="BLOCKED — live mode disabled")
    parser.add_argument("--once", action="store_true", help="Run one scan cycle")
    parser.add_argument("--status", action="store_true", help="Show status dashboard with live readiness")
    parser.add_argument("--bankroll", type=float, default=20.0, help="Starting bankroll")
    parser.add_argument("--interval", type=int, default=900, help="Scan interval in seconds (default: 900=15min)")
    parser.add_argument("--hindcast", action="store_true", help="Generate hindcast report only")
    parser.add_argument("--risk-report", action="store_true", help="Generate city risk report only")
    parser.add_argument("--readiness", action="store_true", help="Check live readiness only")
    args = parser.parse_args()

    if args.live:
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

    bot = WeatherBotV21(bankroll=args.bankroll)
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
                    log.info(f"Cycle {cycle} | Resolved: {readiness['performance']['total_resolved']} | "
                             f"PnL: ${readiness['performance']['total_pnl']:.2f} | "
                             f"PF: {readiness['performance']['profit_factor']:.2f} | "
                             f"WR: {readiness['performance']['win_rate']:.1%}")
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