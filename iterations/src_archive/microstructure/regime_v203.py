#!/usr/bin/env python3
"""
V20.3 Rebuilt Regime Classifier — Section 8
==============================================
Classifies market microstructure regimes using REAL features only.

V20.2 audit found 0.0 bits of entropy because fake spread (0.98) and
fake imbalance (0.0) were fed in. Every observation landed in the same regime.

New regime features:
  - spot_velocity_15s
  - spot_velocity_30s
  - realized_volatility
  - selected_spread
  - selected_imbalance
  - book_depth
  - reference_distance
  - RSI
  - RSI_slope
  - time_to_expiry

Classifications:
  - trend_continuation
  - trend_exhaustion
  - panic_sell
  - balanced_rotation
  - liquidity_vacuum
  - fake_reversal
  - volatility_expansion
  - volatility_compression

Tracks regime entropy. If entropy = 0.0 over 500+ observations:
  REGIME_CLASSIFIER_DEGENERATE = True → block promotion.

Author: Hugh (3rd of 5) for Father Daddy Capital
"""
import math
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
from collections import deque, Counter
from enum import Enum


# ── Regime Types ──
class RegimeV203(str, Enum):
    TREND_CONTINUATION = "trend_continuation"
    TREND_EXHAUSTION = "trend_exhaustion"
    PANIC_SELL = "panic_sell"
    BALANCED_ROTATION = "balanced_rotation"
    LIQUIDITY_VACUUM = "liquidity_vacuum"
    FAKE_REVERSAL = "fake_reversal"
    VOLATILITY_EXPANSION = "volatility_expansion"
    VOLATILITY_COMPRESSION = "volatility_compression"
    UNKNOWN = "unknown"


# ── Configuration ──
ENTROPY_OBSERVATION_THRESHOLD = 500    # Need 500+ obs for entropy check
MINIMUM_ENTROPY_BITS = 0.5             # Below this = degenerate
DEGENERATE_BLOCK_PROMOTION = True      # Block promotion if degenerate


@dataclass
class RegimeFeatures:
    """V20.3 regime features — all must use real market data, never fake."""
    spot_velocity_15s: float = 0.0
    spot_velocity_30s: float = 0.0
    realized_volatility: float = 0.0
    selected_spread: float = 0.0      # REAL bid-ask spread (not UP+DOWN)
    selected_imbalance: float = 0.0   # REAL book imbalance (not 0.0)
    book_depth: float = 0.0           # total depth at top 3 levels
    reference_distance: float = 0.0   # distance from reference price
    rsi: float = 50.0
    rsi_slope: float = 0.0
    time_to_expiry: float = 0.0       # seconds until market expiry


@dataclass
class RegimeResult:
    """V20.3 regime classification result."""
    regime: RegimeV203 = RegimeV203.UNKNOWN
    confidence: float = 0.0
    features: RegimeFeatures = field(default_factory=RegimeFeatures)
    
    # Entropy tracking
    entropy_bits: float = 0.0
    observation_count: int = 0
    is_degenerate: bool = False
    degenerate_reason: str = ""
    
    # Detailed scores for each regime
    regime_scores: Dict[str, float] = field(default_factory=dict)


