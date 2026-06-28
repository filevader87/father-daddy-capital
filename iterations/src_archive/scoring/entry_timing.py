"""V21.5 Entry Timing Engine — §4, §5, §12
=============================================
Observes structure formation, waits for asymmetry emergence,
enters AFTER directional commitment is revealed.
§5: Boost 40-80% elapsed and final 120s. Decrease first 20%.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class EntryWindow(Enum):
    EARLY = "EARLY"           # 0-20% elapsed — least info, avoid
    FORMATION = "FORMATION"   # 20-40% — structure forming
    MOMENTUM = "MOMENTUM"     # 40-80% — directional commitment, HIGH
    LATE = "LATE"             # 80-90% — repricing lag exploitation
    FINAL = "FINAL"           # 90-100% — execution risk rises


@dataclass
class TimingAssessment:
    """Entry timing assessment for a candidate."""
    window: EntryWindow
    window_name: str
    pct_elapsed: float
    entry_priority: float   # 0.0-1.0
    should_enter: bool
    reason: str
    time_to_expiry: float
    final_120s: bool = False
    no_movement_penalty: bool = False


class EntryTimingEngine:
    """§4: Enter AFTER structure reveals itself, not at creation.
    §5: Boost 40-80% and final 120s, decrease first 20%.
    """

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        # §5: Phase-specific priorities
        self.phase_priorities = {
            EntryWindow.EARLY: 0.10,       # §5: decreased — least info
            EntryWindow.FORMATION: 0.40,   # structure forming
            EntryWindow.MOMENTUM: 0.75,    # §5: increased — directional commitment
            EntryWindow.LATE: 0.95,        # repricing lag exploitation
            EntryWindow.FINAL: 0.70,       # execution risk rises
        }

    def assess(self, time_to_expiry: float, interval: str,
               price_directional_delta: float = 0.0,
               oracle_lag: float = 0.0,
               adversarial_score: float = 0.0,
               spot_velocity: float = 0.0,
               no_movement: bool = False) -> TimingAssessment:
        """Assess entry timing for a market candidate.

        Args:
            time_to_expiry: seconds until market resolution
            interval: '5m' or '15m'
            price_directional_delta: recent price change (%)
            oracle_lag: seconds of repricing delay
            adversarial_score: 0.0-1.0 adversarial risk
            spot_velocity: recent price velocity
            no_movement: True if market is stagnant/flat
        """
        interval_secs = 300 if interval == "5m" else 900
        total_time = interval_secs
        pct_elapsed = 1.0 - (time_to_expiry / total_time)
        pct_elapsed = max(0.0, min(1.0, pct_elapsed))

        # Determine window
        if pct_elapsed < 0.20:
            window = EntryWindow.EARLY
        elif pct_elapsed < 0.40:
            window = EntryWindow.FORMATION
        elif pct_elapsed < 0.80:
            window = EntryWindow.MOMENTUM
        elif pct_elapsed < 0.90:
            window = EntryWindow.LATE
        else:
            window = EntryWindow.FINAL

        # Base priority from phase
        priority = self.phase_priorities[window]

        # §5: Boost for final 120 seconds
        final_120s = False
        if time_to_expiry <= 120 and time_to_expiry > 0:
            priority = min(1.0, priority + 0.15)
            final_120s = True

        # §5: Decrease priority for no structure / no movement
        no_movement_penalty = False
        if no_movement or abs(price_directional_delta) < 0.0001:
            priority *= 0.5
            no_movement_penalty = True

        # Directional delta boost — more movement = more info
        if abs(price_directional_delta) > 0.001:
            priority = min(1.0, priority + 0.05)

        # Oracle lag boost — lag = opportunity
        if oracle_lag > 0.05:
            priority = min(1.0, priority + 0.10)

        # Spot velocity boost
        if abs(spot_velocity) > 0.0005:
            priority = min(1.0, priority + 0.05)

        # Adversarial penalty
        if adversarial_score > 0.60:
            priority *= 0.5
        if adversarial_score > 0.80:
            priority = 0.0

        # Build reason
        reasons = []
        if window == EntryWindow.EARLY:
            reasons.append("early_market_low_info")
        if final_120s:
            reasons.append("final_120s_boost")
        if no_movement_penalty:
            reasons.append("no_movement_penalty")
        if abs(price_directional_delta) > 0.001:
            reasons.append("directional_delta_boost")
        if oracle_lag > 0.05:
            reasons.append("oracle_lag_boost")
        if adversarial_score > 0.60:
            reasons.append("adversarial_penalty")

        # Should enter?
        should_enter = priority >= 0.30 and adversarial_score < 0.80

        return TimingAssessment(
            window=window,
            window_name=window.value,
            pct_elapsed=pct_elapsed,
            entry_priority=priority,
            should_enter=should_enter,
            reason="+".join(reasons) if reasons else "normal",
            time_to_expiry=time_to_expiry,
            final_120s=final_120s,
            no_movement_penalty=no_movement_penalty,
        )