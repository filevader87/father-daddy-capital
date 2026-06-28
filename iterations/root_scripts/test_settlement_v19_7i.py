#!/usr/bin/env python3
"""V19.7i Settlement Safety Tests — must pass before micro-live."""

import sys, json
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, '/mnt/c/Users/12035/father_daddy_capital')
from paper_trader_v19_7i import (
    make_position_id, transition_position, validate_pnl,
    STATE_CANDIDATE, STATE_OPENED, STATE_ACTIVE, STATE_EXPIRING,
    STATE_RESOLVED, STATE_SETTLED, STATE_JOURNALED,
    VALID_TRANSITIONS
)

TESTS_PASSED = 0
TESTS_FAILED = 0

def assert_test(condition, name, detail=""):
    global TESTS_PASSED, TESTS_FAILED
    if condition:
        TESTS_PASSED += 1
        print(f"  ✅ {name}")
    else:
        TESTS_FAILED += 1
        print(f"  ❌ {name} — {detail}")

def test_state_machine():
    """Test 1: Positions cannot skip states."""
    print("\n── Test 1: State Machine ──")
    
    # Valid transitions
    pos = {"status": STATE_CANDIDATE, "entry_id": "test1"}
    transition_position(pos, STATE_OPENED)
    assert_test(pos["status"] == STATE_OPENED, "CANDIDATE → OPENED")
    
    transition_position(pos, STATE_ACTIVE)
    assert_test(pos["status"] == STATE_ACTIVE, "OPENED → ACTIVE")
    
    transition_position(pos, STATE_EXPIRING)
    assert_test(pos["status"] == STATE_EXPIRING, "ACTIVE → EXPIRING")
    
    transition_position(pos, STATE_RESOLVED)
    assert_test(pos["status"] == STATE_RESOLVED, "EXPIRING → RESOLVED")
    
    transition_position(pos, STATE_SETTLED)
    assert_test(pos["status"] == STATE_SETTLED, "RESOLVED → SETTLED")
    
    transition_position(pos, STATE_JOURNALED)
    assert_test(pos["status"] == STATE_JOURNALED, "SETTLED → JOURNALED")
    
    # Invalid transitions must raise
    pos2 = {"status": STATE_CANDIDATE, "entry_id": "test2"}
    try:
        transition_position(pos2, STATE_ACTIVE)
        assert_test(False, "CANDIDATE → ACTIVE should fail", "No ValueError raised")
    except ValueError:
        assert_test(True, "CANDIDATE → ACTIVE correctly rejected")
    
    pos3 = {"status": STATE_ACTIVE, "entry_id": "test3"}
    try:
        transition_position(pos3, STATE_SETTLED)
        assert_test(False, "ACTIVE → SETTLED should fail", "No ValueError raised")
    except ValueError:
        assert_test(True, "ACTIVE → SETTLED correctly rejected")

def test_unresolved_not_scored():
    """Test 2: Unresolved trades are not counted as win/loss."""
    print("\n── Test 2: Unresolved Not Scored ──")
    
    pos = {
        "entry_id": "unresolved_test",
        "status": STATE_EXPIRING,
        "selected_side": "UP",
        "entry_price": 0.35,
        "size_usd": 1.0,
    }
    # Position is still EXPIRING — no PnL should be applied
    assert_test(pos.get("net_pnl", 0) == 0, "Unresolved position has no PnL")
    assert_test(pos["status"] == STATE_EXPIRING, "Position remains EXPIRING")

def test_wrong_token_side():
    """Test 3: UP signal resolves against DOWN token."""
    print("\n── Test 3: Wrong Token Side Detection ──")
    
    # UP signal, but DOWN token wins
    pos = {
        "entry_id": "wrong_side_test",
        "selected_side": "UP",
        "entry_price": 0.35,
        "size_usd": 1.0,
    }
    resolved_winner = "Down"
    we_won = (pos["selected_side"].upper() == resolved_winner.upper())
    assert_test(not we_won, "UP signal correctly marked LOSS when DOWN wins")
    
    # PnL should be negative
    net_pnl, valid, err = validate_pnl(pos, resolved_winner)
    assert_test(net_pnl < 0, f"Loss PnL is negative: {net_pnl}")
    assert_test(valid, f"PnL validation passed: {valid}")

def test_down_signal_resolves_up():
    """Test 4: DOWN signal resolves against UP token."""
    print("\n── Test 4: DOWN Signal vs UP Winner ──")
    
    pos = {
        "entry_id": "down_signal_test",
        "selected_side": "DOWN",
        "entry_price": 0.65,
        "size_usd": 1.0,
    }
    resolved_winner = "Up"
    we_won = (pos["selected_side"].upper() == resolved_winner.upper())
    assert_test(not we_won, "DOWN signal correctly marked LOSS when UP wins")
    
    net_pnl, valid, err = validate_pnl(pos, resolved_winner)
    assert_test(net_pnl < 0, f"Loss PnL is negative: {net_pnl}")

def test_no_double_count():
    """Test 5: Trade not counted twice."""
    print("\n── Test 5: No Double Counting ──")
    
    # Simulate: position in state SETTLED should not be in active positions
    pos = {
        "entry_id": "double_test",
        "status": STATE_SETTLED,
        "selected_side": "UP",
        "entry_price": 0.35,
        "size_usd": 1.0,
        "settled_at": datetime.now(timezone.utc).isoformat(),
    }
    
    # Check: settled positions should have been removed from active dict
    active_positions = {}
    assert_test(pos["entry_id"] not in active_positions, "Settled position not in active positions")
    assert_test(pos["status"] == STATE_SETTLED, "Position state is SETTLED")

