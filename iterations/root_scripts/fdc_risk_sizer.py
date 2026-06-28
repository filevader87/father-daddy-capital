#!/usr/bin/env python3
"""
FDC 3-Perspective Risk Sizer
TradingAgents principle: Risk assessment via competing postures, not a single parameter.
Replaces fixed Kelly with Risky/Safe/Neutral deliberation.

Heuristic implementation (no LLM API needed):
  Risky: Full Kelly × momentum multiplier — conviction-weighted
  Safe:  0.25× Kelly with drawdown cap — capital preservation
  Neutral: 0.5× Kelly balanced — hedged with vol-aware sizing

Author: Hugh (3rd of 5)
Source: TradingAgents (arXiv:2412.20138v7) — Risk Management Team architecture
Date: 2026-05-15
"""

from __future__ import annotations
from typing import Dict, Optional, Tuple
from dataclasses import dataclass
import math


# ══════════════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class RiskConfig:
    """Risk posture blending parameters."""

    # Kelly multipliers per posture
    risky_kelly_mult: float = 1.5     # Full Kelly with conviction boost
    safe_kelly_mult: float = 0.25     # Conservative — preserve capital
    neutral_kelly_mult: float = 0.75  # Balanced

    # Posture weights (dynamic — shift with market conditions)
    risky_weight_base: float = 0.25
    safe_weight_base: float = 0.40
    neutral_weight_base: float = 0.35

    # Dynamic adjustments
    bullish_adjust: float = 0.10      # Shift toward risky in trending_up
    bearish_adjust: float = -0.20     # Shift toward safe in trending_down
    volatile_adjust: float = -0.15    # Shift toward safe in high vol
    low_vol_adjust: float = 0.10      # Shift toward risky in calm

    # Caps
    max_position_pct: float = 0.10    # Hard cap: 10% of bankroll
    min_position: float = 3.0         # Minimum bet
    drawdown_cap_pct: float = 0.05    # Max daily drawdown before safe-only

    # Momentum boost for risky posture
    momentum_boost_max: float = 0.40  # Max additional weight from momentum

    # Debate integration
    debate_weight: float = 0.15       # How much debate outcome shifts posture


# ══════════════════════════════════════════════════════════════════════════════
# Market Regime Detection
# ══════════════════════════════════════════════════════════════════════════════

def detect_regime(signal: dict) -> str:
    """Classify current market regime from signal data."""
    rsi = signal.get("rsi", 50)
    macd = signal.get("macd", 0)
    price = signal.get("price", 0)
    sma20 = signal.get("sma20", price)
    prices = signal.get("_prices", [])

    # Volatility check
    volatility = 0.02
    if len(prices) >= 10:
        returns = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, min(11, len(prices)))]
        volatility = sum(abs(r) for r in returns) / len(returns)

    # Regime classification
    is_uptrend = price > sma20 and macd > 0
    is_downtrend = price < sma20 and macd < 0
    is_volatile = volatility > 0.03

    if is_uptrend and not is_volatile:
        return "trending_up"
    elif is_uptrend and is_volatile:
        return "volatile_up"
    elif is_downtrend and not is_volatile:
        return "trending_down"
    elif is_downtrend and is_volatile:
        return "volatile_down"
    elif is_volatile:
        return "volatile"
    else:
        return "ranging"


# ══════════════════════════════════════════════════════════════════════════════
# Posture Calculation
# ══════════════════════════════════════════════════════════════════════════════

def compute_posture_weights(
    signal: dict,
    regime: str,
    debate_net_score: float = 0,
    config: Optional[RiskConfig] = None,
) -> Tuple[float, float, float]:
    """
    Compute dynamic posture weights based on regime + debate outcome.

    Returns:
        (risky_weight, safe_weight, neutral_weight) — sums to 1.0
    """
    if config is None:
        config = RiskConfig()

    rw = config.risky_weight_base
    sw = config.safe_weight_base
    nw = config.neutral_weight_base

    # Regime adjustments
    if regime in ("trending_up", "volatile_up"):
        rw += config.bullish_adjust
        sw -= config.bullish_adjust * 0.7
    elif regime in ("trending_down", "volatile_down"):
        sw += abs(config.bearish_adjust)
        rw += config.bearish_adjust  # negative → reduces risky

    if regime in ("volatile", "volatile_up", "volatile_down"):
        sw += abs(config.volatile_adjust)
        rw += config.volatile_adjust  # negative → reduces risky

    # Momentum boost
    momentum = signal.get("momentum", 2)
    if regime in ("trending_up", "volatile_up") and momentum >= 2:
        mb = config.momentum_boost_max * (momentum / 3.0)
        rw += mb
        sw -= mb * 0.5
        nw -= mb * 0.5

    # Debate integration: positive net_score → shift toward risky
    debate_shift = config.debate_weight * debate_net_score
    rw += debate_shift
    sw -= debate_shift * 0.5
    nw -= debate_shift * 0.5

    # Clamp and normalize
    rw = max(0.05, min(0.70, rw))
    sw = max(0.05, min(0.70, sw))
    nw = max(0.05, min(0.70, nw))

    total = rw + sw + nw
    rw /= total
    sw /= total
    nw /= total

    return round(rw, 3), round(sw, 3), round(nw, 3)


# ══════════════════════════════════════════════════════════════════════════════
# Sizing Engine
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class RiskSizingResult:
    """Blended position size from 3 perspectives."""
    risky_size: float
    safe_size: float
    neutral_size: float
    blended_size: float        # Final: weighted blend of all 3
    risky_weight: float
    safe_weight: float
    neutral_weight: float
    regime: str
    posture_label: str          # "AGGRESSIVE", "BALANCED", or "CONSERVATIVE"


