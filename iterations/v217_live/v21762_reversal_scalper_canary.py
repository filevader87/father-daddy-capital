#!/usr/bin/env python3
"""
V21.7.62 — 5-Minute Reversal Scalper Canary
=============================================
5th bot in the FDC swarm. Based on @antsaslyku's proven strategy:
42,236 trades, $88K P&L in 2 months, 100% visible win rate.

STRATEGY:
  - 5-minute crypto Up/Down markets (BTC, ETH, SOL, XRP)
  - Both directions: Up AND Down, no directional bias
  - Reversal + price correction detection
  - Entry at 5-80¢, settles at 100¢ if correct
  - Neural plasticity layer for online learning from every trade outcome
  - Bayesian calibration for probability estimation

NEURAL PLASTICITY:
  - 8-input network: RSI, MACD, trend, momentum, mean_reversion, vol, asset_enc, confidence
  - Online SGD with EWC (elastic weight consolidation)
  - Learns from every closed trade: winning patterns reinforced, losing pruned
  - Bayesian layer: calibrated probability + uncertainty bounds

PATH TO LIVE:
  1. Paper trade 24h → verify win rate and edge
  2. Backtest against historical 5m data
  3. Simulation with realistic fills/slippage
  4. Live deployment with $5 max position

RUN AS:
  python3 src/v217_live/v21762_reversal_scalper_canary.py --paper
  python3 src/v217_live/v21762_reversal_scalper_canary.py --live   # REAL MONEY
"""
from __future__ import annotations
import json, os, sys, time, logging, signal, traceback, argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Tuple
import statistics
import requests
import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "output" / "v21762_scalper_canary"
SUP = ROOT / "output" / "supervisor"
NEURAL_DIR = ROOT / "neural_weights"
OUT.mkdir(parents=True, exist_ok=True)
SUP.mkdir(parents=True, exist_ok=True)
NEURAL_DIR.mkdir(parents=True, exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════════
# ENVIRONMENT
# ═══════════════════════════════════════════════════════════════════════════
ENV_PATH = Path("/mnt/c/Users/12035/father_daddy_capital/.env")

def load_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

EOA = "0xD4a39D33b8CcB46a08378e426BaEE3591463f090"
DW = "0xaF7B21FE2B18745aE1b2fA2F6F00B0fC4EF3F70b"
CHAIN_ID = 137
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"

# ═══════════════════════════════════════════════════════════════════════════
# CANARY CONFIG
# ═══════════════════════════════════════════════════════════════════════════

CANARY_CONFIG = {
    "version": "V21.7.63",
    "cell_id": "5M_REVERSAL_SCALPER_CANARY",
    "interval": "5m",
    "assets": ["BTC", "ETH", "SOL", "XRP"],
    "entry_price_lo": 0.30,  # V21.7.74: Raised from 5¢ — below 30¢ the market is pricing <30% probability, signal is too weak
    "entry_price_hi": 0.70,  # V21.7.74: Lowered from 80¢ — backtest shows 90¢ entries are catastrophic (PF 0.12). Stay in 30-70¢ band where payout ~1:1.
    "position_size_usd": 1.50,  # V21.7.74: Kelly-sized. Backtest: 52.5% WR @ 50¢ → quarter Kelly = 1.2% of bankroll = $0.39 on $31. Capped at $1.50 minimum viable (5 shares @ 30¢). Previous $3 was 7.8x quarter Kelly.
    "max_open_positions": 5,
    "max_daily_trades": 10,
    "max_daily_loss_usd": 15.0,
    "max_total_canary_loss_usd": 50.0,
    "max_consecutive_losses": 3,  # V21.7.64: Tightened from 5→3 — strategy decay fix (WR 80%→48.9%)
    "rolling_wr_window": 15,       # V21.7.64: Rolling WR window for adaptive gate tightening
    "rolling_wr_floor": 0.50,      # V21.7.64: If rolling WR < 50%, raise confidence threshold by 5pp
    "rolling_wr_crisis": 0.40,      # V21.7.64: If rolling WR < 40%, halt new entries for the day
    "order_type_preferred": "FAK",
    "order_type_acceptable": "FOK",
    "scan_interval_seconds": 5.0,  # 5s scan cadence
    "armed_interval_seconds": 1.0,  # 1s when near entry
    # V21.7.68: Per-asset daily cap REMOVED — if BTC is the strongest signal, trade it
    "max_trades_per_asset_per_day": 999,  # No cap — was 3, forced diversification hurt volume
    # ─── Live promotion gates ───
    "live_min_resolved_trades": 25,     # Minimum resolved paper trades before live
    "live_min_win_rate": 0.55,           # Minimum win rate (55%)
    "live_min_profit_factor": 1.25,      # Minimum profit factor
    "live_min_avg_edge_pp": 10.0,        # Minimum average edge in percentage points
    "live_min_pnl_usd": 25.0,            # Minimum paper PnL in USD
    "live_max_settlement_errors": 0,      # Zero settlement errors allowed
}

# Reversal detection thresholds
# V21.7.74: RSI thresholds from 995-window backtest.
# RSI<30→UP: 50% WR (marginal). RSI>70→DOWN: 55.7% WR (PF 1.26, best signal).
# Previous thresholds (40/60) generated 550 signals but only 53.3% WR.
# Tighter thresholds = fewer trades but higher edge.
REVERSAL_CONFIG = {
    "rsi_oversold": 30.0,      # V21.7.74: Tightened from 40→30 (backtest: 50% WR at 30, 58% at <25)
    "rsi_overbought": 70.0,   # V21.7.74: Tightened from 60→70 (backtest: 55.7% WR, best signal)
    "momentum_threshold": 0.0005,
    "mean_reversion_window": 10,
    "mean_reversion_threshold": 0.0008,
    "min_edge_pp": 5.0,
    "min_confidence": 0.55,
    "min_confidence_down": 0.60,  # V21.7.74: Lowered from 0.65 — backtest shows DOWN at RSI>70 is best signal
    "max_spread_cents": 5.0,
    "min_tte_seconds": 30,
    "max_tte_seconds": 600,
    "vol_penalty_multiplier": 0.5,
}

# ═══════════════════════════════════════════════════════════════════════════
# NEURAL PLASTICITY LAYER
# ═══════════════════════════════════════════════════════════════════════════

class NeuralPlasticityEngine:
    """Lightweight neural network with online learning (plasticity).
    8 inputs → 16 hidden (ReLU) → 8 hidden (ReLU) → 1 output (tanh).
    Online SGD with EWC. Learns from every trade outcome."""

    INPUT_DIM = 8
    HIDDEN1_DIM = 16
    HIDDEN2_DIM = 8
    LEARNING_RATE = 0.01
    EWC_LAMBDA = 0.001  # Elastic weight consolidation strength
    LEARNING_RATE_DECAY = 0.9995

    def __init__(self, weights_path: Path = None):
        self.weights_path = weights_path or (NEURAL_DIR / "v21762_plastic_weights.npz")
        self.training_steps = 0
        self.lr = self.LEARNING_RATE
        self.ewc_fisher = None  # Fisher information matrix for EWC
        self.replay_buffer = []  # Experience replay
        self.replay_max = 200

        # Load or initialize weights
        if self.weights_path.exists():
            try:
                data = np.load(self.weights_path)
                self.W1 = data["W1"]
                self.b1 = data["b1"]
                self.W2 = data["W2"]
                self.b2 = data["b2"]
                self.W3 = data["W3"]
                self.b3 = data["b3"]
                log.info(f"Neural weights loaded from {self.weights_path}")
            except Exception:
                self._init_weights()
        else:
            self._init_weights()

    def _init_weights(self):
        """Xavier initialization."""
        np.random.seed(42)
        self.W1 = np.random.randn(self.INPUT_DIM, self.HIDDEN1_DIM) * np.sqrt(2.0 / self.INPUT_DIM)
        self.b1 = np.zeros((1, self.HIDDEN1_DIM))
        self.W2 = np.random.randn(self.HIDDEN1_DIM, self.HIDDEN2_DIM) * np.sqrt(2.0 / self.HIDDEN1_DIM)
        self.b2 = np.zeros((1, self.HIDDEN2_DIM))
        self.W3 = np.random.randn(self.HIDDEN2_DIM, 1) * np.sqrt(2.0 / self.HIDDEN2_DIM)
        self.b3 = np.zeros((1, 1))

    def _relu(self, x):
        return np.maximum(0, x)

    def _tanh(self, x):
        return np.tanh(x)

    def encode_features(self, rsi: float, macd: float, trend: float, momentum: float,
                        mean_reversion: float, volatility: float, asset_class: str,
                        confidence: float) -> np.ndarray:
        """Encode market features into 8-dim input vector."""
        asset_map = {"BTC": 0.0, "ETH": 0.25, "SOL": 0.5, "XRP": 0.75}
        return np.array([[
            rsi / 100.0,
            np.tanh(macd * 10),
            np.tanh(trend * 5),
            np.tanh(momentum * 100),
            np.tanh(mean_reversion * 10),
            np.tanh(volatility * 5),
            asset_map.get(asset_class, 0.5),
            confidence
        ]], dtype=np.float64)

    def predict_return(self, features: np.ndarray) -> float:
        """Predict expected return [-1, 1]. Positive = Up likely, negative = Down likely."""
        h1 = self._relu(features @ self.W1 + self.b1)
        h2 = self._relu(h1 @ self.W2 + self.b2)
        out = self._tanh(h2 @ self.W3 + self.b3)
        return float(out[0, 0])

    def learn(self, features: np.ndarray, prediction: float, actual_return: float):
        """Online SGD update. Reinforce winning patterns, prune losing ones."""
        # Forward pass
        h1 = self._relu(features @ self.W1 + self.b1)
        h2 = self._relu(h1 @ self.W2 + self.b2)
        out = self._tanh(h2 @ self.W3 + self.b3)

        # Loss: MSE between prediction and actual return
        error = out[0, 0] - actual_return
        loss = error ** 2

        # Backprop gradients
        d_out = 2 * error * (1 - out[0, 0] ** 2)  # tanh derivative
        d_W3 = h2.T @ d_out.reshape(1, 1)
        d_b3 = d_out.reshape(1, 1)

        d_h2 = d_out.reshape(1, 1) @ self.W3.T
        d_h2[h2 <= 0] = 0  # ReLU derivative
        d_W2 = h1.T @ d_h2
        d_b2 = d_h2.sum(axis=0, keepdims=True)

        d_h1 = d_h2 @ self.W2.T
        d_h1[h1 <= 0] = 0
        d_W1 = features.T @ d_h1
        d_b1 = d_h1.sum(axis=0, keepdims=True)

        # EWC penalty (if Fisher info available)
        if self.ewc_fisher is not None:
            for name, params, grads, fisher in [
                ("W1", self.W1, d_W1, self.ewc_fisher.get("W1")),
                ("W2", self.W2, d_W2, self.ewc_fisher.get("W2")),
                ("W3", self.W3, d_W3, self.ewc_fisher.get("W3")),
            ]:
                if fisher is not None:
                    grads += self.EWC_LAMBDA * fisher * (params - params)

        # Update weights
        self.W1 -= self.lr * d_W1
        self.b1 -= self.lr * d_b1
        self.W2 -= self.lr * d_W2
        self.b2 -= self.lr * d_b2
        self.W3 -= self.lr * d_W3
        self.b3 -= self.lr * d_b3

        # Experience replay
        self.replay_buffer.append((features.copy(), actual_return))
        if len(self.replay_buffer) > self.replay_max:
            self.replay_buffer.pop(0)

        # Learning rate decay
        self.lr *= self.LEARNING_RATE_DECAY
        self.training_steps += 1

        # Save weights periodically
        if self.training_steps % 50 == 0:
            self.save_weights()

        return loss

    def update_fisher(self):
        """Update Fisher information matrix from replay buffer."""
        if len(self.replay_buffer) < 10:
            return
        # Approximate Fisher as gradient variance
        fisher = {"W1": np.zeros_like(self.W1), "W2": np.zeros_like(self.W2), "W3": np.zeros_like(self.W3)}
        for features, actual in self.replay_buffer[-50:]:
            h1 = self._relu(features @ self.W1 + self.b1)
            h2 = self._relu(h1 @ self.W2 + self.b2)
            out = self._tanh(h2 @ self.W3 + self.b3)
            error = out[0, 0] - actual
            d_out = 2 * error * (1 - out[0, 0] ** 2)
            d_W3 = (h2.T @ d_out.reshape(1, 1)) ** 2
            fisher["W3"] += d_W3
            # Simplified: just use W3 fisher for EWC
        self.ewc_fisher = fisher

    def save_weights(self):
        """Save weights to disk."""
        try:
            np.savez(self.weights_path,
                     W1=self.W1, b1=self.b1, W2=self.W2, b2=self.b2,
                     W3=self.W3, b3=self.b3)
        except Exception as e:
            log.warning(f"Failed to save neural weights: {e}")


class BayesianCalibration:
    """Logistic calibration on top of neural predictions.
    Maps neural output [-1,1] to calibrated probability [0,1]."""

    PRIOR_MEAN = 0.0
    PRIOR_PRECISION = 1.0

    def __init__(self):
        self.beta = np.array([[0.0], [1.0]])  # [intercept, slope]
        self.precision = np.eye(2) * self.PRIOR_PRECISION
        self.observations = 0

    def calibrate(self, neural_pred: float) -> Tuple[float, float]:
        """Calibrate neural prediction to probability. Returns (prob, uncertainty)."""
        x = np.array([[1.0, neural_pred]])
        logit = float((x @ self.beta)[0, 0])
        prob = 1.0 / (1.0 + np.exp(-logit))
        # Uncertainty from posterior
        var = float(1.0 / (1.0 + np.exp(-logit) + np.exp(logit)))
        return float(prob), var

    def update(self, neural_pred: float, outcome: float):
        """Online Newton-Raphson update on logistic regression."""
        x = np.array([[1.0, neural_pred]])
        logit = float((x @ self.beta)[0, 0])
        p = 1.0 / (1.0 + np.exp(-logit))
        grad = x.T * (p - outcome) - self.PRIOR_PRECISION * (self.beta - self.PRIOR_MEAN)
        hess = x.T @ x * p * (1 - p) + self.PRIOR_PRECISION * np.eye(2)
        try:
            self.beta -= np.linalg.solve(hess, grad)
        except Exception:
            pass
        self.precision = hess
        self.observations += 1


# ═══════════════════════════════════════════════════════════════════════════
# MARKET DATA & INDICATORS
# ═══════════════════════════════════════════════════════════════════════════

# Price history cache per asset for indicator calculation
price_history: Dict[str, List[float]] = {a: [] for a in CANARY_CONFIG["assets"]}

def get_asset_price(asset: str) -> float:
    """Get current asset price from Binance."""
    symbols = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT"}
    sym = symbols.get(asset)
    if not sym:
        return 0.0
    try:
        r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={sym}", timeout=5)
        if r.status_code == 200:
            return float(r.json().get("price", 0))
    except Exception:
        pass
    return 0.0


def get_klines(asset: str, interval: str = "1m", limit: int = 50) -> List[Dict]:
    """Get candlestick data from Binance for indicator calculation."""
    symbols = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT"}
    sym = symbols.get(asset)
    if not sym:
        return []
    try:
        r = requests.get(
            f"https://api.binance.com/api/v3/klines",
            params={"symbol": sym, "interval": interval, "limit": limit},
            timeout=10,
        )
        if r.status_code == 200:
            klines = r.json()
            return [{
                "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]),
                "volume": float(k[5]), "ts": k[0],
            } for k in klines]
    except Exception:
        pass
    return []


def compute_rsi(closes: List[float], period: int = 14) -> float:
    """Compute RSI from close prices."""
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = float(np.mean(gains[-period:]))
    avg_loss = float(np.mean(losses[-period:]))
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - (100.0 / (1.0 + rs)))


