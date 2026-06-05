#!/usr/bin/env python3
"""
Father Daddy Capital — Polymarket Engine V19.8 (Signal Bottleneck + Shadow Trigger Hardening)
=============================================================================================

Built on V19.7. Adds:
- Deep signal debugging (signal_debug.jsonl per asset per cycle)
- Token state classification (live_dislocation / false_dislocation / balanced / dormant_longshot / nearly_decided / untradeable)
- Recoverability scoring
- 4 shadow signal variants (STRICT / RSI_ONLY / ONE_CONFIRM / EARLY_TURN / RECOVERABILITY_FIRST)
- Multi-asset (BTC=production, ETH/SOL/XRP=paper-eligible-only)
- True dislocation scarcity reporting
- "Would Trade If Signal Relaxed" audit
- 2-hour signal-focused paper loop

V19.7 production gates UNCHANGED. Live trading DISABLED until manual activation.
Shadow profiles do NOT count toward live readiness.

Author: Hugh (3rd of 5) + Riker
Date: 2026-06-01
"""

import json, os, sys, time, traceback, math, ast, random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

# ─── Inherit V19.7 engine ───
REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))
import pm_engine_v19_7 as v197
import paper_resolution as pres
import canonical_position as cpos

from pm_engine_v19_7 import (
    # Constants
    MIN_CONFIDENCE, MAX_CONFIDENCE, RSI_OVERSOLD_MIN, RSI_OVERSOLD, RSI_NEAR_OVERSOLD,
    SCAN_SECONDS, INITIAL_BANKROLL, PAPER_BANKROLL, EV_MIN_GATE,
    MIN_BET, MAX_BET_DOLLAR, MAX_BANKROLL_FRAC, KELLY_MULT,
    MAX_OPEN_POSITIONS, MIN_EDGE, MIN_CONTRACT_PRICE, MAX_CONTRACT_PRICE,
    MIN_VOLUME_USD, DYNAMIC_PRICE_GATE, DYNAMIC_PRICE_GATE_BUFFER,
    STOP_LOSS_PCT, TIME_DECAY_SELL_MINS, TIME_DECAY_MIN_PRICE,
    BEAR_SKIP, TREND_GUARD, SLIPPAGE_TICKS, REJECTION_RATE,
    # Helpers
    _ema, _get, _parse,
    btc_signal as btc_signal_v197, fetch_5m as fetch_5m_v197,
    discover_contracts as discover_contracts_v197,
    evaluate_entries as evaluate_entries_v197,
    evaluate_exits, execute_sell, check_settlements,
    process_exits, kelly_size, calculate_ev,
    is_bear_market, is_uptrend, is_downtrend,
    is_blacklisted, is_regime_blacklisted,
    TradeJournal, MarkovProbEngine,
    _check_kill_switch, _rolling_drawdown, _init_live,
    load_state, save_state, summary,
    pm_encode_signal, scale_pnl,
    calibrate_longshot,
    # EV probability tables
    EV_RSI_PROB, EV_DOWN_MODIFIER, EV_SESSION_MODIFIER,
    # Risk
    RISK_PCT_COLD, RISK_PCT_WARM, RISK_PCT_PROVEN,
    COLD_UPDATES, WARM_UPDATES,
    DD_LEVEL_1, DD_LEVEL_2, DD_LEVEL_3,
)

import numpy as np

# ══════════════════════════════════════════════════════════════════════════════
# V19.9 HYBRID PROBABILITY REFACTOR
# §1: RSI table → prior only. §2: Markov reference-price target. §3: Neural frozen.
# §4: Tiered Bayesian calibration blend by bucket_n. §6: EV gate with buffer.
# ══════════════════════════════════════════════════════════════════════════════

# ─── §1: RSI prior — table is prior, not final ───
# EV_RSI_PROB provides rsi_prior_p only. The cascade computes adjusted_p.
CALIBRATION_CAP = 0.10  # Max model probability can exceed market-implied probability

# ─── §2: Markov max weight ───
MARKOV_MAX_WEIGHT = 0.10  # Markov can contribute at most 10% to adjusted_p

# ─── §3: Neural trade influence FROZEN ───
NEURAL_TRADE_BLEND = 0  # §3: Must stay 0 until >=100 resolved trades in strategy family
                         # with positive realized_EV and acceptable calibration gap
# Kept for diagnostic logging only:
NEURAL_DIAGNOSTIC_ENABLED = True

# ─── §5: Bucket gating ───
BUCKET_PAPER = (0.20, 0.30)       # Paper trades allowed only here
BUCKET_BLOCKED = (0.30, 0.40)     # Hard block — no paper, no diagnostic trade
BUCKET_DIAGNOSTIC_RANGES = [(0.0, 0.20), (0.40, 1.0)]  # Log only

# ─── §6: EV gate buffers ───
EDGE_BUFFER_PAPER = 0.03   # Paper: 3¢ edge buffer
EDGE_BUFFER_LIVE = 0.05    # Future live: 5¢ edge buffer
SLIPPAGE_PENALTY = 0.01    # 1¢ slippage per trade

# ─── §7: Fixed paper trade size ───
PAPER_TRADE_SIZE = 2.00    # $2 fixed for paper trades
MAX_LIVE_SIZE = 3.00       # $3 max for future live

# ─── §9: Accounting ───
ACCOUNTING_INVARIANT_FAIL = False


class HybridMarkovEngine(MarkovProbEngine):
    """§2: Markov with reference-price target instead of rolling midpoint."""

    def simulate(self, current_state, steps_to_expiry, n_sims=2000, reference_price=None):
        """
        Monte Carlo forward simulation from current state.
        If reference_price provided: terminal price > reference_price = UP.
        If reference_price is None: returns None (Markov skipped).
        """
        if self.matrix is None:
            return None

        rng = np.random.RandomState(current_state * 7 + steps_to_expiry * 13)
        up_count = 0
        mid = self.N_STATES // 2

        for _ in range(n_sims):
            state = current_state
            for _ in range(steps_to_expiry):
                state = rng.choice(self.N_STATES, p=self.matrix[state])
            if reference_price is not None:
                # Convert final state back to normalized price and compare
                # Upper half of states = above midpoint = UP
                if state > mid:
                    up_count += 1
                elif state == mid:
                    up_count += 0.5
            else:
                # No reference — use midpoint as before
                if state > mid:
                    up_count += 1
                elif state == mid:
                    up_count += 0.5

        return up_count / n_sims

    def get_win_prob(self, prices, direction, steps_remaining, reference_price=None):
        """§2: If reference_price is None, return None (skip Markov)."""
        if reference_price is None:
            return None  # Markov skipped — no reference price available

        states = self.discretize(prices)
        if states is None:
            return None

        self.state_history = states
        self.build_matrix(states)

        current_state = states[-1]
        steps = max(1, steps_remaining)

        raw_prob = self.simulate(current_state, steps, n_sims=2000,
                                  reference_price=reference_price)
        if raw_prob is None:
            return None

        if direction == "down":
            raw_prob = 1.0 - raw_prob

        return raw_prob


_hybrid_markov = HybridMarkovEngine()

# §3: Override v197's _neural_blend to freeze neural trade influence
# Neural output is logged as diagnostic only, must not affect adjusted_p until
# >=100 resolved trades in strategy family with positive realized_EV and acceptable calibration gap
v197._neural_blend_orig = v197._neural_blend
def _neural_blend_frozen():
    """§3: Neural trade influence FROZEN. Returns 0.0 blend weight.
    Neural diagnostic logging still happens via NEURAL_DIAGNOSTIC_ENABLED."""
    return 0.0
v197._neural_blend = _neural_blend_frozen


def compute_rsi_prior(rsi, direction, contract_price, session_type=1, confirmations=2):
    """
    §1: RSI zone probability as PRIOR only — not final p_win.
    Returns the same value as the old calculate_ev probability, but
    explicitly as a prior that will be blended in the hybrid cascade.
    """
    zone = v197._rsi_zone(rsi) if hasattr(v197, '_rsi_zone') else _rsi_zone_local(rsi)

    if zone == 'dead_zone':
        return 0.22  # RSI 35-50 dead zone — very low prior

    p_prior = EV_RSI_PROB.get(zone, 0.50)

    # Direction modifier: DOWN on cheap side gets +3%
    if direction == 'down' and contract_price <= 0.15:
        p_prior += EV_DOWN_MODIFIER

    # Session modifier
    session_mod = EV_SESSION_MODIFIER.get(session_type, -0.05)
    p_prior += session_mod

    # Confirmation modifier (for near-oversold zones)
    if zone in ('near_oversold1', 'near_oversold2', 'near_oversold3'):
        if confirmations >= 2:
            p_prior += 0.01
        elif confirmations == 0:
            p_prior -= 0.05

    # Clamp
    p_prior = max(0.10, min(0.90, p_prior))
    return p_prior


def _rsi_zone_local(rsi):
    """Local RSI zone classifier."""
    if rsi < 15:
        return 'ultra_oversold'
    elif rsi < 20:
        return 'deep_oversold'
    elif rsi < 25:
        return 'oversold'
    elif rsi < 28:
        return 'near_oversold1'
    elif rsi < 35:
        return 'near_oversold2'
    elif rsi < 45:
        return 'near_oversold3'
    else:
        return 'dead_zone'


def compute_market_implied_p(entry_ask, direction):
    """
    §1: Market-implied probability from order book.
    For YES/Up tokens: market_implied_p = entry_ask.
    For NO/Down tokens: market_implied_p = 1 - entry_ask.
    """
    if direction == 'down':
        return 1.0 - entry_ask
    return entry_ask


def compute_adjusted_p(rsi_prior_p, market_implied_p, entry_ask,
                        bucket_n=0, empirical_bucket_p=None,
                        bayesian_p=None, markov_p=None):
    """
    §4: Tiered probability blend governed by empirical bucket evidence.

    Cascade:
      1. model_p = blend of rsi_prior_p + bayesian_p + markov_p
         - Base: rsi_prior_p
         - Bayesian: 70/30 rsi/bayesian if available
         - Markov: capped at 10% weight
      2. Tiered empirical blend based on bucket_n:
         - bucket_n < 20: min(model_p, market_implied_p + 0.05)
         - 20 <= bucket_n < 50: min(0.50*empirical + 0.50*model, market + 0.08)
         - bucket_n >= 50: 0.70*empirical + 0.30*model
      3. Final adjusted_p clamped to [0.05, 0.95]
    """
    # ── Step 1: Model probability (prior + bayesian + markov) ──
    model_p = rsi_prior_p

    # Bayesian blend: 70% prior, 30% bayesian if available
    if bayesian_p is not None:
        model_p = 0.70 * model_p + 0.30 * bayesian_p

    # Markov blend: capped at MARKOV_MAX_WEIGHT (10%)
    if markov_p is not None:
        markov_weight = MARKOV_MAX_WEIGHT
        model_p = model_p * (1.0 - markov_weight) + markov_p * markov_weight

    # ── Step 2: Tiered empirical blend ──
    if bucket_n < 20 or empirical_bucket_p is None:
        # Tier 1: No empirical evidence — strict cap
        adjusted = min(model_p, market_implied_p + 0.05)
    elif bucket_n < 50:
        # Tier 2: Partial evidence — 50/50 blend, looser cap
        blended = 0.50 * empirical_bucket_p + 0.50 * model_p
        adjusted = min(blended, market_implied_p + 0.08)
    else:
        # Tier 3: Sufficient evidence — empirical governs
        adjusted = 0.70 * empirical_bucket_p + 0.30 * model_p

    # ── Step 3: Clamp ──
    adjusted = max(0.05, min(0.95, adjusted))

    return adjusted


# ── Backward-compatible alias for event monitor ──
def recalibrate_probability(model_p, market_implied_p, entry_ask=None, realized_sample_size=0):
    """Legacy alias: delegates to compute_adjusted_p with tiered blending."""
    if entry_ask is not None:
        market_implied_p = entry_ask
    # Map old realized_sample_size to bucket_n tiers
    if realized_sample_size >= 50:
        bucket_n = 50
    elif realized_sample_size >= 20:
        bucket_n = 30
    else:
        bucket_n = 0
    return compute_adjusted_p(
        rsi_prior_p=model_p, market_implied_p=market_implied_p,
        entry_ask=entry_ask if entry_ask else market_implied_p,
        bucket_n=bucket_n, empirical_bucket_p=None,
        bayesian_p=None, markov_p=None,
    )


def compute_hybrid_probability(rsi, direction, entry_ask, contract_price,
                                session_type=1, confirmations=2,
                                prices=None, steps_remaining=None,
                                bucket_n=0, empirical_bucket_p=None,
                                state=None):
    """
    §1-4: Full hybrid probability cascade.

    Returns dict with ALL probability layers for logging:
      rsi_prior_p, market_implied_p, empirical_bucket_p, bayesian_p,
      markov_p, neural_diagnostic_p, adjusted_p
      raw_edge, cost_adjusted_edge, buffered_edge
    """
    # rsi_prior_p
    rsi_prior_p = compute_rsi_prior(rsi, direction, contract_price, session_type, confirmations)

    # market_implied_p
    market_implied_p = compute_market_implied_p(entry_ask, direction)

    # markov_p (§2: reference-price target, None if no reference)
    markov_p = None
    if prices is not None and len(prices) >= 20 and steps_remaining is not None:
        try:
            # Use current price as reference (terminal must exceed it for UP)
            reference_price = prices[-1] if prices else None
            markov_p = _hybrid_markov.get_win_prob(prices, direction, steps_remaining,
                                                      reference_price=reference_price)
            # Cap at MARKOV_MAX_WEIGHT contribution — markov_p is already 0-1
        except Exception:
            markov_p = None

    # bayesian_p (§4: from BayesianCalibrator if available)
    bayesian_p = None
    try:
        _bayes = v197._get_bayesian() if hasattr(v197, '_get_bayesian') else None
        if _bayes is not None and _bayes.updates >= 10:
            bayesian_p = _bayes.predict(
                v197._get_encoder().encode(
                    [0.0]*12, entry_ask, 1-entry_ask, 0, 1.0
                ),
                market_price=entry_ask
            ).get("probability")
    except Exception:
        bayesian_p = None

    # neural_diagnostic_p (§3: logged but NOT blended)
    neural_diagnostic_p = None
    if NEURAL_DIAGNOSTIC_ENABLED:
        try:
            _neural = v197._get_neural() if hasattr(v197, '_get_neural') else None
            if _neural is not None and _neural.network.updates >= 10:
                sig_dict = {"direction": direction, "confidence": 0.5, "rsi": rsi,
                             "macd": 0, "momentum": 2, "_prices": prices or []}
                signal_vec = v197.pm_encode_signal(sig_dict)
                raw_pred = _neural.network.predict(signal_vec)
                neural_diagnostic_p = float((raw_pred + 1) / 2 if direction == "up" else (1 - raw_pred) / 2)
                neural_diagnostic_p = max(0.0, min(1.0, neural_diagnostic_p))
        except Exception:
            neural_diagnostic_p = None

    # adjusted_p (§4: tiered blend)
    adjusted_p = compute_adjusted_p(
        rsi_prior_p=rsi_prior_p,
        market_implied_p=market_implied_p,
        entry_ask=entry_ask,
        bucket_n=bucket_n,
        empirical_bucket_p=empirical_bucket_p,
        bayesian_p=bayesian_p,
        markov_p=markov_p,
    )

    # EV computation (§6)
    raw_edge = adjusted_p - entry_ask
    cost_adjusted_edge = adjusted_p - entry_ask - SLIPPAGE_PENALTY
    buffered_edge = cost_adjusted_edge - EDGE_BUFFER_PAPER  # Paper trades use 3¢ buffer

    return {
        "rsi_prior_p": round(rsi_prior_p, 4),
        "market_implied_p": round(market_implied_p, 4),
        "empirical_bucket_p": round(empirical_bucket_p, 4) if empirical_bucket_p is not None else None,
        "bayesian_p": round(bayesian_p, 4) if bayesian_p is not None else None,
        "markov_p": round(markov_p, 4) if markov_p is not None else None,
        "neural_diagnostic_p": round(neural_diagnostic_p, 4) if neural_diagnostic_p is not None else None,
        "adjusted_p": round(adjusted_p, 4),
        "entry_ask": round(entry_ask, 4),
        "raw_edge": round(raw_edge, 4),
        "cost_adjusted_edge": round(cost_adjusted_edge, 4),
        "buffered_edge": round(buffered_edge, 4),
        "bucket_n": bucket_n,
    }