class RegimeClassifierV203:
    """V20.3 regime classifier with real features and entropy tracking.
    
    Rules-based classification with soft scoring. Each regime gets a score
    based on feature thresholds. Highest-scoring regime wins.
    
    All features must come from real market data:
      - spread from real bid-ask (Section 5)
      - imbalance from real book depth (Section 6)
      - velocities from real spot/token prices
      - RSI from real price series
    """
    
    def __init__(self):
        self._regime_history: List[RegimeV203] = []
        self._entropy_counter: Counter = Counter()
        self._observation_count: int = 0
        self._degenerate: bool = False
        self._degenerate_reason: str = ""
    
    def classify(self, features: RegimeFeatures) -> RegimeResult:
        """Classify current market regime from real features.
        
        Args:
            features: RegimeFeatures with all fields populated from real data.
                     If any critical feature is None/0.0 from missing data,
                     classification confidence drops.
        
        Returns:
            RegimeResult with regime, confidence, entropy status.
        """
        scores: Dict[str, float] = {}
        
        # ── Feature extraction ──
        sv15 = features.spot_velocity_15s
        sv30 = features.spot_velocity_30s
        rv = features.realized_volatility
        spread = features.selected_spread
        imbalance = features.selected_imbalance
        depth = features.book_depth
        ref_dist = features.reference_distance
        rsi = features.rsi
        rsi_slope = features.rsi_slope
        tte = features.time_to_expiry
        
        # ── Trend Continuation ──
        # Strong directional velocity, RSI confirming, spread stable
        trend_strength = abs(sv15 + sv30) / 2.0
        scores["trend_continuation"] = (
            trend_strength * 0.4 +
            (1.0 - rv) * 0.1 +  # Low vol = clean trend
            abs(rsi - 50) / 50 * 0.3 +  # RSI extreme confirms trend
            (1.0 - spread / 0.06) * 0.1 +  # Tight spread confirms
            abs(imbalance) * 0.1  # Order imbalance supports trend
        )
        
        # ── Trend Exhaustion ──
        # Directional velocity fading, RSI overextended, spread widening
        velocity_decay = abs(sv15) - abs(sv30)
        rsi_extreme = max(0, abs(rsi - 50) - 20) / 30  # RSI > 70 or < 30
        scores["trend_exhaustion"] = (
            max(0, velocity_decay) * 0.3 +  # Velocity slowing
            rsi_extreme * 0.3 +  # RSI overextended
            spread * 10 * 0.2 +  # Spread widening
            ref_dist * 0.1 +  # Far from reference
            max(0, -rsi_slope * abs(rsi - 50) / 50) * 0.1  # RSI reversing
        )
        
        # ── Panic Sell ──
        # Sharp negative velocity, extreme RSI, spread blowing out, depth vanishing
        scores["panic_sell"] = (
            max(0, -sv15 * 5) * 0.4 +  # Sharp negative velocity
            max(0, (30 - rsi) / 30) * 0.25 +  # RSI deeply oversold
            spread * 15 * 0.15 +  # Spread blowing out
            max(0, -imbalance) * 0.1 +  # Ask-side dominance
            max(0, 1.0 - depth / 100) * 0.1  # Depth vanishing
        )
        
        # ── Balanced Rotation ──
        # Low velocity, moderate RSI, tight spread, balanced book
        scores["balanced_rotation"] = (
            (1.0 - min(1.0, abs(sv15) * 5)) * 0.3 +  # Low velocity
            (1.0 - abs(rsi - 50) / 30) * 0.2 +  # RSI near center
            (1.0 - spread / 0.04) * 0.2 +  # Tight spread
            (1.0 - abs(imbalance)) * 0.2 +  # Balanced book
            (1.0 - rv) * 0.1  # Low vol
        )
        
        # ── Liquidity Vacuum ──
        # Thin depth, wide spread, but not much velocity yet
        scores["liquidity_vacuum"] = (
            max(0, 1.0 - depth / 50) * 0.4 +  # Thin depth
            spread * 15 * 0.3 +  # Wide spread
            (1.0 - min(1.0, abs(sv15) * 3)) * 0.2 +  # Not much velocity yet
            abs(imbalance) * 0.1  # Lopsided book
        )
        
        # ── Fake Reversal ──
        # RSI hooks back, but velocity weak, spread widening = trap
        scores["fake_reversal"] = (
            max(0, rsi_slope * (50 - rsi) / 50) * 0.35 +  # RSI hooks against trend
            (1.0 - min(1.0, abs(sv15) * 5)) * 0.25 +  # Weak velocity
            spread * 12 * 0.2 +  # Spread widening
            max(0, 0.5 - rv) * 0.1 +  # Low realized vol
            max(0, 1.0 - depth / 30) * 0.1  # Moderate depth
        )
        
        # ── Volatility Expansion ──
        # Rising realized vol, wide spread, imbalanced book
        scores["volatility_expansion"] = (
            rv * 0.4 +  # High realized vol
            spread * 10 * 0.25 +  # Wide spread
            abs(sv15) * 2 * 0.15 +  # Velocity present
            abs(imbalance) * 0.1 +  # Imbalanced
            abs(rsi_slope) * 0.1  # RSI moving
        )
        
        # ── Volatility Compression ──
        # Falling realized vol, tight spread, balanced book, range-bound RSI
        # Often occurs before breakout — time_to_expiry matters
        scores["volatility_compression"] = (
            (1.0 - rv) * 0.3 +  # Low realized vol
            (1.0 - spread / 0.04) * 0.25 +  # Tight spread
            (1.0 - abs(imbalance)) * 0.2 +  # Balanced book
            (1.0 - abs(rsi - 50) / 30) * 0.15 +  # RSI near center
            min(1.0, tte / 300) * 0.1  # Not near expiry yet
        )
        
        # ── Clip negative scores to 0 ──
        for k in scores:
            scores[k] = max(0.0, scores[k])
        
        # ── Winner ──
        winner_name: str = ""
        winner_score: float = 0.0
        for k, v in scores.items():
            if v > winner_score:
                winner_score = v
                winner_name = k

        if not scores or winner_score == 0:
            winner = RegimeV203.UNKNOWN
            confidence = 0.0
        else:
            winner = RegimeV203(winner_name)
            total = sum(scores.values())
            confidence = winner_score / total if total > 0 else 0.0
        
        # ── Entropy tracking ──
        self._observation_count += 1
        self._regime_history.append(winner)
        self._entropy_counter[winner] += 1
        
        # Compute Shannon entropy
        entropy_bits = self._compute_entropy()
        
        # Check degenerate condition
        is_degenerate = False
        degenerate_reason = ""
        
        if self._observation_count >= ENTROPY_OBSERVATION_THRESHOLD:
            if entropy_bits < MINIMUM_ENTROPY_BITS:
                is_degenerate = True
                degenerate_reason = (
                    f"REGIME_CLASSIFIER_DEGENERATE: entropy={entropy_bits:.3f} bits "
                    f"over {self._observation_count} observations. "
                    f"Dominant regime: {self._entropy_counter.most_common(1)[0][0]} "
                    f"({self._entropy_counter.most_common(1)[0][1]}/{self._observation_count})"
                )
                self._degenerate = True
                self._degenerate_reason = degenerate_reason
        
        return RegimeResult(
            regime=winner,
            confidence=round(confidence, 4),
            features=features,
            entropy_bits=round(entropy_bits, 4),
            observation_count=self._observation_count,
            is_degenerate=is_degenerate or self._degenerate,
            degenerate_reason=degenerate_reason or self._degenerate_reason,
            regime_scores={k: round(v, 4) for k, v in scores.items()},
        )
    
    def _compute_entropy(self) -> float:
        """Compute Shannon entropy of regime distribution.
        
        0 bits = all observations in one regime (degenerate)
        log2(8) ≈ 3 bits = uniform across 8 regimes (max info)
        """
        n = sum(self._entropy_counter.values())
        if n == 0:
            return 0.0
        
        entropy = 0.0
        for count in self._entropy_counter.values():
            if count > 0:
                p = count / n
                entropy -= p * math.log2(p)
        
        return entropy
    
    def get_regime_distribution(self) -> Dict[str, int]:
        """Get count of each regime observed."""
        return dict(self._entropy_counter.most_common())
    
    def get_entropy_status(self) -> dict:
        """Get detailed entropy diagnostics."""
        return {
            "entropy_bits": round(self._compute_entropy(), 4),
            "observation_count": self._observation_count,
            "unique_regimes": len(self._entropy_counter),
            "distribution": self.get_regime_distribution(),
            "is_degenerate": self._degenerate,
            "degenerate_reason": self._degenerate_reason,
            "dominant_regime": self._entropy_counter.most_common(1)[0] if self._entropy_counter else None,
        }