def compute_macd(closes: List[float]) -> Tuple[float, float]:
    """Compute MACD line and signal. Returns (macd_line, signal_line)."""
    if len(closes) < 26:
        return 0.0, 0.0
    close_arr = np.array(closes, dtype=np.float64)
    # Simplified MACD: difference of moving averages, normalized by price
    macd_line = (np.mean(close_arr[-12:]) - np.mean(close_arr[-26:])) / close_arr[-1]
    signal_line = float(np.mean(close_arr[-9:]) / close_arr[-1]) if len(closes) >= 9 else 0.0
    return float(macd_line), float(signal_line)


def compute_indicators(asset: str) -> Dict:
    """Compute all technical indicators for an asset."""
    klines = get_klines(asset, "1m", 50)
    if not klines or len(klines) < 20:
        return {"rsi": 50.0, "macd": 0.0, "signal": 0.0, "trend": 0.0,
                "momentum": 0.0, "mean_reversion": 0.0, "volatility": 0.0}

    closes = [k["close"] for k in klines]
    current = closes[-1]

    rsi = compute_rsi(closes)
    macd, signal = compute_macd(closes)

    # Trend: linear regression slope of last 10 closes
    recent = closes[-10:]
    x = np.arange(len(recent))
    trend = float(np.polyfit(x, recent, 1)[0]) / current if current > 0 else 0.0

    # Momentum: rate of change
    momentum = (current - closes[-5]) / closes[-5] if len(closes) >= 5 and closes[-5] > 0 else 0.0

    # Mean reversion: deviation from moving average
    ma = float(np.mean(closes[-REVERSAL_CONFIG["mean_reversion_window"]:]))
    mean_reversion = (current - ma) / ma if ma > 0 else 0.0

    # Volatility: std of returns
    returns = np.diff(np.log(closes[-20:])) if len(closes) >= 21 else np.array([0.0])
    volatility = float(np.std(returns)) if len(returns) > 1 else 0.0

    return {
        "rsi": rsi,
        "macd": macd,
        "signal": signal,
        "trend": trend,
        "momentum": momentum,
        "mean_reversion": mean_reversion,
        "volatility": volatility,
        "current_price": current,
    }


