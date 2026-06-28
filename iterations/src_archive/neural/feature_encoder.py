#!/usr/bin/env python3
"""
FDC — Enhanced Feature Encoder (CCXT-integrated)
=================================================
Extended 12-dim feature vector with CCXT-sourced data.

New features (replacing on-chain placeholder):
  8  funding_rate:     Perpetual futures funding rate (sentiment proxy)
  9  orderbook_skew:   Real bid/ask imbalance from exchange orderbook
  10 cross_exchange_disp: Price divergence across exchanges
  11 volume_weighted_trend: Volume-weighted aggregate trend

Plus updated macro_correlation() using CCXT BTC 1h candles.
"""

import numpy as np
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

OUTPUT_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/output")

N_FEATURES = 12

# ─── BTC Price Features ──────────────────────────────────────────────────────

def compute_btc_momentum(prices_5m: list[float]) -> float:
    if len(prices_5m) < 7:
        return 0.0
    current = prices_5m[-1]
    prior = prices_5m[-7]
    if prior == 0:
        return 0.0
    raw = (current - prior) / prior
    return float(np.clip(raw * 50, -1.0, 1.0))

def compute_btc_volatility(prices_5m: list[float]) -> float:
    if len(prices_5m) < 10:
        return 0.0
    recent_range = max(prices_5m[-5:]) - min(prices_5m[-5:])
    prior_range = max(prices_5m[-10:-5]) - min(prices_5m[-10:-5])
    if prior_range == 0:
        return 0.0 if recent_range == 0 else 1.0
    ratio = recent_range / prior_range
    return float(np.clip(ratio - 1.0, -1.0, 1.0))

def compute_btc_rsi_norm(prices_5m: list[float]) -> float:
    if len(prices_5m) < 8:
        return 0.0
    deltas = [prices_5m[i] - prices_5m[i-1] for i in range(1, len(prices_5m))]
    gains = sum(max(d, 0) for d in deltas[-7:]) / 7
    losses = sum(max(-d, 0) for d in deltas[-7:]) / 7
    rs = gains / max(losses, 1e-9)
    rsi = 100 - (100 / (1 + rs))
    return float(np.clip((rsi - 50) / 30, -1.0, 1.0))


# ─── Polymarket Features ─────────────────────────────────────────────────────

def compute_pm_orderbook_skew(yes_price: float, no_price: float,
                               volume_24h: float, avg_volume: float = 50000) -> float:
    raw_skew = (yes_price - 0.5) * 2.0
    vol_weight = min(2.0, volume_24h / max(avg_volume, 1.0))
    return float(np.clip(raw_skew * vol_weight, -1.0, 1.0))

def compute_pm_volume_anomaly(volume_24h: float, volumes_history: list[float]) -> float:
    if len(volumes_history) < 5:
        return 0.0
    mean = np.mean(volumes_history[-10:])
    std = np.std(volumes_history[-10:])
    if std == 0:
        return 0.0
    z = (volume_24h - mean) / std
    return float(np.clip(z / 3.0, -1.0, 1.0))


# ─── FDC Internal Signal ─────────────────────────────────────────────────────

def compute_scalp_aggregate() -> float:
    state_path = OUTPUT_DIR / "paper_state.json"
    if not state_path.exists():
        return 0.0
    try:
        state = json.loads(state_path.read_text())
    except (json.JSONDecodeError, IOError):
        return 0.0

    scalp = state.get("scalp_positions", {})
    if not scalp:
        return 0.0
    total_score = 0.0
    count = 0
    for pos in scalp.values():
        direction = pos.get("direction", "LONG")
        score = pos.get("signal_score", 0.0)
        total_score += score if direction == "LONG" else -score
        count += 1
    if count == 0:
        return 0.0
    return float(np.clip(total_score / count / 0.8, -1.0, 1.0))


# ─── Macro Correlation (CCXT-powered) ───────────────────────────────────────

