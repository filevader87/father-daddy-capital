#!/usr/bin/env python3
"""
V20.3 Rebuilt Transition Score — Section 7
=============================================
Replaces hard-clamped max(-1, min(1, score)) with soft tanh clipping.

V20.2 audit found 83% of transition scores were clamped to exactly ±1.0,
destroying all continuous signal information. This rebuild uses tanh
normalization which preserves continuous variation.

Components tracked separately:
  - selected_bid_velocity
  - selected_ask_velocity
  - selected_spread_delta
  - selected_imbalance_delta
  - selected_token_price_velocity
  - opposite_token_price_velocity
  - spot_velocity
  - reference_distance_delta

Block promotion if >50% of scores are exactly ±1.0 or exactly ±tanh_limit.

Author: Hugh (3rd of 5) for Father Daddy Capital
"""
import math
from dataclasses import dataclass, field
from typing import Optional, Dict, List
from collections import deque
import time


# ── Configuration ──
TRANSITION_SCALE = 3.0  # tanh(raw_score / scale) — lower = softer clipping
MINIMUM_TRANSITION_THRESHOLD = 0.05  # Minimum |transition_score| to enter
DEGENERATE_UNIQUE_THRESHOLD = 20  # <20 unique scores after 100+ obs = degenerate
DEGENERATE_MAX_RATIO = 0.50  # >50% of scores clamped = degenerate


@dataclass
class TransitionComponents:
    """Raw transition score components, tracked separately."""
    raw_score: float = 0.0
    selected_bid_velocity: float = 0.0
    selected_ask_velocity: float = 0.0
    selected_spread_delta: float = 0.0
    selected_imbalance_delta: float = 0.0
    selected_token_price_velocity: float = 0.0
    opposite_token_price_velocity: float = 0.0
    spot_velocity: float = 0.0
    reference_distance_delta: float = 0.0


@dataclass
class TransitionResultV203:
    """V20.3 transition score result with soft tanh normalization."""
    # Final normalized score
    transition_score: float = 0.0      # tanh(raw_score / TRANSITION_SCALE)
    raw_transition_score: float = 0.0   # Before normalization
    
    # Components
    components: TransitionComponents = field(default_factory=TransitionComponents)
    
    # Legacy compatibility
    bid_strengthening: Optional[bool] = None
    ask_weakening: Optional[bool] = None
    spread_compressing: Optional[bool] = None
    
    # Diagnostics
    is_degenerate: bool = False
    degenerate_reason: str = ""