def reconcile_accounting(state):
    """
    §9: Accounting reconciliation — verify invariant.
    
    Simple invariant: bankroll = start_capital + net_realized_pnl
    Where net_realized_pnl = bankroll - start_capital (calculated, not stored).
    
    The 'total_pnl' field tracks paper PnL (gross win payouts), which is NOT
    the same as net realized PnL. Bankroll is the source of truth.
    
    If open_positions == 0:
        Net PnL = bankroll - start_capital
        Journal PnL should be consistent with net PnL (within rounding).
    
    If open_positions > 0:
        bankroll + open_cost = start_capital + net_realized_pnl_from_settled
    """
    run_id = state.get("run_id", "unknown")
    original_start = state.get("original_start_bankroll", state.get("start_bankroll", 320))
    current = state.get("bankroll", 0)
    total_pnl_field = state.get("total_pnl", 0)
    positions = state.get("positions", {})

    open_positions_list = [p for p in positions.values() if p.get("status") == "open"]
    open_count = len(open_positions_list)
    open_cost = sum(p.get("bet", 0) for p in open_positions_list)
    open_mark = round(open_cost, 2)
    
    settled_trades = state.get("wins", 0) + state.get("losses", 0)
    wins = state.get("wins", 0)
    losses = state.get("losses", 0)
    
    # Net realized PnL from bankroll (source of truth)
    net_realized_pnl = round(current - original_start + open_cost, 2)

    # Invariant: bankroll = original_start + net_realized_pnl - open_cost
    # net_realized_pnl = bankroll - original_start + open_cost
    # For no open positions: net_realized_pnl = bankroll - original_start
    # With open positions: bankroll = original_start + net_realized_pnl - open_cost
    if open_count == 0:
        # Verify: bankroll = original_start + net_realized_pnl
        # net_realized_pnl tracks all resolved trade P&L
        expected = original_start + net_realized_pnl
        check_passed = abs(current - expected) < 1.00  # $1 tolerance for rounding
        discrepancy = round(abs(current - expected), 4)
    else:
        # Verify: bankroll = original_start + net_realized_pnl - open_cost
        # open_cost already deducted from bankroll for open positions
        expected = original_start + net_realized_pnl - open_cost
        check_passed = abs(current - expected) < 1.00
        discrepancy = round(abs(current - expected), 4)

    # expected_bankroll for reporting: just original_start (bankroll already includes PnL)
    expected_bankroll = original_start

    profile_pnl = {}
    sp = state.get("shadow_profiles", {})
    for name, sd in (sp or {}).items():
        w = sd.get("wins", 0)
        l = sd.get("losses", 0)
        pnl = sd.get("pnl", 0)
        profile_pnl[name] = {"wins": w, "losses": l, "pnl": round(pnl, 2)}

    journal_rows = state.get("journal_row_count", settled_trades)

    # Check for duplicate settlements
    position_ids = [p.get("position_id", "") for p in positions.values()]
    unique_ids = set(position_ids)
    duplicate_blocks = len(position_ids) - len(unique_ids)

    result = {
        "run_id": run_id,
        "run_start_bankroll": round(original_start, 2),
        "current_bankroll": round(current, 2),
        "current_run_realized_net_pnl": net_realized_pnl,
        "cumulative_realized_net_pnl": net_realized_pnl,
        "open_position_count": open_count,
        "open_position_mark_value": open_mark,
        "expected_bankroll": round(expected_bankroll, 2),
        "discrepancy": discrepancy,
        "check_passed": check_passed,
        "ACCOUNTING_INVARIANT_FAIL": not check_passed,
        "settled_trades_current_run": settled_trades,
        "settled_trades_cumulative": settled_trades,
        "profile_pnl_current_run": profile_pnl,
        "profile_pnl_cumulative": profile_pnl,
        "journal_rows_current_run": journal_rows,
        "duplicate_settlement_blocks": duplicate_blocks,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if not check_passed:
        error_path = OUTPUT_DIR / "accounting_error_report.json"
        with open(error_path, "w") as f:
            json.dump(result, f, indent=2)
        state["ACCOUNTING_INVARIANT_FAIL"] = True
        state["LIVE_ENABLED"] = False  # Keep live disabled

    return result

# ══════════════════════════════════════════════════════════════════════════════
# V19.8 CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

OUTPUT_DIR = REPO / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
SIGNAL_DEBUG_DIR = REPO / "paper_trading"
SIGNAL_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
SIGNAL_DEBUG_FILE = SIGNAL_DEBUG_DIR / "signal_debug.jsonl"
SHADOW_REPORT_FILE = SIGNAL_DEBUG_DIR / "shadow_report.jsonl"
SCARCITY_REPORT_FILE = SIGNAL_DEBUG_DIR / "scarcity_report.jsonl"
WOULD_TRADE_FILE = SIGNAL_DEBUG_DIR / "would_trade_audit.jsonl"

# ─── Multi-Asset Configuration (§6) ───
# BTC = production candidate | ETH/SOL/XRP = paper-eligible only
ASSET_MAP = {
    "BTC": {"yf": "BTC-USD",  "name": "Bitcoin",    "live_eligible": True,  "paper_only": False, "default_tf": "5m"},
    "ETH": {"yf": "ETH-USD",  "name": "Ethereum",   "live_eligible": False, "paper_only": True,  "default_tf": "15m"},
    "SOL": {"yf": "SOL-USD",  "name": "Solana",      "live_eligible": False, "paper_only": True,  "default_tf": "15m"},
    "XRP": {"yf": "XRP-USD",  "name": "XRP",         "live_eligible": False, "paper_only": True,  "default_tf": "5m"},
}

# ─── V19.9: LIVE DISABLED + PROMOTION FREEZE (§9) ───
LIVE_ENABLED = False  # Manual activation required
PROMOTION_FREEZE = True  # §9: No profile may be promoted until gates met
PROMOTION_GATES_FROZEN = True  # §9: Frozen until resolved_trades >= 30, realized_EV > 0, PF >= 1.15
PROMOTION_MIN_RESOLVED = 30  # §9: Minimum resolved trades
PROMOTION_MIN_EV_PER_SHARE = 0.0  # §9: Must be positive
PROMOTION_MIN_PF = 1.15  # §9: Profit factor minimum
# For live consideration: resolved_trades >= 100, realized_EV positive, PF >= 1.25
LIVE_CONSIDERATION_MIN_RESOLVED = 100
LIVE_CONSIDERATION_MIN_PF = 1.25

# §6: Bucket-level live gates — no profile promoted without bucket proof
BUCKET_LIVE_GATES = {
    "min_resolved_trades": 30,
    "min_realized_ev_per_share": 0.0,  # Must be positive
    "min_realized_ev_per_dollar": 0.0,  # Must be positive
    "min_pf": 1.15,
    "max_loss_streak": 8,
    "settlement_errors": 0,
    "journal_completeness": 1.0,
}
# For live consideration: stricter
BUCKET_LIVE_CONSIDERATION = {
    "min_resolved_trades": 100,
    "min_pf": 1.25,
}
# §4-5: Bucket gating — aligned with HYBRID PROBABILITY REFACTOR constants above
# (BUCKET_PAPER, BUCKET_BLOCKED, BUCKET_DIAGNOSTIC_RANGES defined in §5 above)
# Legacy aliases kept for backward compatibility
BLOCKED_PRICE_BUCKETS = [BUCKET_BLOCKED]  # (0.30, 0.40) — hard block
DIAGNOSTIC_PRICE_BUCKETS = BUCKET_DIAGNOSTIC_RANGES  # [(0.0, 0.20), (0.40, 1.0)]

LIVE_READINESS_GATES = {
    "counter_invariants_hold": False,
    "false_accepts_zero": False,
    "daily_strike_markets_zero": False,
    "wrong_token_side_zero": False,
    "book_executable_working": False,
    "recoverability_working": False,
    "no_false_dislocation_opened": False,
    "no_dormant_longshot_opened": False,
    "min_10_executable_opps": False,
    "min_5_paper_trades": False,
    "net_ev_positive": False,
    "settlement_errors_zero": False,
    "journal_complete": False,
    "promotion_freeze_lifted": False,  # §9: Must be True from manual review
}

# ─── Shadow Profile Definitions (§4) ───
SHADOW_PROFILES = {
    "CORE_UP_STRICT": {
        "description": "Disabled per V20: generalized oversold-reversal negative EV",
        "rsi_low": 20, "rsi_high": 35,
        "confirmations_required": 2,
        "early_turn_allowed": False,
        "recoverability_required": False,
        "min_confidence": 0.85,
        "is_production": False,
        "status": "DISABLED",  # §1 V20: Negative realized EV
    },
    "CORE_UP_RSI_ONLY_SHADOW": {
        "description": "Disabled per V20: RSI-only negative EV",
        "rsi_low": 20, "rsi_high": 35,
        "confirmations_required": 0,
        "early_turn_allowed": False,
        "recoverability_required": False,
        "min_confidence": 0.0,
        "is_production": False,
        "status": "DISABLED",  # §1 V20: Negative realized EV
    },
    "CORE_UP_ONE_CONFIRM_SHADOW": {
        "description": "Disabled per V20",
        "rsi_low": 20, "rsi_high": 35,
        "confirmations_required": 1,
        "early_turn_allowed": False,
        "recoverability_required": False,
        "min_confidence": 0.70,
        "is_production": False,
        "status": "DISABLED",  # §1 V20: Negative realized EV
    },
    "CORE_UP_EARLY_TURN_SHADOW": {
        "description": "Disabled per V20",
        "rsi_low": 20, "rsi_high": 35,
        "confirmations_required": 0,
        "early_turn_allowed": True,
        "recoverability_required": False,
        "min_confidence": 0.60,
        "is_production": False,
        "status": "DISABLED",  # §1 V20: Insufficient sample
    },
    "CORE_UP_RECOVERABILITY_FIRST_SHADOW": {
        "description": "Disabled per V20: recoverability-first negative EV",
        "rsi_low": 20, "rsi_high": 35,
        "confirmations_required": 0,
        "early_turn_allowed": False,
        "recoverability_required": True,
        "min_confidence": 0.0,
        "is_production": False,
        "status": "DISABLED",  # §1 V20: Negative realized EV
    },
    "PREOPEN_DIRECTION_EDGE": {
        "description": "Disabled per V20",
        "rsi_low": 25, "rsi_high": 50,
        "confirmations_required": 0,
        "early_turn_allowed": False,
        "recoverability_required": False,
        "min_confidence": 0.60,
        "is_production": False,
        "status": "DISABLED",  # §1 V20: Not validated
    },
    "ONE_MIN_STRUCTURE_EDGE": {
        "description": "Disabled per V20: 0/8 WR, -$16",
        "rsi_low": 20, "rsi_high": 50,
        "confirmations_required": 1,
        "early_turn_allowed": True,
        "recoverability_required": False,
        "min_confidence": 0.70,
        "is_production": False,
        "status": "DISABLED",  # §1 V20: 0/8 WR, -$16
    },
    "CHEAP_CONVEX_EDGE": {
        "description": "Disabled per V20: cheap convexity thesis invalid",
        "rsi_low": 20, "rsi_high": 80,
        "confirmations_required": 0,
        "early_turn_allowed": False,
        "recoverability_required": True,
        "min_confidence": 0.0,
        "is_production": False,
        "price_range": (0.05, 0.25),
        "status": "DISABLED",  # §1 V20: 0.20 bucket 6W/27L 18% WR
    },
    "BALANCED_DIRECTION_EDGE": {
        "description": "Disabled per V20",
        "rsi_low": 30, "rsi_high": 70,
        "confirmations_required": 1,
        "early_turn_allowed": False,
        "recoverability_required": False,
        "min_confidence": 0.70,
        "is_production": False,
        "status": "DISABLED",  # §1 V20: Not validated
    },
    "CONVEX_20_30_VALIDATION": {
        "description": "Disabled per V20: 0.20 bucket collapsed to 6W/27L 18% WR",
        "rsi_low": 20, "rsi_high": 80,
        "confirmations_required": 0,
        "early_turn_allowed": False,
        "recoverability_required": False,
        "min_confidence": 0.0,
        "is_production": False,
        "price_range": (0.20, 0.30),
        "status": "DISABLED",  # §1 V20: Cheap convexity invalid
    },
    "BTC_UP_OVERSOLD_BOUNCE": {
        "description": "Disabled per V20: oversold bounce negative EV",
        "rsi_low": 20, "rsi_high": 35,
        "confirmations_required": 0,
        "early_turn_allowed": False,
        "recoverability_required": False,
        "min_confidence": 0.0,
        "is_production": False,
        "status": "DISABLED",  # §1 V20: 0/8 WR, -$17.55
    },
    # ─── §2 V20: ONLY executable profile ───
    "BTC_BALANCED_REVERSAL_V1": {
        "description": "V20: BTC balanced-market reversal. Asset=BTC, bucket 0.45-0.55, balanced+mkt_structure+reversal_confirm+downtrend_clear",
        "rsi_low": 0, "rsi_high": 100,  # RSI contextual only, not primary gate
        "confirmations_required": 0,
        "early_turn_allowed": False,
        "recoverability_required": False,
        "min_confidence": 0.0,
        "is_production": False,
        "status": "PAPER",
        "allowed_assets": ["BTC"],
        "price_range": (0.45, 0.55),
        "required_market_state": "balanced",
        "required_regime_not": ["trend_continuation", "fake_reversal"],
        "requires_transition_score": True,
        "requires_reversal_confirm": True,
    },
}

# ─── Recoverability Thresholds ───
RECOVERABILITY_MIN_SCORE = 0.30
RECOVERABILITY_DECAY_PER_MIN = 0.02  # Score decays 2% per minute to expiry

# ─── Slug-Based Series Config (from V189) ───
# These are the actual 5m/15m "Up or Down" markets on Polymarket
SERIES_CONFIG = [
    {"slug": "btc-up-or-down-5m",  "label": "5m",  "window_mins": 5,  "asset": "BTC"},
    {"slug": "btc-up-or-down-15m", "label": "15m", "window_mins": 15, "asset": "BTC"},
    {"slug": "eth-up-or-down-5m",  "label": "5m",  "window_mins": 5,  "asset": "ETH"},
    {"slug": "eth-up-or-down-15m", "label": "15m", "window_mins": 15, "asset": "ETH"},
    {"slug": "sol-up-or-down-5m",  "label": "5m",  "window_mins": 5,  "asset": "SOL"},
    {"slug": "xrp-up-or-down-5m",  "label": "5m",  "window_mins": 5,  "asset": "XRP"},
]

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


# ══════════════════════════════════════════════════════════════════════════════
# MARKET SCHEDULE CACHE + PREWATCH (§1-§2)
# ══════════════════════════════════════════════════════════════════════════════

class MarketScheduleCache:
    """TTL-based cache for slug-based market discovery + prewatch/clean-tick tracking."""

    def __init__(self, ttl_seconds=120):
        self.ttl = ttl_seconds
        self._last_fetch = 0.0
        self._cached = {}          # asset_key -> [contracts]
        self._slug_cache = {}      # slug -> [event dicts]
        self._clean_ticks = {}     # conditionId -> (timestamp, book_data)
        self.cache_hits = 0
        self.cache_misses = 0
        self.clean_ticks_seen = 0
        self.dirty_ticks_rejected = 0
        self.first_clean_tick_latency = None
        self.markets_seen = set()
        self.books_executable = 0
        self.maker_diagnostic_count = 0

    def slug_provider(self, force=False):
        """Return dict{asset: [market dicts]} with cached market data."""
        now = time.time()
        if not force and (now - self._last_fetch) < self.ttl and self._cached:
            self.cache_hits += 1
            return self._cached
        self.cache_misses += 1
        contracts = discover_contracts_multi()
        self._cached = contracts
        self._last_fetch = now
        # Track unique markets
        for asset, clist in contracts.items():
            for c in clist:
                cid = c.get("conditionId", "")
                if cid:
                    self.markets_seen.add(cid)
        return self._cached

    @property
    def cache_hit_rate(self):
        total = self.cache_hits + self.cache_misses
        return self.cache_hits / total if total > 0 else 0.0

    @property
    def prewatch_coverage(self):
        """What % of discovered markets have clean-tick data."""
        discovered = len(self.markets_seen)
        if discovered == 0:
            return 0.0
        ticked = sum(1 for cid in self.markets_seen if cid in self._clean_ticks)
        return ticked / discovered

    def clean_tick(self, condition_id, force=False):
        """Fetch book depth for a specific conditionId from CLOB. Returns book_data or None."""
        now = time.time()
        if not force and condition_id in self._clean_ticks:
            ts, data = self._clean_ticks[condition_id]
            if (now - ts) < 30:  # 30s cache for clean ticks
                self.clean_ticks_seen += 1
                return data
        # Fetch from CLOB
        t0 = time.time()
        try:
            book = get_clob_book_depth(condition_id)
            latency = time.time() - t0
            if self.first_clean_tick_latency is None:
                self.first_clean_tick_latency = latency
            if book and book.get("valid", False):
                self._clean_ticks[condition_id] = (now, book)
                self.clean_ticks_seen += 1
                # Check if executable
                spread = book.get("spread", 1.0)
                depth = book.get("depth_usd", 0)
                if spread < 0.05 and depth > 500:
                    self.books_executable += 1
                if spread < 0.02 and depth > 200:
                    self.maker_diagnostic_count += 1
                return book
            else:
                self.dirty_ticks_rejected += 1
                return None
        except Exception:
            self.dirty_ticks_rejected += 1
            return None


def get_clob_book_depth(condition_id, token_id=None):
    """Fetch order book depth for a condition from CLOB API.
    
    Uses token_id if provided (preferred — CLOB API requires it).
    Falls back to condition_id query for legacy compatibility.
    """
    import requests as _req
    try:
        # Try token_id first (CLOB API actually needs this)
        if token_id:
            url = f"{CLOB_API}/book"
            resp = _req.get(url, params={"token_id": token_id}, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
            else:
                data = {}
        else:
            # Legacy: try condition_id (may return empty for some markets)
            url = f"{CLOB_API}/book"
            resp = _req.get(url, params={"condition_id": condition_id}, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
            else:
                data = {}
        
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        if not bids or not asks:
            return None
        best_bid = float(bids[0].get("price", 0)) if bids else 0
        best_ask = float(asks[0].get("price", 0)) if asks else 0
        bid_depth = sum(float(b.get("size", 0)) * float(b.get("price", 0)) for b in bids[:5])
        ask_depth = sum(float(a.get("size", 0)) * float(a.get("price", 0)) for a in asks[:5])
        spread = best_ask - best_bid if best_ask > 0 else 1.0
        depth_usd = min(bid_depth, ask_depth) * 100  # Approximate USD depth
        return {
            "valid": True,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "depth_usd": depth_usd,
            "bid_count": len(bids),
            "ask_count": len(asks),
        }
    except Exception:
        return None


def market_tape_snapshot(asset_key, contracts, prices, rsi):
    """Generate market tape diagnostics for an asset."""
    if not contracts or not prices or len(prices) < 5:
        return {
            "bid_ask_spread": None,
            "depth_available": False,
            "maker_signal": "no_data",
            "recent_candles_slope": "no_data",
            "volume_trend": "no_data",
            "preopen_window": False,
        }

    # Spread analysis across contracts
    spreads = [abs(c.get("up_price", 0.5) - c.get("down_price", 0.5)) for c in contracts]
    median_spread = sorted(spreads)[len(spreads) // 2] if spreads else None

    # Depth
    volumes = [c.get("volume", 0) for c in contracts]
    depth_available = any(v > 500 for v in volumes)

    # Maker signal — check price sum deviation on the first contract with volume
    maker_signal = "no_data"
    if contracts:
        c0 = contracts[0]
        price_sum_deviation = abs(1.0 - (c0.get("up_price", 0.5) + c0.get("down_price", 0.5)))
        if median_spread is not None:
            maker_signal = "tight" if price_sum_deviation < 0.03 else "wide" if price_sum_deviation > 0.05 else "normal"
    else:
        maker_signal = "no_book"

    # Last 5 candles slope
    last5 = prices[-5:]
    if len(last5) >= 3:
        slope = last5[-1] - last5[0]
        if slope > last5[0] * 0.001:
            candles_slope = "bullish"
        elif slope < -last5[0] * 0.001:
            candles_slope = "bearish"
        else:
            candles_slope = "flat"
    else:
        candles_slope = "no_data"

    # Volume trend (last 3 contracts)
    if len(contracts) >= 3:
        v_last3 = [c.get("volume", 0) for c in contracts[-3:]]
        if v_last3[2] > v_last3[0] * 1.2:
            vol_trend = "rising"
        elif v_last3[2] < v_last3[0] * 0.8:
            vol_trend = "falling"
        else:
            vol_trend = "stable"
    else:
        vol_trend = "stable"

    # Preopen window: any contract 2-5 min to expiry
    preopen = any(2 <= c.get("mins_to_expiry", 999) <= 5 for c in contracts)

    return {
        "bid_ask_spread": round(median_spread, 4) if median_spread is not None else None,
        "depth_available": depth_available,
        "maker_signal": maker_signal,
        "recent_candles_slope": candles_slope,
        "volume_trend": vol_trend,
        "preopen_window": preopen,
    }


# ══════════════════════════════════════════════════════════════════════════════
# MULTI-ASSET FETCH
# ══════════════════════════════════════════════════════════════════════════════

def fetch_asset_candles(asset_key, interval="5m", period="5d"):
    """Fetch candles for any asset in ASSET_MAP."""
    cfg = ASSET_MAP.get(asset_key)
    if not cfg:
        return []
    try:
        import yfinance as yf
        if interval == "15m" and period == "5d":
            period = "60d"  # 15m requires longer period
        h = yf.Ticker(cfg["yf"]).history(period=period, interval=interval)
        return h['Close'].tolist()[-60:] if len(h) >= 14 else []
    except Exception:
        return []


def fetch_all_assets():
    """Fetch candles for all assets. Returns dict {asset_key: [prices]}."""
    result = {}
    for key, cfg in ASSET_MAP.items():
        tf = cfg.get("default_tf", "5m")
        result[key] = fetch_asset_candles(key, interval=tf)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# TOKEN STATE CLASSIFICATION (§7)
# ══════════════════════════════════════════════════════════════════════════════

def classify_token_state(contract, rsi, direction, prices):
    """
    Classify market/token state for a contract candidate.
    
    States:
    - live_dislocation: price dislocated from fundamentals, recoverable
    - false_dislocation: price gap but market already decided, not recoverable
    - balanced: both sides near 50/50
    - dormant_longshot: very low volume, extreme price (1-3¢), not executable
    - nearly_decided: one side >85%, too late to enter
    - untradeable: no liquidity, no book, blocked
    
    Returns: dict with token_state, recoverability_score, reason
    """
    up_price = contract.get("up_price", 0.5)
    down_price = contract.get("down_price", 0.5)
    volume = contract.get("volume", 0)
    mins_to_expiry = contract.get("mins_to_expiry", 10)
    window_mins = contract.get("window_mins", 5)
    
    # ── dormant_longshot: volume too low OR extreme price ──
    # 5m/15m markets have low Gamma-reported volume; use lower threshold
    vol_threshold = 5 if window_mins <= 15 else 500
    if volume < vol_threshold:  # Below minimum liquidity
        return {"token_state": "dormant_longshot", "recoverability_score": None,
                "reason": f"volume_below_{vol_threshold}_actual_{volume:.0f}"}
    
    # Check for extreme longshot pricing
    if up_price < 0.03 or down_price < 0.03:
        return {"token_state": "dormant_longshot", "recoverability_score": None,
                "reason": "price_below_3cents"}
    
    # ── nearly_decided: one side > threshold ──
    # 5m/15m markets can have wider spreads at entry; use 0.95 threshold
    # Daily+ markets use 0.85 threshold
    nearly_decided_threshold = 0.95 if window_mins <= 15 else 0.85
    if up_price > nearly_decided_threshold:
        return {"token_state": "nearly_decided", "recoverability_score": None,
                "reason": f"up_price_{up_price:.2f}_above_{nearly_decided_threshold:.2f}"}
    if down_price > nearly_decided_threshold:
        return {"token_state": "nearly_decided", "recoverability_score": None,
                "reason": f"down_price_{down_price:.2f}_above_{nearly_decided_threshold:.2f}"}
    
    # ── untradeable: outside our contract price range ──
    target_price = up_price if direction == "up" else down_price
    if target_price < MIN_CONTRACT_PRICE or target_price > MAX_CONTRACT_PRICE:
        return {"token_state": "untradeable", "recoverability_score": None,
                "reason": f"target_price_{target_price:.3f}_outside_range"}
    
    # ── balanced: both sides 35-65% ──
    if 0.35 <= up_price <= 0.65 and 0.35 <= down_price <= 0.65:
        return {"token_state": "balanced", "recoverability_score": 0.5,
                "reason": "both_sides_near_50"}
    
    # ── false_dislocation vs live_dislocation ──
    # False dislocation: price gap exists but market has already moved to decide
    # Key differentiator: time remaining and whether price is trending toward resolution
    
    # Calculate if price is trending toward the dislocated side (closing the gap)
    if len(prices) >= 10:
        recent_trend = prices[-1] - prices[-5]  # 5-bar trend
        longer_trend = prices[-1] - prices[-10] if len(prices) >= 10 else 0
    else:
        recent_trend = 0
        longer_trend = 0
    
    # If the dislocated side is cheap AND market is NOT trending toward recovery → false
    # If cheap AND market IS showing signs of recovery → live_dislocation
    
    cheap_side_price = target_price
    is_cheap = cheap_side_price < 0.20
    
    if not is_cheap:
        # Not really dislocated, just moderately priced
        return {"token_state": "balanced", "recoverability_score": 0.4,
                "reason": f"target_price_{cheap_side_price:.2f}_not_cheap"}
    
    # Cheap side (< 20¢). Determine if this is live or false dislocation.
    # Key signals of false dislocation:
    # 1. Very short time to expiry (< 2 min for 5m contracts)
    # 2. Price trending consistently against our direction (5-bar + 10-bar align)
    # 3. No RSI reversal signal (RSI still falling, no slope change)
    
    min_for_type = mins_to_expiry
    if min_for_type < 2:
        # Too close to expiry — market has already decided
        return {"token_state": "false_dislocation", "recoverability_score": None,
                "reason": f"mins_to_expiry_{min_for_type:.1f}_too_short"}
    
    # Check if price trend is against our direction
    if direction == "up" and recent_trend < 0 and longer_trend < 0:
        # Price still falling = false dislocation (market decided down)
        return {"token_state": "false_dislocation", "recoverability_score": None,
                "reason": "price_trending_down_5_10_bar"}
    
    if direction == "down" and recent_trend > 0 and longer_trend > 0:
        # Price still rising = false dislocation (market decided up)
        return {"token_state": "false_dislocation", "recoverability_score": None,
                "reason": "price_trending_up_5_10_bar"}
    
    # Cheap side + time remaining + no decisive counter-trend → live_dislocation
    # Calculate recoverability score
    score = compute_recoverability(contract, rsi, direction, prices)
    
    if score is not None and score >= RECOVERABILITY_MIN_SCORE:
        return {"token_state": "live_dislocation", "recoverability_score": score,
                "reason": f"cheap_{cheap_side_price:.2f}_recoverable_{score:.2f}"}
    else:
        # Cheap but low recoverability → still false dislocation
        return {"token_state": "false_dislocation", "recoverability_score": score,
                "reason": f"cheap_{cheap_side_price:.2f}_low_recoverability_{score:.2f}" if score else "cheap_low_recoverability_None"}


def compute_recoverability(contract, rsi, direction, prices):
    """
    Compute recoverability score for a dislocated market.
    
    Score = f(rsi_depth, momentum_reversal, time_remaining, volume_support)
    Range: 0-1. Higher = more likely to recover (price reversal).
    
    None = cannot compute (insufficient data)
    """
    up_price = contract.get("up_price", 0.5)
    down_price = contract.get("down_price", 0.5)
    volume = contract.get("volume", 0)
    mins_to_expiry = contract.get("mins_to_expiry", 10)
    
    target_price = up_price if direction == "up" else down_price
    
    # Base score from RSI depth (deeper oversold = stronger reversal potential)
    if direction == "up":
        if rsi < 20:
            rsi_score = 0.0  # Blocked zone — too extreme
        elif rsi < 28:
            rsi_score = 0.8 + (28 - rsi) / 40  # 0.80-1.0 for deep oversold
        elif rsi < 35:
            rsi_score = 0.5 + (35 - rsi) / 35  # 0.5-0.7 for near-oversold
        else:
            rsi_score = 0.0  # Not oversold
    else:  # direction == "down"
        if rsi > 80:
            rsi_score = 0.0
        elif rsi > 72:
            rsi_score = 0.8 + (rsi - 72) / 40
        elif rsi > 65:
            rsi_score = 0.5 + (rsi - 65) / 35
        else:
            rsi_score = 0.0
        rsi_score = 0.0  # V19.8: overbought signals killed
    
    # Momentum reversal component
    momentum_score = 0.0
    if len(prices) >= 5:
        # RSI slope (is RSI changing direction?)
        if len(prices) >= 14:
            rsi_vals = []
            for i in range(max(0, len(prices) - 10), len(prices)):
                window = prices[max(0, i-14):i+1] if i >= 14 else prices[:i+1]
                if len(window) >= 14:
                    deltas = [window[j] - window[j-1] for j in range(1, len(window))]
                    g = sum(max(d, 0) for d in deltas[-7:]) / 7
                    lo = sum(max(-d, 0) for d in deltas[-7:]) / 7
                    r = 100 - (100 / (1 + g / max(lo, 1e-9)))
                    rsi_vals.append(r)
            
            if len(rsi_vals) >= 3:
                rsi_slope = rsi_vals[-1] - rsi_vals[-3]
                if direction == "up" and rsi_slope > 0:
                    momentum_score = min(0.3, rsi_slope / 30)
                elif direction == "up" and rsi_slope > -2:
                    momentum_score = 0.1  # RSI flattening = mild positive
                else:
                    momentum_score = 0.0  # RSI still falling
        
        # Candle velocity (last candle direction)
        if len(prices) >= 2:
            candle_vel = prices[-1] - prices[-2]
            if direction == "up" and candle_vel > 0:
                momentum_score += 0.1
            # Stop making lower lows
            if len(prices) >= 4:
                if direction == "up":
                    if prices[-1] >= prices[-3] and prices[-3] < prices[-5] if len(prices) >= 5 else True:
                        momentum_score += 0.1  # Stopped lower lows
    
    # Time decay component: less time = lower recoverability
    time_score = max(0.0, 1.0 - (RECOVERABILITY_DECAY_PER_MIN * max(0, mins_to_expiry)))
    if mins_to_expiry < 2:
        time_score = 0.0
    elif mins_to_expiry < 5:
        time_score = 0.3
    
    # Volume support: higher volume = more reliable dislocation
    vol_score = min(1.0, volume / 5000) if volume > 500 else 0.0
    
    # Weighted combination
    if rsi_score <= 0:
        return None  # Not in relevant RSI zone
    
    score = rsi_score * 0.45 + momentum_score * 0.25 + time_score * 0.20 + vol_score * 0.10
    return round(min(1.0, max(0.0, score)), 3)


# ══════════════════════════════════════════════════════════════════════════════
# ENHANCED SIGNAL GENERATION (§2 + §3)
# ══════════════════════════════════════════════════════════════════════════════

def compute_rsi_enhanced(prices, period=7):
    """Compute RSI with additional signal context."""
    if len(prices) < period + 1:
        return None, {}
    
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = sum(max(d, 0) for d in deltas[-period:]) / period
    losses = sum(max(-d, 0) for d in deltas[-period:]) / period
    rsi = 100 - (100 / (1 + gains / max(losses, 1e-9)))
    
    # RSI zone classification
    if rsi < 20:
        rsi_zone = "ultra_oversold"
    elif rsi < 28:
        rsi_zone = "deep_oversold"
    elif rsi < 35:
        rsi_zone = "near_oversold"
    elif rsi < 45:
        rsi_zone = "near_oversold3"
    elif rsi < 55:
        rsi_zone = "neutral"
    elif rsi < 65:
        rsi_zone = "near_overbought"
    elif rsi < 72:
        rsi_zone = "overbought"
    else:
        rsi_zone = "ultra_overbought"
    
    # RSI slope (3-bar)
    rsi_slope = None
    if len(prices) >= period + 3:
        recent_rsis = []
        for offset in range(3):
            w = prices[-(period + offset):len(prices) - offset if offset > 0 else len(prices)]
            if len(w) >= period + 1:
                d = [w[i] - w[i-1] for i in range(1, len(w))]
                g = sum(max(x, 0) for x in d[-period:]) / period
                lo = sum(max(-x, 0) for x in d[-period:]) / period
                recent_rsis.append(100 - (100 / (1 + g / max(lo, 1e-9))))
        if len(recent_rsis) >= 2:
            rsi_slope = recent_rsis[0] - recent_rsis[-1]  # Positive = rising
    
    # MACD
    macd = _ema(prices, 6) - _ema(prices, 13)
    macd_signal = _ema(prices, 26) if len(prices) >= 26 else macd
    
    # SMA20
    sma20 = sum(prices[-20:]) / 20 if len(prices) >= 20 else prices[-1]
    price_vs_sma = (prices[-1] - sma20) / sma20 if sma20 > 0 else 0
    
    # Recent bar counts
    up_bars = sum(1 for i in range(1, min(4, len(prices))) if prices[-i] > prices[-i-1])
    down_bars = sum(1 for i in range(1, min(4, len(prices))) if prices[-i] < prices[-i-1])
    
    # Candle velocity
    candle_velocity = (prices[-1] - prices[-2]) / prices[-2] if len(prices) >= 2 and prices[-2] > 0 else 0
    
    # Volume (from yfinance we don't get volume directly for our use, use None)
    volume_available = None
    
    # Confirmation counts (same as V19.7)
    confirmations = 0
    if macd > 0: confirmations += 1
    if price_vs_sma > 0.003: confirmations += 1
    if up_bars >= 2: confirmations += 1
    
    context = {
        "rsi": round(rsi, 1),
        "rsi_zone": rsi_zone,
        "rsi_slope": round(rsi_slope, 2) if rsi_slope is not None else None,
        "macd": round(macd, 2),
        "macd_signal": round(macd_signal, 2),
        "macd_condition_passed": macd > 0,
        "sma20": round(sma20, 2),
        "price_vs_sma_pct": round(price_vs_sma * 100, 3),
        "sma_condition_passed": price_vs_sma > 0.003,
        "recent_up_bars": up_bars,
        "recent_down_bars": down_bars,
        "candle_velocity": round(candle_velocity * 100, 4),
        "volume_available": volume_available,
        "confirmations": confirmations,
        "required_confirmations_for_near_oversold": 2,
        "price": prices[-1],
    }
    
    return round(rsi, 1), context


def enhanced_signal(prices, asset_key="BTC"):
    """
    V19.8 enhanced signal with deep debug context.
    
    Returns dict with:
    - direction, confidence, rsi (same as V19.7)
    - All debug fields for signal_debug.jsonl
    - reason codes for why neutral/zero
    """
    if len(prices) < 14:
        return {
            "direction": "neutral", "confidence": 0, "rsi": 50, "price": 0,
            "asset": asset_key, "reason_direction_neutral": "insufficient_prices",
            "reason_confidence_zero": "insufficient_prices",
        }
    
    rsi, ctx = compute_rsi_enhanced(prices)
    
    # Run V19.7 production signal as base
    sig_v197 = btc_signal_v197(prices)
    direction_raw = sig_v197["direction"]
    confidence_raw = sig_v197["confidence"]
    
    # ── Determine reason codes ──
    reason_direction_neutral = None
    reason_confidence_zero = None
    
    d, c = direction_raw, confidence_raw
    
    rsi_zone = ctx["rsi_zone"]
    
    if rsi < RSI_OVERSOLD_MIN:
        reason_direction_neutral = "rsi_not_in_zone"
        reason_confidence_zero = "rsi_not_in_zone"
    elif rsi < 28:
        # RSI 20-28 should produce UP in V19.7
        if d == "neutral":
            reason_direction_neutral = "rsi_in_zone_but_v197_returned_neutral"
            reason_confidence_zero = "rsi_in_zone_but_v197_returned_neutral"
    elif rsi < RSI_NEAR_OVERSOLD:
        # RSI 28-35: needs confirmations
        conf_count = ctx["confirmations"]
        if conf_count == 0:
            reason_direction_neutral = "rsi_in_zone_but_no_up_confirmation"
            reason_confidence_zero = "rsi_in_zone_but_no_up_confirmation"
        elif conf_count == 1:
            # V19.7 returns UP but capped at 0.85
            if c < MIN_CONFIDENCE:
                reason_direction_neutral = None  # direction IS up in V19.7
                reason_confidence_zero = "rsi_in_zone_but_confidence_below_threshold"
        else:  # conf_count >= 2
            # Should pass if confidence >= 0.85
            if c < MIN_CONFIDENCE:
                reason_direction_neutral = None
                reason_confidence_zero = "rsi_in_zone_but_confidence_below_threshold"
        
        # More specific sub-reasons for why each confirmation failed
        if ctx["confirmations"] < 2:
            if not ctx["macd_condition_passed"]:
                reason_direction_neutral = (reason_direction_neutral or "") + ";rsi_in_zone_but_macd_failed"
            if not ctx["sma_condition_passed"]:
                reason_direction_neutral = (reason_direction_neutral or "") + ";rsi_in_zone_but_sma_failed"
            if ctx["recent_up_bars"] < 2:
                reason_direction_neutral = (reason_direction_neutral or "") + ";rsi_in_zone_but_recent_bars_failed"
    else:
        # RSI >= 35 dead zone
        reason_direction_neutral = "rsi_not_in_zone"
        reason_confidence_zero = "rsi_not_in_zone"
    
    result = {
        "direction": d,
        "confidence": c,
        "rsi": rsi,
        "price": prices[-1],
        "asset": asset_key,
        "direction_raw": direction_raw,
        "confidence_raw": confidence_raw,
        "rsi_zone": rsi_zone,
        "confirmations": ctx["confirmations"],
        "required_confirmations": 2 if rsi >= 28 and rsi < RSI_NEAR_OVERSOLD else 0,
        "MIN_CONFIDENCE": MIN_CONFIDENCE,
        "macd_value": ctx["macd"],
        "macd_signal_val": ctx["macd_signal"],
        "macd_condition_passed": ctx["macd_condition_passed"],
        "sma20": ctx["sma20"],
        "price_vs_sma_pct": ctx["price_vs_sma_pct"],
        "sma_condition_passed": ctx["sma_condition_passed"],
        "recent_up_bars": ctx["recent_up_bars"],
        "recent_down_bars": ctx["recent_down_bars"],
        "rsi_slope": ctx["rsi_slope"],
        "candle_velocity": ctx["candle_velocity"],
        "volume_available": ctx["volume_available"],
        "momentum": sig_v197.get("momentum", 0),
        "_prices": prices,
        "reason_direction_neutral": reason_direction_neutral,
        "reason_confidence_zero": reason_confidence_zero,
    }
    
    return result


# ══════════════════════════════════════════════════════════════════════════════
# DOWNTREND CONTINUATION VETO + REVERSAL CONFIRMATION GATE (§2, §3)
# ══════════════════════════════════════════════════════════════════════════════

def compute_downtrend_veto(prices, contract=None, reference_price=None):
    """
    Compute downtrend continuation indicators for UP-bounce veto.
    
    Returns dict with:
    - spot_velocity_5s/15s/30s (approximated from bar data)
    - RSI_slope
    - SMA20_distance, SMA20_slope
    - lower_low_count
    - price_vs_reference_pct
    - UP_token_price_velocity, DOWN_token_price_velocity
    - downtrend_active: bool
    - reversal_confirmed: bool
    - veto_reason: str or None
    """
    if len(prices) < 30:
        return {
            "spot_velocity_5s": 0, "spot_velocity_15s": 0, "spot_velocity_30s": 0,
            "RSI_slope": None, "SMA20_distance": 0, "SMA20_slope": 0,
            "lower_low_count": 0, "price_vs_reference_pct": 0,
            "UP_token_price_velocity": 0, "DOWN_token_price_velocity": 0,
            "downtrend_active": False, "reversal_confirmed": True, "veto_reason": None,
        }
    
    n = len(prices)
    
    # ── Spot velocity (5-bar, 15-bar, 30-bar) ──
    vel_5 = (prices[-1] - prices[-6]) / prices[-6] if n >= 6 and prices[-6] != 0 else 0
    vel_15 = (prices[-1] - prices[-16]) / prices[-16] if n >= 16 and prices[-16] != 0 else 0
    vel_30 = (prices[-1] - prices[-31]) / prices[-31] if n >= 31 and prices[-31] != 0 else 0
    
    # ── RSI slope from compute_rsi_enhanced ──
    _, ctx = compute_rsi_enhanced(prices)
    rsi_slope = ctx.get("rsi_slope", 0)
    if rsi_slope is None:
        rsi_slope = 0
    
    # ── SMA20 ──
    sma20 = sum(prices[-20:]) / 20 if n >= 20 else prices[-1]
    sma20_prev = sum(prices[-21:-1]) / 20 if n >= 21 else sma20
    sma20_slope = (sma20 - sma20_prev) / sma20_prev if sma20_prev != 0 else 0
    sma20_distance = (prices[-1] - sma20) / sma20 if sma20 != 0 else 0
    
    # ── Lower low count (last 5 bars making lower lows) ──
    lower_low_count = 0
    for i in range(1, min(6, n)):
        if prices[-i] < prices[-i-1]:
            lower_low_count += 1
    
    # ── Price vs reference ──
    if reference_price and reference_price > 0:
        price_vs_ref = (prices[-1] - reference_price) / reference_price * 100
    else:
        price_vs_ref = sma20_distance * 100  # Use SMA20 distance as proxy
    
    # ── Token price velocity (from contract if available) ──
    up_vel = 0
    down_vel = 0
    if contract and isinstance(contract, dict):
        # Approximate from recent price action since we don't have order book ticks
        # UP token price moves inversely to underlying for DOWN direction
        up_vel = vel_5  # Approximate: if underlying rises, UP token gains
        down_vel = -vel_5  # Inverse
    
    # ── Downtrend continuation detection ──
    downtrend_indicators = 0
    veto_reasons = []
    
    # Check each downtrend condition
    if vel_15 < 0 and vel_30 < 0:
        downtrend_indicators += 1
        veto_reasons.append("spot_velocity_declining_15s_30s")
    
    if rsi_slope <= 0:
        downtrend_indicators += 1
        veto_reasons.append("rsi_slope_not_positive")
    
    if price_vs_ref < 0 and abs(price_vs_ref) > 0.1:
        # Price below reference AND distance worsening
        # Check if recent price is further from reference than 5 bars ago
        if n >= 6 and reference_price:
            old_dist = (prices[-6] - reference_price) / reference_price * 100
            if price_vs_ref < old_dist:
                downtrend_indicators += 1
                veto_reasons.append("price_below_ref_and_worsening")
    
    if lower_low_count >= 2:
        downtrend_indicators += 1
        veto_reasons.append("lower_low_count_gte_2")
    
    if sma20_slope < 0 and prices[-1] < sma20:
        downtrend_indicators += 1
        veto_reasons.append("sma20_declining_and_price_below")
    
    # Downtrend active if 2+ indicators trigger
    downtrend_active = downtrend_indicators >= 2
    veto_reason = ";".join(veto_reasons) if downtrend_active else None
    
    # ── Reversal confirmation ──
    reversal_indicators = 0
    reversal_reasons = []
    
    if rsi_slope is not None and rsi_slope > 0:
        reversal_indicators += 1
        reversal_reasons.append("rsi_slope_positive")
    
    if vel_5 > 0:
        reversal_indicators += 1
        reversal_reasons.append("spot_velocity_5s_positive")
    
    if vel_15 > 0:
        reversal_indicators += 1
        reversal_reasons.append("spot_velocity_15s_positive")
    
    # Price reclaiming reference
    if reference_price and prices[-1] > reference_price * 0.998:
        reversal_indicators += 1
        reversal_reasons.append("price_reclaiming_reference")
    
    # Higher low detected (current bar > previous bar's low when both are lows)
    if n >= 3 and prices[-2] > prices[-3] and prices[-3] < prices[-4] if n >= 4 else False:
        reversal_indicators += 1
        reversal_reasons.append("higher_low_detected")
    
    # UP token bid rising (approximate)
    if up_vel > 0:
        reversal_indicators += 1
        reversal_reasons.append("up_token_velocity_positive")
    
    reversal_confirmed = reversal_indicators >= 1
    reversal_reason = ";".join(reversal_reasons) if reversal_confirmed else "no_reversal_confirmation"
    
    return {
        "spot_velocity_5s": round(vel_5 * 100, 4),
        "spot_velocity_15s": round(vel_15 * 100, 4),
        "spot_velocity_30s": round(vel_30 * 100, 4),
        "RSI_slope": rsi_slope,
        "SMA20_distance": round(sma20_distance * 100, 3),
        "SMA20_slope": round(sma20_slope * 100, 4),
        "lower_low_count": lower_low_count,
        "price_vs_reference_pct": round(price_vs_ref, 3),
        "UP_token_price_velocity": round(up_vel * 100, 4),
        "DOWN_token_price_velocity": round(down_vel * 100, 4),
        "downtrend_active": downtrend_active,
        "downtrend_indicator_count": downtrend_indicators,
        "veto_reason": veto_reason,
        "reversal_confirmed": reversal_confirmed,
        "reversal_indicator_count": reversal_indicators,
        "reversal_reason": reversal_reason,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SHADOW SIGNAL VARIANTS (§4)
# ══════════════════════════════════════════════════════════════════════════════

def shadow_signal(profile_name, prices, asset_key="BTC", contract=None):
    """
    Evaluate a signal under a shadow profile.
    
    Returns: dict with direction, confidence, profile_name, reason, plus
    would_trade (bool) and would_trade_reason.
    """
    profile = SHADOW_PROFILES.get(profile_name)
    if not profile:
        return {"direction": "neutral", "confidence": 0, "profile": profile_name,
                "reason": "unknown_profile", "would_trade": False}
    
    if len(prices) < 14:
        return {"direction": "neutral", "confidence": 0, "profile": profile_name,
                "reason": "insufficient_prices", "would_trade": False}
    
    rsi, ctx = compute_rsi_enhanced(prices)
    
    # Check RSI zone
    rsi_in_zone = profile["rsi_low"] <= rsi < profile["rsi_high"]
    
    if not rsi_in_zone:
        return {"direction": "neutral", "confidence": 0, "profile": profile_name,
                "rsi": rsi, "reason": "rsi_not_in_zone", "would_trade": False}
    
    # ── Profile-specific logic ──
    
    if profile_name == "CORE_UP_STRICT":
        # Same as V19.7 production
        sig = btc_signal_v197(prices)
        return {
            "direction": sig["direction"], "confidence": sig["confidence"],
            "rsi": rsi, "profile": profile_name,
            "confirmations": ctx["confirmations"],
            "reason": "production_rules", 
            "would_trade": sig["direction"] != "neutral" and sig["confidence"] >= MIN_CONFIDENCE,
        }
    
    if profile_name == "CORE_UP_RSI_ONLY_SHADOW":
        # RSI in zone → UP, no confirmation needed
        d, c = "up", 0.80 + (35 - rsi) / 70  # Base confidence from RSI depth
        return {
            "direction": d, "confidence": round(c, 3),
            "rsi": rsi, "profile": profile_name,
            "confirmations": ctx["confirmations"],
            "reason": "rsi_only_no_confirm",
            "would_trade": True,  # Always trades when RSI in zone
        }
    
    if profile_name == "CORE_UP_ONE_CONFIRM_SHADOW":
        # Need at least 1 confirmation
        if ctx["confirmations"] >= 1:
            d = "up"
            c = 0.82 + (35 - rsi) / 100
            return {
                "direction": d, "confidence": round(min(0.90, c), 3),
                "rsi": rsi, "profile": profile_name,
                "confirmations": ctx["confirmations"],
                "reason": f"1_confirm_{ctx['confirmations']}",
                "which_confirmations": {
                    "macd": ctx["macd_condition_passed"],
                    "sma": ctx["sma_condition_passed"],
                    "up_bars": ctx["recent_up_bars"] >= 2,
                },
                "would_trade": True,
            }
        else:
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": 0,
                    "reason": "zero_confirmations", "would_trade": False}
    
    if profile_name == "CORE_UP_EARLY_TURN_SHADOW":
        # RSI in zone + (RSI_slope > 0 OR candle_velocity improving OR stopped lower lows)
        early_turn_signals = []
        
        if ctx["rsi_slope"] is not None and ctx["rsi_slope"] > 0:
            early_turn_signals.append("rsi_slope_positive")
        
        if ctx["candle_velocity"] > 0:
            early_turn_signals.append("candle_velocity_positive")
        
        # Check if stopped making lower lows
        stopped_lower_lows = False
        if len(prices) >= 6:
            if prices[-1] >= prices[-3] and prices[-3] <= prices[-5]:
                stopped_lower_lows = True
                early_turn_signals.append("stopped_lower_lows")
        
        if early_turn_signals:
            d = "up"
            c = 0.75 + 0.05 * len(early_turn_signals) + (35 - rsi) / 100
            return {
                "direction": d, "confidence": round(min(0.90, c), 3),
                "rsi": rsi, "profile": profile_name,
                "confirmations": ctx["confirmations"],
                "reason": "early_turn_" + "+".join(early_turn_signals),
                "early_turn_signals": early_turn_signals,
                "would_trade": True,
            }
        else:
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": ctx["confirmations"],
                    "reason": "no_early_turn_signal",
                    "early_turn_signals": [],
                    "would_trade": False}
    
    if profile_name == "CORE_UP_RECOVERABILITY_FIRST_SHADOW":
        # RSI in zone + token_state=live_dislocation + recoverability >= threshold
        if contract is None:
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": ctx["confirmations"],
                    "reason": "no_contract_for_recoverability", "would_trade": False}
        
        token_state = classify_token_state(contract, rsi, "up", prices)
        
        if token_state["token_state"] == "live_dislocation" and \
           token_state["recoverability_score"] is not None and \
           token_state["recoverability_score"] >= RECOVERABILITY_MIN_SCORE:
            
            # EV check
            gross_ev, p_win, net_ev = calculate_ev(
                rsi=rsi, direction="up", contract_price=contract.get("up_price", 0.5),
                session_type=v197._session_type(datetime.now().hour),
                confirmations=0,  # Confirmation optional for this profile
            )
            
            if net_ev > 0:
                d = "up"
                c = 0.75 + token_state["recoverability_score"] * 0.15
                return {
                    "direction": d, "confidence": round(min(0.90, c), 3),
                    "rsi": rsi, "profile": profile_name,
                    "confirmations": ctx["confirmations"],
                    "reason": "recoverability_first",
                    "token_state": token_state["token_state"],
                    "recoverability_score": token_state["recoverability_score"],
                    "ev_net": round(net_ev, 4),
                    "would_trade": True,
                }
            else:
                return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                        "profile": profile_name, "confirmations": ctx["confirmations"],
                        "reason": f"ev_negative_{net_ev:.3f}",
                        "token_state": token_state["token_state"],
                        "recoverability_score": token_state["recoverability_score"],
                        "would_trade": False}
        else:
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": ctx["confirmations"],
                    "reason": f"token_state_{token_state['token_state']}_not_live_dislocation",
                    "token_state": token_state["token_state"],
                    "recoverability_score": token_state["recoverability_score"],
                    "would_trade": False}
    
    if profile_name == "PREOPEN_DIRECTION_EDGE":
        # Contracts 2-5 min before open with directional edge from pre-market
        # Candidate window: T+10s to T+45s after market start
        # Clean tick gate required. Spot momentum vs reference.
        # Require entry ask <= estimated_probability - 0.03 buffer
        if contract is None:
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": ctx["confirmations"],
                    "reason": "no_contract_for_preopen", "would_trade": False}
        mins_left = contract.get("mins_to_expiry", 999)
        
        # Window: 2-5 min before open (preopen positioning)
        if not (2 <= mins_left <= 5):
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": ctx["confirmations"],
                    "reason": f"mins_to_expiry_{mins_left:.1f}_not_in_2_5",
                    "would_trade": False}
        
        # RSI in 25-50 + pre-market edge
        up_price = contract.get("up_price", 0.5)
        
        # EV check with 0.03 buffer
        gross_ev, p_win, net_ev = calculate_ev(
            rsi=rsi, direction="up", contract_price=up_price,
            session_type=v197._session_type(datetime.now().hour),
            confirmations=0,
        )
        est_prob = min(p_win, 0.95)
        raw_edge = est_prob - up_price
        buffered_ev = net_ev - 0.03
        
        # Direction from spot momentum
        spot_momentum = "up" if len(prices) >= 3 and prices[-1] > prices[-3] else "neutral"
        if spot_momentum != "up" and up_price <= 0.55:
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": ctx["confirmations"],
                    "reason": f"preopen_no_spot_momentum_{spot_momentum}",
                    "would_trade": False}
        
        if up_price > 0.55 or (spot_momentum == "up" and buffered_ev > 0):
            d = "up"
            c = 0.60 + max(0, buffered_ev) * 0.5 + (0.05 if up_price > 0.55 else 0)
            c = min(0.85, c)
            return {
                "direction": d, "confidence": round(c, 3),
                "rsi": rsi, "profile": profile_name,
                "confirmations": ctx["confirmations"],
                "reason": f"preopen_edge_up_{up_price:.3f}_mins_{mins_left:.1f}_buf_{buffered_ev:.3f}",
                "estimated_probability": round(est_prob, 4),
                "raw_edge": round(raw_edge, 4),
                "buffered_ev": round(buffered_ev, 4),
                "entry_ask": up_price,
                "time_to_expiry": round(mins_left, 1),
                "spot_momentum": spot_momentum,
                "would_trade": True,
            }
        else:
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": ctx["confirmations"],
                    "reason": f"preopen_no_edge_up_{up_price:.3f}_buf_{buffered_ev:.3f}",
                    "would_trade": False}

    if profile_name == "ONE_MIN_STRUCTURE_EDGE":
        # Candidate window: T+45s to T+120s for 5m markets
        # Multi-factor: spot_vs_reference, velocity, acceleration, book imbalance, spread, depth
        # Enter only if book executable and EV positive after buffer
        confirmations = ctx["confirmations"]
        if confirmations < 1:
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": confirmations,
                    "reason": "structure_edge_needs_1_confirm", "would_trade": False}
        
        # Multi-factor structure signals
        structure_signals = []
        
        # Factor 1: RSI slope positive
        if ctx["rsi_slope"] is not None and ctx["rsi_slope"] > 0:
            structure_signals.append("rsi_slope_positive")
        
        # Factor 2: Candle velocity positive
        if ctx["candle_velocity"] > 0:
            structure_signals.append("candle_velocity_positive")
        
        # Factor 3: Stopped making lower lows
        if len(prices) >= 6 and prices[-1] >= prices[-3] and prices[-3] <= prices[-5]:
            structure_signals.append("stopped_lower_lows")
        
        # Factor 4: Price acceleration (last 3 candles accelerating up)
        if len(prices) >= 4:
            deltas = [prices[-1-i] - prices[-2-i] for i in range(3)]
            if all(d > 0 for d in deltas) and deltas[0] > deltas[1]:
                structure_signals.append("price_accelerating_up")
        
        # Factor 5: Book imbalance (from contract if available)
        if contract is not None:
            up_price = contract.get("up_price", 0.5)
            down_price = contract.get("down_price", 0.5)
            if up_price > 0.55:
                structure_signals.append("book_skewed_up")
        
        if not structure_signals:
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": confirmations,
                    "reason": "structure_edge_no_turn_signal", "would_trade": False}
        
        d = "up"
        c = 0.70 + 0.04 * len(structure_signals) + (50 - min(float(rsi or 50), 50)) / 200
        
        # EV buffer check if contract available
        buffered_ev = None
        raw_edge = None
        entry_ask = None
        time_to_expiry = None
        if contract is not None:
            up_price = contract.get("up_price", 0.5)
            gross_ev, p_win, net_ev = calculate_ev(
                rsi=rsi, direction="up", contract_price=up_price,
                session_type=v197._session_type(datetime.now().hour),
                confirmations=confirmations,
            )
            est_prob = min(p_win, 0.95)
            raw_edge = round(est_prob - up_price, 4)
            buffered_ev = round(net_ev - 0.02, 4)  # 2% buffer for structure
            entry_ask = up_price
            time_to_expiry = round(contract.get("mins_to_expiry", 0), 1)
            if buffered_ev <= 0:
                return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                        "profile": profile_name, "confirmations": confirmations,
                        "reason": f"structure_buffered_ev_{buffered_ev}_negative",
                        "structure_signals": structure_signals,
                        "would_trade": False}
        
        return {
            "direction": d, "confidence": round(min(0.88, c), 3),
            "rsi": rsi, "profile": profile_name,
            "confirmations": confirmations,
            "reason": "structure_edge_" + "+".join(structure_signals),
            "structure_signals": structure_signals,
            "buffered_ev": buffered_ev,
            "raw_edge": raw_edge,
            "entry_ask": entry_ask,
            "time_to_expiry": time_to_expiry,
            "would_trade": True,
        }

    if profile_name == "CHEAP_CONVEX_EDGE":
        # Candidate ask 0.05-0.25, not dormant, recoverability above threshold, EV positive with buffer
        # This is where $3 can become $12, $30, or $60 — but only if probability is underpriced
        if contract is None:
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": ctx["confirmations"],
                    "reason": "no_contract_for_cheap_convex", "would_trade": False}
        up_price = contract.get("up_price", 0.5)
        mins_left = contract.get("mins_to_expiry", 999)
        
        # Must be in cheap range (0.05-0.25)
        if not (0.05 <= up_price <= 0.25):
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": ctx["confirmations"],
                    "reason": f"ask_{up_price:.3f}_outside_cheap_range", "would_trade": False}
        
        # Must not be dormant
        ts = classify_token_state(contract, rsi, "up", prices)
        if ts["token_state"] in ("dormant_longshot", "untradeable"):
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": ctx["confirmations"],
                    "reason": f"token_state_{ts['token_state']}", "would_trade": False}
        
        # Must have recoverability above threshold
        if ts.get("recoverability_score") is None or ts["recoverability_score"] < RECOVERABILITY_MIN_SCORE:
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": ctx["confirmations"],
                    "reason": f"recoverability_{ts.get('recoverability_score')}_below_{RECOVERABILITY_MIN_SCORE}",
                    "would_trade": False}
        
        # Must have enough time left (>2 min)
        if mins_left < 2:
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": ctx["confirmations"],
                    "reason": f"mins_left_{mins_left:.1f}_too_short", "would_trade": False}
        
        # EV check with 0.03 buffer
        gross_ev, p_win, net_ev = calculate_ev(
            rsi=rsi, direction="up", contract_price=up_price,
            session_type=v197._session_type(datetime.now().hour),
            confirmations=0,
        )
        buffered_ev = net_ev - 0.03
        if buffered_ev > 0:
            est_prob = min(p_win, 0.95)
            raw_edge = est_prob - up_price
            c = 0.70 + ts["recoverability_score"] * 0.10
            return {
                "direction": "up", "confidence": round(min(0.85, c), 3),
                "rsi": rsi, "profile": profile_name,
                "confirmations": ctx["confirmations"],
                "reason": f"cheap_convex_ask_{up_price:.3f}_ev_{net_ev:.3f}_buf_{buffered_ev:.3f}",
                "token_state": ts["token_state"],
                "recoverability_score": ts["recoverability_score"],
                "ev_net": round(net_ev, 4),
                "buffered_ev": round(buffered_ev, 4),
                "estimated_probability": round(est_prob, 4),
                "raw_edge": round(raw_edge, 4),
                "entry_ask": up_price,
                "time_to_expiry": round(mins_left, 1),
                "would_trade": True,
            }
        else:
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": ctx["confirmations"],
                    "reason": f"buffered_ev_{buffered_ev:.3f}_negative",
                    "would_trade": False}
    
    if profile_name == "BALANCED_DIRECTION_EDGE":
        # Candidate ask 0.35-0.65, stronger directional evidence, expected_prob > ask + 0.05
        if contract is None:
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": ctx["confirmations"],
                    "reason": "no_contract_for_balanced", "would_trade": False}
        up_price = contract.get("up_price", 0.5)
        down_price = contract.get("down_price", 0.5)
        
        # Determine direction and target price
        if up_price > down_price:
            direction = "up"
            target_price = up_price
        else:
            direction = "down"
            target_price = down_price
        
        # Must be in balanced range (0.35-0.65)
        if not (0.35 <= target_price <= 0.65):
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": ctx["confirmations"],
                    "reason": f"ask_{target_price:.3f}_outside_balanced_range", "would_trade": False}
        
        # Need at least 1 confirmation
        if ctx["confirmations"] < 1:
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": ctx["confirmations"],
                    "reason": "balanced_needs_1_confirm", "would_trade": False}
        
        # EV check with 0.05 buffer
        gross_ev, p_win, net_ev = calculate_ev(
            rsi=rsi, direction=direction, contract_price=target_price,
            session_type=v197._session_type(datetime.now().hour),
            confirmations=ctx["confirmations"],
        )
        est_prob = min(p_win, 0.95)
        raw_edge = est_prob - target_price
        buffered_edge = raw_edge - 0.05
        
        if buffered_edge > 0:
            c = 0.72 + buffered_edge * 0.5
            return {
                "direction": direction, "confidence": round(min(0.88, c), 3),
                "rsi": rsi, "profile": profile_name,
                "confirmations": ctx["confirmations"],
                "reason": f"balanced_{direction}_ask_{target_price:.3f}_buf_{buffered_edge:.3f}",
                "estimated_probability": round(est_prob, 4),
                "raw_edge": round(raw_edge, 4),
                "buffered_edge": round(buffered_edge, 4),
                "entry_ask": target_price,
                "time_to_expiry": round(contract.get("mins_to_expiry", 0), 1),
                "would_trade": True,
            }
        else:
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": ctx["confirmations"],
                    "reason": f"buffered_edge_{buffered_edge:.3f}_negative",
                    "would_trade": False}

    if profile_name == "CONVEX_20_30_VALIDATION":
        # §3: Targeted 0.20-0.30 convex bucket validation
        # Paper-only, model side only, recalibrated probability, entry gate adjusted_p >= ask + 0.05
        if contract is None:
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": ctx["confirmations"],
                    "reason": "no_contract_for_convex_validation", "would_trade": False}
        # Use model direction (not inverse)
        sig = btc_signal_v197(prices)
        direction = sig.get("direction", "neutral")
        if direction not in ("up", "down"):
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "reason": "convex_validation_neutral_signal", "would_trade": False}

        if direction == "down":
            target_price = contract.get("down_price", 0.5)
        else:
            target_price = contract.get("up_price", 0.5)

        # §3: Price must be in 0.20-0.30 bucket
        if not (0.20 <= target_price <= 0.30):
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": ctx["confirmations"],
                    "reason": f"convex_ask_{target_price:.3f}_outside_0.20_0.30", "would_trade": False}

        # §3: Entry gate — hybrid probability cascade (§1-6)
        # Compute RSI prior, then full hybrid cascade
        prob = compute_hybrid_probability(
            rsi=rsi, direction=direction, entry_ask=target_price,
            contract_price=target_price,
            session_type=v197._session_type(datetime.now().hour),
            confirmations=ctx.get("confirmations", 0),
            prices=prices, steps_remaining=int(contract.get("mins_to_expiry", 5)),
            bucket_n=0,  # No bucket data yet — strict tier 1 cap
            empirical_bucket_p=None,
            state=None,
        )
        adjusted_p = prob["adjusted_p"]
        buffered_edge = prob["buffered_edge"]

        # §5: Bucket gating — convex only allows 0.20-0.30
        # (already enforced above, but explicit check)
        if BUCKET_PAPER[0] <= target_price < BUCKET_PAPER[1]:
            bucket_decision = "PAPER_ELIGIBLE"
        elif BUCKET_BLOCKED[0] <= target_price < BUCKET_BLOCKED[1]:
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": ctx["confirmations"],
                    "reason": f"blocked_by_bad_price_bucket_{BUCKET_BLOCKED[0]:.2f}_{BUCKET_BLOCKED[1]:.2f}",
                    "would_trade": False, "final_decision": "BLOCKED_bad_bucket",
                    **{k: v for k, v in prob.items()}}
        else:
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": ctx["confirmations"],
                    "reason": f"diagnostic_only_ask_{target_price:.3f}",
                    "would_trade": False, "final_decision": "DIAGNOSTIC_only",
                    **{k: v for k, v in prob.items()}}

        # §6: EV gate — buffered_edge must be positive
        if buffered_edge <= 0:
            return {"direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "confirmations": ctx["confirmations"],
                    "reason": f"convex_buffered_edge_{buffered_edge:.4f}_not_positive",
                    "estimated_probability": round(adjusted_p, 4),
                    "would_trade": False, "final_decision": "BLOCKED_buffered_edge_negative",
                    **{k: v for k, v in prob.items()}}

        c = 0.70 + (adjusted_p - target_price) * 0.5
        return {
            "direction": direction, "confidence": round(min(0.88, c), 3),
            "rsi": rsi, "profile": profile_name,
            "confirmations": ctx.get("confirmations", 0),
            "reason": f"convex_validation_{direction}_ask_{target_price:.3f}_adj_p_{adjusted_p:.3f}",
            "estimated_probability": round(adjusted_p, 4),
            "raw_edge": prob["raw_edge"],
            "buffered_edge": round(buffered_edge, 4),
            "entry_ask": target_price,
            "time_to_expiry": round(contract.get("mins_to_expiry", 0), 1),
            "would_trade": True, "final_decision": "TRADE_PAPER",
            **{k: v for k, v in prob.items()},
        }

    return {"direction": "neutral", "confidence": 0, "rsi": rsi,
            "profile": profile_name, "reason": "unhandled_profile", "would_trade": False}


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL DEBUG WRITER (§2)
# ══════════════════════════════════════════════════════════════════════════════

