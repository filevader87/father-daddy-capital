#!/usr/bin/env python3
"""V21.7.58 Failed-Entry Autopsy — analyze all failed entries from V21.7.55/56/57."""
import json, os
from pathlib import Path
from collections import defaultdict, Counter

BASE = Path(__file__).resolve().parent.parent.parent
OUT = BASE / "output/v21758_edge_discovery_capital_framework"
OUT.mkdir(parents=True, exist_ok=True)

def load_jsonl(p):
    if not os.path.exists(p): return []
    with open(p) as f: return [json.loads(l) for l in f if l.strip()]

def safe_f(v, d=0.0):
    try: return float(v) if v is not None else d
    except: return d

def classify_bucket(p):
    p = safe_f(p)
    if p < 0.10: return "0-10c"
    if p < 0.20: return "10-20c"
    if p < 0.30: return "20-30c"
    if p < 0.40: return "30-40c"
    if p < 0.50: return "40-50c"
    if p < 0.60: return "50-60c"
    if p < 0.70: return "60-70c"
    if p < 0.80: return "70-80c"
    if p < 0.90: return "80-90c"
    return "90-100c"

# Load data
canonical = load_jsonl(str(BASE / "output/v21757_full_entry_scalp_survival/canonical_entry_universe.jsonl"))
outcomes = {r["position_id"]: r["final_strategy_outcome"] for r in load_jsonl(str(BASE / "output/v21757_full_entry_scalp_survival/final_outcome_classification.jsonl"))}
pnls = {r["position_id"]: r.get("strategy_pnl") for r in load_jsonl(str(BASE / "output/v21757_full_entry_scalp_survival/actual_strategy_pnl.jsonl"))}

# Failed entries: not SCALP_EXIT and not EXPIRY_WIN
failed = []
for e in canonical:
    pid = e["position_id"]
    outcome = outcomes.get(pid, "UNKNOWN")
    if outcome in ("SCALP_EXIT", "EXPIRY_WIN"):
        continue
    entry_price = safe_f(e.get("entry_price"))
    max_bid = safe_f(e.get("max_bid_after_entry"))
    min_bid = safe_f(e.get("min_bid_after_entry"), max_bid)
    spread = safe_f(e.get("entry_spread"))
    tte = safe_f(e.get("time_to_expiry_at_entry"))
    pnl = pnls.get(pid)

    # Classify loss_type
    if outcome == "OPEN_UNRESOLVED":
        loss_type = "BID_LIQUIDITY_MISSING"
    elif max_bid < entry_price:
        loss_type = "EXPIRY_LOSS_NO_REPRICE"
    elif max_bid >= entry_price + 0.03:
        loss_type = "EXPIRY_LOSS_REPRICED_BUT_NOT_EXITED"
    elif spread > 0.05:
        loss_type = "SPREAD_TOO_WIDE"
    elif tte <= 30:
        loss_type = "TTE_TOO_SHORT"
    else:
        loss_type = "EXPIRY_LOSS_NO_REPRICE"

    # Classify avoidable
    if loss_type == "TTE_TOO_SHORT":
        avoidable = "TTE_FILTER"
    elif loss_type == "SPREAD_TOO_WIDE":
        avoidable = "SPREAD_FILTER"
    elif loss_type == "EXPIRY_LOSS_REPRICED_BUT_NOT_EXITED":
        avoidable = "FASTER_EXIT"
    elif entry_price >= 0.60:
        avoidable = "BUCKET_FILTER"
    elif loss_type == "BID_LIQUIDITY_MISSING":
        avoidable = "NOT_AVOIDABLE"
    else:
        avoidable = "NOT_AVOIDABLE"

    failed.append({
        "position_id": pid,
        "asset": e.get("asset"),
        "side": e.get("side"),
        "interval": e.get("interval"),
        "cell_id": e.get("cell_id"),
        "entry_bucket": e.get("actual_bucket", classify_bucket(entry_price)),
        "entry_price": entry_price,
        "TTE_at_entry": tte,
        "spread": spread,
        "max_bid_after_entry": max_bid,
        "min_bid_after_entry": min_bid,
        "expiry_result": "LOSS" if outcome == "EXPIRY_LOSS" else outcome,
        "strategy_pnl": pnl,
        "loss_type": loss_type,
        "avoidable_by_filter": avoidable
    })

# Write autopsy
with open(OUT / "failed_entry_autopsy.jsonl", "w") as f:
    for r in failed:
        f.write(json.dumps(r, default=str) + "\n")

# Aggregate
loss_types = Counter(r["loss_type"] for r in failed)
avoidable = Counter(r["avoidable_by_filter"] for r in failed)
filter_pnl_impact = defaultdict(lambda: {"count": 0, "pnl_recoverable": 0.0})
for r in failed:
    if r["avoidable_by_filter"] != "NOT_AVOIDABLE":
        filter_pnl_impact[r["avoidable_by_filter"]]["count"] += 1
        filter_pnl_impact[r["avoidable_by_filter"]]["pnl_recoverable"] += safe_f(r.get("strategy_pnl"))

recs = {
    "total_failed_entries": len(failed),
    "loss_type_distribution": dict(loss_types),
    "avoidable_distribution": dict(avoidable),
    "filter_recommendations": {
        k: {"count": v["count"], "estimated_pnl_recoverable": round(v["pnl_recoverable"], 2)}
        for k, v in sorted(filter_pnl_impact.items(), key=lambda x: -x[1]["pnl_recoverable"])
    },
    "note": "Any proposed filter must be tested out-of-sample before promotion."
}

with open(OUT / "failed_entry_filter_recommendations.json", "w") as f:
    json.dump(recs, f, indent=2, default=str)

print(f"Failed entry autopsy: {len(failed)} entries")
print(f"Loss types: {dict(loss_types)}")
print(f"Avoidable: {dict(avoidable)}")
print(f"Filter recommendations written")
print("OUTPUTS: failed_entry_autopsy.jsonl, failed_entry_filter_recommendations.json")