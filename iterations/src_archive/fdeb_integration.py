#!/usr/bin/env python3
"""
FDEB: FDC DEB Integration Module
=================================
Connects Open-Meteo multi-model ensemble API to the PolyWeather DEB algorithm.

Fetches temperature forecasts from multiple NWP models:
  - Open-Meteo bestmatch (default)
  - ECMWF IFS 0.25°
  - GFS Seamless
  - ICON Seamless
  - JMA Seamless
  - MeteoFrance Seamless
  - GEM Seamless (Environment Canada)
  - Open-Meteo Ensemble (30 members for sigma calculation)

Feeds them to deb_algorithm.calculate_deb_prediction() which:
  - Weights models by inverse MAE with time decay
  - Applies recent bias correction
  - Returns versioned prediction with quality metrics

Integration point: v1_weather_runner_v2.py compute_reality_anchored_probability()
  Replace: compute_deb_weights() → fdeb_integration.deb_enhanced_probability()
"""
from __future__ import annotations

import math
import time
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from pathlib import Path

# ─── Paths ───
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
import sys
sys.path.insert(0, str(PROJECT_ROOT / "src" / "polyweather_analysis"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

log = logging.getLogger("fdeb")

# ─── Model definitions ───
MULTI_MODELS = {
    "open_meteo": None,  # Default model (bestmatch) — no models param needed
    "ecmwf_ifs025": "ecmwf_ifs025",
    "gfs_seamless": "gfs_seamless",
    "icon_seamless": "icon_seamless",
    "jma_seamless": "jma_seamless",
    "meteofrance_seamless": "meteofrance_seamless",
    "gem_seamless": "gem_seamless",
}

# Models that consistently return data (based on API testing)
RELIABLE_MODELS = {
    "open_meteo": None,
    "ecmwf_ifs025": "ecmwf_ifs025",
    "gfs_seamless": "gfs_seamless",
    "icon_seamless": "icon_seamless",
    "jma_seamless": "jma_seamless",
    "meteofrance_seamless": "meteofrance_seamless",
    "gem_seamless": "gem_seamless",
}

API_BASE = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_API = "https://ensemble-api.open-meteo.com/v1/ensemble"
TIMEOUT = 15


def fetch_multi_model_forecasts(lat: float, lon: float, forecast_days: int = 3) -> Dict[str, Dict]:
    """
    Fetch daily max temperature from multiple NWP models.
    
    Returns:
        {model_name: {"dates": [...], "max_temps": [...]}}
    """
    results = {}
    
    for model_name, model_param in RELIABLE_MODELS.items():
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max",
            "timezone": "auto",
            "forecast_days": forecast_days,
        }
        if model_param:
            params["models"] = model_param
        
        try:
            r = requests.get(API_BASE, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                d = r.json()
                daily = d.get("daily", {})
                dates = daily.get("time", [])
                temps = daily.get("temperature_2m_max", [])
                if temps and any(t is not None for t in temps):
                    results[model_name] = {
                        "dates": dates,
                        "max_temps": temps,
                    }
        except Exception as e:
            log.debug(f"Fetch error for {model_name}: {e}")
            continue
    
    return results


def fetch_ensemble_forecast(lat: float, lon: float, forecast_days: int = 3) -> Optional[Dict]:
    """
    Fetch Open-Meteo ensemble (30 members) for sigma calculation.
    
    Returns:
        {"dates": [...], "members": [[temp_day0, temp_day1, ...], ...], 
         "avg": [...], "std": [...], "n_members": int}
    """
    try:
        r = requests.get(ENSEMBLE_API, params={
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max",
            "timezone": "auto",
            "forecast_days": forecast_days,
        }, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        
        d = r.json()
        daily = d.get("daily", {})
        dates = daily.get("time", [])
        
        # Collect member forecasts
        members = []
        for key, values in daily.items():
            if key.startswith("temperature_2m_max_member") and values:
                member_temps = [float(v) if v is not None else None for v in values]
                if any(t is not None for t in member_temps):
                    members.append(member_temps)
        
        if not members:
            return None
        
        # Compute per-day stats
        n_days = len(dates)
        avg = []
        std = []
        for day_idx in range(n_days):
            day_vals = [m[day_idx] for m in members if day_idx < len(m) and m[day_idx] is not None]
            if day_vals:
                avg.append(round(sum(day_vals) / len(day_vals), 2))
                if len(day_vals) > 1:
                    std.append(round(
                        (sum((x - sum(day_vals)/len(day_vals))**2 for x in day_vals) / len(day_vals)) ** 0.5, 2
                    ))
                else:
                    std.append(0.0)
            else:
                avg.append(None)
                std.append(None)
        
        return {
            "dates": dates,
            "members": members,
            "avg": avg,
            "std": std,
            "n_members": len(members),
        }
    except Exception as e:
        log.debug(f"Ensemble fetch error: {e}")
        return None


def build_deb_forecasts(
    multi_model: Dict[str, Dict],
    ensemble: Optional[Dict],
    target_date: str,
) -> Dict[str, float]:
    """
    Build the forecast dict for DEB from multi-model + ensemble data.
    
    Returns dict like:
        {"open_meteo": 25.0, "ecmwf_ifs025": 25.7, "gfs_seamless": 26.6, ...}
        Plus ensemble stats: "Ensemble-avg", "Ensemble-std", "Ensemble-n"
    """
    forecasts = {}
    
    # Multi-model forecasts for target date
    for model_name, data in multi_model.items():
        dates = data.get("dates", [])
        temps = data.get("max_temps", [])
        if target_date in dates:
            idx = dates.index(target_date)
            if idx < len(temps) and temps[idx] is not None:
                forecasts[model_name] = float(temps[idx])
    
    # Ensemble stats for target date
    if ensemble:
        dates = ensemble.get("dates", [])
        if target_date in dates:
            idx = dates.index(target_date)
            avg = ensemble.get("avg", [])
            std = ensemble.get("std", [])
            n = ensemble.get("n_members", 0)
            if idx < len(avg) and avg[idx] is not None:
                forecasts["Ensemble-avg"] = float(avg[idx])
            if idx < len(std) and std[idx] is not None:
                forecasts["Ensemble-std"] = float(std[idx])
            forecasts["Ensemble-n"] = float(n)
    
    return forecasts


def deb_enhanced_probability(
    lat: float,
    lon: float,
    bucket_temp: int,
    city: str,
    target_date: str,
    max_so_far: Optional[float] = None,
    current_temp: Optional[float] = None,
    local_hour: float = 12.0,
    is_cooling: bool = False,
    day_offset: int = 0,
    city_risk: str = "medium",
    city_dist_km: float = 0.0,
) -> Tuple[float, float, str, Dict]:
    """
    Full DEB-enhanced probability calculation.
    
    1. Fetch multi-model forecasts
    2. Fetch ensemble for sigma
    3. Run calculate_deb_prediction() for bias-corrected center
    4. Compute sigma from ensemble spread + risk + distance + horizon
    5. Compute bucket probability with reality anchoring
    
    Returns: (probability, sigma, info_str, deb_result_dict)
    """
    from deb_algorithm import calculate_deb_prediction, update_daily_record, bootstrap_recent_daily_history_if_missing
    from settlement_rounding import apply_city_settlement
    
    # Fetch forecasts
    multi_model = fetch_multi_model_forecasts(lat, lon, forecast_days=3)
    ensemble = fetch_ensemble_forecast(lat, lon, forecast_days=3)
    
    # Build forecast dict for target date
    forecasts = build_deb_forecasts(multi_model, ensemble, target_date)
    
    if not forecasts:
        return 0.01, 2.0, "no forecasts available", {}
    
    # Remove ensemble stats from the forecast dict that goes to DEB
    # (DEB expects model_name → temp_value)
    deb_input = {k: v for k, v in forecasts.items() 
                 if not k.startswith("Ensemble-")}
    
    if not deb_input:
        # Fallback: use ensemble average
        deb_input = {"Ensemble-avg": forecasts.get("Ensemble-avg", 20.0)}
    
    # Bootstrap history if missing (fetches past forecasts + METAR observations)
    try:
        bootstrap_recent_daily_history_if_missing(city, lookback_days=14)
    except Exception as e:
        log.debug(f"Bootstrap error for {city}: {e}")
    
    # Run DEB prediction
    try:
        deb_result = calculate_deb_prediction(
            city_name=city,
            current_forecasts=deb_input,
            lookback_days=7,
            decay_factor=0.85,
            bias_lookback_days=30,
            bias_min_samples=3,
        )
    except Exception as e:
        log.warning(f"DEB prediction error for {city}: {e}")
        # Fallback to simple median
        valid = [v for v in deb_input.values() if v is not None]
        if valid:
            center = sorted(valid)[len(valid) // 2]
        else:
            center = 20.0
        deb_result = {
            "prediction": center,
            "raw_prediction": center,
            "version": "fallback_median",
            "weights_info": f"fallback(median={center:.1f})",
            "bias_adjustment": 0.0,
            "bias_samples": 0,
        }
    
    # Get DEB center (bias-corrected)
    center = deb_result.get("prediction")
    if center is None:
        center = deb_result.get("raw_prediction")
    if center is None:
        valid = [v for v in deb_input.values() if v is not None]
        center = sorted(valid)[len(valid) // 2] if valid else 20.0
    
    # ─── Reality anchor ───
    mu = float(center)
    forecast_median = sorted([v for v in deb_input.values() if v is not None])[len(deb_input) // 2] if deb_input else center
    
    peak_status = "before"
    # Approximate peak hours by latitude
    lat_abs = abs(lat)
    if lat_abs > 55:
        first_peak, last_peak = 10.0, 16.0
    elif lat_abs > 35:
        first_peak, last_peak = 11.0, 15.0
    elif lat_abs > 20:
        first_peak, last_peak = 11.5, 14.5
    else:
        first_peak, last_peak = 12.0, 14.0
    
    if local_hour >= first_peak and local_hour <= last_peak:
        peak_status = "in_window"
    elif local_hour > last_peak:
        peak_status = "past"
    
    if max_so_far is not None:
        if peak_status in ("past", "in_window") and max_so_far < forecast_median - 2.0:
            if is_cooling or peak_status == "past":
                mu = float(max_so_far)
            else:
                mu = float(max_so_far) + 0.5
        elif peak_status in ("past", "in_window"):
            mu = forecast_median * 0.7 + float(center) * 0.3
            if max_so_far > mu:
                mu = float(max_so_far) + (0.3 if not is_cooling else 0.0)
    
    # ─── Sigma calculation ───
    ensemble_std = forecasts.get("Ensemble-std")
    ensemble_n = int(forecasts.get("Ensemble-n", 0))
    
    if ensemble_std is not None and ensemble_n >= 10:
        sigma = max(1.0, float(ensemble_std))
    elif len(deb_input) > 2:
        vals = [v for v in deb_input.values() if v is not None]
        sigma = max(1.0, (max(vals) - min(vals)) / 2.0) if vals else 2.0
    else:
        sigma = 2.0
    
    # Risk tier adjustments
    risk_sigma_add = {"low": 0.0, "medium": 0.3, "high": 0.8}
    sigma += risk_sigma_add.get(city_risk, 0.3)
    
    # Resolution uncertainty: airport-city distance
    if city_dist_km > 10:
        sigma += 0.5 * (city_dist_km / 10.0)
    
    # Forecast horizon penalty
    if day_offset > 0:
        sigma += 0.5 * day_offset
    
    # Peak-time sigma reduction
    if peak_status == "past" and local_hour >= 21:
        sigma *= 0.6
    elif peak_status == "past" and local_hour > last_peak:
        sigma *= 0.8
    elif peak_status == "in_window":
        sigma *= 0.9
    
    # Dead market detection
    is_dead = False
    if max_so_far is not None and current_temp is not None:
        if local_hour >= 21 and max_so_far - current_temp >= 3.0:
            is_dead = True
        elif peak_status == "past" and max_so_far - current_temp >= 1.5:
            is_dead = True
    
    if is_dead:
        sigma = max(1.0, sigma * 0.5)
    
    # ─── Bucket probability ───
    phi = lambda z: 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    
    if is_dead and max_so_far is not None:
        settled_temp = apply_city_settlement(city, max_so_far)
        prob = 1.0 if bucket_temp == settled_temp else 0.01
    else:
        z_low = (bucket_temp - 0.5 - mu) / sigma
        z_high = (bucket_temp + 0.5 - mu) / sigma
        prob = phi(z_high) - phi(z_low)
    
    # Cap at 0.85
    prob = max(0.01, min(0.85, prob))
    
    # ─── Info string ───
    deb_ver = deb_result.get("version", "unknown")
    deb_w = deb_result.get("weights_info", "")
    bias_adj = deb_result.get("bias_adjustment", 0.0)
    bias_n = deb_result.get("bias_samples", 0)
    
    info = (f"μ={mu:.1f} σ={sigma:.1f} peak={peak_status}"
            f" dead={is_dead} max={max_so_far} cur={current_temp}"
            f" h={local_hour:.0f}"
            f" DEB[{deb_ver}] bias={bias_adj:+.1f}(n={bias_n})"
            f" models={len(deb_input)} ens_n={ensemble_n}")
    if deb_w:
        info += f" W:{deb_w[:80]}"
    
    return prob, sigma, info, deb_result


def record_actual_high(city: str, date_str: str, actual_high: float,
                       forecasts: Dict[str, float] = None):
    """
    Record an actual high temperature + forecasts for DEB learning.
    Call this after settlement to update DEB history.
    """
    from deb_algorithm import update_daily_record
    try:
        update_daily_record(
            city_name=city,
            date_str=date_str,
            forecasts=forecasts or {},
            actual_high=actual_high,
        )
        log.info(f"DEB history updated: {city} {date_str} actual={actual_high}°C")
    except Exception as e:
        log.warning(f"DEB history update error: {e}")


if __name__ == "__main__":
    # Quick test
    print("FDEB Integration Test — London")
    print("=" * 60)
    
    lat, lon = 51.5048, 0.0522
    target = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    
    prob, sigma, info, deb = deb_enhanced_probability(
        lat=lat, lon=lon,
        bucket_temp=25,
        city="london",
        target_date=target,
        local_hour=12.0,
        day_offset=1,
        city_risk="low",
        city_dist_km=12.7,
    )
    
    print(f"Target: {target}")
    print(f"Bucket: 25°C")
    print(f"Probability: {prob:.3f}")
    print(f"Sigma: {sigma:.2f}")
    print(f"DEB version: {deb.get('version', '?')}")
    print(f"DEB prediction: {deb.get('prediction', '?')}")
    print(f"Bias adj: {deb.get('bias_adjustment', 0):+.1f} (n={deb.get('bias_samples', 0)})")
    print(f"Info: {info}")