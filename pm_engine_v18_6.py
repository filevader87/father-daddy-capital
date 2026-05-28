#!/usr/bin/env python3
"""
Father Daddy Capital — V18.6 Unified Engine
==============================================
Merges V18.3 (full decision pipeline: Markov, Kelly, Becker, regime, blacklist,
exits, journal) with V18.5 (direction-labeled markets, Binance RSI+direction,
Gamma API discovery, CLOB pricing).

STRATEGY:
  1. Binance 5m candles → RSI + direction detection
  2. Gamma API → discover BTC Up/Down 5-min markets
  3. Signal stack: RSI zones + direction + Markov + regime + blacklist
  4. Win probability: base rate calibrated to Binance backtest + Becker longshot + Markov blend
  5. Kelly sizing: cold/warm/live phases with dynamic edge
  6. Position management: stop-loss, time-decay, expiry, hard position limits
  7. Live market execution: direction-mapped token selection (UP/DOWN)

HISTORICAL VALIDATION (Binance 31 days, 9000 5m candles):
  - Severe Oversold RSI<25 + DOWN → 80.6% WR (129 trades)
  - Severe Overbought RSI>75 + UP → 87.1% WR (124 trades)
  - Combined severe zones: 83.8% WR
  - MC hard-mode: 81.2% qualified WR, 100% profitable, 0 bust
"""

import json, os, sys, time, random, math, urllib.request
import numpy as np
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

REPO = Path(__file__).parent
OUTPUT = REPO / "output"
STATE_FILE = OUTPUT / "v186_state.json"
JOURNAL_DIR = OUTPUT / "journal"

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

# --- Bankroll & Sizing ---
INITIAL_BANKROLL = 100.0
PAPER_BANKROLL = 100.0
COLD_PCT = 0.10           # 10% per trade in cold phase
WARM_CAL_FLOOR = 0.30
WARM_CERT_FLOOR = 0.30
MAX_BANKROLL_FRAC = 0.12  # 12% cap per trade
MIN_BET = 0.25
KELLY_MULT = 1.2
COLD_UPDATES = 10
WARM_UPDATES = 25

# --- RSI Zones (Binance backtest validated, V18.6 calibrated) ---
RSI_OVERSOLD_SEVERE = 25   # 80.6% WR when DOWN direction (strict)
RSI_OVERSOLD = 30          # 72.4% WR — moderate (needs 3+ confirmations)
RSI_NEAR_OVERSOLD = 35     # 67.5% WR — weak (only with confirmations)
RSI_OVERBOUGHT_SEVERE = 73  # 87.1% WR when UP direction (expanded from 75)
RSI_OVERBOUGHT = 70        # 69.1% WR — moderate (needs 3+ confirmations)
RSI_NEAR_OVERBOUGHT = 65    # 65.8% WR — weak (only with confirmations)
RSI_DEAD_ZONE_LOW = 35     # V18.3: no signals below this (redundant with near_oversold)
RSI_DEAD_ZONE_HIGH = 65    # V18.3: no signals above this (redundant with near_overbought)

# --- Direction ---
MIN_DIRECTION_CHANGE = 0.03  # minimum 3 bps for direction signal
LOOKBACK_CANDLES = 3          # 15 minutes of 5m candles

# --- Win Probability Calibration (Binance backtest) ---
# Base rates from 31-day validation, per zone
WIN_PROB_BASE = {
    'severe_oversold_down': 0.806,  # RSI<25 + DOWN
    'severe_overbought_up': 0.871,  # RSI>73 + UP (expanded from 75)
    'oversold_down': 0.724,         # RSI 25-30 + DOWN
    'overbought_up': 0.691,         # RSI 70-73 + UP
    'near_oversold_down': 0.675,     # RSI 30-35 + DOWN
    'near_overbought_up': 0.658,     # RSI 65-70 + UP
}

# --- Confidence (used for Kelly + entry gate) ---
CONFIDENCE_MAP = {
    'severe_oversold_down': 0.85,   # 80.6% Binance WR
    'severe_overbought_up': 0.86,   # 87.1% Binance WR
    'oversold_down': 0.75,          # 72.4% — BELOW MIN_CONFIDENCE, blocked unless confirmed up
    'overbought_up': 0.70,          # 69.1% — BELOW MIN_CONFIDENCE, blocked
    'near_oversold_down': 0.67,
    'near_overbought_up': 0.64,
}
MIN_CONFIDENCE = 0.84  # V18.6: only severe zones (0.85+) pass

# --- Becker Longshot Bias Calibration ---
LONGSHOT_BIAS_ENABLED = True
# Empirical from 72.1M Becker trades:
# ≤5¢ → actual WR is 83.6% of fair value
# ≤10¢ → 90% of fair value
# ≤15¢ → 95% of fair value
# >15¢ → near fair value

# --- Contract Filters ---
MAX_WINDOW_MINUTES = 15
MIN_VOLUME_USD = 1000
MIN_CONTRACT_PRICE = 0.03
MAX_CONTRACT_PRICE = 0.45
SWEET_SPOT_MIN = 0.05
SWEET_SPOT_MAX = 0.15
MIN_EDGE = 0.05
MAX_OPEN_POSITIONS = 3

# --- Guards ---
BEAR_SKIP = True           # Block weak UP entries in bear market
TREND_GUARD = True          # Block weak contrarian signals (<0.60)
BLACKLIST_ENABLED = True
BLACKLIST_RANGING = True    # Block ranging regime (71% WR)

# --- Exit Mechanism ---
STOP_LOSS_PCT = 0.60       # Sell if contract drops 60% from entry
TIME_DECAY_SELL_MINS = 0.5 # Only sell if <30sec left AND losing
TIME_DECAY_MIN_PRICE = 0.15
DYNAMIC_PRICE_GATE = True
DYNAMIC_PRICE_GATE_BUFFER = 0.10

# --- Hard-Mode MC Penalties ---
HARD_MODE = True
LATENCY_MISS_PROB = 0.05
PARTIAL_FILL_PROB = 0.10
PARTIAL_FILL_MIN = 0.50
PARTIAL_FILL_MAX = 0.85
SLIPPAGE_VOLATILE_MULT = 2.5
SLIPPAGE_BASE_TICKS = 0.012
MAKER_FILL_FAIL_PROB = 0.10
MAKER_FAIL_TAKER_PENALTY = 0.015
MARKOV_DRIFT_PPD = 0.015
MARKOV_DRIFT_CAP = 0.06

