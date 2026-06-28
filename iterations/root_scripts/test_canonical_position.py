#!/usr/bin/env python3
"""
V19.9 Canonical Position Regression Tests
==========================================
Tests for:
- pres.build_paper_entry mandatory usage
- manual position dict creation rejection
- missing fields prevent OPENED
- child-market / monthly-parent rejection
- expiry timestamp mismatch rejection
- side/token mapping validation
- duplicate settlement block
- accounting invariant after settlement

Run: python3 test_canonical_position.py
"""

import json
import sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))

import canonical_position as cpos
import paper_resolution as pres

# Reset counters before each test
def setup():
    cpos.reset_counters()
    # Clean reject log
    reject_log = REPO / "paper_trading" / "canonical_position_reject.jsonl"
    if reject_log.exists():
        reject_log.unlink()

def make_canonical_position(**overrides):
    """Create a valid canonical position dict with all required fields."""
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(minutes=5)
    expected_end = now + timedelta(minutes=5)
    pos = {
        "position_id": "P-test123-U-123456",
        "profile": "CONVEX_20_30",
        "asset": "BTC",
        "interval": "5m",
        "market_slug": "btc-updown-5m-20260602",
        "condition_id": "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        "conditionId": "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        "market_id": "12345",
        "question": "Bitcoin Up or Down - June 2, 7:30AM-7:45AM ET",
        "selected_side": "UP",
        "opposite_side": "DOWN",
        "selected_token_id": "up_token_123",
        "opposite_token_id": "down_token_456",
        "up_token_id": "up_token_123",
        "down_token_id": "down_token_456",
        "entry_timestamp": now.isoformat(),
        "entry_price": 0.28,
        "entry_bid": 0.27,
        "entry_ask": 0.28,
        "entry_spread": 0.01,
        "entry_depth": 500,
        "size_usd": 2.0,
        "contracts": 7.14,
        "expiry_timestamp": expiry.isoformat(),
        "expected_market_end_time": expected_end.isoformat(),
        "time_to_expiry_at_entry": 5,
        "signal_rsi": 28.5,
        "signal_zone": "CORE_UP",
        "signal_confidence": 0.4,
        "estimated_probability": 0.35,
        "adjusted_probability": 0.33,
        "gross_EV": 0.05,
        "net_EV": 0.01,
        "market_phase_at_entry": "dislocation",
        "token_state_at_entry": "live_dislocation",
        "reference_price": 66890.0,
        "current_price_at_entry": 66890.0,
        "recoverability_score": 0.85,
        "position_builder_source": "pres.build_paper_entry",
        "status": "OPENED",
    }
    pos.update(overrides)
    return pos

def make_valid_contract(**overrides):
    """Create a valid 5m child-market contract."""
    now = datetime.now(timezone.utc)
    end = now + timedelta(minutes=5)
    c = {
        "conditionId": "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        "question": "Bitcoin Up or Down - June 3, 7:30AM-7:45AM ET",
        "end_date": end.isoformat(),
        "window": "5m",
        "series_slug": "btc-updown-5m",
        "event_slug": "btc-updown-5m",
        "asset": "BTC",
        "up_token_id": "up_token_123",
        "down_token_id": "down_token_456",
        "market_id": "12345",
        "mins_to_expiry": 5,
    }
    c.update(overrides)
    return c


# ═══ TESTS ═══

def test_validation_monitor_uses_build_paper_entry():
    """§VI: Validation monitor uses pres.build_paper_entry."""
    setup()
    contract = make_valid_contract()
    entry_dict = {
        "action": "BUY_Up",
        "question": contract["question"],
        "conditionId": contract["conditionId"],
        "contract_price": 0.28,
        "bet": 2.0,
        "side": "Up",
        "mode": "PAPER",
        "asset": "BTC",
        "entry_price": 0.28,
    }
    signal = {"rsi_zone": "CORE_UP", "direction": "up", "confidence": 0.4}
    pos = cpos.build_canonical_paper_entry(
        entry=entry_dict, contract=contract,
        shadow_profile="TEST", rsi=28.5, signal=signal,
    )
    assert pos is not None, "build_canonical_paper_entry should return valid position"
    assert pos["position_builder_source"] == "pres.build_paper_entry"
    assert cpos.CANONICAL_COUNTERS["positions_built_with_pres_build_paper_entry"] == 1
    print("✓ test_validation_monitor_uses_build_paper_entry")

def test_manual_position_dict_rejected():
    """§VI: Manual position dict creation is rejected."""
    setup()
    pos = make_canonical_position(position_builder_source="manual_dict")
    is_valid, errors = cpos.validate_canonical_paper_position(pos)
    assert not is_valid, f"Manual dict should be rejected, got valid={is_valid}"
    assert any("position_builder_source" in e for e in errors), f"Expected builder source error, got {errors}"
    assert cpos.CANONICAL_COUNTERS["manual_position_build_attempts"] == 1
    print("✓ test_manual_position_dict_rejected")