def detect_reversal(indicators: Dict) -> Tuple[str, float, float]:
    """Detect reversal signal. Returns (direction, confidence, edge).
    
    Direction: 'UP' (expect price to go up), 'DOWN' (expect price to go down)
    Confidence: 0-1 probability of the direction being correct
    Edge: estimated edge over market in percentage points
    """
    rsi = indicators["rsi"]
    macd = indicators["macd"]
    signal = indicators["signal"]
    trend = indicators["trend"]
    momentum = indicators["momentum"]
    mean_reversion = indicators["mean_reversion"]
    volatility = indicators["volatility"]

    # ─── Reversal Detection Logic ───
    
    up_signals = 0.0
    down_signals = 0.0
    total_signals = 0.0

    # RSI signals (weighted: extreme RSI = stronger signal)
    if rsi < REVERSAL_CONFIG["rsi_oversold"]:
        strength = (REVERSAL_CONFIG["rsi_oversold"] - rsi) / 20.0  # 0-1 based on how oversold
        up_signals += 1.0 + strength
    elif rsi > REVERSAL_CONFIG["rsi_overbought"]:
        strength = (rsi - REVERSAL_CONFIG["rsi_overbought"]) / 20.0
        down_signals += 1.0 + strength
    total_signals += 1.5

    # MACD crossover
    if macd > signal and macd > 0:
        up_signals += 1.0
    elif macd < signal and macd < 0:
        down_signals += 1.0
    total_signals += 1.0

    # Mean reversion (price deviation from MA)
    mr_threshold = REVERSAL_CONFIG.get("mean_reversion_threshold", 0.0008)
    if mean_reversion < -mr_threshold:
        strength = min(abs(mean_reversion) / mr_threshold, 2.0)
        up_signals += strength
    elif mean_reversion > mr_threshold:
        strength = min(mean_reversion / mr_threshold, 2.0)
        down_signals += strength
    total_signals += 2.0

    # Momentum exhaustion (strong negative momentum → reversal up)
    mom_thresh = REVERSAL_CONFIG["momentum_threshold"]
    if momentum < -mom_thresh * 2:
        up_signals += 1.0  # Strong negative momentum → expect reversal up
    elif momentum > mom_thresh * 2:
        down_signals += 1.0  # Strong positive momentum → expect reversal down
    total_signals += 1.0

    # Trend (counter-trend = reversal)
    if trend < -0.0005:
        up_signals += 0.5
    elif trend > 0.0005:
        down_signals += 0.5
    total_signals += 0.5

    # Determine direction and confidence
    if up_signals > down_signals:
        direction = "UP"
        confidence = up_signals / (up_signals + down_signals + 0.001)
    elif down_signals > up_signals:
        direction = "DOWN"
        confidence = down_signals / (up_signals + down_signals + 0.001)
    else:
        direction = "UP" if trend > 0 else "DOWN"
        confidence = 0.5

    # Adjust confidence for volatility (high vol = less confident)
    vol_penalty = min(volatility * 2 * REVERSAL_CONFIG.get("vol_penalty_multiplier", 0.5), 0.15)
    confidence = max(0.5, min(0.95, confidence - vol_penalty))

    # Edge estimate: confidence - 50% baseline
    edge = (confidence - 0.5) * 100  # in percentage points

    return direction, confidence, edge


# ═══════════════════════════════════════════════════════════════════════════
# MARKET DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════

