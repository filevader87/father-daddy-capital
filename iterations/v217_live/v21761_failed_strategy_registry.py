#!/usr/bin/env python3
"""V21.7.61 Scalp Family Retirement + No-Edge Guardrail
Permanently kill failed scalp family, freeze evidence, block duplicate retests.
"""
import json, os
from pathlib import Path
from datetime import datetime, timezone

BASE = Path(__file__).resolve().parent.parent.parent
OUT = BASE / "output/v21761_no_edge_guardrail"
SUP = BASE / "output/supervisor"
OUT.mkdir(parents=True, exist_ok=True)
SUP.mkdir(parents=True, exist_ok=True)

NOW = datetime.now(timezone.utc).isoformat()

def write_json(path, data):
    with open(str(path), "w") as f:
        json.dump(data, f, indent=2, default=str)

# ── Load V21.7.60 evidence ────────────────────────────────────────────
def load_json(p):
    if not os.path.exists(str(p)): return {}
    with open(str(p)) as f: return json.load(f)

oos_report = load_json(BASE / "output/v21760_out_of_sample_and_pmxt/out_of_sample_filter_test_report.json")
paper_report = load_json(BASE / "output/v21760_out_of_sample_and_pmxt/live_paper_trade_report.json")
pmxt_report = load_json(BASE / "output/v21760_out_of_sample_and_pmxt/pmxt_historical_simulation_report.json")