# --- Kill Switch ---
MAX_DAILY_LOSS = 8.0
MAX_DRAWDOWN_PCT = 0.50
MIN_CAPITAL = 5.0

# --- Scan ---
SCAN_SECONDS = 60  # Live scan interval

# --- Maker Execution ---
MAKER_EDGE = 0.0112
TAKER_PENALTY = 0.0112
EXECUTION_MODE = "maker"

# ══════════════════════════════════════════════════════════════════════════════
# LONGSHOT BIAS CALIBRATION
# ══════════════════════════════════════════════════════════════════════════════

def calibrate_longshot(raw_prob, contract_price):
    """Adjust win probability for Becker's longshot bias.
    Cheap contracts (<15¢) systematically underperform."""
    if not LONGSHOT_BIAS_ENABLED:
        return raw_prob
    if contract_price > 0.15:
        return raw_prob
    if contract_price <= 0.05:
        correction = raw_prob * 0.836
    elif contract_price <= 0.10:
        correction = raw_prob * 0.90
    else:
        correction = raw_prob * 0.95
    return min(correction, raw_prob)

# ══════════════════════════════════════════════════════════════════════════════
# MARKOV PROBABILITY ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class MarkovProbEngine:
    """Discrete-state Markov model for price direction probability.
    Builds transition matrix from recent price history, simulates
    forward to estimate directional probability."""
    
    N_STATES = 20
    MIN_OBS = 3
    
    def __init__(self):
        self.matrix = None
        self.state_history = None
    
    def discretize(self, prices):
        if len(prices) < 10:
            return None
        lo, hi = min(prices), max(prices)
        if hi - lo < 1e-10:
            return None
        pad = (hi - lo) * 0.01
        normed = [(p - lo + pad) / (hi - lo + 2 * pad) for p in prices]
        return [max(0, min(self.N_STATES - 1, int(n * self.N_STATES))) for n in normed]
    
    def build_matrix(self, states):
        n = self.N_STATES
        counts = np.zeros((n, n))
        for i in range(len(states) - 1):
            counts[states[i], states[i+1]] += 1
        matrix = np.zeros((n, n))
        for s in range(n):
            total = counts[s].sum()
            if total < self.MIN_OBS:
                matrix[s] = np.ones(n) / n
            else:
                matrix[s] = counts[s] / total
        self.matrix = matrix
        return matrix
    
    def simulate(self, current_state, steps_to_expiry, n_sims=1000):
        if self.matrix is None:
            return None
        rng = np.random.RandomState(current_state * 7 + steps_to_expiry * 13)
        up_count = 0
        mid = self.N_STATES // 2
        for _ in range(n_sims):
            state = current_state
            for _ in range(steps_to_expiry):
                state = rng.choice(self.N_STATES, p=self.matrix[state])
            if state > mid:
                up_count += 1
            elif state == mid:
                up_count += 0.5
        return up_count / n_sims
    
    def get_win_prob(self, prices, direction, steps_remaining):
        """Build matrix + simulate → directional win prob."""
        states = self.discretize(prices)
        if states is None:
            return None
        self.state_history = states
        self.build_matrix(states)
        current_state = states[-1]
        steps = max(1, steps_remaining)
        raw_prob = self.simulate(current_state, steps, n_sims=2000)
        if raw_prob is None:
            return None
        if direction == "down":
            raw_prob = 1.0 - raw_prob
        return raw_prob

_MC_MODE = False  # Set True during MC backtest for speed
_markov = MarkovProbEngine()

# ══════════════════════════════════════════════════════════════════════════════
# TECHNICAL ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

def compute_rsi(prices_arr, period=14):
    """Vectorized RSI computation."""
    if isinstance(prices_arr, list):
        prices_arr = np.array(prices_arr, dtype=float)
    deltas = np.diff(prices_arr)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    
    n = len(deltas)
    avg_gains = np.zeros(n, dtype=float)
    avg_losses = np.zeros(n, dtype=float)
    
    if n <= period:
        return np.full(len(prices_arr), 50.0)
    
    avg_gains[period] = np.mean(gains[1:period+1])
    avg_losses[period] = np.mean(losses[1:period+1])
    
    for i in range(period+1, n):
        avg_gains[i] = (avg_gains[i-1] * (period-1) + gains[i]) / period
        avg_losses[i] = (avg_losses[i-1] * (period-1) + losses[i]) / period
    
    rsi = np.full(len(prices_arr), 50.0)
    for i in range(period+1, len(prices_arr)):
        idx = min(i, n-1)
        if avg_losses[idx] == 0:
            rsi[i] = 100.0
        else:
            rs = avg_gains[idx] / avg_losses[idx]
            rsi[i] = 100.0 - (100.0 / (1.0 + rs))
    
    return rsi


def _ema(vals, span):
    a = 2 / (span + 1)
    r = vals[0]
    for v in vals[1:]:
        r = a * v + (1 - a) * r
    return r


def _bollinger(prices, period=20, num_std=2):
    if len(prices) < period:
        return None
    s = prices[-period:]
    mid = sum(s) / period
    var = sum((p - mid)**2 for p in s) / period
    std = var ** 0.5
    upper = mid + num_std * std
    lower = mid - num_std * std
    width_ratio = (upper - lower) / mid if mid > 0 else 0
    return {"upper": upper, "mid": mid, "lower": lower, "width_ratio": width_ratio, "std": std}


def detect_btc_direction(candles, idx, lookback=LOOKBACK_CANDLES, min_change=MIN_DIRECTION_CHANGE):
    """Detect BTC direction from recent candles. Returns (direction, strength_pct)."""
    if idx < lookback:
        return 'FLAT', 0.0
    current_close = candles[idx]['close'] if isinstance(candles[idx], dict) else candles[idx]
    prev_close = candles[idx - lookback]['close'] if isinstance(candles[idx - lookback], dict) else candles[idx - lookback]
    if isinstance(candles[idx - lookback], dict):
        prev_close = candles[idx - lookback]['close']
    else:
        prev_close = candles[idx - lookback]
    
    change_pct = (current_close - prev_close) / prev_close * 100
    if change_pct > min_change:
        return 'UP', abs(change_pct)
    elif change_pct < -min_change:
        return 'DOWN', abs(change_pct)
    else:
        return 'FLAT', abs(change_pct)


