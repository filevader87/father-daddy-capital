"""V21.5 Opportunity Scoring Engine — Soft Probabilistic Ranking
================================================================
Every market receives a weighted opportunity score. No hard gates.
Components: directional (25%), momentum (20%), lag (15%), volatility (10%),
time-to-expiry (10%), execution (10%), cross-asset (5%), RSI (5%).
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class OpportunityScore:
    """Weighted opportunity score for a single market+direction candidate."""
    market_slug: str
    asset: str
    interval: str
    direction: str  # UP or DOWN
    time_to_expiry: float  # seconds

    # Component scores (0.0–1.0)
    directional_score: float = 0.0    # 25% weight
    momentum_score: float = 0.0       # 20% weight
    lag_score: float = 0.0             # 15% weight
    volatility_score: float = 0.0      # 10% weight
    tte_score: float = 0.0            # 10% weight
    execution_score: float = 0.0      # 10% weight
    cross_asset_score: float = 0.0     # 5% weight
    rsi_context_score: float = 0.0    # 5% weight

    # Raw inputs
    estimated_probability: float = 0.5
    entry_price: float = 0.0
    spread: float = 0.0
    slippage_estimate: float = 0.0
    adversarial_score: float = 0.0

    # Derived
    credible_ev: float = 0.0
    ranking_position: int = 0
    profile_id: str = ""
    cell_id: str = ""
    execution_decision: str = "SKIP"  # SKIP, QUEUE, EXECUTE

    # Weights
    WEIGHTS = {
        'directional': 0.25,
        'momentum': 0.20,
        'lag': 0.15,
        'volatility': 0.10,
        'tte': 0.10,
        'execution': 0.10,
        'cross_asset': 0.05,
        'rsi_context': 0.05,
    }

    @property
    def composite_score(self) -> float:
        """Weighted opportunity score (0.0–1.0)."""
        return (
            self.directional_score * self.WEIGHTS['directional'] +
            self.momentum_score * self.WEIGHTS['momentum'] +
            self.lag_score * self.WEIGHTS['lag'] +
            self.volatility_score * self.WEIGHTS['volatility'] +
            self.tte_score * self.WEIGHTS['tte'] +
            self.execution_score * self.WEIGHTS['execution'] +
            self.cross_asset_score * self.WEIGHTS['cross_asset'] +
            self.rsi_context_score * self.WEIGHTS['rsi_context']
        )

    def compute_ev(self) -> float:
        """Compute credible EV from probability vs executable price."""
        if self.entry_price <= 0 or self.entry_price >= 1.0:
            self.credible_ev = 0.0
            return 0.0
        raw_ev = self.estimated_probability - self.entry_price
        cost = self.spread + self.slippage_estimate
        self.credible_ev = max(0.0, (raw_ev - cost) * (1.0 - self.adversarial_score))
        return self.credible_ev


class OpportunityRanker:
    """Scores and ranks all market opportunities every scan cycle.

    V21.5 philosophy: score almost everything, reject very little,
    rank aggressively, execute selectively.
    """

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.min_composite_score = self.config.get('min_composite_score', 0.15)
        self.min_ev = self.config.get('min_ev', 0.01)
        self.adversarial_kill_threshold = self.config.get('adversarial_kill', 0.80)
        self.adversarial_halve_threshold = self.config.get('adversarial_halve', 0.60)

    def score_directional(self, spot_delta: float, direction: str,
                         continuation_stats: dict | None = None) -> float:
        """Directional persistence score (25% weight).

        Uses spot price movement and continuation statistics.
        No hard rejection — soft contribution.
        """
        base = 0.0

        # Spot delta contribution
        if direction == "UP":
            base = max(0.0, min(1.0, spot_delta * 500))
        else:  # DOWN
            base = max(0.0, min(1.0, -spot_delta * 500))

        # Continuation stats boost if available
        if continuation_stats:
            wins = continuation_stats.get('wins', 0)
            total = continuation_stats.get('total', 0)
            if total >= 3:
                wr = wins / total
                # Blend observed WR with spot signal
                base = 0.6 * base + 0.4 * wr

        return base

    def score_momentum(self, price_velocity: float, volume_trend: float = 0.0) -> float:
        """Momentum score (20% weight).
        Price velocity + volume trend.
        """
        velocity_score = max(0.0, min(1.0, abs(price_velocity) * 200))
        volume_score = max(0.0, min(1.0, volume_trend * 10))
        return 0.7 * velocity_score + 0.3 * volume_score

    def score_lag(self, oracle_lag: float) -> float:
        """Repricing lag score (15% weight).
        Higher lag = higher opportunity.
        """
        if oracle_lag < 0:
            oracle_lag = abs(oracle_lag)
        # 0.05 lag = 0.5, 0.10 lag = 1.0
        return min(1.0, oracle_lag * 10)

    def score_volatility(self, recent_volatility: float,
                         avg_volatility: float = 0.01) -> float:
        """Volatility expansion score (10% weight).
        Recent vol exceeding average = expansion = opportunity.
        """
        if avg_volatility <= 0:
            return 0.3  # neutral
        ratio = recent_volatility / avg_volatility
        # ratio < 1 = contraction, 1-2 = normal, >2 = expansion
        if ratio < 0.5:
            return 0.1
        elif ratio < 1.0:
            return 0.3
        elif ratio < 2.0:
            return 0.6
        elif ratio < 3.0:
            return 0.85
        else:
            return 1.0

    def score_tte(self, time_to_expiry: float, interval: str) -> float:
        """Time-to-expiry acceleration score (10% weight).
        Priority increases mid-to-late market. Decreases early.
        """
        # Convert interval to seconds
        interval_secs = 300 if interval == "5m" else 900
        pct_elapsed = 1.0 - (time_to_expiry / interval_secs)

        if pct_elapsed < 0.20:
            # First 20% — least information, low priority
            return 0.1
        elif pct_elapsed < 0.40:
            # 20-40% — structure formation window
            return 0.4
        elif pct_elapsed < 0.80:
            # 40-80% — momentum exploitation window
            return 0.75
        elif pct_elapsed < 0.90:
            # 80-90% — late window, repricing lag exploitation
            return 0.95
        else:
            # Final 10% — maximum lag, but execution risk increases
            return 0.85

    def score_execution(self, effective_spread: float,
                        fill_probability: float = 1.0) -> float:
        """Execution quality score (10% weight).
        Lower spread + higher fill probability = better execution.
        """
        # Spread contribution (inverted — lower is better)
        if effective_spread <= 0.05:
            spread_score = 1.0
        elif effective_spread <= 0.10:
            spread_score = 0.8
        elif effective_spread <= 0.20:
            spread_score = 0.5
        elif effective_spread <= 0.50:
            spread_score = 0.2
        else:
            spread_score = 0.05  # Not zero — still contributes

        return spread_score * fill_probability

    def score_cross_asset(self, asset: str,
                          cross_asset_deltas: dict | None = None) -> float:
        """Cross-asset confirmation score (5% weight).
        If BTC moves UP and ETH also moves UP, confirmation is higher.
        """
        if not cross_asset_deltas:
            return 0.3  # neutral without data

        direction = cross_asset_deltas.get('direction', 'NONE')
        confirming = sum(1 for a, d in cross_asset_deltas.get('assets', {}).items()
                        if a != asset and d == direction)
        total_other = len(cross_asset_deltas.get('assets', {})) - 1  # exclude self

        if total_other <= 0:
            return 0.3

        return min(1.0, (confirming / total_other) * 0.8 + 0.2)

    def score_rsi_context(self, rsi: float, direction: str,
                          direction_stats: dict | None = None) -> float:
        """RSI context contribution (5% weight).
        RSI is context only, NOT primary authority.
        """
        base = 0.5  # neutral

        if direction == "UP":
            if rsi < 25:
                base = 0.85  # deep oversold, UP continuation likely
            elif rsi < 35:
                base = 0.7   # oversold context
            elif rsi < 45:
                base = 0.55  # slight oversold
            elif rsi < 55:
                base = 0.5   # neutral
            elif rsi < 65:
                base = 0.45  # slight overbought
            elif rsi < 75:
                base = 0.35  # overbought — UP less likely but not rejected
            else:
                base = 0.25  # deep overbought — UP continuation still possible
        else:  # DOWN
            if rsi > 75:
                base = 0.85  # deep overbought, DOWN continuation likely
            elif rsi > 65:
                base = 0.7
            elif rsi > 55:
                base = 0.55
            elif rsi > 45:
                base = 0.5
            elif rsi > 35:
                base = 0.45
            elif rsi > 25:
                base = 0.35
            else:
                base = 0.25  # deep oversold — DOWN still possible

        # Blend with observed direction stats if available
        if direction_stats:
            wins = direction_stats.get('wins', 0)
            total = direction_stats.get('total', 0)
            if total >= 3:
                observed_wr = wins / total
                base = 0.5 * base + 0.5 * observed_wr

        return base

    def rank_opportunities(self, candidates: list[OpportunityScore]) -> tuple[list[OpportunityScore], list[OpportunityScore]]:
        """Rank all candidates by composite score, filter by minimum thresholds.

        V21.5: Score almost everything, reject very little,
        rank aggressively, execute selectively.
        """
        # Compute EV for each
        for c in candidates:
            c.compute_ev()

        # Soft adversarial filter — not hard rejection
        for c in candidates:
            if c.adversarial_score >= self.adversarial_kill_threshold:
                c.credible_ev = 0.0
                c.execution_decision = "SKIP"
                c.composite_score  # ensure computed
            elif c.adversarial_score >= self.adversarial_halve_threshold:
                c.credible_ev *= 0.5

        # Sort by composite score descending
        ranked = sorted(candidates, key=lambda c: c.composite_score, reverse=True)

        # Assign ranking positions
        for i, c in enumerate(ranked):
            c.ranking_position = i + 1

        # Decision logic — soft thresholds
        executable = []
        for c in ranked:
            if c.adversarial_score >= self.adversarial_kill_threshold:
                c.execution_decision = "SKIP"
                continue
            if c.composite_score < self.min_composite_score:
                c.execution_decision = "SKIP"
                continue
            if c.credible_ev <= 0 and c.composite_score < 0.30:
                c.execution_decision = "SKIP"
                continue

            # If we have positive EV and decent composite, queue for execution
            if c.credible_ev > self.min_ev:
                c.execution_decision = "EXECUTE"
                executable.append(c)
            elif c.composite_score >= 0.30:
                # High composite but low EV — still queue as opportunity
                c.execution_decision = "QUEUE"
                executable.append(c)
            else:
                c.execution_decision = "SKIP"

        return ranked, executable