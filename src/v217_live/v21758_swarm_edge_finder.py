#!/usr/bin/env python3
"""V21.7.58 Swarm Edge Finder — map edge across all assets/intervals/sides."""
import json, os
from pathlib import Path
from collections import defaultdict

BASE = Path(__file__).resolve().parent.parent.parent
OUT = BASE / "output/v21758_edge_discovery_capital_framework"
OUT.mkdir(parents=True, exist_ok=True)

QUOTE_FILE = BASE / "output/v21751_persistent_1s_observer/quote_state_1s.jsonl"

def safe_f(v, d=0.0):
    try: return float(v) if v is not None else d
    except: return d

def classify_bucket(p):
    p = safe_f(p)
    if p < 0.03: return "0-3c"
    if p < 0.08: return "3-8c"
    if p < 0.12: return "8-12c"
    if p < 0.20: return "12-20c"
    if p < 0.30: return "20-30c"
    if p < 0.60: return "30-60c"
    if p < 0.85: return "60-85c"
    return "85-95c"

def classify_tte(tte):
    t = safe_f(tte)
    if t <= 30: return "0-30s"
    if t <= 60: return "31-60s"
    if t <= 120: return "61-120s"
    if t <= 180: return "121-180s"
    if t <= 240: return "181-240s"
    return "241s+"

# Sample quotes across all assets
events = []
seen = set()
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
        if count % 200 != 0:  # sample every 200th
            continue
        
        slug = d.get("market_slug", "")
        asset = d.get("asset", "?")
        side = d.get("side", "?")
        interval = "15m" if "15m" in slug else "5m"
        
        key = f"{slug}_{side}"
        if key in seen:
            continue
        seen.add(key)
        
        spread = safe_f(d.get("spread"))
        bid_depth = safe_f(d.get("bid_depth_top5", 0))
        ask_depth = safe_f(d.get("ask_depth_top5", 0))
        book_imbalance = safe_f(d.get("book_imbalance", 0))
        token_price = bid if side == "UP" else ask
        
        # Classify
        if spread > 0.05:
            cls = "BLOCKED_BY_SLIPPAGE"
        elif bid_depth < 50:
            cls = "BLOCKED_BY_LIQUIDITY"
        else:
            cls = "PAPER_ONLY_CANDIDATE"
        
        events.append({
            "asset": asset,
            "interval": interval,
            "side": side,
            "market_slug": slug,
            "best_bid": bid,
            "best_ask": ask,
            "spread": spread,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "book_imbalance": book_imbalance,
            "token_price": token_price,
            "price_bucket": classify_bucket(token_price),
            "tte": tte,
            "tte_band": classify_tte(tte),
            "scalp_exit_possible": bid >= token_price + 0.03,
            "classification": cls
        })

# Write events
with open(OUT / "swarm_edge_events.jsonl", "w") as f:
    for r in events:
        f.write(json.dumps(r, default=str) + "\n")

# Build edge surface
surface = defaultdict(lambda: {"count": 0, "scalp_possible": 0, "blocked_spread": 0, "blocked_liquidity": 0})
for e in events:
    key = f"{e['asset']}_{e['interval']}_{e['side']}_{e['price_bucket']}_{e['tte_band']}"
    surface[key]["count"] += 1
    if e["scalp_exit_possible"]:
        surface[key]["scalp_possible"] += 1
    if e["classification"] == "BLOCKED_BY_SLIPPAGE":
        surface[key]["blocked_spread"] += 1
    if e["classification"] == "BLOCKED_BY_LIQUIDITY":
        surface[key]["blocked_liquidity"] += 1

surface_json = {k: v for k, v in sorted(surface.items(), key=lambda x: -x[1]["count"])}
with open(OUT / "swarm_edge_surface.json", "w") as f:
    json.dump(surface_json, f, indent=2)

# Candidate ranking
rankings = []
for key, data in surface.items():
    parts = key.split("_")
    asset = parts[0] if len(parts) > 0 else "?"
    interval = parts[1] if len(parts) > 1 else "?"
    side = parts[2] if len(parts) > 2 else "?"
    bucket = parts[3] if len(parts) > 3 else "?"
    tte_band = parts[4] if len(parts) > 4 else "?"
    
    scalp_rate = data["scalp_possible"] / data["count"] if data["count"] > 0 else 0
    
    if data["count"] >= 10 and scalp_rate > 0.3:
        cls = "EDGE_CANDIDATE_NEEDS_SAMPLE"
    elif data["count"] >= 10 and scalp_rate > 0.5:
        cls = "EDGE_CANDIDATE_POSITIVE"
    elif data["blocked_spread"] > data["count"] * 0.5:
        cls = "BLOCKED_BY_SLIPPAGE"
    elif data["blocked_liquidity"] > data["count"] * 0.5:
        cls = "BLOCKED_BY_LIQUIDITY"
    elif data["count"] < 5:
        cls = "REJECTED_FALSE_EDGE"
    else:
        cls = "NO_EDGE"
    
    rankings.append({
        "asset": asset,
        "interval": interval,
        "side": side,
        "entry_bucket": bucket,
        "TTE_band": tte_band,
        "closed_entries": 0,
        "WR": 0,
        "net_PnL": 0,
        "PF": 0,
        "max_DD": 0,
        "slippage_status": "unknown",
        "depth_status": "unknown",
        "settlement_status": "unknown",
        "classification": cls
    })

rankings.sort(key=lambda x: x["classification"])
with open(OUT / "swarm_candidate_ranking.json", "w") as f:
    json.dump(rankings, f, indent=2)

print(f"Swarm edge finder: {len(events)} events, {len(surface)} surface cells")
print(f"Classifications: {dict(defaultdict(int, ((r['classification'], 0) for r in rankings)))}")
print("OUTPUTS: swarm_edge_events.jsonl, swarm_edge_surface.json, swarm_candidate_ranking.json")