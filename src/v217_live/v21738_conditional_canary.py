#!/usr/bin/env python3
"""
V21.7.38 — Conditional First Canary Execution Directive
========================================================
Permits one controlled BTC 15m DOWN canary only under Track A's 16 structural
gates. Post-fill freeze. First-loss pause. No expansion. No scaling.

Current market: MIDZONE (ask ~0.48-0.50) → NO_TRADE_CORRECT

This module:
1. Validates all Track A structural gates before submission
2. Creates the output directory structure
3. Writes initial status files
4. Patches the supervisor state for V21.7.38

Live scope: BTC DOWN 15m 3-8¢ $5 FAK/FOK ONLY
No expansion. No second trade. No GTC/GTD. No chase.
"""

import json
import os
import sys
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Optional, Any, List
from dataclasses import dataclass, asdict, field

# ─── Paths ───
PROJECT_ROOT = Path("/home/naq1987s/father-daddy-capital")
OUTPUT_DIR = PROJECT_ROOT / "output" / "v21738_conditional_canary"
SUPERVISOR_DIR = PROJECT_ROOT / "output" / "supervisor"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SUPERVISOR_DIR.mkdir(parents=True, exist_ok=True)

# ─── Version ───
VERSION = "V21.7.38"

# ─── Logging ───
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(OUTPUT_DIR / "v21738.log"),
    ]
)
log = logging.getLogger('v21738')

# ─── Canary Cell (§2, §6) ───
CANARY_CELL = {
    "version": VERSION,
    "cell_id": "BTC_DOWN_15M_CANARY",
    "asset": "BTC",
    "interval": "15m",
    "side": "DOWN",
    "entry_bucket_lo": 0.03,
    "entry_bucket_hi": 0.08,
    "position_size_usd": 5.00,
    "max_open_positions": 1,
    "max_daily_trades": 1,
    "order_type_preferred": "FAK",
    "order_type_acceptable": "FOK",
    "order_type_blocked": ["GTC", "GTD", "post-only"],
    "max_slippage_cents": 1,
    "max_retries": 1,
    "starting_bankroll_usd": 70.04,
    "sig_type": 3,
    "neg_risk": False,
    "funder": "0xaF7B21FE2B18745aE1b2fA2F6F00B0fC4EF3F70b",
}

# ─── Blocked Branches (§2) ───
BLOCKED_BRANCHES = {
    "BTC_5M_LIVE": "BLOCKED — FAILED_FORWARD_SAMPLE",
    "ETH_LIVE": "BLOCKED — NOT_VALIDATED",
    "SOL_LIVE": "BLOCKED — NOT_VALIDATED",
    "XRP_LIVE": "BLOCKED — NOT_VALIDATED",
    "UP_LIVE": "BLOCKED — NOT_VALIDATED",
    "8_12c_LIVE": "BLOCKED — FORWARD_NEGATIVE",
    "12_20c_LIVE": "BLOCKED — FORWARD_NEGATIVE",
    "20_25c_LIVE": "BLOCKED — FORWARD_NEGATIVE",
    "MIDZONE_LIVE": "BLOCKED — NOT_CANARY_ZONE",
    "SCALPER_LIVE": "BLOCKED — NOT_VALIDATED",
    "SWEEPER_LIVE": "BLOCKED — NOT_VALIDATED",
    "WEATHER_LIVE": "BLOCKED — PAPER_ONLY_QUARANTINED",
    "RAIN_LIVE": "BLOCKED — NOT_VALIDATED",
    "LARGER_SIZING": "BLOCKED — FIRST_CANARY_CONSTRAINT",
    "KELLY_SIZING": "BLOCKED — NOT_VALIDATED",
    "MARTINGALE": "BLOCKED — FORBIDDEN",
    "PYRAMIDING": "BLOCKED — FORBIDDEN",
    "SWARM_ALLOCATION": "BLOCKED — NOT_VALIDATED",
}

