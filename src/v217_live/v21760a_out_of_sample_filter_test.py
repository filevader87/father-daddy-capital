#!/usr/bin/env python3
"""V21.7.60a Out-of-Sample Filter Test — test V21.7.59 filters on held-out data."""
import json, os, math
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict, Counter
import random

BASE = Path(__file__).resolve().parent.parent.parent
OUT = BASE / "output/v21760_out_of_sample_and_pmxt"
OUT.mkdir(parents=True, exist_ok=True)

def load_jsonl(p):
    if not os.path.exists(str(p)): return []
    with open(str(p)) as f: return [json.loads(l) for l in f if l.strip()]

def safe_f(v, d=0.0):
    try: return float(v) if v is not None else d
    except: return d

def write_json(path, data):
    with open(str(path), "w") as f: json.dump(data, f, indent=2, default=str)

NOW = datetime.now(timezone.utc).isoformat()

# Load canonical entries and outcomes
canonical = load_jsonl(BASE / "output/v21757_full_entry_scalp_survival/canonical_entry_universe.jsonl")
outcomes_raw = load_jsonl(BASE / "output/v21757_full_entry_scalp_survival/final_outcome_classification.jsonl")
pnl_raw = load_jsonl(BASE / "output/v21757_full_entry_scalp_survival/actual_strategy_pnl.jsonl")
outcomes = {r["position_id"]: r["final_strategy_outcome"] for r in outcomes_raw}
pnls = {r["position_id"]: r.get("strategy_pnl") for r in pnl_raw}

# Also load 1s observer data for additional out-of-sample testing
QUOTE_FILE = BASE / "output/v21751_persistent_1s_observer/quote_state_1s.jsonl"

# ── Split data into train (60%) and test (40%) by timestamp ────────────
# Sort by entry_timestamp
entries_with_ts = [(e.get("entry_timestamp", ""), e) for e in canonical]
entries_with_ts.sort(key=lambda x: x[0])
total = len(entries_with_ts)
split_idx = int(total * 0.6)
train = [e for _, e in entries_with_ts[:split_idx]]
test = [e for _, e in entries_with_ts[split_idx:]]

print(f"Out-of-sample split: {len(train)} train / {len(test)} test")

# ── Filters to test (from V21.7.59 contrast report) ───────────────────
filters = {
    "bucket_30_60c": lambda e: e.get("actual_bucket", "") in ("30-40c", "40-50c", "50-60c"),
    "spread_le_3c": lambda e: safe_f(e.get("entry_spread")) <= 0.03,
    "spread_le_2c": lambda e: safe_f(e.get("entry_spread")) <= 0.02,
    "TTE_30_to_180": lambda e: 30 <= safe_f(e.get("time_to_expiry_at_entry")) <= 180,
    "not_XRP": lambda e: e.get("asset") != "XRP",
    "bucket_30_60c_and_spread_le_3c": lambda e: e.get("actual_bucket", "") in ("30-40c", "40-50c", "50-60c") and safe_f(e.get("entry_spread")) <= 0.03,
    "bucket_30_60c_and_not_XRP": lambda e: e.get("actual_bucket", "") in ("30-40c", "40-50c", "50-60c") and e.get("asset") != "XRP",
    "bucket_30_60c_and_TTE_30_180": lambda e: e.get("actual_bucket", "") in ("30-40c", "40-50c", "50-60c") and 30 <= safe_f(e.get("time_to_expiry_at_entry")) <= 180,
}

# ── Test each filter on train set first (in-sample) ────────────────────
train_results = {}
for fname, ffunc in filters.items():
    train_passed = [e for e in train if ffunc(e)]
    train_scalps = sum(1 for e in train_passed if outcomes.get(e["position_id"]) == "SCALP_EXIT")
    train_losses = sum(1 for e in train_passed if outcomes.get(e["position_id"]) == "EXPIRY_LOSS")
    train_pnl = sum(safe_f(pnls.get(e["position_id"])) for e in train_passed)
    train_results[fname] = {
        "in_sample_passed": len(train_passed),
        "in_sample_scalps": train_scalps,
        "in_sample_losses": train_losses,
        "in_sample_pnl": round(train_pnl, 2),
        "in_sample_scalp_rate": round(train_scalps / len(train_passed), 4) if train_passed else 0
    }

