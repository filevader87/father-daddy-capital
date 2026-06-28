"""
V20 Orderbook Transition Engine

Tracks rolling 15s/30s changes in orderbook microstructure to generate
a transition_score ∈ [-1, 1]:

  +1.0 = strong bullish reversal pressure (bid strengthening, ask weakening, spread compressing)
  -1.0 = strong bearish continuation pressure (bid weakening, ask strengthening, spread widening)
   0.0 = neutral / no clear transition

A trade may only occur if transition_score > minimum_transition_threshold.

Transition features tracked:
  - bid velocity / ask velocity changes
  - spread compression / widening
  - imbalance delta
  - token price velocity
  - spot reference velocity
  - degenerate detection (<20 unique scores after 100+ observations)
"""

from dataclasses import dataclass
from typing import Optional, Dict
from collections import deque
import time

# ── Configuration ──
MINIMUM_TRANSITION_THRESHOLD = 0.05  # §2: Lowered from 0.10 for continuous scoring


@dataclass
class TransitionSnapshot:
    timestamp: float
    bid_depth: float
    ask_depth: float
    spread: float
    imbalance: float          # (bid - ask) / (bid + ask), ∈ [-1, 1]
    up_price: float
    down_price: float
    spot_reference: float = 0.0


@dataclass
class TransitionResult:
    transition_score: float    # ∈ [-1, 1]
    # Continuous normalized velocity components
    bid_velocity_15s: float = 0.0
    ask_velocity_15s: float = 0.0
    spread_delta_15s: float = 0.0
    imbalance_delta_15s: float = 0.0
    token_price_velocity_15s: float = 0.0
    spot_velocity_15s: float = 0.0
    # Legacy optional fields (for API compatibility)
    bid_strengthening: Optional[bool] = None
    ask_weakening: Optional[bool] = None
    spread_compressing: Optional[bool] = None
    bid_depth_acceleration: Optional[float] = None
    ask_depth_collapse: Optional[float] = None
    aggressive_bid_stepping: Optional[bool] = None
    liquidity_withdrawal: Optional[float] = None
    bullish_signals: Optional[int] = None
    bearish_signals: Optional[int] = None
    features: Optional[Dict] = None


