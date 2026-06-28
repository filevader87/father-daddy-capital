"""V21.5 Opportunity Scoring Engine — Soft Probabilistic Ranking
================================================================
Every market receives a weighted opportunity score. No hard gates.
Oracle lag is ONE component, not a gate.
Both UP and DOWN scored independently — score decides side.
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
    lag_score: float = 0.0             # 10% weight (reduced from 15% — not a gate)
    volatility_score: float = 0.0      # 10% weight
    tte_score: float = 0.0            # 10% weight
    execution_score: float = 0.0      # 10% weight
    cross_asset_score: float = 0.0     # 10% weight (increased from 5%)
    rsi_context_score: float = 0.0    # 5% weight

    # Side selection
    relative_side_advantage: float = 0.0  # UP_score - DOWN_score for this market
    is_top_side: bool = False              # True if this is the higher-scored side

    # Directional persistence (§6)
    spot_velocity_15s: float = 0.0
    spot_velocity_30s: float = 0.0
    spot_velocity_60s: float = 0.0
    candle_direction: str = "NEUTRAL"  # UP, DOWN, NEUTRAL
    consecutive_directional_moves: int = 0
    distance_from_reference: float = 0.0
    price_approach: str = "NEUTRAL"  # TOWARD, AWAY, NEUTRAL

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
    execution_decision: str = "SKIP"  # SKIP, QUEUE, EXECUTE, FORCED_TOP_RANKED_PAPER

    # Weights — oracle lag reduced, cross-asset increased
    WEIGHTS = {
        'directional': 0.25,
        'momentum': 0.20,
        'lag': 0.10,
        'volatility': 0.10,
        'tte': 0.10,
        'execution': 0.10,
        'cross_asset': 0.10,
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

    def to_dict(self) -> dict:
        """Serialize for JSONL output."""
        return {
            'slug': self.market_slug,
            'asset': self.asset,
            'interval': self.interval,
            'direction': self.direction,
            'tte': round(self.time_to_expiry, 1),
            'composite': round(self.composite_score, 4),
            'ev': round(self.credible_ev, 6),
            'dir_score': round(self.directional_score, 4),
            'mom_score': round(self.momentum_score, 4),
            'lag_score': round(self.lag_score, 4),
            'vol_score': round(self.volatility_score, 4),
            'tte_score': round(self.tte_score, 4),
            'exec_score': round(self.execution_score, 4),
            'cross_score': round(self.cross_asset_score, 4),
            'rsi_score': round(self.rsi_context_score, 4),
            'side_adv': round(self.relative_side_advantage, 4),
            'is_top_side': self.is_top_side,
            'entry_price': round(self.entry_price, 4),
            'spread': round(self.spread, 4),
            'adv_score': round(self.adversarial_score, 4),
            'rank': self.ranking_position,
            'decision': self.execution_decision,
            'spot_vel_15s': round(self.spot_velocity_15s, 6),
            'spot_vel_30s': round(self.spot_velocity_30s, 6),
            'spot_vel_60s': round(self.spot_velocity_60s, 6),
            'candle_dir': self.candle_direction,
            'consec_moves': self.consecutive_directional_moves,
            'dist_from_ref': round(self.distance_from_reference, 6),
            'price_approach': self.price_approach,
            'profile': self.profile_id,
        }


class OpportunityRanker:
    """Scores and ranks all market opportunities every scan cycle.

    V21.5 philosophy: score almost everything, reject very little,
    rank aggressively, execute selectively.
    Side selection: score decides, no directional ideology.
    """

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.min_composite_score = self.config.get('min_composite_score', 0.15)
        self.min_ev = self.config.get('min_ev', 0.01)
        self.adversarial_kill_threshold = self.config.get('adversarial_kill', 0.80)
        self.adversarial_halve_threshold = self.config.get('adversarial_halve', 0.60)

        # Track per-market side winners for side selection (§7)
        self._market_best_side: dict[str, tuple[str, float]] = {}  # slug → (direction, score)

    def score_directional(self, spot_delta: float, direction: str,
                          continuation_stats: dict | None = None,
                          persistence: dict | None = None) -> float:
        """Directional persistence score (25% weight).

        Uses spot velocity, candle direction, consecutive moves,
        distance from reference, price approach — not just spot delta.
        No hard rejection — soft contribution.
        """
        base = 0.0

        # Spot delta contribution (weaker, blended)
        if direction == "UP":
            delta_signal = max(0.0, min(1.0, spot_delta * 500))
        else:  # DOWN
            delta_signal = max(0.0, min(1.0, -spot_delta * 500))

        # Directional persistence contribution (§6)
        persistence_signal = 0.0
        if persistence:
            # Candle direction alignment
            candle_dir = persistence.get('candle_direction', 'NEUTRAL')
            aligns = (direction == "UP" and candle_dir == "UP") or \
                      (direction == "DOWN" and candle_dir == "DOWN") or \
                      (candle_dir == "NEUTRAL")
            candle_score = 0.7 if aligns else 0.2

            # Consecutive directional moves (exponential scoring)
            consec = persistence.get('consecutive_moves', 0)
            consec_score = min(1.0, consec * 0.15 + 0.1)

            # Velocity at multiple horizons
            vel_15 = abs(persistence.get('velocity_15s', 0.0))
            vel_30 = abs(persistence.get('velocity_30s', 0.0))
            vel_60 = abs(persistence.get('velocity_60s', 0.0))
            # If velocity aligns with direction
            raw_vel = persistence.get('velocity_30s', 0.0)
            if direction == "UP":
                vel_align = max(0.0, min(1.0, raw_vel * 500))
            else:
                vel_align = max(0.0, min(1.0, -raw_vel * 500))
            vel_score = 0.4 * vel_align + 0.3 * min(1.0, (vel_15 + vel_30 + vel_60) * 300) + 0.3

            # Price approach (toward reference = continuation, away = reversal attempt)
            approach = persistence.get('price_approach', 'NEUTRAL')
            approach_score = 0.7 if approach == 'TOWARD' else (0.3 if approach == 'AWAY' else 0.5)

            # Distance from reference (further = stronger direction)
            dist = abs(persistence.get('distance_from_reference', 0.0))
            dist_score = min(1.0, dist * 500 + 0.2)

            persistence_signal = (0.25 * candle_score +
                                 0.20 * consec_score +
                                 0.25 * vel_score +
                                 0.15 * approach_score +
                                 0.15 * dist_score)

        # Blend: 40% delta, 60% persistence (if available)
        if persistence:
            base = 0.4 * delta_signal + 0.6 * persistence_signal
        else:
            base = delta_signal

        # Continuation stats boost if available
        if continuation_stats:
            wins = continuation_stats.get('wins', 0)
            total = continuation_stats.get('total', 0)
            if total >= 3:
                wr = wins / total
                base = 0.6 * base + 0.4 * wr

        return max(0.0, min(1.0, base))

    def score_momentum(self, price_velocity: float, volume_trend: float = 0.0) -> float:
        """Momentum score (20% weight)."""
        velocity_score = max(0.0, min(1.0, abs(price_velocity) * 200))
        volume_score = max(0.0, min(1.0, volume_trend * 10))
        return 0.7 * velocity_score + 0.3 * volume_score

    def score_lag(self, oracle_lag: float) -> float:
        """Repricing lag score (10% weight — reduced, NOT a gate).
        Higher lag = higher opportunity. Zero lag = neutral, not zero.
        """
        if oracle_lag < 0:
            oracle_lag = abs(oracle_lag)
        # Zero lag = 0.2 (neutral, not 0 — oracle lag is ONE component)
        # 0.05 lag = 0.5, 0.10 lag = 1.0
        return 0.2 + min(0.8, oracle_lag * 8)

    def score_volatility(self, recent_volatility: float,
                         avg_volatility: float = 0.01) -> float:
        """Volatility expansion score (10% weight)."""
        if avg_volatility <= 0:
            return 0.3
        ratio = recent_volatility / avg_volatility
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

    def score_tte(self, time_to_expiry: float, interval: str,
                  pct_elapsed: float = 0.0) -> float:
        """Time-to-expiry with market phase weighting (10% weight).

        §5: Increase priority for 40-80% elapsed and final 120s.
        Decrease priority for first 20% and no-movement periods.
        """
        interval_secs = 300 if interval == "5m" else 900
        if pct_elapsed <= 0:
            pct_elapsed = 1.0 - (time_to_expiry / interval_secs)

        # Base TTE scoring
        if pct_elapsed < 0.20:
            base = 0.10  # §5: decreased priority for first 20%
        elif pct_elapsed < 0.40:
            base = 0.40  # structure formation
        elif pct_elapsed < 0.80:
            base = 0.75  # §5: increased priority for 40-80%
        elif pct_elapsed < 0.90:
            base = 0.95  # late window
        else:
            base = 0.85  # final 10%

        # §5: Boost for final 120 seconds
        if time_to_expiry <= 120 and time_to_expiry > 0:
            base = min(1.0, base + 0.15)

        return min(1.0, base)

    def score_execution(self, effective_spread: float,
                        fill_probability: float = 1.0) -> float:
        """Execution quality score (10% weight). Lower spread = better."""
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
        """Cross-asset confirmation score (10% weight — increased from 5%)."""
        if not cross_asset_deltas:
            return 0.3

        direction = cross_asset_deltas.get('direction', 'NONE')
        confirming = sum(1 for a, d in cross_asset_deltas.get('assets', {}).items()
                        if a != asset and d == direction)
        total_other = len(cross_asset_deltas.get('assets', {})) - 1

        if total_other <= 0:
            return 0.3

        return min(1.0, (confirming / total_other) * 0.8 + 0.2)

    def score_rsi_context(self, rsi: float, direction: str,
                           direction_stats: dict | None = None) -> float:
        """RSI context contribution (5% weight). Context only, NOT authority."""
        base = 0.5

        if direction == "UP":
            if rsi < 25:    base = 0.85
            elif rsi < 35:  base = 0.7
            elif rsi < 45:  base = 0.55
            elif rsi < 55:  base = 0.5
            elif rsi < 65:  base = 0.45
            elif rsi < 75:  base = 0.35
            else:           base = 0.25
        else:  # DOWN
            if rsi > 75:    base = 0.85
            elif rsi > 65:  base = 0.7
            elif rsi > 55:  base = 0.55
            elif rsi > 45:  base = 0.5
            elif rsi > 35:  base = 0.45
            elif rsi > 25:  base = 0.35
            else:           base = 0.25

        if direction_stats:
            wins = direction_stats.get('wins', 0)
            total = direction_stats.get('total', 0)
            if total >= 3:
                observed_wr = wins / total
                base = 0.5 * base + 0.5 * observed_wr

        return base

    def select_side(self, candidates: list[OpportunityScore]) -> list[OpportunityScore]:
        """§7: Side selection — let score decide, no ideology.

        For each market, mark which direction has the higher composite score.
        """
        # Group by market slug
        by_market: dict[str, list[OpportunityScore]] = {}
        for c in candidates:
            slug_key = f"{c.asset}-{c.interval}-{c.time_to_expiry:.0f}"
            if slug_key not in by_market:
                by_market[slug_key] = []
            by_market[slug_key].append(c)

        # For each market, identify the top side
        for slug_key, sides in by_market.items():
            if len(sides) < 2:
                # Only one side available
                for c in sides:
                    c.is_top_side = True
                    c.relative_side_advantage = c.composite_score
                continue

            up_score = 0.0
            down_score = 0.0
            for c in sides:
                if c.direction == "UP":
                    up_score = c.composite_score
                else:
                    down_score = c.composite_score

            for c in sides:
                if c.direction == "UP":
                    c.relative_side_advantage = up_score - down_score
                    c.is_top_side = (up_score >= down_score)
                else:
                    c.relative_side_advantage = down_score - up_score
                    c.is_top_side = (down_score > up_score)

        return candidates

    def rank_opportunities(self, candidates: list[OpportunityScore]
                           ) -> tuple[list[OpportunityScore], list[OpportunityScore]]:
        """Rank all candidates by composite score, filter by minimum thresholds.

        V21.5: Score almost everything, reject very little,
        rank aggressively, execute selectively.
        Side selection applied before ranking.
        """
        # Compute EV for each
        for c in candidates:
            c.compute_ev()

        # §7: Side selection
        candidates = self.select_side(candidates)

        # Soft adversarial filter — not hard rejection
        for c in candidates:
            if c.adversarial_score >= self.adversarial_kill_threshold:
                c.credible_ev = 0.0
                c.execution_decision = "SKIP"
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

            if c.credible_ev > self.min_ev:
                c.execution_decision = "EXECUTE"
                executable.append(c)
            elif c.composite_score >= 0.30:
                c.execution_decision = "QUEUE"
                executable.append(c)
            else:
                c.execution_decision = "SKIP"

        return ranked, executable

    def get_top_per_market(self, ranked: list[OpportunityScore],
                           n: int = 1) -> dict[str, list[OpportunityScore]]:
        """Get top-n opportunities per market."""
        by_market: dict[str, list[OpportunityScore]] = {}
        for c in ranked:
            key = f"{c.asset}-{c.interval}"
            if key not in by_market:
                by_market[key] = []
            by_market[key].append(c)

        result = {}
        for key, opps in by_market.items():
            result[key] = opps[:n]
        return result