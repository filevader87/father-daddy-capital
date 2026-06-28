#!/usr/bin/env python3
"""
V21.7.52 — Weather Sigma Fix Hindcast Test
===========================================
Re-runs the probability model with FIXED sigma on the 5 prior losses
to verify the fix would have prevented catastrophic edge overstatement.
"""

import sys, json, math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src" / "weather"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src" / "v217_live"))

from v1_weather_runner_v2 import (
    CITY_REGISTRY, RISK_PROFILES, compute_reality_anchored_probability
)

# Prior 5 losses: city, forecast, actual, bucket, day_offset
PRIOR_LOSSES = [
    {"city": "amsterdam", "forecast": 20.2, "actual": 13.0, "bucket": 22, "day_offset": 1, "old_sigma": 0.3, "old_prob": 0.935, "market_prob": 0.05},
    {"city": "istanbul", "forecast": 23.1, "actual": 20.0, "bucket": 24, "day_offset": 1, "old_sigma": 0.3, "old_prob": 0.655, "market_prob": 0.15},
    {"city": "helsinki", "forecast": 25.0, "actual": 13.0, "bucket": 23, "day_offset": 1, "old_sigma": 0.3, "old_prob": 0.61, "market_prob": 0.30},
    {"city": "moscow", "forecast": 27.0, "actual": 19.0, "bucket": 25, "day_offset": 1, "old_sigma": 0.3, "old_prob": 0.96, "market_prob": 0.20},
    {"city": "london", "forecast": 18.2, "actual": 13.0, "bucket": 20, "day_offset": 1, "old_sigma": 0.3, "old_prob": 0.955, "market_prob": 0.15},
]

def gaussian_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

def compute_fixed_probability(forecast, bucket, city, day_offset=0, local_hour=12.0):
    """Compute probability with FIXED sigma model."""
    forecast_temps = {
        "Open-Meteo": forecast,
        "Ensemble-avg": forecast,
        "Ensemble-std": 2.5,  # Typical ensemble spread for European cities
        "Ensemble-n": 30,
        "Ensemble-max": forecast + 2.5,
        "Ensemble-min": forecast - 2.5,
    }
    
    prob, sigma, info = compute_reality_anchored_probability(
        forecast_temps, bucket, max_so_far=None, current_temp=None,
        city=city, local_hour=local_hour, is_cooling=False,
        day_offset=day_offset,
    )
    return prob, sigma, info

print("=" * 70)
print("V21.7.52 — Weather Sigma Fix Hindcast Test")
print("=" * 70)
print()
print(f"{'City':<12} {'Bucket':>6} {'Forecast':>8} {'Actual':>6} | {'Old σ':>6} {'Old P':>6} {'Old Edge':>8} | {'New σ':>6} {'New P':>6} {'New Edge':>8} | {'Would Trade?':>11}")
print("-" * 110)

total_old_edge = 0
total_new_edge = 0
trades_prevented = 0

for loss in PRIOR_LOSSES:
    new_prob, new_sigma, info = compute_fixed_probability(
        loss["forecast"], loss["bucket"], loss["city"], loss["day_offset"]
    )
    
    old_edge = (loss["old_prob"] - loss["market_prob"]) * 100
    new_edge = (new_prob - loss["market_prob"]) * 100
    
    # Minimum edge threshold = 15pp (low risk) + risk profile edge_add
    meta = CITY_REGISTRY.get(loss["city"], {})
    risk = meta.get("risk", "medium")
    risk_profile = RISK_PROFILES.get(risk, RISK_PROFILES["medium"])
    min_edge = 15.0 + risk_profile["edge_add"]
    
    would_trade = new_edge >= min_edge
    
    total_old_edge += old_edge
    total_new_edge += new_edge
    if not would_trade:
        trades_prevented += 1
    
    print(f"{loss['city']:<12} {loss['bucket']:>6}° {loss['forecast']:>7.1f}° {loss['actual']:>5.1f}° | {loss['old_sigma']:>5.1f} {loss['old_prob']:>5.1%} {old_edge:>+7.1f}pp | {new_sigma:>5.1f} {new_prob:>5.1%} {new_edge:>+7.1f}pp | {'YES ❌' if would_trade else 'NO ✅':>11}")

print("-" * 110)
print(f"{'TOTAL':>34} | {'':>6} {'':>6} {total_old_edge/5:>+7.1f}pp | {'':>6} {'':>6} {total_new_edge/5:>+7.1f}pp |")
print()
print(f"Old model: avg edge = {total_old_edge/5:+.1f}pp → entered 5 trades → 0W/5L = -$7.60")
print(f"New model: avg edge = {total_new_edge/5:+.1f}pp → would enter {5-trades_prevented} trades")
print(f"Trades prevented by fix: {trades_prevented}/5")
print()

if trades_prevented == 5:
    print("✅ ALL 5 CATASTROPHIC TRADES WOULD HAVE BEEN PREVENTED")
elif trades_prevented >= 3:
    print(f"⚠️  {trades_prevented}/5 trades prevented — significant improvement")
else:
    print("❌ Fix insufficient — too many trades still pass")

print()
print("Sigma comparison:")
for loss in PRIOR_LOSSES:
    new_prob, new_sigma, info = compute_fixed_probability(
        loss["forecast"], loss["bucket"], loss["city"], loss["day_offset"]
    )
    meta = CITY_REGISTRY.get(loss["city"], {})
    dist = meta.get("dist", 0)
    print(f"  {loss['city']:<12}: old σ=0.3  → new σ={new_sigma:.1f}  (dist={dist:.0f}km, risk={meta.get('risk','?')})")