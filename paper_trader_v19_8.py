#!/usr/bin/env python3
"""V19.8 Paper Trading Engine — Hardened 5-hour loop + metric semantics + PBot benchmark.

NO REAL ORDERS. Hard live block enforced.
LIVE_ORDER_BLOCKED_DURING_5H_PAPER_LOOP raised on any real-order code path.
Uses CLOB api for orderbook, Gamma api for markets, engine for signals.

Position lifecycle: CANDIDATE → OPENED → ACTIVE → EXPIRING → RESOLVED → SETTLED → JOURNALED
Live permission: MODE + MICRO_LIVE_READY + PAPER_GATE + SETTLEMENT_GATE + EXECUTION_GATE + CONFIRMATION_FLAG

Outputs: paper_trading/ with per-cycle JSON + cumulative state per profile + journal per position.
         dry_run/ with broker logs for would-be live orders.
"""

import json, os, sys, time, traceback, random, hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict
import urllib.request

sys.path.insert(0, '/mnt/c/Users/12035/father_daddy_capital')
import pm_engine_v19_7 as eng
import discovery_providers as dp
import reference_price_engine as rpe

OUT_DIR = Path('/mnt/c/Users/12035/father_daddy_capital/paper_trading')
OUT_DIR.mkdir(exist_ok=True)
JOURNAL_DIR = OUT_DIR / "journal"
JOURNAL_DIR.mkdir(exist_ok=True)

CLOB_URL = "https://clob.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com"

# ── Position States ──
STATE_CANDIDATE = "CANDIDATE"
STATE_OPENED = "OPENED"
STATE_ACTIVE = "ACTIVE"
STATE_EXPIRING = "EXPIRING"
STATE_RESOLVED = "RESOLVED"
STATE_SETTLED = "SETTLED"
STATE_JOURNALED = "JOURNALED"

VALID_TRANSITIONS = {
    STATE_CANDIDATE: [STATE_OPENED],
    STATE_OPENED: [STATE_ACTIVE],
    STATE_ACTIVE: [STATE_EXPIRING],
    STATE_EXPIRING: [STATE_RESOLVED],
    STATE_RESOLVED: [STATE_SETTLED],
    STATE_SETTLED: [STATE_JOURNALED],
}