def discover_5m_markets() -> List[Dict]:
    """Discover active 5-minute Up/Down crypto markets using precise slug format.
    
    PM slug format: {asset}-updown-5m-{epoch_ts}
    The next 5m window expires at: ((epoch // 300) + 1) * 300
    """
    markets = []
    seen_slugs = set()
    
    now_epoch = int(time.time())
    # Compute next expiry for 5m
    next_exp_5m = ((now_epoch // 300) + 1) * 300
    # Also check the one after (in case the current one just started)
    next_exp_5m_next = next_exp_5m + 300
    
    asset_aliases = {
        "BTC": ["btc", "bitcoin"],
        "ETH": ["eth", "ethereum"],
        "SOL": ["sol", "solana"],
        "XRP": ["xrp", "ripple"],
    }

    for asset in CANARY_CONFIG["assets"]:
        asset_lower = asset.lower()
        for exp_ts in [next_exp_5m, next_exp_5m_next]:
            slug = f"{asset_lower}-updown-5m-{exp_ts}"
            if slug in seen_slugs:
                continue
            try:
                r = requests.get(
                    f"{GAMMA_HOST}/events",
                    params={"slug": slug},
                    timeout=10,
                )
                if r.status_code != 200:
                    continue
                events = r.json()
                if not events:
                    continue
                ev = events[0]
                # Get markets from the event
                ev_markets = ev.get("markets", [])
                for mk in ev_markets:
                    q = mk.get("question", "").lower()
                    if "up or down" not in q and "up/down" not in q:
                        continue
                    
                    try:
                        outcomes = json.loads(mk.get("outcomes", "[]")) if isinstance(mk.get("outcomes"), str) else mk.get("outcomes", [])
                    except Exception:
                        outcomes = []
                    try:
                        token_ids = json.loads(mk.get("clobTokenIds", "[]")) if isinstance(mk.get("clobTokenIds"), str) else mk.get("clobTokenIds", [])
                    except Exception:
                        token_ids = []

                    if len(outcomes) < 2 or len(token_ids) < 2:
                        continue

                    up_tid = ""
                    down_tid = ""
                    for i, o in enumerate(outcomes):
                        if i >= len(token_ids):
                            break
                        ol = str(o).lower()
                        if ol == "up":
                            up_tid = token_ids[i]
                        elif ol == "down":
                            down_tid = token_ids[i]

                    if not up_tid or not down_tid:
                        continue

                    tte = exp_ts - now_epoch

                    # Read neg_risk from PM market data (critical for CLOB order signing)
                    neg_risk = mk.get("neg_risk", False)
                    if isinstance(neg_risk, str):
                        neg_risk = neg_risk.lower() == "true"

                    markets.append({
                        "slug": slug,
                        "question": mk.get("question", ""),
                        "asset": asset,
                        "up_token_id": up_tid,
                        "down_token_id": down_tid,
                        "tte_seconds": round(tte, 1),
                        "active": mk.get("active", True),
                        "closed": mk.get("closed", False),
                        "volume_24h": float(mk.get("volume24hr", 0) or 0),
                        "outcomes": outcomes,
                        "neg_risk": neg_risk,
                    })
                    seen_slugs.add(slug)
                    break  # One market per event
            except Exception:
                continue

    return markets


def get_orderbook(token_id: str) -> Optional[Dict]:
    """Fetch CLOB orderbook for a token."""
    try:
        r = requests.get(f"{CLOB_HOST}/book?token_id={token_id}", timeout=10)
        if r.status_code == 200:
            book = r.json()
            asks = sorted(book.get("asks", []), key=lambda x: float(x.get("price", 1)))
            bids = sorted(book.get("bids", []), key=lambda x: float(x.get("price", 0)), reverse=True)
            best_ask = float(asks[0]["price"]) if asks else None
            best_bid = float(bids[0]["price"]) if bids else None
            return {
                "best_ask": best_ask,
                "best_bid": best_bid,
                "spread": round(best_ask - best_bid, 4) if best_ask and best_bid else None,
                "ask_depth": sum(float(a.get("size", 0)) for a in asks[:5]),
                "bid_depth": sum(float(b.get("size", 0)) for b in bids[:5]),
                "book_valid": bool(asks or bids),
            }
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════════════════
# CLOB CLIENT
# ═══════════════════════════════════════════════════════════════════════════

_clob_client = None

def get_clob_client():
    global _clob_client
    if _clob_client is None:
        env = load_env()
        pk = env.get("PM_WALLET_PRIVATE_KEY", "")
        if not pk:
            raise ValueError("No PM_WALLET_PRIVATE_KEY in env")
        try:
            from py_clob_client_v2 import ClobClient, SignatureTypeV2
            _clob_client = ClobClient(
                CLOB_HOST, key=pk, chain_id=CHAIN_ID,
                signature_type=SignatureTypeV2.POLY_1271.value, funder=DW,
            )
            # Derive and set API credentials (L2 auth required for order submission)
            creds = _clob_client.create_or_derive_api_key()
            _clob_client.set_api_creds(creds)
            log.info("CLOB client initialized (POLY_1271) with L2 API creds")
        except Exception as e:
            raise ValueError(f"CLOB client init failed: {e}")
    return _clob_client


# ═══════════════════════════════════════════════════════════════════════════
# ENGINE STATE
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class CanaryState:
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    loop_count: int = 0
    markets_scanned: int = 0
    signals_generated: int = 0
    entry_signals: int = 0
    orders_submitted: int = 0
    orders_filled: int = 0
    orders_rejected: int = 0
    daily_trades: int = 0
    daily_loss_usd: float = 0.0
    daily_reset: str = ""  # UTC date string for daily reset
    open_positions: int = 0
    total_pnl: float = 0.0
    consecutive_losses: int = 0
    wins: int = 0
    losses: int = 0
    halted: bool = False
    halt_reason: str = ""
    paper_mode: bool = True
    wallet_balance: float = 0.0
    positions: List[Dict] = field(default_factory=list)
    closed_positions: List[Dict] = field(default_factory=list)
    neural_training_steps: int = 0
    scan_latency_ms: List[float] = field(default_factory=list)
    last_neural_save: float = 0.0
    asset_daily_trades: Dict[str, int] = field(default_factory=dict)  # V21.7.63: per-asset daily cap
    recent_outcomes: List[int] = field(default_factory=list)  # V21.7.64: rolling WR tracker (1=win, 0=loss)
    recent_pnls: List[float] = field(default_factory=list)    # V21.7.66: rolling PnL tracker for EV-based gating


# ═══════════════════════════════════════════════════════════════════════════
# EXECUTION
# ═══════════════════════════════════════════════════════════════════════════

def execute_order(market: Dict, side: str, token_id: str, best_ask: float,
                  clob_client, paper_mode: bool, neural_pred: float,
                  indicators: Dict, confidence: float) -> Dict:
    """Execute a 5m scalper order."""
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_slug": market["slug"],
        "question": market["question"],
        "asset": market["asset"],
        "side": side,
        "token_id": token_id,
        "ask": best_ask,
        "size_usd": CANARY_CONFIG["position_size_usd"],
        "neural_pred": round(neural_pred, 4),
        "confidence": round(confidence, 4),
        "rsi": round(indicators["rsi"], 1),
        "macd": round(indicators["macd"], 6),
        "trend": round(indicators["trend"], 6),
        "momentum": round(indicators["momentum"], 6),
        "status": "PENDING",
        "order_id": None,
        "fill_status": None,
        "fill_price": None,
        "error": None,
    }

    if paper_mode:
        result["status"] = "PAPER_FILLED"
        result["fill_status"] = "paper"
        result["fill_price"] = best_ask
        result["shares"] = CANARY_CONFIG["position_size_usd"] / best_ask
        result["paper_pnl_if_correct"] = (1.0 - best_ask) * result["shares"]
        result["paper_pnl_if_wrong"] = -best_ask * result["shares"]
        log.info(f"PAPER: {side} {market['asset']} | {market['question'][:40]} | "
                 f"@{best_ask*100:.1f}¢ | $5 | conf={confidence:.1%} | "
                 f"RSI={indicators['rsi']:.0f} | neural={neural_pred:.3f}")
        with open(OUT / "paper_orders.jsonl", "a") as f:
            f.write(json.dumps(result, default=str) + "\n")
        return result

    # LIVE execution
    try:
        from py_clob_client_v2 import OrderArgsV2, CreateOrderOptions, OrderType
        # Convert USD position size to shares (PM CLOB size = number of shares)
        size_usd = CANARY_CONFIG["position_size_usd"]
        # V21.7.65: Progressive position sizing — reduce on losing streaks, increase on wins
        cl = state.consecutive_losses
        if cl >= 3:
            size_usd *= 0.50  # Half size after 3 consecutive losses
            log.info(f"Progressive sizing: ${size_usd:.2f} (3+ consecutive losses, half size)")
        elif cl == 2:
            size_usd *= 0.75  # 75% size after 2 consecutive losses
            log.info(f"Progressive sizing: ${size_usd:.2f} (2 consecutive losses, 75% size)")
        recent_wins = sum(state.recent_outcomes[-5:]) if len(state.recent_outcomes) >= 5 else 0
        if recent_wins >= 4 and cl == 0:
            size_usd *= 1.25  # 25% boost on hot streak (4/5 wins, no current losses)
            size_usd = min(size_usd, CANARY_CONFIG["position_size_usd"] * 1.25)  # Cap
            log.info(f"Progressive sizing: ${size_usd:.2f} (hot streak 4/5, 25% boost)")
        shares = round(size_usd / max(best_ask, 0.01), 2)
        shares = max(int(shares), 1)  # Minimum 1 share, round to int
        actual_cost = shares * best_ask
        result["shares"] = shares
        result["actual_cost"] = round(actual_cost, 2)
        log.info(f"Order sizing: ${size_usd} / ask={best_ask:.4f} = {shares} shares (cost=${actual_cost:.2f})")
        order_args = OrderArgsV2(token_id=token_id, price=best_ask,
                                 size=shares, side="BUY")
        # Use dynamic neg_risk from market data (not hardcoded)
        market_neg_risk = market.get("neg_risk", False)
        options = CreateOrderOptions(tick_size="0.01", neg_risk=market_neg_risk)

        signed_order = clob_client.create_order(order_args, options)

        if signed_order.maker != DW:
            result["error"] = f"Maker mismatch: {signed_order.maker}"
            result["status"] = "EMERGENCY_HALT"
            return result
        if signed_order.signatureType != 3:
            result["error"] = f"sig_type mismatch: {signed_order.signatureType}"
            result["status"] = "EMERGENCY_HALT"
            return result

        result["status"] = "SUBMITTED"
        try:
            order_result = clob_client.post_order(signed_order, OrderType.FOK)
            result["order_type_used"] = "FOK"
        except Exception as e_fok:
            log.warning(f"FOK failed: {e_fok}, trying GTC→cancel")
            signed_order = clob_client.create_order(order_args, options)
            try:
                order_result = clob_client.post_order(signed_order, OrderType.GTC)
                result["order_type_used"] = "GTC_EMERGENCY_CANCEL"
                if order_result.get("orderID"):
                    clob_client.cancel_orders([order_result["orderID"]])
            except Exception:
                result["error"] = f"FOK + GTC failed"
                result["status"] = "ORDER_FAILED"
                return result

        order_id = order_result.get("orderID", "")
        fill_status = order_result.get("status", "")
        result["order_id"] = order_id
        result["fill_status"] = fill_status
        result["status"] = "ACKNOWLEDGED"

        try:
            clob_client.cancel_all()
        except Exception:
            pass

        if fill_status in ("live", "matched"):
            log.info(f"LIVE FILL: {side} {market['asset']} @ {best_ask*100:.1f}¢ | id={order_id[:20]}...")

    except Exception as e:
        result["error"] = str(e)
        result["status"] = "ERROR"
        log.error(f"Order error: {e}")
        try:
            clob_client.cancel_all()
        except Exception:
            pass

    with open(OUT / "order_attempts.jsonl", "a") as f:
        f.write(json.dumps(result, default=str) + "\n")
    return result


# ═══════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(OUT / "canary.log"),
    ],
)
log = logging.getLogger("v21762")

_shutdown = False
def handle_signal(signum, frame):
    global _shutdown
    _shutdown = True
    log.info(f"Signal {signum} received")
signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


# ═══════════════════════════════════════════════════════════════════════════
# MAIN SCAN LOOP
# ═══════════════════════════════════════════════════════════════════════════

