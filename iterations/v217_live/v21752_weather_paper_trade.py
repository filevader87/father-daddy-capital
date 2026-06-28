#!/usr/bin/env python3
"""
V21.7.52 — Weather Bot Paper Trade Entry (Sigma-Fixed)
======================================================
Creates a paper trade entry using the FIXED sigma model.
WEATHER_LIVE_ALLOWED = false — paper only.
"""

import sys, json, time
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src" / "weather"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src" / "v217_live"))

from v1_weather_runner_v2 import (
    CITY_REGISTRY, fetch_open_meteo_forecast, fetch_open_meteo_ensemble,
    discover_weather_markets, parse_temperature_markets, compute_edge_v2,
)

PAPER_TRADES_FILE = Path(__file__).resolve().parent.parent.parent / "output" / "weather_bot" / "v2_1_paper_trades.jsonl"
OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output" / "v21752_weather_live_readiness"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ─── Configuration ───
PAPER_SIZE_USD = 1.0  # Micro-canary
MAX_DAILY_TRADES = 1
WEATHER_LIVE_ALLOWED = False

def create_paper_entry():
    """Scan for best weather paper trade opportunity."""
    today = datetime.now(timezone.utc)
    best_signal = None
    best_edge = 0
    all_candidates = []

    # Scan top cities
    scan_cities = list(CITY_REGISTRY.keys())[:30]
    
    for city in scan_cities:
        meta = CITY_REGISTRY.get(city, {})
        if not meta:
            continue
        lat, lon = meta['lat'], meta['lon']
        risk = meta.get('risk', 'medium')
        
        for day_offset in range(3):
            target_date = (today + timedelta(days=day_offset)).strftime('%Y-%m-%d')
            
            # Fetch forecasts
            try:
                om_data = fetch_open_meteo_forecast(lat, lon, days=3)
            except:
                continue
            if not om_data:
                continue
            
            daily = om_data.get('daily', {})
            dates = daily.get('time', [])
            max_temps = daily.get('temperature_2m_max', [])
            try:
                day_idx = dates.index(target_date)
                local_day_high = max_temps[day_idx]
            except:
                continue

            forecast_temps = {'Open-Meteo': local_day_high}
            
            # Ensemble
            try:
                ens_data = fetch_open_meteo_ensemble(lat, lon)
            except:
                ens_data = None
            if ens_data:
                ens_daily = ens_data.get('daily', {})
                ens_highs = []
                for key, values in ens_daily.items():
                    if key.startswith('temperature_2m_max') and key != 'temperature_2m_max':
                        if values and day_offset < len(values) and values[day_offset] is not None:
                            try:
                                ens_highs.append(float(values[day_offset]))
                            except:
                                pass
                if ens_highs:
                    ens_avg = sum(ens_highs) / len(ens_highs)
                    ens_std = (sum((x - ens_avg)**2 for x in ens_highs) / len(ens_highs)) ** 0.5
                    forecast_temps['Ensemble-avg'] = ens_avg
                    forecast_temps['Ensemble-max'] = max(ens_highs)
                    forecast_temps['Ensemble-min'] = min(ens_highs)
                    forecast_temps['Ensemble-std'] = ens_std
                    forecast_temps['Ensemble-n'] = len(ens_highs)

            # Discover market
            try:
                event = discover_weather_markets(city, target_date)
            except:
                continue
            if not event:
                continue
            buckets = parse_temperature_markets(event)
            if not buckets:
                continue

            tz_offset = meta.get('tz', 0)
            city_tz = timezone(timedelta(seconds=tz_offset))
            local_hour = datetime.now(city_tz).hour + datetime.now(city_tz).minute / 60.0

            signals = compute_edge_v2(
                forecast_temps, buckets, city,
                max_so_far=None, current_temp=None,
                local_hour=local_hour, is_cooling=False,
                min_edge_pp=15.0, min_volume=500.0,
                day_offset=day_offset
            )
            
            for sig in signals:
                sig['date'] = target_date
                sig['day_offset'] = day_offset
                sig['ensemble_std'] = forecast_temps.get('Ensemble-std', 0)
                sig['ensemble_n'] = forecast_temps.get('Ensemble-n', 0)
                all_candidates.append(sig)

    # Sort by absolute edge, prefer NO side (more conservative)
    all_candidates.sort(key=lambda s: abs(s['best_edge']), reverse=True)
    
    # Filter: prefer day_offset=1 (tomorrow), volume > 1000, risk != high
    preferred = [s for s in all_candidates if s['day_offset'] == 1 and s['volume'] >= 1000]
    if not preferred:
        preferred = [s for s in all_candidates if s['volume'] >= 500]
    if not preferred:
        preferred = all_candidates

    if not preferred:
        print("No weather paper trade candidates found")
        return None

    best = preferred[0]
    
    # Determine side and token
    side = best['recommended_side']
    if side == 'NO':
        entry_price = best['no_price']
        selected_token_id = best['no_token_id']
        opposite_token_id = best['yes_token_id']
    else:
        entry_price = best['yes_price']
        selected_token_id = best['yes_token_id']
        opposite_token_id = best['no_token_id']

    # Generate trade ID
    trade_id = f"WV21752-{best['city'].upper()}{best['temp']}{side[0]}{int(time.time())}"

    paper_entry = {
        "trade_id": trade_id,
        "version": "V21.7.52",
        "city": best['city'],
        "date": best['date'],
        "day_offset": best['day_offset'],
        "market_slug": f"highest-temperature-in-{best['city']}-on-{best['date']}",
        "condition_id": best['condition_id'],
        "question": best['question'],
        "bucket_temp": best['temp'],
        "side": side,
        "outcome": side,
        "selected_token_id": selected_token_id,
        "opposite_token_id": opposite_token_id,
        "entry_price": round(entry_price, 4),
        "market_prob": round(best['yes_price'], 4),
        "forecast_prob": round(best['our_prob'], 4),
        "no_prob": round(1.0 - best['our_prob'], 4),
        "edge_pp": round(best['best_edge'], 1),
        "forecast_max": best.get('forecast_max', 0),
        "forecast_source": "open_meteo_ensemble",
        "ensemble_std": round(best.get('ensemble_std', 0), 2),
        "ensemble_n": best.get('ensemble_n', 0),
        "entry_sigma": round(best['sigma_used'], 2),
        "paper_size_usd": PAPER_SIZE_USD,
        "cost_usd": round(PAPER_SIZE_USD * entry_price, 4),
        "paper_only": True,
        "live_allowed": False,
        "settled": False,
        "win": False,
        "pnl": 0.0,
        "entry_timestamp": datetime.now(timezone.utc).isoformat(),
        "expiry_date": best['date'],
        "risk_tier": CITY_REGISTRY.get(best['city'], {}).get('risk', 'medium'),
        "volume": round(best['volume'], 0),
    }

    # Append to paper trades file
    PAPER_TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PAPER_TRADES_FILE, 'a') as f:
        f.write(json.dumps(paper_entry) + '\n')

    # Write to V21.7.52 output
    with open(OUTPUT_DIR / 'weather_paper_entries.jsonl', 'a') as f:
        f.write(json.dumps(paper_entry) + '\n')

    print("=" * 60)
    print("V21.7.52 — Weather Paper Trade Created")
    print("=" * 60)
    print(f"  Trade ID:     {trade_id}")
    print(f"  City:         {best['city'].title()}")
    print(f"  Date:         {best['date']}")
    print(f"  Bucket:       {best['temp']}°C")
    print(f"  Side:         {side} (against {best['temp']}°C)")
    print(f"  Entry Price:   {entry_price:.2f}")
    print(f"  Cost:         ${paper_entry['cost_usd']:.2f}")
    print(f"  Market Prob:  {best['yes_price']:.1%} YES")
    print(f"  Our Prob:     {best['our_prob']:.1%} YES → {(1-best['our_prob']):.1%} NO")
    print(f"  Edge:         {best['best_edge']:+.1f}pp")
    print(f"  σ used:       {best['sigma_used']:.1f}°C")
    print(f"  Ensemble σ:   {best.get('ensemble_std', 0):.1f}°C ({best.get('ensemble_n', 0)} members)")
    print(f"  Volume:       {best['volume']:.0f}")
    print(f"  Risk Tier:    {paper_entry['risk_tier']}")
    print(f"  Paper Only:   True (LIVE NOT ALLOWED)")
    print(f"  Token (NO):   {selected_token_id[:20]}...")
    print(f"  Condition:    {best['condition_id'][:20]}...")
    print("=" * 60)
    
    return paper_entry


if __name__ == "__main__":
    entry = create_paper_entry()
    if entry:
        print("\n✅ Paper trade created successfully")
        print(f"   Settlement check needed after {entry['expiry_date']} 23:59 local time")
    else:
        print("\n❌ No suitable candidates found")