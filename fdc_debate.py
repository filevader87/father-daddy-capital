#!/usr/bin/env python3
"""
FDC Adversarial Debate Layer
TradingAgents principle: forced bull/bear counterargument before every trade.
Eliminates confirmation bias — the primary cause of trending_down bleed.

Heuristic implementation (no LLM API needed):
  Bull case: aggregates positive indicators from signal stack
  Bear case: aggregates negative/risk indicators
  Debate score: Bull - Bear → modifies entry confidence

Author: Hugh (3rd of 5)
Source: TradingAgents (arXiv:2412.20138v7) — Bull/Bear Researcher architecture
Date: 2026-05-15
"""

from __future__ import annotations
from typing import Dict, Optional, Tuple
from dataclasses import dataclass, field
import math


# ══════════════════════════════════════════════════════════════════════════════
# Debate Configuration
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class DebateConfig:
    """Scoring weights for bull/bear argument components."""

    # Bull components (positive signals)
    rsi_oversold_weight: float = 0.25     # RSI < 35 = bullish
    macd_positive_weight: float = 0.20    # MACD > 0 = bullish
    trend_above_sma_weight: float = 0.20  # Price > 20-SMA = bullish
    momentum_up_weight: float = 0.15      # 3-candle momentum = bullish
    volume_expanding_weight: float = 0.10 # Volume spike confirms
    edge_strength_weight: float = 0.10    # Raw edge size = bullish

    # Bear components (risk / negative signals)
    rsi_overbought_weight: float = 0.20   # RSI > 70 = bearish
    macd_negative_weight: float = 0.15    # MACD < 0 = bearish
    trend_below_sma_weight: float = 0.20  # Price < 20-SMA = bearish
    volatility_penalty: float = 0.15      # High vol = risk
    time_pressure_weight: float = 0.15    # Near expiry = risky
    signal_divergence_weight: float = 0.15 # Direction vs trend mismatch

    # Thresholds
    min_bull_score: float = 0.30          # Minimum bull score to proceed
    max_bear_score: float = 0.50          # Maximum bear score before blocking
    confidence_modifier_max: float = 0.30 # Max confidence adjustment from debate


# ══════════════════════════════════════════════════════════════════════════════
# Debate Engine
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class DebateResult:
    """Outcome of bull/bear deliberation."""
    bull_score: float          # 0-1, higher = stronger bull case
    bear_score: float          # 0-1, higher = stronger bear case
    net_score: float           # Bull - Bear, -1 to +1
    verdict: str               # "ENTER", "SKIP", or "REDUCE"
    confidence_modifier: float # How much to adjust entry confidence
    bull_reasons: list[str]    # Specific reasons for the bull case
    bear_reasons: list[str]    # Specific reasons for the bear case


