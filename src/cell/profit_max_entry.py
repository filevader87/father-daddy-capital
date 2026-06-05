#!/usr/bin/env python3
"""
V20.3 Profit-Max Entry Logic — §8
====================================
Bayesian entry decision using Beta posterior EV.

expected_value = empirical_p - entry_ask
Prior: Beta(1,1)
Update after every resolution.

Trade paper if:
  posterior_EV credible interval not strongly negative

For live candidate later, require:
  lower_credible_bound_EV > 0

Author: Hugh (3rd of 5) for Father Daddy Capital
"""
import math
from dataclasses import dataclass
from typing import Optional, Tuple

import sys
sys.path.insert(0, '/home/naq1987s/father-daddy-capital')
from src.cell.cell_framework import CellKey, CellState, CellTracker


# ── Configuration ──

BETA_ALPHA_PRIOR = 1.0   # Prior pseudocount for wins
BETA_BETA_PRIOR = 1.0    # Prior pseudocount for losses
CREDIBLE_LEVEL = 0.95     # 95% credible interval
MIN_SAMPLES_FOR_CREDIBLE = 5  # Need at least 5 samples for meaningful CI
MIN_POSTERIOR_PROB = 0.30   # Don't trade if P(win) < 30% even in exploration


@dataclass
class EntryDecision:
    """Decision on whether to enter a paper trade."""
    should_enter: bool = False
    cell_key: Optional[CellKey] = None
    side: str = ""                  # "UP" or "DOWN"
    entry_ask: float = 0.0         # Price we'd pay
    size_usd: float = 2.0
    expected_value: float = 0.0    # posterior_EV = 2*p - 1 at 0.50
    posterior_p: float = 0.0       # P(win)
    credible_lower_p: float = 0.0  # Lower bound of credible interval
    credible_upper_p: float = 0.0  # Upper bound of credible interval
    credible_lower_ev: float = 0.0 # Lower bound EV
    credible_upper_ev: float = 0.0 # Upper bound EV
    reason: str = ""                 # Why enter or not enter
    direction_tag: str = ""          # DOWN_CONTINUATION, etc.


