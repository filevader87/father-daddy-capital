#!/usr/bin/env python3
"""V21.7.58 Breakaway Physics — test present-window price displacement from strike."""
import json, os, math
from pathlib import Path
from collections import defaultdict

BASE = Path(__file__).resolve().parent.parent.parent
OUT = BASE / "output/v21758_edge_discovery_capital_framework"
OUT.mkdir(parents=True, exist_ok=True)

QUOTE_FILE = BASE / "output/v21751_persistent_1s_observer/quote_state_1s.jsonl"

def safe_f(v, d=0.0):
    try: return float(v) if v is not None else d
    except: return d

# Load quote data and compute breakaway physics
events = []
# Track price history per asset for velocity computation
price_history = defaultdict(list)  # asset -> list of (timestamp_str, btc_price)
count = 0

with open(QUOTE_FILE) as f:
    for line in f:
        d = json.loads(line)
        if not d.get("is_current_window") or not d.get("active"):
            continue
        bid = safe_f(d.get("best_bid"))
        ask = safe_f(d.get("best_ask"))
        if bid <= 0 or ask <= 0:
            continue
        tte = safe_f(d.get("time_to_expiry_seconds"))
        if tte < 10:
            continue
        
        count += 1
        if count % 500 != 0:  # sample heavily
            continue
        
        asset = d.get("asset", "?")
        side = d.get("side", "?")
        slug = d.get("market_slug", "")
        interval = "15m" if "15m" in slug else "5m"
        btc_price = safe_f(d.get("btc_external_price"))
        ts = d.get("timestamp", "")
        
        # Track price history for velocity
        price_history[asset].append((ts, btc_price))
        
        # Compute velocity if we have enough history
        vel_5s = vel_15s = vel_30s = vel_60s = 0.0
        hist = price_history[asset]
        if len(hist) >= 2:
            latest_price = hist[-1][1]
            for lookback in [5, 15, 30, 60]:
                if len(hist) > lookback:
                    old_price = hist[-1 - lookback][1]
                    vel = (latest_price - old_price) / old_price * 100 if old_price > 0 else 0
                    if lookback == 5: vel_5s = vel
                    elif lookback == 15: vel_15s = vel
                    elif lookback == 30: vel_30s = vel
                    elif lookback == 60: vel_60s = vel
        
        spread = safe_f(d.get("spread"))
        bid_depth = safe_f(d.get("bid_depth_top5", 0))
        ask_depth = safe_f(d.get("ask_depth_top5", 0))
        book_imbalance = safe_f(d.get("book_imbalance", 0))
        token_price = bid if side == "UP" else ask
        
        # Reference distance: how far is token price from 50¢ (the "strike" for binary)
        distance_from_strike_pct = abs(token_price - 0.5) * 100
        distance_from_strike_bps = distance_from_strike_pct * 100
        
        # Trend confirmation
        trend_confirmation = False
        if side == "UP" and vel_30s > 0.01:
            trend_confirmation = True
        if side == "DOWN" and vel_30s < -0.01:
            trend_confirmation = True
        
        # Estimated true probability (simplified: based on token price + velocity)
        market_prob = token_price
        estimated_true_prob = market_prob
        if trend_confirmation:
            estimated_true_prob = min(0.95, market_prob + abs(vel_30s) * 0.5)
        
        spread_adjusted_ev = (estimated_true_prob - token_price) - spread
        depth_adjusted_ev = spread_adjusted_ev - (0.01 if bid_depth < 100 else 0)
        
        # Breakaway candidate
        is_candidate = (
            distance_from_strike_pct > 10 and
            trend_confirmation and
            token_price < estimated_true_prob and
            spread <= 0.04 and
            bid_depth >= 50
        )
        
        events.append({
            "asset": asset,
            "interval": interval,
            "side": side,
            "market_slug": slug,
            "reference_price": btc_price,
            "token_price": token_price,
            "distance_from_strike_pct": round(distance_from_strike_pct, 4),
            "distance_from_strike_bps": round(distance_from_strike_bps, 2),
            "time_to_expiry": tte,
            "reference_velocity_5s": round(vel_5s, 4),
            "reference_velocity_15s": round(vel_15s, 4),
            "reference_velocity_30s": round(vel_30s, 4),
            "reference_velocity_60s": round(vel_60s, 4),
            "trend_confirmation": trend_confirmation,
            "market_probability": market_prob,
            "estimated_true_probability": round(estimated_true_prob, 4),
            "spread": spread,
            "spread_adjusted_EV": round(spread_adjusted_ev, 4),
            "depth_adjusted_EV": round(depth_adjusted_ev, 4),
            "bid_depth": bid_depth,
            "book_imbalance": book_imbalance,
            "breakaway_candidate": is_candidate
        })

# Write events
with open(OUT / "breakaway_physics_events.jsonl", "w") as f:
    for r in events:
        f.write(json.dumps(r, default=str) + "\n")

# Probability surface
surface = defaultdict(lambda: {"count": 0, "candidates": 0, "avg_ev": 0.0})
for e in events:
    key = f"{e['asset']}_{e['interval']}_{e['side']}_{e['distance_from_strike_pct']:.0f}pct"
    surface[key]["count"] += 1
    if e["breakaway_candidate"]:
        surface[key]["candidates"] += 1
    surface[key]["avg_ev"] += e["depth_adjusted_EV"]

for k in surface:
    if surface[k]["count"] > 0:
        surface[k]["avg_ev"] = round(surface[k]["avg_ev"] / surface[k]["count"], 4)

with open(OUT / "breakaway_probability_surface.json", "w") as f:
    json.dump(dict(surface), f, indent=2)

# Candidate report
candidates = [e for e in events if e["breakaway_candidate"]]
report = {
    "total_events": len(events),
    "breakaway_candidates": len(candidates),
    "candidate_rate": round(len(candidates) / len(events), 4) if events else 0,
    "by_asset": dict(defaultdict(int, ((e["asset"], 0) for e in candidates))),
    "note": "Breakaway candidates use present-window physics only. No Markov sequence memory.",
    "next_action": "accumulate_candidates_and_test_forward_paper"
}
with open(OUT / "breakaway_candidate_report.json", "w") as f:
    json.dump(report, f, indent=2, default=str)

print(f"Breakaway physics: {len(events)} events, {len(candidates)} candidates")
print(f"Candidate rate: {report['candidate_rate']:.2%}")
print("OUTPUTS: breakaway_physics_events.jsonl, breakaway_probability_surface.json, breakaway_candidate_report.json")