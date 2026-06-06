"""V21.5 Entry Timing Engine — §4, §12
=========================================
Observes structure formation, waits for asymmetry emergence,
enters AFTER directional commitment is revealed.
Primary windows: 20-40% (structure), 40-80% (momentum), final 60-120s (lag).
"""

from __future__ import annotations
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class EntryWindow(Enum):
    """Market lifecycle phases with different entry priority."""
    EARLY = "early"          # 0-20% elapsed — LEAST information, low priority
    FORMATION = "formation"  # 20-40% — structure forming, moderate priority
    MOMENTUM = "momentum"   # 40-80% — directional commitment revealed, HIGH priority
    LATE = "late"           # 80-90% — repricing lag exploitation, highest priority
    FINAL = "final"         # 90-100% — execution risk rises, high but declining


@dataclass
class TimingAssessment:
    """Full timing assessment for a market at current moment."""
    window: EntryWindow
    pct_elapsed: float           # 0.0–1.0 how far into the market lifecycle
    time_to_expiry: float        # seconds remaining
    structure_score: float       # 0.0–1.0 how much directional structure has formed
    momentum_acceleration: float # 0.0–1.0 is momentum accelerating or fading
    entry_priority: float       # 0.0–1.0 overall timing priority
    should_enter: bool           # composite timing decision
    reason: str                  # human-readable explanation

    @property
    def window_name(self) -> str:
        return self.window.value