def debate(
    signal: dict,
    contract: dict,
    config: Optional[DebateConfig] = None,
) -> DebateResult:
    """
    Run adversarial debate on a trade candidate.

    Args:
        signal: btc_signal() output — direction, confidence, rsi, macd, momentum, prices
        contract: discovered contract — up_price, down_price, mins_to_expiry, volume
        config: DebateConfig (uses defaults if None)

    Returns:
        DebateResult with scores, verdict, and rationale
    """
    if config is None:
        config = DebateConfig()

    direction = signal.get("direction", "neutral")
    confidence = signal.get("confidence", 0)
    rsi = signal.get("rsi", 50)
    macd = signal.get("macd", 0)
    price = signal.get("price", 0)
    momentum = signal.get("momentum", 2)  # 0-3 candles up
    sma20 = signal.get("sma20", price)
    prices = signal.get("_prices", [])

    # Compute volatility from recent candles
    volatility = 0.02
    if len(prices) >= 10:
        returns = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, min(11, len(prices)))]
        volatility = sum(abs(r) for r in returns) / len(returns)

    mins = contract.get("mins_to_expiry", 15)
    entry_price = contract.get("up_price") if direction == "up" else contract.get("down_price", 0.5)
    edge = confidence - entry_price
    volume = contract.get("volume", 0)

    # ── BULL CASE ──────────────────────────────────────────────────────

    bull_score = 0.0
    bull_reasons = []

    # RSI oversold → bullish for UP, bearish for DOWN
    if direction == "up" and rsi < 35:
        bull_score += config.rsi_oversold_weight
        bull_reasons.append(f"RSI oversold ({rsi:.0f}) — reversal potential")
    elif direction == "down" and rsi > 65:
        bull_score += config.rsi_overbought_weight * 0.7
        bull_reasons.append(f"RSI overbought ({rsi:.0f}) — mean reversion down")

    # MACD alignment
    if (direction == "up" and macd > 0) or (direction == "down" and macd < 0):
        bull_score += config.macd_positive_weight
        bull_reasons.append(f"MACD aligned ({macd:+.1f}) — trend confirms direction")

    # Price vs SMA
    if (direction == "up" and price > sma20) or (direction == "down" and price < sma20):
        bull_score += config.trend_above_sma_weight
        bull_reasons.append(f"Price vs 20-SMA confirms trend")

    # Momentum
    if (direction == "up" and momentum >= 2) or (direction == "down" and momentum <= 1):
        bull_score += config.momentum_up_weight
        bull_reasons.append(f"Momentum ({momentum}/3 candles) supports {direction}")

    # Volume expanding (confirms signal)
    if len(prices) >= 10:
        recent_range = max(prices[-5:]) - min(prices[-5:])
        prior_range = max(prices[-10:-5]) - min(prices[-10:-5])
        if recent_range > prior_range * 1.3:
            bull_score += config.volume_expanding_weight
            bull_reasons.append("Volume expanding — signal strength confirmed")

    # Edge strength
    if edge > 0.05:
        bull_score += config.edge_strength_weight * min(1.0, edge / 0.3)
        bull_reasons.append(f"Edge {edge:.3f} — significant market mispricing")
    elif edge > 0:
        bull_score += config.edge_strength_weight * 0.3

    # ── BEAR CASE ──────────────────────────────────────────────────────

    bear_score = 0.0
    bear_reasons = []

    # RSI extremes
    if direction == "up" and rsi > 70:
        bear_score += config.rsi_overbought_weight
        bear_reasons.append(f"RSI overbought ({rsi:.0f}) — may reverse against UP")
    elif direction == "down" and rsi < 30:
        bear_score += config.rsi_oversold_weight * 0.8
        bear_reasons.append(f"RSI oversold ({rsi:.0f}) — may bounce against DOWN")

    # MACD divergence
    if (direction == "up" and macd < 0) or (direction == "down" and macd > 0):
        bear_score += config.macd_negative_weight
        bear_reasons.append(f"MACD divergence ({macd:+.1f}) — fights {direction} signal")

    # Price vs SMA divergence
    if (direction == "up" and price < sma20) or (direction == "down" and price > sma20):
        bear_score += config.trend_below_sma_weight
        bear_reasons.append(f"Price vs 20-SMA diverges from {direction} signal — countertrend risk")

    # High volatility
    if volatility > 0.03:
        bear_score += config.volatility_penalty * min(1.0, volatility / 0.05)
        bear_reasons.append(f"Volatility {volatility:.1%} — elevated noise")

    # Time pressure (short expiry = less time to be right)
    if mins < 15:
        bear_score += config.time_pressure_weight * (1 - mins / 15)
        bear_reasons.append(f"Expiry {mins:.0f}m — limited time for signal to resolve")

    # Signal/trend divergence
    trend_ok = (direction == "up" and price > sma20) or (direction == "down" and price < sma20)
    if not trend_ok and abs(macd) > 50:
        bear_score += config.signal_divergence_weight
        bear_reasons.append("Signal direction fights established trend")

    # Contract price extreme
    if entry_price < 0.03 or entry_price > 0.90:
        bear_score += 0.05
        bear_reasons.append(f"Contract price {entry_price:.3f} — extreme, may be illiquid")

    # ── VERDICT ────────────────────────────────────────────────────────

    bull_score = min(1.0, bull_score)
    bear_score = min(1.0, bear_score)
    net_score = bull_score - bear_score

    # Decision
    if bear_score > config.max_bear_score and bull_score < config.min_bull_score:
        verdict = "SKIP"
        modifier = -0.30
    elif bear_score > config.max_bear_score:
        verdict = "REDUCE"
        modifier = -config.confidence_modifier_max
    elif bull_score < config.min_bull_score:
        verdict = "SKIP"
        modifier = -0.20
    elif net_score > 0.15:
        verdict = "ENTER"
        modifier = min(config.confidence_modifier_max, net_score * 0.3)
    elif net_score > -0.10:
        verdict = "REDUCE"
        modifier = net_score * 0.2
    else:
        verdict = "SKIP"
        modifier = -0.15

    return DebateResult(
        bull_score=round(bull_score, 3),
        bear_score=round(bear_score, 3),
        net_score=round(net_score, 3),
        verdict=verdict,
        confidence_modifier=round(modifier, 3),
        bull_reasons=bull_reasons,
        bear_reasons=bear_reasons,
    )


def debate_summary(result: DebateResult) -> str:
    """One-line debate summary."""
    bull_badges = ", ".join(result.bull_reasons[:2])
    bear_badges = ", ".join(result.bear_reasons[:2])
    icon = {"ENTER": "🟢", "REDUCE": "🟡", "SKIP": "🔴"}.get(result.verdict, "❓")
    return (
        f"{icon} {result.verdict} | Bull:{result.bull_score:.2f} Bear:{result.bear_score:.2f} "
        f"Net:{result.net_score:+.2f} | +:{bull_badges} | -:{bear_badges}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Quick Test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Strong UP signal — should pass debate
    sig_up = {"direction": "up", "confidence": 0.85, "rsi": 28, "macd": 120,
              "momentum": 3, "price": 79200, "sma20": 78800,
              "_prices": [79000]*15 + [79100, 79150, 79200, 79180, 79200]}
    contract = {"up_price": 0.16, "down_price": 0.84, "mins_to_expiry": 12, "volume": 500000}

    print("=== Strong UP Signal ===")
    r = debate(sig_up, contract)
    print(debate_summary(r))

    # Weak counter-trend signal — should be blocked
    sig_weak = {"direction": "up", "confidence": 0.20, "rsi": 55, "macd": -180,
                "momentum": 1, "price": 78500, "sma20": 79200,
                "_prices": [79200, 79000, 78900, 78700, 78500]}
    print("\n=== Weak Counter-Trend ===")
    r2 = debate(sig_weak, contract)
    print(debate_summary(r2))

    # Strong DOWN in bear market — should pass
    sig_down = {"direction": "down", "confidence": 0.78, "rsi": 72, "macd": -350,
                "momentum": 0, "price": 78300, "sma20": 79000,
                "_prices": [79000, 78800, 78600, 78400, 78300]}
    print("\n=== Strong DOWN (bear trend) ===")
    r3 = debate(sig_down, contract)
    print(debate_summary(r3))