class OrderbookTransitionTracker:
    """Tracks orderbook snapshots and computes transition scores."""
    
    WINDOW_15S = 60   # Widened from 15s to 60s (cycle times ~50s)
    WINDOW_30S = 120  # Widened from 30s to 120s
    
    def __init__(self):
        self._snapshots: deque = deque(maxlen=240)
        self._observation_count: int = 0
        self._score_history: list = []
        self._unique_scores: set = set()
        self.degenerate: bool = False
    
    def add_observation(self, score: float):
        """Track a score observation for degenerate detection."""
        self._observation_count += 1
        self._score_history.append(score)
        self._unique_scores.add(score)
        
        # Degenerate detection: after 100+ observations, <20 unique scores = degenerate
        if self._observation_count >= 100 and len(self._unique_scores) < 20:
            self.degenerate = True
    
    def get_score_distribution(self) -> dict:
        """Return distribution statistics for transition scores."""
        if not self._unique_scores:
            return {
                "count": 0,
                "unique_count": 0,
                "mean": 0.0,
                "p25": 0.0,
                "p50": 0.0,
                "p75": 0.0,
                "p95": 0.0,
            }
        
        sorted_scores = sorted(self._unique_scores)
        unique_sorted = sorted(self._unique_scores)
        
        # Compute percentiles
        n = len(unique_sorted)
        def _percentile(scores, p):
            if not scores:
                return 0.0
            k = (len(scores) - 1) * p / 100.0
            f = int(k)
            c = f + 1 if f + 1 < len(scores) else f
            return scores[c] + (scores[f] - scores[c]) * (k - f)
        
        return {
            "count": len(self._score_history),
            "unique_count": len(unique_sorted),
            "mean": sum(self._score_history) / len(self._score_history) if self._score_history else 0.0,
            "p25": _percentile(unique_sorted, 25) if unique_sorted else 0.0,
            "p50": _percentile(unique_sorted, 50) if unique_sorted else 0.0,
            "p75": _percentile(unique_sorted, 75) if unique_sorted else 0.0,
            "p95": _percentile(unique_sorted, 95) if unique_sorted else 0.0,
        }
    
    def _get_snapshot_at(self, target_time: float):
        """Get the snapshot closest to target_time (must be within 30s)."""
        best = None
        best_diff = float('inf')
        for snap in self._snapshots:
            diff = abs(snap.timestamp - target_time)
            if diff < best_diff:
                best_diff = diff
                best = snap
            if snap.timestamp > target_time:
                break
        if best and best_diff < 30.0:
            return best
        return None
    
    def _neutral_result(self, reason: str) -> TransitionResult:
        return TransitionResult(
            transition_score=0.0,
            bid_velocity_15s=0.0,
            ask_velocity_15s=0.0,
            spread_delta_15s=0.0,
            imbalance_delta_15s=0.0,
            token_price_velocity_15s=0.0,
            spot_velocity_15s=0.0,
            bid_strengthening=None,
            ask_weakening=None,
            spread_compressing=None,
            bid_depth_acceleration=None,
            ask_depth_collapse=None,
            aggressive_bid_stepping=None,
            liquidity_withdrawal=None,
            bullish_signals=None,
            bearish_signals=None,
            features={"reason": reason},
        )

    def add_snapshot(self, snapshot):
        self._snapshots.append(snapshot)
    
    def compute_transition(self) -> TransitionResult:
        if len(self._snapshots) < 2:
            return self._neutral_result("Insufficient snapshots")
        
        # Get reference snapshot (oldest in window) and current (newest)
        snap_now = self._snapshots[-1]
        
        window_size = min(32, 240)  # Use last 32 snapshots (~min 50-100s coverage)
        if len(self._snapshots) < window_size:
            window_size = len(self._snapshots)
        snap_ref = self._snapshots[-window_size]
        
        ref = {
            "bid_depth": snap_ref.bid_depth,
            "ask_depth": snap_ref.ask_depth,
            "spread": snap_ref.spread,
            "imbalance": snap_ref.imbalance,
            "up_price": snap_ref.up_price,
            "down_price": snap_ref.down_price,
            "spot": snap_ref.spot_reference if hasattr(snap_ref, 'spot_reference') else 0.0,
        }
        now = {
            "bid_depth": snap_now.bid_depth,
            "ask_depth": snap_now.ask_depth,
            "spread": snap_now.spread,
            "imbalance": snap_now.imbalance,
            "up_price": snap_now.up_price,
            "down_price": snap_now.down_price,
            "spot": snap_now.spot_reference if hasattr(snap_now, 'spot_reference') else 0.0,
        }
        
        # Compute velocity components
        bid_delta = now["bid_depth"] - ref["bid_depth"]
        ask_delta = now["ask_depth"] - ref["ask_depth"]
        spread_delta = now["spread"] - ref["spread"]
        imbalance_delta = now["imbalance"] - ref["imbalance"]
        delta_time = now["timestamp"] - ref["timestamp"] if hasattr(ref, "timestamp") and hasattr(now, "timestamp") else max(1.0, (snap_now.timestamp - snap_ref.timestamp))
        
        # Normalize velocities to comparable scales
        scale_bid = (ref["bid_depth"] or 1.0) * 1.0 / 8.0
        scale_ask = (ref["ask_depth"] or 1.0) * 1.0 / 8.0
        vel_bid = bid_delta / delta_time / scale_bid
        vel_ask = ask_delta / delta_time / scale_ask
        
        # Spread compression: negative spread_delta = compression
        compress_spread = spread_delta < -0.5
        
        # Normalize imbalance delta: |delta| / (2 * spread)
        norm_imbalance_delta = abs(imbalance_delta) / (2.0 * (now["spread"] if now["spread"] > 0 else 1.0))
        
        # Rough price velocity (if prices available)
        price_vel = 0.0
        if ref["up_price"] and now["up_price"]:
            price_vel = (now["up_price"] - ref["up_price"]) / max(delta_time, 0.01)
        
        # Degenerate check
        if not self.degenerate and self._observation_count > 50 and len(self._unique_scores) < 20:
            self.degenerate = True
        
        # Compute transition score
        score = 0.0
        
        # Bid velocity component (bullish: positive)
        score += vel_bid * delta_time * 5.0
        
        # Ask velocity component (bearish: negative)
        score += vel_ask * delta_time * 5.0
        
        # Spread compression component (bullish: negative delta = compression)
        if compress_spread:
            score += 0.15
        
        # Imbalance delta component
        score += imbalance_delta * 0.5
        
        price_vel_scaled = price_vel / (now["up_price"] if now["up_price"] else 1.0)
        score += price_vel_scaled * delta_time * 0.25
        
        # Normalize to [-1, 1]
        clamped_score = max(-1.0, min(1.0, score))
        
        # Add observation tracking
        self.add_observation(clamped_score)
        
        # Build result
        return TransitionResult(
            transition_score=clamped_score,
            bid_velocity_15s=vel_bid * delta_time,
            ask_velocity_15s=vel_ask * delta_time,
            spread_delta_15s=spread_delta,
            imbalance_delta_15s=imbalance_delta,
            token_price_velocity_15s=price_vel_scaled * delta_time,
            spot_velocity_15s=0.0,  # Would need to compute against spot reference
            bid_strengthening=vel_bid > 0.01,
            ask_weakening=vel_ask < -0.01,
            spread_compressing=compress_spread,
            bid_depth_acceleration=bid_delta,
            ask_depth_collapse=ask_delta,
            aggressive_bid_stepping=vel_bid > 0.05,
            liquidity_withdrawal=min(1.0, -vel_ask * delta_time) if vel_ask < 0 else 0.0,
            bullish_signals=1 if score > 0 else 0,
            bearish_signals=1 if score < 0 else 0,
            features={"score_raw": score, "obs_count": self._observation_count},
        )

def compute_transition_score(bid_depth: float, ask_depth: float, spread: float, \
                             imbalance: float, up_price: float, down_price: float, \
                             up_velocity: float = 0.0, down_velocity: float = 0.0, \
                             tracker: Optional[OrderbookTransitionTracker] = None) -> TransitionResult:
    """Convenience function to compute transition score."""
    snapshot = TransitionSnapshot(
        timestamp=time.time(),
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        spread=spread,
        imbalance=imbalance,
        up_price=up_price,
        down_price=down_price,
    )
    
    if tracker is not None:
        tracker.add_snapshot(snapshot)
        return tracker.compute_transition()
    
    return tracker and _neutral_result("Single-point fallback", score=0.0) or TransitionResult(
        transition_score=0.0
    )