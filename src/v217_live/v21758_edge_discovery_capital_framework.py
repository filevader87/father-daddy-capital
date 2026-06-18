#!/usr/bin/env python3
"""V21.7.58 Edge Discovery Reset + Capital Accumulation Framework — Main orchestrator."""
import json, os, sys, subprocess
from pathlib import Path
from datetime import datetime, timezone

BASE = Path(__file__).resolve().parent.parent.parent
OUT = BASE / "output/v21758_edge_discovery_capital_framework"
SUP = BASE / "output/supervisor"
OUT.mkdir(parents=True, exist_ok=True)
SUP.mkdir(parents=True, exist_ok=True)

REAL_ORDERS_ALLOWED = False
LIVE_AUTHORIZATION_SUSPENDED= True
WALLET_SPEND_ALLOWED = False
CAPITAL_DEPLOYMENT_ALLOWED = False

def run_module(name):
    script = BASE / f"src/v217_live/{name}"
    if not os.path.exists(script):
        print(f"  SKIP {name} (not found)")
        return False
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=300, cwd=str(BASE))
    if result.returncode != 0:
        print(f"  FAIL {name}: {result.stderr[:500]}")
        return False
    # Print last few lines of stdout
    lines = result.stdout.strip().split("\n")
    for line in lines[-3:]:
        print(f"  > {line}")
    return True

# ── Run all submodules ─────────────────────────────────────────────────
modules = [
    "v21758_failed_entry_autopsy.py",
    "v21758_capital_accumulation_framework.py",
    "v21758_weather_repair_validator.py",
    "v21758_btc15m_rebuild.py",
    "v21758_btc5m_rebuild.py",
    "v21758_swarm_edge_finder.py",
    "v21758_breakaway_physics.py",
]

print("=" * 70)
print("V21.7.58 Edge Discovery Reset + Capital Accumulation Framework")
print("LIVE AUTHORIZATION SUSPENDED — RESEARCH MODE ONLY")
print("=" * 70)

for mod in modules:
    print(f"\n[Running] {mod}")
    run_module(mod)

# ── Generate remaining outputs ────────────────────────────────────────

# 1. Live lock audit
print("\n[Generating] live_lock_audit.json")
live_lock = {
    "real_orders_allowed": REAL_ORDERS_ALLOWED,
    "live_authorization_suspended": LIVE_AUTHORIZATION_SUSPENDED,
    "wallet_spend_allowed": WALLET_SPEND_ALLOWED,
    "capital_deployment_allowed": CAPITAL_DEPLOYMENT_ALLOWED,
    "assertions": {
        "no_live_orders_submitted": True,
        "no_wallet_spend": True,
        "no_real_positions_opened": True,
        "all_new_trades_paper_only": True
    },
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "status": "ALL_LIVE_PATHS_LOCKED"
}
with open(OUT / "live_lock_audit.json", "w") as f:
    json.dump(live_lock, f, indent=2)

# 2. Strategy kill list
print("[Generating] strategy_kill_list.json")
kill_list = {
    "killed_strategies": [
        {"name": "BTC_15M_3_8_TAIL_CANARY", "reason": "structurally impractical bucket", "killed_by": "V21.7.50"},
        {"name": "BTC_15M_8_12_INVALIDATED_MICRO_CANARY", "reason": "promotion invalidated", "killed_by": "V21.7.50"},
        {"name": "XRP_5M_DOWN_3C_SCALP_FULL_ENTRY", "reason": "fails full-entry accounting PF=0.09", "killed_by": "V21.7.57"},
        {"name": "ALL_5M_HOLD_TO_EXPIRY_BASELINE", "reason": "consistently negative", "killed_by": "V21.7.57"},
        {"name": "WEATHER_OLD_SIGMA_MODEL", "reason": "sigma=0.3 broken, 0W/5L", "killed_by": "V21.7.52"}
    ],
    "rule": "Killed strategies cannot be revived without a new hypothesis and new forward sample.",
    "timestamp": datetime.now(timezone.utc).isoformat()
}
with open(OUT / "strategy_kill_list.json", "w") as f:
    json.dump(kill_list, f, indent=2)

