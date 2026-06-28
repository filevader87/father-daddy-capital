#!/usr/bin/env python3
"""
V21.6 Liquidity Topology Mapper (§3)
======================================
Continuously maps per-cell liquidity surfaces:
- bucket liquidity
- slippage surface
- queue delay
- spread compression frequency
- fill survivability
- maker/taker viability
- orderbook resiliency
- price pinning behavior

Produces: liquidity_score ∈ [0,1], execution_quality_score ∈ [0,1]
Per: asset × interval × side × bucket × timing × regime × volatility
"""

import numpy as np
from collections import defaultdict
from typing import Dict, Optional, Tuple


# Dynamic bucket zones (§4) — reranked every 15min live / 1hr paper
DEFAULT_BUCKET_ZONES = {
    'ultra_cheap':  (0.03, 0.05),
    'cheap':       (0.05, 0.08),
    'mid_cheap':   (0.08, 0.12),
    'mid':         (0.12, 0.15),
    'mid_rich':    (0.15, 0.20),
    'rich':        (0.20, 0.30),
    'expensive':   (0.30, 0.50),
}

# Timing phases (§6)
TIMING_PHASES = {
    'EARLY':     {'priority': 0.10, 'spread_mult': 1.5},
    'FORMATION': {'priority': 0.35, 'spread_mult': 1.2},
    'MOMENTUM':  {'priority': 0.80, 'spread_mult': 0.8},
    'LATE':      {'priority': 0.95, 'spread_mult': 0.6},
    'FINAL':     {'priority': 0.40, 'spread_mult': 1.0},
}


class LiquidityCell:
    """Per-cell liquidity state (§8)."""
    __slots__ = [
        'asset', 'interval', 'side', 'bucket_zone', 'timing', 'regime',
        'n_obs', 'n_filled', 'n_partial', 'n_rejected', 'n_stale',
        'avg_slippage_bps', 'avg_spread_bps', 'avg_queue_delay_ms',
        'avg_fill_latency_ms', 'avg_fill_pct',
        'realized_pnl', 'realized_wins', 'realized_trades',
        'spread_compressions', 'spread_widenings',
        'liquidity_score', 'execution_quality_score',
        'last_updated', 'hostility_count',
    ]

    def __init__(self, asset='BTC', interval='5m', side='DOWN',
                 bucket_zone='cheap', timing='MOMENTUM', regime='trending_down'):
        self.asset = asset
        self.interval = interval
        self.side = side
        self.bucket_zone = bucket_zone
        self.timing = timing
        self.regime = regime
        self.n_obs = 0
        self.n_filled = 0
        self.n_partial = 0
        self.n_rejected = 0
        self.n_stale = 0
        self.avg_slippage_bps = 0.0
        self.avg_spread_bps = 0.0
        self.avg_queue_delay_ms = 0.0
        self.avg_fill_latency_ms = 0.0
        self.avg_fill_pct = 1.0
        self.realized_pnl = 0.0
        self.realized_wins = 0
        self.realized_trades = 0
        self.spread_compressions = 0
        self.spread_widenings = 0
        self.liquidity_score = 0.5
        self.execution_quality_score = 0.5
        self.last_updated = 0.0
        self.hostility_count = 0

    @property
    def cell_key(self):
        return (self.asset, self.interval, self.side, self.bucket_zone,
                self.timing, self.regime)

    @property
    def fill_rate(self):
        return self.n_filled / max(self.n_obs, 1)

    @property
    def win_rate(self):
        return self.realized_wins / max(self.realized_trades, 1)

    @property
    def realized_ev(self):
        if self.realized_trades == 0:
            return 0.0
        return self.realized_pnl / self.realized_trades

    def update_ema(self, current, new_val, alpha=0.15):
        """Exponential moving average update."""
        if current == 0.0:
            return new_val
        return current * (1 - alpha) + new_val * alpha