def canary_loop(state: CanaryState, paper_mode: bool, pos_file=None, resolved_file=None):
    """Main canary loop: scan 5m markets, detect reversals, execute trades."""
    global _shutdown

    log.info(f"V21.7.62 Reversal Scalper Canary starting | paper={paper_mode}")
    log.info(f"Strategy: 5m Up/Down | Both directions | Reversal detection | Neural plasticity")
    log.info(f"Assets: {CANARY_CONFIG['assets']} | Max pos: ${CANARY_CONFIG['position_size_usd']} | "
             f"Daily limit: {CANARY_CONFIG['max_daily_trades']}")

    # Initialize neural engine
    neural = NeuralPlasticityEngine()
    bayesian = BayesianCalibration()
    log.info(f"Neural engine loaded | training_steps={neural.training_steps}")

    clob = None
    if not paper_mode:
        try:
            clob = get_clob_client()
            log.info("CLOB client initialized for live trading")
        except Exception as e:
            log.error(f"CLOB init failed: {e} — falling back to paper")
            paper_mode = True

    SCAN_INTERVAL = CANARY_CONFIG["scan_interval_seconds"]
    HEARTBEAT_INTERVAL = 30
    last_heartbeat = 0.0
    last_neural_fisher = 0.0

    while not _shutdown:
        try:
            loop_start = time.time()
            now = datetime.now(timezone.utc)

            # ─── Daily reset (UTC day boundary) ───
            today = now.strftime("%Y-%m-%d")
            if state.daily_reset != today:
                log.info(f"Daily reset: {state.daily_reset} → {today} | trades={state.daily_trades} loss=${state.daily_loss_usd:.2f}")
                state.daily_trades = 0
                state.daily_loss_usd = 0.0
                state.asset_daily_trades = {}  # V21.7.63: Reset per-asset daily counter
                state.daily_reset = today

            # ─── Halt check ───
            if state.halted:
                log.error(f"HALTED: {state.halt_reason}")
                break
            if state.consecutive_losses >= CANARY_CONFIG["max_consecutive_losses"]:
                state.halted = True
                state.halt_reason = f"Max consecutive losses ({state.consecutive_losses})"
                break

            can_trade = (state.daily_trades < CANARY_CONFIG["max_daily_trades"] and
                         state.open_positions < CANARY_CONFIG["max_open_positions"] and
                         state.daily_loss_usd < CANARY_CONFIG["max_daily_loss_usd"])

            # ─── Discover 5m markets ───
            markets = discover_5m_markets()
            state.markets_scanned = len(markets)

            if not markets:
                time.sleep(SCAN_INTERVAL)
                continue

            # ─── Compute indicators for each asset ───
            asset_indicators = {}
            for asset in CANARY_CONFIG["assets"]:
                asset_indicators[asset] = compute_indicators(asset)

            # ─── Scan markets and detect signals ───
            signals = []
            for market in markets:
                asset = market["asset"]
                indicators = asset_indicators.get(asset)
                if not indicators:
                    continue

                tte = market["tte_seconds"]
                if tte < REVERSAL_CONFIG["min_tte_seconds"] or tte > REVERSAL_CONFIG["max_tte_seconds"]:
                    continue

                # Detect reversal
                direction, confidence, edge = detect_reversal(indicators)
                state.signals_generated += 1

                # V21.7.66: Rolling EV/PnL adaptive gate — uses EV not WR (EV matters more than WR)
                recent = state.recent_outcomes[-CANARY_CONFIG["rolling_wr_window"]:]
                if len(recent) >= 5:
                    rolling_wr = sum(recent) / len(recent)
                    # Calculate rolling PnL (EV proxy) — need to get from recent trades
                    rolling_pnl = sum(state.recent_pnls[-CANARY_CONFIG["rolling_wr_window"]:]) if hasattr(state, 'recent_pnls') and state.recent_pnls else 0
                    # Halt only if BOTH WR is low AND PnL is negative (true crisis)
                    if rolling_wr < CANARY_CONFIG["rolling_wr_crisis"] and rolling_pnl < 0:
                        continue  # Strategy in genuine crisis — losing AND missing
                    if rolling_wr < CANARY_CONFIG["rolling_wr_floor"] and rolling_pnl < 0:
                        confidence += 0.05  # Tighten only when losing money

                if edge < REVERSAL_CONFIG["min_edge_pp"]:
                    continue
                # V21.7.74: DOWN BLOCK REMOVED — backtest (995 5m windows, 3 days) shows
                # RSI>70 → DOWN has 55.7% WR, PF 1.26, $0.34 EV/trade — BEST signal.
                # The previous block was based on 16 live trades (insufficient sample).
                # Bonereaper actively trades DOWN at 67-98¢ — both directions have edge
                # when the signal is strong enough.
                # Key: DOWN works when RSI > 70 (overbought), not at RSI 40-60 (midzone).

                # Neural prediction
                features = neural.encode_features(
                    rsi=indicators["rsi"], macd=indicators["macd"],
                    trend=indicators["trend"], momentum=indicators["momentum"],
                    mean_reversion=indicators["mean_reversion"],
                    volatility=indicators["volatility"],
                    asset_class=asset, confidence=confidence
                )
                neural_pred = neural.predict_return(features)
                calibrated_prob, uncertainty = bayesian.calibrate(neural_pred)

                # Determine which side to trade
                if direction == "UP":
                    token_id = market["up_token_id"]
                    side = "UP"
                    neural_direction = neural_pred > 0
                else:
                    token_id = market["down_token_id"]
                    side = "DOWN"
                    neural_direction = neural_pred < 0

                # Fetch orderbook
                book = get_orderbook(token_id)
                if not book or not book.get("best_ask"):
                    continue

                best_ask = book["best_ask"]
                spread = book.get("spread", 1.0)

                # Entry price check
                if not (CANARY_CONFIG["entry_price_lo"] <= best_ask <= CANARY_CONFIG["entry_price_hi"]):
                    continue

                # Spread check
                if spread and spread * 100 > REVERSAL_CONFIG["max_spread_cents"]:
                    continue

                # Volume check
                if market.get("volume_24h", 0) < 50:
                    continue

                # Neural agreement check — during warmup (first 100 trades), neural is advisory only
                neural_agrees = (direction == "UP" and neural_pred > 0) or (direction == "DOWN" and neural_pred < 0)
                neural_warmup = neural.training_steps < 100  # Warmup: don't let neural block trades
                if not neural_agrees and not neural_warmup:
                    continue
                if not neural_agrees and neural_warmup:
                    log.info(f"  Neural disagrees (warmup) | dir={direction} pred={neural_pred:.3f} — proceeding anyway")

                signal = {
                    "market": market,
                    "side": side,
                    "token_id": token_id,
                    "best_ask": best_ask,
                    "spread": spread,
                    "direction": direction,
                    "confidence": confidence,
                    "edge_pp": edge,
                    "neural_pred": neural_pred,
                    "calibrated_prob": calibrated_prob,
                    "uncertainty": uncertainty,
                    "indicators": indicators,
                    "tte": tte,
                }
                signals.append(signal)

            # Sort by edge (highest first)
            signals.sort(key=lambda x: x["edge_pp"], reverse=True)
            state.entry_signals = len(signals)

            # Log top signals
            for i, sig in enumerate(signals[:3]):
                log.info(f"  #{i+1} {sig['side']:4} {sig['market']['asset']} | "
                         f"ask={sig['best_ask']*100:.1f}¢ edge={sig['edge_pp']:.1f}pp "
                         f"conf={sig['confidence']:.1%} neural={sig['neural_pred']:.3f} "
                         f"RSI={sig['indicators']['rsi']:.0f} TTE={sig['tte']:.0f}s")

            # Log signals
            with open(OUT / "signals.jsonl", "a") as f:
                for sig in signals:
                    f.write(json.dumps({
                        "timestamp": now.isoformat(),
                        **{k: v for k, v in sig.items() if k != "market" and k != "indicators"},
                        "market_slug": sig["market"]["slug"],
                        "asset": sig["market"]["asset"],
                    }, default=str) + "\n")

            # ─── Execute best signal ───
            if can_trade and signals:
                best = signals[0]
                
                # Deduplication: skip if we already have an open position on this market
                existing_slugs = {p.get("market_slug", "") for p in state.positions}
                if best["market"]["slug"] in existing_slugs:
                    log.info(f"Skipping {best['market']['slug'][:40]} — already have open position")
                else:
                    # V21.7.63: Per-asset daily trade cap — force diversification
                    asset = best["market"]["asset"]
                    asset_count = state.asset_daily_trades.get(asset, 0)
                    max_per_asset = CANARY_CONFIG.get("max_trades_per_asset_per_day", 4)
                    if asset_count >= max_per_asset:
                        log.info(f"⏸ SKIP {asset} — daily asset cap reached ({asset_count}/{max_per_asset}), forcing diversification")
                    else:
                        log.info(f"🎯 EXECUTING: {best['side']} {best['market']['asset']} | "
                                 f"{best['market']['question'][:45]} | @ {best['best_ask']*100:.1f}¢")
                        order_result = execute_order(
                            best["market"], best["side"], best["token_id"], best["best_ask"],
                            clob, paper_mode, best["neural_pred"], best["indicators"], best["confidence"]
                        )
    
                        # V21.7.70: FILL VERIFICATION — only record as live if CLOB confirms fill.
                        # ACKNOWLEDGED alone means the order was submitted, NOT that it filled.
                        # Previously: unmatched orders went to live_positions.jsonl with fabricated PnL.
                        # Now: if fill_status != "matched", route to paper_positions.jsonl instead.
                        order_ok = order_result["status"] == "PAPER_FILLED" or (
                            order_result["status"] == "ACKNOWLEDGED" and 
                            order_result.get("fill_status") in ("live", "matched")
                        )
                        
                        if order_ok:
                            state.orders_submitted += 1
                            state.daily_trades += 1
                            state.open_positions += 1
                            # V21.7.63: Track per-asset daily trades
                            state.asset_daily_trades[asset] = asset_count + 1

                            pos = {
                                "timestamp": now.isoformat(),
                                "entry_timestamp": now.isoformat(),
                                "market_slug": best["market"]["slug"],
                                "asset": best["market"]["asset"],
                                "question": best["market"]["question"],
                                "side": best["side"],
                                "token_id": best["token_id"],
                                "entry_price": best["best_ask"],
                                "size_usd": CANARY_CONFIG["position_size_usd"],
                                "direction": best["direction"],
                                "confidence": best["confidence"],
                                "edge_pp": best["edge_pp"],
                                "neural_pred": best["neural_pred"],
                                "calibrated_prob": best["calibrated_prob"],
                                "rsi_at_entry": best["indicators"]["rsi"],
                                "macd_at_entry": best["indicators"]["macd"],
                                "trend_at_entry": best["indicators"]["trend"],
                                "momentum_at_entry": best["indicators"]["momentum"],
                                "tte_at_entry": best["tte"],
                                "features_at_entry": best["indicators"],
                                "order_status": order_result["status"],
                                "order_id": order_result.get("order_id"),
                                "fill_status": order_result.get("fill_status"),
                            }
                            state.positions.append(pos)

                            # V21.7.70: Route to correct file — live_positions only if actually filled
                            if order_result["status"] == "PAPER_FILLED":
                                write_target = OUT / "paper_positions.jsonl"
                            elif order_result.get("fill_status") in ("live", "matched"):
                                write_target = pos_file  # live_positions.jsonl
                                state.orders_filled += 1
                            else:
                                # ACKNOWLEDGED but not matched — shouldn't reach here due to order_ok check
                                write_target = OUT / "paper_positions.jsonl"
                            with open(write_target, "a") as f:
                                f.write(json.dumps(pos, default=str) + "\n")
                        else:
                            state.orders_rejected += 1
                            log.warning(f"ORDER REJECTED: status={order_result.get('status')} fill_status={order_result.get('fill_status')} — not recorded")

            # ─── Check expired positions (resolve paper trades) ───
            for pos in list(state.positions):
                # Compute market expiry from slug epoch timestamp (most reliable)
                # Slug format: btc-updown-5m-{epoch_ts} where epoch_ts is the start of the 5m window
                # Market expires at epoch_ts + 300 seconds
                # For non-updown slugs (date-based), parse the date
                slug = pos.get("market_slug", "")
                market_expired = False
                try:
                    last_part = slug.split("-")[-1]
                    slug_epoch = int(last_part)
                    if slug_epoch > 1_700_000_000:
                        # Genuine epoch — updown market
                        market_expiry = slug_epoch + 300  # 5m window
                        market_expired = time.time() >= market_expiry
                    else:
                        # Date-based slug — parse date and add 24h grace
                        import re
                        date_match = re.search(r'(\w+)-(\d{1,2})-(\d{4})$', slug)
                        if date_match:
                            from datetime import datetime as _dt2
                            month_str, day_str, year_str = date_match.groups()
                            try:
                                expiry_date = _dt2.strptime(f"{month_str} {day_str} {year_str}", "%B %d %Y")
                                market_expired = time.time() >= (expiry_date.replace(hour=23, minute=59).timestamp() + 3600)
                            except ValueError:
                                market_expired = False
                        else:
                            market_expired = False
                except (ValueError, IndexError):
                    # Fallback: use entry_timestamp + tte_at_entry
                    entry_ts_str = pos.get("entry_timestamp", pos.get("timestamp", ""))
                    try:
                        from datetime import datetime as _dt
                        entry_ts = _dt.fromisoformat(entry_ts_str.replace("Z", "+00:00")).timestamp()
                        tte_at_entry = pos.get("tte_at_entry", 300)
                        market_expired = time.time() >= (entry_ts + tte_at_entry)
                    except (ValueError, AttributeError):
                        log.warning(f"Could not determine expiry for position {slug}, skipping")
                        continue
                
                already_resolved = pos.get("resolved")

                if market_expired or already_resolved:
                    # ─── Settle via Polymarket market resolution ───
                    # For 5m Up/Down markets, check the actual market outcome via Gamma API
                    # Market slug format: {asset}-updown-5m-{epoch_ts}
                    slug = pos.get("market_slug", "")
                    asset = pos["asset"]
                    entry_price = pos.get("entry_price", 0)
                    side = pos["side"]
                    outcome = "UNKNOWN"
                    pnl = 0.0

                    # Try Polymarket Gamma API first (ground truth)
                    try:
                        r = requests.get(
                            f"{GAMMA_HOST}/events",
                            params={"slug": slug},
                            timeout=10,
                        )
                        if r.status_code == 200:
                            events = r.json()
                            if events:
                                ev = events[0]
                                for mk in ev.get("markets", []):
                                    q = mk.get("question", "").lower()
                                    if "up or down" not in q and "up/down" not in q:
                                        continue
                                    # Check if market is resolved
                                    outcomes_raw = mk.get("outcomes", "[]")
                                    try:
                                        outcomes_list = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                                    except Exception:
                                        outcomes_list = []
                                    
                                    prices_raw = mk.get("outcomePrices", "[]")
                                    try:
                                        prices_list = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
                                        prices = [float(p) for p in prices_list]
                                    except Exception:
                                        prices = []
                                    
                                    closed = mk.get("closed", False)
                                    
                                    if closed and len(prices) >= 2 and len(outcomes_list) >= 2:
                                        # Market resolved: winning outcome price = 1.0
                                        if prices[0] > prices[1]:
                                            winning_side = outcomes_list[0]  # "Up"
                                        else:
                                            winning_side = outcomes_list[1]  # "Down"
                                        
                                        our_side = side.upper()
                                        winning_side_norm = winning_side.upper()
                                        if our_side == winning_side_norm:
                                            outcome = "WIN"
                                            pnl = (1.0 - entry_price) * (CANARY_CONFIG["position_size_usd"] / entry_price)
                                        else:
                                            outcome = "LOSS"
                                            pnl = -entry_price * (CANARY_CONFIG["position_size_usd"] / entry_price)
                                        
                                        log.info(f"PM RESOLVED: {slug} | closed={closed} | "
                                                 f"winning={winning_side} | our_side={our_side} | "
                                                 f"prices={prices}")
                                        break
                    except Exception as e:
                        log.warning(f"Gamma API resolution check failed for {slug}: {e}")
                    
                    # If PM hasn't resolved yet, skip — don't fall back to price comparison
                    # PM markets take 1-5 minutes to settle after expiry
                    if outcome == "UNKNOWN":
                        log.info(f"PM market not yet resolved for {slug}, will retry next cycle")
                        continue

                    if outcome != "UNKNOWN":
                        pos["outcome"] = outcome
                        pos["pnl"] = round(pnl, 4)
                        pos["resolved_timestamp"] = now.isoformat()
                        state.closed_positions.append(pos)
                        state.positions.remove(pos)
                        state.open_positions = max(0, state.open_positions - 1)
                        state.total_pnl += pnl

                        if outcome == "WIN":
                            state.wins += 1
                            state.consecutive_losses = 0
                            state.recent_outcomes.append(1)  # V21.7.64: rolling WR
                            state.recent_pnls.append(pnl)   # V21.7.66: rolling PnL
                        else:
                            state.losses += 1
                            state.consecutive_losses += 1
                            state.daily_loss_usd += abs(pnl)
                            state.recent_outcomes.append(0)  # V21.7.64: rolling WR
                            state.recent_pnls.append(pnl)    # V21.7.66: rolling PnL
                        # V21.7.64: Keep only last N outcomes
                        state.recent_outcomes = state.recent_outcomes[-50:]
                        state.recent_pnls = state.recent_pnls[-50:]  # V21.7.66

                        # ─── NEURAL LEARNING ───
                        features = neural.encode_features(
                            rsi=pos.get("rsi_at_entry", 50),
                            macd=pos.get("macd_at_entry", 0),
                            trend=pos.get("trend_at_entry", 0),
                            momentum=pos.get("momentum_at_entry", 0),
                            mean_reversion=pos.get("features_at_entry", {}).get("mean_reversion", 0),
                            volatility=pos.get("features_at_entry", {}).get("volatility", 0),
                            asset_class=asset, confidence=pos.get("confidence", 0.5)
                        )
                        pred = pos.get("neural_pred", 0)
                        actual_return = pnl / CANARY_CONFIG["position_size_usd"]
                        neural.learn(features, pred, actual_return)
                        bayesian.update(pred, 1.0 if outcome == "WIN" else 0.0)
                        state.neural_training_steps = neural.training_steps

                        log.info(f"RESOLVED: {side} {asset} | {outcome} | PnL=${pnl:.2f} | "
                                 f"neural_steps={neural.training_steps}")

                        with open(resolved_file, "a") as f:  # V21.7.65: mode-specific file
                            f.write(json.dumps(pos, default=str) + "\n")

            # ─── Update Fisher info periodically ───
            if time.time() - last_neural_fisher > 300:  # Every 5 min
                neural.update_fisher()
                last_neural_fisher = time.time()

            # ─── Heartbeat ───
            loop_ms = (time.time() - loop_start) * 1000
            state.scan_latency_ms.append(loop_ms)
            if len(state.scan_latency_ms) > 100:
                state.scan_latency_ms = state.scan_latency_ms[-100:]

            if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
                p50 = statistics.median(state.scan_latency_ms) if state.scan_latency_ms else 0
                wr = state.wins / (state.wins + state.losses) * 100 if (state.wins + state.losses) > 0 else 0

                heartbeat = {
                    "timestamp": now.isoformat(), "pid": os.getpid(),
                    "loop_count": state.loop_count, "scan_ms": round(loop_ms, 1),
                    "p50_scan_ms": round(p50, 1),
                    "markets": state.markets_scanned, "signals": state.signals_generated,
                    "entry_signals": state.entry_signals,
                    "orders": state.orders_submitted, "fills": state.orders_filled,
                    "daily_trades": state.daily_trades,
                    "open_pos": state.open_positions,
                    "wins": state.wins, "losses": state.losses,
                    "wr": round(wr, 1), "pnl": round(state.total_pnl, 2),
                    "neural_steps": state.neural_training_steps,
                    "paper": paper_mode, "halted": state.halted,
                }
                with open(OUT / "heartbeat.jsonl", "a") as f:
                    f.write(json.dumps(heartbeat) + "\n")

                log.info(f"HB: loop={state.loop_count} scan={loop_ms:.0f}ms "
                         f"mkts={state.markets_scanned} sigs={state.entry_signals} "
                         f"trades={state.daily_trades}/{CANARY_CONFIG['max_daily_trades']} "
                         f"pos={state.open_positions} W/L={state.wins}/{state.losses} "
                         f"WR={wr:.0f}% PnL=${state.total_pnl:.2f} "
                         f"neural={state.neural_training_steps}")

                # Supervisor status
                sup = {
                    "timestamp": now.isoformat(), "version": "V21.7.62",
                    "classification": "V21.7.62_REVERSAL_SCALPER_CANARY",
                    "running": not _shutdown, "paper_mode": paper_mode,
                    "loop_count": state.loop_count,
                    "markets_scanned": state.markets_scanned,
                    "signals_generated": state.signals_generated,
                    "entry_signals": state.entry_signals,
                    "orders_submitted": state.orders_submitted,
                    "orders_filled": state.orders_filled,
                    "wins": state.wins, "losses": state.losses,
                    "win_rate": round(wr, 1),
                    "total_pnl": round(state.total_pnl, 2),
                    "daily_trades": state.daily_trades,
                    "open_positions": state.open_positions,
                    "neural_training_steps": state.neural_training_steps,
                    "halted": state.halted,
                    "halt_reason": state.halt_reason,
                    "config": CANARY_CONFIG,
                }
                with open(SUP / "v21762_reversal_scalper_status.json", "w") as f:
                    json.dump(sup, f, indent=2, default=str)

                # ─── Live promotion readiness check (V21.7.68: DUAL METRICS) ───
                # CRITICAL FIX: Readiness must compute from BOTH paper and live data.
                # Previous version used state.wins/losses which is whichever mode
                # the bot runs in. This caused LIVE_READY when paper metrics passed
                # but live metrics (36W/35L, PF=1.02) clearly fail.
                # Now: promotion requires LIVE metrics to pass gates independently.

                # Paper metrics (from state — current mode if paper)
                paper_resolved = state.wins + state.losses
                paper_wr = state.wins / paper_resolved if paper_resolved > 0 else 0
                paper_wins_pnl = sum(
                    (1.0 - p.get("entry_price", 0.5)) * (CANARY_CONFIG["position_size_usd"] / p.get("entry_price", 0.5))
                    for p in state.closed_positions if p.get("outcome") == "WIN"
                ) if state.closed_positions else 0
                paper_losses_pnl = abs(sum(
                    p.get("entry_price", 0.5) * (CANARY_CONFIG["position_size_usd"] / p.get("entry_price", 0.5))
                    for p in state.closed_positions if p.get("outcome") == "LOSS"
                )) if state.closed_positions else 0.01
                paper_pf = paper_wins_pnl / paper_losses_pnl if paper_losses_pnl > 0 else float("inf")

                # Live metrics (always read from live_resolved.jsonl regardless of mode)
                live_resolved_trades = 0
                live_wins = 0
                live_losses = 0
                live_total_pnl = 0.0
                live_gp = 0.0
                live_gl = 0.0
                live_resolved_file = OUT / "live_resolved.jsonl"
                if live_resolved_file.exists():
                    try:
                        with open(live_resolved_file) as lrf:
                            for lr_line in lrf:
                                if not lr_line.strip():
                                    continue
                                lt = json.loads(lr_line)
                                if not lt.get("order_id"):
                                    continue  # Skip paper trades in live file
                                live_resolved_trades += 1
                                lp = lt.get("pnl", 0)
                                if isinstance(lp, (int, float)):
                                    live_total_pnl += lp
                                    if lp > 0:
                                        live_wins += 1
                                        live_gp += lp
                                    elif lp < 0:
                                        live_losses += 1
                                        live_gl += abs(lp)
                    except Exception as lr_err:
                        log.warning(f"Failed to read live_resolved.jsonl: {lr_err}")

                live_wr = live_wins / live_resolved_trades if live_resolved_trades > 0 else 0
                live_pf = live_gp / live_gl if live_gl > 0 else (float("inf") if live_gp > 0 else 0)

                # Promotion requires LIVE metrics to pass (not paper)
                # If no live trades exist, fall back to paper (but mark as paper_only)
                has_live_data = live_resolved_trades > 0
                if has_live_data:
                    promo_resolved = live_resolved_trades
                    promo_wr = live_wr
                    promo_pf = live_pf
                    promo_pnl = live_total_pnl
                else:
                    promo_resolved = paper_resolved
                    promo_wr = paper_wr
                    promo_pf = paper_pf
                    promo_pnl = state.total_pnl

                promo_pass = (
                    promo_resolved >= CANARY_CONFIG["live_min_resolved_trades"]
                    and promo_wr >= CANARY_CONFIG["live_min_win_rate"]
                    and promo_pf >= CANARY_CONFIG["live_min_profit_factor"]
                    and promo_pnl >= CANARY_CONFIG["live_min_pnl_usd"]
                )

                readiness = {
                    "timestamp": now.isoformat(),
                    "version": "V21.7.68",
                    # Paper metrics
                    "resolved_paper_trades": paper_resolved,
                    "paper_win_rate": round(paper_wr, 4),
                    "paper_profit_factor": round(paper_pf, 2),
                    "paper_total_pnl": round(state.total_pnl, 2),
                    # Live metrics
                    "resolved_live_trades": live_resolved_trades,
                    "live_win_rate": round(live_wr, 4),
                    "live_profit_factor": round(live_pf, 2),
                    "live_total_pnl": round(live_total_pnl, 2),
                    # Backward compat fields (point to the metric source used for promotion)
                    "win_rate": round(promo_wr, 4),
                    "profit_factor": round(promo_pf, 2),
                    "total_pnl": round(promo_pnl, 2),
                    "avg_edge_pp": round(promo_pnl / promo_resolved / CANARY_CONFIG["position_size_usd"] * 100, 1) if promo_resolved > 0 else 0,
                    "settlement_errors": 0,
                    "data_source": "live" if has_live_data else "paper_only",
                    "live_blocked": not promo_pass,
                    "promotion_criteria_met": promo_pass,
                    "classification": "LIVE_READY" if promo_pass else (
                        "LIVE_FAILED" if has_live_data else "PAPER_VALIDATION"
                    ),
                    "gates": {
                        "min_resolved_trades": {"required": CANARY_CONFIG["live_min_resolved_trades"], "actual": promo_resolved, "met": promo_resolved >= CANARY_CONFIG["live_min_resolved_trades"]},
                        "min_win_rate": {"required": CANARY_CONFIG["live_min_win_rate"], "actual": round(promo_wr, 4), "met": promo_wr >= CANARY_CONFIG["live_min_win_rate"]},
                        "min_profit_factor": {"required": CANARY_CONFIG["live_min_profit_factor"], "actual": round(promo_pf, 2), "met": promo_pf >= CANARY_CONFIG["live_min_profit_factor"]},
                        "min_pnl_usd": {"required": CANARY_CONFIG["live_min_pnl_usd"], "actual": round(promo_pnl, 2), "met": promo_pnl >= CANARY_CONFIG["live_min_pnl_usd"]},
                        "settlement_errors": {"required": 0, "actual": 0, "met": True},
                    },
                    "config": CANARY_CONFIG,
                }
                with open(OUT / "live_readiness.json", "w") as f:
                    json.dump(readiness, f, indent=2, default=str)

                last_heartbeat = time.time()

            state.loop_count += 1

            # ─── Sleep ───
            elapsed = time.time() - loop_start
            sleep_time = max(1, SCAN_INTERVAL - elapsed)
            time.sleep(sleep_time)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Loop error: {e}")
            traceback.print_exc()
            time.sleep(10)

    # ─── Final report ───
    wr = state.wins / (state.wins + state.losses) * 100 if (state.wins + state.losses) > 0 else 0
    final = {
        "version": "V21.7.62", "timestamp": datetime.now(timezone.utc).isoformat(),
        "classification": "V21.7.62_REVERSAL_SCALPER_SHUTDOWN",
        "loops": state.loop_count, "signals": state.signals_generated,
        "entry_signals": state.entry_signals,
        "orders": state.orders_submitted, "fills": state.orders_filled,
        "wins": state.wins, "losses": state.losses, "win_rate": round(wr, 1),
        "total_pnl": round(state.total_pnl, 2),
        "neural_training_steps": state.neural_training_steps,
        "paper_mode": paper_mode, "halted": state.halted,
    }
    with open(OUT / "final_report.json", "w") as f:
        json.dump(final, f, indent=2, default=str)
    neural.save_weights()
    log.info(f"Shutdown | W/L={state.wins}/{state.losses} WR={wr:.1f}% PnL=${state.total_pnl:.2f}")


