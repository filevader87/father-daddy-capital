#!/usr/bin/env python3
"""
V21.6 Execution Router (§7)
=============================
Dynamically decides: maker vs taker vs hybrid passive-aggressive entry.

Logic:
- high convexity + low liquidity → passive maker
- high momentum + collapsing spread → taker
- queue instability → cancel/reprice
- widening spread → abort
"""

import numpy as np
from enum import Enum
from typing import Tuple, Optional
from dataclasses import dataclass


class ExecutionMode(Enum):
    MAKER = "maker"
    TAKER = "taker"
    HYBRID = "hybrid"
    ABORT = "abort"


@dataclass
class RoutingDecision:
    mode: ExecutionMode
    confidence: float  # ∈ [0,1]
    reason: str
    max_slippage_bps: float
    limit_price_offset: float  # Offset from mid for maker orders
    time_limit_ms: int  # Maximum wait time for fill
    size_pct: float  # Fraction of full position to use
    should_cancel_on_widen: bool
    should_reprice: bool


class ExecutionRouter:
    """
    §7: Routes execution decisions based on convexity, liquidity, and market conditions.
    """

    def __init__(self):
        self.consecutive_aborts = 0
        self.consecutive_maker_fills = 0
        self.consecutive_taker_fills = 0
        self.total_maker = 0
        self.total_taker = 0
        self.total_hybrid = 0
        self.total_aborts = 0

    def route(self, convexity_score: float,
              liquidity_score: float,
              friction_score: float,
              spread_bps: float,
              spread_trend: str,  # 'tightening', 'stable', 'widening'
              momentum_strength: float,  # |velocity|
              volatility: float,
              hostility_score: float,
              price: float,
              bucket_zone: str) -> RoutingDecision:
        """
        §7: Determine optimal execution mode.

        High convexity + low liquidity → passive maker (wait for better fill)
        High momentum + collapsing spread → taker (execute immediately)
        Queue instability → cancel/reprice
        Widening spread → abort
        """
        self.consecutive_aborts = 0  # Reset on evaluation

        # === §12: Adversarial awareness ===
        if hostility_score > 0.75:
            # High hostility: reduce sizing, increase selectivity
            return self._route_hostile(convexity_score, friction_score,
                                         spread_bps, price, bucket_zone)

        if spread_trend == 'widening' and spread_bps > 1500:
            # §7: Widening spread → abort
            self.total_aborts += 1
            return RoutingDecision(
                mode=ExecutionMode.ABORT,
                confidence=0.8,
                reason=f"widening_spread_{spread_bps:.0f}bps",
                max_slippage_bps=0,
                limit_price_offset=0,
                time_limit_ms=0,
                size_pct=0.0,
                should_cancel_on_widen=True,
                should_reprice=False,
            )

        # === Decision matrix ===

        # Convexity × Liquidity interaction
        high_convexity = convexity_score > 0.5
        high_liquidity = liquidity_score > 0.6
        high_momentum = abs(momentum_strength) > 0.2
        collapsing_spread = spread_trend == 'tightening'
        high_friction = friction_score > 0.6

        # §7: Core routing logic
        if high_convexity and not high_liquidity:
            # High convexity + low liquidity → passive maker
            # Wait for better price, the edge is large enough to be patient
            return self._route_maker(convexity_score, liquidity_score,
                                       friction_score, spread_bps, price,
                                       bucket_zone, momentum_strength)

        elif high_momentum and collapsing_spread:
            # High momentum + collapsing spread → taker
            # Speed matters, the spread is compressing in our favor
            return self._route_taker(convexity_score, friction_score,
                                       spread_bps, price, bucket_zone,
                                       momentum_strength)

        elif high_convexity and high_liquidity:
            # Good convexity, good liquidity → hybrid
            # Split: passive start, aggressive finish
            return self._route_hybrid(convexity_score, liquidity_score,
                                       friction_score, spread_bps, price,
                                       bucket_zone)

        elif high_friction:
            # High friction → either abort or very selective maker
            if convexity_score < 0.3:
                self.total_aborts += 1
                return RoutingDecision(
                    mode=ExecutionMode.ABORT,
                    confidence=0.7,
                    reason="high_friction_low_convexity",
                    max_slippage_bps=0,
                    limit_price_offset=0,
                    time_limit_ms=0,
                    size_pct=0.0,
                    should_cancel_on_widen=True,
                    should_reprice=False,
                )
            else:
                # High convexity despite friction → cautious maker
                return self._route_maker(convexity_score, liquidity_score,
                                           friction_score, spread_bps, price,
                                           bucket_zone, momentum_strength)

        else:
            # Default: moderate conditions → hybrid with smaller size
            return self._route_hybrid(convexity_score, liquidity_score,
                                       friction_score, spread_bps, price,
                                       bucket_zone)

    def _route_maker(self, convexity, liquidity, friction, spread_bps,
                     price, bucket, momentum) -> RoutingDecision:
        """Passive maker: limit order at favorable price."""
        self.total_maker += 1
        self.consecutive_maker_fills += 1
        self.consecutive_taker_fills = 0

        # Maker limit offset: how far from mid to place
        # More offset when liquidity is good (can afford to wait)
        limit_offset = 0.005 + (1.0 - liquidity) * 0.01  # 0.5-1.5¢ offset
        if spread_bps > 1000:
            limit_offset *= 1.5  # Wider markets need more patience

        # Time limit: wait longer for cheap tokens (more edge)
        if price < 0.08:
            time_limit = 5000  # 5 seconds for ultra-cheap
        elif price < 0.12:
            time_limit = 3000
        else:
            time_limit = 2000

        # Size: full conviction for high convexity
        size_pct = min(1.0, convexity * 1.2)

        # For DOWN tokens: buy at bid (lower is better)
        # Limit price below current ask
        max_slip = 300  # Maker allows less slippage

        return RoutingDecision(
            mode=ExecutionMode.MAKER,
            confidence=min(1.0, convexity * (1 - friction * 0.5)),
            reason="high_convexity_low_liquidity",
            max_slippage_bps=max_slip,
            limit_price_offset=limit_offset,
            time_limit_ms=time_limit,
            size_pct=size_pct,
            should_cancel_on_widen=True,
            should_reprice=True,
        )

    def _route_taker(self, convexity, friction, spread_bps, price,
                     bucket, momentum) -> RoutingDecision:
        """Aggressive taker: market order for speed."""
        self.total_taker += 1
        self.consecutive_taker_fills += 1
        self.consecutive_maker_fills = 0

        # Taker accepts higher slippage for speed
        max_slip = 500 + friction * 1000  # 500-1500bps tolerance
        if price < 0.08:
            max_slip = 800  # More tolerance for cheap tokens

        # Fast execution needed
        time_limit = 1000  # 1 second max

        # Size: conviction based, momentum bonus
        size_pct = min(1.0, convexity * (1.0 + abs(momentum) * 0.5))

        return RoutingDecision(
            mode=ExecutionMode.TAKER,
            confidence=min(1.0, convexity * (1 - friction * 0.3)),
            reason="high_momentum_collapsing_spread",
            max_slippage_bps=max_slip,
            limit_price_offset=0,  # No offset for taker
            time_limit_ms=time_limit,
            size_pct=size_pct,
            should_cancel_on_widen=True,
            should_reprice=False,
        )

    def _route_hybrid(self, convexity, liquidity, friction, spread_bps,
                      price, bucket) -> RoutingDecision:
        """Hybrid passive-aggressive: start passive, finish aggressive."""
        self.total_hybrid += 1
        self.consecutive_maker_fills = 0
        self.consecutive_taker_fills = 0

        # Phase 1: Passive (50% of position)
        # Phase 2: Aggressive (50% after timeout)
        limit_offset = 0.003  # Small offset for passive phase
        time_limit = 3000  # 3 seconds for passive phase

        size_pct = min(0.85, convexity * (1 - friction * 0.4))

        return RoutingDecision(
            mode=ExecutionMode.HYBRID,
            confidence=min(1.0, convexity * (1 - friction * 0.4)),
            reason="hybrid_passive_aggressive",
            max_slippage_bps=400,
            limit_price_offset=limit_offset,
            time_limit_ms=time_limit,
            size_pct=size_pct,
            should_cancel_on_widen=True,
            should_reprice=True,
        )

    def _route_hostile(self, convexity, friction, spread_bps,
                       price, bucket) -> RoutingDecision:
        """§12: High hostility — reduce sizing, increase selectivity."""
        # Only proceed if convexity is very strong
        if convexity < 0.6:
            self.total_aborts += 1
            return RoutingDecision(
                mode=ExecutionMode.ABORT,
                confidence=0.9,
                reason="hostile_market_low_convexity",
                max_slippage_bps=0,
                limit_price_offset=0,
                time_limit_ms=0,
                size_pct=0.0,
                should_cancel_on_widen=True,
                should_reprice=False,
            )

        # Proceed but with reduced sizing and maker preference
        self.total_maker += 1
        return RoutingDecision(
            mode=ExecutionMode.MAKER,
            confidence=0.4,
            reason="hostile_market_cautious_maker",
            max_slippage_bps=200,
            limit_price_offset=0.002,
            time_limit_ms=4000,
            size_pct=0.5,  # Half size in hostile conditions
            should_cancel_on_widen=True,
            should_reprice=True,
        )

    def get_routing_stats(self) -> dict:
        """Return routing mode statistics."""
        total = self.total_maker + self.total_taker + self.total_hybrid + self.total_aborts
        return {
            'maker': self.total_maker,
            'taker': self.total_taker,
            'hybrid': self.total_hybrid,
            'abort': self.total_aborts,
            'total_decisions': total,
            'maker_pct': self.total_maker / max(total, 1) * 100,
            'taker_pct': self.total_taker / max(total, 1) * 100,
        }