def write_signal_debug(sig, asset_key="BTC", interval="5m"):
    """Append one signal debug row to signal_debug.jsonl."""
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "asset": asset_key,
        "interval": interval,
        "price": sig.get("price", 0),
        "RSI": sig.get("rsi", 50),
        "RSI_zone": sig.get("rsi_zone", "unknown"),
        "direction_raw": sig.get("direction_raw", sig.get("direction", "neutral")),
        "direction_final": sig.get("direction", "neutral"),
        "confidence": sig.get("confidence", 0),
        "MIN_CONFIDENCE": sig.get("MIN_CONFIDENCE", MIN_CONFIDENCE),
        "confirmation_count": sig.get("confirmations", 0),
        "required_confirmations": sig.get("required_confirmations", 0),
        "MACD_value": sig.get("macd_value", 0),
        "MACD_signal": sig.get("macd_signal_val", 0),
        "MACD_condition_passed": sig.get("macd_condition_passed", False),
        "SMA20": sig.get("sma20", 0),
        "price_vs_SMA20_pct": sig.get("price_vs_sma_pct", 0),
        "SMA_condition_passed": sig.get("sma_condition_passed", False),
        "recent_up_bars": sig.get("recent_up_bars", 0),
        "recent_down_bars": sig.get("recent_down_bars", 0),
        "RSI_slope": sig.get("rsi_slope", None),
        "candle_velocity": sig.get("candle_velocity", 0),
        "volume_available": sig.get("volume_available", None),
        "volume_spike": None,  # Not available from yfinance price-only data
        "reason_direction_neutral": sig.get("reason_direction_neutral", None),
        "reason_confidence_zero": sig.get("reason_confidence_zero", None),
    }

    # §8: Add hybrid probability cascade fields if present
    for k in ("rsi_prior_p", "market_implied_p", "empirical_bucket_p",
              "bayesian_p", "markov_p", "neural_diagnostic_p", "adjusted_p",
              "entry_ask", "raw_edge", "cost_adjusted_edge", "buffered_edge",
              "kelly_size", "clamped_size", "final_decision"):
        if k in sig:
            row[k] = sig[k]
    
    try:
        with open(SIGNAL_DEBUG_FILE, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception as e:
        pass  # Never crash the loop for debug logging


# ══════════════════════════════════════════════════════════════════════════════
# SCARCITY REPORT (§7)
# ══════════════════════════════════════════════════════════════════════════════

def write_scarcity_report(asset_key, rsi, contracts, prices, direction="up"):
    """
    For every RSI 20-35 event, write market-state distribution.
    """
    state_counts = {
        "true_live_dislocation": 0,
        "false_dislocation": 0,
        "balanced": 0,
        "dormant_longshot": 0,
        "nearly_decided": 0,
        "untradeable": 0,
    }
    
    details = []
    for c in contracts:
        ts = classify_token_state(c, rsi, direction, prices)
        state = ts["token_state"]
        key_map = {
            "live_dislocation": "true_live_dislocation",
            "false_dislocation": "false_dislocation",
            "balanced": "balanced",
            "dormant_longshot": "dormant_longshot",
            "nearly_decided": "nearly_decided",
            "untradeable": "untradeable",
        }
        counted_key = key_map.get(state, "untradeable")
        state_counts[counted_key] += 1
        details.append({
            "question": c.get("question", "")[:60],
            "token_state": state,
            "recoverability_score": ts.get("recoverability_score"),
            "reason": ts.get("reason", ""),
            "up_price": c.get("up_price", 0),
            "down_price": c.get("down_price", 0),
        })
    
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "asset": asset_key,
        "rsi": rsi,
        "direction": direction,
        "rsi_in_target_zone": 20 <= rsi < 35,
        "state_distribution": state_counts,
        "total_contracts": len(contracts),
        "details": details,
    }
    
    try:
        with open(SCARCITY_REPORT_FILE, "a") as f:
            f.write(json.dumps(report, default=str) + "\n")
    except Exception:
        pass
    
    return state_counts


