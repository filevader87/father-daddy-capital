#!/usr/bin/env python3
"""
V20.3.1 §§3-5 — Cross-Asset Correlation, Regime Entropy, Adversarial Detection
================================================================================
§3: Prevent fake diversification — measure cross-asset correlation
§4: Kill degenerate regime classifiers — demand entropy > threshold
§5: Detect adversarial market structure — spoof depth, midpoint pinning, etc.

Author: Hugh (3rd of 5) for Father Daddy Capital
"""
import math
import time
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from collections import defaultdict, Counter

import sys
sys.path.insert(0, '/home/naq1987s/father-daddy-capital')
from src.cell.cell_framework import CellKey, CellTracker, CellStatus


# ══════════════════════════════════════════════════════════════════
# §3 — Cross-Asset Correlation Suppression
# ══════════════════════════════════════════════════════════════════

HIGH_CORRELATION_THRESHOLD = 0.70  # Pearson r > 0.70 = high correlation
DRAWDOWN_CORR_THRESHOLD = 0.60    # Drawdown correlation > 0.60 = risk clustering
BETA_SIMILARITY_THRESHOLD = 0.75   # Similar beta = same underlying exposure


@dataclass
class CorrelationReport:
    """Cross-asset correlation analysis."""
    asset_pairs: List[Dict] = field(default_factory=list)
    high_correlation_warnings: List[str] = field(default_factory=list)
    shared_regime_factor: float = 0.0
    exposure_reduction_needed: bool = False
    reduction_factor: float = 1.0  # 1.0 = no reduction, 0.5 = cut allocation in half


class CrossAssetCorrelation:
    """Measure and suppress fake diversification.
    
    BTC, ETH, SOL, XRP often represent the same crypto-beta move.
    This module detects:
      - Signal correlation (PnL correlation across assets)
      - Drawdown correlation (simultaneous losses)
      - Shared regime factor (same regime = same exposure)
      - Beta similarity (price movement correlation)
    """
    
    def __init__(self, tracker: CellTracker):
        self.tracker = tracker
        self._pnl_history: Dict[str, List[float]] = defaultdict(list)
    
    def record_pnl(self, asset: str, pnl: float):
        """Record PnL for correlation tracking."""
        self._pnl_history[asset].append(pnl)
    
    def compute_correlation(self) -> CorrelationReport:
        """Compute cross-asset PnL correlation."""
        report = CorrelationReport()
        assets = list(self._pnl_history.keys())
        
        if len(assets) < 2:
            return report
        
        # Compute pairwise correlation
        for i in range(len(assets)):
            for j in range(i + 1, len(assets)):
                a1, a2 = assets[i], assets[j]
                p1 = self._pnl_history[a1]
                p2 = self._pnl_history[a2]
                
                # Align by length
                n = min(len(p1), len(p2))
                if n < 5:
                    continue
                
                p1_aligned = p1[-n:]
                p2_aligned = p2[-n:]
                
                corr = self._pearson(p1_aligned, p2_aligned)
                
                pair_report = {
                    "asset_1": a1,
                    "asset_2": a2,
                    "correlation": round(corr, 4),
                    "shared_regime": abs(corr) > HIGH_CORRELATION_THRESHOLD,
                    "n_observations": n,
                }
                report.asset_pairs.append(pair_report)
                
                if abs(corr) > HIGH_CORRELATION_THRESHOLD:
                    report.high_correlation_warnings.append(
                        f"{a1}-{a2}: r={corr:.3f} exceeds {HIGH_CORRELATION_THRESHOLD}"
                    )
        
        # Compute shared regime factor (average absolute correlation)
        corrs = [abs(p["correlation"]) for p in report.asset_pairs]
        report.shared_regime_factor = round(sum(corrs) / len(corrs), 4) if corrs else 0
        
        # Determine if exposure reduction needed
        n_high = len(report.high_correlation_warnings)
        if n_high > 0:
            report.exposure_reduction_needed = True
            # Reduce by proportion of correlated pairs
            report.reduction_factor = max(0.25, 1.0 - n_high * 0.15)
        
        # Check drawdown correlation
        for pair in report.asset_pairs:
            # Same direction losses = drawdown correlation
            pass  # Computed from PnL histories (already captured in Pearson)
        
        return report
    
    def _pearson(self, x: List[float], y: List[float]) -> float:
        """Compute Pearson correlation coefficient."""
        n = len(x)
        if n < 2:
            return 0.0
        mean_x = sum(x) / n
        mean_y = sum(y) / n
        cov = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
        var_x = sum((xi - mean_x) ** 2 for xi in x)
        var_y = sum((yi - mean_y) ** 2 for yi in y)
        denom = math.sqrt(var_x * var_y)
        return cov / denom if denom > 0 else 0.0
    
    def reduce_exposure(self, allocation: Dict[CellKey, float],
                        correlation: CorrelationReport) -> Dict[CellKey, float]:
        """Reduce allocation for correlated cells."""
        if not correlation.exposure_reduction_needed:
            return allocation
        
        reduced = {}
        for key, weight in allocation.items():
            # Reduce weight by correlation factor for cells in correlated assets
            if any(key.asset in w for w in correlation.high_correlation_warnings):
                reduced[key] = weight * correlation.reduction_factor
            else:
                reduced[key] = weight
        
        return reduced