# ── Test each filter on test set (out-of-sample) ──────────────────────
test_results = {}
for fname, ffunc in filters.items():
    test_passed = [e for e in test if ffunc(e)]
    test_scalps = sum(1 for e in test_passed if outcomes.get(e["position_id"]) == "SCALP_EXIT")
    test_losses = sum(1 for e in test_passed if outcomes.get(e["position_id"]) == "EXPIRY_LOSS")
    test_pnl = sum(safe_f(pnls.get(e["position_id"])) for e in test_passed)
    
    # Compute PF
    test_pnls_list = [safe_f(pnls.get(e["position_id"])) for e in test_passed]
    gross_profit = sum(p for p in test_pnls_list if p > 0)
    gross_loss = abs(sum(p for p in test_pnls_list if p < 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else float('inf') if gross_profit > 0 else 0
    
    # Max drawdown
    cumulative = 0; peak = 0; max_dd = 0
    for p in test_pnls_list:
        cumulative += p
        if cumulative > peak: peak = cumulative
        dd = peak - cumulative
        if dd > max_dd: max_dd = dd
    
    test_results[fname] = {
        "out_of_sample_passed": len(test_passed),
        "out_of_sample_scalps": test_scalps,
        "out_of_sample_losses": test_losses,
        "out_of_sample_pnl": round(test_pnl, 2),
        "out_of_sample_scalp_rate": round(test_scalps / len(test_passed), 4) if test_passed else 0,
        "out_of_sample_PF": round(pf, 4),
        "out_of_sample_max_DD": round(max_dd, 2),
        "positive_after_filter": test_pnl > 0,
        "edge_survives_out_of_sample": test_pnl > 0 and pf >= 1.0
    }

# ── Also test using 1s observer data as additional out-of-sample ──────
# Load a sample of 1s observer data for BTC 5m
btc_5m_quotes = []
count = 0
with open(str(QUOTE_FILE)) as f:
    for line in f:
        d = json.loads(line)
        if d.get("asset") != "BTC" or "5m" not in d.get("market_slug", ""):
            continue
        if not d.get("is_current_window") or not d.get("active"):
            continue
        bid = safe_f(d.get("best_bid")); ask = safe_f(d.get("best_ask"))
        if bid <= 0 or ask <= 0: continue
        count += 1
        if count % 1000 != 0: continue
        tte = safe_f(d.get("time_to_expiry_seconds"))
        spread = safe_f(d.get("spread"))
        token_price = bid if d.get("side") == "UP" else ask
        btc_5m_quotes.append({
            "timestamp": d.get("timestamp"), "side": d.get("side"),
            "token_price": token_price, "spread": spread, "tte": tte,
            "best_bid": bid, "best_ask": ask,
            "book_imbalance": safe_f(d.get("book_imbalance", 0)),
            "bid_depth": safe_f(d.get("bid_depth_top5", 0))
        })

# Simulate filter application on 1s observer data
# For each quote, check if it passes the bucket_30_60c filter and what the outcome would be
# We can't know the actual outcome (no settlement), but we can measure pass rates
observer_pass_rates = {}
for fname, ffunc in filters.items():
    # Adapt filter for quote data (no outcome, just pass rate)
    if "bucket" in fname:
        passed = sum(1 for q in btc_5m_quotes if 0.30 <= q["token_price"] <= 0.60)
    elif "spread" in fname:
        threshold = 0.03 if "3c" in fname else 0.02
        passed = sum(1 for q in btc_5m_quotes if q["spread"] <= threshold)
    elif "TTE" in fname:
        passed = sum(1 for q in btc_5m_quotes if 30 <= q["tte"] <= 180)
    elif "not_XRP" in fname:
        passed = len(btc_5m_quotes)  # all BTC
    else:
        # Combined filters
        passed = sum(1 for q in btc_5m_quotes 
                     if 0.30 <= q["token_price"] <= 0.60 and q["spread"] <= 0.03)
    observer_pass_rates[fname] = {
        "observer_total": len(btc_5m_quotes),
        "observer_passed": passed,
        "observer_pass_rate": round(passed / len(btc_5m_quotes), 4) if btc_5m_quotes else 0
    }

# ── Generate report ───────────────────────────────────────────────────
report = {
    "module": "V21.7.60a",
    "timestamp": NOW,
    "split": {"train_size": len(train), "test_size": len(test), "split_ratio": 0.6},
    "in_sample_results": train_results,
    "out_of_sample_results": test_results,
    "observer_pass_rates": observer_pass_rates,
    "filters_that_survive_out_of_sample": [
        fname for fname, r in test_results.items() if r["edge_survives_out_of_sample"]
    ],
    "filters_that_fail_out_of_sample": [
        fname for fname, r in test_results.items() if not r["edge_survives_out_of_sample"]
    ],
    "real_orders_allowed": False,
    "live_authorization_suspended": True,
    "classification": "OUT_OF_SAMPLE_TEST_COMPLETE"
}

write_json(OUT / "out_of_sample_filter_test_report.json", report)

# Print summary
print(f"\n{'='*60}")
print("OUT-OF-SAMPLE FILTER TEST RESULTS")
print(f"{'='*60}")
print(f"\nTrain: {len(train)} | Test: {len(test)}")
print(f"\n{'Filter':<35} {'OOS PnL':>10} {'OOS PF':>8} {'OOS Scalp%':>12} {'Survives':>10}")
print("-" * 80)
for fname, r in test_results.items():
    survives = "YES" if r["edge_survives_out_of_sample"] else "NO"
    print(f"{fname:<35} ${r['out_of_sample_pnl']:>8.2f} {r['out_of_sample_PF']:>8.4f} {r['out_of_sample_scalp_rate']:>11.2%} {survives:>10}")
print(f"\nFilters surviving OOS: {report['filters_that_survive_out_of_sample']}")
print(f"Filters failing OOS: {report['filters_that_fail_out_of_sample']}")
print(f"\nOutput: out_of_sample_filter_test_report.json")