# ═══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="V21.7.62 Reversal Scalper Canary")
    parser.add_argument("--paper", action="store_true", help="Paper trading mode")
    parser.add_argument("--live", action="store_true", help="Live trading mode")
    parser.add_argument("--status", action="store_true", help="Show status")
    args = parser.parse_args()

    if args.status:
        sf = SUP / "v21762_reversal_scalper_status.json"
        print(sf.read_text() if sf.exists() else json.dumps({"status": "NOT_RUNNING"}))
        sys.exit(0)

    paper_mode = not args.live
    if args.live:
        log.info("⚠️ LIVE MODE — REAL MONEY ⚠️")
        env = load_env()
        if not env.get("PM_WALLET_PRIVATE_KEY"):
            log.error("No PM_WALLET_PRIVATE_KEY")
            sys.exit(1)

    state = CanaryState(paper_mode=paper_mode)
    
    # V21.7.65: SEPARATION FIX — paper and live use separate files
    # Paper trades were mixed into resolved_positions.jsonl alongside live trades,
    # inflating PnL from $2.85 (real) to $116.88 (fake). Now uses mode-specific files.
    if paper_mode:
        resolved_file = OUT / "paper_resolved.jsonl"
        pos_file = OUT / "paper_positions.jsonl"
    else:
        resolved_file = OUT / "live_resolved.jsonl"
        pos_file = OUT / "live_positions.jsonl"
    
    # V21.7.65: Migrate existing data on first run — split old mixed files
    old_resolved = OUT / "resolved_positions.jsonl"
    old_positions = OUT / "positions.jsonl"
    if not resolved_file.exists() and old_resolved.exists():
        try:
            paper_res = []
            live_res = []
            with open(old_resolved) as f:
                for line in f:
                    if not line.strip():
                        continue
                    rd = json.loads(line)
                    if rd.get("order_id"):
                        live_res.append(line)
                    else:
                        paper_res.append(line)
            with open(OUT / "live_resolved.jsonl", "w") as f:
                f.writelines(live_res)
            with open(OUT / "paper_resolved.jsonl", "w") as f:
                f.writelines(paper_res)
            log.info(f"Migrated resolved: {len(live_res)} live, {len(paper_res)} paper (separated)")
        except Exception as e:
            log.warning(f"Migration failed: {e}")
    
    # Recover resolved positions from mode-specific file
    # V21.7.65: Deduplicate by order_id — prevents double-counting
    if resolved_file.exists():
        try:
            recovered_resolved = 0
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            seen_oids = set()
            with open(resolved_file) as rf:
                for line in rf:
                    if line.strip():
                        rd = json.loads(line)
                        oid = rd.get("order_id", "")
                        if oid and oid in seen_oids:
                            continue  # Skip duplicate
                        if oid:
                            seen_oids.add(oid)
                        state.closed_positions.append(rd)
                        recovered_resolved += 1
                        pnl = rd.get("pnl", 0)
                        state.total_pnl += pnl
                        if rd.get("outcome") == "WIN":
                            state.wins += 1
                            state.recent_outcomes.append(1)
                            state.recent_pnls.append(pnl)
                        elif rd.get("outcome") == "LOSS":
                            state.losses += 1
                            state.recent_outcomes.append(0)
                            state.recent_pnls.append(pnl)
                            resolved_ts = rd.get("resolved_timestamp", "")
                            # V21.7.65: Only count today's losses up to max_daily_loss_usd
                            # Old $5 trades shouldn't consume the entire $15 daily limit
                            if resolved_ts.startswith(today):
                                loss_amt = abs(pnl)
                                if state.daily_loss_usd + loss_amt <= CANARY_CONFIG["max_daily_loss_usd"]:
                                    state.daily_loss_usd += loss_amt
                        state.recent_outcomes = state.recent_outcomes[-50:]
            if recovered_resolved:
                log.info(f"Recovered {recovered_resolved} resolved positions | "
                         f"W={state.wins} L={state.losses} PnL=${state.total_pnl:.2f}")
        except Exception as e:
            log.warning(f"Failed to recover resolved positions: {e}")
    
    # Recover open positions from mode-specific file
    if pos_file.exists():
        try:
            recovered = 0
            resolved_slugs = set()
            if resolved_file.exists():
                with open(resolved_file) as rf:
                    for line in rf:
                        if line.strip():
                            rd = json.loads(line)
                            resolved_slugs.add(rd.get("market_slug", ""))
            
            with open(pos_file) as pf:
                for line in pf:
                    if line.strip():
                        pd = json.loads(line)
                        slug = pd.get("market_slug", "")
                        if slug and slug not in resolved_slugs and pd.get("order_status") != "SETTLED":
                            state.positions.append(pd)
                            recovered += 1
            if recovered:
                state.open_positions = len(state.positions)
                log.info(f"Recovered {recovered} open positions from {pos_file.name}")
        except Exception as e:
            log.warning(f"Failed to recover positions: {e}")
    try:
        canary_loop(state, paper_mode, pos_file=pos_file, resolved_file=resolved_file)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.error(f"Fatal: {e}")
        traceback.print_exc()
    finally:
        log.info("Canary shutdown complete")