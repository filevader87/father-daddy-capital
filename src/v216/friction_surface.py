#!/usr/bin/env python3
"""
V21.6 Friction Surface Engine (§5)
====================================
Tracks per-execution friction and produces:
- friction_score ∈ [0,1] (lower = less friction)
- survivability_score ∈ [0,1] (higher = more survivable)

Components:
- realized slippage
- expected slippage
- queue latency
- partial fill frequency
- cancellation rate
- spread widening/compression
- orderbook evaporation
- fill asymmetry
"""

import numpy as np
from collections import defaultdict, deque
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field


@dataclass
class FrictionObservation:
    """Single friction observation from an execution."""
    slippage_bps: float
    expected_slippage_bps: float
    queue_delay_ms: float
    fill_latency_ms: float
    fill_pct: float
    was_rejected: bool
    was_stale: bool
    was_repriced: bool
    repriced_delta: float
    spread_bps: float
    entry_price: float
    bucket_zone: str
    asset: str
    timing: str
    regime: str
    timestamp: float = 0.0


class FrictionSurface:
    """
    §5: Per-cell friction tracking and scoring.
    Generates friction_score and survivability_score for each execution context.
    """

    def __init__(self, window_size: int = 500):
        self.window_size = window_size
        self.observations: deque = deque(maxlen=window_size)
        self.cell_history: Dict[tuple, deque] = defaultdict(
            lambda: deque(maxlen=200)
        )

        # Per-bucket rolling stats
        self.bucket_stats: Dict[str, dict] = defaultdict(
            lambda: {
                'slippages': deque(maxlen=100),
                'fill_rates': deque(maxlen=100),
                'queue_delays': deque(maxlen=100),
                'spreads': deque(maxlen=100),
                'rejection_rate': deque(maxlen=100),
                'pnls': deque(maxlen=100),
            }
        )

        # Global rolling stats
        self.global_slippage_ema = 0.0
        self.global_spread_ema = 0.0
        self.global_fill_rate_ema = 1.0
        self.global_queue_ema = 0.0
        self.alpha = 0.12  # EMA decay

    def record(self, obs: FrictionObservation):
        """Record a friction observation."""
        self.observations.append(obs)

        # Per-cell history
        cell_key = (obs.asset, obs.bucket_zone, obs.timing, obs.regime)
        self.cell_history[cell_key].append(obs)

        # Per-bucket stats
        bucket = obs.bucket_zone
        self.bucket_stats[bucket]['slippages'].append(obs.slippage_bps)
        self.bucket_stats[bucket]['fill_rates'].append(
            0.0 if obs.was_rejected else (obs.fill_pct if not obs.was_stale else 0.0)
        )
        self.bucket_stats[bucket]['queue_delays'].append(obs.queue_delay_ms)
        self.bucket_stats[bucket]['spreads'].append(obs.spread_bps)
        self.bucket_stats[bucket]['rejection_rate'].append(
            1.0 if (obs.was_rejected or obs.was_stale) else 0.0
        )

        # EMA global updates
        self.global_slippage_ema = (
            self.global_slippage_ema * (1 - self.alpha) + obs.slippage_bps * self.alpha
        )
        self.global_spread_ema = (
            self.global_spread_ema * (1 - self.alpha) + obs.spread_bps * self.alpha
        )
        fill_rate = 0.0 if obs.was_rejected else obs.fill_pct
        self.global_fill_rate_ema = (
            self.global_fill_rate_ema * (1 - self.alpha) + fill_rate * self.alpha
        )
        self.global_queue_ema = (
            self.global_queue_ema * (1 - self.alpha) + obs.queue_delay_ms * self.alpha
        )

    def compute_friction_score(self, bucket_zone: str = None,
                                timing: str = None,
                                regime: str = None) -> float:
        """
        §5: friction_score ∈ [0,1] — lower = less friction = better.
        Composite of slippage, spread, queue, rejection, partial fill.
        """
        # Use bucket-specific data if available
        if bucket_zone and bucket_zone in self.bucket_stats:
            stats = self.bucket_stats[bucket_zone]
            slippages = list(stats['slippages'])
            spreads = list(stats['spreads'])
            queues = list(stats['queue_delays'])
            rejections = list(stats['rejection_rate'])
        else:
            # Use global EMA
            slippages = [self.global_slippage_ema]
            spreads = [self.global_spread_ema]
            queues = [self.global_queue_ema]
            rejections = [1.0 - self.global_fill_rate_ema]

        if not slippages:
            return 0.5

        avg_slip = np.mean(slippages)
        avg_spread = np.mean(spreads) if spreads else self.global_spread_ema
        avg_queue = np.mean(queues) if queues else self.global_queue_ema
        avg_reject = np.mean(rejections) if rejections else 0.1

        # Normalize each component to [0,1]
        # Slippage: 0bps=perfect, 3000bps=terrible
        slip_score = np.clip(avg_slip / 3000.0, 0, 1)
        # Spread: 100bps=perfect, 2000bps=terrible
        spread_score = np.clip(avg_spread / 2000.0, 0, 1)
        # Queue: 50ms=perfect, 3000ms=terrible
        queue_score = np.clip(avg_queue / 3000.0, 0, 1)
        # Rejection: 0%=perfect, 30%=terrible
        reject_score = np.clip(avg_reject / 0.3, 0, 1)

        # Weighted composite — slippage is king (§2)
        friction = (
            0.35 * slip_score +
            0.25 * spread_score +
            0.15 * queue_score +
            0.25 * reject_score
        )
        return float(np.clip(friction, 0.0, 1.0))

    def compute_survivability_score(self, bucket_zone: str = None,
                                      timing: str = None) -> float:
        """
        §5: survivability_score ∈ [0,1] — higher = more survivable.
        Based on: positive realized EV after friction, fill rate, slippage < 8%.
        """
        if bucket_zone and bucket_zone in self.bucket_stats:
            stats = self.bucket_stats[bucket_zone]
            pnl_list = list(stats['pnls'])
            slippages = list(stats['slippages'])
            fill_rates = list(stats['fill_rates'])
        else:
            return 0.5

        if not pnl_list or len(pnl_list) < 3:
            return 0.5

        # Positive EV after friction?
        avg_pnl = np.mean(pnl_list)
        ev_score = np.tanh(max(0, avg_pnl))

        # Slippage under 8% (800bps)?
        avg_slip = np.mean(slippages) if slippages else 2000
        slip_survivable = 1.0 if avg_slip < 800 else max(0.0, 1.0 - (avg_slip - 800) / 3000.0)

        # Fill rate
        avg_fill = np.mean(fill_rates) if fill_rates else 0.5
        fill_score = np.clip(avg_fill, 0, 1)

        # Variance of outcomes (lower = more predictable = more survivable)
        pnl_std = np.std(pnl_list) if len(pnl_list) > 2 else 1.0
        variance_score = np.clip(1.0 / (1.0 + pnl_std), 0, 1)

        score = (
            0.35 * ev_score +
            0.30 * slip_survivable +
            0.20 * fill_score +
            0.15 * variance_score
        )
        return float(np.clip(score, 0.0, 1.0))

    def compute_net_convexity(self, price: float, win_prob: float,
                               bucket_zone: str = None) -> float:
        """
        §2: net_convexity_score = expected_payout × win_prob
                                        - spread_tax - slippage_drag
                                        - queue_decay - fill_failure_cost
                                        - latency_decay
        """
        # Expected payout for cheap token: (1 - price) if win, -price if lose
        expected_payout = win_prob * (1.0 - price) - (1.0 - win_prob) * price

        # Friction components
        friction = self.compute_friction_score(bucket_zone)

        # Convert friction to cost terms
        # spread_tax: modeled as spread in price terms
        spread_tax = price * friction * 0.05  # 5% of price × friction score

        # slippage_drag
        avg_slip = self.global_slippage_ema if self.global_slippage_ema > 0 else 1500
        slippage_drag = price * avg_slip / 10000.0

        # queue_decay
        queue_cost = price * self.global_queue_ema / 1000000.0

        # fill_failure_cost (opportunity cost)
        fill_failure_cost = expected_payout * (1.0 - self.global_fill_rate_ema)

        # latency_decay
        latency_decay = abs(expected_payout) * 0.002  # ~0.2% per second alpha decay

        net = (expected_payout * (1.0 - friction * 0.3)  # Survivability discount
               - spread_tax
               - slippage_drag
               - queue_cost
               - fill_failure_cost
               - latency_decay)
        return float(net)

    def get_slippage_by_bucket(self) -> Dict[str, dict]:
        """Returns slippage statistics per bucket zone."""
        result = {}
        for bucket, stats in self.bucket_stats.items():
            slips = list(stats['slippages'])
            if slips:
                result[bucket] = {
                    'count': len(slips),
                    'avg_bps': float(np.mean(slips)),
                    'p50_bps': float(np.percentile(slips, 50)),
                    'p95_bps': float(np.percentile(slips, 95)),
                    'p99_bps': float(np.percentile(slips, 99)),
                    'survivability': self.compute_survivability_score(bucket),
                }
        return result

    def should_enter(self, price: float, bucket_zone: str) -> Tuple[bool, str]:
        """
        §5: Binary entry decision based on friction surface.
        Returns (should_enter, reason).
        """
        friction = self.compute_friction_score(bucket_zone)

        # Hard blocks
        if price < 0.03:
            return False, "price_below_minimum"

        # Slippage survival check
        avg_slip = self.global_slippage_ema if self.global_slippage_ema > 0 else 1500
        if avg_slip > 3500:  # §13: >35% slippage = not survivable
            return False, "slippage_unsurvivable"

        # Friction too high
        if friction > 0.85:
            return False, "friction_too_high"

        # Fill rate too low
        if self.global_fill_rate_ema < 0.70:
            return False, "fill_rate_too_low"

        # Net convexity check
        net = self.compute_net_convexity(price, 0.35, bucket_zone)
        if net <= 0:
            return False, "negative_net_convexity"

        return True, "pass"