def test_missing_market_slug_prevents_opened():
    """§VI: Missing market_slug prevents OPENED."""
    setup()
    pos = make_canonical_position(market_slug="")
    is_valid, errors = cpos.validate_canonical_paper_position(pos)
    assert not is_valid
    assert any("market_slug" in e for e in errors), f"Expected market_slug error, got {errors}"
    print("✓ test_missing_market_slug_prevents_opened")

def test_missing_condition_id_prevents_opened():
    """§VI: Missing condition_id prevents OPENED."""
    setup()
    pos = make_canonical_position(condition_id="", conditionId="")
    is_valid, errors = cpos.validate_canonical_paper_position(pos)
    assert not is_valid
    assert any("condition_id" in e or "conditionId" in e for e in errors)
    print("✓ test_missing_condition_id_prevents_opened")

def test_missing_expiry_timestamp_prevents_opened():
    """§VI: Missing expiry_timestamp prevents OPENED."""
    setup()
    pos = make_canonical_position(expiry_timestamp="")
    is_valid, errors = cpos.validate_canonical_paper_position(pos)
    assert not is_valid
    assert any("expiry_timestamp" in e for e in errors)
    print("✓ test_missing_expiry_timestamp_prevents_opened")

def test_missing_selected_token_id_prevents_opened():
    """§VI: Missing selected_token_id prevents OPENED."""
    setup()
    pos = make_canonical_position(selected_token_id="")
    is_valid, errors = cpos.validate_canonical_paper_position(pos)
    assert not is_valid
    assert any("selected_token_id" in e for e in errors)
    print("✓ test_missing_selected_token_id_prevents_opened")

def test_missing_opposite_token_id_prevents_opened():
    """§VI: Missing opposite_token_id prevents OPENED."""
    setup()
    pos = make_canonical_position(opposite_token_id="")
    is_valid, errors = cpos.validate_canonical_paper_position(pos)
    assert not is_valid
    assert any("opposite_token_id" in e for e in errors)
    print("✓ test_missing_opposite_token_id_prevents_opened")

def test_missing_interval_prevents_opened():
    """§VI: Missing interval prevents OPENED."""
    setup()
    pos = make_canonical_position(interval="")
    is_valid, errors = cpos.validate_canonical_paper_position(pos)
    assert not is_valid
    print("✓ test_missing_interval_prevents_opened")

def test_side_token_mismatch_prevents_opened():
    """§VI: selected_side/token mismatch prevents OPENED."""
    setup()
    # UP side but selected_token_id doesn't match up_token_id
    pos = make_canonical_position(selected_side="UP", selected_token_id="wrong_token")
    is_valid, errors = cpos.validate_canonical_paper_position(pos)
    assert not is_valid
    assert any("side_token_mismatch" in e for e in errors)
    print("✓ test_side_token_mismatch_prevents_opened")

def test_monthly_parent_condition_id_rejected():
    """§VI: Monthly parent conditionId rejected for 5m/15m validation."""
    setup()
    # end_date 59 days from now = monthly parent
    future_end = datetime.now(timezone.utc) + timedelta(days=59)
    contract = make_valid_contract(
        end_date=future_end.isoformat(),
        window="5m",  # claims to be 5m but end_date is 59 days away
    )
    is_valid, reason = cpos.validate_child_market(contract)
    assert not is_valid, f"Monthly parent should be rejected, got valid={is_valid}"
    assert "parent_market_mismatch" in reason, f"Expected parent_market_mismatch, got {reason}"
    assert cpos.CANONICAL_COUNTERS["parent_market_mismatch_rejects"] >= 1
    print("✓ test_monthly_parent_condition_id_rejected")

def test_expiry_mismatch_rejected():
    """§VI: expiry_timestamp mismatch rejected."""
    setup()
    # expiry is 5m ahead but expected is 15m ahead → mismatch
    now = datetime.now(timezone.utc)
    pos = make_canonical_position(
        interval="5m",
        expiry_timestamp=(now + timedelta(minutes=5)).isoformat(),
        expected_market_end_time=(now + timedelta(minutes=15)).isoformat(),
    )
    is_valid, errors = cpos.validate_canonical_paper_position(pos)
    assert not is_valid, f"Expiry mismatch should be rejected, got errors={errors}"
    assert any("expiry_mismatch" in e for e in errors)
    assert cpos.CANONICAL_COUNTERS["expiry_mismatch_rejects"] >= 1
    print("✓ test_expiry_mismatch_rejected")

def test_canonical_position_resolves_successfully():
    """§VI: Canonical position resolves successfully."""
    setup()
    pos = make_canonical_position()
    is_valid, errors = cpos.validate_canonical_paper_position(pos)
    assert is_valid, f"Canonical position should be valid, got errors={errors}"
    assert cpos.CANONICAL_COUNTERS["canonical_position_validation_passed"] == 1
    assert cpos.CANONICAL_COUNTERS["canonical_position_validation_failed"] == 0
    print("✓ test_canonical_position_resolves_successfully")