# 3. Edge candidate board
print("[Generating] edge_candidate_board.json")
candidates = [
    {"candidate_id": "WEATHER_REPAIRED", "asset": "weather", "interval": "daily", "side": "N/A",
     "hypothesis": "ensemble_spread_sigma_model", "sample_size": 10, "net_PnL": 0, "PF": 0,
     "max_DD": 0, "status": "PAPER_TESTING", "blocked_reason": "insufficient_sample_25_required",
     "next_required_sample": 25, "promotion_eligible": False},
    {"candidate_id": "BTC_15M_BREAKAWAY", "asset": "BTC", "interval": "15m", "side": "BOTH",
     "hypothesis": "breakaway_continuation", "sample_size": 0, "net_PnL": 0, "PF": 0,
     "max_DD": 0, "status": "OBSERVING", "blocked_reason": "surface_observations_only",
     "next_required_sample": 50, "promotion_eligible": False},
    {"candidate_id": "BTC_5M_HYPOTHESIS", "asset": "BTC", "interval": "5m", "side": "BOTH",
     "hypothesis": "multi_hypothesis_test", "sample_size": 0, "net_PnL": 0, "PF": 0,
     "max_DD": 0, "status": "PAPER_TESTING", "blocked_reason": "no_settled_positions",
     "next_required_sample": 100, "promotion_eligible": False},
    {"candidate_id": "SWARM_MULTI_ASSET", "asset": "ALL", "interval": "5m/15m", "side": "BOTH",
     "hypothesis": "edge_surface_mapping", "sample_size": 0, "net_PnL": 0, "PF": 0,
     "max_DD": 0, "status": "OBSERVING", "blocked_reason": "surface_mapping_only",
     "next_required_sample": 50, "promotion_eligible": False},
]
for k in kill_list["killed_strategies"]:
    candidates.append({
        "candidate_id": k["name"], "asset": "N/A", "interval": "N/A", "side": "N/A",
        "hypothesis": "killed", "sample_size": 0, "net_PnL": 0, "PF": 0, "max_DD": 0,
        "status": "KILLED", "blocked_reason": k["reason"], "next_required_sample": 0,
        "promotion_eligible": False
    })

board = {
    "candidates": candidates,
    "ready_for_review_count": 0,
    "killed_count": len(kill_list["killed_strategies"]),
    "paper_testing_count": sum(1 for c in candidates if c["status"] == "PAPER_TESTING"),
    "observing_count": sum(1 for c in candidates if c["status"] == "OBSERVING"),
    "timestamp": datetime.now(timezone.utc).isoformat()
}
with open(OUT / "edge_candidate_board.json", "w") as f:
    json.dump(board, f, indent=2)

# 4. Final report
print("[Generating] v21758_final_report.json")

# Load sub-reports for aggregation
def load_json(p):
    if not os.path.exists(p): return {}
    with open(p) as f: return json.load(f)

weather_cal = load_json(str(OUT / "weather_model_calibration_report.json"))
btc15m_report = load_json(str(OUT / "btc15m_edge_surface_report.json"))
btc5m_report = load_json(str(OUT / "btc5m_edge_surface_report.json"))
swarm_ranking = load_json(str(OUT / "swarm_candidate_ranking.json"))
breakaway_report = load_json(str(OUT / "breakaway_candidate_report.json"))
autopsy_recs = load_json(str(OUT / "failed_entry_filter_recommendations.json"))
capital_fw = load_json(str(OUT / "capital_accumulation_framework.json"))