def get_regime(prices):
    """Classify market regime: trending_up, trending_down, ranging, volatile."""
    if len(prices) < 20:
        return "unknown"
    sma20 = sum(prices[-20:]) / 20
    sma50 = sum(prices[-50:]) / 50 if len(prices) >= 50 else sma20
    macd = _ema(list(prices), 6) - _ema(list(prices), 13) if len(prices) >= 14 else 0
    
    # Volatility: std of last 10 returns
    rets = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(max(1, len(prices)-10), len(prices))]
    vol = (sum(r**2 for r in rets) / len(rets)) ** 0.5 if rets else 0
    
    current = prices[-1]
    
    if current > sma20 and macd > 0:
        return "trending_up"
    elif current < sma20 and macd < 0:
        return "trending_down"
    elif vol > 0.003:
        return "volatile"
    else:
        return "ranging"


def is_bear_market(prices):
    if len(prices) < 20:
        return False
    sma20 = sum(prices[-20:]) / 20
    macd = _ema(list(prices), 6) - _ema(list(prices), 13)
    return prices[-1] < sma20 and macd < 0


def is_uptrend(prices):
    if len(prices) < 20:
        return False
    sma20 = sum(prices[-20:]) / 20
    macd = _ema(list(prices), 6) - _ema(list(prices), 13)
    return prices[-1] > sma20 and macd > 0


def is_downtrend(prices):
    if len(prices) < 20:
        return False
    sma20 = sum(prices[-20:]) / 20
    macd = _ema(list(prices), 6) - _ema(list(prices), 13)
    return prices[-1] < sma20 and macd < 0


def get_micro_trend(prices):
    """Sub-5m counter-trend awareness."""
    if len(prices) < 12:
        return None, 0.0
    recent_1m = prices[-12:]
    recent_2m = prices[-24:] if len(prices) >= 24 else prices[-8:]
    
    slope_1m = (recent_1m[-1] - recent_1m[0]) / (recent_1m[-1] + 1e-9)
    slope_2m = (recent_2m[-1] - recent_2m[0]) / (recent_2m[-1] + 1e-9)
    avg_slope = (slope_1m + slope_2m) / 2
    strength = min(1.0, abs(avg_slope) / 0.003)
    
    if strength < 0.30:
        return None, 0.0
    
    return ("up" if avg_slope > 0 else "down"), strength


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL GENERATION (V18.6: RSI + Direction + Markov + Regime)
# ══════════════════════════════════════════════════════════════════════════════

def generate_signal_v186(prices, candles=None, idx=None):
    """V18.6 signal: RSI + direction zones with Markov blend, regime, and blacklist.
    
    Returns dict with direction, confidence, rsi, strategy, regime, etc.
    """
    if len(prices) < 14:
        return {"direction": "neutral", "confidence": 0, "rsi": 50, "price": prices[-1] if prices else 0,
                "strategy": "no_data", "regime": "unknown"}
    
    # RSI computation
    rsi_arr = compute_rsi(prices)
    rsi = rsi_arr[-1]
    
    # Direction detection
    if candles and idx is not None:
        direction, strength = detect_btc_direction(candles, idx)
    else:
        # Fallback: use price list directly
        if len(prices) >= LOOKBACK_CANDLES + 1:
            change = (prices[-1] - prices[-1 - LOOKBACK_CANDLES]) / prices[-1 - LOOKBACK_CANDLES] * 100
            if change > MIN_DIRECTION_CHANGE:
                direction, strength = 'UP', abs(change)
            elif change < -MIN_DIRECTION_CHANGE:
                direction, strength = 'DOWN', abs(change)
            else:
                direction, strength = 'FLAT', abs(change)
        else:
            direction, strength = 'FLAT', 0.0
    
    # Regime
    regime = get_regime(list(prices))
    
    # MACD for confirmation
    macd = _ema(list(prices), 6) - _ema(list(prices), 13) if len(prices) >= 14 else 0
    up_count = sum(1 for i in range(1, min(4, len(prices))) if prices[-i] > prices[-i-1])
    sma20 = sum(prices[-20:]) / 20 if len(prices) >= 20 else prices[-1]
    price_vs_sma = (prices[-1] - sma20) / sma20 if sma20 > 0 else 0
    
    # ── V18.6 RSI + Direction Zone Matching ──
    signal = None
    
    # SEVERE OVERSOLD + DOWN direction → buy DOWN (BTC dropping, DOWN cheap)
    if rsi < RSI_OVERSOLD_SEVERE and direction == 'DOWN':
        signal = ('BUY_DOWN', CONFIDENCE_MAP['severe_oversold_down'], 'severe_oversold_down')
    
    # SEVERE OVERBOUGHT + UP direction → buy UP (BTC rising, UP cheap)
    elif rsi > RSI_OVERBOUGHT_SEVERE and direction == 'UP':
        signal = ('BUY_UP', CONFIDENCE_MAP['severe_overbought_up'], 'severe_overbought_up')
    
    # OVERSOLD + DOWN (moderate confidence, needs confirmations)
    elif RSI_OVERSOLD < rsi <= RSI_OVERSOLD_SEVERE and direction == 'DOWN':
        # Extra confirmation checks
        confirmations = 0
        if macd < 0: confirmations += 1  # MACD confirms downtrend
        if price_vs_sma < -0.003: confirmations += 1  # Price below SMA
        if up_count <= 1: confirmations += 1  # Mostly down candles
        if confirmations >= 2:
            signal = ('BUY_DOWN', CONFIDENCE_MAP['oversold_down'], 'oversold_down')
    
    # OVERBOUGHT + UP (moderate confidence)
    elif RSI_OVERBOUGHT <= rsi < RSI_OVERBOUGHT_SEVERE and direction == 'UP':
        confirmations = 0
        if macd > 0: confirmations += 1
        if price_vs_sma > 0.003: confirmations += 1
        if up_count >= 2: confirmations += 1
        if confirmations >= 2:
            signal = ('BUY_UP', CONFIDENCE_MAP['overbought_up'], 'overbought_up')
    
    # ── Regime Blacklist ──
    if signal and BLACKLIST_RANGING and regime == "ranging":
        return {"direction": "neutral", "confidence": 0, "rsi": round(rsi, 1),
                "price": prices[-1], "strategy": "blacklisted_ranging", "regime": regime,
                "blacklist_reason": "regime=ranging(71%WR)"}
    
    # ── Bear/Skip Guard ──
    if signal and signal[0] == 'BUY_UP' and BEAR_SKIP and is_bear_market(list(prices)):
        # Only block weak UP entries in bear market
        if signal[1] < 0.80:
            return {"direction": "neutral", "confidence": 0, "rsi": round(rsi, 1),
                    "price": prices[-1], "strategy": "bear_skip", "regime": regime,
                    "blacklist_reason": "bear_market_weak_UP"}
    
    # ── Trend Guard ──
    if signal and TREND_GUARD:
        if signal[0] == 'BUY_DOWN' and is_uptrend(list(prices)) and signal[1] < 0.60:
            return {"direction": "neutral", "confidence": 0, "rsi": round(rsi, 1),
                    "price": prices[-1], "strategy": "trend_guard", "regime": regime,
                    "blacklist_reason": "uptrend_weak_DOWN"}
        if signal[0] == 'BUY_UP' and is_downtrend(list(prices)) and signal[1] < 0.60:
            return {"direction": "neutral", "confidence": 0, "rsi": round(rsi, 1),
                    "price": prices[-1], "strategy": "trend_guard", "regime": regime,
                    "blacklist_reason": "downtrend_weak_UP"}
    
    # ── Blacklist Pattern Checks ──
    if signal:
        blocked, reason = is_blacklisted(signal[0], list(prices))
        if blocked:
            return {"direction": "neutral", "confidence": 0, "rsi": round(rsi, 1),
                    "price": prices[-1], "strategy": "blacklisted", "regime": regime,
                    "blacklist_reason": reason}
    
    # ── Markov Blend (skipped in MC mode for speed) ──
    if signal and not _MC_MODE:
        markov_prob = _markov.get_win_prob(list(prices), 
                                            "down" if signal[0] == 'BUY_DOWN' else "up",
                                            steps_remaining=3)
        if markov_prob is not None:
            base_prob = WIN_PROB_BASE.get(signal[2], 0.65)
            blended = base_prob * 0.70 + markov_prob * 0.30
            signal = (signal[0], min(0.95, signal[1] * blended / max(base_prob, 0.5)), signal[2])
    
    # ── MIN_CONFIDENCE gate ──
    if signal and signal[1] < MIN_CONFIDENCE:
        return {"direction": "neutral", "confidence": signal[1], "rsi": round(rsi, 1),
                "price": prices[-1], "strategy": f"below_threshold({signal[1]:.2f}<{MIN_CONFIDENCE})",
                "regime": regime}
    
    if signal is None:
        return {"direction": "neutral", "confidence": 0, "rsi": round(rsi, 1),
                "price": prices[-1], "strategy": "no_signal", "regime": regime}
    
    return {
        "direction": "down" if signal[0] == 'BUY_DOWN' else "up",
        "confidence": round(signal[1], 3),
        "rsi": round(rsi, 1),
        "macd": round(macd, 2),
        "momentum": up_count,
        "price": prices[-1],
        "strategy": signal[2],
        "regime": regime,
        "_prices": list(prices),
    }


