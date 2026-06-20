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
import json, os, sys, time, logging, signal, traceback, argparse, math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    "version": "V21.7.62",
    "cell_id": "5M_REVERSAL_SCALPER_CANARY",
    "interval": "5m",
    "assets": ["BTC", "ETH", "SOL", "XRP"],
    "entry_price_lo": 0.05,  # 5¢ minimum entry
    "entry_price_hi": 0.80,  # 80¢ maximum entry
    "position_size_usd": 5.00,
    "max_open_positions": 5,
    "max_daily_trades": 10,
    "max_daily_loss_usd": 15.0,
    "max_total_canary_loss_usd": 50.0,
    "max_consecutive_losses": 5,
    "order_type_preferred": "FAK",
    "order_type_acceptable": "FOK",
    "scan_interval_seconds": 5.0,  # 5s scan cadence
    "armed_interval_seconds": 1.0,  # 1s when near entry
}

# Reversal detection thresholds
REVERSAL_CONFIG = {
    "rsi_oversold": 40.0,      # RSI < 40 → oversold → expect Up reversal
    "rsi_overbought": 60.0,   # RSI > 60 → overbought → expect Down reversal
    "momentum_threshold": 0.0005,  # 0.05% momentum threshold
    "mean_reversion_window": 10,  # 10-period mean reversion window
    "mean_reversion_threshold": 0.0008,  # 0.08% deviation from MA
    "min_edge_pp": 3.0,         # 3pp minimum edge to enter (lowered from 5)
    "min_confidence": 0.52,     # 52% minimum confidence (lowered from 55%)
    "max_spread_cents": 5.0,    # Max 5¢ spread (widened from 3)
    "min_tte_seconds": 30,     # At least 30s to expiry
    "max_tte_seconds": 600,     # Max 10 min (current + next window)
    "vol_penalty_multiplier": 0.5,  # Reduced vol penalty
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
            except:
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
        except:
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
    except:
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
    except:
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
                    except:
                        outcomes = []
                    try:
                        token_ids = json.loads(mk.get("clobTokenIds", "[]")) if isinstance(mk.get("clobTokenIds"), str) else mk.get("clobTokenIds", [])
                    except:
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
                    })
                    seen_slugs.add(slug)
                    break  # One market per event
            except:
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
    except:
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
            from py_clob_client_v2 import ClobClientV2 as ClobClient, SignatureTypeV2
            _clob_client = ClobClient(
                CLOB_HOST, key=pk, chain_id=CHAIN_ID,
                signature_type=SignatureTypeV2.POLY_1271.value, funder=DW,
            )
            log.info("CLOB client initialized (POLY_1271)")
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
        order_args = OrderArgsV2(token_id=token_id, price=best_ask,
                                 size=CANARY_CONFIG["position_size_usd"], side="BUY")
        options = CreateOrderOptions(tick_size="0.01", neg_risk=False)

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
            except:
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
        except:
            pass

        if fill_status in ("live", "matched"):
            log.info(f"LIVE FILL: {side} {market['asset']} @ {best_ask*100:.1f}¢ | id={order_id[:20]}...")

    except Exception as e:
        result["error"] = str(e)
        result["status"] = "ERROR"
        log.error(f"Order error: {e}")
        try:
            clob_client.cancel_all()
        except:
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

def canary_loop(state: CanaryState, paper_mode: bool):
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

                if edge < REVERSAL_CONFIG["min_edge_pp"]:
                    continue
                if confidence < REVERSAL_CONFIG["min_confidence"]:
                    continue

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
                log.info(f"🎯 EXECUTING: {best['side']} {best['market']['asset']} | "
                         f"{best['market']['question'][:45]} | @ {best['best_ask']*100:.1f}¢")
                order_result = execute_order(
                    best["market"], best["side"], best["token_id"], best["best_ask"],
                    clob, paper_mode, best["neural_pred"], best["indicators"], best["confidence"]
                )

                if order_result["status"] in ("PAPER_FILLED", "ACKNOWLEDGED"):
                    state.orders_submitted += 1
                    state.daily_trades += 1
                    state.open_positions += 1

                    pos = {
                        "timestamp": now.isoformat(),
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
                    }
                    state.positions.append(pos)

                    with open(OUT / "positions.jsonl", "a") as f:
                        f.write(json.dumps(pos, default=str) + "\n")

                    if order_result["status"] == "ACKNOWLEDGED" and order_result.get("fill_status") in ("live", "matched"):
                        state.orders_filled += 1
                else:
                    state.orders_rejected += 1

            # ─── Check expired positions (resolve paper trades) ───
            for pos in list(state.positions):
                tte_remaining = pos.get("tte_at_entry", 0) - (time.time() - loop_start)
                if tte_remaining <= 0 or pos.get("resolved"):
                    # Position should be settled
                    # Get current price to determine outcome
                    asset = pos["asset"]
                    current_price = get_asset_price(asset)
                    entry_price = pos.get("entry_price", 0)
                    side = pos["side"]

                    # For paper trades, we need to check if the market resolved
                    # Since we can't easily check resolution, we'll use price comparison
                    # If UP and price went up → win; if DOWN and price went down → win
                    # This is a simplification — in production we'd check actual market resolution
                    if pos.get("features_at_entry"):
                        entry_price_actual = pos["features_at_entry"].get("current_price", 0)
                        if entry_price_actual > 0:
                            if side == "UP" and current_price > entry_price_actual:
                                outcome = "WIN"
                                pnl = (1.0 - entry_price) * (CANARY_CONFIG["position_size_usd"] / entry_price)
                            elif side == "DOWN" and current_price < entry_price_actual:
                                outcome = "WIN"
                                pnl = (1.0 - entry_price) * (CANARY_CONFIG["position_size_usd"] / entry_price)
                            else:
                                outcome = "LOSS"
                                pnl = -entry_price * (CANARY_CONFIG["position_size_usd"] / entry_price)
                        else:
                            outcome = "UNKNOWN"
                            pnl = 0.0
                    else:
                        outcome = "UNKNOWN"
                        pnl = 0.0

                    if outcome != "UNKNOWN":
                        pos["outcome"] = outcome
                        pos["pnl"] = round(pnl, 4)
                        pos["resolved_timestamp"] = now.isoformat()
                        state.closed_positions.append(pos)
                        state.positions.remove(pos)
                        state.open_positions -= 1
                        state.total_pnl += pnl

                        if outcome == "WIN":
                            state.wins += 1
                            state.consecutive_losses = 0
                        else:
                            state.losses += 1
                            state.consecutive_losses += 1
                            state.daily_loss_usd += abs(pnl)

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

                        with open(OUT / "resolved_positions.jsonl", "a") as f:
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
    try:
        canary_loop(state, paper_mode)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.error(f"Fatal: {e}")
        traceback.print_exc()
    finally:
        log.info("Canary shutdown complete")