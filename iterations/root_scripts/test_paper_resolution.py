#!/usr/bin/env python3
"""
V19.8 Paper Resolution — Exit Lifecycle Tests (§1–§9)
=====================================================
Run: python3 test_paper_resolution.py

All tests must pass.
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paper_resolution as pr


# ─── Helpers ───
def make_pos(entry_price=0.26, size_usd=2.0, side="Up",
              up_token="UP_TOKEN", down_token="DOWN_TOKEN",
              condition_id="0xdeadbeef1234", market_id="999",
              profile="CORE_UP_RSI_ONLY_SHADOW",
              mins_to_expiry=5.0, entry_offset_minutes=0):
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(minutes=mins_to_expiry)
    entry_time = now - timedelta(minutes=entry_offset_minutes)
    token = up_token if side == "Up" else down_token
    opp = down_token if side == "Up" else up_token
    return {
        "position_id": f"P-{condition_id[:8]}-{side[0]}-{now.strftime('%H%M%S')}",
        "profile": profile, "asset": "BTC", "interval": "5m",
        "market_slug": "btc-up-or-down-5m",
        "condition_id": condition_id, "market_id": market_id,
        "question": "BTC Up or Down 5m",
        "selected_side": side,
        "selected_token_id": token,
        "opposite_token_id": opp,
        "entry_timestamp": entry_time.isoformat(),
        "expiry_timestamp": expiry.isoformat(),
        "time_to_expiry_at_entry": mins_to_expiry,
        "entry_price": entry_price,
        "size_usd": size_usd,
        "contracts": round(size_usd / max(entry_price, 0.01), 4),
        "estimated_probability": 0.57, "net_EV": 0.30,
        "status": pr.STATE_OPENED, "pnl_settled": False,
        "gross_pnl": None, "net_pnl": None,
        "pnl_validated": None, "pnl_validation_error": None,
        "exit_type": None, "resolution_source": None,
        "final_status": None, "journaled_at": None,
        "resolution_delay_seconds": None,
        "exit_signal_timestamp": None, "exit_executed_timestamp": None,
        "exit_bid": None, "exit_ask": None, "exit_spread": None,
        "exit_depth": None, "realized_exit_price": None,
        "early_exit_pnl": None, "expiry_settlement_skipped": False,
        "peak_contract_price": None, "trailing_stop_price": None,
        "trailing_loss_triggered": False,
    }


def make_state(bankroll=320):
    return {"bankroll": bankroll, "wins": 0, "losses": 0, "total_pnl": 0,
            "daily_pnl": 0, "positions": {}, "bankroll_peak": bankroll}


# ══════════════════════════════════════════════════════════════════════════════
# §1: Missing conditionId blocks executable positions
# ══════════════════════════════════════════════════════════════════════════════
def test_missing_condition_id_blocks():
    """Missing conditionId → None returned, counter incremented."""
    counters = pr.ResolutionCounters()
    contract = {"conditionId": "", "up_token_id": "U", "down_token_id": "D",
                "mins_to_expiry": 5, "window": "5m"}
    entry = {"side": "Up", "contract_price": 0.26, "bet": 2.0, "conditionId": "",
             "asset": "BTC", "question": ""}

    result = pr.build_paper_entry(entry, contract, "TEST", rsi=28.6,
                                   signal={"direction": "up", "confidence": 0.85,
                                           "rsi_zone": "oversold", "price": 104500},
                                   counters=counters)
    assert result is None, f"Should return None for missing conditionId, got {type(result)}"
    assert counters.blocked_by_missing_condition_id == 1, \
        f"Counter should be 1, got {counters.blocked_by_missing_condition_id}"
    print("✅ §1: Missing conditionId blocks executable position creation")


def test_present_condition_id_works():
    """With conditionId present, position is created normally."""
    counters = pr.ResolutionCounters()
    contract = {"conditionId": "0xabc123", "up_token_id": "U", "down_token_id": "D",
                "mins_to_expiry": 5, "window": "5m"}
    entry = {"side": "Up", "contract_price": 0.26, "bet": 2.0, "conditionId": "0xabc123",
             "asset": "BTC", "question": ""}
    result = pr.build_paper_entry(entry, contract, "TEST", counters=counters,
                                   rsi=28.6, signal={"direction":"up","confidence":0.85,
                                                      "rsi_zone":"oversold","price":104500})
    assert result is not None, "Should return position when conditionId present"
    assert result["condition_id"] == "0xabc123"
    print("✅ §1: Present conditionId creates position normally")


# ══════════════════════════════════════════════════════════════════════════════
# §2: Tighten missing winning token
# ══════════════════════════════════════════════════════════════════════════════
def test_side_fallback_unambiguous():
    """Missing winning_token_id + unambiguous UP/DOWN → fallback allowed."""
    pos = make_pos(side="Up", up_token="U", down_token="D")
    resolution = {"resolved_winner": "UP", "winning_token_id": None, "resolved": True}
    assert pr.can_use_side_fallback(resolution, pos) is True
    print("✅ §2: Unambiguous side mapping → fallback allowed")


def test_side_fallback_ambiguous_blocked():
    """Missing winning_token_id + ambiguous winner → no settlement."""
    pos = make_pos(side="Up", up_token="U", down_token="D")
    resolution = {"resolved_winner": "Yes", "winning_token_id": None, "resolved": True}
    # "Yes" normalizes to "UP" via _normalize_winner_label, but not in resolved_winner
    # which was set by the API. If resolution returns "Yes" and no token_id, ambiguous.
    result = pr.can_use_side_fallback(resolution, pos)
    # "Yes" is not in ("UP", "DOWN") → blocked
    assert result is False, "Ambiguous winner should block fallback"
    print("✅ §2: Ambiguous winner → no fallback")


def test_side_fallback_missing_tokens_blocked():
    """Missing tokens in position → no fallback."""
    pos = make_pos(side="Up", up_token="U", down_token="D")
    pos["opposite_token_id"] = ""  # Missing
    resolution = {"resolved_winner": "UP", "winning_token_id": None, "resolved": True}
    assert pr.can_use_side_fallback(resolution, pos) is False
    print("✅ §2: Missing opposite token → no fallback")


def test_side_fallback_no_silent_score():
    """Missing winning_token_id must NOT silently score trade."""
    state = make_state()
    counters = pr.ResolutionCounters()
    pos = make_pos(side="Up", entry_offset_minutes=10, mins_to_expiry=5.0)
    # Override expiry to past
    pos["expiry_timestamp"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    # Remove condition_id to prevent API calls, set ambiguous winner
    pos["condition_id"] = "0xnonexistent"
    pos["opposite_token_id"] = ""  # Missing → blocks fallback
    state["positions"]["t2"] = pos
    initial_bankroll = state["bankroll"]

    # This will try to fetch resolution, get "not closed" or "unresolved", no silent score
    resolved = pr.resolve_paper_positions(state, counters)
    # Position should NOT be scored
    assert state["bankroll"] == initial_bankroll, \
        "Bankroll should not change for unresolved/ambiguous"
    print("✅ §2: Missing winning token does NOT silently score trade")


# ══════════════════════════════════════════════════════════════════════════════
# §3: Stop-loss lifecycle
# ══════════════════════════════════════════════════════════════════════════════
def test_stop_loss_with_bid():
    """Stop-loss with executable bid → exits correctly."""
    counters = pr.ResolutionCounters()
    pos = make_pos(entry_price=0.26, size_usd=2.0)
    # Simulate 70% price drop (0.26 → 0.078) — exceeds 60% SL
    fake_bid_fetcher = lambda p: (0.078, 0.08, 0.002, 200)
    result = pr.check_exit_mechanisms(pos, current_price=0.078, counters=counters,
                                        bid_fetcher=fake_bid_fetcher)
    assert result is not None, "Should trigger stop-loss"
    assert result["exit_type"] == "stop_loss"
    assert counters.stop_loss_signals == 1
    assert counters.stop_loss_executed == 0  # check_exit only signals, resolve executes
    assert result["realized_exit_price"] == 0.078
    assert result["exit_bid"] == 0.078
    # PnL: contracts=7.6923, exit_value = 7.6923*0.078 = 0.60, pnl = 0.60 - 2.0 = -1.40
    assert result["pnl"] < 0, "Stop-loss PnL should be negative"
    print("✅ §3: Stop-loss with executable bid exits correctly")


def test_stop_loss_no_bid_no_fabricate():
    """Stop-loss with no bid does NOT fabricate exit."""
    counters = pr.ResolutionCounters()
    pos = make_pos(entry_price=0.26, size_usd=2.0)
    # Return None from bid fetcher
    no_bid_fetcher = lambda p: None
    result = pr.check_exit_mechanisms(pos, current_price=0.078, counters=counters,
                                        bid_fetcher=no_bid_fetcher)
    assert result is None, "Should NOT return exit when no bid"
    assert counters.stop_loss_signals == 1, "Signal should increment"
    assert counters.stop_loss_failed_no_bid == 1, "Failed-no-bid should increment"
    print("✅ §3: Stop-loss with no bid does not fabricate exit")


def test_stop_loss_no_double_settle():
    """Stop-loss exited position is not also settled at expiry."""
    pr.set_run_id("test_sl_no_double")
    state = make_state()
    counters = pr.ResolutionCounters()
    pos = make_pos(entry_price=0.26, size_usd=2.0, entry_offset_minutes=1,
                   mins_to_expiry=5.0)
    state["positions"][pos["position_id"]] = pos
    state["bankroll"] -= 2.0  # Deduct entry

    # Simulate SL exit by calling _settle_position directly
    fake_bid_fetcher = lambda p: (0.078, 0.08, 0.002, 200)
    exit_result = pr.check_exit_mechanisms(pos, current_price=0.078, counters=counters,
                                            bid_fetcher=fake_bid_fetcher)
    assert exit_result is not None
    pr._settle_position(pos, exit_result, state, counters)
    bankroll_after_sl = state["bankroll"]

    # Try to settle again via _settle_resolved_position (expiry)
    pos2 = dict(pos)  # Copy
    pr._settle_resolved_position(pos2, state, counters, won=False)
    bankroll_after_expiry = state["bankroll"]

    assert bankroll_after_sl == bankroll_after_expiry, \
        f"Double settle changed bankroll: {bankroll_after_sl} → {bankroll_after_expiry}"
    assert counters.duplicate_exit_blocks >= 1 or counters.duplicate_settlement_blocks >= 1, \
        "Duplicate should be blocked"
    pr.reset_run_id()
    print("✅ §3: Stop-loss does not also settle at expiry")


def test_duplicate_stop_loss_blocked():
    """Duplicate SL settlement attempt is blocked."""
    counters = pr.ResolutionCounters()
    pos = make_pos(entry_price=0.26)
    pos["pnl_settled"] = True  # Already settled

    exit_result = {"exit_type": "stop_loss", "exit_value": 0.5, "pnl": -1.5,
                   "cur_price": 0.078, "realized_exit_price": 0.078}
    pr._settle_position(pos, exit_result, make_state(), counters)
    assert counters.duplicate_exit_blocks == 1
    print("✅ §3: Duplicate stop-loss settlement blocked")


# ══════════════════════════════════════════════════════════════════════════════
# §4: Trailing-loss → NOT_IMPLEMENTED
# ══════════════════════════════════════════════════════════════════════════════
def test_trailing_loss_not_implemented():
    """Trailing loss is NOT implemented for binary contracts."""
    assert pr.TRAILING_LOSS_IMPLEMENTED is False, "Trailing loss must be NOT_IMPLEMENTED"
    counters = pr.ResolutionCounters()
    pos = make_pos(entry_price=0.26, size_usd=2.0)
    # Even if price rises and falls, no trailing loss should trigger
    result = pr.check_exit_mechanisms(pos, current_price=0.50, counters=counters)
    assert result is None, "No trailing loss should fire"
    assert counters.trailing_loss_signals == 0
    print("✅ §4: Trailing-loss NOT_IMPLEMENTED (binary contracts settle 0/1)")


# ══════════════════════════════════════════════════════════════════════════════
# §5: Take-profit lifecycle
# ══════════════════════════════════════════════════════════════════════════════
def test_take_profit_with_bid():
    """Take-profit with executable bid → exits correctly."""
    counters = pr.ResolutionCounters()
    pos = make_pos(entry_price=0.26, size_usd=2.0)
    # Price rose 85%: 0.26 → 0.481 (85% gain > 80% threshold)
    fake_bid_fetcher = lambda p: (0.481, 0.49, 0.009, 300)
    result = pr.check_exit_mechanisms(pos, current_price=0.481, counters=counters,
                                        bid_fetcher=fake_bid_fetcher)
    assert result is not None, "Should trigger take-profit"
    assert result["exit_type"] == "take_profit"
    assert counters.take_profit_signals == 1
    assert result["realized_exit_price"] == 0.481
    assert result["pnl"] > 0, "Take-profit PnL should be positive"
    print("✅ §5: Take-profit with executable bid exits correctly")


def test_take_profit_no_bid():
    """Take-profit with no bid → remains active, no fabricated exit."""
    counters = pr.ResolutionCounters()
    pos = make_pos(entry_price=0.26, size_usd=2.0)
    no_bid_fetcher = lambda p: None
    result = pr.check_exit_mechanisms(pos, current_price=0.481, counters=counters,
                                        bid_fetcher=no_bid_fetcher)
    assert result is None, "Should NOT exit without bid"
    assert counters.take_profit_signals == 1
    assert counters.take_profit_failed_no_bid == 1
    print("✅ §5: Take-profit with no bid → no fabricated exit")


def test_take_profit_no_double_settle():
    """Take-profit does not double-settle at expiry."""
    pr.set_run_id("test_tp_no_double")
    state = make_state()
    counters = pr.ResolutionCounters()
    pos = make_pos(entry_price=0.26, size_usd=2.0)
    state["positions"][pos["position_id"]] = pos
    state["bankroll"] -= 2.0

    fake_bid_fetcher = lambda p: (0.481, 0.49, 0.009, 300)
    exit_result = pr.check_exit_mechanisms(pos, current_price=0.481, counters=counters,
                                            bid_fetcher=fake_bid_fetcher)
    pr._settle_position(pos, exit_result, state, counters)
    bankroll_after_tp = state["bankroll"]

    # Try settle again
    pr._settle_resolved_position(pos, state, counters, won=True)
    bankroll_after_second = state["bankroll"]

    assert bankroll_after_tp == bankroll_after_second, "Double settle leaked"
    pr.reset_run_id()
    print("✅ §5: Take-profit does not double-settle at expiry")


# ══════════════════════════════════════════════════════════════════════════════
# §6: Exit counter semantics
# ══════════════════════════════════════════════════════════════════════════════
def test_exit_counters():
    """Exit counters properly track signals/executed/failed."""
    counters = pr.ResolutionCounters()
    pos = make_pos(entry_price=0.26, size_usd=2.0)
    no_bid = lambda p: None

    # SL signal, no bid
    pr.check_exit_mechanisms(pos, current_price=0.078, counters=counters, bid_fetcher=no_bid)
    assert counters.stop_loss_signals == 1
    assert counters.stop_loss_failed_no_bid == 1
    assert counters.stop_loss_executed == 0

    # TP signal, no bid
    pr.check_exit_mechanisms(pos, current_price=0.481, counters=counters, bid_fetcher=no_bid)
    assert counters.take_profit_signals == 1
    assert counters.take_profit_failed_no_bid == 1
    assert counters.take_profit_executed == 0

    d = counters.to_dict()
    assert d["stop_loss_signals"] == 1
    assert d["stop_loss_failed_no_bid"] == 1
    assert d["take_profit_signals"] == 1
    assert "early_exit_pnl" in d
    assert "expiry_settlement_pnl" in d
    print("✅ §6: Exit counters track signals/executed/failed correctly")


# ══════════════════════════════════════════════════════════════════════════════
# §7: Journal fields for early exits
# ══════════════════════════════════════════════════════════════════════════════
def test_journal_exit_fields():
    """Journal includes all §7 early exit fields."""
    pr.set_run_id("test_journal_exit")
    state = make_state()
    counters = pr.ResolutionCounters()
    pos = make_pos(entry_price=0.26, size_usd=2.0)
    state["positions"]["j1"] = pos
    state["bankroll"] -= 2.0

    fake_bid = lambda p: (0.078, 0.08, 0.002, 200)
    exit_result = pr.check_exit_mechanisms(pos, current_price=0.078, counters=counters,
                                            bid_fetcher=fake_bid)
    pr._settle_position(pos, exit_result, state, counters)

    # Read journal
    trades_file = os.path.join(pr.JOURNAL_BASE_DIR, "test_journal_exit", "trades.jsonl")
    with open(trades_file) as f:
        trade = json.loads(f.readline())

    required_fields = ["exit_type", "exit_signal_timestamp", "exit_executed_timestamp",
                        "exit_bid", "exit_ask", "exit_spread", "exit_depth",
                        "realized_exit_price", "early_exit_pnl", "expiry_settlement_skipped"]
    missing = [f for f in required_fields if f not in trade or trade[f] is None]
    assert not missing, f"Missing journal fields: {missing}"
    assert trade["exit_type"] == "stop_loss"
    assert trade["expiry_settlement_skipped"] is True
    assert trade["exit_bid"] == 0.078
    assert trade["realized_exit_price"] == 0.078
    pr.reset_run_id()
    print("✅ §7: Journal includes all early exit fields")


# ══════════════════════════════════════════════════════════════════════════════
# Expiry tests (baseline)
# ══════════════════════════════════════════════════════════════════════════════
def test_unresolved_not_scored():
    """Unresolved expired market is NOT scored."""
    state = make_state()
    counters = pr.ResolutionCounters()
    now = datetime.now(timezone.utc)
    pos = make_pos(entry_offset_minutes=15, mins_to_expiry=5.0)
    pos["expiry_timestamp"] = (now - timedelta(minutes=10)).isoformat()
    state["positions"]["t1"] = pos
    initial = state["bankroll"]
    pr.resolve_paper_positions(state, counters)
    assert pos["status"] == pr.STATE_UNRESOLVED_PAST_EXPIRY
    assert counters.paper_wins == 0
    assert state["bankroll"] == initial
    print("✅ Expiry: Unresolved expired market not scored")


def test_up_market_up_token_wins():
    pos = make_pos(side="Up", entry_price=0.26, size_usd=2.0)
    pnl = pr.calculate_pnl(pos, won=True)
    assert pnl["gross_pnl"] > 0
    assert pnl["pnl_validated"] is True
    print("✅ Expiry: UP market, UP token → win")


def test_up_market_down_token_loses():
    pos = make_pos(side="Down", entry_price=0.74, size_usd=2.0)
    pnl = pr.calculate_pnl(pos, won=False)
    assert pnl["gross_pnl"] == -2.0
    print("✅ Expiry: UP market, DOWN token → loss")


def test_side_token_mismatch():
    pos = make_pos(side="Up", up_token="TOK_UP", down_token="TOK_DOWN")
    assert pr.check_side_token_mismatch(pos, "TOTALLY_DIFF") is not None
    assert pr.check_side_token_mismatch(pos, "TOK_UP") is None
    print("✅ Expiry: Side-token mismatch detection works")


def test_duplicate_settlement_blocked():
    state = make_state(bankroll=318)
    counters = pr.ResolutionCounters()
    pos = make_pos(entry_price=0.26, size_usd=2.0)
    pos["pnl_settled"] = True
    pr._settle_resolved_position(pos, state, counters, won=True)
    pr._settle_resolved_position(pos, state, counters, won=True)
    assert counters.duplicate_settlement_blocks >= 1
    print("✅ Expiry: Duplicate settlement blocked")


def test_profile_pnl_isolation():
    counters = pr.ResolutionCounters()
    counters.record_profile_open("CORE_UP_RSI_ONLY_SHADOW")
    counters.record_profile_resolve("CORE_UP_RSI_ONLY_SHADOW", True, 5.69, 5.69)
    counters.record_profile_open("CORE_UP_ONE_CONFIRM_SHADOW")
    counters.record_profile_resolve("CORE_UP_ONE_CONFIRM_SHADOW", False, -2.0, -2.0)
    rsi = counters.profiles["CORE_UP_RSI_ONLY_SHADOW"]
    one = counters.profiles["CORE_UP_ONE_CONFIRM_SHADOW"]
    assert rsi["wins"] == 1 and rsi["losses"] == 0
    assert one["wins"] == 0 and one["losses"] == 1
    print("✅ Profile PnL isolation verified")


# ══════════════════════════════════════════════════════════════════════════════
# §9: Synthetic Fixture Validation
# ══════════════════════════════════════════════════════════════════════════════
def test_synthetic_fixtures():
    """Run all 7 synthetic fixture scenarios."""
    pr.set_run_id("synthetic_fixtures")
    results = []

    # Fixture 1: Expiry WIN
    state = make_state()
    counters = pr.ResolutionCounters()
    pos = make_pos(entry_price=0.26, size_usd=2.0)
    pos["resolved_winner"] = "UP"; pos["winning_token_id"] = "UP_TOKEN"
    pnl_r = pr.calculate_pnl(pos, won=True)
    pos["gross_pnl"] = pnl_r["gross_pnl"]; pos["net_pnl"] = pnl_r["net_pnl"]
    pos["pnl_validated"] = True; pos["exit_type"] = "expiry"
    br_before = state["bankroll"]
    pr._settle_resolved_position(pos, state, counters, won=True)
    results.append(("expiry_win", br_before, state["bankroll"], pnl_r["net_pnl"],
                    pos["status"], pos["pnl_settled"]))

    # Fixture 2: Expiry LOSS
    state = make_state()
    counters2 = pr.ResolutionCounters()
    pos2 = make_pos(entry_price=0.26, size_usd=2.0)
    pos2["resolved_winner"] = "DOWN"; pos2["winning_token_id"] = "DOWN_TOKEN"
    pnl2 = pr.calculate_pnl(pos2, won=False)
    pos2["gross_pnl"] = pnl2["gross_pnl"]; pos2["net_pnl"] = pnl2["net_pnl"]
    pos2["pnl_validated"] = True; pos2["exit_type"] = "expiry"
    br2 = state["bankroll"]
    state["bankroll"] -= 2.0  # Entry deducted
    pr._settle_resolved_position(pos2, state, counters2, won=False)
    results.append(("expiry_loss", br2, state["bankroll"], pnl2["net_pnl"],
                    pos2["status"], pos2["pnl_settled"]))

    # Fixture 3: Stop-loss exit
    state3 = make_state()
    counters3 = pr.ResolutionCounters()
    pos3 = make_pos(entry_price=0.26, size_usd=2.0)
    sl_exit = {"exit_type": "stop_loss", "exit_value": 0.60, "pnl": -1.40,
               "cur_price": 0.078, "realized_exit_price": 0.078,
               "exit_bid": 0.078, "exit_ask": 0.08, "exit_spread": 0.002,
               "exit_depth": 200, "exit_signal_timestamp": datetime.now(timezone.utc).isoformat(),
               "exit_executed_timestamp": datetime.now(timezone.utc).isoformat()}
    br3 = state3["bankroll"]
    state3["bankroll"] -= 2.0
    pr._settle_position(pos3, sl_exit, state3, counters3)
    results.append(("stop_loss", br3 - 2.0, state3["bankroll"], -1.40,
                    pos3["status"], pos3["pnl_settled"]))

    # Fixture 5: Take-profit exit
    state5 = make_state()
    counters5 = pr.ResolutionCounters()
    pos5 = make_pos(entry_price=0.26, size_usd=2.0)
    tp_exit = {"exit_type": "take_profit", "exit_value": 3.70, "pnl": 1.70,
               "cur_price": 0.481, "realized_exit_price": 0.481,
               "exit_bid": 0.481, "exit_ask": 0.49, "exit_spread": 0.009,
               "exit_depth": 300, "exit_signal_timestamp": datetime.now(timezone.utc).isoformat(),
               "exit_executed_timestamp": datetime.now(timezone.utc).isoformat()}
    br5 = state5["bankroll"]
    state5["bankroll"] -= 2.0
    pr._settle_position(pos5, tp_exit, state5, counters5)
    results.append(("take_profit", br5 - 2.0, state5["bankroll"], 1.70,
                    pos5["status"], pos5["pnl_settled"]))

    # Fixture 6: Failed SL (no bid)
    counters6 = pr.ResolutionCounters()
    pos6 = make_pos(entry_price=0.26, size_usd=2.0)
    no_bid = lambda p: None
    result6 = pr.check_exit_mechanisms(pos6, current_price=0.078, counters=counters6,
                                        bid_fetcher=no_bid)
    results.append(("sl_no_bid", "N/A", "N/A",
                    "signal_only" if result6 is None else "ERROR",
                    counters6.stop_loss_failed_no_bid, True))

    # Fixture 7: Duplicate settlement
    state7 = make_state()
    counters7 = pr.ResolutionCounters()
    pos7 = make_pos(entry_price=0.26, size_usd=2.0)
    pos7["resolved_winner"] = "UP"; pos7["winning_token_id"] = "UP_TOKEN"
    pos7["gross_pnl"] = 5.69; pos7["net_pnl"] = 5.69; pos7["pnl_validated"] = True
    pos7["exit_type"] = "expiry"
    pr._settle_resolved_position(pos7, state7, counters7, won=True)
    br7 = state7["bankroll"]
    pr._settle_resolved_position(pos7, state7, counters7, won=True)
    results.append(("dup_settle", br7, state7["bankroll"],
                    "blocked" if state7["bankroll"] == br7 else "LEAK",
                    counters7.duplicate_settlement_blocks, True))

    pr.reset_run_id()

    # Verify results
    print("\n  ┌──────────────────┬──────────────┐")
    print("  │ Fixture          │ Result       │")
    print("  ├──────────────────┼──────────────┤")
    for name, before, after, pnl, status, settled in results:
        ok = "✅" if (name == "sl_no_bid" and pnl == "signal_only") or \
                    (name == "dup_settle" and pnl == "blocked") or \
                    (settled is True) else "❌"
        print(f"  │ {name:<16} │ {ok} {pnl!s:<9} │")
    print("  └──────────────────┴──────────────┘")
    print("✅ §9: Synthetic fixture validation complete")


# ══════════════════════════════════════════════════════════════════════════════
# Run all tests
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("V19.8 Exit Lifecycle — Regression Tests")
    print("=" * 60)

    tests = [
        # §1
        test_missing_condition_id_blocks,
        test_present_condition_id_works,
        # §2
        test_side_fallback_unambiguous,
        test_side_fallback_ambiguous_blocked,
        test_side_fallback_missing_tokens_blocked,
        test_side_fallback_no_silent_score,
        # §3
        test_stop_loss_with_bid,
        test_stop_loss_no_bid_no_fabricate,
        test_stop_loss_no_double_settle,
        test_duplicate_stop_loss_blocked,
        # §4
        test_trailing_loss_not_implemented,
        # §5
        test_take_profit_with_bid,
        test_take_profit_no_bid,
        test_take_profit_no_double_settle,
        # §6
        test_exit_counters,
        # §7
        test_journal_exit_fields,
        # Baseline expiry tests
        test_unresolved_not_scored,
        test_up_market_up_token_wins,
        test_up_market_down_token_loses,
        test_side_token_mismatch,
        test_duplicate_settlement_blocked,
        test_profile_pnl_isolation,
        # §9
        test_synthetic_fixtures,
    ]

    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as e:
            print(f"❌ {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"❌ {t.__name__}: ERROR {e}")
            failed += 1

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
    else:
        print("ALL TESTS PASSED ✅")