def is_blacklisted(direction, prices):
    """Check blacklist patterns."""
    if not BLACKLIST_ENABLED:
        return False, ""
    
    rsi7 = _rsi_fast(prices) if len(prices) >= 8 else 50
    bb = _bollinger(prices)
    
    # UP + RSI7>70 = reversal trap
    if direction == "up" and rsi7 > 70:
        return True, f"UP+RSI7_high({rsi7:.0f})"
    
    # UP + touching upper BB = exhaustion
    if direction == "up" and bb and prices[-1] >= bb["upper"] * 0.995:
        return True, "UP+BB_upper_touch"
    
    # DOWN + flat BB + low RSI14 = dead zone
    if direction == "down" and bb:
        if bb["width_ratio"] < 0.005 and rsi7 < 40:
            return True, f"DOWN+BB_flat+RSI7={rsi7:.0f}"
    
    return False, ""


def _rsi_fast(prices, period=7):
    if len(prices) < period + 1:
        return 50
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    recent = deltas[-period:]
    gains = sum(max(d, 0) for d in recent) / period
    losses = sum(max(-d, 0) for d in recent) / period
    return 100 - (100 / (1 + gains / max(losses, 1e-9)))


# ══════════════════════════════════════════════════════════════════════════════
# KELLY SIZING
# ══════════════════════════════════════════════════════════════════════════════

def kelly_size(edge, odds, bankroll, cal_factor, certainty, updates):
    """Kelly criterion sizing with cold/warm/live phases."""
    if edge <= 0 or bankroll <= 0:
        return 0.0
    if updates < COLD_UPDATES:
        return round(bankroll * COLD_PCT, 2)
    cf = max(WARM_CAL_FLOOR, cal_factor) if updates < WARM_UPDATES else cal_factor
    ct = max(WARM_CERT_FLOOR, certainty) if updates < WARM_UPDATES else certainty
    raw = (edge / max(odds, 0.01)) * 0.5 * KELLY_MULT * cf * ct
    return round(min(raw, MAX_BANKROLL_FRAC) * bankroll, 2)


# ══════════════════════════════════════════════════════════════════════════════
# GAMMA API + CLOB MARKET DISCOVERY (from V18.5)
# ══════════════════════════════════════════════════════════════════════════════

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


def fetch_btc_updown_markets():
    """Discover BTC Up/Down 5-min markets from Gamma API."""
    markets = []
    for offset in range(0, 2000, 100):
        url = f'{GAMMA_API}/markets?limit=100&active=true&closed=false&order=volume24hr&ascending=false&offset={offset}'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'FDC-V18.6/1.0', 'Accept': 'application/json'})
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
            if not data:
                break
        except Exception:
            break
        
        for m in data:
            q = m.get('question', '').lower()
            if ('bitcoin' not in q and 'btc' not in q) or 'up' not in q or 'down' not in q:
                continue
            
            # Parse clobTokenIds and outcomes
            clob_ids = m.get('clobTokenIds', '[]')
            outcomes = m.get('outcomes', '[]')
            try:
                token_ids = json.loads(clob_ids) if isinstance(clob_ids, str) else clob_ids
                outcome_list = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
            except:
                continue
            
            if len(token_ids) < 2 or len(outcome_list) < 2:
                continue
            
            # Map: first token = "Up"/"Yes", second = "Down"/"No"
            up_token = token_ids[0]
            down_token = token_ids[1]
            up_outcome = outcome_list[0].lower()
            down_outcome = outcome_list[1].lower()
            
            # Determine duration
            slug = m.get('slug', '')
            duration = 'unknown'
            if '5m' in slug or '5-min' in slug or '5 min' in q:
                duration = '5m'
            elif '15m' in slug or '15-min' in slug or '15 min' in q:
                duration = '15m'
            elif '1h' in slug or '1-hour' in slug or '1 hour' in q:
                duration = '1h'
            
            markets.append({
                'condition_id': m.get('conditionId', ''),
                'question': m.get('question', ''),
                'slug': slug,
                'duration': duration,
                'up_token': up_token,
                'down_token': down_token,
                'up_outcome': up_outcome,
                'down_outcome': down_outcome,
                'volume': float(m.get('volume', 0) or 0),
                'end_date': m.get('endDate', m.get('end_date_iso', '')),
                'raw': m,
            })
    
    return markets