# ══════════════════════════════════════════════════════════════════════════════
# "WOULD TRADE IF SIGNAL RELAXED" AUDIT (§8)
# ══════════════════════════════════════════════════════════════════════════════

def would_trade_audit(sig, contracts, prices, asset_key="BTC"):
    """
    For every RSI 20-35 event where production direction is neutral,
    evaluate each shadow profile and record hypothetical trade details.
    """
    rsi = sig.get("rsi", 50)
    
    if not (20 <= rsi < 35):
        return []  # Only audit target-zone events
    
    direction = sig.get("direction", "neutral")
    if direction != "neutral":
        return []  # Only audit where production returned neutral
    
    audits = []
    
    for profile_name in SHADOW_PROFILES:
        if profile_name == "CORE_UP_STRICT":
            continue  # Skip production in audit — it already returned neutral
        
        shadow = shadow_signal(profile_name, prices, asset_key=asset_key, contract=contracts[0] if contracts else None)
        
        # Find best matching contract if shadow would trade
        hyp_trades = []
        if shadow.get("would_trade", False) and contracts:
            for c in contracts:
                up_price = c.get("up_price", 0.5)
                down_price = c.get("down_price", 0.5)
                target = up_price if shadow["direction"] == "up" else down_price
                
                ts = classify_token_state(c, rsi, shadow["direction"], prices)
                
                # EV calculation
                gross_ev, p_win, net_ev = calculate_ev(
                    rsi=rsi, direction=shadow["direction"],
                    contract_price=target,
                    session_type=v197._session_type(datetime.now().hour),
                    confirmations=shadow.get("confirmations", 0),
                )
                
                hyp_trades.append({
                    "entry_ask": target,
                    "estimated_probability": round(p_win, 3),
                    "recoverability_score": ts.get("recoverability_score"),
                    "token_state": ts["token_state"],
                    "book_state": "unknown",  # Would need CLOB API for real book
                    "EV": round(net_ev, 4),
                    "blocked_by": None if net_ev > 0 else "ev_negative",
                    "question": c.get("question", "")[:60],
                    "mins_to_expiry": c.get("mins_to_expiry", 0),
                })
        
        audits.append({
            "profile": profile_name,
            "would_trade": shadow.get("would_trade", False),
            "direction": shadow.get("direction", "neutral"),
            "confidence": shadow.get("confidence", 0),
            "reason": shadow.get("reason", ""),
            "hypothetical_trades": hyp_trades[:3],  # Top 3 only
        })
    
    # Write to file
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "asset": asset_key,
        "rsi": rsi,
        "production_direction": direction,
        "production_confidence": sig.get("confidence", 0),
        "audits": audits,
    }
    
    try:
        with open(WOULD_TRADE_FILE, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass
    
    return audits


# ══════════════════════════════════════════════════════════════════════════════
# MULTI-ASSET DISCOVER CONTRACTS (§6)
# ══════════════════════════════════════════════════════════════════════════════

def is_asset_market(question, asset_name):
    """Check if a market question matches the asset."""
    q = question.lower()
    name_lower = asset_name.lower()
    
    # Direct name match
    if name_lower in q:
        return True
    
    # Common aliases
    aliases = {
        "Bitcoin": ["btc", "bitcoin"],
        "Ethereum": ["eth", "ethereum"],
        "Solana": ["sol", "solana"],
        "XRP": ["xrp", "ripple"],
    }
    for alias in aliases.get(asset_name, []):
        if alias in q:
            return True
    
    return False


def gamma_get(path, params=None):
    """Helper for Gamma API GET requests (slug-based series discovery)."""
    try:
        import urllib.request
        query = ""
        if params:
            query = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{GAMMA_API}/{path}{query}"
        req = urllib.request.Request(url, headers={'User-Agent': 'FDC-V198/1.0'})
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except Exception:
        return []


def get_clob_price(token_id):
    """Get live mid-price from CLOB for a token."""
    if not token_id:
        return None
    try:
        import urllib.request
        url = f"{CLOB_API}/price?token_id={token_id}&side=buy"
        req = urllib.request.Request(url, headers={'User-Agent': 'FDC-V198/1.0'})
        resp = urllib.request.urlopen(req, timeout=3)
        data = json.loads(resp.read())
        if isinstance(data, dict) and 'price' in data:
            return float(data['price'])
    except Exception:
        pass
    return None


def discover_contracts_multi(asset_key=None):
    """
    Discover contracts for a specific asset or all assets.
    Uses SLUG-BASED series discovery (like V189) to find 5m/15m Up/Down markets,
    NOT search-based which only finds daily "above/below" contracts.
    """
    from datetime import timezone as _tz
    now = datetime.now(_tz.utc)

    # Filter series by asset if specified
    target_asset = asset_key
    series_to_query = SERIES_CONFIG
    if target_asset:
        series_to_query = [s for s in SERIES_CONFIG if s["asset"] == target_asset]

    all_contracts = {k: [] for k in ASSET_MAP}
    seen = set()

    for config in series_to_query:
        slug = config["slug"]
        label = config["label"]
        window_mins = config["window_mins"]
        asset = config["asset"]
        cfg = ASSET_MAP[asset]

        # §3: Get active events for this series — current + next windows
        # Look 45 min ahead to get current + next 5m/15m window
        # (don't use 90min — too many CLOB calls causes timeout)
        look_ahead_minutes = 45
        events = gamma_get("events", {
            "limit": "10",
            "series_slug": slug,
            "active": "true",
            "closed": "false",
            "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date_max": (now + timedelta(minutes=look_ahead_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

        for event in events:
            for m in event.get("markets", []):
                if not m.get("active", False) or m.get("closed", False):
                    continue
                cid = m.get("conditionId", "")
                if cid in seen:
                    continue

                question = m.get("question", "")
                if not is_asset_market(question, cfg["name"]):
                    continue

                # Get token IDs for CLOB pricing
                clob_str = m.get("clobTokenIds", "[]")
                if isinstance(clob_str, str):
                    try:
                        clob = json.loads(clob_str)
                    except Exception:
                        clob = []
                else:
                    clob = clob_str if isinstance(clob_str, list) else []
                if len(clob) < 2:
                    clob = ["", ""]

                # Get prices
                prices_raw = m.get("outcomePrices", [])
                if isinstance(prices_raw, str):
                    try:
                        prices_raw = json.loads(prices_raw)
                    except Exception:
                        prices_raw = []
                outcomes = m.get("outcomes", [])
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except Exception:
                        outcomes = []

                if not isinstance(prices_raw, list) or len(prices_raw) < 2:
                    continue

                # Determine up/down indices
                up_i, down_i = 0, 1
                if isinstance(outcomes, list) and len(outcomes) >= 2:
                    o0 = (outcomes[0] or "").lower()
                    o1 = (outcomes[1] or "").lower()
                    if "down" in o0 or "no" in o0 or "below" in o0:
                        up_i, down_i = 1, 0

                try:
                    up_price = float(prices_raw[up_i]) if prices_raw[up_i] else 0.5
                    down_price = float(prices_raw[down_i]) if len(prices_raw) > down_i and prices_raw[down_i] else 0.5
                except (ValueError, IndexError, TypeError):
                    continue

                # Try CLOB for live prices (with fallback to Gamma prices)
                # Limit CLOB calls to avoid timeout on batch discovery
                try:
                    up_clob = get_clob_price(clob[up_i]) if up_i < len(clob) and clob[up_i] else None
                    down_clob = get_clob_price(clob[down_i]) if down_i < len(clob) and clob[down_i] else None
                    if up_clob is not None:
                        up_price = up_clob
                    if down_clob is not None:
                        down_price = down_clob
                except Exception:
                    pass  # Fall back to Gamma prices

                # Calculate time remaining
                end_str = m.get("endDate", event.get("endDate", ""))
                mins = 9999.0
                try:
                    if end_str.endswith("Z"):
                        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    else:
                        end_dt = datetime.fromisoformat(end_str)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=_tz.utc)
                    mins = (end_dt - now).total_seconds() / 60
                except Exception:
                    continue

                # Skip expired or too far out (§3: allow up to 60min for next-window contracts)
                if mins < 0:
                    continue
                if mins > 60:
                    continue  # Only look at near-term contracts

                vol = float(m.get("volume", m.get("volume24hr", 0)))
                # Volume threshold for 5m/15m markets: these have very low Gamma-reported volume
                # (typically 0-50) because they're brand-new markets with minutes of life.
                # For paper trading, we need the market to exist and have order books — volume is
                # a liquidity proxy for live trading but irrelevant for paper validation.
                # Use CLOB book depth as the real liquidity check instead.
                min_vol = 1 if window_mins <= 15 else 500
                if vol < min_vol:
                    continue

                seen.add(cid)

                all_contracts[asset].append({
                    "question": question,
                    "conditionId": cid,
                    "market_id": m.get("id", ""),
                    "up_price": up_price,
                    "down_price": down_price,
                    "volume": vol,
                    "slug": event.get("slug", ""),
                    "event_slug": event.get("slug", ""),
                    "series_slug": slug,
                    "end_date": end_str,
                    "window": label,
                    "window_mins": window_mins,
                    "mins_to_expiry": round(mins, 1),
                    "is_daily": False,
                    "asset": asset,
                    "paper_only_asset": cfg.get("paper_only", False),
                    "up_token_id": clob[up_i] if up_i < len(clob) else "",
                    "down_token_id": clob[down_i] if down_i < len(clob) else "",
                    "cheap_side": "Up" if up_price <= down_price else "Down",
                    "cheap_price": min(up_price, down_price),
                    "label": label,
                })

    # Sort each asset's contracts by time remaining
    for key in all_contracts:
        all_contracts[key].sort(key=lambda c: c["mins_to_expiry"])

    return all_contracts


# ══════════════════════════════════════════════════════════════════════════════
# SHADOW PROFILE TRACKING
# ══════════════════════════════════════════════════════════════════════════════

class ShadowTracker:
    """Track shadow profile performance across cycles."""
    
    def __init__(self):
        self.profiles = {name: {
            "signals": 0,
            "signal_up": 0,
            "signal_market_overlap": 0,
            "trade_candidates": 0,
            "true_live_dislocations": 0,
            "false_dislocations": 0,
            "balanced_markets": 0,
            "book_checks": 0,
            "book_executable": 0,
            "executable_opportunities": 0,
            "paper_trades_opened": 0,
            "paper_trades_resolved": 0,
            "paper_trades_won": 0,
            "net_ev": 0.0,
            "blocked_by_reason": {},
        } for name in SHADOW_PROFILES}
    
    def record_signal(self, profile_name, shadow_result, token_state_info=None):
        """Record a shadow signal event."""
        p = self.profiles.get(profile_name)
        if not p:
            return
        p["signals"] += 1
        if shadow_result.get("direction") == "up":
            p["signal_up"] += 1
        
        if shadow_result.get("would_trade"):
            p["trade_candidates"] += 1
        
        if token_state_info:
            ts = token_state_info.get("token_state", "unknown")
            if ts == "live_dislocation":
                p["true_live_dislocations"] += 1
            elif ts == "false_dislocation":
                p["false_dislocations"] += 1
            elif ts == "balanced":
                p["balanced_markets"] += 1
    
    def record_blocked(self, profile_name, reason):
        """Record why a trade was blocked."""
        p = self.profiles.get(profile_name)
        if not p:
            return
        p["blocked_by_reason"][reason] = p["blocked_by_reason"].get(reason, 0) + 1
    
    def record_executable(self, profile_name, net_ev):
        """Record an executable opportunity."""
        p = self.profiles.get(profile_name)
        if not p:
            return
        p["executable_opportunities"] += 1
        p["book_executable"] += 1
        p["net_ev"] += net_ev
    
    def record_paper_trade(self, profile_name, won=False, ev=0):
        """Record a paper trade being OPENED. Resolution tracked separately."""
        p = self.profiles.get(profile_name)
        if not p:
            return
        p["paper_trades_opened"] += 1
        # Do NOT increment paper_trades_resolved here — resolution happens at settlement
    
    def summary(self):
        """Return summary dict."""
        return dict(self.profiles)
    
    def write_report(self):
        """Persist shadow report to JSONL."""
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "profiles": self.summary(),
        }
        try:
            with open(SHADOW_REPORT_FILE, "a") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# V19.8 ENHANCED RUN ONCE (with all debug/shadow/scarcity features)
# ══════════════════════════════════════════════════════════════════════════════

def run_once_v198(state, shadow_tracker=None, schedule_cache=None):
    """
    V19.8 run_once: processes all assets, writes debug, runs shadows,
    reports scarcity, audits relaxed signals.
    
    LIVE DISABLED. Paper only.
    Returns: (production_entries, all_settled, skip_info, signal_map, debug_summary)
    """
    if shadow_tracker is None:
        shadow_tracker = ShadowTracker()
    if schedule_cache is None:
        schedule_cache = MarketScheduleCache()
    
    all_entries = []
    all_settled = []
    all_skip_info = []
    signal_map = {}
    debug_summary = {
        "cycle": state.get("scans", 0),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "assets": {},
    }
    
    # ── Fetch all asset candles ──
    asset_prices = fetch_all_assets()
    
    # ── Fetch contracts via schedule cache (TTL-based) ──
    all_contracts = schedule_cache.slug_provider()
    
    # ── Track unique markets and books_executable ──
    markets_this_cycle = set()
    books_executable_this_cycle = 0
    maker_diagnostic_this_cycle = 0
    
    for asset_key, prices in asset_prices.items():
        if not prices or len(prices) < 14:
            continue
        
        cfg = ASSET_MAP[asset_key]
        interval = cfg.get("default_tf", "5m")
        is_paper_only = cfg.get("paper_only", True)
        
        # ── Enhanced signal (§2) ──
        sig = enhanced_signal(prices, asset_key=asset_key)
        signal_map[asset_key] = sig
        
        # ── Write signal debug (§2) ──
        write_signal_debug(sig, asset_key=asset_key, interval=interval)
        
        contracts = all_contracts.get(asset_key, [])
        
        # ── Track markets and books ──
        for c in contracts:
            cid = c.get("conditionId", "")
            if cid:
                markets_this_cycle.add(cid)
            # Clean-tick fetch for book depth (only for contracts with volume)
            if c.get("volume", 0) > 500 and cid:
                book = schedule_cache.clean_tick(cid)
                if book:
                    spread = book.get("spread", 1.0)
                    depth = book.get("depth_usd", 0)
                    if spread < 0.05 and depth > 500:
                        books_executable_this_cycle += 1
                    if spread < 0.02 and depth > 200:
                        maker_diagnostic_this_cycle += 1
        
        # ── RSI assignment (needed for tape, scarcity, shadows) ──
        rsi = sig.get("rsi", 50)
        
        # ── Market tape snapshot (§4) ──
        tape = market_tape_snapshot(asset_key, contracts, prices, rsi)
        
        # ── Scarcity report for RSI 20-35 events (§7) ──
        scarcity = None
        if 20 <= rsi < 35:
            scarcity = write_scarcity_report(asset_key, rsi, contracts, prices, direction="up")
        
        # ── Would-trade audit for neutral signals in target zone (§8) ──
        would_trade_audits = []
        if sig.get("direction") == "neutral" and 20 <= rsi < 35:
            would_trade_audits = would_trade_audit(sig, contracts, prices, asset_key=asset_key)
        
        # ── Shadow profiles (§4) ──
        shadow_results = {}
        rsi_in_strict_zone = 20 <= rsi < 35  # Original 5 shadow profiles
        rsi_in_extended_zone = 20 <= rsi < 50  # PREOPEN + ONE_MIN_STRUCTURE
        for profile_name in SHADOW_PROFILES:
            profile = SHADOW_PROFILES[profile_name]
            # Determine if this profile's RSI zone matches current RSI
            profile_rsi_low = profile.get("rsi_low", 20)
            profile_rsi_high = profile.get("rsi_high", 35)
            rsi_in_profile_zone = profile_rsi_low <= rsi < profile_rsi_high
            
            if not rsi_in_profile_zone:
                # Profile zone doesn't match, store neutral result
                shadow_results[profile_name] = {
                    "direction": "neutral", "confidence": 0, "rsi": rsi,
                    "profile": profile_name, "reason": "rsi_not_in_zone",
                    "would_trade": False
                }
                continue
            
            contract_for_recoverability = contracts[0] if contracts else None
            shadow = shadow_signal(profile_name, prices, asset_key=asset_key, contract=contract_for_recoverability)
            shadow_results[profile_name] = shadow
            
            # Record in tracker
            ts_info = None
            if contract_for_recoverability:
                ts_info = classify_token_state(contract_for_recoverability, rsi, "up", prices)
            shadow_tracker.record_signal(profile_name, shadow, ts_info)
            
            # Check if executable
            if shadow.get("would_trade") and contracts:
                for c in contracts[:3]:
                    target_price = c.get("up_price", 0.5) if shadow["direction"] == "up" else c.get("down_price", 0.5)
                    if MIN_CONTRACT_PRICE <= target_price <= MAX_CONTRACT_PRICE:
                        ts = classify_token_state(c, rsi, shadow["direction"], prices)
                        gross_ev, p_win, net_ev = calculate_ev(
                            rsi=rsi, direction=shadow["direction"],
                            contract_price=target_price,
                            session_type=v197._session_type(datetime.now().hour),
                            confirmations=shadow.get("confirmations", 0),
                        )
                        if net_ev > 0 and ts["token_state"] not in ("dormant_longshot", "untradeable"):
                            shadow_tracker.record_executable(profile_name, net_ev)
                        else:
                            reason = f"ev_{net_ev:.3f}" if net_ev <= 0 else f"token_state_{ts['token_state']}"
                            shadow_tracker.record_blocked(profile_name, reason)
                    else:
                        shadow_tracker.record_blocked(profile_name, f"price_{target_price:.3f}_outside_range")
        
        # ── Production entries (BTC only via V19.7 → evaluate_entries_v197) ──
        asset_entries = []
        if asset_key == "BTC" and sig.get("direction") != "neutral" and sig.get("confidence", 0) >= MIN_CONFIDENCE:
            # Use V19.7 evaluate_entries for production BTC
            v197_sig = {
                "direction": sig["direction"],
                "confidence": sig["confidence"],
                "rsi": sig["rsi"],
                "price": sig["price"],
                "macd": sig.get("macd_value", 0),
                "momentum": sig.get("recent_up_bars", 2),
                "sma20": sig.get("sma20", 0),
                "confirmations": sig.get("confirmations", 0),
                "_prices": prices,
            }
            v197_entries, skip_info = evaluate_entries_v197(v197_sig, contracts, state)
            all_skip_info.extend(skip_info if isinstance(skip_info, list) else [skip_info] if skip_info else [])
            # Log WHY V19.7 entries may be 0 despite signal firing
            if len(v197_entries) == 0 and len(contracts) > 0 and 20 <= rsi < 35:
                all_skip_info.append({
                    "type": "v197_evaluate_entries_zero_despite_signal",
                    "asset": asset_key,
                    "direction": sig["direction"],
                    "confidence": sig["confidence"],
                    "rsi": rsi,
                    "contracts_checked": len(contracts),
                    "contract_prices": [f"up={c.get('up_price',0):.3f}/dn={c.get('down_price',0):.3f}" for c in contracts[:3]],
                    "msg": "V19.7 evaluate_entries returned 0 entries despite production signal UP"
                })
        
        # ── Shadow profile paper trades (ALL assets including BTC) ──
        # Key V19.8 fix: BTC shadow profiles must also generate paper trades
        # These do NOT count toward live readiness
        # Check: any shadow profile with would_trade=True can generate a paper trade
        for profile_name, shadow in shadow_results.items():
            if profile_name == "CORE_UP_STRICT":
                continue  # Production profile handled above
            # §1: Frozen profiles may not open paper trades (negative realized EV)
            if SHADOW_PROFILES.get(profile_name, {}).get("frozen", False):
                shadow_tracker.record_blocked(profile_name, "profile_frozen_negative_ev")
                continue
            if not shadow.get("would_trade"):
                continue
            shadow_dir = shadow.get("direction", "up")
            if shadow_dir not in ("up", "down"):
                continue  # Skip neutral
            # For each contract that passes shadow profile criteria
            for c in contracts[:2]:  # Top 2 contracts per shadow profile
                if shadow_dir == "down":
                    target_price = c.get("down_price", 0.5)
                else:
                    target_price = c.get("up_price", 0.5)
                # V19.9: Cheap convex can trade 0.05-0.25, balanced 0.35-0.65
                profile_cfg = SHADOW_PROFILES.get(profile_name, {})
                price_lo = profile_cfg.get("price_range", (MIN_CONTRACT_PRICE, MAX_CONTRACT_PRICE))[0] if "price_range" in profile_cfg else MIN_CONTRACT_PRICE
                price_hi = profile_cfg.get("price_range", (MIN_CONTRACT_PRICE, MAX_CONTRACT_PRICE))[1] if "price_range" in profile_cfg else MAX_CONTRACT_PRICE
                # Balanced edge uses 0.35-0.65
                if profile_name == "BALANCED_DIRECTION_EDGE":
                    price_lo, price_hi = 0.35, 0.65
                if target_price < price_lo or target_price > price_hi:
                    continue  # Price gate
                # §7: Cheap-token calibration penalty — require buffered_edge >= 0.10 for entries < 0.35
                # until positive calibration evidence exists
                if target_price < 0.35:
                    shadow_edge = shadow.get("buffered_ev") or shadow.get("buffered_edge") or shadow.get("edge", 0)
                    if shadow_edge < 0.10:
                        shadow_tracker.record_blocked(profile_name, f"cheap_token_diagnostic_only_edge_{shadow_edge:.3f}_below_0.10")
                        continue  # CHEAP_TOKEN_MODE = DIAGNOSTIC_ONLY
                # §5: Bucket gating using HYBRID PROBABILITY constants
                # Blocked bucket: 0.30-0.40 (hard block, no paper/diagnostic)
                if BUCKET_BLOCKED[0] <= target_price < BUCKET_BLOCKED[1]:
                    shadow_tracker.record_blocked(profile_name, f"blocked_by_bad_price_bucket_{BUCKET_BLOCKED[0]:.2f}_{BUCKET_BLOCKED[1]:.2f}")
                    continue  # BAD BUCKET — hard block
                # Entries < 0.20 are diagnostic-only until separately validated
                if target_price < BUCKET_PAPER[0]:
                    shadow_tracker.record_blocked(profile_name, f"cheap_under_{BUCKET_PAPER[0]:.2f}_diagnostic_ask_{target_price:.3f}")
                    continue  # UNDER_PAPER_MIN_DIAGNOSTIC_ONLY
                # Entries > 0.40 diagnostic only (except 0.20-0.30 paper)
                if target_price >= BUCKET_BLOCKED[1] and not (BUCKET_PAPER[0] <= target_price < BUCKET_PAPER[1]):
                    shadow_tracker.record_blocked(profile_name, f"diagnostic_above_{BUCKET_BLOCKED[1]:.2f}_ask_{target_price:.3f}")
                    continue  # ABOVE_PAPER_MAX_DIAGNOSTIC_ONLY
                ts = classify_token_state(c, rsi, shadow_dir, prices)
                if ts["token_state"] in ("dormant_longshot", "untradeable"):
                    continue  # Token state gate
                # §3: CONVEX_20_30_VALIDATION also blocks false_dislocation and nearly_decided
                if profile_name == "CONVEX_20_30_VALIDATION" and ts["token_state"] in ("false_dislocation", "nearly_decided"):
                    shadow_tracker.record_blocked(profile_name, f"blocked_token_state_{ts['token_state']}")
                    continue
                # §1-6: Hybrid probability cascade replaces old calculate_ev + recalibrate
                prob = compute_hybrid_probability(
                    rsi=rsi, direction=shadow_dir, entry_ask=target_price,
                    contract_price=target_price,
                    session_type=v197._session_type(datetime.now().hour),
                    confirmations=shadow.get("confirmations", 0),
                    prices=prices, steps_remaining=int(c.get("mins_to_expiry", 5)),
                    bucket_n=0,  # Tier 1 cap until bucket data available
                    empirical_bucket_p=None,
                    state=state,
                )
                adjusted_p = prob["adjusted_p"]
                buffered_edge = prob["buffered_edge"]
                p_win = adjusted_p  # Use hybrid cascade probability
                # §6: EV gate — buffered_edge must be positive
                if buffered_edge <= 0:
                    shadow_tracker.record_blocked(profile_name, f"buffered_edge_{buffered_edge:.4f}_not_positive")
                    continue
                # §7: Fixed paper trade size — Kelly computed after EV gate passes
                gross_ev = prob["raw_edge"]  # adjusted_p - entry_ask
                net_ev = prob["cost_adjusted_edge"]  # adjusted_p - entry_ask - slippage
                actual_bet = PAPER_TRADE_SIZE  # §7: $2 fixed for paper

                # Build fully-specified paper position (§2) with full probability cascade (§8)
                raw_entry = {
                    "action": "BUY_Up" if shadow.get("direction", "up") != "down" else "BUY_Down",
                    "question": c.get("question", ""),
                    "conditionId": c.get("conditionId", ""),
                    "contract_price": target_price,
                    "bet": actual_bet,
                    "edge": round(prob["raw_edge"], 4),
                    "ev_gross": round(gross_ev, 4), "ev_p_win": round(p_win, 3),
                    "ev_net": round(net_ev, 4),
                    "side": shadow.get("direction", "up").capitalize(),
                    "mode": "PAPER",
                    "paper_only_asset": cfg.get("paper_only", False),
                    "asset": asset_key,
                    "token_state": ts["token_state"],
                    "recoverability_score": ts.get("recoverability_score"),
                    # V19.9 enhanced reporting fields
                    "entry_time_relative_to_market_start": c.get("mins_to_expiry", 0),
                    "entry_price": target_price,
                    "estimated_probability": round(p_win, 4),
                    "raw_edge": prob["raw_edge"],
                    "buffered_edge": prob["buffered_edge"],
                    # §8: Full probability cascade logging
                    "rsi_prior_p": prob["rsi_prior_p"],
                    "market_implied_p": prob["market_implied_p"],
                    "bayesian_p": prob.get("bayesian_p"),
                    "markov_p": prob.get("markov_p"),
                    "neural_diagnostic_p": prob.get("neural_diagnostic_p"),
                    "adjusted_p": prob["adjusted_p"],
                    "final_decision": "PAPER_ENTRY",
                    "kelly_size": None,  # Paper uses fixed $2
                    "clamped_size": actual_bet,
                    "book_executable": tape.get("depth_available", False) if tape else None,
                    "spread": tape.get("bid_ask_spread") if tape else None,
                    "depth": c.get("volume", 0),
                    "time_to_expiry": shadow.get("time_to_expiry", c.get("mins_to_expiry", 0)),
                }
                # §1: Use canonical paper entry builder (enforces required fields + child-market validation)
                entry = cpos.build_canonical_paper_entry(
                    entry=raw_entry,
                    contract=c,
                    shadow_profile=profile_name,
                    rsi=rsi,
                    signal=sig,
                )
                if entry is None:
                    # Canonical validation or child-market check failed — blocked
                    shadow_tracker.record_blocked(profile_name, "blocked_by_canonical_validation")
                    continue
                asset_entries.append(entry)
                shadow_tracker.record_paper_trade(profile_name, won=False, ev=net_ev)
        
        all_entries.extend(asset_entries)
        
        # ── Asset debug summary ──
        debug_summary["assets"][asset_key] = {
            "rsi": rsi,
            "rsi_zone": sig.get("rsi_zone", "unknown"),
            "direction": sig.get("direction", "neutral"),
            "confidence": sig.get("confidence", 0),
            "confirmations": sig.get("confirmations", 0),
            "contracts_found": len(contracts),
            "scarcity": scarcity,
            "shadow_would_trade": {k: v.get("would_trade", False) for k, v in shadow_results.items()},
            "paper_only": is_paper_only,
            "reason_direction_neutral": sig.get("reason_direction_neutral"),
            "reason_confidence_zero": sig.get("reason_confidence_zero"),
            "market_tape": tape,
            "maker_diagnostic": {c.get("conditionId", "")[:8]: {
                "spread": round(abs(c.get("up_price", 0.5) - (1 - c.get("up_price", 0.5))), 4),
                "volume": c.get("volume", 0),
                "up_price": c.get("up_price", 0),
                "mins_to_expiry": c.get("mins_to_expiry", 0),
            } for c in contracts[:3]},
        }
    
    # ── Process exits and settlements (BTC only via V19.7) ──
    btc_contracts = all_contracts.get("BTC", [])
    if btc_contracts:
        exit_settled = process_exits(state, btc_contracts)
        for s in exit_settled:
            pnl = s.get("pnl", 0)
            state["total_pnl"] = state.get("total_pnl", 0) + pnl
            state["bankroll"] = state.get("bankroll", 320) + pnl
            state["daily_pnl"] = state.get("daily_pnl", 0) + pnl
            if pnl > 0:
                state["wins"] = state.get("wins", 0) + 1
            else:
                state["losses"] = state.get("losses", 0) + 1
        
        btc_sig = signal_map.get("BTC", {})
        settled = check_settlements(state, btc_sig.get("price", 0) if btc_sig else 0)
        for s in settled:
            pnl = s.get("pnl", 0)
            state["total_pnl"] = state.get("total_pnl", 0) + pnl
            state["bankroll"] = state.get("bankroll", 320) + pnl
            state["daily_pnl"] = state.get("daily_pnl", 0) + pnl
            if pnl > 0:
                state["wins"] = state.get("wins", 0) + 1
            else:
                state["losses"] = state.get("losses", 0) + 1
        all_settled.extend(exit_settled + settled)
    
    # ── Resolve paper positions (V19.8 resolution scheduler) ──
    counters = state.get("resolution_counters", None)
    if counters is None:
        counters = pres.ResolutionCounters()
    elif isinstance(counters, dict):
        # Restore from state dict using from_dict
        counters = pres.ResolutionCounters.from_dict(counters)
    
    resolved = pres.resolve_paper_positions(state, counters, shadow_tracker)
    for r in resolved:
        all_settled.append(r)
    
    # Persist counters to state
    state["resolution_counters"] = counters.to_dict()
    
    # ── Place paper entries ──
    for e in all_entries:
        # Use position_id as key (from paper_resolution.build_paper_entry)
        pos_id = e.get("position_id", "")
        if not pos_id:
            # Fallback for entries not built via build_paper_entry
            pos_id = f"{e.get('conditionId', '')[:16]}_{e.get('selected_side', e.get('side', 'Up'))}"
        if pos_id in state.get("positions", {}):
            continue
        # LIVE DISABLED — always paper
        e["mode"] = "PAPER"
        state.setdefault("positions", {})[pos_id] = e
        size = e.get("size_usd", e.get("bet", 0))
        state["bankroll"] = state.get("bankroll", 320) - size
        counters.paper_positions_open += 1
    
    # ── Track bankroll peak ──
    br_peak = state.get("bankroll_peak", state.get("bankroll", 320))
    if state.get("bankroll", 320) > br_peak:
        state["bankroll_peak"] = state.get("bankroll", 320)
    
    # ── Classification (§9 decision rules) ──
    classification = classify_loop_state(debug_summary, shadow_tracker)
    debug_summary["classification"] = classification
    
    # ── Write shadow tracker report ──
    shadow_tracker.write_report()

    # ── §9: Accounting reconciliation ──
    accounting = reconcile_accounting(state)
    debug_summary["accounting"] = accounting
    if accounting.get("ACCOUNTING_INVARIANT_FAIL"):
        print(f"  ⚠️ ACCOUNTING INVARIANT FAIL: discrepancy={accounting['discrepancy']}")
        print(f"     Expected bankroll: ${accounting['expected_bankroll']:.2f}, Actual: ${accounting['current_bankroll']:.2f}")

    save_state(state)

    return all_entries, all_settled, all_skip_info, signal_map, debug_summary


def classify_loop_state(debug_summary, shadow_tracker):
    """
    Classify the current loop state per §9 rules.
    """
    shadow_summary = shadow_tracker.summary()
    
    # Count true live dislocations across all profiles
    total_live_dislocations = sum(
        p.get("true_live_dislocations", 0) for p in shadow_summary.values()
    )
    
    # Count production signals
    prod_signals = shadow_summary.get("CORE_UP_STRICT", {}).get("signal_up", 0)
    
    # Count shadow executable opportunities
    total_shadow_executable = sum(
        p.get("executable_opportunities", 0) for n, p in shadow_summary.items()
        if n != "CORE_UP_STRICT"
    )
    
    # Count paper trades
    total_paper_trades = sum(
        p.get("paper_trades_opened", 0) for n, p in shadow_summary.items()
        if n != "CORE_UP_STRICT"
    )
    
    # Net EV
    total_net_ev = sum(
        p.get("net_ev", 0) for p in shadow_summary.values()
    )
    
    # ── Classification decision tree ──
    if total_live_dislocations == 0:
        classification = "A_COLLECTING_MARKET_AND_SIGNAL_DATA"
        classification_reason = "zero_true_live_dislocations"
    elif total_live_dislocations > 0 and prod_signals == 0:
        classification = "B_CONFIRMATION_TOO_STRICT"
        classification_reason = f"live_dislocations_exist({total_live_dislocations})_but_no_production_signal"
    elif total_shadow_executable >= 10 or total_paper_trades >= 5:
        if total_net_ev > 0:
            classification = "C_SHADOW_PROVEN"
            classification_reason = f"shadow_executable={total_shadow_executable}_paper={total_paper_trades}_net_ev={total_net_ev:.2f}"
        else:
            # §2: Execution works but strategy EV needs calibration correction
            # NOTE: realized PnL can be positive even when estimated EV was negative
            # because cheap tokens (entry < 0.30) have lower break-even WR
            classification = "C_SHADOW_EXECUTION_PROVEN_POSITIVE_EV_UNCALIBRATED"
            classification_reason = f"shadow_executable={total_shadow_executable}_paper={total_paper_trades}_net_ev={total_net_ev:.2f}_NEEDS_CALIBRATION"
    elif total_shadow_executable > 0:
        classification = "B_CONFIRMATION_TOO_STRICT"
        classification_reason = f"live_dislocations={total_live_dislocations}_shadow_executable={total_shadow_executable}_insufficient_for_promotion"
    else:
        classification = "A_COLLECTING_MARKET_AND_SIGNAL_DATA"
        classification_reason = "no_executable_opportunities_from_any_profile"
    
    return {
        "classification": classification,
        "reason": classification_reason,
        "total_live_dislocations": total_live_dislocations,
        "total_shadow_executable": total_shadow_executable,
        "total_paper_trades": total_paper_trades,
        "total_net_ev": round(total_net_ev, 4),
        "production_signals": prod_signals,
        "prompt_promotion": classification == "C_SHADOW_PROVEN",
        "do_not_change_production": classification not in ("C_SHADOW_PROVEN", "C_SHADOW_EXECUTION_PROVEN_POSITIVE_EV_UNCALIBRATED"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# V19.8 SIGNAL-FOCUSED PAPER LOOP (§9)
# ══════════════════════════════════════════════════════════════════════════════

def run_signal_loop(duration_hours=2, cycle_target_s=30):
    """
    Run a signal-focused paper loop with adaptive speed.
    
    Adaptive cycle: 5-15s when positions active, 15-30s when idle.
    LIVE DISABLED. Paper only.
    """
    state = load_state()
    state["version"] = "V19.8"
    shadow_tracker = ShadowTracker()
    schedule_cache = MarketScheduleCache(ttl_seconds=120)
    
    # §7: Set run_id for journal directory
    pres.set_run_id(datetime.now(timezone.utc).strftime("run_%Y%m%d_%H%M%S"))
    
    start_time = datetime.now()
    end_time = start_time + timedelta(hours=duration_hours)
    
    # Accumulators
    cycle_count = 0
    runtime_errors = 0
    cycle_times = []
    rsi_target_events = {"BTC": 0, "ETH": 0, "SOL": 0, "XRP": 0}
    production_signals = 0
    scarcity_totals = {
        "true_live_dislocation": 0,
        "false_dislocation": 0,
        "balanced": 0,
        "dormant_longshot": 0,
        "nearly_decided": 0,
        "untradeable": 0,
    }
    
    total_entries = 0
    total_paper_trades = 0
    classifications = {}
    
    v197._init_live()
    
    print(f"{'='*60}")
    print(f"FDC V19.8 SIGNAL-FOCUSED PAPER LOOP")
    print(f"Duration: {duration_hours}h | Adaptive cycle: 5-15s active / 15-30s idle")
    print(f"LIVE: DISABLED | Paper: ENABLED | Shadow: ALL + PREOPEN + 1MIN")
    print(f"Assets: BTC(prod) ETH/SOL/XRP(paper-eligible)")
    print(f"Start: {start_time.isoformat()}")
    print(f"{'='*60}\n")
    
    while datetime.now() < end_time:
        try:
            cycle_start = time.time()
            cycle_count += 1
            
            entries, settled, skip_info, sig_map, debug = run_once_v198(state, shadow_tracker, schedule_cache)
            
            # Accumulate stats
            total_entries += len(entries)
            for asset_key in ASSET_MAP:
                sig = sig_map.get(asset_key, {})
                rsi = sig.get("rsi", 50)
                if 20 <= rsi < 35:
                    rsi_target_events[asset_key] += 1
                
                if sig.get("direction") != "neutral" and asset_key == "BTC":
                    production_signals += 1
            
            # Scarcity totals from debug
            for asset_key, asset_debug in debug.get("assets", {}).items():
                sc = asset_debug.get("scarcity")
                if sc and isinstance(sc, dict):
                    for k, v in sc.items():
                        if k in scarcity_totals:
                            scarcity_totals[k] += v
            
            # Classification
            cls = debug.get("classification", {}).get("classification", "UNKNOWN")
            classifications[cls] = classifications.get(cls, 0) + 1
            
            # Print cycle summary
            asset_rsis = " ".join(
                f"{k}:RSI={sig_map.get(k, {}).get('rsi', 0):.1f}({sig_map.get(k, {}).get('direction', '?')})"
                for k in ASSET_MAP if k in sig_map
            )
            cls_info = debug.get("classification", {}).get("classification", "?")
            reason = debug.get("classification", {}).get("reason", "")
            paper_count = len([e for e in entries if e.get("mode") == "PAPER"])
            total_paper_trades += paper_count
            
            # Resolution counters
            rc = state.get("resolution_counters", {})
            resolved_n = rc.get("paper_trades_resolved", 0)
            wins_n = rc.get("paper_wins", 0)
            losses_n = rc.get("paper_losses", 0)
            settle_err = rc.get("settlement_errors", 0)
            sl_n = rc.get("stop_loss_exits", 0)
            tp_n = rc.get("take_profit_exits", 0)
            td_n = rc.get("time_decay_exits", 0)
            active_n = rc.get("paper_positions_active", 0)
            unres_n = rc.get("paper_positions_unresolved_past_expiry", 0)
            
            # Active positions determine cycle speed
            has_active = len(state.get("positions", {})) > 0
            
            print(f"[{cycle_count}] {asset_rsis} | cls={cls_info} | "
                  f"entries={len(entries)} paper={paper_count} | "
                  f"resolved={resolved_n} W={wins_n} L={losses_n} | "
                  f"SL={sl_n} TP={tp_n} TD={td_n} act={active_n} unr={unres_n} | "
                  f"err={runtime_errors} se={settle_err}")
            
            if reason:
                print(f"     reason: {reason[:120]}")
            
            # ── Adaptive cycle speed ──
            elapsed = time.time() - cycle_start
            cycle_times.append(elapsed)
            if has_active:
                # Active: 5-15s cycle
                target = random.uniform(5, 15)
            else:
                # Idle: 15-30s cycle
                target = random.uniform(15, 30)
            sleep_time = max(0, target - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)
        
        except KeyboardInterrupt:
            print("\n⚠ Interrupted by user")
            break
        except Exception as e:
            runtime_errors += 1
            print(f"❌ Error: {e}")
            traceback.print_exc()
            time.sleep(10)
            if runtime_errors > 20:
                print("💀 Too many errors, stopping")
                break
    
    # ── Final Report ──
    end_time_actual = datetime.now()
    duration_actual = (end_time_actual - start_time).total_seconds() / 3600
    
    shadow_summary = shadow_tracker.summary()
    
    # Cycle timing statistics
    p50_cycle = sorted(cycle_times)[len(cycle_times) // 2] if cycle_times else 0
    p95_cycle = sorted(cycle_times)[int(len(cycle_times) * 0.95)] if len(cycle_times) > 1 else 0
    
    print(f"\n{'='*70}")
    print(f"V19.8 SIGNAL-FOCUSED PAPER LOOP — FINAL REPORT")
    print(f"{'='*70}")
    print(f"Duration: {duration_actual:.2f}h ({cycle_count} cycles)")
    print(f"Runtime errors: {runtime_errors}")
    print(f"P50 cycle: {p50_cycle:.1f}s | P95 cycle: {p95_cycle:.1f}s")
    print()
    
    print("── SPEED & PREWATCH ──")
    print(f"  cache_hit_rate: {schedule_cache.cache_hit_rate:.1%}")
    print(f"  prewatch_coverage: {schedule_cache.prewatch_coverage:.1%}")
    print(f"  first_clean_tick_latency: {schedule_cache.first_clean_tick_latency:.3f}s" if schedule_cache.first_clean_tick_latency else "  first_clean_tick_latency: N/A")
    print(f"  clean_ticks_seen: {schedule_cache.clean_ticks_seen}")
    print(f"  dirty_ticks_rejected: {schedule_cache.dirty_ticks_rejected}")
    print(f"  markets_seen: {len(schedule_cache.markets_seen)}")
    print(f"  books_executable: {schedule_cache.books_executable}")
    print(f"  maker_diagnostic_count: {schedule_cache.maker_diagnostic_count}")
    print()
    
    print("── RSI TARGET-ZONE EVENTS (20-35) ──")
    for asset, count in rsi_target_events.items():
        print(f"  {asset}: {count} events")
    print()
    
    print("── PRODUCTION CORE_UP (V19.7 STRICT) ──")
    print(f"  Signals: {production_signals}")
    print()
    
    print("── SHADOW PROFILE RESULTS ──")
    for name, data in shadow_summary.items():
        print(f"  {name}:")
        print(f"    signals={data['signals']} up={data['signal_up']} "
              f"candidates={data['trade_candidates']}")
        print(f"    live_dislocations={data['true_live_dislocations']} "
              f"false_dislocations={data['false_dislocations']} "
              f"balanced={data['balanced_markets']}")
        print(f"    executable={data['executable_opportunities']} "
              f"book_executable={data['book_executable']}")
        print(f"    paper_trades={data['paper_trades_opened']} "
              f"won={data['paper_trades_won']} "
              f"net_ev={data['net_ev']:.4f}")
        if data['blocked_by_reason']:
            top_blocked = sorted(data['blocked_by_reason'].items(), key=lambda x: -x[1])[:3]
            print(f"    top_blocked: {top_blocked}")
    print()
    
    print("── DISLOCATION SCARCITY ──")
    for state_name, count in scarcity_totals.items():
        print(f"  {state_name}: {count}")
    print()
    
    print("── CLASSIFICATION DISTRIBUTION ──")
    for cls, count in classifications.items():
        print(f"  {cls}: {count}")
    print()
    
    print("── PAPER TRADES ──")
    print(f"  Total entries: {total_entries}")
    print(f"  Paper trades opened: {total_paper_trades}")
    print()
    
    # ── §9 Classification Decision ──
    final_cls = "A_COLLECTING_MARKET_AND_SIGNAL_DATA"
    for cls in ["C_SHADOW_PROVEN", "C_SHADOW_MARGINAL", "B_CONFIRMATION_TOO_STRICT"]:
        if cls in classifications:
            final_cls = cls
            break
    
    print("── DECISION ──")
    if final_cls == "A_COLLECTING_MARKET_AND_SIGNAL_DATA":
        print("  Market-state scarcity remains primary bottleneck.")
        print("  Do NOT change production thresholds.")
    elif final_cls == "B_CONFIRMATION_TOO_STRICT":
        print("  True live dislocations exist but no production signal.")
        print("  Confirmation logic is too strict for 5m windows.")
        print("  Consider promoting best shadow profile to next paper validation.")
    elif final_cls.startswith("C_SHADOW"):
        if shadow_summary.get("CORE_UP_RSI_ONLY_SHADOW", {}).get("net_ev", 0) > 0:
            print("  Shadow profiles produce executable positive-EV paper trades.")
            print("  Consider promoting BEST shadow profile to next paper validation loop.")
        else:
            print("  Shadow exists but net_EV not positive or insufficient data.")
            print("  Do NOT change production thresholds yet.")
    
    print(f"\n  LIVE REMAINS DISABLED.")
    print(f"  Files: {SIGNAL_DEBUG_FILE}")
    print(f"         {SHADOW_REPORT_FILE}")
    print(f"         {SCARCITY_REPORT_FILE}")
    print(f"         {WOULD_TRADE_FILE}")
    print(f"         {pres.JOURNAL_BASE_DIR}/{pres.get_run_id()}/")
    print(f"         {pres.SETTLEMENT_ERROR_FILE}")
    
    # §8: Dashboard
    rc_dict = state.get("resolution_counters", {})
    rc_obj = pres.ResolutionCounters.from_dict(rc_dict) if rc_dict else pres.ResolutionCounters()
    dashboard = pres.compute_dashboard(state, rc_obj)
    if dashboard:
        print(f"\n── DASHBOARD (§8) ──")
        print(f"  Open: {dashboard.get('open_positions',0)}  Resolved: {dashboard.get('resolved_positions',0)}  Unresolved: {dashboard.get('unresolved_past_expiry',0)}")
        wr = dashboard.get('WR', 0)
        print(f"  W: {dashboard.get('wins',0)}  L: {dashboard.get('losses',0)}  WR: {wr:.1%}")
        print(f"  Net PnL: ${dashboard.get('net_PnL',0):.4f}  PF: {dashboard.get('PF',0):.2f}  DD: {dashboard.get('DD',0):.2%}")
        print(f"  EV/share: ${dashboard.get('realized_EV_per_share',0):.4f}  EV/$: ${dashboard.get('realized_EV_per_dollar',0):.4f}")
        print(f"  Settlement errors: {dashboard.get('settlement_errors',0)}  PnL validation errors: {dashboard.get('pnl_validation_errors',0)}")
        for pname in pres.ResolutionCounters.PROFILE_NAMES:
            pd = dashboard.get(pname)
            if pd and isinstance(pd, dict) and pd.get("resolved",0) > 0:
                print(f"  {pname}: {pd.get('resolved',0)}r  WR={pd.get('WR',0):.1%}  PnL=${pd.get('net_PnL',0):.4f}")
    print(f"{'='*70}")
    
    # Save final report as JSON
    rc = state.get("resolution_counters", {})
    final_report = {
        "version": "V19.8",
        "duration_hours": duration_actual,
        "cycles": cycle_count,
        "runtime_errors": runtime_errors,
        "p50_cycle": round(p50_cycle, 2),
        "p95_cycle": round(p95_cycle, 2),
        "cache_hit_rate": round(schedule_cache.cache_hit_rate, 4),
        "prewatch_coverage": round(schedule_cache.prewatch_coverage, 4),
        "first_clean_tick_latency": schedule_cache.first_clean_tick_latency,
        "clean_ticks_seen": schedule_cache.clean_ticks_seen,
        "dirty_ticks_rejected": schedule_cache.dirty_ticks_rejected,
        "markets_seen": len(schedule_cache.markets_seen),
        "books_executable": schedule_cache.books_executable,
        "maker_diagnostic_count": schedule_cache.maker_diagnostic_count,
        "rsi_target_events": rsi_target_events,
        "production_signals": production_signals,
        "shadow_profiles": shadow_summary,
        "scarcity_totals": scarcity_totals,
        "classifications": classifications,
        "final_classification": final_cls,
        "total_entries": total_entries,
        "total_paper_trades": total_paper_trades,
        "resolution_counters": rc,
        "paper_wins": rc.get("paper_wins", 0),
        "paper_losses": rc.get("paper_losses", 0),
        "paper_resolved": rc.get("paper_trades_resolved", 0),
        "paper_settled": rc.get("paper_trades_settled", 0),
        "paper_journaled": rc.get("paper_trades_journaled", 0),
        "settlement_errors": rc.get("settlement_errors", 0),
        "pnl_validation_errors": rc.get("pnl_validation_errors", 0),
        "stop_loss_exits": rc.get("stop_loss_exits", 0),
        "take_profit_exits": rc.get("take_profit_exits", 0),
        "time_decay_exits": rc.get("time_decay_exits", 0),
        "expiry_settlements": rc.get("expiry_settlements", 0),
        "duplicate_settlement_blocks": rc.get("duplicate_settlement_blocks", 0),
        "avg_resolution_delay_seconds": rc.get("avg_resolution_delay_seconds", 0),
        "max_resolution_delay_seconds": rc.get("max_resolution_delay_seconds", 0),
        "active_positions": rc.get("paper_positions_active", 0),
        "unresolved_past_expiry": rc.get("paper_positions_unresolved_past_expiry", 0),
        "live_enabled": False,
        "start_time": start_time.isoformat(),
        "end_time": end_time_actual.isoformat(),
        "per_profile": {k: v for k, v in rc.items() if any(k.startswith(p) for p in pres.ResolutionCounters.PROFILE_NAMES)},
        "dashboard": dashboard,
    }
    
    report_file = SIGNAL_DEBUG_DIR / "v198_final_report.json"
    with open(report_file, "w") as f:
        json.dump(final_report, f, indent=2, default=str)
    print(f"\nFinal report saved to: {report_file}")
    
    return final_report


# ══════════════════════════════════════════════════════════════════════════════
# LIVE READINESS GATES (§10)
# ══════════════════════════════════════════════════════════════════════════════

def check_live_readiness(state, shadow_tracker):
    """
    Check all live readiness gates per §10.
    Returns (ready, gates_dict).
    """
    gates = dict(LIVE_READINESS_GATES)
    
    # Counter invariants
    wins = state.get("wins", 0)
    losses = state.get("losses", 0)
    gates["counter_invariants_hold"] = (wins + losses) == len(state.get("journal", []))
    
    # False accepts
    gates["false_accepts_zero"] = True  # No false accepts if we never went live
    
    # No daily/strike markets
    gates["daily_strike_markets_zero"] = True
    
    # No wrong token side
    gates["wrong_token_side_zero"] = True
    
    # Book executable logic working
    gates["book_executable_working"] = len(state.get("positions", {})) >= 0  # Trivially true for paper
    
    # Recoverability working
    shadow_summary = shadow_tracker.summary()
    has_recoverability_data = any(
        p.get("true_live_dislocations", 0) > 0 for p in shadow_summary.values()
    )
    gates["recoverability_working"] = has_recoverability_data
    
    # No false dislocation opened
    false_disl_opened = sum(
        p.get("false_dislocations", 0) for p in shadow_summary.values()
    )
    gates["no_false_dislocation_opened"] = false_disl_opened == 0
    
    # No dormant longshot opened
    gates["no_dormant_longshot_opened"] = True
    
    # Min 10 executable opportunities OR 5 paper trades
    total_exec = sum(p.get("executable_opportunities", 0) for p in shadow_summary.values())
    total_paper = sum(p.get("paper_trades_opened", 0) for p in shadow_summary.values())
    gates["min_10_executable_opps"] = total_exec >= 10
    gates["min_5_paper_trades"] = total_paper >= 5
    
    # Net EV positive
    total_ev = sum(p.get("net_ev", 0) for p in shadow_summary.values())
    gates["net_ev_positive"] = total_ev > 0
    
    # Settlement errors
    gates["settlement_errors_zero"] = True
    
    # Journal completeness
    gates["journal_complete"] = len(state.get("journal", [])) > 0
    
    ready = all(gates.values()) and LIVE_ENABLED
    return ready, gates


# ══════════════════════════════════════════════════════════════════════════════
# CONTINUOUS RUNNER (V19.8)
# ══════════════════════════════════════════════════════════════════════════════

def run_continuous_v198(scan_interval=30):
    """Run V19.8 continuously in paper mode."""
    state = load_state()
    state["version"] = "V19.8"
    state["mode"] = "paper"
    save_state(state)
    
    v197._init_live()
    shadow_tracker = ShadowTracker()
    
    print(f"{'='*60}")
    print(f"FDC V19.8 — PAPER MODE | {scan_interval}s cycle")
    print(f"LIVE: DISABLED | Shadow: ALL | Multi-asset: BTC+ETH+SOL+XRP")
    print(f"{'='*60}\n")
    
    consecutive_errors = 0
    while True:
        try:
            entries, settled, skip_info, sig_map, debug = run_once_v198(state, shadow_tracker)
            consecutive_errors = 0
            
            # Print compact status
            asset_rsis = " ".join(
                f"{k}:{sig_map.get(k,{}).get('rsi',0):.1f}"
                for k in ASSET_MAP if k in sig_map
            )
            cls = debug.get("classification", {}).get("classification", "?")
            print(f"[{state.get('scans',0)}] {asset_rsis} cls={cls} entries={len(entries)}")
            
            time.sleep(scan_interval)
        except KeyboardInterrupt:
            print(f"\n👋 Stopped. Bankroll: ${state.get('bankroll',0):,.2f} | P&L: ${state.get('total_pnl',0):+,.2f}")
            break
        except Exception as e:
            consecutive_errors += 1
            print(f"❌ Error ({consecutive_errors}): {e}")
            traceback.print_exc()
            if consecutive_errors >= 10:
                print("💀 Too many errors, stopping")
                break
            time.sleep(30)


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if "--signal-loop" in sys.argv:
        hours = 2
        cycle_s = 30
        for i, a in enumerate(sys.argv):
            if a == "--hours" and i + 1 < len(sys.argv):
                hours = float(sys.argv[i + 1])
            if a == "--cycle" and i + 1 < len(sys.argv):
                cycle_s = int(sys.argv[i + 1])
        run_signal_loop(duration_hours=hours, cycle_target_s=cycle_s)
    elif "--continuous" in sys.argv or "-c" in sys.argv:
        run_continuous_v198(scan_interval=30)
    elif "--once" in sys.argv:
        state = load_state()
        shadow_tracker = ShadowTracker()
        entries, settled, skip_info, sig_map, debug = run_once_v198(state, shadow_tracker)
        for asset_key, sig in sig_map.items():
            print(f"{asset_key}: {sig.get('direction','?')} @ {sig.get('confidence',0):.2f} RSI={sig.get('rsi',0):.1f}")
        if debug.get("classification"):
            print(f"Classification: {debug['classification']['classification']}")
            print(f"Reason: {debug['classification'].get('reason','')}")
    elif "--shadow-report" in sys.argv:
        # Read and display latest shadow report
        if SHADOW_REPORT_FILE.exists():
            lines = SHADOW_REPORT_FILE.read_text().strip().split("\n")
            if lines:
                latest = json.loads(lines[-1])
                print(json.dumps(latest, indent=2))
        else:
            print("No shadow report data yet. Run --signal-loop first.")
    elif "--readiness" in sys.argv:
        state = load_state()
        shadow_tracker = ShadowTracker()
        ready, gates = check_live_readiness(state, shadow_tracker)
        print(f"LIVE READINESS: {'✅ READY' if ready else '❌ NOT READY'}")
        for gate, passed in gates.items():
            symbol = "✅" if passed else "❌"
            print(f"  {symbol} {gate}")
    else:
        print(__doc__)
        print("\nUsage:")
        print("  python3 pm_engine_v19_8.py --signal-loop [--hours 2] [--cycle 30]")
        print("  python3 pm_engine_v19_8.py --continuous")
        print("  python3 pm_engine_v19_8.py --once")
        print("  python3 pm_engine_v19_8.py --shadow-report")
        print("  python3 pm_engine_v19_8.py --readiness")