def size_position(
    signal: dict,
    contract: dict,
    bankroll: float,
    debate_net_score: float = 0,
    config: Optional[RiskConfig] = None,
    cal_factor: float = 0.5,
    certainty: float = 0.5,
) -> RiskSizingResult:
    """
    3-perspective position sizing.

    Args:
        signal: btc_signal() output
        contract: discovered contract with up_price/down_price
        bankroll: available capital
        debate_net_score: from fdc_debate.debate() — bull_score - bear_score
        config: RiskConfig
        cal_factor: Bayesian calibration factor
        certainty: Bayesian certainty

    Returns:
        RiskSizingResult with all 3 perspectives + blended size
    """
    if config is None:
        config = RiskConfig()

    direction = signal.get("direction", "up")
    confidence = signal.get("confidence", 0)
    entry_price = contract.get("up_price") if direction == "up" else contract.get("down_price", 0.5)
    edge = max(0, confidence - entry_price)
    odds = 1.0 - entry_price
    momentum = signal.get("momentum", 2)
    rsi = signal.get("rsi", 50)

    regime = detect_regime(signal)
    rw, sw, nw = compute_posture_weights(signal, regime, debate_net_score, config)

    # ── Kelly base ──
    kelly_fraction = edge / max(odds, 0.01) if edge > 0 else 0

    # ── Risky perspective ──
    # Full Kelly × conviction boost. High momentum + aligned trend = more.
    risky_k = kelly_fraction * config.risky_kelly_mult * cal_factor * certainty
    momentum_bonus = 1.0 + (momentum / 3.0) * 0.3 if regime in ("trending_up", "volatile_up") else 1.0
    risky_size = min(bankroll * config.max_position_pct, bankroll * risky_k * momentum_bonus)
    risky_size = max(config.min_position, risky_size)

    # ── Safe perspective ──
    # 0.25× Kelly. Hard DD cap. Ignores momentum.
    safe_k = kelly_fraction * config.safe_kelly_mult * cal_factor * certainty
    safe_size = min(bankroll * 0.03, bankroll * safe_k)  # 3% hard cap for safe
    safe_size = max(config.min_position * 0.5, safe_size)

    # ── Neutral perspective ──
    # 0.75× Kelly. Balanced. Vol-aware.
    neutral_k = kelly_fraction * config.neutral_kelly_mult * cal_factor * certainty
    neutral_size = min(bankroll * 0.06, bankroll * neutral_k)
    neutral_size = max(config.min_position, neutral_size)

    # ── Blend ──
    blended = risky_size * rw + safe_size * sw + neutral_size * nw
    blended = min(bankroll * config.max_position_pct, blended)
    blended = max(config.min_position, blended)

    # Posture label
    if rw > 0.45:
        posture = "AGGRESSIVE"
    elif sw > 0.45:
        posture = "CONSERVATIVE"
    else:
        posture = "BALANCED"

    return RiskSizingResult(
        risky_size=round(risky_size, 2),
        safe_size=round(safe_size, 2),
        neutral_size=round(neutral_size, 2),
        blended_size=round(blended, 2),
        risky_weight=rw,
        safe_weight=sw,
        neutral_weight=nw,
        regime=regime,
        posture_label=posture,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Quick Test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Trending up signal — should be AGGRESSIVE
    sig_up = {"direction": "up", "confidence": 0.85, "rsi": 28, "macd": 120,
              "momentum": 3, "price": 79200, "sma20": 78800,
              "_prices": [79000]*15 + [79100, 79150, 79200, 79180, 79200]}
    contract = {"up_price": 0.16, "down_price": 0.84, "mins_to_expiry": 12}

    print("=== Trending UP (strong signal) ===")
    r = size_position(sig_up, contract, bankroll=250, debate_net_score=0.35)
    print(f"Regime: {r.regime} | Posture: {r.posture_label}")
    print(f"Weights: Risky={r.risky_weight:.0%} Safe={r.safe_weight:.0%} Neutral={r.neutral_weight:.0%}")
    print(f"Sizes: Risky=${r.risky_size:.2f} Safe=${r.safe_size:.2f} Neutral=${r.neutral_size:.2f}")
    print(f"Blended: ${r.blended_size:.2f}")

    # Weak counter-trend — should be CONSERVATIVE
    sig_weak = {"direction": "up", "confidence": 0.20, "rsi": 55, "macd": -180,
                "momentum": 1, "price": 78500, "sma20": 79200,
                "_prices": [79200, 79000, 78900, 78700, 78500]}
    print("\n=== Trending DOWN (counter-trend signal) ===")
    r2 = size_position(sig_weak, contract, bankroll=250, debate_net_score=-0.25)
    print(f"Regime: {r2.regime} | Posture: {r2.posture_label}")
    print(f"Weights: Risky={r2.risky_weight:.0%} Safe={r2.safe_weight:.0%} Neutral={r2.neutral_weight:.0%}")
    print(f"Blended: ${r2.blended_size:.2f}")

    # Strong bear trend — momentum down
    sig_down = {"direction": "down", "confidence": 0.78, "rsi": 72, "macd": -350,
                "momentum": 0, "price": 78300, "sma20": 79000,
                "_prices": [79000, 78800, 78600, 78400, 78300]}
    print("\n=== Trending DOWN (aligned signal) ===")
    r3 = size_position(sig_down, contract, bankroll=250, debate_net_score=0.20)
    print(f"Regime: {r3.regime} | Posture: {r3.posture_label}")
    print(f"Blended: ${r3.blended_size:.2f}")