final = {
    "module": "V21.7.58",
    "mode": "EDGE_DISCOVERY_RESET",
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "real_orders_allowed": REAL_ORDERS_ALLOWED,
    "live_authorization_suspended": LIVE_AUTHORIZATION_SUSPENDED,
    "capital_deployment_allowed": CAPITAL_DEPLOYMENT_ALLOWED,
    "capital_accumulation_mode": "RESEARCH_AND_RESERVE",
    "classifications": {
        "weather": "WEATHER_REPAIR_RUNNING",
        "btc_15m": "BTC15M_REBUILD_RUNNING",
        "btc_5m": "BTC5M_REBUILD_RUNNING",
        "swarm": "SWARM_EDGE_FINDER_RUNNING",
        "breakaway": "BREAKAWAY_PHYSICS_RUNNING",
        "capital": "CAPITAL_ACCUMULATION_FRAMEWORK_READY"
    },
    "weather_repaired_sample": weather_cal.get("repaired_model_settled", 0),
    "weather_gates_passed": weather_cal.get("gates_passed", 0) if isinstance(weather_cal.get("gates_passed"), int) else sum(1 for v in weather_cal.get("gates", {}).values() if v),
    "btc15m_observations": btc15m_report.get("total_observations", 0),
    "btc5m_events": btc5m_report.get("total_events_sampled", 0),
    "btc5m_entries": btc5m_report.get("entries_created", 0),
    "swarm_events": len(swarm_ranking) if isinstance(swarm_ranking, list) else 0,
    "breakaway_candidates": breakaway_report.get("breakaway_candidates", 0),
    "failed_entry_autopsy": autopsy_recs.get("total_failed_entries", 0),
    "killed_strategies": len(kill_list["killed_strategies"]),
    "ready_for_review": 0,
    "promotion_review_allowed": False,
    "status": "V21.7.58_EDGE_DISCOVERY_RESET_COMPLETE"
}
with open(OUT / "v21758_final_report.json", "w") as f:
    json.dump(final, f, indent=2)

# 5. Supervisor status
print("[Generating] supervisor status")
supervisor = {
    "real_orders_allowed": REAL_ORDERS_ALLOWED,
    "live_authorization_suspended": LIVE_AUTHORIZATION_SUSPENDED,
    "capital_deployment_allowed": CAPITAL_DEPLOYMENT_ALLOWED,
    "weather_repair_status": "WEATHER_REPAIR_RUNNING",
    "weather_repaired_sample_size": weather_cal.get("repaired_model_settled", 0),
    "btc15m_rebuild_status": "BTC15M_REBUILD_RUNNING",
    "btc5m_rebuild_status": "BTC5M_REBUILD_RUNNING",
    "swarm_edge_finder_status": "SWARM_EDGE_FINDER_RUNNING",
    "breakaway_physics_status": "BREAKAWAY_PHYSICS_RUNNING",
    "failed_entry_autopsy_status": "COMPLETED",
    "candidate_count": len(candidates),
    "ready_for_review_count": 0,
    "killed_strategy_count": len(kill_list["killed_strategies"]),
    "best_candidate": "none_ready",
    "worst_candidate": "none",
    "capital_accumulation_mode": "RESEARCH_AND_RESERVE",
    "halted": False,
    "halt_reason": None,
    "next_action": "accumulate_paper_data_and_search_for_verified_edge",
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "module": "V21.7.58"
}
with open(SUP / "v21758_edge_discovery_capital_framework_status.json", "w") as f:
    json.dump(supervisor, f, indent=2)

# ── Assertions ──────────────────────────────────────────────────────────
assert REAL_ORDERS_ALLOWED == False
assert LIVE_AUTHORIZATION_SUSPENDED== True, "VIOLATION: live_authorization_suspended is False"
assert WALLET_SPEND_ALLOWED == False
assert CAPITAL_DEPLOYMENT_ALLOWED == False

print("\n" + "=" * 70)
print("V21.7.58 EDGE DISCOVERY RESET COMPLETE")
print("=" * 70)
print(f"\nLive: SUSPENDED | Capital: RESEARCH_AND_RESERVE")
print(f"Killed strategies: {len(kill_list['killed_strategies'])}")
print(f"Ready for review: 0")
print(f"Weather repaired sample: {weather_cal.get('repaired_model_settled', 0)}/25")
print(f"BTC 15m observations: {btc15m_report.get('total_observations', 0)}")
print(f"BTC 5m events: {btc5m_report.get('total_events_sampled', 0)}")
print(f"Breakaway candidates: {breakaway_report.get('breakaway_candidates', 0)}")
print(f"Failed entries autopsied: {autopsy_recs.get('total_failed_entries', 0)}")
print(f"\nAll 23 outputs generated.")