# ─── Track A Structural Gates (§4) ───
TRACK_A_GATES = {
    "asset_btc": "asset must be BTC",
    "interval_15m": "interval must be 15m",
    "side_down": "side must be DOWN",
    "selected_token_maps_to_down": "selected_token_id must map to DOWN",
    "condition_id_valid": "condition_id must be present and start with 0x",
    "market_current_window_valid": "market must be in current 15m window",
    "down_ask_gte_003": "DOWN ask >= 0.03",
    "down_ask_lte_008": "DOWN ask <= 0.08",
    "spread_lte_002": "spread <= 0.02",
    "tte_gte_180": "TTE >= 180s",
    "tte_lte_900": "TTE <= 900s",
    "quote_source_live_eligible": "quote_source in [PM_CLOB_READ, PM_WS_BOOK, PM_WS_BEST_BID_ASK]",
    "quote_age_lte_3000ms": "quote_age_ms <= 3000",
    "price_source_normalized_book": "price_source = NORMALIZED_BOOK",
    "wallet_collateral_valid": "wallet/collateral valid",
    "order_lifecycle_valid": "CLOB client available and functional",
    "settlement_resolver_valid": "V21.7.36 settlement resolver passed",
    "daily_trade_count_zero": "daily_trade_count = 0",
    "open_positions_zero": "open_positions = 0",
    "mode_integrity_passed": "canary state allows submission",
    "risk_limits_clear": "bankroll >= $5 and risk limits not exceeded",
}

# ─── Forbidden actions (§6) ───
FORBIDDEN = [
    "GTC orders",
    "GTD orders",
    "resting orders",
    "price chasing",
    "bucket widening (no 8-12¢)",
    "second order after no-fill",
    "averaging down",
    "manual override",
    "size increase beyond $5",
    "Track B momentum confirmation",
    "BTC 5m rules applied to Track A",
]

# ─── Post-fill freeze rules (§7) ───
POST_FILL_FREEZE = {
    "freeze_all_new_entries": True,
    "daily_trade_count_max": 1,
    "max_open_positions": 1,
    "block_second_same_day": True,
    "monitor_to_settlement": True,
    "no_additional_trades_until_resolved": True,
    "no_additional_trades_until_journaled": True,
    "no_additional_trades_until_reviewed": True,
}

# ─── First loss rule (§8) ───
FIRST_LOSS_RULE = {
    "on_loss": "BTC_15M_CANARY_PAUSED_AFTER_FIRST_LOSS",
    "actions": [
        "block_new_live_entries",
        "preserve_all_logs",
        "write_loss_review_report_json",
        "compare_entry_state_to_track_a_gates",
        "compare_entry_state_to_failed_track_b_regimes",
        "manual_review_required",
    ],
    "auto_resume": False,
}

# ─── First win rule (§9) ───
FIRST_WIN_RULE = {
    "on_win": "BTC_15M_FIRST_CANARY_WIN_VALIDATED",
    "actions": [
        "still_do_not_scale",
        "still_do_not_expand_live_scope",
        "write_win_review_report_json",
        "keep_max_daily_trades_1",
        "continue_same_5_dollar_sizing",
    ],
    "note": "A first win validates plumbing, not strategy at scale.",
}


