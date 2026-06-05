#!/usr/bin/env python3
"""
V20.3 DOWN-Side Investigation — Section 10
============================================
BTC_DOWN_CONTINUATION_DIAGNOSTIC profile.

The 11-trade V20.2 audit found:
  - UP selected: 2/11 wins (18.2% WR)
  - DOWN implied: 9/11 wins (81.8% WR)
  - DOWN-side 8-51¢ entries = 488% ROI on the whale

This is a DIAGNOSTIC-ONLY profile. NO LIVE TRADING.

Evaluation criteria:
  - RSI < 35
  - transition_score bullish/negative mismatch
  - spot_velocity negative
  - reference_distance worsening
  - market still 0.40-0.60

Logs for each candidate:
  - would_buy_down: bool
  - down_entry_price: float
  - down_binary_outcome: "WIN" / "LOSS" / "UNRESOLVED"
  - down_realized_pnl: float

Author: Hugh (3rd of 5) for Father Daddy Capital
"""
from dataclasses import dataclass, field
from typing import Optional, List, Dict


# ── Diagnostic Profile Config ──
PROFILE_NAME = "BTC_DOWN_CONTINUATION_DIAGNOSTIC"
PROFILE_TYPE = "DIAGNOSTIC_ONLY"  # Never promote to live
LIVE_TRADING_BLOCKED = True


@dataclass
class DOWNCandidateV203:
    """A candidate for DOWN-side evaluation."""
    slug: str = ""
    timestamp: str = ""
    
    # Market state
    up_price: float = 0.0
    down_price: float = 0.0
    up_bid: float = 0.0
    up_ask: float = 0.0
    down_bid: float = 0.0
    down_ask: float = 0.0
    
    # Diagnostics
    rsi: float = 50.0
    rsi_below_35: bool = False
    spot_velocity: float = 0.0
    spot_velocity_negative: bool = False
    reference_distance: float = 0.0
    reference_distance_worsening: bool = False
    transition_score: float = 0.0
    transition_mismatch: bool = False    # bullish score but negative spot
    regime: str = ""
    
    # DOWN evaluation
    would_buy_down: bool = False
    down_entry_price: float = 0.0       # ask price for DOWN token
    down_binary_outcome: str = "UNRESOLVED"  # "WIN", "LOSS", "UNRESOLVED"
    down_realized_pnl: float = 0.0
    down_shares: float = 0.0
    down_size_usd: float = 2.0
    
    # Conditions met
    meets_rsi_condition: bool = False
    meets_velocity_condition: bool = False
    meets_transition_mismatch: bool = False
    meets_price_range: bool = False
    meets_reference_worsening: bool = False
    
    # Gate failures
    gate_failures: List[str] = field(default_factory=list)