def compute_macro_correlation(ccxt_provider=None) -> float:
    """
    BTC/SPY correlation using CCXT for BTC 1h candles.
    Falls back to yfinance if CCXT unavailable.
    """
    try:
        if ccxt_provider is not None:
            import asyncio
            loop = asyncio.get_event_loop()
            if not loop.is_running():
                btc_df = loop.run_until_complete(ccxt_provider.get_btc_1h())
                spy_df = loop.run_until_complete(ccxt_provider.get_spy_1h())
                if len(btc_df) >= 10 and len(spy_df) >= 10:
                    btc_ret = btc_df['close'].pct_change().dropna().values[-40:]
                    spy_ret = spy_df['Close'].pct_change().dropna().values[-40:]
                    min_len = min(len(btc_ret), len(spy_ret))
                    if min_len >= 5:
                        corr = np.corrcoef(btc_ret[-min_len:], spy_ret[-min_len:])[0, 1]
                        return float(np.clip(corr, -1.0, 1.0)) if not np.isnan(corr) else 0.0
    except Exception:
        pass

    # Fallback: yfinance
    try:
        import yfinance as yf
        btc = yf.Ticker("BTC-USD").history(period="5d", interval="1h")
        spy = yf.Ticker("SPY").history(period="5d", interval="1h")
        if len(btc) >= 10 and len(spy) >= 10:
            btc_ret = btc['Close'].pct_change().dropna().values[-40:]
            spy_ret = spy['Close'].pct_change().dropna().values[-40:]
            min_len = min(len(btc_ret), len(spy_ret))
            if min_len >= 5:
                corr = np.corrcoef(btc_ret[-min_len:], spy_ret[-min_len:])[0, 1]
                return float(np.clip(corr, -1.0, 1.0)) if not np.isnan(corr) else 0.0
    except Exception:
        pass
    return 0.0


# ─── Time Decay ──────────────────────────────────────────────────────────────

def compute_time_decay(hours_to_resolution: float) -> float:
    if hours_to_resolution <= 0:
        return 1.0
    if hours_to_resolution < 6:
        return float(1.0 - hours_to_resolution / 6.0 * 0.7)
    elif hours_to_resolution < 48:
        return float(0.3 - (hours_to_resolution - 6) / 42.0 * 0.25)
    else:
        return 0.05


# ─── Encoder ─────────────────────────────────────────────────────────────────

class FeatureEncoder:
    """12-dim Bayesian feature encoder with CCXT integration."""

    def __init__(self, calibrator=None, ccxt_provider=None):
        self.calibrator = calibrator
        self.ccxt = ccxt_provider
        self._volume_history: list[float] = []

    def encode(
        self,
        btc_prices_5m: list[float],
        contract_yes_price: float,
        contract_no_price: float,
        contract_volume: float,
        hours_to_resolution: float,
        funding_rate: float = 0.0,
        orderbook_data: Optional[dict] = None,
        cross_exchange_disp: float = 0.0,
        volume_weighted_trend: float = 0.0,
    ) -> np.ndarray:
        """12-dim feature vector with CCXT-sourced fields."""
        self._volume_history.append(contract_volume)
        if len(self._volume_history) > 50:
            self._volume_history = self._volume_history[-50:]

        features = np.zeros(N_FEATURES, dtype=float)

        features[0] = compute_btc_momentum(btc_prices_5m)
        features[1] = compute_btc_volatility(btc_prices_5m)
        features[2] = compute_btc_rsi_norm(btc_prices_5m)
        features[3] = compute_pm_orderbook_skew(contract_yes_price, contract_no_price, contract_volume)
        features[4] = compute_pm_volume_anomaly(contract_volume, self._volume_history)
        features[5] = compute_scalp_aggregate()
        features[6] = compute_macro_correlation(self.ccxt)
        features[7] = compute_time_decay(hours_to_resolution)

        # CCXT-sourced features
        features[8] = float(np.clip(funding_rate, -1.0, 1.0))
        features[9] = float(np.clip(
            orderbook_data.get('skew', 0.0) if orderbook_data else 0.0, -1.0, 1.0))
        features[10] = float(np.clip(cross_exchange_disp / 5.0, 0.0, 1.0))
        features[11] = float(np.clip(volume_weighted_trend, -1.0, 1.0))

        return np.clip(features, -1.0, 1.0)


# ─── Kelly Sizer ─────────────────────────────────────────────────────────────

def kelly_sizer(
    edge: float, odds: float, bankroll: float,
    calibration_factor: float, certainty: float,
    max_bankroll_fraction: float = 0.02, min_bet: float = 5.0,
) -> float:
    if edge <= 0 or odds <= 0 or bankroll <= 0:
        return 0.0
    raw_kelly = edge / max(odds, 0.01)
    safe_kelly = raw_kelly * 0.5
    adjusted_fraction = safe_kelly * calibration_factor * certainty
    capped_fraction = min(adjusted_fraction, max_bankroll_fraction)
    position = capped_fraction * bankroll
    return max(min_bet, round(position, 2)) if position >= min_bet else 0.0