class OrderbookTransitionTrackerV203:
    """V20.3 Orderbook transition tracker with tanh normalization."""
    
    WINDOW_SIZE = 60  # Number of snapshots to keep (~15s at 250ms polling)
    
    def __init__(self):
        self._snapshots: deque = deque(maxlen=self.WINDOW_SIZE * 2)
        self._observation_count: int = 0
        self._raw_score_history: List[float] = []
        self._normalized_score_history: List[float] = []
        self._unique_raw: set = set()
        self._unique_normalized: set = set()
        self.degenerate: bool = False
        self.degenerate_reason: str = ""
    
    def add_snapshot(self, bid_depth: float, ask_depth: float,
                     spread: float, imbalance: float,
                     up_price: float, down_price: float,
                     spot_reference: float = 0.0,
                     timestamp: Optional[float] = None):
        """Add an orderbook snapshot."""
        from dataclasses import dataclass as _dc
        
        @_dc
        class Snap:
            timestamp: float
            bid_depth: float
            ask_depth: float
            spread: float
            imbalance: float
            up_price: float
            down_price: float
            spot_reference: float
        
        snap = Snap(
            timestamp=timestamp or time.time(),
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            spread=spread,
            imbalance=imbalance,
            up_price=up_price,
            down_price=down_price,
            spot_reference=spot_reference,
        )
        self._snapshots.append(snap)
    
    def compute_transition(self) -> TransitionResultV203:
        """Compute V20.3 transition score with tanh normalization."""
        if len(self._snapshots) < 2:
            reason = "Insufficient snapshots"
            return TransitionResultV203(
                transition_score=0.0,
                raw_transition_score=0.0,
                is_degenerate=False,
                degenerate_reason=reason,
            )
        
        # Reference = oldest in window, current = newest
        window_size = min(32, len(self._snapshots))
        snap_now = self._snapshots[-1]
        snap_ref = self._snapshots[-window_size]
        
        # Compute deltas
        bid_delta = snap_now.bid_depth - snap_ref.bid_depth
        ask_delta = snap_now.ask_depth - snap_ref.ask_depth
        spread_delta = snap_now.spread - snap_ref.spread
        imbalance_delta = snap_now.imbalance - snap_ref.imbalance
        up_delta = (snap_now.up_price - snap_ref.up_price) if snap_ref.up_price else 0.0
        down_delta = (snap_now.down_price - snap_ref.down_price) if snap_ref.down_price else 0.0
        spot_delta = (snap_now.spot_reference - snap_ref.spot_reference) if snap_ref.spot_reference else 0.0
        
        # Time delta
        dt = max(1.0, snap_now.timestamp - snap_ref.timestamp)
        
        # Scale factors (prevent division by zero)
        bid_scale = max(snap_ref.bid_depth, 1.0) / 8.0
        ask_scale = max(snap_ref.ask_depth, 1.0) / 8.0
        
        # Velocity components
        bid_velocity = (bid_delta / dt) / bid_scale
        ask_velocity = (ask_delta / dt) / ask_scale
        price_velocity = up_delta / max(snap_ref.up_price, 0.01) / dt if snap_ref.up_price else 0.0
        opposite_velocity = down_delta / max(snap_ref.down_price, 0.01) / dt if snap_ref.down_price else 0.0
        
        # Raw score composition (weighted sum)
        raw_score = 0.0
        raw_score += bid_velocity * dt * 5.0   # Bid strengthening = bullish
        raw_score += ask_velocity * dt * 5.0   # Ask weakening = bullish (negative ask delta)
        raw_score += 0.15 if spread_delta < -0.5 else 0.0  # Spread compression
        raw_score += imbalance_delta * 0.5
        raw_score += (price_velocity * dt * 0.25)
        
        # ── V20.3: Soft tanh normalization ──
        # Instead of: max(-1.0, min(1.0, score))
        # Use: tanh(score / TRANSITION_SCALE)
        # This preserves continuous variation while bounding to (-1, 1)
        normalized_score = math.tanh(raw_score / TRANSITION_SCALE)
        
        # Component tracking
        components = TransitionComponents(
            raw_score=raw_score,
            selected_bid_velocity=bid_velocity * dt,
            selected_ask_velocity=ask_velocity * dt,
            selected_spread_delta=spread_delta,
            selected_imbalance_delta=imbalance_delta,
            selected_token_price_velocity=price_velocity * dt,
            opposite_token_price_velocity=opposite_velocity * dt,
            spot_velocity=spot_delta / dt if dt > 0 else 0.0,
            reference_distance_delta=spot_delta,
        )
        
        # Detect degenerate scores
        self._observation_count += 1
        self._raw_score_history.append(raw_score)
        self._normalized_score_history.append(normalized_score)
        self._unique_raw.add(round(raw_score, 6))
        self._unique_normalized.add(round(normalized_score, 6))
        
        # Check degenerate: too few unique values or too many clamped
        clamped_ratio = 0.0
        if self._normalized_score_history:
            near_extreme = sum(
                1 for s in self._normalized_score_history
                if abs(s) > 0.99  # tanh(±~7) ≈ ±0.9999
            )
            clamped_ratio = near_extreme / len(self._normalized_score_history)
        
        if clamped_ratio > DEGENERATE_MAX_RATIO and self._observation_count >= 100:
            self.degenerate = True
            self.degenerate_reason = (
                f"CLAMPED_RATIO={clamped_ratio:.2%} exceeds {DEGENERATE_MAX_RATIO:.0%} "
                f"after {self._observation_count} observations"
            )
        elif len(self._unique_normalized) < DEGENERATE_UNIQUE_THRESHOLD and self._observation_count >= 100:
            self.degenerate = True
            self.degenerate_reason = (
                f"Only {len(self._unique_normalized)} unique normalized scores "
                f"after {self._observation_count} observations"
            )
        
        return TransitionResultV203(
            transition_score=normalized_score,
            raw_transition_score=raw_score,
            components=components,
            bid_strengthening=bid_velocity > 0.01,
            ask_weakening=ask_velocity < -0.01,
            spread_compressing=spread_delta < -0.5,
            is_degenerate=self.degenerate,
            degenerate_reason=self.degenerate_reason,
        )
    
    def get_score_distribution(self) -> dict:
        """Return distribution statistics for both raw and normalized scores."""
        n = len(self._normalized_score_history)
        if n == 0:
            return {"count": 0, "degenerate": self.degenerate}
        
        sorted_norm = sorted(self._normalized_score_history)
        sorted_raw = sorted(self._raw_score_history)
        
        extreme_ratio = sum(1 for s in sorted_norm if abs(s) > 0.99) / n
        
        return {
            "count": n,
            "unique_normalized": len(self._unique_normalized),
            "unique_raw": len(self._unique_raw),
            "normalized_mean": sum(sorted_norm) / n,
            "normalized_p25": sorted_norm[int(n * 0.25)],
            "normalized_p50": sorted_norm[int(n * 0.50)],
            "normalized_p75": sorted_norm[int(n * 0.75)],
            "raw_mean": sum(sorted_raw) / n,
            "raw_p25": sorted_raw[int(n * 0.25)],
            "raw_p50": sorted_raw[int(n * 0.50)],
            "raw_p75": sorted_raw[int(n * 0.75)],
            "extreme_ratio": round(extreme_ratio, 4),
            "is_degenerate": self.degenerate,
            "degenerate_reason": self.degenerate_reason,
        }