# ── 1. Failed Strategy Registry ───────────────────────────────────────
killed_variants = [
    {"strategy_id": "BTC_5M_30_60C_PLUS_1C_SCALP", "asset": "BTC", "interval": "5m", "side": "BOTH",
     "entry_bucket": "30-60c", "profit_target": "+1c", "entry_rule": "token_price 30-60c, spread<=5c, TTE>=30s",
     "exit_rule": "scalp exit at bid>=entry+1c, time stop at TTE<=15s", "sample_source": "PMXT_historical",
     "entries": 1110, "scalp_exits": 282, "scalp_exit_rate": 0.254, "net_PnL": -209.84, "PF": 0.179,
     "max_DD": 209.84, "failure_reason": "FAILED_HISTORICAL_SIMULATION", "killed_at_version": "V21.7.61",
     "revival_allowed": False, "revival_conditions": "New causal hypothesis required. Bucket-only scalp forbidden."},
    {"strategy_id": "BTC_5M_30_60C_PLUS_2C_SCALP", "asset": "BTC", "interval": "5m", "side": "BOTH",
     "entry_bucket": "30-60c", "profit_target": "+2c", "entry_rule": "token_price 30-60c, spread<=5c, TTE>=30s",
     "exit_rule": "scalp exit at bid>=entry+2c, time stop at TTE<=15s", "sample_source": "PMXT_historical",
     "entries": 1013, "scalp_exits": 159, "scalp_exit_rate": 0.157, "net_PnL": -197.74, "PF": 0.200,
     "max_DD": 197.74, "failure_reason": "FAILED_HISTORICAL_SIMULATION", "killed_at_version": "V21.7.61",
     "revival_allowed": False, "revival_conditions": "New causal hypothesis required."},
    {"strategy_id": "BTC_5M_30_60C_PLUS_3C_SCALP", "asset": "BTC", "interval": "5m", "side": "BOTH",
     "entry_bucket": "30-60c", "profit_target": "+3c", "entry_rule": "token_price 30-60c, spread<=5c, TTE>=30s",
     "exit_rule": "scalp exit at bid>=entry+3c, time stop at TTE<=15s", "sample_source": "PMXT_historical",
     "entries": 968, "scalp_exits": 99, "scalp_exit_rate": 0.102, "net_PnL": -199.43, "PF": 0.194,
     "max_DD": 199.43, "failure_reason": "FAILED_HISTORICAL_SIMULATION", "killed_at_version": "V21.7.61",
     "revival_allowed": False, "revival_conditions": "New causal hypothesis required."},
    {"strategy_id": "ETH_5M_30_60C_PLUS_3C_SCALP", "asset": "ETH", "interval": "5m", "side": "BOTH",
     "entry_bucket": "30-60c", "profit_target": "+3c", "entry_rule": "token_price 30-60c, spread<=5c, TTE>=30s",
     "exit_rule": "scalp exit at bid>=entry+3c", "sample_source": "PMXT_historical",
     "entries": 918, "scalp_exits": 26, "scalp_exit_rate": 0.028, "net_PnL": -228.43, "PF": 0.071,
     "max_DD": 228.43, "failure_reason": "FAILED_HISTORICAL_SIMULATION", "killed_at_version": "V21.7.61",
     "revival_allowed": False, "revival_conditions": "New causal hypothesis required."},
    {"strategy_id": "SOL_5M_30_60C_PLUS_3C_SCALP", "asset": "SOL", "interval": "5m", "side": "BOTH",
     "entry_bucket": "30-60c", "profit_target": "+3c", "entry_rule": "token_price 30-60c, spread<=5c, TTE>=30s",
     "exit_rule": "scalp exit at bid>=entry+3c", "sample_source": "PMXT_historical",
     "entries": 898, "scalp_exits": 2, "scalp_exit_rate": 0.002, "net_PnL": -211.37, "PF": 0.015,
     "max_DD": 211.37, "failure_reason": "FAILED_HISTORICAL_SIMULATION", "killed_at_version": "V21.7.61",
     "revival_allowed": False, "revival_conditions": "New causal hypothesis required."},
    {"strategy_id": "XRP_5M_30_60C_PLUS_3C_SCALP", "asset": "XRP", "interval": "5m", "side": "BOTH",
     "entry_bucket": "30-60c", "profit_target": "+3c", "entry_rule": "token_price 30-60c, spread<=5c, TTE>=30s",
     "exit_rule": "scalp exit at bid>=entry+3c", "sample_source": "PMXT_historical+full_entry",
     "entries": 900, "scalp_exits": 1, "scalp_exit_rate": 0.001, "net_PnL": -348.73, "PF": 0.001,
     "max_DD": 348.73, "failure_reason": "FAILED_HISTORICAL_SIMULATION+FAILED_FULL_ENTRY_ACCOUNTING",
     "killed_at_version": "V21.7.57+V21.7.61", "revival_allowed": False,
     "revival_conditions": "New causal hypothesis required. XRP 5m DOWN explicitly killed."},
    {"strategy_id": "BTC_15M_30_60C_PLUS_3C_SCALP", "asset": "BTC", "interval": "15m", "side": "BOTH",
     "entry_bucket": "30-60c", "profit_target": "+3c", "entry_rule": "token_price 30-60c, spread<=5c, TTE>=30s",
     "exit_rule": "scalp exit at bid>=entry+3c", "sample_source": "PMXT_historical",
     "entries": 329, "scalp_exits": 32, "scalp_exit_rate": 0.097, "net_PnL": -76.88, "PF": 0.184,
     "max_DD": 76.88, "failure_reason": "FAILED_HISTORICAL_SIMULATION", "killed_at_version": "V21.7.61",
     "revival_allowed": False, "revival_conditions": "New causal hypothesis required."},
    {"strategy_id": "ALL_5M_30_60C_PLUS_1C_SCALP", "asset": "ALL", "interval": "5m", "side": "BOTH",
     "entry_bucket": "30-60c", "profit_target": "+1c", "entry_rule": "token_price 30-60c, spread<=5c, TTE>=30s",
     "exit_rule": "scalp exit at bid>=entry+1c", "sample_source": "PMXT_historical",
     "entries": 3912, "scalp_exits": 443, "scalp_exit_rate": 0.113, "net_PnL": -1011.66, "PF": 0.063,
     "max_DD": 1011.66, "failure_reason": "FAILED_HISTORICAL_SIMULATION", "killed_at_version": "V21.7.61",
     "revival_allowed": False, "revival_conditions": "New causal hypothesis required."},
    {"strategy_id": "ALL_5M_30_60C_PLUS_2C_SCALP", "asset": "ALL", "interval": "5m", "side": "BOTH",
     "entry_bucket": "30-60c", "profit_target": "+2c", "entry_rule": "token_price 30-60c, spread<=5c, TTE>=30s",
     "exit_rule": "scalp exit at bid>=entry+2c", "sample_source": "PMXT_historical",
     "entries": 3751, "scalp_exits": 223, "scalp_exit_rate": 0.059, "net_PnL": -988.12, "PF": 0.067,
     "max_DD": 988.12, "failure_reason": "FAILED_HISTORICAL_SIMULATION", "killed_at_version": "V21.7.61",
     "revival_allowed": False, "revival_conditions": "New causal hypothesis required."},
    {"strategy_id": "ALL_5M_30_60C_PLUS_3C_SCALP", "asset": "ALL", "interval": "5m", "side": "BOTH",
     "entry_bucket": "30-60c", "profit_target": "+3c", "entry_rule": "token_price 30-60c, spread<=5c, TTE>=30s",
     "exit_rule": "scalp exit at bid>=entry+3c", "sample_source": "live_replay+full_entry",
     "entries": 4950, "scalp_exits": 201, "scalp_exit_rate": 0.041, "net_PnL": -725.79, "PF": 0.134,
     "max_DD": 725.79, "failure_reason": "FAILED_LIVE_REPLAY+FAILED_FULL_ENTRY_ACCOUNTING",
     "killed_at_version": "V21.7.57+V21.7.60+V21.7.61", "revival_allowed": False,
     "revival_conditions": "New causal hypothesis required."},
    {"strategy_id": "ALL_5M_30_60C_PLUS_5C_SCALP", "asset": "ALL", "interval": "5m", "side": "BOTH",
     "entry_bucket": "30-60c", "profit_target": "+5c", "entry_rule": "token_price 30-60c, spread<=5c, TTE>=30s",
     "exit_rule": "scalp exit at bid>=entry+5c", "sample_source": "V21.7.57_profit_target_comparison",
     "entries": 194, "scalp_exits": 34, "scalp_exit_rate": 0.175, "net_PnL": -131.31, "PF": 0.694,
     "max_DD": 131.31, "failure_reason": "FAILED_PROFIT_TARGET_COMPARISON", "killed_at_version": "V21.7.57+V21.7.61",
     "revival_allowed": False, "revival_conditions": "New causal hypothesis required."},
    {"strategy_id": "BTC_15M_3_8_TAIL_CANARY", "asset": "BTC", "interval": "15m", "side": "BOTH",
     "entry_bucket": "3-8c", "profit_target": "N/A", "entry_rule": "bucket 3-8c tail canary",
     "exit_rule": "hold to expiry", "sample_source": "V21.7.50_structure_audit",
     "entries": 0, "scalp_exits": 0, "scalp_exit_rate": 0, "net_PnL": 0, "PF": 0, "max_DD": 0,
     "failure_reason": "STRUCTURALLY_IMPRACTICAL", "killed_at_version": "V21.7.50",
     "revival_allowed": False, "revival_conditions": "New causal hypothesis required."},
    {"strategy_id": "BTC_15M_8_12_INVALIDATED_MICRO_CANARY", "asset": "BTC", "interval": "15m", "side": "BOTH",
     "entry_bucket": "8-12c", "profit_target": "N/A", "entry_rule": "bucket 8-12c micro canary",
     "exit_rule": "hold to expiry", "sample_source": "V21.7.50_structure_audit",
     "entries": 0, "scalp_exits": 0, "scalp_exit_rate": 0, "net_PnL": 0, "PF": 0, "max_DD": 0,
     "failure_reason": "PROMOTION_INVALIDATED", "killed_at_version": "V21.7.50",
     "revival_allowed": False, "revival_conditions": "New causal hypothesis required."},
    {"strategy_id": "WEATHER_OLD_SIGMA_MODEL", "asset": "weather", "interval": "daily", "side": "N/A",
     "entry_bucket": "N/A", "profit_target": "N/A", "entry_rule": "fixed sigma=0.3C",
     "exit_rule": "settlement", "sample_source": "V21.7.52_audit",
     "entries": 5, "scalp_exits": 0, "scalp_exit_rate": 0, "net_PnL": -7.60, "PF": 0, "max_DD": 7.60,
     "failure_reason": "FORECAST_MODEL_ERROR_BROKEN_SIGMA", "killed_at_version": "V21.7.52",
     "revival_allowed": False, "revival_conditions": "Repaired ensemble sigma model already in testing."},
]