class LiquidityTopology:
    """
    §3: Maps liquidity topology across all cells.
    Produces liquidity_score and execution_quality_score per cell.
    """

    def __init__(self):
        self.cells: Dict[tuple, LiquidityCell] = defaultdict(LiquidityCell)
        self.bucket_rerank_ts = 0.0
        self.bucket_scores: Dict[str, float] = {}

    def get_or_create_cell(self, asset, interval, side, bucket_zone,
                           timing, regime) -> LiquidityCell:
        key = (asset, interval, side, bucket_zone, timing, regime)
        if key not in self.cells:
            cell = LiquidityCell(asset, interval, side, bucket_zone, timing, regime)
            self.cells[key] = cell
        return self.cells[key]

    def record_observation(self, asset, interval, side, price, timing,
                           regime, filled=True, partial=False, rejected=False,
                           stale=False, slippage_bps=0.0, spread_bps=0.0,
                           queue_delay_ms=0.0, fill_latency_ms=0.0,
                           fill_pct=1.0, pnl=0.0, won=False):
        """Record a real market observation into the topology."""
        # Determine bucket zone from price
        bucket_zone = self._price_to_bucket(price)

        cell = self.get_or_create_cell(asset, interval, side, bucket_zone,
                                        timing, regime)
        cell.n_obs += 1
        cell.last_updated = 0.0  # Would be timestamp in live

        if filled and not partial and not rejected:
            cell.n_filled += 1
        elif partial:
            cell.n_partial += 1
            cell.n_filled += 1  # Partial fill still counts
        elif rejected:
            cell.n_rejected += 1
        elif stale:
            cell.n_stale += 1

        # EMA updates
        cell.avg_slippage_bps = cell.update_ema(cell.avg_slippage_bps, slippage_bps)
        cell.avg_spread_bps = cell.update_ema(cell.avg_spread_bps, spread_bps)
        cell.avg_queue_delay_ms = cell.update_ema(cell.avg_queue_delay_ms, queue_delay_ms)
        cell.avg_fill_latency_ms = cell.update_ema(cell.avg_fill_latency_ms, fill_latency_ms)
        cell.avg_fill_pct = cell.update_ema(cell.avg_fill_pct, fill_pct)

        # PnL tracking
        if filled and not rejected:
            cell.realized_trades += 1
            cell.realized_pnl += pnl
            if won:
                cell.realized_wins += 1

        # Spread compression/widening
        if spread_bps < 200:  # Tight spread
            cell.spread_compressions += 1
        elif spread_bps > 800:  # Wide spread
            cell.spread_widenings += 1

        # Recompute scores
        cell.liquidity_score = self._compute_liquidity_score(cell)
        cell.execution_quality_score = self._compute_execution_quality(cell)

    def _price_to_bucket(self, price: float) -> str:
        """§4: Dynamic bucket classification."""
        for zone, (lo, hi) in DEFAULT_BUCKET_ZONES.items():
            if lo <= price < hi:
                return zone
        return 'expensive' if price >= 0.50 else 'ultra_cheap'

    def _compute_liquidity_score(self, cell: LiquidityCell) -> float:
        """
        §3: liquidity_score ∈ [0,1]
        Based on: fill rate, spread tightness, queue depth, orderbook resilience
        """
        if cell.n_obs < 3:
            return 0.5  # Insufficient data

        # Fill rate component (0-1)
        fill_rate = cell.fill_rate

        # Spread component (lower = better, capped at 0)
        # 200bps = excellent, 1000bps = poor
        spread_score = max(0.0, 1.0 - cell.avg_spread_bps / 1500.0)

        # Queue delay component (faster = better)
        # 50ms = excellent, 2000ms = poor
        queue_score = max(0.0, 1.0 - cell.avg_queue_delay_ms / 3000.0)

        # Partial fill penalty
        partial_penalty = 1.0 - (cell.n_partial / max(cell.n_obs, 1))

        # Spread compression frequency (tight markets = more compressions)
        compression_rate = cell.spread_compressions / max(cell.n_obs, 1)
        compression_bonus = min(1.0, compression_rate * 2.0)

        # Weighted composite
        score = (
            0.30 * fill_rate +
            0.25 * spread_score +
            0.15 * queue_score +
            0.15 * partial_penalty +
            0.15 * compression_bonus
        )
        return float(np.clip(score, 0.0, 1.0))

    def _compute_execution_quality(self, cell: LiquidityCell) -> float:
        """
        §3: execution_quality_score ∈ [0,1]
        Based on: realized post-friction PnL, slippage, fill quality
        """
        if cell.realized_trades < 3:
            return 0.5

        # Positive EV component
        ev = cell.realized_ev
        ev_score = np.tanh(max(0, ev))  # Maps [0, ∞] → [0, 1]

        # Slippage component (lower = better)
        # 300bps = excellent, 3000bps = terrible
        slippage_score = max(0.0, 1.0 - cell.avg_slippage_bps / 4000.0)

        # Fill quality (fill % and rate)
        fill_quality = cell.avg_fill_pct * cell.fill_rate

        # Win rate contribution (not primary, but real)
        wr_contrib = np.tanh(cell.win_rate * 2.0)

        score = (
            0.35 * ev_score +
            0.30 * slippage_score +
            0.20 * fill_quality +
            0.15 * wr_contrib
        )
        return float(np.clip(score, 0.0, 1.0))

    def compute_hostility_score(self, cell: LiquidityCell) -> float:
        """
        §12: Market hostility score ∈ [0,1].
        Higher = more hostile.
        """
        if cell.n_obs < 3:
            return 0.5

        # High rejection rate = hostile
        rejection_rate = (cell.n_rejected + cell.n_stale) / max(cell.n_obs, 1)

        # High slippage = hostile
        slippage_hostility = min(1.0, cell.avg_slippage_bps / 3000.0)

        # High spread widening = hostile
        widening_rate = cell.spread_widenings / max(cell.n_obs, 1)

        # Low fill rate = hostile
        low_fill_hostility = 1.0 - cell.fill_rate

        # Negative EV = hostile
        ev_hostility = min(1.0, max(0.0, -cell.realized_ev))

        score = (
            0.25 * rejection_rate +
            0.25 * slippage_hostility +
            0.15 * widening_rate +
            0.15 * low_fill_hostility +
            0.20 * ev_hostility
        )
        return float(np.clip(score, 0.0, 1.0))

    def rerank_buckets(self) -> Dict[str, float]:
        """
        §4: Dynamic bucket reranking.
        bucket_score = convexity × survivability × fillability × realized_pnl
        """
        bucket_pnls = defaultdict(list)
        bucket_fills = defaultdict(list)
        bucket_slips = defaultdict(list)

        for key, cell in self.cells.items():
            bucket_pnls[cell.bucket_zone].append(cell.realized_ev)
            bucket_fills[cell.bucket_zone].append(cell.fill_rate)
            bucket_slips[cell.bucket_zone].append(cell.avg_slippage_bps)

        bucket_scores = {}
        for zone in DEFAULT_BUCKET_ZONES:
            evs = bucket_pnls.get(zone, [0])
            fills = bucket_fills.get(zone, [0.5])
            slips = bucket_slips.get(zone, [2000])

            avg_ev = np.mean(evs) if evs else 0
            fillability = np.mean(fills)
            survivability = max(0.0, 1.0 - np.mean(slips) / 4000.0)

            # Convexity proxy from average EV
            convexity = np.tanh(max(0, avg_ev))

            # §4: bucket_score = convexity × survivability × fillability × realized
            realized_factor = 1.0 if avg_ev > 0 else 0.3
            bucket_scores[zone] = convexity * survivability * fillability * realized_factor

        self.bucket_scores = bucket_scores
        return bucket_scores

    def get_best_buckets(self, n: int = 3) -> list:
        """Return top-N bucket zones by combined score."""
        if not self.bucket_scores:
            self.rerank_buckets()
        ranked = sorted(self.bucket_scores.items(), key=lambda x: x[1], reverse=True)
        return ranked[:n]

    def get_cell_summary(self) -> list:
        """Return all cells sorted by execution quality."""
        cells = list(self.cells.values())
        cells.sort(key=lambda c: c.execution_quality_score, reverse=True)
        return cells