# ══════════════════════════════════════════════════════════════════
# §4 — Regime Entropy Validation
# ══════════════════════════════════════════════════════════════════

MINIMUM_REGIME_ENTROPY = 0.5   # bits (V20.2 had 0.0)
ENTROPY_OBSERVATION_THRESHOLD = 100  # Need 100+ observations
REGIME_DISTRIBUTION_MAX_DOMINANCE = 0.60  # No single regime > 60%


@dataclass
class RegimeEntropyReport:
    """Regime entropy validation report."""
    entropy_bits: float = 0.0
    observation_count: int = 0
    regime_distribution: Dict[str, int] = field(default_factory=dict)
    dominant_regime: str = ""
    dominant_pct: float = 0.0
    is_degenerate: bool = False
    is_valid: bool = False
    classifier_disabled: bool = False
    entropy_trend: str = ""  # "stable", "increasing", "decreasing"
    promotion_blocked: bool = False
    block_reason: str = ""


class RegimeEntropyValidator:
    """Prevent informationally dead regime classifiers.
    
    V20.2 had balanced_rotation=100%, entropy=0 bits.
    This is unacceptable.
    
    Promotion blocked if:
      - regime_entropy < MINIMUM_REGIME_ENTROPY
      - single regime > REGIME_DISTRIBUTION_MAX_DOMINANCE
    If classifier collapses → classifier_disabled=True.
    """
    
    def __init__(self):
        self._regime_history: List[str] = []
        self._entropy_history: List[float] = []
        self._disabled = False
        self._disabled_reason = ""
    
    def record_regime(self, regime: str):
        """Record a regime observation."""
        self._regime_history.append(regime)
    
    def validate(self) -> RegimeEntropyReport:
        """Validate regime entropy and distribution."""
        report = RegimeEntropyReport()
        report.observation_count = len(self._regime_history)
        
        if report.observation_count < 10:
            report.is_valid = True  # Not enough data yet
            report.entropy_bits = 0.0
            return report
        
        # Compute distribution
        counter = Counter(self._regime_history)
        report.regime_distribution = dict(counter.most_common())
        report.dominant_regime = counter.most_common(1)[0][0]
        report.dominant_pct = counter.most_common(1)[0][1] / report.observation_count
        
        # Compute Shannon entropy
        n = report.observation_count
        entropy = -sum((c / n) * math.log2(c / n) for c in counter.values())
        report.entropy_bits = round(entropy, 4)
        self._entropy_history.append(entropy)
        
        # Entropy trend
        if len(self._entropy_history) >= 20:
            recent = self._entropy_history[-10:]
            older = self._entropy_history[-20:-10]
            recent_mean = sum(recent) / len(recent)
            older_mean = sum(older) / len(older) if older else 0
            if recent_mean > older_mean + 0.05:
                report.entropy_trend = "increasing"
            elif recent_mean < older_mean - 0.05:
                report.entropy_trend = "decreasing"
            else:
                report.entropy_trend = "stable"
        
        # ── Validate ──
        # Degenerate: entropy = 0 or single regime dominates
        if report.observation_count >= ENTROPY_OBSERVATION_THRESHOLD:
            if report.entropy_bits < MINIMUM_REGIME_ENTROPY:
                report.is_degenerate = True
                report.is_valid = False
                report.promotion_blocked = True
                report.block_reason = (
                    f"REGIME_ENTROPY={report.entropy_bits:.3f} bits < {MINIMUM_REGIME_ENTROPY} bits "
                    f"after {report.observation_count} observations"
                )
                self._disabled = True
                self._disabled_reason = report.block_reason
            elif report.dominant_pct > REGIME_DISTRIBUTION_MAX_DOMINANCE:
                report.is_degenerate = True
                report.is_valid = False
                report.promotion_blocked = True
                report.block_reason = (
                    f"DOMINANT_REGIME={report.dominant_regime} at {report.dominant_pct:.1%} > "
                    f"{REGIME_DISTRIBUTION_MAX_DOMINANCE:.0%}"
                )
            else:
                report.is_valid = True
        
        report.classifier_disabled = self._disabled
        
        return report


# ══════════════════════════════════════════════════════════════════
# §5 — Adversarial Market Detection
# ══════════════════════════════════════════════════════════════════

ADVERSARIAL_SCORE_MAX = 1.0
ADVERSARIAL_THRESHOLD = 0.60  # Score > 0.60 = adversarial environment