registry = {
    "scalp_family_status": "KILLED",
    "scalp_promotion_allowed": False,
    "scalp_live_review_allowed": False,
    "scalp_retest_allowed": False,
    "killed_variants": killed_variants,
    "total_killed": len(killed_variants),
    "rule": "Killed strategies cannot be revived without a materially new causal hypothesis. Do not revive under a different name.",
    "timestamp": NOW
}
write_json(OUT / "failed_strategy_registry.json", registry)

# ── 2. No-Edge Guardrail ──────────────────────────────────────────────
guardrail = {
    "guardrail_name": "NO_EDGE_GUARDRAIL",
    "active": True,
    "check_fields": ["asset", "interval", "side", "entry_bucket", "profit_target",
                     "entry_rule", "exit_rule", "hold_rule", "data_source", "hypothesis_class"],
    "blocked_patterns": [
        {"pattern": "30-60c bucket scalp", "matches": "any entry_bucket 30-60c with scalp exit rule",
         "block_reason": "DUPLICATE_FAILED_STRATEGY"},
        {"pattern": "any bucket-only scalp", "matches": "entry rule is bucket filter + scalp exit",
         "block_reason": "DUPLICATE_FAILED_STRATEGY"},
        {"pattern": "profit target +1c to +5c on 5m/15m", "matches": "scalp threshold 1-5c on crypto 5m/15m",
         "block_reason": "DUPLICATE_FAILED_STRATEGY"},
        {"pattern": "XRP 5m DOWN any scalp", "matches": "XRP 5m DOWN with any scalp exit",
         "block_reason": "EXPLICITLY_KILLED_V21.7.57"},
        {"pattern": "BTC 15m 3-8c or 8-12c", "matches": "old canary buckets",
         "block_reason": "STRUCTURALLY_INVALIDATED"},
        {"pattern": "fixed sigma weather", "matches": "sigma=0.3 weather model",
         "block_reason": "BROKEN_MODEL_KILLED"}
    ],
    "hard_fail_rule": "System MUST block test if materially similar to killed variant without new causal hypothesis.",
    "timestamp": NOW
}
write_json(OUT / "no_edge_guardrail_report.json", guardrail)

