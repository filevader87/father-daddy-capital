#!/usr/bin/env python3
"""V21.7.58 Capital Accumulation Framework — defines deployment rules. NO live trading."""
import json
from pathlib import Path
from datetime import datetime, timezone

BASE = Path(__file__).resolve().parent.parent.parent
OUT = BASE / "output/v21758_edge_discovery_capital_framework"
OUT.mkdir(parents=True, exist_ok=True)

framework = {
    "mode": "RESEARCH_AND_RESERVE",
    "capital_deployment_allowed": False,
    "real_orders_allowed": False,
    "live_authorization_suspended": True,
    "wallet_spend_allowed": False,
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "verified_pUSD_balance": None,
    "available_collateral": None,
    "open_live_exposure": 0,
    "paper_edge_candidates": [],
    "approved_live_cells": [],
    "daily_loss_limit": 5.0,
    "weekly_loss_limit": 15.0,
    "max_position_size": 5.0,
    "max_open_positions": 1,
    "cooldown_after_loss_streak": 3,
    "profit_lock_rule": "Lock 50% of weekly profit to reserve. Do not reinvest until 25 live micro-trades reviewed.",
    "withdrawal_or_reserve_rule": "After first $10 profit, withdraw 50% to reserve. Maintain minimum $20 operational balance.",
    "initial_live_rules_after_future_approval": {
        "start_with_one_cell_only": True,
        "max_order_size": 5.0,
        "max_live_positions": 1,
        "max_trades_per_day": 1,
        "halt_after_first_loss": True,
        "manual_review_after_any_live_settlement": True,
        "no_scale_until_25_live_micro_trades": True
    },
    "capital_deployment_gates": [
        "verified_forward_paper_edge",
        "positive_PF_above_1.25",
        "controlled_drawdown_below_15pct",
        "slippage_survives_stress",
        "settlement_proven",
        "quote_provenance_clean",
        "separate_live_review_directive"
    ],
    "capital_accumulation_mode": "RESEARCH_AND_RESERVE",
    "next_phase": "Find verified edge through forward paper testing. No deployment until gates pass."
}

with open(OUT / "capital_accumulation_framework.json", "w") as f:
    json.dump(framework, f, indent=2)

print("Capital accumulation framework written")
print(f"Mode: {framework['capital_accumulation_mode']}")
print(f"Deployment allowed: {framework['capital_deployment_allowed']}")
print("OUTPUT: capital_accumulation_framework.json")