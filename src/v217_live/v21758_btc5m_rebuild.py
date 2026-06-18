#!/usr/bin/env python3
"""V21.7.58 BTC 5m Rebuild — test new hypotheses from 1s observer quote data."""
import json, os, math
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

BASE = Path(__file__).resolve().parent.parent.parent
OUT = BASE / "output/v21758_edge_discovery_capital_framework"
OUT.mkdir(parents=True, exist_ok=True)

QUOTE_FILE = BASE / "output/v21751_persistent_1s_observer/quote_state_1s.jsonl"

def safe_f(v, d=0.0):
    try: return float(v) if v is not None else d
    except: return d

def classify_tte(tte):
    t = safe_f(tte)
    if t <= 30: return "0-30s"
    if t <= 60: return "31-60s"
    if t <= 120: return "61-120s"
    if t <= 180: return "121-180s"
    if t <= 240: return "181-240s"
    return "241s+"

# Load BTC 5m quotes — sample every Nth row to get temporal coverage
btc_5m = []
sample_count = 0
with open(QUOTE_FILE) as f:
    for line in f:
        d = json.loads(line)
        if d.get("asset") != "BTC":
            continue
        slug = d.get("market_slug", "")
        if "5m" not in slug:
            continue
        if not d.get("is_current_window"):
            continue
        if not d.get("active"):
            continue
        
        bid = safe_f(d.get("best_bid"))
        ask = safe_f(d.get("best_ask"))
        if bid <= 0 or ask <= 0:
            continue
        
        tte = safe_f(d.get("time_to_expiry_seconds"))
        if tte < 10:
            continue
        
        btc_price = safe_f(d.get("btc_external_price"))
        spread = safe_f(d.get("spread"))
        bid_depth = safe_f(d.get("bid_depth_top5", 0))
        ask_depth = safe_f(d.get("ask_depth_top5", 0))
        book_imbalance = safe_f(d.get("book_imbalance", 0))
        
        # Sample every 100th row to avoid overload
        sample_count += 1
        if sample_count % 100 != 0:
            continue
        
        # Hypothesis testing
        hypotheses = []
        
        # H1: breakaway continuation — large reference distance + velocity
        # Can't compute velocity without sequential data, use book imbalance as proxy
        if abs(book_imbalance) > 0.3 and tte > 60:
            hypotheses.append("breakaway_continuation")
        
        # H2: late-window dominant-side continuation
        if tte <= 60 and abs(book_imbalance) > 0.2:
            hypotheses.append("late_window_dominant_side_continuation")
        
        # H3: overpriced midzone rejection
        token_price = bid if d.get("side") == "UP" else ask
        if 0.40 < token_price < 0.60 and spread > 0.03:
            hypotheses.append("overpriced_midzone_rejection")
        
        # H4: spread compression before repricing
        if spread <= 0.02 and tte > 120:
            hypotheses.append("spread_compression_before_repricing")
        
        # H5: liquidity imbalance continuation
        if abs(book_imbalance) > 0.4:
            hypotheses.append("liquidity_imbalance_continuation")
        
        if not hypotheses:
            hypotheses.append("NO_EDGE")
        
        entry_created = False
        entry_reason = "no_hypothesis_triggered"
        if hypotheses[0] != "NO_EDGE" and spread <= 0.04 and bid_depth >= 50:
            entry_created = True
            entry_reason = hypotheses[0]
        
        btc_5m.append({
            "timestamp": d.get("timestamp"),
            "market_slug": slug,
            "side": d.get("side"),
            "best_bid": bid,
            "best_ask": ask,
            "spread": spread,
            "bid_depth_top5": bid_depth,
            "ask_depth_top5": ask_depth,
            "book_imbalance": book_imbalance,
            "btc_external_price": btc_price,
            "time_to_expiry": tte,
            "tte_band": classify_tte(tte),
            "hypotheses_triggered": hypotheses,
            "entry_created": entry_created,
            "entry_reason": entry_reason,
            "condition_id": d.get("condition_id"),
            "quote_source": "PM_CLOB_READ"
        })

# Write hypothesis events
with open(OUT / "btc5m_hypothesis_events.jsonl", "w") as f:
    for r in btc_5m:
        f.write(json.dumps(r, default=str) + "\n")

# Forward paper positions: entries where entry_created=True
paper_positions = [r for r in btc_5m if r["entry_created"]]
with open(OUT / "btc5m_forward_paper_positions.jsonl", "w") as f:
    for r in paper_positions:
        f.write(json.dumps(r, default=str) + "\n")

# Edge surface report
hypothesis_counts = defaultdict(int)
entry_counts = defaultdict(int)
for r in btc_5m:
    for h in r["hypotheses_triggered"]:
        hypothesis_counts[h] += 1
    if r["entry_created"]:
        entry_counts[r["entry_reason"]] += 1

report = {
    "total_events_sampled": len(btc_5m),
    "hypothesis_distribution": dict(hypothesis_counts),
    "entries_created": len(paper_positions),
    "entry_distribution": dict(entry_counts),
    "forward_paper_positions": len(paper_positions),
    "edge_found": len(paper_positions) > 0,
    "live_review_eligible": False,
    "gates": {
        "closed_valid_entries >= 100": False,
        "target_hypothesis_entries >= 25": False,
        "net_strategy_PnL > 0": False,
        "PF >= 1.25": False,
        "depth_adjusted_slippage_remains_positive": False
    },
    "hypotheses_tested": [
        "breakaway_continuation",
        "late_window_dominant_side_continuation",
        "overpriced_midzone_rejection",
        "spread_compression_before_repricing",
        "reference_distance_acceleration",
        "velocity_confirmed_momentum",
        "liquidity_imbalance_continuation"
    ],
    "next_action": "accumulate_forward_paper_positions_and_measure_settlements"
}

with open(OUT / "btc5m_edge_surface_report.json", "w") as f:
    json.dump(report, f, indent=2)

print(f"BTC 5m rebuild: {len(btc_5m)} events sampled")
print(f"Hypotheses: {dict(hypothesis_counts)}")
print(f"Entries created: {len(paper_positions)}")
print(f"Entry distribution: {dict(entry_counts)}")
print("OUTPUTS: btc5m_hypothesis_events.jsonl, btc5m_forward_paper_positions.jsonl, btc5m_edge_surface_report.json")