class EntryTimingEngine:
    """Determines optimal entry timing based on market lifecycle position.

    V21.5 philosophy: the beginning contains the least information.
    The edge develops after participants reveal directional commitment.

    Priority curve:
    - First 20%: minimal priority (least structure)
    - 20-40%: rising priority (structure formation)
    - 40-80%: high priority (momentum exploitation)
    - 80-90%: peak priority (repricing lag exploitation)
    - Final 10%: declining priority (execution risk dominates)
    """

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        # Minimum structure score required for early entry
        self.min_structure_early = self.config.get('min_structure_early', 0.3)
        # Minimum entry priority to proceed
        self.min_entry_priority = self.config.get('min_entry_priority', 0.15)

    def classify_window(self, time_to_expiry: float,
                        interval: str) -> EntryWindow:
        """Classify which entry window the market is currently in."""
        interval_secs = 300 if interval == "5m" else 900
        pct_elapsed = 1.0 - (time_to_expiry / interval_secs)

        if pct_elapsed < 0.20:
            return EntryWindow.EARLY
        elif pct_elapsed < 0.40:
            return EntryWindow.FORMATION
        elif pct_elapsed < 0.80:
            return EntryWindow.MOMENTUM
        elif pct_elapsed < 0.90:
            return EntryWindow.LATE
        else:
            return EntryWindow.FINAL

    def compute_structure_score(self, pct_elapsed: float,
                                 price_directional_delta: float,
                                 orderbook_asymmetry: float = 0.0,
                                 volume_commitment: float = 0.0) -> float:
        """How much directional structure has formed.

        Early markets have low structure. Structure builds as participants
        commit to directions.
        """
        # Time contribution — structure builds with time
        time_factor = min(1.0, pct_elapsed * 2.0)  # reaches 1.0 at 50% elapsed

        # Price directional commitment
        direction_factor = min(1.0, abs(price_directional_delta) * 500)

        # Orderbook asymmetry — if bids >> asks or vice versa, structure exists
        ob_factor = min(1.0, abs(orderbook_asymmetry) * 5.0)

        # Volume commitment — trades happening means commitment
        vol_factor = min(1.0, volume_commitment * 10.0)

        # Weighted blend
        score = (0.3 * time_factor +
                 0.3 * direction_factor +
                 0.2 * ob_factor +
                 0.2 * vol_factor)

        return min(1.0, max(0.0, score))

    def compute_momentum_acceleration(self, recent_deltas: list[float]) -> float:
        """Whether momentum is accelerating or fading.

        Accelerating momentum = higher entry priority.
        Fading momentum = lower priority (direction may be exhausted).
        """
        if len(recent_deltas) < 2:
            return 0.5  # neutral

        # Check if absolute deltas are increasing (acceleration)
        abs_deltas = [abs(d) for d in recent_deltas]
        accelerations = [abs_deltas[i] - abs_deltas[i-1]
                         for i in range(1, len(abs_deltas))]

        if not accelerations:
            return 0.5

        avg_accel = sum(accelerations) / len(accelerations)

        # Normalize: positive acceleration → higher score
        # Maps typical accelerations to 0-1 range
        score = 0.5 + min(0.5, max(-0.5, avg_accel * 5000))
        return score

    def compute_entry_priority(self, window: EntryWindow,
                                structure_score: float,
                                momentum_acceleration: float,
                                oracle_lag: float = 0.0,
                                adversarial_score: float = 0.0) -> float:
        """Composite entry priority combining all timing factors.

        Higher priority = better entry timing.
        """
        # Base priority from window
        window_priority = {
            EntryWindow.EARLY: 0.10,
            EntryWindow.FORMATION: 0.40,
            EntryWindow.MOMENTUM: 0.75,
            EntryWindow.LATE: 0.95,
            EntryWindow.FINAL: 0.70,  # execution risk rises
        }
        base = window_priority[window]

        # Boost from structure (high structure = more confidence)
        structure_boost = structure_score * 0.15

        # Boost from momentum acceleration
        momentum_boost = momentum_acceleration * 0.10

        # Boost from oracle lag (lag = opportunity)
        lag_boost = min(1.0, oracle_lag * 10) * 0.15

        # Reduce from adversarial conditions
        adversarial_penalty = adversarial_score * 0.30

        priority = base + structure_boost + momentum_boost + lag_boost - adversarial_penalty
        return max(0.0, min(1.0, priority))

    def assess(self, time_to_expiry: float, interval: str,
               price_directional_delta: float = 0.0,
               orderbook_asymmetry: float = 0.0,
               volume_commitment: float = 0.0,
               recent_deltas: list[float] | None = None,
               oracle_lag: float = 0.0,
               adversarial_score: float = 0.0) -> TimingAssessment:
        """Full timing assessment for a market.

        Returns TimingAssessment with window, scores, and entry decision.
        """
        interval_secs = 300 if interval == "5m" else 900
        pct_elapsed = 1.0 - (time_to_expiry / interval_secs)
        window = self.classify_window(time_to_expiry, interval)

        # Structure score
        structure = self.compute_structure_score(
            pct_elapsed, price_directional_delta,
            orderbook_asymmetry, volume_commitment
        )

        # Momentum acceleration
        momentum = self.compute_momentum_acceleration(
            recent_deltas if recent_deltas else []
        )

        # Entry priority
        priority = self.compute_entry_priority(
            window, structure, momentum,
            oracle_lag, adversarial_score
        )

        # Entry decision
        should_enter = priority >= self.min_entry_priority

        # Early window requires minimum structure
        if window == EntryWindow.EARLY and structure < self.min_structure_early:
            should_enter = False

        # Build reason string
        if window == EntryWindow.EARLY:
            reason = f"EARLY ({pct_elapsed:.0%} elapsed) — waiting for structure"
        elif window == EntryWindow.FORMATION:
            reason = f"FORMATION ({pct_elapsed:.0%} elapsed) — structure forming"
        elif window == EntryWindow.MOMENTUM:
            reason = f"MOMENTUM ({pct_elapsed:.0%} elapsed) — directional commitment"
        elif window == EntryWindow.LATE:
            reason = f"LATE ({pct_elapsed:.0%} elapsed) — repricing lag window"
        else:
            reason = f"FINAL ({pct_elapsed:.0%} elapsed) — execution risk zone"

        if not should_enter and priority < self.min_entry_priority:
            reason += f" [priority {priority:.2f} < {self.min_entry_priority:.2f}]"
        elif should_enter:
            reason += f" [ENTER priority={priority:.2f}]"

        return TimingAssessment(
            window=window,
            pct_elapsed=pct_elapsed,
            time_to_expiry=time_to_expiry,
            structure_score=structure,
            momentum_acceleration=momentum,
            entry_priority=priority,
            should_enter=should_enter,
            reason=reason,
        )