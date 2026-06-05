"""
V20 Market Regime Classifier

Classifies each candidate into one of 8 structural regimes based on
microstructure features — NOT RSI alone.

Regimes:
  trend_continuation   — velocity + SMA aligned, making new extremes
  trend_exhaustion     — velocity decelerating, SMA still trending
  panic_sell           — extreme velocity down, bid collapse, widening spread
  balanced_rotation    — near SMA, low velocity, compressing spread
  volatility_expansion — spread widening, velocity accelerating
  volatility_compression — spread narrowing, velocity decelerating
  fake_reversal        — RSI bounce but no microstructure confirmation
  liquidity_vacuum     — thin books, large imbalance, no depth

Priority order for features:
  1. Market structure (SMA, velocity direction)
  2. Orderbook transition (bid/ask changes)
  3. Velocity transition (acceleration/deceleration)
  4. Reversal confirmation (multi-signal)
  5. Regime classification (this module)
  6. RSI context (supplementary only)
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any


class Regime(str, Enum):
    TREND_CONTINUATION = "trend_continuation"
    TREND_EXHAUSTION = "trend_exhaustion"
    PANIC_SELL = "panic_sell"
    BALANCED_ROTATION = "balanced_rotation"
    VOLATILITY_EXPANSION = "volatility_expansion"
    VOLATILITY_COMPRESSION = "volatility_compression"
    FAKE_REVERSAL = "fake_reversal"
    LIQUIDITY_VACUUM = "liquidity_vacuum"


REGIME_LABELS = {r.value for r in Regime}

# Regimes that BLOCK trading
BLOCKED_REGIMES = {Regime.TREND_CONTINUATION, Regime.FAKE_REVERSAL}

# Regimes that ALLOW BTC_BALANCED_REVERSAL_V1
ALLOWED_REGIMES = {
    Regime.BALANCED_ROTATION,
    Regime.VOLATILITY_COMPRESSION,
    Regime.TREND_EXHAUSTION,
}


@dataclass
class RegimeResult:
    regime: Regime
    confidence: float          # 0-1
    features: Dict[str, Any]   # feature values used
    blocked: bool              # True if regime blocks trading
    reason: str                # Human-readable classification reason


def classify_regime(
    asset: str,
    spot_price: float,
    spot_velocity_5s: Optional[float] = None,
    spot_velocity_15s: Optional[float] = None,
    spot_velocity_30s: Optional[float] = None,
    RSI: Optional[float] = None,
    RSI_slope: Optional[float] = None,
    SMA20: Optional[float] = None,
    SMA20_slope: Optional[float] = None,
    spread: Optional[float] = None,
    spread_change: Optional[float] = None,
    bid_depth: Optional[float] = None,
    ask_depth: Optional[float] = None,
    bid_depth_change: Optional[float] = None,
    ask_depth_change: Optional[float] = None,
    imbalance: Optional[float] = None,
    imbalance_change: Optional[float] = None,
    book_depth_total: Optional[float] = None,
    lower_low_count: int = 0,
    higher_low_count: int = 0,
    price_vs_reference_pct: Optional[float] = None,
    time_to_expiry_minutes: Optional[float] = None,
    transition_score: Optional[float] = None,
) -> RegimeResult:
    """
    Classify market regime from microstructure features.
    
    Priority: structure → orderbook → velocity → reversal → regime → RSI context
    Returns RegimeResult with classification, confidence, features, and block status.
    """
    features = {
        "asset": asset,
        "spot_price": spot_price,
        "velocity_5s": spot_velocity_5s,
        "velocity_15s": spot_velocity_15s,
        "velocity_30s": spot_velocity_30s,
        "RSI": RSI,
        "RSI_slope": RSI_slope,
        "SMA20": SMA20,
        "SMA20_slope": SMA20_slope,
        "spread": spread,
        "spread_change": spread_change,
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "bid_depth_change": bid_depth_change,
        "ask_depth_change": ask_depth_change,
        "imbalance": imbalance,
        "imbalance_change": imbalance_change,
        "book_depth_total": book_depth_total,
        "lower_low_count": lower_low_count,
        "higher_low_count": higher_low_count,
        "price_vs_reference_pct": price_vs_reference_pct,
        "time_to_expiry_minutes": time_to_expiry_minutes,
        "transition_score": transition_score,
    }

    # ── Feature extraction with safe defaults ──
    # §9: Lower thresholds for regime sensitivity — use 10x lower velocity thresholds
    # because crypto 5m candle velocities are typically 0.0001-0.003, not 0.003+
    vel_5 = spot_velocity_5s or 0.0
    vel_15 = spot_velocity_15s or 0.0
    vel_30 = spot_velocity_30s or 0.0
    sma_slope = SMA20_slope or 0.0
    rsi_slope = RSI_slope or 0.0
    spread_val = spread or 0.0
    spread_chg = spread_change or 0.0
    bid_d = bid_depth or 0.0
    ask_d = ask_depth or 0.0
    bid_chg = bid_depth_change or 0.0
    ask_chg = ask_depth_change or 0.0
    imb = imbalance or 0.0
    imb_chg = imbalance_change or 0.0
    depth = book_depth_total or 0.0
    ll_count = lower_low_count
    hl_count = higher_low_count
    ref_pct = price_vs_reference_pct or 0.0
    t_trans = transition_score or 0.0

    # Price relative to SMA
    above_sma = SMA20 is not None and spot_price > SMA20
    below_sma = SMA20 is not None and spot_price < SMA20
    near_sma = SMA20 is not None and abs(spot_price - SMA20) / max(SMA20, 1e-9) < 0.01
    
    # §9: Accelerating vs decelerating — velocity changes direction
    vel_accelerating_down = vel_30 < vel_15 and vel_15 < 0
    vel_accelerating_up = vel_30 > vel_15 and vel_15 > 0
    vel_decelerating = abs(vel_15) < abs(vel_30)  # slowing down

    # ── Regime classification hierarchy ──

    # 1. PANIC_SELL: strong negative velocity + bid collapse + widening spread
    # §9: Relaxed thresholds — crypto 5m velocities are 0.0001-0.003
    if vel_15 < -0.0005 and vel_30 < -0.0003 and (bid_chg < -0.1 or spread_chg > 0.05 or vel_15 < -0.001):
        return RegimeResult(
            regime=Regime.PANIC_SELL,
            confidence=min(0.9, abs(vel_15) * 100 + abs(bid_chg)),
            features=features,
            blocked=True,
            reason=f"PANIC: vel15={vel_15:.4f} vel30={vel_30:.4f} bid_chg={bid_chg:.2f} spread_chg={spread_chg:.2f}",
        )

    # 2. LIQUIDITY_VACUUM: thin books, large imbalance, no depth
    # §9: Relaxed — depth < 200 is thin for BTC
    if depth < 200 and abs(imb) > 0.2 and spread_val > 0.01:
        return RegimeResult(
            regime=Regime.LIQUIDITY_VACUUM,
            confidence=min(0.85, abs(imb) + (1 - depth / 100)),
            features=features,
            blocked=True,
            reason=f"LIQUIDITY_VACUUM: depth={depth:.0f} imbalance={imb:.2f} spread={spread_val:.4f}",
        )

    # 3. TREND_CONTINUATION: velocity aligned with SMA trend + making extremes
    # §9: Relaxed — vel thresholds 0.0001 instead of 0.001
    if sma_slope < -0.0001 and vel_15 < -0.0001 and vel_30 < -0.0001 and ll_count >= 2:
        return RegimeResult(
            regime=Regime.TREND_CONTINUATION,
            confidence=min(0.85, abs(sma_slope) * 50 + ll_count * 0.15),
            features=features,
            blocked=True,
            reason=f"TREND_CONTINUATION: sma_slope={sma_slope:.4f} vel15={vel_15:.4f} vel30={vel_30:.4f} ll={ll_count}",
        )
    if sma_slope > 0.0001 and vel_15 > 0.0001 and vel_30 > 0.0001 and hl_count >= 2:
        return RegimeResult(
            regime=Regime.TREND_CONTINUATION,
            confidence=min(0.85, abs(sma_slope) * 50 + hl_count * 0.15),
            features=features,
            blocked=True,
            reason=f"TREND_CONTINUATION(up): sma_slope={sma_slope:.4f} vel15={vel_15:.4f} hl={hl_count}",
        )

    # 4. FAKE_REVERSAL: RSI shows bounce but no microstructure confirmation
    # §9: RSI < 40 (was 35) — catches more deceptive bounces
    rsi_oversold = RSI is not None and RSI < 40
    no_microstructure_reversal = (
        (t_trans <= 0) and  # transition score not bullish
        (vel_15 <= 0 or vel_30 <= 0) and  # velocity not recovering
        (bid_chg <= 0)  # bids not strengthening
    )
    if rsi_oversold and no_microstructure_reversal and below_sma:
        return RegimeResult(
            regime=Regime.FAKE_REVERSAL,
            confidence=min(0.8, (35 - (RSI or 35)) / 35 + abs(sma_slope) * 20),
            features=features,
            blocked=True,
            reason=f"FAKE_REVERSAL: RSI={RSI:.1f} transition={t_trans:.3f} vel={vel_15:.4f} bid_chg={bid_chg:.2f}",
        )

    # 5. VOLATILITY_EXPANSION: spread widening + velocity accelerating
    # §9: Relaxed — 0.05 spread change threshold instead of 0.15
    if spread_chg > 0.05 and (abs(vel_15) > 0.0001 or abs(vel_30) > 0.0001):
        direction = "up" if vel_15 > 0 else "down"
        return RegimeResult(
            regime=Regime.VOLATILITY_EXPANSION,
            confidence=min(0.75, spread_chg + abs(vel_15) * 20),
            features=features,
            blocked=False,  # not blocked but requires extra caution
            reason=f"VOLATILITY_EXPANSION({direction}): spread_chg={spread_chg:.3f} vel15={vel_15:.4f}",
        )

    # 6. VOLATILITY_COMPRESSION: spread narrowing + velocity low
    # §9: Relaxed — 0.02 spread change and 0.0001 velocity
    if spread_chg < -0.02 and abs(vel_15) < 0.001:
        return RegimeResult(
            regime=Regime.VOLATILITY_COMPRESSION,
            confidence=min(0.7, abs(spread_chg) + (1 - abs(vel_15) * 500)),
            features=features,
            blocked=False,
            reason=f"VOLATILITY_COMPRESSION: spread_chg={spread_chg:.3f} vel15={vel_15:.4f}",
        )

    # 7. TREND_EXHAUSTION: velocity turning while SMA still trending
    # §9: Relaxed — detect when vel turns against SMA direction
    if abs(sma_slope) > 0.00005 and vel_decelerating and abs(vel_15) < abs(vel_30) * 0.5:
        # Down trend but velocity turning → exhaustion
        return RegimeResult(
            regime=Regime.TREND_EXHAUSTION,
            confidence=min(0.7, abs(sma_slope) * 30 + (vel_15 - vel_30) * 100),
            features=features,
            blocked=False,
            reason=f"TREND_EXHAUSTION: sma_slope={sma_slope:.4f} vel15={vel_15:.4f} vel30={vel_30:.4f}",
        )
    # Also: velocity opposing SMA direction (exhaustion pattern)

    # 8. BALANCED_ROTATION: default — near SMA, low velocity, manageable spread
    return RegimeResult(
        regime=Regime.BALANCED_ROTATION,
        confidence=0.5,  # default confidence
        features=features,
        blocked=False,
        reason=f"BALANCED_ROTATION: default regime (no strong directional signals)",
    )