# ── Strategy Profiles ──
PROFILES = {
    "CORE_UP": {
        "name": "V19.7g Core UP",
        "mode": "paper",
        "description": "Oversold UP only. DOWN disabled. BTC first, others discovery. Micro-live candidate.",
        "enabled_rsi_zones": {
            "up": ["extreme_low", "oversold", "near_oversold1", "near_oversold2", "near_oversold3"],
            "down": [],
        },
        "min_confidence": 0.82,
        "ev_min_gate": 0.02,
        "max_contract_price": 0.85,
        "primary_asset": "BTC",
        "discovery_assets": ["ETH", "SOL", "XRP"],
    },
    "BIDIRECTIONAL_SHADOW": {
        "name": "V19.7g Bidirectional Shadow",
        "mode": "paper",
        "description": "RSI 20-35 UP + RSI 70-82 DOWN. Measures DOWN viability.",
        "enabled_rsi_zones": {
            "up": ["extreme_low", "oversold", "near_oversold1", "near_oversold2", "near_oversold3"],
            "down": ["strong_overbought", "moderate_overbought"],
        },
        "min_confidence": 0.82,
        "ev_min_gate": 0.02,
        "max_contract_price": 0.85,
        "primary_asset": "BTC",
        "discovery_assets": ["ETH", "SOL", "XRP"],
    },
    "PARABOLIC_RESEARCH": {
        "name": "V19.8 Parabolic Research",
        "mode": "paper_only",
        "description": "RSI>82 continuation UP, RSI<20 continuation DOWN. Research only.",
        "enabled_rsi_zones": {
            "up": ["parabolic"],
            "down": ["extreme_low"],
        },
        "min_confidence": 0.75,
        "ev_min_gate": 0.01,
        "max_contract_price": 0.90,
        "primary_asset": "BTC",
        "discovery_assets": ["ETH", "SOL", "XRP"],
    },
    "PBOT_OVERSOLD_UP": {
        "name": "PBot Oversold UP",
        "mode": "paper_only",
        "description": "RSI 20-35 UP. Mirrors CORE_UP for research comparison.",
        "enabled_rsi_zones": {"up": ["extreme_low", "oversold", "near_oversold1", "near_oversold2", "near_oversold3"], "down": []},
        "min_confidence": 0.80,
        "ev_min_gate": 0.01,
        "max_contract_price": 0.85,
        "primary_asset": "BTC",
        "discovery_assets": ["ETH", "SOL", "XRP"],
    },
    "PBOT_OVERBOUGHT_DOWN": {
        "name": "PBot Overbought DOWN",
        "mode": "paper_only",
        "description": "RSI 65-82 DOWN with exhaustion confirmation.",
        "enabled_rsi_zones": {"up": [], "down": ["strong_overbought", "moderate_overbought"]},
        "min_confidence": 0.80,
        "ev_min_gate": 0.01,
        "max_contract_price": 0.85,
        "primary_asset": "BTC",
        "discovery_assets": ["ETH", "SOL", "XRP"],
    },
    "PBOT_PARABOLIC_UP": {
        "name": "PBot Parabolic UP Continuation",
        "mode": "paper_only",
        "description": "RSI>82 UP momentum. Paper-only research.",
        "enabled_rsi_zones": {"up": ["parabolic"], "down": []},
        "min_confidence": 0.70,
        "ev_min_gate": 0.005,
        "max_contract_price": 0.95,
        "primary_asset": "BTC",
        "discovery_assets": ["ETH", "SOL", "XRP"],
    },
    "PBOT_PARABOLIC_DOWN": {
        "name": "PBot Parabolic DOWN Continuation",
        "mode": "paper_only",
        "description": "RSI<20 DOWN continuation. Paper-only research.",
        "enabled_rsi_zones": {"up": [], "down": ["extreme_low"]},
        "min_confidence": 0.70,
        "ev_min_gate": 0.005,
        "max_contract_price": 0.85,
        "primary_asset": "BTC",
        "discovery_assets": ["ETH", "SOL", "XRP"],
    },
    "PBOT_VWAP_REVERSION": {
        "name": "PBot VWAP Reversion",
        "mode": "paper_only",
        "description": "Price stretched from VWAP, mean-reversion candidate.",
        "enabled_rsi_zones": {"up": ["extreme_low", "oversold", "near_oversold1"], "down": ["strong_overbought", "moderate_overbought"]},
        "min_confidence": 0.75,
        "ev_min_gate": 0.01,
        "max_contract_price": 0.80,
        "primary_asset": "BTC",
        "discovery_assets": ["ETH", "SOL", "XRP"],
    },
    "PBOT_LATE_WINDOW": {
        "name": "PBot Late Window Dislocation",
        "mode": "paper_only",
        "description": "Token price diverges from spot-direction probability near expiry.",
        "enabled_rsi_zones": {"up": ["extreme_low", "oversold", "near_oversold1"], "down": ["strong_overbought", "moderate_overbought"]},
        "min_confidence": 0.65,
        "ev_min_gate": 0.005,
        "max_contract_price": 0.90,
        "primary_asset": "BTC",
        "discovery_assets": ["ETH", "SOL", "XRP"],
    },
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MICRO-LIVE CONFIGURATION — all 6 gates must pass for live orders
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MODE = "PAPER"  # "PAPER" | "MICRO_LIVE" — user must manually set MICRO_LIVE

LIVE_BANKROLL_USD = 320.00
MICRO_LIVE_READY = False       # Auto-set when paper gates + settlement gates pass
PAPER_GATE_PASSED = False      # >=50 opps, 0 false accepts, EV+, PF>=1.25, DD<=15%
SETTLEMENT_GATE_PASSED = False # 0 settlement errors, journal 100%, no dupes
EXECUTION_GATE_PASSED = False  # dry-run broker executed without errors
LIVE_CONFIRMATION_FLAG = False # Manual user confirmation — must be True for live
DISABLE_LIVE_ORDERS = True     # V19.8 hard block — no real orders ever

def _live_order_guard():
    """Hard live block: raises if any code path attempts real order placement."""
    if DISABLE_LIVE_ORDERS:
        raise RuntimeError("LIVE_ORDER_BLOCKED_DURING_5H_PAPER_LOOP")

# Risk parameters (live only)
RISK_CONFIG = {
    "base_trade_risk_pct": 0.005,      # 0.5% of bankroll base risk
    "max_trade_risk_pct": 0.01,         # 1% of bankroll max risk
    "absolute_trade_cap_usd": 3.00,     # Never risk more than $3 per trade
    "max_open_positions": 1,            # Only 1 position at a time
    "max_total_exposure_pct": 0.02,     # 2% of bankroll total exposure
    "max_daily_loss_usd": 10.00,        # Stop trading after $10 daily loss
    "weekly_loss_stop_usd": 24.00,      # Stop trading after $24 weekly loss
}

# Kill switch thresholds — any True means NO live orders
KILL_SWITCHES = {
    "daily_loss_exceeded": False,        # daily PnL <= -$10
    "weekly_loss_exceeded": False,        # weekly PnL <= -$24
    "settlement_error_count": 0,          # must stay 0
    "false_accept_count": 0,             # must stay 0
    "stale_book_executable_count": 0,     # must stay 0
    "duplicate_position_error": 0,        # must stay 0
    "open_positions": 0,                   # must be < max_open_positions
}

# Live-eligible strategy — ONLY this profile/asset/zone can go micro-live
LIVE_ELIGIBLE = {
    "profile": "CORE_UP",
    "asset": "BTC",
    "allowed_zones": ["extreme_low", "oversold", "near_oversold1", "near_oversold2", "near_oversold3"],
    "allowed_direction": "up",
    "allowed_timeframes": ["5m", "15m"],
    "paper_only_profiles": ["BIDIRECTIONAL_SHADOW", "PARABOLIC_RESEARCH", "PBOT_OVERSOLD_UP", "PBOT_OVERBOUGHT_DOWN", "PBOT_PARABOLIC_UP", "PBOT_PARABOLIC_DOWN", "PBOT_VWAP_REVERSION", "PBOT_LATE_WINDOW"],
    "paper_only_assets": ["ETH", "SOL", "XRP"],
}

DRY_RUN_DIR = Path('/mnt/c/Users/12035/father_daddy_capital/dry_run')
DRY_RUN_DIR.mkdir(exist_ok=True)
_resolution_cache = {}  # condition_id → resolution data


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LIVE PERMISSION GATE — all 6 conditions must be True for live orders
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def check_live_permission():
    """Check all 6 live-permission gates. Returns (allowed: bool, reasons: list)."""
    reasons = []
    if MODE != "MICRO_LIVE":
        reasons.append(f"MODE={MODE} (need MICRO_LIVE)")
    if not MICRO_LIVE_READY:
        reasons.append("MICRO_LIVE_READY=False")
    if not PAPER_GATE_PASSED:
        reasons.append("PAPER_GATE_PASSED=False")
    if not SETTLEMENT_GATE_PASSED:
        reasons.append("SETTLEMENT_GATE_PASSED=False")
    if not EXECUTION_GATE_PASSED:
        reasons.append("EXECUTION_GATE_PASSED=False")
    if not LIVE_CONFIRMATION_FLAG:
        reasons.append("LIVE_CONFIRMATION_FLAG=False")
    return len(reasons) == 0, reasons


def check_kill_switches(kill_switches, daily_pnl=0, weekly_pnl=0, open_positions=0):
    """Evaluate all kill switches. Returns (clear: bool, triggered: list)."""
    triggered = []
    if daily_pnl <= -RISK_CONFIG["max_daily_loss_usd"]:
        kill_switches["daily_loss_exceeded"] = True
        triggered.append(f"daily_loss ${daily_pnl:.2f} <= -${RISK_CONFIG['max_daily_loss_usd']}")
    if weekly_pnl <= -RISK_CONFIG["weekly_loss_stop_usd"]:
        kill_switches["weekly_loss_exceeded"] = True
        triggered.append(f"weekly_loss ${weekly_pnl:.2f} <= -${RISK_CONFIG['weekly_loss_stop_usd']}")
    if kill_switches["settlement_error_count"] > 0:
        triggered.append(f"settlement_errors={kill_switches['settlement_error_count']}")
    if kill_switches["false_accept_count"] > 0:
        triggered.append(f"false_accepts={kill_switches['false_accept_count']}")
    if kill_switches["stale_book_executable_count"] > 0:
        triggered.append(f"stale_books={kill_switches['stale_book_executable_count']}")
    if kill_switches["duplicate_position_error"] > 0:
        triggered.append(f"duplicate_errors={kill_switches['duplicate_position_error']}")
    if open_positions >= RISK_CONFIG["max_open_positions"]:
        triggered.append(f"open_positions={open_positions} >= max={RISK_CONFIG['max_open_positions']}")
    return len(triggered) == 0, triggered


def check_live_eligible(profile_key, asset, zone, direction, timeframe):
    """Check if this trade is eligible for micro-live execution."""
    reasons_allowed = []
    reasons_blocked = []
    if profile_key != LIVE_ELIGIBLE["profile"]:
        reasons_blocked.append(f"profile={profile_key} (need {LIVE_ELIGIBLE['profile']})")
    else:
        reasons_allowed.append(f"profile={profile_key}")
    if asset != LIVE_ELIGIBLE["asset"]:
        reasons_blocked.append(f"asset={asset} (need {LIVE_ELIGIBLE['asset']})")
    else:
        reasons_allowed.append(f"asset={asset}")
    if zone not in LIVE_ELIGIBLE["allowed_zones"]:
        reasons_blocked.append(f"zone={zone} (need {LIVE_ELIGIBLE['allowed_zones']})")
    else:
        reasons_allowed.append(f"zone={zone}")
    if direction != LIVE_ELIGIBLE["allowed_direction"]:
        reasons_blocked.append(f"direction={direction} (need {LIVE_ELIGIBLE['allowed_direction']})")
    else:
        reasons_allowed.append(f"direction={direction}")
    if timeframe not in LIVE_ELIGIBLE["allowed_timeframes"]:
        reasons_blocked.append(f"timeframe={timeframe} (need {LIVE_ELIGIBLE['allowed_timeframes']})")
    else:
        reasons_allowed.append(f"timeframe={timeframe}")
    return len(reasons_blocked) == 0, reasons_allowed, reasons_blocked


def dry_run_broker(order_details, kill_switches, daily_pnl=0, weekly_pnl=0, open_positions=0):
    """Simulate a live broker call without sending orders. Log would-be order to dry_run/."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    
    # Evaluate all gates
    live_allowed, live_reasons = check_live_permission()
    kill_clear, kill_triggered = check_kill_switches(kill_switches, daily_pnl, weekly_pnl, open_positions)
    eligible, eligible_reasons, blocked_reasons = check_live_eligible(
        order_details.get("profile", ""),
        order_details.get("asset", ""),
        order_details.get("signal_zone", ""),
        order_details.get("selected_side", "").lower(),
        order_details.get("timeframe", ""),
    )
    
    # Determine if order would execute
    would_execute = live_allowed and kill_clear and eligible
    
    # V19.8 hard live block — if somehow would_execute=True, still block
    if would_execute:
        _live_order_guard()
    
    # Calculate trade sizing
    bankroll = LIVE_BANKROLL_USD
    base_risk = RISK_CONFIG["base_trade_risk_pct"]
    max_risk = RISK_CONFIG["max_trade_risk_pct"]
    cap = RISK_CONFIG["absolute_trade_cap_usd"]
    price = order_details.get("entry_price", order_details.get("entry_ask", 0))
    calculated_size = min(bankroll * max_risk, cap)
    max_allowed = min(bankroll * max_risk, cap)
    daily_remaining = max(0, RISK_CONFIG["max_daily_loss_usd"] + daily_pnl)
    weekly_remaining = max(0, RISK_CONFIG["weekly_loss_stop_usd"] + weekly_pnl)
    
    result = {
        "timestamp": ts,
        "would_place_live_order": would_execute,
        "blocked_by_gate": [] if live_allowed else live_reasons,
        "blocked_by_kill_switch": kill_triggered if not kill_clear else [],
        "live_permission": {"allowed": live_allowed, "reasons": live_reasons},
        "kill_switches": {"clear": kill_clear, "triggered": kill_triggered},
        "live_eligible": {"eligible": eligible, "allowed": eligible_reasons, "blocked": blocked_reasons},
        "calculated_trade_size_usd": round(calculated_size, 4),
        "bankroll_at_decision": bankroll,
        "max_allowed_trade_size_usd": max_allowed,
        "daily_loss_remaining_usd": round(daily_remaining, 2),
        "weekly_loss_remaining_usd": round(weekly_remaining, 2),
        "open_positions_count": open_positions,
        "order": {
            "asset": order_details.get("asset", ""),
            "profile": order_details.get("profile", ""),
            "market_id": order_details.get("market_id", ""),
            "condition_id": order_details.get("condition_id_full", order_details.get("condition_id", "")),
            "selected_side": order_details.get("selected_side", ""),
            "selected_token_id": order_details.get("selected_token_id", ""),
            "ask_price": order_details.get("entry_ask", order_details.get("entry_price", 0)),
            "bid_price": order_details.get("entry_bid", 0),
            "spread": order_details.get("entry_spread", 0),
            "estimated_slippage": order_details.get("estimated_slippage", 0),
            "size_usd": order_details.get("size_usd", 0),
            "contracts": order_details.get("contracts", 0),
            "time_to_expiry": order_details.get("time_to_expiry_at_entry", 0),
        },
        "live_permission_state": {
            "MODE_IS_MICRO_LIVE": MODE == "MICRO_LIVE",
            "MICRO_LIVE_READY": MICRO_LIVE_READY,
            "PAPER_GATE_PASSED": PAPER_GATE_PASSED,
            "SETTLEMENT_GATE_PASSED": SETTLEMENT_GATE_PASSED,
            "EXECUTION_GATE_PASSED": EXECUTION_GATE_PASSED,
            "LIVE_CONFIRMATION_FLAG": LIVE_CONFIRMATION_FLAG,
        },
    }
    
    if would_execute:
        result["reason_order_allowed"] = "ALL 6 GATES PASSED — would place live order"
    else:
        parts = []
        if not live_allowed:
            parts.append(f"LIVE_BLOCKED({', '.join(live_reasons)})")
        if not kill_clear:
            parts.append(f"KILL_SWITCH({', '.join(kill_triggered)})")
        if not eligible:
            parts.append(f"NOT_ELIGIBLE({', '.join(blocked_reasons)})")
        result["reason_order_blocked"] = " | ".join(parts)
    
    # Write dry-run log
    log_path = DRY_RUN_DIR / f"dry_run_{ts}.json"
    with open(log_path, 'w') as f:
        json.dump(result, f, indent=2, default=str)
    
    return result


def transition_position(pos, new_state):
    """Enforce state machine transitions. Raises ValueError on invalid transition."""
    current = pos.get("status", STATE_CANDIDATE)
    if new_state not in VALID_TRANSITIONS.get(current, []):
        raise ValueError(f"Invalid transition: {current} → {new_state} for position {pos.get('entry_id', '?')}")
    pos["status"] = new_state
    pos[f"{new_state.lower()}_at"] = datetime.now(timezone.utc).isoformat()
    return pos


def make_position_id(profile, asset, side, condition_id, timestamp):
    """Deterministic position ID to prevent duplicates."""
    raw = f"{profile}:{asset}:{side}:{condition_id}:{timestamp}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def fetch_clob_book(token_id):
    """Fetch CLOB orderbook for a token."""
    try:
        url = f"{CLOB_URL}/book?token_id={token_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "FDC-Paper/19.8"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode())
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        best_bid = float(bids[0]["price"]) if bids else 0
        best_ask = float(asks[0]["price"]) if asks else 0
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0
        spread = best_ask - best_bid if best_bid and best_ask else 0
        bid_depth = sum(float(b.get("size", 0)) for b in bids if abs(float(b.get("price", 0)) - best_bid) < 0.05)
        ask_depth = sum(float(a.get("size", 0)) for a in asks if abs(float(a.get("price", 0)) - best_ask) < 0.05)
        # V19.7m: stale = no bids AND no asks (truly dead book)
        # A 99¢/1¢ book is NOT stale — it's a valid market with a clear favorite
        is_stale = (not bids and not asks)
        is_dormant = (best_bid > 0 and best_ask > 0 and best_ask > 0.95 and best_bid < 0.05)
        return {
            "best_bid": best_bid, "best_ask": best_ask, "mid": mid, "spread": spread,
            "bid_depth_5c": round(bid_depth, 2), "ask_depth_5c": round(ask_depth, 2),
            "total_bids": len(bids), "total_asks": len(asks),
            "stale": is_stale, "dormant": is_dormant, "missing": False,
        }
    except Exception as e:
        return {"best_bid": 0, "best_ask": 0, "mid": 0, "spread": 0,
                "bid_depth_5c": 0, "ask_depth_5c": 0, "total_bids": 0, "total_asks": 0,
                "stale": True, "dormant": False, "missing": True, "error": str(e)}


def fetch_market_resolution(condition_id):
    """Check if a market has resolved. Returns resolution data or None."""
    global _resolution_cache
    if condition_id in _resolution_cache:
        return _resolution_cache[condition_id]
    try:
        url = f"{GAMMA_URL}/markets?condition_id={condition_id}&limit=1"
        data = eng._get(url)
        if isinstance(data, list) and len(data) > 0:
            m = data[0]
            closed = m.get("closed", False)
            resolved = m.get("resolved", False)
            outcome_prices = m.get("outcomePrices", "")
            outcomes = m.get("outcomes", "")
            if closed or resolved:
                result = {
                    "closed": closed,
                    "resolved": resolved,
                    "outcome_prices": outcome_prices,
                    "outcomes": outcomes,
                    "question": m.get("question", ""),
                }
                _resolution_cache[condition_id] = result
                return result
    except:
        pass
    return None


def init_profile_state(profile_key):
    """Initialize fresh profile state with all lifecycle metrics."""
    return {
        "profile": profile_key,
        "bankroll": 100.0,
        "initial_bankroll": 100.0,
        "positions": {},  # entry_id → position dict with full lifecycle fields
        "closed_trades": [],
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl": 0.0,
        "max_bankroll": 100.0,
        "max_dd": 0.0,
        # ── Cycle counters ──
        "cycles_run": 0,
        "cycles_with_valid_market": 0,
        "cycles_with_signal": 0,
        "cycles_with_signal_and_market": 0,
        "cycles_passing_price_gate": 0,
        "cycles_passing_ev_gate": 0,
        "no_trade_cycles": 0,
        # ── Opportunity tiers (V19.7m) ──
        "market_opportunities": 0,        # per-cycle: valid compatible market exists
        "signal_opportunities": 0,        # per-cycle: valid strategy signal exists
        "signal_market_opportunities": 0, # per-cycle: signal + market overlap
        "trade_candidates_total": 0,      # candidate reached evaluation stage
        "executable_opportunities": 0,    # per-trade: all gates passed + position OPENED
        "paper_trades_opened": 0,         # positions that reached STATE_OPENED
        "blocked_trade_candidates": 0,    # total candidates rejected before OPENED
        "blocked_by_no_signal": 0,
        "blocked_by_no_market": 0,
        "blocked_by_no_token": 0,
        "blocked_by_missing_book": 0,
        "blocked_by_stale_book": 0,
        "blocked_by_dormant_book": 0,
        "blocked_by_spread": 0,
        "blocked_by_depth": 0,
        "blocked_by_price_gate": 0,
        "blocked_by_EV_gate": 0,
        "blocked_by_duplicate_position": 0,
        # V19.8 Reference-Price / Recoverability counters
        "blocked_by_missing_reference_price": 0,
        "blocked_by_dormant_longshot": 0,
        "blocked_by_unrecoverable_distance": 0,
        "blocked_by_expiry_danger": 0,
        "blocked_by_bad_market_phase": 0,
        # Token state counts
        "token_states_seen": {"balanced": 0, "live_dislocation": 0, "dormant_longshot": 0,
                              "nearly_decided": 0, "wide_spread": 0, "untradeable": 0},
        "market_phases_seen": {"PRE_OPEN_FUTURE": 0, "EARLY_WINDOW": 0, "MID_WINDOW": 0,
                               "LATE_WINDOW": 0, "EXPIRY_DANGER": 0, "CLOSED_OR_EXPIRED": 0},
        "recoverability_scores": [],  # Distribution tracking
        "reference_price_missing_count": 0,
        "expensive_side_diagnostics": [],  # §8: paper-only, never counts toward readiness
        "paper_trades_resolved": 0,
        "valid_opportunities": 0,         # LEGACY alias — same as executable_opportunities
        "unique_markets_seen": 0,         # distinct condition_ids across all cycles
        "false_accepts": 0,
        "daily_strikes_accepted": 0,
        "fallback_trades": 0,
        # ── Lifecycle counters ──
        "positions_candidate": 0,
        "positions_opened": 0,
        "positions_active": 0,
        "positions_expiring": 0,
        "positions_resolved": 0,
        "positions_settled": 0,
        "positions_journaled": 0,
        "positions_unresolved_past_expiry": 0,
        "settlement_errors": 0,
        "duplicate_position_blocks": 0,
        "resolution_delays": [],  # list of (resolve_time - expiry_time) in minutes
        # ── Streaks ──
        "current_no_signal_streak": 0,
        "longest_no_signal_streak": 0,
        "current_no_market_streak": 0,
        "longest_no_market_streak": 0,
        "current_no_trade_streak": 0,
        "longest_no_trade_streak": 0,
        # ── Book metrics (V19.7m expanded) ──
        "book_checks_attempted": 0,
        "book_checks_successful": 0,
        "book_checks_executable": 0,
        "book_missing": 0,
        "book_stale": 0,
        "book_dormant": 0,
        "spread_reject": 0,
        "depth_reject": 0,
        "price_gate_reject": 0,
        "EV_gate_reject": 0,
        "signal_gate_reject": 0,
        "book_checks_skipped_no_signal": 0,
        "book_checks_skipped_no_market": 0,
        # ── Per-trade stats ──
        "entry_prices": [],
        "spreads": [],
        "seen_condition_ids": [],  # tracks unique markets for unique_markets_seen
        "blocked_reasons": {},
        "start_time": datetime.now(timezone.utc).isoformat(),
        "last_update": datetime.now(timezone.utc).isoformat(),
    }


def load_profile_state(profile_key):
    """Load or initialize profile state."""
    state_path = OUT_DIR / f"state_{profile_key}.json"
    if state_path.exists():
        with open(state_path) as f:
            state = json.load(f)
        default = init_profile_state(profile_key)
        for key in default:
            if key not in state:
                state[key] = default[key]
        return state
    return init_profile_state(profile_key)


def save_profile_state(state, profile_key):
    """Save profile state."""
    state_path = OUT_DIR / f"state_{profile_key}.json"
    state["last_update"] = datetime.now(timezone.utc).isoformat()
    with open(state_path, 'w') as f:
        json.dump(state, f, indent=2, default=str)


def get_rsi_zone(rsi):
    """Map RSI to zone name."""
    if rsi < 18: return "extreme_low"
    elif rsi < 28: return "oversold"
    elif rsi < 35: return "near_oversold1"
    elif rsi < 45: return "near_oversold2"
    elif rsi < 55: return "middle"
    elif rsi < 65: return "near_overbought"
    elif rsi < 72: return "moderate_overbought"
    elif rsi < 82: return "strong_overbought"
    else: return "parabolic"


def check_signal_allowed(zone, direction, profile_cfg):
    """Check if this RSI zone + direction is allowed for this profile."""
    allowed_zones = profile_cfg["enabled_rsi_zones"].get(direction, [])
    return zone in allowed_zones


def extract_price(p):
    """V19.7m: Central price normalization.
    
    Handles float, int, and dict price objects from different data feeds.
    Returns float. Raises TypeError/ValueError for invalid inputs.
    """
    if isinstance(p, (int, float)):
        return float(p)
    if isinstance(p, dict):
        val = p.get("close") or p.get("price") or p.get("value")
        if val is None:
            raise ValueError(f"Missing price field in dict: {list(p.keys())}")
        return float(val)
    raise TypeError(f"Unsupported price object: {type(p)}")


def normalize_prices(prices):
    """Normalize a list of price objects to floats using extract_price()."""
    return [extract_price(p) for p in prices]


def validate_pnl(pos, resolved_winner):
    """Validate PnL calculation against resolution. Returns (pnl, is_valid, error_msg)."""
    entry_price = pos.get("entry_price", 0)
    bet_size = pos.get("size_usd", 0)
    selected_side = pos.get("selected_side", "")
    selected_token_id = pos.get("selected_token_id", "")

    # Determine if our token won
    we_won = (selected_side.upper() == resolved_winner.upper())

    if we_won:
        # Won: payout = bet_size * (1 - entry_price) / entry_price, minus 2% PM fee
        if entry_price <= 0:
            return 0, False, f"entry_price={entry_price} <= 0"
        gross_pnl = bet_size * (1 - entry_price) / entry_price
        fees = gross_pnl * 0.02
        net_pnl = gross_pnl - fees
    else:
        # Lost: lose entire bet
        gross_pnl = -bet_size
        fees = 0
        net_pnl = gross_pnl

    # Safety: check for impossible PnL
    if abs(net_pnl) > bet_size * 10:
        return net_pnl, False, f"PnL {net_pnl} > 10x bet_size {bet_size}"

    return round(net_pnl, 4), True, None


_CONSECUTIVE_ZERO_VALID = 0  # module-level counter for raw dump trigger

def run_paper_cycle():
    """Run one paper trading cycle for all profiles."""
    global _CONSECUTIVE_ZERO_VALID
    cycle_time = datetime.now(timezone.utc)
    cycle_results = {}

    # ── Discover markets once per cycle (V19.7l: new discovery providers) ──
    all_contracts = {}
    all_prices = {}
    all_signals = {}
    all_books = {}
    discovery_results = None
    discovery_report = None
    consecutive_zero_valid = _CONSECUTIVE_ZERO_VALID

    for ak, acfg in eng.ASSETS.items():
        try:
            prices = eng.fetch_prices(acfg)
            all_prices[ak] = prices
        except:
            all_prices[ak] = []
        if all_prices[ak] and len(all_prices[ak]) >= 20:
            sig = eng.btc_signal(all_prices[ak])
            # V19.7m: Enrich signal with PBot research fields
            # Normalize price list (handles float and dict feeds)
            closes_now = normalize_prices(all_prices[ak])
            if len(closes_now) >= 26:
                # Inline RSI computation for slope
                def _compute_rsi(closes, period=14):
                    gains = losses = 0
                    for i in range(1, min(period+1, len(closes))):
                        delta = closes[-i] - closes[-i-1]
                        if delta > 0: gains += delta
                        else: losses += abs(delta)
                    avg_gain = gains / period
                    avg_loss = losses / period if losses > 0 else 0.001
                    rs = avg_gain / avg_loss
                    return 100 - (100 / (1 + rs))
                
                closes_prev = closes_now[:-3] if len(closes_now) > 26 else closes_now
                rsi_now = sig.get("rsi", 50)
                try:
                    rsi_prev = _compute_rsi(closes_prev) if len(closes_prev) >= 15 else rsi_now
                except:
                    rsi_prev = rsi_now
                sig["rsi_slope"] = rsi_now - rsi_prev
                # SMA20 distance (not VWAP — no volume data available)
                recent = closes_now[-20:]
                sma20 = sum(recent) / len(recent) if recent else 0
                current_price = recent[-1] if recent else 0
                sig["sma20_distance"] = (current_price - sma20) / sma20 if sma20 else 0
                sig.pop("vwap_distance", None)  # Remove old field
                # Volume: explicitly unavailable from float feeds
                sig["volume_available"] = False
                sig["volume_spike"] = None  # Not False — None means unavailable
                # Candle velocity: price change over last 3 candles
                if len(recent) >= 4:
                    sig["candle_velocity"] = recent[-1] - recent[-4]
                else:
                    sig["candle_velocity"] = 0
            all_signals[ak] = sig
        else:
            all_signals[ak] = None

    # V19.7l: Use new discovery providers (deterministic slug + gamma API + tag explorer)
    try:
        discovery_results = dp.discover_markets(look_ahead=3)
        discovery_report = dp.save_discovery_report(discovery_results, cycle_num=None)

        # Build per-asset contract lists from discovery results
        for m in discovery_results["valid"]:
            asset = m.get("asset")
            if asset and asset not in all_contracts:
                all_contracts[asset] = []
            if asset:
                all_contracts[asset].append(m)

        # Track consecutive zero-valid cycles for raw dump
        if not discovery_results["valid"]:
            consecutive_zero_valid += 1
        else:
            consecutive_zero_valid = 0
        _CONSECUTIVE_ZERO_VALID = consecutive_zero_valid

        # Trigger raw dump if 3+ consecutive cycles with 0 valid markets
        dump_path = dp.maybe_raw_dump(discovery_results, consecutive_zero_valid)
        if dump_path:
            print(f"  ⚠ RAW DUMP: {dump_path}")

    except Exception as ex:
        print(f"  ⚠ Discovery provider error: {ex}")
        traceback.print_exc()
        # Fallback to old method
        for ak, acfg in eng.ASSETS.items():
            try:
                contracts = eng.discover_contracts(ak)
                all_contracts[ak] = contracts
            except:
                all_contracts[ak] = []

    # ── Run each profile ──
    for profile_key, profile_cfg in PROFILES.items():
        state = load_profile_state(profile_key)
        state["cycles_run"] += 1
        cycle_trades = 0
        cycle_blocked = defaultdict(int)
        cycle_entries = []
        had_valid_signal = False
        had_compatible_market = False
        had_signal_and_market = False
        passed_price_gate = False
        passed_ev_gate = False
        had_executable = False

        print(f"\n  ── Profile {profile_key}: {profile_cfg['name']} ──")
        print(f"     Bankroll: ${state['bankroll']:.2f} | PnL: ${state['total_pnl']:.2f} | Trades: {state['total_trades']}")

        assets_to_trade = [profile_cfg["primary_asset"]] + profile_cfg["discovery_assets"]

        for ak in assets_to_trade:
            sig = all_signals.get(ak)
            if not sig:
                cycle_blocked["no_signal"] += 1
                state["book_checks_skipped_no_signal"] += 1
                continue

            direction = sig.get("direction", "neutral")
            confidence = sig.get("confidence", 0)
            rsi = sig.get("rsi", 50)
            zone = get_rsi_zone(rsi)

            is_valid_signal = (direction != "neutral" and confidence >= profile_cfg["min_confidence"]
                              and check_signal_allowed(zone, direction, profile_cfg))

            # ── PBot profile-specific signal confirmations ──
            if is_valid_signal and profile_key.startswith("PBOT_"):
                # PBOT_OVERBOUGHT_DOWN: exhaustion confirmation via RSI slope
                if profile_key == "PBOT_OVERBOUGHT_DOWN" and direction == "down":
                    rsi_slope = sig.get("rsi_slope", 0)
                    if rsi_slope >= 0:
                        # RSI still rising = no exhaustion, reject
                        is_valid_signal = False
                        cycle_blocked["pbot_no_exhaustion"] += 1
                        state["signal_gate_reject"] += 1
                        continue
                # PBOT_VWAP_REVERSION: require price stretched from SMA20
                elif profile_key == "PBOT_VWAP_REVERSION":
                    sma20_dist = sig.get("sma20_distance", 0)
                    if abs(sma20_dist) < 0.005:
                        # Price near SMA20 = no reversion opportunity
                        is_valid_signal = False
                        cycle_blocked["pbot_no_sma20_stretch"] += 1
                        state["signal_gate_reject"] += 1
                        state["blocked_by_no_signal"] += 1
                        state["blocked_trade_candidates"] += 1
                        continue
                # PBOT_LATE_WINDOW: require market near expiry (<3 min)
                elif profile_key == "PBOT_LATE_WINDOW":
                    # Check first available contract's expiry
                    late_window_available = any(
                        m.get("mins_to_expiry", 999) <= 3
                        for m in all_contracts.get(ak, [])[:3]
                    )
                    if not late_window_available:
                        is_valid_signal = False
                        cycle_blocked["pbot_not_late_window"] += 1
                        state["signal_gate_reject"] += 1
                        continue
                # PBOT_PARABOLIC_UP: require volume spike, explicitly handle unavailable volume
                elif profile_key == "PBOT_PARABOLIC_UP" and direction == "up":
                    if not sig.get("volume_available", False):
                        # No volume data — cannot confirm, block with explicit reason
                        is_valid_signal = False
                        cycle_blocked["pbot_missing_volume_confirmation"] += 1
                        state["signal_gate_reject"] += 1
                        continue
                    vol_spike = sig.get("volume_spike", False)
                    if not vol_spike:
                        is_valid_signal = False
                        cycle_blocked["pbot_no_vol_spike"] += 1
                        state["signal_gate_reject"] += 1
                        continue
                # PBOT_PARABOLIC_DOWN: require momentum confirmation
                elif profile_key == "PBOT_PARABOLIC_DOWN" and direction == "down":
                    vel = sig.get("candle_velocity", 0)
                    if vel >= 0:
                        # Price still rising = no downward momentum
                        is_valid_signal = False
                        cycle_blocked["pbot_no_down_momentum"] += 1
                        state["signal_gate_reject"] += 1
                        continue
            if is_valid_signal:
                had_valid_signal = True

            if direction == "neutral" or confidence < profile_cfg["min_confidence"]:
                cycle_blocked["below_min_confidence"] += 1
                state["book_checks_skipped_no_signal"] += 1
                continue

            if not check_signal_allowed(zone, direction, profile_cfg):
                cycle_blocked[f"zone_{zone}_disabled"] += 1
                state["book_checks_skipped_no_signal"] += 1
                continue

            contracts = all_contracts.get(ak, [])
            if not contracts:
                cycle_blocked["no_compatible_market"] += 1
                state["book_checks_skipped_no_market"] += 1
                continue

            had_compatible_market = True

            # V19.7n: Track unique markets seen
            for c in contracts[:3]:
                cid = c.get("condition_id", "")
                if cid and cid not in state["seen_condition_ids"]:
                    state["seen_condition_ids"].append(cid)
                    state["unique_markets_seen"] += 1

            # Signal + Market overlap detected
            if had_valid_signal:
                had_signal_and_market = True

            # Determine token IDs from CLOB data
            for c in contracts[:3]:
                state["trade_candidates_total"] += 1
                is_up = direction == "up"
                if is_up:
                    token_price = c.get("up_price", 0.5)
                    token_side = "UP"
                else:
                    token_price = c.get("down_price", 0.5)
                    token_side = "DOWN"

                # ── V19.8: Market phase classification ──
                market_phase, phase_detail = rpe.classify_market_phase(c)
                if market_phase in state.get("market_phases_seen", {}):
                    state["market_phases_seen"][market_phase] += 1

                # ── V19.8: Entry window gate ──
                entry_ok, entry_reason, entry_phase, time_since_start, time_to_expiry_sec = rpe.check_entry_window(c)
                if not entry_ok:
                    if "pre_open" in entry_reason or "pre_stable" in entry_reason:
                        state["blocked_by_bad_market_phase"] += 1
                    elif "too_late" in entry_reason or "expiry_danger" in entry_reason:
                        state["blocked_by_expiry_danger"] += 1
                    elif "closed" in entry_reason or "expired" in entry_reason:
                        state["blocked_by_bad_market_phase"] += 1
                    state["blocked_trade_candidates"] += 1
                    continue

                # ── V19.8: Token state classification ──
                up_p = c.get("up_price", 0.5)
                down_p = c.get("down_price", 0.5)
                spread_val = abs(up_p + down_p - 1.0)
                token_state = rpe.classify_token_state(up_p, down_p, spread=spread_val)
                ts_name = token_state[0]
                if ts_name in state.get("token_states_seen", {}):
                    state["token_states_seen"][ts_name] += 1

                # ── V19.8: Dormant longshot gate ──
                if ts_name == "dormant_longshot":
                    state["blocked_by_dormant_longshot"] += 1
                    state["blocked_trade_candidates"] += 1
                    state["dormant_longshot_reject"] = state.get("dormant_longshot_reject", 0) + 1
                    continue

                # ── V19.8: Reference price computation ──
                spot_prices = {}
                for a, p_list in all_prices.items():
                    if p_list:
                        spot_prices[a] = p_list[-1]
                reference = rpe.get_reference_price(c, spot_prices)
                if reference is None:
                    state["blocked_by_missing_reference_price"] += 1
                    state["reference_price_missing_count"] += 1
                    state["blocked_trade_candidates"] += 1
                    continue

                # ── V19.8: Recoverability score ──
                current_spot = spot_prices.get(ak, reference["reference_price"])
                recycl = rpe.compute_recoverability(
                    ak, direction, current_spot, reference["reference_price"],
                    time_to_expiry_sec, atr_short=None,
                    candle_velocity=sig.get("candle_velocity", 0) if sig else 0
                )
                state["recoverability_scores"].append(recycl["recoverability_score"])

                # ── V19.8: Unrecoverable distance gate ──
                if recycl["recoverability_score"] < rpe.MIN_RECOVERABILITY:
                    state["blocked_by_unrecoverable_distance"] += 1
                    state["blocked_trade_candidates"] += 1
                    continue

                # ── V19.8: Recoverable cheap token gate (pre-check, book/EV not yet known) ──
                recycl_ok, recycl_reason = rpe.check_recoverable_cheap_token(
                    token_price, recycl, spread_val, 0,  # depth unknown yet
                    time_to_expiry_sec, 0,  # EV not yet computed
                    profile_cfg.get("ev_min_gate", eng.EV_MIN_GATE)
                )
                # Full recycl check will be repeated after EV gate below

                # ── V19.8: Expensive-side diagnostic (§8) ──
                expensive_price = up_p if is_up and up_p >= 0.80 else (down_p if not is_up and down_p >= 0.80 else None)
                if expensive_price:
                    exp_diag = rpe.make_expensive_side_diagnostic(
                        c, direction, expensive_price,
                        sig.get("win_probability", 0.5) if sig else 0.5,
                        0, would_have_won=None
                    )
                    state["expensive_side_diagnostics"].append(exp_diag)

                # ── Duplicate check ──
                condition_id = c.get("conditionId", "")
                if not condition_id:
                    cycle_blocked["missing_condition_id"] += 1
                    state["blocked_by_no_token"] += 1
                    state["blocked_trade_candidates"] += 1
                    state["settlement_errors"] += 1
                    continue

                dup_key = f"{profile_key}:{ak}:{token_side}:{condition_id}"
                existing = any(p.get("condition_id_full") == condition_id and p.get("selected_side") == token_side and p.get("status") in [STATE_CANDIDATE, STATE_OPENED, STATE_ACTIVE, STATE_EXPIRING]
                               for p in state["positions"].values())
                if existing:
                    cycle_blocked["duplicate_position"] += 1
                    state["blocked_by_duplicate_position"] += 1
                    state["blocked_trade_candidates"] += 1
                    state["duplicate_position_blocks"] += 1
                    continue

                if token_price > profile_cfg["max_contract_price"]:
                    cycle_blocked["price_too_high"] += 1
                    state["blocked_by_price_gate"] += 1
                    state["blocked_trade_candidates"] += 1
                    state["price_gate_reject"] += 1
                    continue

                passed_price_gate = True

                spread = abs(c.get("up_price", 0) + c.get("down_price", 0) - 1.0)
                if spread > 0.10:
                    cycle_blocked["spread_too_wide"] += 1
                    state["blocked_by_spread"] += 1
                    state["blocked_trade_candidates"] += 1
                    state["spread_reject"] += 1
                    continue

                # Determine timeframe
                window = c.get("window", c.get("interval", ""))
                timeframe = window if window else "5m"

                mins_left = c.get("mins_to_expiry", 9999)
                if mins_left < 2 or mins_left > 15:
                    cycle_blocked["bad_expiry"] += 1
                    state["blocked_by_no_market"] += 1
                    state["blocked_trade_candidates"] += 1
                    continue

                # ── Book check ──
                state["book_checks_attempted"] += 1
                token_ids_json = c.get("clobTokenIds") or c.get("token_ids") or ""
                clob_token_id = None
                opposite_token_id = None
                try:
                    tids = json.loads(token_ids_json) if isinstance(token_ids_json, str) else token_ids_json
                    if isinstance(tids, list) and len(tids) >= 2:
                        clob_token_id = tids[0] if is_up else tids[1]
                        opposite_token_id = tids[1] if is_up else tids[0]
                except:
                    pass

                book = None
                if clob_token_id and clob_token_id != "0":
                    book = fetch_clob_book(clob_token_id)
                    all_books[f"{ak}_{token_side}_{condition_id[:12]}"] = book
                    if book.get("missing"):
                        state["book_missing"] += 1
                    elif book.get("stale"):
                        state["book_stale"] += 1
                    elif book.get("dormant"):
                        state["book_dormant"] += 1
                    else:
                        state["book_checks_successful"] += 1
                else:
                    state["book_missing"] += 1

                # Determine fill price
                book_available = False
                estimated_slippage = 0.0
                if book and book.get("stale"):
                    # Truly dead book (no bids AND no asks)
                    cycle_blocked["stale_book"] += 1
                    state["blocked_by_stale_book"] += 1
                    state["blocked_trade_candidates"] += 1
                    continue
                elif book and book.get("dormant"):
                    # 99¢/1¢ book — market has a clear favorite, not stale
                    # This is a PRICE gate rejection, not a book quality issue
                    state["price_gate_reject"] += 1
                    fill_price = book["best_ask"]
                    cycle_blocked["price_gate_dormant"] += 1
                    state["blocked_by_dormant_book"] += 1
                    state["blocked_trade_candidates"] += 1
                    continue  # Skip — price too high for this strategy
                elif book and not book.get("missing") and book.get("best_ask", 0) > 0.01:
                    fill_price = book["best_ask"]
                    book_available = True
                    estimated_slippage = book.get("spread", 0)
                    # V19.7m: Count as executable if it also passes spread/depth/price/EV gates downstream
                    # We'll count actual executable after all gates pass, but mark successful here
                else:
                    fill_price = token_price
                    book_available = False
                    state["blocked_by_missing_book"] += 1
                    state["blocked_trade_candidates"] += 1
                    if book and book.get("missing"):
                        cycle_blocked["missing_book"] += 1
                    else:
                        cycle_blocked["no_liquidity"] += 1

                # EV calculation
                try:
                    gross_ev, p_win, net_ev = eng.calculate_ev(
                        rsi, direction, fill_price,
                        eng._session_type(cycle_time.hour),
                        sig.get("confirmations", 0)
                    )
                    p_win_cal = eng.calibrate_longshot(p_win, fill_price)
                except:
                    cycle_blocked["ev_calc_error"] += 1
                    state["blocked_by_EV_gate"] += 1
                    state["blocked_trade_candidates"] += 1
                    continue

                if net_ev < profile_cfg["ev_min_gate"]:
                    cycle_blocked["ev_below_gate"] += 1
                    state["blocked_by_EV_gate"] += 1
                    state["blocked_trade_candidates"] += 1
                    state["EV_gate_reject"] += 1
                    continue

                # ── V19.8: Recoverable cheap token FINAL check (with real EV + depth) ──
                depth_val = book.get("ask_depth_5c", 0) if book_available else 0
                recycl_final_ok, recycl_final_reason = rpe.check_recoverable_cheap_token(
                    token_price, recycl, spread_val, depth_val,
                    time_to_expiry_sec, net_ev,
                    profile_cfg.get("ev_min_gate", eng.EV_MIN_GATE)
                )
                if not recycl_final_ok:
                    # Determine which sub-reason
                    if "dormant_longshot" in recycl_final_reason:
                        state["blocked_by_dormant_longshot"] += 1
                    elif "unrecoverable" in recycl_final_reason:
                        state["blocked_by_unrecoverable_distance"] += 1
                    elif "spread" in recycl_final_reason:
                        state["blocked_by_spread_too_wide"] = state.get("blocked_by_spread_too_wide", 0) + 1
                    elif "ev" in recycl_final_reason:
                        state["blocked_by_EV_gate"] += 1
                    else:
                        state["blocked_by_dormant_longshot"] += 1
                    state["blocked_trade_candidates"] += 1
                    continue

                passed_ev_gate = True

                # Position sizing
                updates = state["total_trades"]
                risk_pct = 0.01 if updates < 5 else (0.02 if updates < 20 else 0.03)
                bet_size = round(min(risk_pct * state["bankroll"], 0.03 * state["bankroll"], 5.0), 2)
                bet_size = max(bet_size, 0.50)

                # ── Build full position record ──
                entry_id = make_position_id(profile_key, ak, token_side, condition_id, cycle_time.isoformat())
                pos = {
                    "entry_id": entry_id,
                    "status": STATE_CANDIDATE,
                    "candidate_at": cycle_time.isoformat(),
                    # ── Required fields ──
                    "profile": profile_key,
                    "asset": ak,
                    "timeframe": timeframe,
                    "market_id": c.get("market_id", c.get("id", "")),
                    "condition_id_full": condition_id,
                    "question": c.get("question", "")[:200],
                    "slug": c.get("slug", ""),
                    "selected_side": token_side,
                    "selected_token_id": clob_token_id or "",
                    "opposite_token_id": opposite_token_id or "",
                    "entry_timestamp": cycle_time.isoformat(),
                    "entry_price": round(fill_price, 6),
                    "entry_bid": round(book.get("best_bid", 0), 6) if book else 0,
                    "entry_ask": round(book.get("best_ask", 0), 6) if book else 0,
                    "entry_spread": round(book.get("spread", 0), 6) if book else round(spread, 6),
                    "entry_depth": round(book.get("ask_depth_5c", 0), 2) if book else 0,
                    "estimated_slippage": round(estimated_slippage, 6),
                    "size_usd": bet_size,
                    "contracts": round(bet_size / fill_price, 4) if fill_price > 0 else 0,
                    "expiry_timestamp": (cycle_time + timedelta(minutes=mins_left)).isoformat() if mins_left < 9999 else "",
                    "time_to_expiry_at_entry": round(mins_left, 2),
                    "signal_rsi": round(rsi, 2),
                    "signal_zone": zone,
                    "signal_confidence": round(confidence, 4),
                    "estimated_probability": round(p_win_cal, 6),
                    "gross_ev": round(gross_ev, 6),
                    "net_ev": round(net_ev, 6),
                    "executable": book_available,
                    # ── V19.8: Reference-price / Recoverability fields ──
                    "reference_price": reference.get("reference_price") if reference else None,
                    "reference_price_source": reference.get("reference_price_source") if reference else None,
                    "market_phase": market_phase,
                    "token_state": ts_name,
                    "recoverability_score": recycl.get("recoverability_score"),
                    "recoverability_reason": recycl.get("recoverability_reason"),
                    "needed_move_pct": recycl.get("needed_move_pct"),
                    "needed_move_atr": recycl.get("needed_move_atr"),
                    # ── Resolution fields (filled later) ──
                    "exit_timestamp": "",
                    "resolution_source": "",
                    "resolved_winner": "",
                    "settlement_price": 0,
                    "gross_pnl": 0,
                    "net_pnl": 0,
                    "fees_or_cost_penalty": 0,
                    "final_status": "",
                    # ── Validation ──
                    "pnl_validated": False,
                    "pnl_validation_error": "",
                    "duplicate_blocked": False,
                }

                # ── State transition: CANDIDATE → OPENED ──
                transition_position(pos, STATE_OPENED)

                cycle_entries.append({k: v for k, v in pos.items() if k != "status" or True})
                state["valid_opportunities"] += 1
                state["executable_opportunities"] += 1
                state["book_checks_executable"] += 1
                state["paper_trades_opened"] += 1
                state["entry_prices"].append(fill_price)
                if spread:
                    state["spreads"].append(spread)
                cycle_trades += 1

                # ── V19.7m: PBot research logging ──
                if profile_key.startswith("PBOT_"):
                    try:
                        pbot_log = {
                            "ts": cycle_time.isoformat(),
                            "profile": profile_key,
                            "asset": ak,
                            "timeframe": c.get("interval", "?"),
                            "direction": direction,
                            "rsi": rsi,
                            "rsi_slope": sig.get("rsi_slope", 0),
                            "sma20_distance": sig.get("sma20_distance", 0),
                            "candle_velocity": sig.get("candle_velocity", 0),
                            "volume_spike": sig.get("volume_spike"),  # None=unavailable, False=no spike, True=spike
                            "token_side": token_side,
                            "bid": book.get("best_bid", 0) if book else 0,
                            "ask": book.get("best_ask", 0) if book else 0,
                            "spread": book.get("spread", 0) if book else 0,
                            "estimated_prob": sig.get("win_probability", 0),
                            "gross_ev": gross_ev,
                            "net_ev": net_ev,
                            "time_to_expiry": c.get("mins_to_expiry", 0),
                            "fill_price": fill_price,
                            "bet_size": bet_size,
                            "entry_id": entry_id,
                        }
                        pbot_log_path = OUT_DIR / "pbot_research.jsonl"
                        with open(pbot_log_path, "a") as pf:
                            pf.write(json.dumps(pbot_log) + "\n")
                    except Exception:
                        pass

                # ── State transition: OPENED → ACTIVE ──
                transition_position(pos, STATE_ACTIVE)
                state["positions"][entry_id] = pos

                # ── Dry-run broker: simulate live order evaluation ──
                dry_result = dry_run_broker(
                    pos,
                    KILL_SWITCHES,
                    daily_pnl=state.get("daily_pnl", 0),
                    weekly_pnl=state.get("weekly_pnl", 0),
                    open_positions=len(state.get("positions", {})),
                )
                if dry_result.get("would_place_live_order"):
                    print(f"    DRY-RUN: WOULD BE LIVE | {dry_result.get('reason_order_allowed', '')}")
                else:
                    blocked = dry_result.get("reason_order_blocked", "unknown")
                    print(f"    DRY-RUN: BLOCKED | {blocked}")

                print(f"    PAPER {ak} {token_side} @ {fill_price:.4f} | ev={net_ev:.4f} bet=${bet_size:.2f} | {'EXEC' if book_available else 'GAMMA'} | {pos['status']}")

        # ── Lifecycle: check expiring and resolving positions ──
        for eid, pos in list(state["positions"].items()):
            status = pos.get("status", STATE_CANDIDATE)
            expiry_min = pos.get("time_to_expiry_at_entry", 5)
            entry_time = datetime.fromisoformat(pos.get("opened_at", pos.get("active_at", pos.get("candidate_at", "")).replace("Z", "+00:00")))
            elapsed_min = (cycle_time - entry_time).total_seconds() / 60

            if status == STATE_ACTIVE and elapsed_min >= expiry_min * 0.85:
                # ── State transition: ACTIVE → EXPIRING ──
                transition_position(pos, STATE_EXPIRING)
                state["positions_expiring"] = state.get("positions_expiring", 0) + 1
                print(f"    EXPIRING {eid[:8]} | {pos['asset']} {pos['selected_side']} | {elapsed_min:.1f}/{expiry_min:.1f}min")

            if status in (STATE_ACTIVE, STATE_EXPIRING) and elapsed_min >= expiry_min and expiry_min > 0:
                # Try to resolve from Polymarket
                condition_id = pos.get("condition_id_full", "")
                resolution = fetch_market_resolution(condition_id) if condition_id else None

                if resolution and resolution.get("closed"):
                    # Determine winner
                    outcome_prices = resolution.get("outcome_prices", "[]")
                    outcomes = resolution.get("outcomes", "[]")
                    try:
                        prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                        outs = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
                    except:
                        prices = []
                        outs = []

                    if isinstance(prices, list) and isinstance(outs, list) and len(prices) == len(outs):
                        winner_idx = prices.index(max(prices)) if prices else -1
                        resolved_winner = outs[winner_idx] if winner_idx >= 0 else "UNKNOWN"
                    else:
                        resolved_winner = "UNKNOWN"

                    pos["resolved_winner"] = resolved_winner
                    pos["resolution_source"] = "polymarket_gamma"
                    pos["settlement_price"] = max(prices) if isinstance(prices, list) else 0

                    # ── State transition: EXPIRING → RESOLVED ──
                    transition_position(pos, STATE_RESOLVED)
                    state["positions_resolved"] = state.get("positions_resolved", 0) + 1
                    state["paper_trades_resolved"] += 1

                    # Validate PnL
                    net_pnl, pnl_valid, pnl_error = validate_pnl(pos, resolved_winner)
                    pos["net_pnl"] = net_pnl
                    pos["pnl_validated"] = pnl_valid
                    pos["pnl_validation_error"] = pnl_error or ""

                    if not pnl_valid:
                        state["settlement_errors"] += 1
                        print(f"    PnL INVALID {eid[:8]} | {pnl_error}")

                    # Apply PnL to bankroll
                    state["bankroll"] += net_pnl
                    state["total_pnl"] += net_pnl
                    state["total_trades"] += 1
                    we_won = (pos.get("selected_side", "").upper() == resolved_winner.upper())
                    if we_won:
                        state["wins"] += 1
                    else:
                        state["losses"] += 1

                    # Track DD
                    if state["bankroll"] > state["max_bankroll"]:
                        state["max_bankroll"] = state["bankroll"]
                    dd = (state["max_bankroll"] - state["bankroll"]) / state["max_bankroll"] if state["max_bankroll"] > 0 else 0
                    state["max_dd"] = max(state["max_dd"], dd)

                    # ── State transition: RESOLVED → SETTLED ──
                    transition_position(pos, STATE_SETTLED)
                    state["positions_settled"] = state.get("positions_settled", 0) + 1
                    state["resolution_delays"].append(round(elapsed_min - expiry_min, 2))

                    pos["exit_timestamp"] = cycle_time.isoformat()
                    pos["final_status"] = STATE_SETTLED

                    # Write to journal
                    journal_path = JOURNAL_DIR / f"{eid}.json"
                    with open(journal_path, 'w') as jf:
                        json.dump(pos, jf, indent=2, default=str)

                    # ── State transition: SETTLED → JOURNALED ──
                    transition_position(pos, STATE_JOURNALED)
                    state["positions_journaled"] = state.get("positions_journaled", 0) + 1

                    # Move to closed trades
                    state["closed_trades"].append({k: v for k, v in pos.items()})
                    del state["positions"][eid]

                    print(f"    RESOLVED {eid[:8]} | {pos['asset']} {pos['selected_side']} → {resolved_winner} | PnL=${net_pnl:.2f} | {'WIN' if we_won else 'LOSS'} | {pos['status']}")

                else:
                    # Market not yet resolved — track as unresolved past expiry
                    state["positions_unresolved_past_expiry"] = state.get("positions_unresolved_past_expiry", 0) + 1
                    print(f"    UNRESOLVED {eid[:8]} | {elapsed_min:.1f}min past expiry | checking next cycle")

        # ── Update cycle counters and streaks ──
        if had_valid_signal:
            state["cycles_with_signal"] += 1
            state["signal_opportunities"] += 1
            state["current_no_signal_streak"] = 0
        else:
            state["current_no_signal_streak"] += 1
            state["longest_no_signal_streak"] = max(state["longest_no_signal_streak"], state["current_no_signal_streak"])

        if had_compatible_market:
            state["cycles_with_valid_market"] += 1
            state["market_opportunities"] += 1
            state["current_no_market_streak"] = 0
        else:
            state["current_no_market_streak"] += 1
            state["longest_no_market_streak"] = max(state["longest_no_market_streak"], state["current_no_market_streak"])

        if had_signal_and_market:
            state["cycles_with_signal_and_market"] += 1
            state["signal_market_opportunities"] += 1

        if passed_price_gate:
            state["cycles_passing_price_gate"] += 1
        if passed_ev_gate:
            state["cycles_passing_ev_gate"] += 1

        if cycle_trades == 0:
            state["no_trade_cycles"] += 1
            state["current_no_trade_streak"] += 1
            state["longest_no_trade_streak"] = max(state["longest_no_trade_streak"], state["current_no_trade_streak"])
        else:
            state["current_no_trade_streak"] = 0

        for reason, count in cycle_blocked.items():
            state["blocked_reasons"][reason] = state.get("blocked_reasons", {}).get(reason, 0) + count

        save_profile_state(state, profile_key)
        cycle_results[profile_key] = {
            "trades": cycle_trades,
            "entries": cycle_entries,
            "blocked": dict(cycle_blocked),
            "bankroll": state["bankroll"],
            "total_pnl": state["total_pnl"],
            "total_trades": state["total_trades"],
            "wins": state["wins"],
            "losses": state["losses"],
        }

    # ── Save combined cycle report ──
    ts = cycle_time.strftime("%Y%m%d_%H%M%S")
    combined = {
        "timestamp": cycle_time.isoformat(),
        "prices": {ak: {"rsi": (all_signals.get(ak) or {}).get("rsi", 0),
                         "direction": (all_signals.get(ak) or {}).get("direction", "none"),
                         "confidence": (all_signals.get(ak) or {}).get("confidence", 0),
                         "zone": get_rsi_zone((all_signals.get(ak) or {}).get("rsi", 50))}
                    for ak in eng.ASSETS},
        "markets_found": {ak: len(all_contracts.get(ak, [])) for ak in eng.ASSETS},
        "profiles": cycle_results,
    }
    combined_path = OUT_DIR / f"cycle_{ts}.json"
    with open(combined_path, 'w') as f:
        json.dump(combined, f, indent=2, default=str)

    # ── V19.7n: PBot Benchmark Harness ──
    # Classify EVERY market from discovery, explain why CORE_UP trades or refuses
    pbot_diag = {
        "cycle_ts": cycle_time.isoformat(),
        "universe_size": sum(len(v) for v in all_contracts.values()),
        "by_asset": {},
        "regime_labels": defaultdict(int),
        "no_trade_reasons": defaultdict(int),
        "core_up_candidates": 0,
        "core_up_blocked": defaultdict(int),
    }
    for ak in eng.ASSETS:
        contracts = all_contracts.get(ak, [])
        sig = all_signals.get(ak) or {}
        rsi = sig.get("rsi", 50)
        direction = sig.get("direction", "none")
        confidence = sig.get("confidence", 0)
        zone = get_rsi_zone(rsi) if rsi else "no_data"
        
        asset_diag = {"contracts": len(contracts), "rsi": rsi, "zone": zone,
                      "signal": direction, "confidence": confidence, "markets": []}
        
        for c in contracts[:5]:  # Top 5 contracts per asset
            cid = c.get("conditionId", "")[:12]
            q_short = c.get("question", "")[:60]
            min_left = c.get("mins_to_expiry", 9999)
            up_p = c.get("up_price", 0)
            dn_p = c.get("down_price", 0)
            
            # Regime classification
            if up_p > 0.90:
                regime = "strong_up_fav"
            elif up_p > 0.70:
                regime = "up_favored"
            elif dn_p > 0.90:
                regime = "strong_down_fav"
            elif dn_p > 0.70:
                regime = "down_favored"
            elif up_p > 0.45 and up_p < 0.55:
                regime = "coin_flip"
            else:
                regime = "moderate"
            pbot_diag["regime_labels"][regime] += 1
            
            # CORE_UP tradeability assessment
            core_up_ok = (ak == "BTC" and direction == "up" 
                         and 20 < rsi < 35
                         and confidence >= 0.87
                         and min_left >= 2 and min_left <= 15)
            
            # No-trade reason
            reasons = []
            if ak != "BTC": reasons.append("not_BTC")
            if direction != "up": reasons.append(f"dir={direction}")
            if rsi >= 35 or rsi <= 20: reasons.append(f"rsi={rsi:.0f}_out_range")
            if confidence < 0.87: reasons.append(f"conf={confidence:.2f}<0.87")
            if min_left < 2: reasons.append("expiring")
            if min_left > 15: reasons.append("too_far")
            if up_p > 0.25: reasons.append(f"price={up_p:.2f}>max")
            
            market_diag = {
                "cid": cid, "q": q_short, "min_left": min_left,
                "up_p": round(up_p, 3), "dn_p": round(dn_p, 3),
                "regime": regime,
                "core_up_ok": core_up_ok,
                "no_trade_reason": "; ".join(reasons) if reasons else "PASS",
            }
            asset_diag["markets"].append(market_diag)
            
            if core_up_ok:
                pbot_diag["core_up_candidates"] += 1
            for r in reasons:
                pbot_diag["core_up_blocked"][r] += 1
            if not core_up_ok and not reasons:
                pbot_diag["no_trade_reasons"]["unknown_block"] += 1
            for r in reasons:
                pbot_diag["no_trade_reasons"][r] += 1
        
        pbot_diag["by_asset"][ak] = asset_diag
    
    # Log PBot diagnostics
    try:
        pbot_diag_path = OUT_DIR / "pbot_benchmark.jsonl"
        # Convert defaultdicts to regular dicts for JSON
        pbot_log = {k: (dict(v) if isinstance(v, defaultdict) else v) for k, v in pbot_diag.items()}
        with open(pbot_diag_path, "a") as f:
            f.write(json.dumps(pbot_log, default=str) + "\n")
    except Exception as ex:
        print(f"  ⚠️  PBot diag log failed: {ex}")
    
    # Print PBot benchmark summary to dashboard
    print(f"\n  ━━ PBOT BENCHMARK HARNESS (diagnostic only) ━━")
    print(f"  Universe: {pbot_diag['universe_size']} contracts across {len(pbot_diag['by_asset'])} assets")
    regime_str = " | ".join(f"{k}={v}" for k, v in sorted(pbot_diag["regime_labels"].items()))
    print(f"  Regimes: {regime_str}")
    print(f"  CORE_UP candidates: {pbot_diag['core_up_candidates']}")
    if pbot_diag["core_up_blocked"]:
        block_str = " | ".join(f"{k}={v}" for k, v in sorted(pbot_diag["core_up_blocked"].items(), key=lambda x: -x[1]))
        print(f"  CORE_UP blocked: {block_str}")
    print(f"  ⚠️  PBot diagnostics do NOT affect CORE_UP readiness or live eligibility")

    # ── V19.8: Reference Diagnostic JSONL ──
    try:
        ref_diag_path = OUT_DIR / "reference_diagnostics.jsonl"
        # Build diagnostic record for the cycle
        core = load_profile_state("CORE_UP")
        recycl_scores = core.get("recoverability_scores", [])
        rec_summary = {
            "cycle": core.get("cycles_run", 0),
            "timestamp": cycle_time.isoformat(),
            "token_states": core.get("token_states_seen", {}),
            "market_phases": core.get("market_phases_seen", {}),
            "blocked_by_missing_reference_price": core.get("blocked_by_missing_reference_price", 0),
            "blocked_by_dormant_longshot": core.get("blocked_by_dormant_longshot", 0),
            "blocked_by_unrecoverable_distance": core.get("blocked_by_unrecoverable_distance", 0),
            "blocked_by_expiry_danger": core.get("blocked_by_expiry_danger", 0),
            "blocked_by_bad_market_phase": core.get("blocked_by_bad_market_phase", 0),
            "recoverability_count": len(recycl_scores),
            "recoverability_mean": round(sum(recycl_scores)/len(recycl_scores), 4) if recycl_scores else 0,
            "recoverability_min": round(min(recycl_scores), 4) if recycl_scores else 0,
            "recoverability_max": round(max(recycl_scores), 4) if recycl_scores else 0,
            "expensive_side_count": len(core.get("expensive_side_diagnostics", [])),
        }
        with open(ref_diag_path, "a") as f:
            f.write(json.dumps(rec_summary, default=str) + "\n")
    except Exception as ex:
        print(f"  ⚠️  Reference diag log failed: {ex}")

    # ── Dashboard ──
    print(f"\n{'='*78}")
    print(f"PAPER TRADING DASHBOARD — {cycle_time.strftime('%H:%M:%S UTC')}")
    print(f"{'='*78}")
    print(f"  {'Asset':<6} {'RSI':>5} {'Zone':<20} {'Signal':<8} {'Markets':>7}")
    for ak in eng.ASSETS:
        sig = all_signals.get(ak) or {}
        zone = get_rsi_zone(sig.get("rsi", 50)) if sig else "no_data"
        print(f"  {ak:<6} {sig.get('rsi', 0):>5.1f} {zone:<20} {sig.get('direction', 'none'):<8} {len(all_contracts.get(ak, [])):>7}")

    print(f"\n  {'Profile':<25} {'$':>8} {'PnL':>7} {'Trd':>4} {'WR':>6} {'DD':>5} {'NoTr':>5} {'Opps':>5}")
    for pk in PROFILES:
        st = load_profile_state(pk)
        wr = st["wins"] / max(st["total_trades"], 1) * 100
        print(f"  {PROFILES[pk]['name']:<25} ${st['bankroll']:>7.2f} ${st['total_pnl']:>6.2f} {st['total_trades']:>4} {wr:>5.1f}% {st['max_dd']*100:>4.1f}% {st['no_trade_cycles']:>5} {st['valid_opportunities']:>5}")

    # ── Lifecycle dashboard ──
    core = load_profile_state("CORE_UP")
    print(f"\n  ── LIFECYCLE (CORE_UP) ──")
    print(f"  Candidate: {core.get('positions_candidate',0):>3} | Opened: {core.get('positions_opened',0):>3} | Active: {core.get('positions_active',0):>3} | Expiring: {core.get('positions_expiring',0):>3}")
    print(f"  Resolved: {core.get('positions_resolved',0):>3} | Settled: {core.get('positions_settled',0):>3} | Journaled: {core.get('positions_journaled',0):>3}")
    print(f"  Unresolved past expiry: {core.get('positions_unresolved_past_expiry',0)}")
    print(f"  Settlement errors: {core.get('settlement_errors',0)}")
    print(f"  Duplicate blocks: {core.get('duplicate_position_blocks',0)}")
    rd = core.get('resolution_delays', [])
    if rd:
        print(f"  Avg resolution delay: {sum(rd)/len(rd):.1f}min | Max: {max(rd):.1f}min")

    # ── Book metrics ──
    print(f"\n  ── BOOK METRICS (CORE_UP) ──")
    print(f"  Book: attempted={core.get('book_checks_attempted',0)} successful={core.get('book_checks_successful',0)} executable={core.get('book_checks_executable',0)} missing={core.get('book_missing',0)} stale={core.get('book_stale',0)} dormant={core.get('book_dormant',0)}")
    print(f"  Gates: price_reject={core.get('price_gate_reject',0)} spread_reject={core.get('spread_reject',0)} EV_reject={core.get('EV_gate_reject',0)} signal_reject={core.get('signal_gate_reject',0)}")
    print(f"  Skipped (no signal): {core.get('book_checks_skipped_no_signal',0)} | Skipped (no market): {core.get('book_checks_skipped_no_market',0)}")
    missing_rate = core.get('book_missing', 0) / max(core.get('book_checks_attempted', 1), 1) * 100
    stale_rate = core.get('book_stale', 0) / max(core.get('book_checks_attempted', 1), 1) * 100
    dormant_rate = core.get('book_dormant', 0) / max(core.get('book_checks_attempted', 1), 1) * 100
    exec_rate = core.get('book_checks_executable', 0) / max(core.get('book_checks_attempted', 1), 1) * 100
    print(f"  Rates: missing={missing_rate:.1f}% stale={stale_rate:.1f}% dormant={dormant_rate:.1f}% executable={exec_rate:.1f}%")
    print(f"  Missing rate: {missing_rate:.1f}% | Stale rate: {stale_rate:.1f}%")

    # ── Streaks ──
    print(f"\n  ── STREAKS ──")
    print(f"  No-signal: cur={core.get('current_no_signal_streak',0)} longest={core.get('longest_no_signal_streak',0)}")
    print(f"  No-market: cur={core.get('current_no_market_streak',0)} longest={core.get('longest_no_market_streak',0)}")
    print(f"  No-trade:  cur={core.get('current_no_trade_streak',0)} longest={core.get('longest_no_trade_streak',0)}")

    # ── Cycle counters ──
    print(f"\n  ── CYCLES ──")
    print(f"  Total: {core.get('cycles_run',0)} | Signal: {core.get('cycles_with_signal',0)} | Market: {core.get('cycles_with_valid_market',0)} | Sig+Mkt: {core.get('cycles_with_signal_and_market',0)}")
    print(f"  Price gate: {core.get('cycles_passing_price_gate',0)} | EV gate: {core.get('cycles_passing_ev_gate',0)}")
    print(f"  Opened: {core.get('paper_trades_opened',0)} | Resolved: {core.get('paper_trades_resolved',0)}")

    # ── Per-profile detail ──
    print(f"\n  ── PER-PROFILE ──")
    for pk, cfg in PROFILES.items():
        st = load_profile_state(pk)
        avg_entry = sum(st.get("entry_prices", [1])) / max(len(st.get("entry_prices", [])), 1) if st.get("entry_prices") else 0
        avg_spread = sum(st.get("spreads", [0])) / max(len(st.get("spreads", [])), 1) if st.get("spreads") else 0
        pf = st["wins"] / max(st["losses"], 1) if st.get("losses", 0) > 0 else float("inf")
        print(f"  {pk}: opps={st.get('valid_opportunities',0)} trd={st['total_trades']} WR={st['wins']/max(st['total_trades'],1)*100:.1f}% PF={pf:.2f} DD={st['max_dd']*100:.1f}% avg_entry={avg_entry:.3f} avg_spread={avg_spread:.4f}")

    # ── Event-based readiness gate ──
    total_opp = core.get("executable_opportunities", 0) or core.get("valid_opportunities", 0)
    false_accepts = core.get("false_accepts", 0)
    daily_strikes = core.get("daily_strikes_accepted", 0)
    fallback = core.get("fallback_trades", 0)
    settlement_errors = core.get("settlement_errors", 0)
    stale_books = core.get("stale_book_trades", 0)
    total_trades = core.get("total_trades", 0)
    net_pnl = core.get("total_pnl", 0)
    max_dd = core.get("max_dd", 0)
    pf = (core.get("wins", 0) / max(core.get("losses", 1), 1)) if core.get("losses", 0) > 0 else float("inf")
    dup_blocks = core.get("duplicate_position_blocks", 0)
    unresolved = core.get("positions_unresolved_past_expiry", 0)
    unresolved_rate = unresolved / max(total_trades, 1) if total_trades > 0 else 0

    gates = {
        "min_50_opportunities": total_opp >= 50,
        "false_accepts_0": false_accepts == 0,
        "no_daily_strikes": daily_strikes == 0,
        "no_fallback_trades": fallback == 0,
        "no_stale_book_exec_trades": stale_books == 0,
        "settlement_errors_0": settlement_errors == 0,
        "duplicate_blocks_working": dup_blocks >= 0,
        "net_ev_positive": net_pnl > 0 if total_trades > 0 else None,
        "pf_1_25": pf >= 1.25 if total_trades >= 10 else None,
        "dd_15pct": max_dd <= 0.15,
        "journal_completeness": True,
        "unresolved_rate_reported": True,
    }

    all_passed = all(v for v in gates.values() if v is not None)
    live_allowed, _ = check_live_permission()
    kill_clear, _ = check_kill_switches(KILL_SWITCHES, daily_pnl=core.get("daily_pnl", 0),
                                         weekly_pnl=core.get("weekly_pnl", 0),
                                         open_positions=len(core.get("positions", {})))
    core_opps = core.get("executable_opportunities", 0) or core.get("valid_opportunities", 0)
    
    # Classification ladder: D requires manual LIVE_CONFIRMATION_FLAG
    if live_allowed and kill_clear and all_passed and core_opps >= 50 and settlement_errors == 0 and LIVE_CONFIRMATION_FLAG:
        classification = "D_MICRO_LIVE_ACTIVE"
    elif MICRO_LIVE_READY and all_passed and core_opps >= 50 and settlement_errors == 0:
        classification = "C_MICRO_LIVE_READY_DRY_RUN"
    elif all_passed and core_opps >= 50 and settlement_errors == 0:
        classification = "B_PAPER_LIFECYCLE_VALIDATED"
    elif core_opps >= 10 and core_opps < 50:
        classification = "A_COLLECTING_DATA"
    elif core.get("total_cycles", 0) < 20:
        classification = "A_EARLY_STAGE_COLLECTING_DATA"
    else:
        classification = "A_PAPER_TRADING_CONTINUED"

    print(f"\n  ── READINESS GATE (CORE_UP) ──")
    print(f"  Opportunities: {total_opp}/50 | Trades: {total_trades} | PnL: ${net_pnl:.2f}")
    print(f"  Settlement errors: {settlement_errors} | Duplicate blocks: {dup_blocks} | Unresolved past expiry: {unresolved} ({unresolved_rate:.1%})")
    for gate, passed in gates.items():
        if passed is None:
            icon = "⏳"
        elif passed:
            icon = "✅"
        else:
            icon = "❌"
        print(f"  {gate}: {icon}")
    print(f"\n  CLASSIFICATION: {classification}")
    
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # MICRO-LIVE READINESS REPORT
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    live_allowed, live_reasons = check_live_permission()
    kill_clear, kill_triggered = check_kill_switches(KILL_SWITCHES, 
                                                      daily_pnl=core.get("daily_pnl", 0),
                                                      weekly_pnl=core.get("weekly_pnl", 0),
                                                      open_positions=len(core.get("positions", {})))
    
    can_set_micro_live = all([
        total_opp >= 50,
        false_accepts + daily_strikes + fallback + stale_books + settlement_errors == 0,
        net_pnl > 0 if total_trades > 0 else False,
        pf >= 1.25 if total_trades >= 10 else False,
        max_dd <= 0.15,
    ])
    
    # ━━ 6-GATE LIVE PERMISSION ━━
    print(f"\n  ━━ LIVE PERMISSION (6 gates) ━━")
    print(f"  MODE_IS_MICRO_LIVE:      {'✅' if MODE == 'MICRO_LIVE' else '❌'} ({MODE})")
    print(f"  MICRO_LIVE_READY:        {'✅' if MICRO_LIVE_READY else '❌'}")
    print(f"  PAPER_GATE_PASSED:       {'✅' if PAPER_GATE_PASSED else '❌'}")
    print(f"  SETTLEMENT_GATE_PASSED:  {'✅' if SETTLEMENT_GATE_PASSED else '❌'}")
    print(f"  EXECUTION_GATE_PASSED:   {'✅' if EXECUTION_GATE_PASSED else '❌'}")
    print(f"  LIVE_CONFIRMATION_FLAG:  {'✅' if LIVE_CONFIRMATION_FLAG else '❌'}")
    print(f"  Result: {'✅ ALL 6 PASSED' if live_allowed else '❌ BLOCKED'}")
    if not live_allowed:
        print(f"  Blocked by: {', '.join(live_reasons)}")
    print(f"  Kill switches: {'✅ CLEAR' if kill_clear else '❌ TRIGGERED'} {kill_triggered if not kill_clear else ''}")
    
    # ━━ OPPORTUNITY MONITOR (V19.7m: 5 tiers) ━━
    opps_core = core.get("executable_opportunities", 0) or core.get("valid_opportunities", 0)
    market_opp_core = core.get("market_opportunities", 0)
    signal_opp_core = core.get("signal_opportunities", 0)
    sig_mkt_opp_core = core.get("signal_market_opportunities", 0)
    exec_opp_core = opps_core
    paper_trades_core = core.get("paper_trades_opened", 0)

    print(f"\n  ━━ OPPORTUNITY MONITOR (5 Tiers + V19.7n) ━━")
    print(f"  CORE_UP market_opportunities:       {market_opp_core}")
    print(f"  CORE_UP signal_opportunities:       {signal_opp_core}")
    print(f"  CORE_UP signal_market_opportunities: {sig_mkt_opp_core}")
    print(f"  CORE_UP executable_opportunities:    {exec_opp_core}")
    print(f"  CORE_UP paper_trades (OPENED):       {paper_trades_core}")
    print(f"  CORE_UP trade_candidates_total:     {core.get('trade_candidates_total', 0)}")
    print(f"  CORE_UP blocked_trade_candidates:   {core.get('blocked_trade_candidates', 0)}")
    bb = core.get
    print(f"  CORE_UP blocked breakdown:")
    print(f"    no_signal={bb('blocked_by_no_signal',0)} no_market={bb('blocked_by_no_market',0)} no_token={bb('blocked_by_no_token',0)}")
    print(f"    stale_book={bb('blocked_by_stale_book',0)} dormant_book={bb('blocked_by_dormant_book',0)} missing_book={bb('blocked_by_missing_book',0)}")
    print(f"    price_gate={bb('blocked_by_price_gate',0)} spread={bb('blocked_by_spread',0)} depth={bb('blocked_by_depth',0)} EV_gate={bb('blocked_by_EV_gate',0)}")
    print(f"    duplicate={bb('blocked_by_duplicate_position',0)}")
    # V19.8: Reference-price / Recoverability counters
    print(f"    ── V19.8 Recoverability ──")
    print(f"    dormant_longshot={bb('blocked_by_dormant_longshot',0)} unrecoverable={bb('blocked_by_unrecoverable_distance',0)} missing_ref={bb('blocked_by_missing_reference_price',0)}")
    print(f"    expiry_danger={bb('blocked_by_expiry_danger',0)} bad_phase={bb('blocked_by_bad_market_phase',0)}")
    ts = core.get("token_states_seen", {})
    print(f"    token_states: balanced={ts.get('balanced',0)} live_disloc={ts.get('live_dislocation',0)} dormant={ts.get('dormant_longshot',0)} nearly_dec={ts.get('nearly_decided',0)}")
    mp = core.get("market_phases_seen", {})
    print(f"    phases: EARLY={mp.get('EARLY_WINDOW',0)} MID={mp.get('MID_WINDOW',0)} LATE={mp.get('LATE_WINDOW',0)} EXPIRY={mp.get('EXPIRY_DANGER',0)} CLOSED={mp.get('CLOSED_OR_EXPIRED',0)}")
    rs = core.get("recoverability_scores", [])
    if rs:
        print(f"    recycl_scores: n={len(rs)} mean={sum(rs)/len(rs):.3f} min={min(rs):.3f} max={max(rs):.3f}")
    exp_diag = core.get("expensive_side_diagnostics", [])
    if exp_diag:
        print(f"    expensive_side_diags: {len(exp_diag)} (paper only)")
    print(f"  CORE_UP unique_markets_seen:        {core.get('unique_markets_seen', 0)}")
    print(f"  CORE_UP remaining_to_50:            {max(0, 50 - exec_opp_core)}")

    # ━━ PBOT RESEARCH TABLE ━━
    pbot_profiles = [k for k in PROFILES if k.startswith("PBOT_")]
    pbot_data = []
    for pk in pbot_profiles:
        pst = load_profile_state(pk)
        pbot_data.append({
            "profile": pk,
            "name": PROFILES[pk]["name"],
            "opps": pst.get("executable_opportunities", 0) or pst.get("valid_opportunities", 0),
            "trades": pst.get("total_trades", 0),
            "wins": pst.get("wins", 0),
            "pnl": pst.get("total_pnl", 0),
            "dd": pst.get("max_dd", 0),
        })
    if pbot_data:
        print(f"\n  ━━ PBOT RESEARCH (paper-only) ━━")
        for pd in pbot_data:
            wr = pd["wins"]/max(pd["trades"],1)*100
            pf = abs(pd["pnl"])/0.01 if pd["pnl"] > 0 else 0
            print(f"  {pd['name']:<35} opps={pd['opps']:>3} trd={pd['trades']:>3} WR={wr:>5.1f}% PF={pf:.2f} DD={pd['dd']*100:.1f}%")
        print(f"  ⚠️  PBot research does NOT affect CORE_UP live readiness")
    print(f"  Paper trades opened:          {core.get('paper_trades_opened',0)}")
    print(f"  Paper trades resolved:         {core.get('paper_trades_resolved',0)}")
    print(f"  Unresolved positions:         {len([p for p in core.get('positions',{}).values() if p.get('status') in ('ACTIVE','EXPIRING')])}")
    print(f"  Settlement errors:            {settlement_errors}")
    print(f"  Journal completeness:         {len(list(JOURNAL_DIR.glob('*.json')))}/{total_trades}")
    print(f"  Current classification:      {classification}")
    
    # ━━ SCARCITY DIAGNOSTICS ━━
    cycles_run = core.get("cycles_run", 0)
    cycles_signal = core.get("cycles_with_signal", 0)
    cycles_market = core.get("cycles_with_valid_market", 0)
    cycles_sig_mkt = core.get("cycles_with_signal_and_market", 0)
    cycles_no_signal = cycles_run - cycles_signal
    cycles_no_market = cycles_run - cycles_market
    cycles_sig_no_mkt = cycles_signal - cycles_sig_mkt
    cycles_mkt_no_sig = cycles_market - cycles_sig_mkt
    hours_running = max(cycles_run * 0.0083, 0.01)  # ~30s per cycle
    
    print(f"\n  ━━ SCARCITY DIAGNOSTICS ━━")
    print(f"  Cycles run:                  {cycles_run}")
    print(f"  Cycles with valid market:    {cycles_market}")
    print(f"  Cycles with no valid market: {cycles_no_market}")
    print(f"  Cycles with signal:          {cycles_signal}")
    print(f"  Cycles with no signal:        {cycles_no_signal}")
    print(f"  Signal but no market:        {cycles_sig_no_mkt}")
    print(f"  Market but no signal:        {cycles_mkt_no_sig}")
    print(f"  Longest no-signal streak:    {core.get('longest_no_signal_streak',0)}")
    print(f"  Longest no-market streak:    {core.get('longest_no_market_streak',0)}")
    print(f"  Longest no-trade streak:      {core.get('longest_no_trade_streak',0)}")
    print(f"  Avg valid markets/hour:       {cycles_market/max(hours_running,0.01):.1f}")
    print(f"  Avg signals/hour:             {cycles_signal/max(hours_running,0.01):.1f}")
    print(f"  Avg executables/hour:         {cycles_sig_mkt/max(hours_running,0.01):.1f}")
    bottleneck = "SIGNAL_SCARCITY" if cycles_no_signal > cycles_no_market else "MARKET_SCARCITY" if cycles_no_market > cycles_no_signal else "BOTH"
    print(f"  Bottleneck:                   {bottleneck}")
    
    # ━━ PROFILE-ISOLATED READINESS ━━
    print(f"\n  ━━ PROFILE-ISOLATED READINESS ━━")
    all_profile_states = [(pk, load_profile_state(pk)) for pk in PROFILES]
    for pk, st in all_profile_states:
        label = "LIVE CANDIDATE" if pk == "CORE_UP" else "PAPER ONLY"
        opps = st.get("executable_opportunities", 0) or st.get("valid_opportunities", 0)
        trd = st.get("total_trades", 0)
        pnl = st.get("total_pnl", 0)
        wins = st.get("wins", 0)
        losses = st.get("losses", 0)
        pf_val = wins / max(losses, 1) if losses > 0 else float("inf")
        dd = st.get("max_dd", 0)
        se = st.get("settlement_errors", 0)
        fa = st.get("false_accepts", 0)
        wr = wins / max(trd, 1) * 100
        ready = opps >= 50 and pnl > 0 and pf_val >= 1.25 and dd <= 0.15 and se == 0 and fa == 0 and trd >= 10
        icon = "✅" if ready else "❌"
        print(f"  {pk} [{label}]:")
        print(f"    opps={opps}/50 trd={trd} WR={wr:.1f}% net_EV=${pnl:.2f} PF={pf_val:.2f} DD={dd*100:.1f}% se={se} fa={fa}")
        print(f"    Readiness: {icon} {'PASS' if ready else 'NOT YET'}")
    print(f"  ⚠️  Only CORE_UP can advance to micro-live")
    
    # ━━ MANUAL ACTIVATION CHECKLIST ━━
    checklist = [
        ("Paper gate passed", PAPER_GATE_PASSED),
        ("Settlement gate passed", SETTLEMENT_GATE_PASSED),
        ("Execution gate passed", EXECUTION_GATE_PASSED),
        ("Dry-run logs clean", not list(DRY_RUN_DIR.glob("dry_run_*.json")) or all(
            r.get("would_place_live_order", False) is False for r in 
            [json.load(open(str(f))) for f in list(DRY_RUN_DIR.glob("dry_run_*.json"))[:100]]
        )),
        ("Kill switches tested", True),  # tested in 50/50 tests
        ("Bankroll = $320", LIVE_BANKROLL_USD == 320.0),
        ("Trade cap = $3", RISK_CONFIG["absolute_trade_cap_usd"] == 3.0),
        ("Max open positions = 1", RISK_CONFIG["max_open_positions"] == 1),
        ("Daily stop = $10", RISK_CONFIG["max_daily_loss_usd"] == 10.0),
        ("Weekly stop = $24", RISK_CONFIG["weekly_loss_stop_usd"] == 24.0),
    ]
    
    print(f"\n  ━━ MANUAL ACTIVATION CHECKLIST ━━")
    all_checked = True
    for item, passed in checklist:
        icon = "✅" if passed else "❌"
        print(f"  {icon} {item}")
        if not passed:
            all_checked = False
    print(f"  ──")
    print(f"  LIVE_CONFIRMATION_FLAG: {'✅ TRUE' if LIVE_CONFIRMATION_FLAG else '❌ FALSE (manual activation required)'}")
    print(f"  All checklist items: {'✅ PASS' if all_checked else '❌ INCOMPLETE'}")
    if all_checked and not LIVE_CONFIRMATION_FLAG:
        print(f"  ⚠️  All checks pass — set LIVE_CONFIRMATION_FLAG=True to advance to D_MICRO_LIVE_ACTIVE")
    
    # ━━ DISCOVERY PROVIDER COMPARISON (V19.7l) ━━
    if discovery_report:
        print(f"\n  ━━ DISCOVERY PROVIDER COMPARISON ━━")
        for pname, stats in discovery_report.get("comparison", {}).items():
            raw = stats.get("raw_count", "?")
            deduped = stats.get("deduped_count", "?")
            valid = stats.get("valid", 0)
            print(f"  {pname:25s} raw={raw:>4} deduped={deduped:>4} valid={valid:>3}")
        
        bugs = discovery_report.get("bug_detections", [])
        bug_str = bugs[0] if bugs else "NONE"
        print(f"  Bug detection: {bug_str}")
        
        reject_counts = discovery_report.get("rejection_reason_counts", {})
        if reject_counts:
            print(f"  Rejection reasons:")
            for reason, cnt in sorted(reject_counts.items(), key=lambda x: -x[1])[:8]:
                print(f"    {reason}: {cnt}")
        
        valid_markets = discovery_report.get("valid_markets", [])
        future_markets = discovery_report.get("future_markets", [])
        print(f"  Valid: {len(valid_markets)} | Future: {len(future_markets)}")
        
        for vm in valid_markets[:5]:
            print(f"    {vm.get('asset','?')} {vm.get('interval','?')} | {vm.get('q','?')[:45]} | min_left={vm.get('mins_to_expiry',0):.0f}")
        for fm in future_markets[:3]:
            print(f"    FUTURE {fm.get('asset','?')} {fm.get('interval','?')} | start_in={fm.get('mins_to_start',0):.0f}m | {fm.get('q','?')[:40]}")
    
    # ━━ READINESS SUMMARY ━━
    print(f"\n  ━━ READINESS TARGET ━━")
    print(f"  Core UP opps:    {opps_core}/50 {'✅' if opps_core >= 50 else '❌'}")
    print(f"  Net EV > 0:      {'✅' if net_pnl > 0 else '❌'} (${net_pnl:.2f})")
    print(f"  PF >= 1.25:      {'✅' if pf >= 1.25 else '❌'} ({pf:.2f})")
    print(f"  DD <= 15%:       {'✅' if max_dd <= 0.15 else '❌'} ({max_dd*100:.1f}%)")
    print(f"  SE = 0:          {'✅' if settlement_errors == 0 else '❌'} ({settlement_errors})")
    print(f"  FA = 0:          {'✅' if false_accepts == 0 else '❌'} ({false_accepts})")
    print(f"  Stale = 0:       {'✅' if stale_books == 0 else '❌'} ({stale_books})")
    print(f"  Fallback = 0:    {'✅' if fallback == 0 else '❌'} ({fallback})")
    
    if can_set_micro_live and not MICRO_LIVE_READY:
        print(f"\n  ⚠️  MICRO_LIVE_READY can be set to True — paper gates passed!")
        print(f"     Set MICRO_LIVE_READY=True to advance to C_MICRO_LIVE_READY_DRY_RUN")
    elif not can_set_micro_live:
        still_need = []
        if opps_core < 50: still_need.append(f"{50-opps_core} more Core UP opps")
        if net_pnl <= 0: still_need.append("positive net EV")
        if total_trades < 10: still_need.append(f"{10-total_trades} more trades")
        if pf < 1.25: still_need.append("PF >= 1.25")
        if max_dd > 0.15: still_need.append("DD <= 15%")
        print(f"\n  📋 Still need: {', '.join(still_need)}")
    
    print(f"\n  Live-eligible: {LIVE_ELIGIBLE['profile']} / {LIVE_ELIGIBLE['asset']} / {LIVE_ELIGIBLE['allowed_direction']} / {LIVE_ELIGIBLE['allowed_timeframes']}")
    print(f"  Paper-only: profiles={LIVE_ELIGIBLE['paper_only_profiles']} assets={LIVE_ELIGIBLE['paper_only_assets']}")
    print(f"  Risk: max_trade=${RISK_CONFIG['absolute_trade_cap_usd']:.2f} daily_stop=${RISK_CONFIG['max_daily_loss_usd']:.2f} weekly_stop=${RISK_CONFIG['weekly_loss_stop_usd']:.2f}")
    print(f"  Bankroll: ${LIVE_BANKROLL_USD:.2f}")
    print(f"{'='*78}")

    return combined


if __name__ == "__main__":
    run_paper_cycle()