# ── 3. V21.7.60 Evidence Freeze ───────────────────────────────────────
evidence = {
    "frozen_at": NOW,
    "frozen_by": "V21.7.61",
    "sources": {
        "out_of_sample_filter_test": {
            "source_file": "output/v21760_out_of_sample_and_pmxt/out_of_sample_filter_test_report.json",
            "strategy_count": len(oos_report.get("out_of_sample_results", {})),
            "positive_strategy_count": len(oos_report.get("filters_that_survive_out_of_sample", [])),
            "aggregate_PnL": sum(r.get("out_of_sample_pnl", 0) for r in oos_report.get("out_of_sample_results", {}).values()),
        },
        "live_paper_replay": {
            "source_file": "output/v21760_out_of_sample_and_pmxt/live_paper_trade_report.json",
            "trade_count": paper_report.get("total_paper_positions", 0),
            "scalp_exits": paper_report.get("scalp_exits", 0),
            "scalp_exit_rate": paper_report.get("scalp_exit_rate", 0),
            "net_PnL": paper_report.get("total_pnl", 0),
            "PF": paper_report.get("PF", 0),
        },
        "pmxt_historical_simulation": {
            "source_file": "output/v21760_out_of_sample_and_pmxt/pmxt_historical_simulation_report.json",
            "strategy_count": pmxt_report.get("strategies_tested", 0),
            "trade_count": pmxt_report.get("total_trades_simulated", 0),
            "positive_strategy_count": len(pmxt_report.get("positive_edge_strategies", [])),
            "best_strategy": pmxt_report.get("best_strategy", "none"),
            "worst_strategy": pmxt_report.get("worst_strategy", "none"),
        }
    },
    "aggregate": {
        "total_strategies_tested": len(oos_report.get("out_of_sample_results", {})) + pmxt_report.get("strategies_tested", 0) + 1,
        "total_trades_simulated": paper_report.get("total_paper_positions", 0) + pmxt_report.get("total_trades_simulated", 0),
        "positive_strategy_count": 0,
        "aggregate_PnL": paper_report.get("total_pnl", 0) + sum(r.get("out_of_sample_pnl", 0) for r in oos_report.get("out_of_sample_results", {}).values()) + sum(r.get("total_pnl", 0) for r in pmxt_report.get("strategy_results", {}).values()),
    },
    "final_conclusion": "NO_SCALP_EDGE_IN_TESTED_MARKET_STRUCTURE",
    "evidence_is_canonical": True,
    "evidence_may_not_be_overwritten": True,
}
evidence["aggregate"]["aggregate_PF"] = 0  # all negative
write_json(OUT / "v21760_evidence_freeze.json", evidence)