class DOWNDiagnosticV203:
    """V20.3 DOWN-side diagnostic evaluator.
    
    Evaluates whether DOWN edge exists when:
      1. RSI < 35 (oversold)
      2. transition_score bullish/negative mismatch
      3. spot_velocity negative
      4. reference_distance worsening
      5. Market still 0.40-0.60 (not already extreme)
    
    Each candidate is logged with all conditions and binary outcome.
    No live trading.
    """
    
    # Conditions for DOWN entry
    RSI_THRESHOLD = 35.0
    VELOCITY_THRESHOLD = -0.001  # Negative velocity
    PRICE_RANGE = (0.40, 0.60)   # Market not at extremes
    TRANSITION_MISMATCH = True   # Score says UP but spot says DOWN
    
    def evaluate_candidate(
        self,
        slug: str,
        up_price: float,
        down_price: float,
        up_bid: float = 0.0,
        up_ask: float = 0.0,
        down_bid: float = 0.0,
        down_ask: float = 0.0,
        rsi: float = 50.0,
        spot_velocity: float = 0.0,
        reference_distance: float = 0.0,
        reference_distance_delta: float = 0.0,
        transition_score: float = 0.0,
        regime: str = "",
        size_usd: float = 2.0,
    ) -> DOWNCandidateV203:
        """Evaluate a DOWN-side candidate.
        
        Returns DOWNCandidateV203 with all conditions and whether
        DOWN would have been profitable.
        """
        from datetime import datetime, timezone
        
        candidate = DOWNCandidateV203(
            slug=slug,
            timestamp=datetime.now(timezone.utc).isoformat(),
            up_price=up_price,
            down_price=down_price,
            up_bid=up_bid,
            up_ask=up_ask,
            down_bid=down_bid,
            down_ask=down_ask,
            rsi=rsi,
            spot_velocity=spot_velocity,
            reference_distance=reference_distance,
            transition_score=transition_score,
            regime=regime,
            down_size_usd=size_usd,
        )
        
        # ── Condition 1: RSI < 35 ──
        candidate.meets_rsi_condition = rsi < self.RSI_THRESHOLD
        candidate.rsi_below_35 = candidate.meets_rsi_condition
        if not candidate.meets_rsi_condition:
            candidate.gate_failures.append(f"RSI={rsi:.1f} >= {self.RSI_THRESHOLD}")
        
        # ── Condition 2: spot_velocity negative ──
        candidate.meets_velocity_condition = spot_velocity < self.VELOCITY_THRESHOLD
        candidate.spot_velocity_negative = candidate.meets_velocity_condition
        if not candidate.meets_velocity_condition:
            candidate.gate_failures.append(f"spot_velocity={spot_velocity:.6f} >= {self.VELOCITY_THRESHOLD}")
        
        # ── Condition 3: transition_score bullish/velocity mismatch ──
        # Score says UP (positive) but spot velocity is negative
        candidate.transition_mismatch = (transition_score > 0.1 and spot_velocity < 0)
        candidate.meets_transition_mismatch = candidate.transition_mismatch
        if not candidate.meets_transition_mismatch:
            candidate.gate_failures.append(
                f"transition_score={transition_score:.3f} vs velocity={spot_velocity:.6f} — no mismatch"
            )
        
        # ── Condition 4: reference_distance worsening ──
        candidate.reference_distance_worsening = reference_distance_delta > 0
        candidate.meets_reference_worsening = candidate.reference_distance_worsening
        if not candidate.meets_reference_worsening:
            candidate.gate_failures.append(f"reference_distance_delta={reference_distance_delta:.4f} not worsening")
        
        # ── Condition 5: Market in 0.40-0.60 range ──
        candidate.meets_price_range = (
            self.PRICE_RANGE[0] <= up_price <= self.PRICE_RANGE[1]
        )
        if not candidate.meets_price_range:
            candidate.gate_failures.append(f"up_price={up_price:.3f} outside {self.PRICE_RANGE}")
        
        # ── Would we buy DOWN? ──
        # Require at least 2 of 5 conditions
        conditions_met = sum([
            candidate.meets_rsi_condition,
            candidate.meets_velocity_condition,
            candidate.meets_transition_mismatch,
            candidate.meets_reference_worsening,
            candidate.meets_price_range,
        ])
        
        # Relaxed: would_buy_down if velocity_condition + at least 1 other
        # Or: RSI < 35 + price in range
        candidate.would_buy_down = (
            (candidate.meets_velocity_condition and conditions_met >= 2) or
            (candidate.meets_rsi_condition and candidate.meets_price_range)
        )
        
        # ── DOWN entry price ──
        candidate.down_entry_price = down_ask if down_ask > 0 else down_price
        
        # If we would have bought DOWN, compute hypothetical PnL
        if candidate.would_buy_down and candidate.down_entry_price > 0:
            candidate.down_shares = round(size_usd / candidate.down_entry_price, 6)
            # outcome set later when market resolves
        
        return candidate
    
    def resolve_candidate(
        self,
        candidate: DOWNCandidateV203,
        resolved_winner: str,
    ) -> DOWNCandidateV203:
        """Resolve a DOWN candidate with actual binary outcome.
        
        Args:
            candidate: The candidate to resolve
            resolved_winner: "UP" or "DOWN"
        
        Returns:
            Updated candidate with binary outcome and realized PnL.
        """
        if resolved_winner not in ("UP", "DOWN"):
            candidate.down_binary_outcome = "UNRESOLVED"
            return candidate
        
        # DOWN token wins if market goes DOWN
        is_down_win = (resolved_winner == "DOWN")
        settlement_value = 1.0 if is_down_win else 0.0
        
        candidate.down_binary_outcome = "WIN" if is_down_win else "LOSS"
        
        if candidate.would_buy_down and candidate.down_entry_price > 0:
            payout = candidate.down_shares * settlement_value
            candidate.down_realized_pnl = round(payout - candidate.down_size_usd, 4)
        else:
            candidate.down_realized_pnl = 0.0
        
        return candidate