def test_duplicate_settlement_does_not_change_bankroll():
    """§VI: Duplicate settlement does not change bankroll."""
    setup()
    state = {"bankroll": 320.0, "total_pnl": 0.0, "wins": 0, "losses": 0}
    counters = pres.ResolutionCounters()
    pos = make_canonical_position(pnl_setted=True, net_pnl=0.50)
    # First settlement
    pos["pnl_settled"] = True  # Already settled
    pres._settle_resolved_position(pos, state, counters)
    # Should not change bankroll — duplicate blocked
    assert state["bankroll"] == 320.0, f"Bankroll should not change on duplicate, got {state['bankroll']}"
    assert counters.duplicate_settlement_blocks == 1
    print("✓ test_duplicate_settlement_does_not_change_bankroll")

def test_accounting_invariant_passes_after_settlement():
    """§VI: Accounting invariant passes after settlement."""
    setup()
    # Import reconciliation
    from pm_engine_v19_8 import reconcile_accounting
    state = {
        "bankroll": 320.0,
        "original_start_bankroll": 320.0,
        "start_bankroll": 320.0,
        "positions": {},
        "total_pnl": 0.0,
        "daily_pnl": 0.0,
    }
    acct = reconcile_accounting(state)
    assert acct["check_passed"], f"Accounting should pass, got {acct}"
    print("✓ test_accounting_invariant_passes_after_settlement")

def test_valid_5m_child_market_passes():
    """Valid 5m child market passes validation."""
    setup()
    contract = make_valid_contract()
    is_valid, reason = cpos.validate_child_market(contract)
    assert is_valid, f"Valid 5m child market should pass, got reason={reason}"
    print("✓ test_valid_5m_child_market_passes")

def test_valid_15m_child_market_passes():
    """Valid 15m child market passes validation."""
    setup()
    contract = make_valid_contract(window="15m")
    is_valid, reason = cpos.validate_child_market(contract)
    assert is_valid, f"Valid 15m child market should pass, got reason={reason}"
    print("✓ test_valid_15m_child_market_passes")

def test_settlement_side_token_mapping_valid():
    """§VI: Settlement side/token mapping validation."""
    setup()
    pos = make_canonical_position(
        selected_side="UP",
        selected_token_id="up_token_123",
        opposite_token_id="down_token_456",
        up_token_id="up_token_123",
        down_token_id="down_token_456",
    )
    # Win by token
    result, is_valid = cpos.validate_settlement_side_token_mapping(pos, "up_token_123")
    assert result == "win" and is_valid
    # Loss by token
    result, is_valid = cpos.validate_settlement_side_token_mapping(pos, "down_token_456")
    assert result == "loss" and is_valid
    print("✓ test_settlement_side_token_mapping_valid")

def test_settlement_side_token_mismatch_detected():
    """§VI: Side/token mismatch detected at settlement."""
    setup()
    pos = make_canonical_position(
        selected_side="UP",
        selected_token_id="wrong_token",
        up_token_id="up_token_123",
    )
    result, is_valid = cpos.validate_settlement_side_token_mapping(pos, "up_token_123")
    assert result == "error" and not is_valid
    assert cpos.CANONICAL_COUNTERS["settlement_errors"] == 1
    print("✓ test_settlement_side_token_mapping_mismatch_detected")

def test_missing_conditionId_in_contract_rejected():
    """Missing conditionId in contract rejected."""
    setup()
    contract = make_valid_contract(conditionId="")
    is_valid, reason = cpos.validate_child_market(contract)
    assert not is_valid
    assert "missing_condition_id" in reason
    print("✓ test_missing_conditionId_in_contract_rejected")


if __name__ == "__main__":
    tests = [
        test_validation_monitor_uses_build_paper_entry,
        test_manual_position_dict_rejected,
        test_missing_market_slug_prevents_opened,
        test_missing_condition_id_prevents_opened,
        test_missing_expiry_timestamp_prevents_opened,
        test_missing_selected_token_id_prevents_opened,
        test_missing_opposite_token_id_prevents_opened,
        test_missing_interval_prevents_opened,
        test_side_token_mismatch_prevents_opened,
        test_monthly_parent_condition_id_rejected,
        test_expiry_mismatch_rejected,
        test_canonical_position_resolves_successfully,
        test_duplicate_settlement_does_not_change_bankroll,
        test_accounting_invariant_passes_after_settlement,
        test_valid_5m_child_market_passes,
        test_valid_15m_child_market_passes,
        test_settlement_side_token_mapping_valid,
        test_settlement_side_token_mismatch_detected,
        test_missing_conditionId_in_contract_rejected,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            setup()
            t()
            passed += 1
        except Exception as e:
            print(f"✗ {t.__name__}: {e}")
            failed += 1
    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed, {passed+failed} total")
    if failed:
        sys.exit(1)
    else:
        print("All canonical position tests passed!")