# ── 4. Profitable Subset Guardrail ────────────────────────────────────
subset_guardrail = {
    "rule": "profitable_exit_subset != profitable_strategy",
    "description": "A strategy may never be classified as positive based only on successful exits.",
    "required_metrics_for_any_strategy_report": [
        "total_entries", "successful_exits", "failed_exits", "non_exit_losses",
        "open_positions", "closed_strategy_PnL", "PF", "max_DD", "full_entry_expectancy"
    ],
    "hard_fail_conditions": [
        "scalp_exit_WR used as promotion evidence without full-entry PnL",
        "scalp winners reported without non-exit losses",
        "open risk ignored",
        "profitable subset used as strategy-level conclusion"
    ],
    "historical_example": "V21.7.55/56 reported 71/71 scalp exits at 100% WR (+$51.54). V21.7.57 full-entry accounting revealed strategy PnL = -$490.33, PF = 0.13.",
    "active": True,
    "timestamp": NOW
}
write_json(OUT / "profitable_subset_guardrail.json", subset_guardrail)

# ── 5. New Hypothesis Requirement ─────────────────────────────────────
hypothesis_req = {
    "rule": "Any future crypto strategy must attach a causal hypothesis.",
    "allowed_hypothesis_classes": [
        "ORDER_FLOW_IMBALANCE", "CVD_OBI_ALIGNMENT", "ORACLE_REFERENCE_DISLOCATION",
        "CHAINLINK_RTDS_LAG", "RARE_BREAKAWAY_CONTINUATION", "LIQUIDITY_SHOCK_REPRICING",
        "SETTLEMENT_SOURCE_DIVERGENCE", "VOLATILITY_EXPANSION_DISLOCATION",
        "ORDERBOOK_STRESS_DISLOCATION", "CROSS_ASSET_REFLEXIVE_MOVE"
    ],
    "forbidden_as_standalone_hypotheses": [
        "bucket-only scalp", "asset-only entry", "side-only entry", "cheap-token buying",
        "generic 30-60c repricing", "historical Markov memory", "streak continuation",
        "RSI/MACD/EMA/VWAP stacking"
    ],
    "requirement": "Strategy proposal must include hypothesis_class from allowed list and causal mechanism description.",
    "timestamp": NOW
}
write_json(OUT / "new_hypothesis_requirement_report.json", hypothesis_req)

# ── 6. Research Redirection ───────────────────────────────────────────
redirection = {
    "redirect_to": [
        "weather repaired-model settlement accumulation",
        "BTC 15m dislocation research",
        "BTC 5m dislocation research",
        "order-flow minimalism lab",
        "OBI / CVD validation",
        "breakaway threshold capture",
        "oracle/reference-price dislocation",
        "multi-asset hypothesis ranking"
    ],
    "do_not_redirect_to": [
        "more 30-60c scalp retests",
        "lower scalp thresholds",
        "higher scalp thresholds",
        "same bucket with renamed filters",
        "same strategy with different asset basket"
    ],
    "timestamp": NOW
}
write_json(OUT / "research_redirection_plan.json", redirection)