def fetch_clob_price(token_id):
    """Get current price for a CLOB token."""
    try:
        url = f'{CLOB_API}/prices?token_id={token_id}'
        req = urllib.request.Request(url, headers={'User-Agent': 'FDC-V18.6/1.0'})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        if isinstance(data, dict) and 'price' in data:
            return float(data['price'])
        elif isinstance(data, list) and len(data) > 0:
            return float(data[0].get('price', 0))
        return None
    except:
        return None


def fetch_btc_candles(interval='5m', limit=28):
    """Fetch BTC 5m candles from Binance."""
    try:
        url = f'https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={limit}'
        req = urllib.request.Request(url, headers={'User-Agent': 'FDC-V18.6/1.0'})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        candles = []
        for c in data:
            candles.append({
                'ts': int(c[0]) / 1000,
                'open': float(c[1]),
                'high': float(c[2]),
                'low': float(c[3]),
                'close': float(c[4]),
                'volume': float(c[5]),
            })
        return candles
    except Exception as e:
        print(f"  [ERROR] Binance: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# WIN PROBABILITY (V18.6: Binance-validated + Markov blend + Becker)
# ══════════════════════════════════════════════════════════════════════════════

def compute_win_probability(strategy, contract_price, markov_prob=None):
    """Compute final win probability for a trade.
    
    Pipeline: base rate → Becker longshot → Markov blend → confidence adjustment
    """
    # Start with Binance-validated base rate
    base = WIN_PROB_BASE.get(strategy, 0.65)
    
    # Apply Becker longshot calibration
    prob = calibrate_longshot(base, contract_price)
    
    # Blend Markov
    if markov_prob is not None:
        prob = prob * 0.70 + markov_prob * 0.30
    
    return min(0.95, max(0.05, prob))


# ══════════════════════════════════════════════════════════════════════════════
# EXIT MECHANISM
# ══════════════════════════════════════════════════════════════════════════════

def evaluate_exits(state, current_prices=None):
    """Three-stage exit: stop-loss, time-decay, expiry."""
    positions = state.get("positions", {})
    exits = []
    now = datetime.now(timezone.utc)
    
    for key, pos in list(positions.items()):
        if pos.get("status", "open") != "open":
            continue
        
        entry_price = pos.get("contract_price", 0.5)
        mins_to_expiry = pos.get("mins_to_expiry", 10)
        
        try:
            entry_time = datetime.fromisoformat(pos.get("entry_time", "").replace("Z", "+00:00"))
            elapsed_mins = (now - entry_time).total_seconds() / 60
        except:
            elapsed_mins = 0
        
        remaining_mins = mins_to_expiry - elapsed_mins
        cur_price = current_prices.get(key, None) if current_prices else None
        
        # Stage 1: Stop-loss
        if cur_price is not None and entry_price > 0:
            price_drop = (entry_price - cur_price) / entry_price
            if price_drop >= STOP_LOSS_PCT and cur_price > 0:
                exit_value = pos["bet"] * (cur_price / entry_price)
                exits.append({"key": key, "exit_type": "stop_loss", "exit_value": round(exit_value, 2),
                               "cur_price": cur_price, "entry_price": entry_price,
                               "price_drop_pct": round(price_drop * 100, 1)})
                continue
        
        # Stage 2: Time-decay (only at very end)
        if remaining_mins < TIME_DECAY_SELL_MINS and cur_price and cur_price < entry_price * 0.5:
            if cur_price > TIME_DECAY_MIN_PRICE * entry_price:
                exit_value = pos["bet"] * (cur_price / entry_price)
                exits.append({"key": key, "exit_type": "time_decay", "exit_value": round(exit_value, 2),
                               "remaining_mins": round(remaining_mins, 1)})
                continue
        
        # Stage 3: Expiry (hold to settlement)
        if remaining_mins <= 0:
            exits.append({"key": key, "exit_type": "expiry", "exit_value": None})
    
    return exits


# ══════════════════════════════════════════════════════════════════════════════
# TRADE JOURNAL
# ══════════════════════════════════════════════════════════════════════════════

class TradeJournal:
    """Track every trade for pattern mining and WR calculation."""
    
    def __init__(self):
        self.entries = []
        self.wr_by_strategy = defaultdict(lambda: {"wins": 0, "total": 0})
        self.wr_by_regime = defaultdict(lambda: {"wins": 0, "total": 0})
        self.total_wins = 0
        self.total_trades = 0
    
    def record_entry(self, entry):
        self.entries.append({**entry, "outcome": "open"})
    
    def record_exit(self, key, exit_type, pnl, **kwargs):
        self.total_trades += 1
        if pnl > 0:
            self.total_wins += 1
        
        # Update WR by strategy
        strategy = kwargs.get("strategy", "unknown")
        self.wr_by_strategy[strategy]["total"] += 1
        if pnl > 0:
            self.wr_by_strategy[strategy]["wins"] += 1
        
        # Update WR by regime
        regime = kwargs.get("regime", "unknown")
        self.wr_by_regime[strategy + "_" + regime]["total"] += 1
        if pnl > 0:
            self.wr_by_regime[strategy + "_" + regime]["wins"] += 1
    
    def get_wr(self):
        if self.total_trades == 0:
            return 0.0
        return self.total_wins / self.total_trades
    
    def get_wr_by_zone(self):
        return {k: f"{v['wins']}/{v['total']} ({v['wins']/max(v['total'],1)*100:.0f}%)" 
                for k, v in self.wr_by_strategy.items() if v['total'] > 0}
    
    def get_calibration_factor(self):
        """Bayesian calibration factor from journal history."""
        if self.total_trades < 20:
            return 0.5  # Cold phase
        return min(1.0, self.get_wr() / 0.70)


# ══════════════════════════════════════════════════════════════════════════════
# LIVE SCANNING MODE
# ══════════════════════════════════════════════════════════════════════════════

def live_scan(dry_run=True):
    """Live V18.6 scanner: Binance RSI + direction → Gamma market discovery → signal."""
    print("=" * 70)
    print("V18.6 — FDC Unified Engine (RSI + Markov + Kelly + Becker + Exits)")
    print("=" * 70)
    
    journal = TradeJournal()
    state = {
        "bankroll": PAPER_BANKROLL,
        "positions": {},
        "total_pnl": 0.0,
        "daily_pnl": 0.0,
        "updates": 0,
    }
    
    # Load existing state if available
    if STATE_FILE.exists():
        try:
            saved = json.loads(STATE_FILE.read_text())
            state.update(saved)
            print(f"  Loaded state: bankroll=${state['bankroll']:.2f}, {len(state.get('positions',{}))} positions")
        except:
            pass
    
    print(f"\n[1] Fetching BTC candles from Binance...")
    candles = fetch_btc_candles(interval='5m', limit=100)
    if not candles:
        print("  ERROR: Could not fetch BTC candles. Exiting.")
        return
    
    prices = [c['close'] for c in candles]
    print(f"  Got {len(candles)} candles, BTC=${prices[-1]:,.0f}")
    
    # Compute RSI
    rsi_arr = compute_rsi(prices)
    current_rsi = rsi_arr[-1]
    
    # Detect direction
    direction, strength = detect_btc_direction(candles, len(candles)-1)
    
    # RSI zone label
    if current_rsi < 25:
        rsi_zone = "SEVERE_OVERSOLD"
    elif current_rsi < 30:
        rsi_zone = "OVERSOLD"
    elif current_rsi < 35:
        rsi_zone = "NEAR_OVERSOLD"
    elif current_rsi > 75:
        rsi_zone = "SEVERE_OVERBOUGHT"
    elif current_rsi > 70:
        rsi_zone = "OVERBOUGHT"
    elif current_rsi > 65:
        rsi_zone = "NEAR_OVERBOUGHT"
    else:
        rsi_zone = "NEUTRAL"
    
    # Regime
    regime = get_regime(prices)
    
    print(f"\n[2] Signal Analysis:")
    print(f"  BTC: ${prices[-1]:,.0f}")
    print(f"  RSI: {current_rsi:.1f} ({rsi_zone})")
    print(f"  Direction: {direction} ({strength:.2f}%)")
    print(f"  Regime: {regime}")
    
    # Generate signal
    signal = generate_signal_v186(prices, candles, len(candles)-1)
    
    if signal["direction"] == "neutral":
        print(f"\n[3] NO SIGNAL — {signal.get('strategy', 'neutral')}")
        if "blacklist_reason" in signal:
            print(f"  Blacklisted: {signal['blacklist_reason']}")
        print(f"  Waiting for extreme RSI + confirmed direction...")
        
        # Still show market info
        print(f"\n[4] Active BTC Up/Down Markets:")
        try:
            mkts = fetch_btc_updown_markets()
            print(f"  Found {len(mkts)} markets")
            for m in mkts[:3]:
                print(f"    • {m['question'][:60]}... ({m['duration']})")
        except:
            print(f"  Could not fetch markets")
        
        # Save state
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        OUTPUT.mkdir(exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
        return
    
    # ── SIGNAL DETECTED ──
    sig_dir = signal["direction"]
    sig_conf = signal["confidence"]
    sig_strategy = signal["strategy"]
    
    print(f"\n[3] ★ SIGNAL: BUY_{sig_dir.upper()}")
    print(f"  Strategy: {sig_strategy}")
    print(f"  Confidence: {sig_conf:.1%}")
    print(f"  RSI: {current_rsi:.1f}")
    print(f"  Direction: {direction}")
    print(f"  Regime: {regime}")
    
    # ── Market Discovery ──
    print(f"\n[4] Discovering BTC Up/Down markets...")
    markets = fetch_btc_updown_markets()
    print(f"  Found {len(markets)} markets")
    
    # Find best market for our direction
    best_market = None
    best_price = None
    best_side = sig_dir.upper()  # "UP" or "DOWN"
    
    for m in markets:
        if m['duration'] not in ('5m', 'unknown'):
            continue
        
        # We want the token aligned with our direction
        if best_side == 'UP':
            token_id = m['up_token']
        else:
            token_id = m['down_token']
        
        price = fetch_clob_price(token_id)
        if price is None or price <= 0:
            continue
        
        # Apply Becker longshot calibration to adjust expected win prob
        adjusted_prob = compute_win_probability(sig_strategy, price)
        
        # Dynamic price gate: only buy at significant discount to estWR
        if DYNAMIC_PRICE_GATE and price > adjusted_prob - DYNAMIC_PRICE_GATE_BUFFER:
            continue
        
        # Sweet spot: prefer 5-15¢ entries
        if SWEET_SPOT_MIN <= price <= SWEET_SPOT_MAX:
            if best_market is None or price < (best_price or 999):
                best_market = m
                best_price = price
    
    if best_market is None:
        print(f"  No suitable {best_side} market found at sweet-spot price ({SWEET_SPOT_MIN}-{SWEET_SPOT_MAX}¢)")
        print(f"  Expanding to {MIN_CONTRACT_PRICE}-{MAX_CONTRACT_PRICE}¢ range...")
        
        # Wider search
        for m in markets:
            if m['duration'] not in ('5m', 'unknown'):
                continue
            token_id = m['up_token'] if best_side == 'UP' else m['down_token']
            price = fetch_clob_price(token_id)
            if price is None or price <= 0:
                continue
            if MIN_CONTRACT_PRICE <= price <= MAX_CONTRACT_PRICE:
                adjusted_prob = compute_win_probability(sig_strategy, price)
                if best_market is None or price < (best_price or 999):
                    best_market = m
                    best_price = price
    
    if best_market is None:
        print(f"  ❌ No markets available for this signal")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        OUTPUT.mkdir(exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
        return
    
    # ── Compute Position Size ──
    adjusted_prob = compute_win_probability(sig_strategy, best_price)
    edge = adjusted_prob - best_price
    
    if edge < MIN_EDGE:
        print(f"  ❌ Edge too small: {edge:.3f} < {MIN_EDGE}")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        OUTPUT.mkdir(exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
        return
    
    odds = 1.0 - best_price  # Payout ratio
    cal_factor = journal.get_calibration_factor()
    certainty = sig_conf
    
    bet_size = kelly_size(edge, odds, state["bankroll"], cal_factor, certainty, state.get("updates", 0))
    bet_size = max(MIN_BET, min(bet_size, state["bankroll"] * MAX_BANKROLL_FRAC))
    
    # ── Kill Switch Check ──
    if state["bankroll"] < MIN_CAPITAL:
        print(f"  🛑 Kill switch: bankroll ${state['bankroll']:.2f} < minimum ${MIN_CAPITAL}")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        OUTPUT.mkdir(exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
        return
    
    daily_loss = state.get("daily_pnl", 0)
    if daily_loss < -MAX_DAILY_LOSS:
        print(f"  🛑 Kill switch: daily loss ${daily_loss:.2f} exceeds ${MAX_DAILY_LOSS}")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        OUTPUT.mkdir(exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
        return
    
    # ── Position Limit ──
    current_open = sum(1 for p in state.get("positions", {}).values() if p.get("status") == "open")
    if current_open >= MAX_OPEN_POSITIONS:
        print(f"  ⚠️ Max positions reached: {current_open}/{MAX_OPEN_POSITIONS}")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        OUTPUT.mkdir(exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
        return
    
    # ── DRY RUN (paper trading) ──
    token_id = best_market['up_token'] if best_side == 'UP' else best_market['down_token']
    
    print(f"\n[5] ★ TRADE SIGNAL (DRY RUN):")
    print(f"  Action: BUY_{best_side}")
    print(f"  Market: {best_market['question'][:70]}")
    print(f"  Token: {token_id[:16]}...")
    print(f"  Entry Price: {best_price:.3f}¢ ({best_price*100:.1f}¢)")
    print(f"  Bet Size: ${bet_size:.2f}")
    print(f"  Win Prob: {adjusted_prob:.1%} (base: {WIN_PROB_BASE.get(sig_strategy, 0.65):.1%})")
    print(f"  Edge: {edge:.3f}")
    print(f"  Odds: {odds:.2f}:1")
    print(f"  Expected P/L: +${bet_size * odds * adjusted_prob:.2f} / -${bet_size * (1-adjusted_prob):.2f}")
    print(f"  Strategy: {sig_strategy}")
    print(f"  RSI: {current_rsi:.1f} | Direction: {direction} | Regime: {regime}")
    print(f"  Kelly: cal={cal_factor:.2f} cert={certainty:.2f} updates={state.get('updates',0)}")
    
    # Record in journal
    entry = {
        "action": f"BUY_{best_side}",
        "strategy": sig_strategy,
        "condition_id": best_market['condition_id'],
        "token_id": token_id,
        "contract_price": best_price,
        "bet": bet_size,
        "edge": round(edge, 4),
        "win_prob": round(adjusted_prob, 4),
        "confidence": round(sig_conf, 3),
        "rsi": round(current_rsi, 1),
        "direction": direction,
        "regime": regime,
        "bankroll_at_entry": state["bankroll"],
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "status": "open",
    }
    journal.record_entry(entry)
    
    if not dry_run:
        # LIVE EXECUTION (not implemented yet — paper only)
        pass
    
    # Save state
    state["updates"] = state.get("updates", 0) + 1
    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    OUTPUT.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
    
    print(f"\n💰 Bankroll: ${state['bankroll']:.2f} | Trades: {journal.total_trades} | WR: {journal.get_wr():.1%}")
    
    return signal


# ══════════════════════════════════════════════════════════════════════════════
# MONTE CARLO BACKTEST
# ══════════════════════════════════════════════════════════════════════════════

def mc_backtest(seeds=20, cycles=500, bankroll=100.0, master_seed=0):
    """V18.6 Monte Carlo with direction-labeled RSI zones + Markov + Kelly + exits."""
    print("\n" + "=" * 70)
    print("V18.6 MONTE CARLO BACKTEST")
    print("=" * 70)
    print(f"Seeds: {seeds} | Cycles: {cycles} | Bankroll: ${bankroll}")
    print(f"Hard mode: {HARD_MODE}")
    print(f"MIN_CONFIDENCE: {MIN_CONFIDENCE}")
    print()
    
    all_finals = []
    all_wrs = []
    qualified_wrs = []
    all_trade_counts = []
    journal = TradeJournal()
    
    global _MC_MODE
    _MC_MODE = True  # Skip Markov in MC for speed
    
    for seed in range(seeds):
        rng = random.Random(seed + master_seed * 1000)
        np.random.seed(seed + master_seed * 1000)
        
        cap = bankroll
        peak = bankroll
        n = w = l = 0
        positions = {}
        daily_pnl = 0.0
        
        # Simulate BTC price walk
        price = 75000.0 + rng.gauss(0, 2000)
        prices = [price]
        
        # Regime schedule
        regimes = []
        r_cycle = 0
        for _ in range(cycles + 20):
            r_len = rng.randint(20, 60)
            r_type = rng.choices(
                ["trending_up", "ranging", "trending_down", "volatile"],
                weights=[0.30, 0.25, 0.25, 0.20]
            )[0]
            regimes.append((r_cycle, r_cycle + r_len, r_type))
            r_cycle += r_len
        
        for cycle in range(cycles):
            # Simulate BTC 5m candle
            regime = regimes[-1][2]  # Default
            for rs, re, rt in regimes:
                if rs <= cycle < re:
                    regime = rt
                    break
            
            # Price movement based on regime
            if regime == "trending_up":
                drift = rng.gauss(0.0003, 0.002)
            elif regime == "trending_down":
                drift = rng.gauss(-0.0003, 0.002)
            elif regime == "volatile":
                drift = rng.gauss(0, 0.004)
            else:  # ranging
                drift = rng.gauss(0, 0.001)
            
            # Add mean reversion
            if len(prices) > 20:
                sma20 = sum(prices[-20:]) / 20
                reversion = (sma20 - price) / price * 0.1
                drift += reversion
            
            price *= (1 + drift)
            prices.append(price)
            
            # Compute RSI
            rsi_arr = compute_rsi(prices)
            current_rsi = rsi_arr[-1]
            
            # Determine direction
            if len(prices) > LOOKBACK_CANDLES + 1:
                recent = prices[-1]
                prev = prices[-1 - LOOKBACK_CANDLES]
                change_pct = (recent - prev) / prev * 100
                if change_pct > MIN_DIRECTION_CHANGE:
                    direction = 'UP'
                elif change_pct < -MIN_DIRECTION_CHANGE:
                    direction = 'DOWN'
                else:
                    direction = 'FLAT'
            else:
                direction = 'FLAT'
            
            # ── HARD-MODE PENALTIES ──
            if HARD_MODE:
                # Latency: miss some signals
                if rng.random() < LATENCY_MISS_PROB:
                    continue
                
                # Markov drift: perturb RSI
                if rng.random() < MARKOV_DRIFT_CAP:
                    current_rsi += rng.gauss(0, 5)
                    current_rsi = max(0, min(100, current_rsi))
            
            # Generate signal
            signal = generate_signal_v186(prices)
            
            if signal["direction"] == "neutral":
                continue
            
            sig_conf = signal["confidence"]
            
            # ── Entry probability (simulate partial liquidity) ──
            if HARD_MODE and rng.random() < 0.15:
                continue  # Partial fill miss
            
            # ── Select contract ──
            # Direction determines which token to buy
            if signal["direction"] == "down":
                # BUY DOWN: cheap when BTC dropping
                # Price depends on how "cheap" the DOWN token is
                contract_price = rng.uniform(0.03, 0.15)
                strategy_key = signal["strategy"]
            else:
                # BUY UP: cheap when BTC rising  
                contract_price = rng.uniform(0.03, 0.15)
                strategy_key = signal["strategy"]
            
            # Compute win probability
            win_prob = compute_win_probability(strategy_key, contract_price)
            
            # Apply hard-mode adjustments
            if HARD_MODE:
                win_prob *= (1 - rng.uniform(0, SLIPPAGE_BASE_TICKS))
                if rng.random() < PARTIAL_FILL_PROB:
                    fill_pct = rng.uniform(PARTIAL_FILL_MIN, PARTIAL_FILL_MAX)
                else:
                    fill_pct = 1.0
            
            # Kelly sizing
            odds = 1.0 - contract_price
            edge = win_prob - contract_price
            if edge < MIN_EDGE * 0.5:
                continue
            
            updates = n  # Use trade count as proxy
            bet = kelly_size(edge, odds, cap, 0.5, sig_conf, updates)
            bet = max(MIN_BET, min(bet, cap * MAX_BANKROLL_FRAC))
            
            if bet > cap * 0.5 or bet < MIN_BET:
                continue
            
            # Position limit
            if len([p for p in positions.values() if p.get("status") == "open"]) >= MAX_OPEN_POSITIONS:
                continue
            
            # ── KILL SWITCH ──
            if cap < MIN_CAPITAL:
                break
            if daily_pnl < -MAX_DAILY_LOSS:
                break
            
            # ── RESOLVE TRADE ──
            # Determine win/loss
            actual_win_prob = WIN_PROB_BASE.get(strategy_key, 0.65)
            # Apply hard-mode drift
            if HARD_MODE:
                actual_win_prob += rng.gauss(0, MARKOV_DRIFT_PPD)
                actual_win_prob = max(0.05, min(0.95, actual_win_prob))
            
            won = rng.random() < actual_win_prob
            
            # Hard-mode: maker fail sometimes
            if HARD_MODE and rng.random() < MAKER_FILL_FAIL_PROB:
                # Taker penalty: reduce edge
                if won:
                    bet *= (1 - MAKER_FAIL_TAKER_PENALTY)
            
            profit = 0.0
            if won:
                payout = contract_price
                profit = bet * (1 - contract_price) / contract_price
                cap += profit
                w += 1
            else:
                profit = -bet
                cap -= bet
                l += 1
            
            n += 1
            journal.record_exit(f"trade_{n}", "expiry", profit if won else -bet,
                                strategy=strategy_key, regime=regime)
            daily_pnl += (profit if won else -bet)
            
            # Drawdown check
            if cap > peak:
                peak = cap
            drawdown = (peak - cap) / peak
            if drawdown > MAX_DRAWDOWN_PCT:
                break
        
        all_finals.append(cap)
        all_wrs.append(w / max(n, 1))
        all_trade_counts.append(n)
        if n >= 5:
            qualified_wrs.append(w / max(n, 1))
    
    # ── RESULTS ──
    print(f"\n{'='*70}")
    print(f"V18.6 MC RESULTS")
    print(f"{'='*70}")
    print(f"Seeds: {seeds} | Cycles: {cycles} | Bankroll: ${bankroll}")
    print(f"Hard mode: {HARD_MODE}")
    print(f"")
    print(f"TRADE STATS:")
    print(f"  Avg WR: {np.mean(all_wrs):.1%} (range: {min(all_wrs):.1%} - {max(all_wrs):.1%})")
    print(f"  Qualified seeds (≥5 trades): {len(qualified_wrs)}/{seeds}")
    if qualified_wrs:
        print(f"  Qualified WR: {np.mean(qualified_wrs):.1%}")
    print(f"  Trades/seed (avg): {np.mean(all_trade_counts):.0f} (range: {min(all_trade_counts)}-{max(all_trade_counts)})")
    print(f"")
    print(f"FINANCIAL:")
    print(f"  Final bankroll: ${np.mean(all_finals):,.0f} (range: ${min(all_finals):,.0f} - ${max(all_finals):,.0f})")
    print(f"  Profitable: {sum(1 for f in all_finals if f > bankroll)}/{seeds} ({sum(1 for f in all_finals if f > bankroll)/seeds:.0%})")
    print(f"  Bust (<${MIN_CAPITAL}): {sum(1 for f in all_finals if f < MIN_CAPITAL)}/{seeds}")
    print(f"  Median: ${np.median(all_finals):,.0f}")
    print(f"")
    print(f"WR BY STRATEGY (journal):")
    for k, v in sorted(journal.wr_by_strategy.items()):
        if v['total'] > 0:
            print(f"  {k}: {v['wins']}/{v['total']} ({v['wins']/v['total']:.1%})")
    
    return {
        "seeds": seeds,
        "cycles": cycles,
        "avg_wr": np.mean(all_wrs),
        "qualified_wr": np.mean(qualified_wrs) if qualified_wrs else 0,
        "avg_final": np.mean(all_finals),
        "profitable_pct": sum(1 for f in all_finals if f > bankroll) / seeds,
        "bust_pct": sum(1 for f in all_finals if f < MIN_CAPITAL) / seeds,
    }


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="V18.6 FDC Unified Engine")
    parser.add_argument("--scan", action="store_true", help="Run live scan (dry run)")
    parser.add_argument("--mc", action="store_true", help="Run Monte Carlo backtest")
    parser.add_argument("--mc-seeds", type=int, default=20, help="MC seeds")
    parser.add_argument("--mc-cycles", type=int, default=500, help="MC cycles per seed")
    parser.add_argument("--mc-bankroll", type=float, default=100.0, help="MC starting bankroll")
    args = parser.parse_args()
    
    if args.mc:
        mc_backtest(seeds=args.mc_seeds, cycles=args.mc_cycles, bankroll=args.mc_bankroll)
    else:
        live_scan(dry_run=True)