"""Directional Asymmetry Engine — §5 V21
==========================================
Explicitly models continuation > reversal unless data proves otherwise.
Tracks directional hypotheses across RSI × Direction contexts.
Data decides. No reversal assumptions.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from enum import Enum
import math
import time


class RSIZone(str, Enum):
    LOW = "low"       # RSI < 35
    MID = "mid"       # 35 <= RSI <= 65
    HIGH = "high"     # RSI > 65


class DirectionContext(str, Enum):
    """Full directional context — no reversal assumptions."""
    # RSI < 35
    LOW_UP_CONTINUATION = "low_up_continuation"
    LOW_DOWN_CONTINUATION = "low_down_continuation"
    LOW_UP_REVERSAL = "low_up_reversal"
    LOW_DOWN_EXHAUSTION = "low_down_exhaustion"
    # RSI > 65
    HIGH_UP_CONTINUATION = "high_up_continuation"
    HIGH_DOWN_REVERSAL = "high_down_reversal"
    HIGH_DOWN_CONTINUATION = "high_down_continuation"
    HIGH_UP_EXHAUSTION = "high_up_exhaustion"
    # RSI mid
    MID_CONTINUATION = "mid_continuation"
    MID_REVERSAL = "mid_reversal"


@dataclass
class DirectionObservation:
    """A single directional observation with context."""
    context: DirectionContext
    rsi: float
    spot_move_pct: float
    direction: str   # "UP" or "DOWN"
    correct: bool     # Did the direction match outcome?
    pnl: float
    timestamp: float


@dataclass
class DirectionStats:
    """Statistics for a specific RSI × Direction context."""
    total: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0
    ev_per_dollar: float = 0.0
    half_life_trades: float = 0.0
    last_updated: float = 0.0
    
    # Continuation vs reversal breakdown
    continuation_wins: int = 0
    continuation_total: int = 0
    reversal_wins: int = 0
    reversal_total: int = 0


class DirectionalAsymmetryEngine:
    """V21 §5 — Directional persistence > reversal, data decides.
    
    Continuation > reversal is the DEFAULT hypothesis.
    Reversal must be EARNED through verified data.
    
    The engine separately tracks every RSI × Direction context
    and resolves which contexts actually have edge.
    """
    
    def __init__(self):
        self.context_stats: Dict[str, DirectionStats] = defaultdict(DirectionStats)
        self.observations: List[DirectionObservation] = []
        self.max_history = 5000
        
        # Prior: continuation bias
        self.continuation_prior_alpha = 3.0  # pseudo-counts favoring continuation
        self.continuation_prior_beta = 1.0   # pseudo-counts against
    
    def classify_rsi(self, rsi: float) -> RSIZone:
        """Classify RSI into zone."""
        if rsi < 35:
            return RSIZone.LOW
        elif rsi > 65:
            return RSIZone.HIGH
        else:
            return RSIZone.MID
    
    def classify_context(self, rsi: float, spot_move_direction: str, 
                         prev_spot_move_direction: Optional[str] = None) -> DirectionContext:
        """Classify into full directional context. §5 explicit tracking."""
        zone = self.classify_rsi(rsi)
        
        continuation = (spot_move_direction == prev_spot_move_direction) if prev_spot_move_direction else True
        
        if zone == RSIZone.LOW:
            if spot_move_direction == "UP":
                return DirectionContext.LOW_UP_CONTINUATION if continuation else DirectionContext.LOW_UP_REVERSAL
            else:  # DOWN
                return DirectionContext.LOW_DOWN_CONTINUATION if continuation else DirectionContext.LOW_DOWN_EXHAUSTION
        
        elif zone == RSIZone.HIGH:
            if spot_move_direction == "UP":
                return DirectionContext.HIGH_UP_CONTINUATION if continuation else DirectionContext.HIGH_UP_EXHAUSTION
            else:  # DOWN
                return DirectionContext.HIGH_DOWN_CONTINUATION if continuation else DirectionContext.HIGH_DOWN_REVERSAL
        
        else:  # MID
            return DirectionContext.MID_CONTINUATION if continuation else DirectionContext.MID_REVERSAL
    
    def record_observation(self, observation: DirectionObservation):
        """Record a directional observation with context."""
        ctx_key = observation.context.value
        stats = self.context_stats[ctx_key]
        
        stats.total += 1
        if observation.correct:
            stats.wins += 1
        else:
            stats.losses += 1
        stats.total_pnl += observation.pnl
        stats.win_rate = stats.wins / stats.total if stats.total > 0 else 0
        stats.ev_per_dollar = stats.total_pnl / stats.total if stats.total > 0 else 0
        stats.last_updated = time.time()
        
        # Track continuation vs reversal
        is_continuation = "continuation" in observation.context.value
        if is_continuation:
            stats.continuation_total += 1
            if observation.correct:
                stats.continuation_wins += 1
        else:
            stats.reversal_total += 1
            if observation.correct:
                stats.reversal_wins += 1
        
        # Half-life estimation
        if stats.total >= 10:
            recent_10_wr = sum(1 for o in self.observations[-10:] 
                            if o.context == observation.context and o.correct) / min(10, stats.total)
            stats.half_life_trades = 50 / max(0.01, abs(recent_10_wr - 0.5))
        
        self.observations.append(observation)
        if len(self.observations) > self.max_history:
            self.observations = self.observations[-self.max_history:]
    
    def get_directional_probability(self, context: DirectionContext) -> float:
        """Bayesian estimate of directional probability with continuation prior.
        
        Prior: continuation > reversal (3:1 ratio).
        Data updates this prior.
        """
        ctx_key = context.value
        stats = self.context_stats.get(ctx_key)
        
        alpha = self.continuation_prior_alpha
        beta = self.continuation_prior_beta
        
        # Is this a continuation context?
        is_continuation = "continuation" in context.value
        
        if is_continuation:
            alpha += stats.wins if stats else 0
            beta += stats.losses if stats else 0
        else:
            # Reversal context: weaker prior (1:1)
            alpha = 1.0 + (stats.wins if stats else 0)
            beta = 1.0 + (stats.losses if stats else 0)
        
        return alpha / (alpha + beta)
    
    def get_best_direction(self, asset: str, interval: str, rsi: float,
                           spot_move: str, prev_spot_move: Optional[str] = None) -> Tuple[str, float]:
        """Return best direction and its probability for given context.
        
        Compares continuation vs reversal in current RSI/momentum context.
        Returns (direction, estimated_probability).
        """
        zone = self.classify_rsi(rsi)
        
        # Build both continuation and reversal contexts
        cont_context = self.classify_context(rsi, spot_move, prev_spot_move)
        # Flip direction for reversal
        rev_direction = "DOWN" if spot_move == "UP" else "UP"
        rev_context = self.classify_context(rsi, rev_direction, prev_spot_move)
        
        cont_prob = self.get_directional_probability(cont_context)
        rev_prob = self.get_directional_probability(rev_context)
        
        # Continuation gets structural boost (§5: continuation > reversal by default)
        cont_prob *= 1.05  # Small structural prior for continuation
        
        if cont_prob >= rev_prob:
            return spot_move, cont_prob
        else:
            return rev_direction, rev_prob
    
    def get_continuation_vs_reversal_stats(self) -> Dict[str, Dict]:
        """Summary stats for all contexts showing continuation vs reversal performance."""
        results = {}
        for ctx_key, stats in self.context_stats.items():
            results[ctx_key] = {
                "total": stats.total,
                "win_rate": stats.win_rate,
                "ev_per_dollar": stats.ev_per_dollar,
                "continuation_total": stats.continuation_total,
                "continuation_wr": stats.continuation_wins / max(1, stats.continuation_total),
                "reversal_total": stats.reversal_total,
                "reversal_wr": stats.reversal_wins / max(1, stats.reversal_total),
                "continuation_dominant": stats.continuation_total > stats.reversal_total,
            }
        return results
    
    def is_verified_reversal(self, context: DirectionContext, min_trades: int = 10) -> bool:
        """Check if a reversal context has been verified by data.
        
        Reversal must EARN the right to be traded:
        - Minimum trades
        - Win rate > 55%
        - EV > 0
        """
        stats = self.context_stats.get(context.value)
        if not stats or stats.total < min_trades:
            return False
        return stats.win_rate > 0.55 and stats.ev_per_dollar > 0