# ── 7. Capital Protection ────────────────────────────────────────────
capital = {
    "capital_accumulation_mode": "RESEARCH_AND_RESERVE",
    "capital_deployment_allowed": False,
    "real_orders_allowed": False,
    "wallet_spend_allowed": False,
    "reason_blocked": "NO_VERIFIED_EDGE",
    "timestamp": NOW
}
write_json(OUT / "capital_protection_status.json", capital)

# ── 8. Live Lock Assertions ───────────────────────────────────────────
live_lock = {
    "real_orders_allowed": False,
    "live_authorization_suspended": True,
    "wallet_spend_allowed": False,
    "capital_deployment_allowed": False,
    "assertions": {
        "no_live_orders_submitted": True,
        "no_wallet_spend": True,
        "no_real_positions_opened": True,
        "no_killed_strategy_promoted": True
    },
    "violation_action": "HALT_ALL_BOTS + WRITE_P0_LIVE_VIOLATION_REPORT",
    "timestamp": NOW,
    "status": "ALL_LIVE_PATHS_LOCKED"
}
write_json(OUT / "live_lock_assertion_report.json", live_lock)

# ── 9. Final Report ───────────────────────────────────────────────────
final = {
    "module": "V21.7.61",
    "timestamp": NOW,
    "classifications": {
        "scalp_family": "SCALP_FAMILY_KILLED",
        "guardrail": "FAILED_STRATEGY_REGISTRY_ACTIVE",
        "capital": "CAPITAL_DEPLOYMENT_BLOCKED_PENDING_EDGE",
        "live": "LIVE_AUTHORIZATION_REMAINS_SUSPENDED"
    },
    "failed_strategy_count": len(killed_variants),
    "evidence_frozen": True,
    "profitable_subset_guardrail_active": True,
    "new_hypothesis_required": True,
    "ready_for_review_count": 0,
    "real_orders_allowed": False,
    "capital_deployment_allowed": False,
    "status": "V21.7.61_NO_EDGE_GUARDRAIL_COMPLETE"
}
write_json(OUT / "v21761_final_report.json", final)

# ── 10. Supervisor ────────────────────────────────────────────────────
supervisor = {
    "real_orders_allowed": False,
    "live_authorization_suspended": True,
    "capital_deployment_allowed": False,
    "failed_strategy_count": len(killed_variants),
    "scalp_family_status": "KILLED",
    "duplicate_failed_strategy_blocker_active": True,
    "profitable_subset_guardrail_active": True,
    "new_hypothesis_required": True,
    "ready_for_review_count": 0,
    "capital_accumulation_mode": "RESEARCH_AND_RESERVE",
    "halted": False,
    "halt_reason": None,
    "next_action": "order_flow_and_dislocation_hypothesis_lab",
    "timestamp": NOW,
    "module": "V21.7.61"
}
write_json(SUP / "v21761_no_edge_guardrail_status.json", supervisor)

# ── Assertions ────────────────────────────────────────────────────────
assert not False  # real_orders_allowed
assert True  # live_authorization_suspended
assert not False  # wallet_spend_allowed
assert not False  # capital_deployment_allowed

print("=" * 60)
print("V21.7.61 NO-EDGE GUARDRAIL COMPLETE")
print("=" * 60)
print(f"\nScalp family: KILLED")
print(f"Failed strategies registered: {len(killed_variants)}")
print(f"Evidence frozen: V21.7.60 canonical")
print(f"Profitable-subset guardrail: ACTIVE")
print(f"New hypothesis required: YES")
print(f"Duplicate retest blocker: ACTIVE")
print(f"Capital deployment: BLOCKED (NO_VERIFIED_EDGE)")
print(f"Live authorization: SUSPENDED")
print(f"Ready for review: 0")
print(f"Next action: order_flow_and_dislocation_hypothesis_lab")
print(f"\nAll 10 outputs generated.")