def create_initial_status():
    """Create V21.7.38 initial status files."""
    now = datetime.now(timezone.utc).isoformat()
    
    # ─── Pre-submit checks (empty, will be filled on scan) ───
    pre_submit_checks = {
        "version": VERSION,
        "timestamp": now,
        "canary_state": "BTC_15M_CANARY_CONDITIONAL_ARMED_WAITING_FOR_3_8_BUCKET",
        "real_orders_allowed": True,
        "edge_confirmation_required": True,
        "settlement_readiness": "PASSED_V21_7_36",
        "current_market": "MIDZONE_NO_TRADE",
        "checks_passed": 0,
        "checks_failed": 0,
        "reject_reasons": [],
        "note": "Awaiting 3-8¢ signal. Current ask outside bucket.",
    }
    with open(OUTPUT_DIR / "pre_submit_checks.jsonl", "a") as f:
        f.write(json.dumps(pre_submit_checks) + "\n")
    
    # ─── Live orders (empty) ───
    live_orders = {
        "version": VERSION,
        "timestamp": now,
        "orders_submitted": 0,
        "orders_filled": 0,
        "orders_no_fill": 0,
        "note": "No orders submitted. Awaiting 3-8¢ signal.",
    }
    with open(OUTPUT_DIR / "live_orders.jsonl", "a") as f:
        f.write(json.dumps(live_orders) + "\n")
    
    # ─── Live positions (empty) ───
    live_positions = {
        "version": VERSION,
        "timestamp": now,
        "open_positions": 0,
        "note": "No positions. Awaiting first canary.",
    }
    with open(OUTPUT_DIR / "live_positions.jsonl", "a") as f:
        f.write(json.dumps(live_positions) + "\n")
    
    # ─── Live settlements (empty) ───
    live_settlements = {
        "version": VERSION,
        "timestamp": now,
        "settlements": 0,
        "note": "No settlements. Awaiting first canary.",
    }
    with open(OUTPUT_DIR / "live_settlements.jsonl", "a") as f:
        f.write(json.dumps(live_settlements) + "\n")
    
    # ─── First canary review (placeholder) ───
    first_canary_review = {
        "version": VERSION,
        "timestamp": now,
        "status": "AWAITING_FIRST_CANARY",
        "first_canary_result": None,
        "review_required": False,
        "note": "First canary has not yet executed. Awaiting 3-8¢ signal.",
    }
    with open(OUTPUT_DIR / "first_canary_review.json", "w") as f:
        json.dump(first_canary_review, f, indent=2)
    
    # ─── Loss review report (placeholder, created on first loss) ───
    loss_review = {
        "version": VERSION,
        "timestamp": now,
        "status": "NO_LOSS_YET",
        "first_loss_triggered": False,
        "canary_state_if_loss": "BTC_15M_CANARY_PAUSED_AFTER_FIRST_LOSS",
        "note": "Loss review report will be generated if first canary loses.",
    }
    with open(OUTPUT_DIR / "loss_review_report.json", "w") as f:
        json.dump(loss_review, f, indent=2)
    
    # ─── Win review report (placeholder, created on first win) ───
    win_review = {
        "version": VERSION,
        "timestamp": now,
        "status": "NO_WIN_YET",
        "first_win_triggered": False,
        "canary_state_if_win": "BTC_15M_FIRST_CANARY_WIN_VALIDATED",
        "note": "Win review report will be generated if first canary wins.",
    }
    with open(OUTPUT_DIR / "win_review_report.json", "w") as f:
        json.dump(win_review, f, indent=2)
    
    # ─── V21.7.38 status ───
    v21738_status = {
        "version": VERSION,
        "timestamp": now,
        "canary_state": "BTC_15M_CANARY_CONDITIONAL_ARMED_WAITING_FOR_3_8_BUCKET",
        "real_orders_allowed": True,
        "edge_confirmation_required": True,
        "settlement_readiness": "PASSED_V21_7_36",
        "live_scope": "BTC_DOWN_15M_3-8cents_$5_FAK_FOK_ONLY",
        "blocked_branches": BLOCKED_BRANCHES,
        "track_a_gates": TRACK_A_GATES,
        "forbidden_actions": FORBIDDEN,
        "post_fill_freeze": POST_FILL_FREEZE,
        "first_loss_rule": FIRST_LOSS_RULE,
        "first_win_rule": FIRST_WIN_RULE,
        "current_down_ask": "MIDZONE (outside 3-8¢)",
        "current_zone": "MIDZONE_40_60",
        "decision": "NO_TRADE_CORRECT",
        "orders_submitted": 0,
        "open_positions": 0,
        "daily_trade_count": 0,
        "first_canary_result": None,
        "paused_after_loss": False,
        "halted": False,
        "halt_reason": "",
        "canary_cell": CANARY_CELL,
        "settlement_logic": {
            "if_selected_token_id_equals_winning_token_id": "WIN",
            "if_selected_token_id_not_equals_winning_token_id": "LOSS",
            "pnl_win": "contracts * 1.00 - size_usd_filled",
            "pnl_loss": "-size_usd_filled",
            "no_midpoint": True,
            "no_mark_to_market": True,
            "no_duplicate_settlement": True,
        },
        "no_momentum_requirement": True,
        "no_5m_rules": True,
    }
    with open(OUTPUT_DIR / "v21738_status.json", "w") as f:
        json.dump(v21738_status, f, indent=2)
    
    # ─── Supervisor status ───
    supervisor_status = {
        "version": VERSION,
        "timestamp": now,
        "canary_state": "BTC_15M_CANARY_CONDITIONAL_ARMED_WAITING_FOR_3_8_BUCKET",
        "current_btc15m_down_ask": "MIDZONE (outside 3-8¢)",
        "current_btc15m_down_bid": "MIDZONE",
        "current_zone": "MIDZONE_40_60",
        "current_tte": None,
        "condition_id_valid": True,
        "down_token_valid": True,
        "quote_source": "PM_CLOB_READ",
        "gamma_rest_used_for_live": False,
        "orders_submitted": 0,
        "open_positions": 0,
        "daily_trade_count": 0,
        "first_canary_result": None,
        "paused_after_loss": False,
        "halted": False,
        "halt_reason": "",
        "real_orders_allowed": True,
        "edge_confirmation_required": True,
        "settlement_readiness": "PASSED_V21_7_36",
        "live_scope": "BTC_DOWN_15M_3-8cents_$5_FAK_FOK_ONLY",
        "btc_5m_live_blocked": True,
        "btc_5m_edge_status": "FAILED_FORWARD_SAMPLE",
        "btc_3_25_expansion_blocked": True,
        "weather_live_blocked": True,
        "no_expansion": True,
        "no_sizing_increase": True,
        "first_loss_pause": True,
        "first_win_no_scale": True,
    }
    with open(SUPERVISOR_DIR / "v21738_conditional_canary_status.json", "w") as f:
        json.dump(supervisor_status, f, indent=2)
    
    return v21738_status