class ProfitMaxEntryLogic:
    """Profit-maximizing entry logic using Bayesian posterior.
    
    For each candidate side, computes:
      expected_value = posterior_p * (1/entry_ask) - 1
      
    Where posterior_p comes from Beta(alpha, beta) distribution
    updated with wins/losses for that cell.
    
    Trade paper if credible interval not strongly negative.
    Require lower_credible_bound_EV > 0 for live.
    """
    
    def __init__(self, tracker: CellTracker):
        self.tracker = tracker
    
    def evaluate_entry(
        self,
        cell_key: CellKey,
        side: str,
        entry_ask: float,
        size_usd: float = 2.0,
        rsi: float = 50.0,
        direction_tag: str = "NEUTRAL",
    ) -> EntryDecision:
        """Evaluate whether to enter a paper trade.
        
        Args:
            cell_key: Cell identifier with asset/interval/side/bucket/regime/decile/tte
            side: "UP" or "DOWN"
            entry_ask: Ask price (what we'd pay)
            size_usd: Position size in dollars
            rsi: Current RSI
            direction_tag: DOWN_CONTINUATION, UP_REVERSAL, etc.
        
        Returns:
            EntryDecision with should_enter and all posterior stats.
        """
        decision = EntryDecision(
            cell_key=cell_key,
            side=side,
            entry_ask=entry_ask,
            size_usd=size_usd,
            direction_tag=direction_tag,
        )
        
        # Get cell state
        cell = self.tracker.get_or_create_cell(cell_key)
        
        # ── Cell disabled → no entry ──
        if cell.status.value == "DISABLED":
            decision.reason = f"Cell DISABLED: {cell.kill_reason}"
            return decision
        
        # ── Compute posterior ──
        alpha = cell.alpha  # wins + prior
        beta = cell.beta    # losses + prior
        
        posterior_p = alpha / (alpha + beta)
        decision.posterior_p = round(posterior_p, 4)
        
        # ── Expected value ──
        # EV = posterior_p * (1 / entry_ask) * entry_ask - size_usd
        # Simplified: EV_per_dollar = posterior_p / entry_ask - 1
        # At entry 0.50: EV = 2*posterior_p - 1
        # At entry 0.56: EV = posterior_p / 0.56 - 1
        
        if entry_ask > 0:
            shares = size_usd / entry_ask
            win_payout = shares * 1.0  # Binary: winner pays $1 per share
            ev = posterior_p * win_payout - size_usd
            ev_per_dollar = ev / size_usd
        else:
            decision.reason = "Invalid entry_ask <= 0"
            return decision
        
        decision.expected_value = round(ev_per_dollar, 4)
        
        # ── Credible interval ──
        if cell.resolved_trades >= MIN_SAMPLES_FOR_CREDIBLE:
            # Approximate 95% CI using normal approximation to Beta
            variance = (alpha * beta) / ((alpha + beta) ** 2 * (alpha + beta + 1))
            std = math.sqrt(variance) if variance > 0 else 0
            
            lower_p = max(0, posterior_p - 1.96 * std)
            upper_p = min(1, posterior_p + 1.96 * std)
        else:
            # Too few samples → wide interval
            lower_p = max(0, posterior_p - 0.3)
            upper_p = min(1, posterior_p + 0.3)
        
        decision.credible_lower_p = round(lower_p, 4)
        decision.credible_upper_p = round(upper_p, 4)
        
        # Convert to EV bounds
        if entry_ask > 0:
            decision.credible_lower_ev = round(lower_p / entry_ask - 1, 4)
            decision.credible_upper_ev = round(upper_p / entry_ask - 1, 4)
        
        # ── Entry decision ──
        # Paper mode: enter if credible interval not strongly negative
        # (lower bound > -0.30 means we're not confident it's terrible)
        lower_ev_threshold = -0.30 if cell.resolved_trades < 20 else -0.10
        
        if cell.resolved_trades == 0:
            # No data yet → exploration mode, enter unless price is extreme
            if entry_ask > 0.85 or entry_ask < 0.10:
                decision.reason = f"Entry_ask={entry_ask:.3f} too extreme for exploration"
                return decision
            decision.should_enter = True
            decision.reason = f"Exploration: no data for cell, entering at {entry_ask:.3f}"
            return decision
        
        if posterior_p < MIN_POSTERIOR_PROB:
            decision.reason = f"Posterior P(win)={posterior_p:.3f} < {MIN_POSTERIOR_PROB}"
            return decision
        
        if decision.credible_lower_ev < lower_ev_threshold:
            decision.reason = (
                f"Credible lower EV={decision.credible_lower_ev:.4f} < {lower_ev_threshold:.2f}. "
                f"P(win)=[{lower_p:.3f}, {upper_p:.3f}]"
            )
            return decision
        
        # Check if cell is approaching kill thresholds
        if cell.resolved_trades >= 10 and cell.ev_per_dollar < -0.10:
            decision.reason = f"Cell EV/dollar={cell.ev_per_dollar:.4f} near kill threshold"
            return decision
        
        decision.should_enter = True
        decision.reason = (
            f"Enter {side} at {entry_ask:.3f}. "
            f"P(win)=[{lower_p:.3f}, {upper_p:.3f}], "
            f"EV=[{decision.credible_lower_ev:.4f}, {decision.credible_upper_ev:.4f}], "
            f"N={cell.resolved_trades}"
        )
        return decision
    
    def live_entry_check(self, decision: EntryDecision) -> EntryDecision:
        """Additional checks for live candidate entry.
        
        For live: require lower_credible_bound_EV > 0
        Plus all V20.3 safety gates.
        """
        if not decision.should_enter:
            return decision
        
        # Live requires positive lower bound
        if decision.credible_lower_ev <= 0:
            decision.should_enter = False
            decision.reason = (
                f"LIVE BLOCKED: credible lower EV={decision.credible_lower_ev:.4f} <= 0. "
                f"Requires positive lower credible bound."
            )
            return decision
        
        # Live requires at least 50 resolved trades
        if decision.cell_key is None:
            decision.should_enter = False
            decision.reason = "LIVE BLOCKED: no cell key."
            return decision
        cell = self.tracker.get_or_create_cell(decision.cell_key)
        if cell.resolved_trades < 50:
            decision.should_enter = False
            decision.reason = (
                f"LIVE BLOCKED: only {cell.resolved_trades} resolved trades. "
                f"Requires 50+ for live."
            )
            return decision
        
        return decision