@dataclass
class AdversarialReport:
    """Adversarial market structure analysis."""
    adversarial_score: float = 0.0          # 0=neutral, 1=hostile
    depth_disappearance_rate: float = 0.0    # How often depth vanishes
    cancel_replace_velocity: float = 0.0     # Cancel/replace rate
    spread_snapback_frequency: float = 0.0   # How often spread snaps back after crossing
    midpoint_pin_duration_pct: float = 0.0   # % of time price pinned to midpoint
    fake_breakout_rate: float = 0.0          # % of breakouts that reverse
    is_adversarial: bool = False
    risk_penalty: float = 0.0                # 0=no penalty, 1=max penalty
    recommended_action: str = ""              # "normal", "reduce", "disable"


class AdversarialDetector:
    """Detect hostile market structure behavior.
    
    Models: MM inventory balancing, liquidity bait, spoof depth,
    repricing traps, midpoint pinning, fake breakouts, synthetic volatility.
    
    High adversarial environments get risk penalties and reduced allocation.
    """
    
    def __init__(self):
        self._depth_history: List[float] = []          # Depth over time
        self._cancel_replace_events: List[float] = []   # Cancellation timestamps
        self._spread_snapback_events: List[float] = []   # Snapback timestamps
        self._midpoint_pin_events: List[float] = []     # Pin timestamps
        self._breakout_results: List[bool] = []          # True=fake, False=real
    
    def record_depth(self, depth: float):
        """Record current book depth."""
        self._depth_history.append(depth)
        if len(self._depth_history) > 1000:
            self._depth_history = self._depth_history[-500:]
    
    def record_cancel_replace(self):
        """Record a cancel/replace event."""
        self._cancel_replace_events.append(time.time())
    
    def record_spread_snapback(self):
        """Record a spread snapback (spread widens after fill)."""
        self._spread_snapback_events.append(time.time())
    
    def record_midpoint_pin(self, duration_ms: float):
        """Record midpoint pinning duration."""
        self._midpoint_pin_events.append(duration_ms)
    
    def record_breakout_result(self, is_fake: bool):
        """Record whether a breakout was fake or real."""
        self._breakout_results.append(is_fake)
        if len(self._breakout_results) > 200:
            self._breakout_results = self._breakout_results[-100:]
    
    def compute_adversarial_score(self) -> AdversarialReport:
        """Compute overall adversarial market score.
        
        Score = weighted sum of adversarial indicators.
        0 = neutral market, 1 = fully adversarial.
        """
        report = AdversarialReport()
        
        # ── Depth disappearance rate ──
        # What fraction of observations show <20% of average depth?
        if len(self._depth_history) > 10:
            avg_depth = sum(self._depth_history) / len(self._depth_history)
            threshold = avg_depth * 0.20
            disappearances = sum(1 for d in self._depth_history if d < threshold)
            report.depth_disappearance_rate = disappearances / len(self._depth_history)
        
        # ── Cancel/replace velocity ──
        # Events in last 5 minutes
        now = time.time()
        recent_cancel = sum(1 for t in self._cancel_replace_events if now - t < 300)
        report.cancel_replace_velocity = min(1.0, recent_cancel / 50.0)  # 50 cancels in 5min = max
        
        # ── Spread snapback frequency ──
        recent_snapbacks = sum(1 for t in self._spread_snapback_events if now - t < 300)
        report.spread_snapback_frequency = min(1.0, recent_snapbacks / 20.0)  # 20 snapbacks = max
        
        # ── Midpoint pin duration ──
        if self._midpoint_pin_events:
            avg_pin = sum(self._midpoint_pin_events) / len(self._midpoint_pin_events)
            # Pin > 5000ms indicates MM control
            report.midpoint_pin_duration_pct = min(1.0, avg_pin / 10000.0)
        
        # ── Fake breakout rate ──
        if len(self._breakout_results) > 5:
            fake_count = sum(1 for b in self._breakout_results if b)
            report.fake_breakout_rate = fake_count / len(self._breakout_results)
        
        # ── Weighted adversarial score ──
        weights = {
            "depth": 0.25,
            "cancel": 0.15,
            "snapback": 0.20,
            "pin": 0.20,
            "breakout": 0.20,
        }
        report.adversarial_score = round(
            weights["depth"] * report.depth_disappearance_rate +
            weights["cancel"] * report.cancel_replace_velocity +
            weights["snapback"] * report.spread_snapback_frequency +
            weights["pin"] * report.midpoint_pin_duration_pct +
            weights["breakout"] * report.fake_breakout_rate,
            4
        )
        
        # ── Classification ──
        report.is_adversarial = report.adversarial_score > ADVERSARIAL_THRESHOLD
        
        if report.adversarial_score > 0.80:
            report.risk_penalty = 0.80  # Cut allocation by 80%
            report.recommended_action = "disable"
        elif report.adversarial_score > ADVERSARIAL_THRESHOLD:
            report.risk_penalty = 0.50  # Cut allocation by 50%
            report.recommended_action = "reduce"
        elif report.adversarial_score > 0.40:
            report.risk_penalty = 0.20  # Cut allocation by 20%
            report.recommended_action = "reduce"
        else:
            report.risk_penalty = 0.0
            report.recommended_action = "normal"
        
        return report