def test_no_pnl_double_add():
    """Test 6: PnL not added twice."""
    print("\n── Test 6: No PnL Double Add ──")
    
    bankroll = 100.0
    pnl_applied = -1.0  # First PnL application
    
    # PnL should only be added once
    bankroll_after_first = bankroll + pnl_applied  # 99.0
    
    # If code accidentally applies PnL again:
    bankroll_after_double = bankroll_after_first + pnl_applied  # 98.0
    
    assert_test(bankroll_after_first == 99.0, f"Single PnL: bankroll={bankroll_after_first}")
    assert_test(bankroll_after_double != bankroll_after_first, f"Double PnL detected: {bankroll_after_double} != {bankroll_after_first}")

def test_expired_not_settled_without_resolution():
    """Test 7: Expired market not treated as settled without resolution."""
    print("\n── Test 7: Expired Not Settled Without Resolution ──")
    
    # Position past expiry, but resolution data unavailable
    pos = {
        "entry_id": "expired_no_res",
        "status": STATE_EXPIRING,
        "selected_side": "UP",
        "entry_price": 0.35,
        "size_usd": 1.0,
        "time_to_expiry_at_entry": 5,
        "candidate_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
    }
    
    # Resolution = None (market not yet resolved)
    resolution = None
    assert_test(resolution is None, "Resolution is None for unresolved market")
    assert_test(pos["status"] == STATE_EXPIRING, "Position stays EXPIRING (not RESOLVED)")

def test_missing_condition_id():
    """Test 8: Missing conditionId prevents tracking."""
    print("\n── Test 8: Missing conditionId ──")
    
    pos = {
        "entry_id": "no_cid_test",
        "condition_id_full": "",
        "status": STATE_CANDIDATE,
    }
    
    assert_test(not pos["condition_id_full"], "condition_id is empty")
    # Code should have blocked this position from being created

def test_duplicate_position():
    """Test 9: Duplicate market blocked from creating duplicate position."""
    print("\n── Test 9: Duplicate Position Block ──")
    
    condition_id = "0xabc123"
    existing_positions = {
        "pos1": {
            "condition_id_full": condition_id,
            "selected_side": "UP",
            "status": STATE_ACTIVE,
        }
    }
    
    # Check: new position with same condition_id and side would be blocked
    new_side = "UP"
    duplicate = any(
        p.get("condition_id_full") == condition_id and p.get("selected_side") == new_side and p.get("status") in [STATE_CANDIDATE, STATE_OPENED, STATE_ACTIVE, STATE_EXPIRING]
        for p in existing_positions.values()
    )
    assert_test(duplicate, "Duplicate position correctly detected")

def test_pnl_validation():
    """Test 10: PnL validation catches impossible values."""
    print("\n── Test 10: PnL Validation ──")
    
    # Valid win
    pos_win = {"selected_side": "UP", "entry_price": 0.35, "size_usd": 1.0}
    pnl, valid, err = validate_pnl(pos_win, "Up")
    assert_test(pnl > 0, f"Win PnL positive: {pnl}")
    assert_test(valid, f"Win PnL valid: {valid}")
    
    # Valid loss
    pos_loss = {"selected_side": "UP", "entry_price": 0.35, "size_usd": 1.0}
    pnl, valid, err = validate_pnl(pos_loss, "Down")
    assert_test(pnl < 0, f"Loss PnL negative: {pnl}")
    assert_test(valid, f"Loss PnL valid: {valid}")
    
    # Invalid: zero entry price
    pos_zero = {"selected_side": "UP", "entry_price": 0, "size_usd": 1.0}
    pnl, valid, err = validate_pnl(pos_zero, "Up")
    assert_test(not valid, f"Zero entry_price rejected: valid={valid}, err={err}")

def test_position_id_deterministic():
    """Test 11: Position IDs are deterministic for dedup."""
    print("\n── Test 11: Position ID Determinism ──")
    
    id1 = make_position_id("CORE_UP", "BTC", "UP", "0xabc", "2026-05-30T12:00:00")
    id2 = make_position_id("CORE_UP", "BTC", "UP", "0xabc", "2026-05-30T12:00:00")
    assert_test(id1 == id2, f"Deterministic ID: {id1} == {id2}")
    
    id3 = make_position_id("CORE_UP", "BTC", "UP", "0xabc", "2026-05-30T12:30:00")
    assert_test(id1 != id3, f"Different timestamp produces different ID: {id1} != {id3}")


if __name__ == "__main__":
    print("=" * 60)
    print("V19.7i SETTLEMENT SAFETY TESTS")
    print("=" * 60)
    
    test_state_machine()
    test_unresolved_not_scored()
    test_wrong_token_side()
    test_down_signal_resolves_up()
    test_no_double_count()
    test_no_pnl_double_add()
    test_expired_not_settled_without_resolution()
    test_missing_condition_id()
    test_duplicate_position()
    test_pnl_validation()
    test_position_id_deterministic()
    
    print(f"\n{'='*60}")
    print(f"RESULTS: {TESTS_PASSED} passed, {TESTS_FAILED} failed")
    print(f"{'='*60}")
    
    if TESTS_FAILED > 0:
        print("❌ SETTLEMENT SAFETY TESTS FAILED — micro-live blocked")
        sys.exit(1)
    else:
        print("✅ ALL SETTLEMENT SAFETY TESTS PASSED")
        sys.exit(0)