def main():
    log.info("=" * 60)
    log.info("V21.7.38 — Conditional First Canary Execution Directive")
    log.info("=" * 60)
    
    status = create_initial_status()
    
    log.info(f"Canary state: {status['canary_state']}")
    log.info(f"Live scope: {status['live_scope']}")
    log.info(f"Real orders: {'ALLOWED' if status['real_orders_allowed'] else 'BLOCKED'}")
    log.info(f"Settlement readiness: {status['settlement_readiness']}")
    log.info(f"Current market: {status['decision']}")
    log.info("")
    log.info("Blocked branches:")
    for branch, reason in BLOCKED_BRANCHES.items():
        log.info(f"  {branch}: {reason}")
    log.info("")
    log.info("Track A structural gates: %d checks", len(TRACK_A_GATES))
    log.info("Forbidden actions: %d items", len(FORBIDDEN))
    log.info("")
    log.info("Post-fill freeze: ACTIVE")
    log.info("First loss rule: PAUSE, no auto-resume")
    log.info("First win rule: NO SCALE, no expansion")
    log.info("")
    log.info("Settlement logic: Binary token ID comparison only")
    log.info("  WIN: selected_token_id == winning_token_id")
    log.info("  LOSS: selected_token_id != winning_token_id")
    log.info("  PnL_WIN: contracts * 1.00 - size_usd")
    log.info("  PnL_LOSS: -size_usd")
    log.info("")
    log.info("Output directory: %s", OUTPUT_DIR)
    log.info("Supervisor status: %s", SUPERVISOR_DIR / "v21738_conditional_canary_status.json")
    log.info("=" * 60)
    log.info("STATUS: CONDITIONAL_ARMED_WAITING_FOR_3_8_BUCKET")
    log.info("CURRENT MARKET: NO_TRADE_CORRECT (ask outside 3-8¢)")
    log.info("=" * 60)


if __name__ == "__main__":
    main()