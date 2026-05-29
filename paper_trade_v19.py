#!/usr/bin/env python3
"""
V19.1 Refined 5m/15m BTC Up/Down Paper Trader
================================================
V19 + 7 refinements from live trade analysis + Prediction Arena paper:
1. Price-tier entry gate: reject 8-60¢ dead zone (only allow ≤8¢ or ≥60¢)
2. Correlation limit: max 2 same-direction, max 3 total positions
3. Confluence-weighted sizing: 6-7/10→3%, 7-8/10→4%, 8+/10→5-6%
4. Dynamic max price by vol: high_vol max 8¢, normal max 15¢, low_vol max 20¢
5. Stricter RSI: DOWN requires RSI<30 (not <45), UP requires RSI>65 (not >55)
6. Time-in-window decay: peak confluence at 7-9min, decay at edges
7. Re-entry cooldown: 15min after stop-loss
"""

import json, os, sys, time, traceback, math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pm_engine_v18_8 import (
    MIN_CONFIDENCE, RSI_OVERSOLD_SEVERE, RSI_OVERBOUGHT_SEVERE,
    SCAN_SECONDS, MIN_BET, MAX_OPEN_POSITIONS,
    compute_rsi, detect_btc_direction, generate_signal_v188,
    fetch_btc_candles, compute_win_probability, kelly_size, TradeJournal,
    get_regime,
)

import urllib.request

OUTPUT = Path(__file__).parent / "output"
OUTPUT.mkdir(exist_ok=True)
STATE_FILE = OUTPUT / "v19_paper_state.json"
LOG_FILE = Path(__file__).parent / "paper_trades" / "scanner_v19.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# ═══════════════════════════════════════════════════════════════════════════════
# V19 CONFIGURATION
# ════════════════════════════════════════════════════════════════════════════════
BANKROLL = 400.0
MAX_OPEN_POSITIONS = 3
MAX_SAME_DIRECTION = 2          # Max 2 positions in same direction (correlation limit)
POSITION_SIZE_PCT = 0.03
MAX_POSITION_PCT = 0.08
MIN_CONFIDENCE_FAIR_PRICE = 0.70
MIN_CONFLUENCE = 6           # Require 6/10 confluence to trade (Krajekis: 7/10, we're more aggressive)
DAILY_LOSS_LIMIT = 3         # Stop after 3 losses in a day (Krajekis: 2-4 rule-based losses)
DAILY_LOSS_PCT = 0.05        # Also stop if down 5% of bankroll in a day
COOLDOWN_MINS = 15           # Re-entry cooldown after stop-loss (minutes)

# V19.1: Price-tier dead zone — reject entries priced 8-60¢ unless fair-price ≥60¢
DEAD_ZONE_LOW = 0.08
DEAD_ZONE_HIGH = 0.60

# Time-window filter (Krajekis: avoid early window, prefer mid-to-late)
SERIES_CONFIG = [
    {"slug": "btc-up-or-down-15m", "label": "15m", "window_mins": 15, "min_remaining": 5, "max_remaining": 12},  # 5-12 min left (mid-window)
    {"slug": "btc-up-or-down-5m", "label": "5m", "window_mins": 5,  "min_remaining": 2, "max_remaining": 4},    # 2-4 min left (late window)
]

# Exit strategies (same as V18.9)
STOP_LOSS_PCT = 0.50
TAKE_PROFIT_PRICE = 0.90
TRAILING_STOP_PCT = 0.40
TRAILING_ACTIVATE_MINS = 2.0
TIME_DECAY_SELL_MINS = 1.0
TIME_DECAY_MIN_PRICE = 0.03

# Tiers
TIER_CONFIG = {
    "severe_oversold":    {"size": 0.10, "max_price": 0.30},
    "severe_overbought":  {"size": 0.10, "max_price": 0.30},
    "oversold_down":      {"size": 0.06, "max_price": 0.15},
    "overbought_up":      {"size": 0.06, "max_price": 0.15},
    "direction_down_cheap": {"size": 0.03, "max_price": 0.08},
    "direction_up_cheap":   {"size": 0.03, "max_price": 0.08},
    "confluence_down":      {"size": 0.04, "max_price": 0.30},
    "confluence_up":        {"size": 0.04, "max_price": 0.30},
}


# ═══════════════════════════════════════════════════════════════════════════════
# V19 INDICATORS: VWAP, EMA, ATR
# ═══════════════════════════════════════════════════════════════════════════════

def compute_ema(prices, period):
    """Compute Exponential Moving Average."""
    if len(prices) < period:
        return prices[-1] if prices else 0
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def compute_atr(candles, period=14):
    """Compute Average True Range from OHLC candles."""
    if len(candles) < period + 1:
        return 0
    trs = []
    for i in range(1, len(candles)):
        c = candles[i]
        p = candles[i-1]
        high = c.get('high', c['close'])
        low = c.get('low', c['close'])
        prev_close = p.get('close', p['close'])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0
    return sum(trs[-period:]) / period


def compute_vwap(candles):
    """Compute Volume Weighted Average Price from recent candles."""
    total_vol = 0
    total_vp = 0
    for c in candles[-20:]:  # Last 20 candles
        h = c.get('high', c['close'])
        l = c.get('low', c['close'])
        typical = (h + l + c['close']) / 3
        vol = c.get('volume', 1)
        total_vp += typical * vol
        total_vol += vol
    return total_vp / total_vol if total_vol > 0 else candles[-1]['close']


def compute_macd(prices, fast=12, slow=26, signal=9):
    """Compute MACD line and signal line."""
    if len(prices) < slow + signal:
        return 0, 0, 0
    ema_fast = compute_ema(prices, fast)
    ema_slow = compute_ema(prices, slow)
    macd_line = ema_fast - ema_slow

    # MACD histogram approximation
    macd_vals = []
    for i in range(slow, len(prices)):
        ef = compute_ema(prices[:i+1], fast)
        es = compute_ema(prices[:i+1], slow)
        macd_vals.append(ef - es)

    if len(macd_vals) >= signal:
        signal_line = compute_ema(macd_vals, signal)
    else:
        signal_line = 0

    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def get_session(utc_hour):
    """
    Classify trading session by UTC hour (Krajekis session logic).
    Returns (session_name, signal_weight):
    - asia: low vol, mean-reversion bias, weight 0.7
    - london_open: sweeps + reversals, weight 0.8
    - ny_open: high vol directional, weight 1.0 (best)
    - london_close: exhaustion, weight 0.8
    - off_hours: thin/unclear, weight 0.5
    """
    est_hour = (utc_hour - 5) % 24  # UTC → EST

    if 19 <= est_hour or est_hour < 3:   # 7PM-3AM EST = Asia
        return "asia", 0.7
    elif 2 <= est_hour < 5:                # 2-5AM EST = London kill zone
        return "london_open", 0.8
    elif 7 <= est_hour < 11:               # 7-11AM EST = NY open/overlap
        return "ny_open", 1.0
    elif 10 <= est_hour < 12:              # 10AM-12PM EST = London close
        return "london_close", 0.8
    elif 3 <= est_hour < 7:                # 3-7AM EST = off hours
        return "off_hours", 0.5
    elif 12 <= est_hour < 19:              # 12PM-7PM EST = afternoon
        return "ny_afternoon", 0.6
    else:
        return "off_hours", 0.5


def classify_volatility(atr, price):
    """
    ATR-based volatility classification (Krajekis vol regime).
    Returns (vol_regime, max_entry_price):
    - low_vol: buy expensive 70-95¢ with trend alignment
    - medium_vol: buy moderate 30-70¢
    - high_vol: buy cheap 5-20¢ for mean-reversion
    """
    atr_pct = atr / price * 100 if price > 0 else 0

    if atr_pct < 0.3:      # <0.3% ATR → low vol
        return "low_vol", 0.95
    elif atr_pct < 0.8:     # 0.3-0.8% → medium vol
        return "medium_vol", 0.55
    else:                   # >0.8% → high vol
        return "high_vol", 0.20


def compute_confluence(rsi, direction, regime, ema21, ema50, vwap, price, macd_hist, session, atr_vol, signal_dir):
    """
    Krajekis confluence scoring (0-10).
    Each factor contributes 0-1 points:
    1. RSI extreme: oversold<30/overbought>70
    2. Direction alignment: signal direction matches price movement
    3. EMA alignment: 21>50=bullish, 21<50=bearish
    4. VWAP position: price relative to VWAP confirms signal
    5. MACD histogram: expanding in signal direction
    6. Session quality: NY=1.0, London=0.8, Asia=0.7, off=0.5
    7. ATR vol regime: appropriate entry price for vol level
    8. Trend consistency: regime matches signal direction
    9. RSI divergence: momentum divergence with signal
    10. Price structure: trading above/below key levels
    """
    score = 0.0
    details = []

    # 1. RSI extreme or zone (1 point) — V19.1: stricter RSI thresholds
    if signal_dir == "DOWN" and rsi < 25:
        score += 1.0
        details.append("RSI<25")
    elif signal_dir == "DOWN" and rsi < 30:
        score += 0.8
        details.append("RSI<30")
    elif signal_dir == "DOWN" and rsi < 38:
        score += 0.3
        details.append("RSI<38")
    elif signal_dir == "UP" and rsi > 75:
        score += 1.0
        details.append("RSI>75")
    elif signal_dir == "UP" and rsi > 65:
        score += 0.8
        details.append("RSI>65")
    elif signal_dir == "UP" and rsi > 55:
        score += 0.3
        details.append("RSI>55")
    else:
        details.append("RSI_meh")

    # 2. Direction alignment (1 point)
    if direction in ("UP", "DOWN"):
        score += 1.0
        details.append(f"Dir={direction}")
    else:
        details.append("Dir=FLAT")

    # 3. EMA alignment (1 point)
    if ema21 > ema50 and signal_dir == "UP":
        score += 1.0
        details.append("EMA_bullish")
    elif ema21 < ema50 and signal_dir == "DOWN":
        score += 1.0
        details.append("EMA_bearish")
    elif abs(ema21 - ema50) / ema50 < 0.001:  # Flat EMA = neutral
        score += 0.3
        details.append("EMA_flat")
    else:
        details.append("EMA_against")

    # 4. VWAP position (1 point)
    if signal_dir == "UP" and price > vwap:
        score += 1.0
        details.append("Above_VWAP")
    elif signal_dir == "DOWN" and price < vwap:
        score += 1.0
        details.append("Below_VWAP")
    elif signal_dir == "UP" and price < vwap * 1.002:  # Within 0.2% of VWAP
        score += 0.5
        details.append("Near_VWAP")
    else:
        details.append("VWAP_against")

    # 5. MACD histogram (1 point)
    if signal_dir == "UP" and macd_hist > 0:
        score += 1.0
        details.append("MACD_expanding")
    elif signal_dir == "DOWN" and macd_hist < 0:
        score += 1.0
        details.append("MACD_declining")
    elif abs(macd_hist) < 0.0001:
        score += 0.3
        details.append("MACD_neutral")
    else:
        details.append("MACD_against")

    # 6. Session quality (1 point, weighted by session)
    session_name, session_weight = session
    score += session_weight
    details.append(f"S={session_name[:3]}({session_weight:.1f})")

    # 7. ATR vol regime match (1 point)
    vol_regime, max_entry = atr_vol
    if vol_regime == "high_vol" and signal_dir in ("UP", "DOWN"):
        score += 1.0  # High vol = better for cheap counter/mean-rev
        details.append("HighVol_good")
    elif vol_regime == "low_vol" and regime in ("trending_up", "trending_down"):
        score += 0.8  # Low vol + trend = good for expensive directional
        details.append("LowVol_trend")
    elif vol_regime == "medium_vol":
        score += 0.5
        details.append("MedVol")
    else:
        details.append("Vol_meh")

    # 8. Trend consistency (1 point)
    if regime == "trending_up" and signal_dir == "UP":
        score += 1.0
        details.append("Trend_match")
    elif regime == "trending_down" and signal_dir == "DOWN":
        score += 1.0
        details.append("Trend_match")
    elif regime == "ranging":
        score += 0.3
        details.append("Ranging")
    else:
        details.append("Trend_against")

    # 9. RSI divergence (1 point - simplified)
    # Price making new low but RSI not = bullish divergence (good for UP signal)
    # Price making new high but RSI not = bearish divergence (good for DOWN signal)
    score += 0.5  # Neutral default (divergence calculation needs more history)
    details.append("NoDiv")

    # 10. Price structure (1 point)
    if signal_dir == "UP" and price > ema21:
        score += 1.0
        details.append("Above_EMA21")
    elif signal_dir == "DOWN" and price < ema21:
        score += 1.0
        details.append("Below_EMA21")
    else:
        score += 0.3
        details.append("Price_mixed")

    return min(10.0, score), details


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except:
            pass
    return {
        "bankroll": BANKROLL,
        "positions": {},
        "total_pnl": 0.0,
        "trades": [],
        "resolutions": [],
        "last_scan": None,
        "daily_losses": 0,
        "daily_loss_amount": 0.0,
        "daily_reset": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "daily_trades": 0,
        "last_stop_loss_time": None,
        "version": "v19.1",
    }


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def get_clob_price(token_id):
    try:
        url = f"{CLOB_API}/price?token_id={token_id}&side=buy"
        req = urllib.request.Request(url, headers={'User-Agent': 'FDC-V19/1.0'})
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        if isinstance(data, dict) and 'price' in data:
            return float(data['price'])
    except:
        pass
    return None


def gamma_get(path, params=None):
    try:
        query = ""
        if params:
            query = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{GAMMA_API}/{path}{query}"
        req = urllib.request.Request(url, headers={'User-Agent': 'FDC-V19/1.0'})
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except Exception as e:
        log(f"[WARN] Gamma API error: {e}")
        return []


def fetch_5m_15m_markets():
    """Discover active 5m and 15m BTC Up/Down markets via Gamma series API."""
    now = datetime.now(timezone.utc)
    all_markets = []

    for config in SERIES_CONFIG:
        slug = config["slug"]
        label = config["label"]
        window_mins = config["window_mins"]

        events = gamma_get("events", {
            "limit": "10",
            "series_slug": slug,
            "active": "true",
            "closed": "false",
            "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date_max": (now + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

        for event in events:
            markets = event.get("markets", [])
            for m in markets:
                if not m.get("active", False) or m.get("closed", False):
                    continue

                question = m.get("question", "")
                if "bitcoin" not in question.lower() and "btc" not in question.lower():
                    continue

                clob_str = m.get("clobTokenIds", "[]")
                if isinstance(clob_str, str):
                    try:
                        clob = json.loads(clob_str)
                    except:
                        clob = []
                else:
                    clob = clob_str
                if len(clob) < 2:
                    continue

                prices = m.get("outcomePrices", [])
                if isinstance(prices, str):
                    try:
                        prices = json.loads(prices)
                    except:
                        prices = []
                outcomes = m.get("outcomes", [])
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except:
                        outcomes = []

                if len(prices) < 2:
                    continue

                up_idx = 0
                down_idx = 1
                if len(outcomes) >= 2:
                    if outcomes[1].lower() == "up":
                        up_idx = 1
                        down_idx = 0

                up_price = float(prices[up_idx]) if prices[up_idx] else 0.5
                down_price = float(prices[down_idx]) if len(prices) > down_idx and prices[down_idx] else 0.5

                up_clob = get_clob_price(clob[up_idx]) if up_idx < len(clob) else None
                down_clob = get_clob_price(clob[down_idx]) if down_idx < len(clob) else None
                if up_clob:
                    up_price = up_clob
                if down_clob:
                    down_price = down_clob

                end_str = m.get("endDate", event.get("endDate", ""))
                try:
                    if end_str.endswith("Z"):
                        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    else:
                        end_dt = datetime.fromisoformat(end_str)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    minutes_left = (end_dt - now).total_seconds() / 60
                except:
                    continue

                cheap_side = "Up" if up_price <= down_price else "Down"
                cheap_price = min(up_price, down_price)

                all_markets.append({
                    "question": question,
                    "slug": m.get("slug", event.get("slug", "")),
                    "condition_id": m.get("conditionId", ""),
                    "event_slug": event.get("slug", ""),
                    "up_token_id": clob[up_idx] if up_idx < len(clob) else "",
                    "down_token_id": clob[down_idx] if down_idx < len(clob) else "",
                    "up_price": up_price,
                    "down_price": down_price,
                    "cheap_side": cheap_side,
                    "cheap_price": cheap_price,
                    "end_date": end_str,
                    "minutes_left": minutes_left,
                    "window_mins": window_mins,
                    "label": label,
                    "volume24hr": m.get("volume24hr", 0),
                    "market_active": m.get("active", False),
                    "series_slug": slug,
                })

    all_markets.sort(key=lambda m: m["minutes_left"])
    return all_markets


def evaluate_exits(state):
    """Check open positions for exit conditions using live CLOB prices."""
    now = datetime.now(timezone.utc)
    to_close = []

    for pos_id, pos in list(state.get("positions", {}).items()):
        if pos.get("status") != "open":
            continue

        entry_price = pos.get("entry_price", 0.5)
        entry_time = pos.get("entry_time", pos.get("timestamp", ""))
        try:
            if entry_time.endswith("Z"):
                et = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
            else:
                et = datetime.fromisoformat(entry_time)
            if et.tzinfo is None:
                et = et.replace(tzinfo=timezone.utc)
            elapsed_mins = (now - et).total_seconds() / 60
        except:
            elapsed_mins = 999

        token_id = pos.get("token_id", "")
        cur_price = get_clob_price(token_id) if token_id else None

        resolved = check_market_resolution(pos)
        if resolved:
            to_close.append((pos_id, resolved))
            continue

        if cur_price is None:
            continue

        peak_price = pos.get("peak_price", entry_price)
        if cur_price > peak_price:
            pos["peak_price"] = cur_price
            peak_price = cur_price

        # Exit 1: Stop-Loss
        price_drop = (entry_price - cur_price) / entry_price if entry_price > 0 else 0
        if price_drop >= STOP_LOSS_PCT and cur_price > 0:
            exit_value = pos["bet"] * (cur_price / entry_price)
            to_close.append((pos_id, {
                "exit_type": "stop_loss", "exit_price": cur_price,
                "exit_value": round(exit_value, 2), "pnl": round(exit_value - pos["bet"], 2),
                "price_drop_pct": round(price_drop * 100, 1), "elapsed_mins": round(elapsed_mins, 1),
            }))
            continue

        # Exit 2: Take-Profit
        if cur_price >= TAKE_PROFIT_PRICE:
            exit_value = pos["bet"] * (cur_price / entry_price)
            to_close.append((pos_id, {
                "exit_type": "take_profit", "exit_price": cur_price,
                "exit_value": round(exit_value, 2), "pnl": round(exit_value - pos["bet"], 2),
                "elapsed_mins": round(elapsed_mins, 1),
            }))
            continue

        # Exit 3: Trailing Stop
        if elapsed_mins >= TRAILING_ACTIVATE_MINS and peak_price > entry_price:
            drop_from_peak = (peak_price - cur_price) / peak_price if peak_price > 0 else 0
            if drop_from_peak >= TRAILING_STOP_PCT:
                exit_value = pos["bet"] * (cur_price / entry_price)
                to_close.append((pos_id, {
                    "exit_type": "trailing_stop", "exit_price": cur_price,
                    "exit_value": round(exit_value, 2), "pnl": round(exit_value - pos["bet"], 2),
                    "peak_price": peak_price, "elapsed_mins": round(elapsed_mins, 1),
                }))
                continue

        # Exit 4: Time-Decay
        end_str = pos.get("market_end_date", "")
        try:
            if end_str.endswith("Z"):
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            else:
                end_dt = datetime.fromisoformat(end_str)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            remaining = (end_dt - now).total_seconds() / 60
        except:
            remaining = 999

        if remaining < TIME_DECAY_SELL_MINS and cur_price < entry_price * 0.5:
            if cur_price > TIME_DECAY_MIN_PRICE:
                exit_value = pos["bet"] * (cur_price / entry_price)
                to_close.append((pos_id, {
                    "exit_type": "time_decay", "exit_price": cur_price,
                    "exit_value": round(exit_value, 2), "pnl": round(exit_value - pos["bet"], 2),
                    "remaining_mins": round(remaining, 2), "elapsed_mins": round(elapsed_mins, 1),
                }))
                continue

        pos["last_price"] = cur_price
        pos["last_check"] = now.isoformat()

    for pos_id, exit_info in to_close:
        pos = state["positions"][pos_id]
        exit_value = exit_info.get("exit_value", 0)
        pnl = exit_info.get("pnl", exit_value - pos["bet"])

        state["bankroll"] += exit_value
        pos["status"] = "closed"
        pos["outcome"] = exit_info["exit_type"]
        pos["pnl"] = pnl
        pos["exit_price"] = exit_info.get("exit_price", 0)
        pos["exit_type"] = exit_info["exit_type"]
        pos["closed_at"] = now.isoformat()

        # Track daily losses
        if pnl < 0:
            state["daily_losses"] = state.get("daily_losses", 0) + 1
            state["daily_loss_amount"] = state.get("daily_loss_amount", 0) + abs(pnl)
            # V19.1 #7: Track stop-loss time for cooldown
            if exit_info["exit_type"] == "stop_loss":
                state["last_stop_loss_time"] = now.isoformat()

        for t in state.get("trades", []):
            if t.get("id") == pos_id:
                t.update({"status": "closed", "outcome": exit_info["exit_type"], "pnl": pnl, "exit_price": exit_info.get("exit_price", 0)})

        state["resolutions"].append({**pos, **exit_info})

        emoji = "🎯" if exit_info["exit_type"] == "take_profit" else "🛑" if exit_info["exit_type"] == "stop_loss" else "📉" if exit_info["exit_type"] == "trailing_stop" else "⏰"
        log(f"{emoji} EXIT {exit_info['exit_type']}: {pos_id} | {pos['side']} @ {pos['entry_price']*100:.1f}¢ → {exit_info.get('exit_price',0)*100:.1f}¢ | PnL: ${pnl:+.2f} | Bankroll: ${state['bankroll']:.2f}")
        del state["positions"][pos_id]

    save_state(state)
    return len(to_close) > 0


def check_market_resolution(pos):
    slug = pos.get("market_slug", "")
    if not slug:
        return None
    try:
        data = gamma_get("markets", {"slug": slug})
        for m in data:
            if m.get("closed", False) or m.get("resolved", False):
                outcomes = m.get("outcomes", [])
                prices = m.get("outcomePrices", [])
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                if isinstance(prices, str):
                    prices = json.loads(prices)
                if len(outcomes) >= 2 and len(prices) >= 2:
                    winning_idx = 0 if float(prices[0]) > float(prices[1]) else 1
                    winning_side = outcomes[winning_idx]
                    side = pos.get("side", "")
                    if side.lower() == winning_side.lower():
                        payout = pos["bet"] / pos["entry_price"]
                        return {"exit_type": "expiry_win", "exit_price": 1.0, "exit_value": round(payout, 2), "pnl": round(payout - pos["bet"], 2), "winning_side": winning_side}
                    else:
                        return {"exit_type": "expiry_loss", "exit_price": 0.0, "exit_value": 0.0, "pnl": round(-pos["bet"], 2), "winning_side": winning_side}
    except:
        pass
    return None


def resolve_positions():
    state = load_state()
    to_remove = []
    for trade_id, trade in list(state.get("positions", {}).items()):
        if trade.get("status") != "open":
            continue
        result = check_market_resolution(trade)
        if result:
            state["bankroll"] += result["exit_value"]
            trade["status"] = "resolved"
            trade["outcome"] = result["exit_type"]
            trade["pnl"] = result["pnl"]
            trade["exit_price"] = result.get("exit_price", 0)
            trade["resolved_at"] = datetime.now(timezone.utc).isoformat()
            state["resolutions"].append({**trade, **result})
            to_remove.append(trade_id)
            emoji = "✅" if result["exit_type"] == "expiry_win" else "❌"
            log(f"{emoji} {result['exit_type'].upper()}: {trade_id} | {trade['side']} @ {trade['entry_price']*100:.1f}¢ | PnL: ${result['pnl']:+.2f} | Bankroll: ${state['bankroll']:.2f}")
            if result["pnl"] < 0:
                state["daily_losses"] = state.get("daily_losses", 0) + 1
                state["daily_loss_amount"] = state.get("daily_loss_amount", 0) + abs(result["pnl"])
            for t in state.get("trades", []):
                if t.get("id") == trade_id:
                    t.update({"status": "resolved", "outcome": result["exit_type"], "pnl": result["pnl"]})
    for trade_id in to_remove:
        if trade_id in state.get("positions", {}):
            del state["positions"][trade_id]
    save_state(state)


def run_scan():
    """Single V19 scan with confluence scoring + Krajekis filters."""
    state = load_state()
    journal = TradeJournal()

    # ── DAILY LOSS RESET ──
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("daily_reset") != today:
        state["daily_losses"] = 0
        state["daily_loss_amount"] = 0.0
        state["daily_reset"] = today
        state["daily_trades"] = 0
        save_state(state)

    # ── DAILY LOSS LIMIT ──
    if state.get("daily_losses", 0) >= DAILY_LOSS_LIMIT:
        log(f"  🛑 Daily loss limit reached: {state['daily_losses']}/{DAILY_LOSS_LIMIT} losses")
        return
    if state.get("daily_loss_amount", 0) >= BANKROLL * DAILY_LOSS_PCT:
        log(f"  🛑 Daily loss amount limit: ${state['daily_loss_amount']:.2f} >= {BANKROLL * DAILY_LOSS_PCT:.2f}")
        return

    resolved = len([t for t in state.get("trades", []) if t.get("status") in ("resolved", "closed")])
    wins = len([t for t in state.get("trades", []) if t.get("outcome") in ("win", "expiry_win", "take_profit")])
    wr = wins / resolved * 100 if resolved > 0 else 0
    total_pnl = sum(r.get("pnl", 0) for r in state.get("resolutions", []))

    log(f"📊 Bankroll: ${state['bankroll']:.2f} | Trades: {resolved} (WR: {wr:.1f}%) | P&L: ${total_pnl:+.2f} | DailyLosses: {state.get('daily_losses',0)}/{DAILY_LOSS_LIMIT}")

    # 1. Evaluate exits
    evaluate_exits(state)
    resolve_positions()
    state = load_state()

    # 2. Fetch BTC candles
    candles = fetch_btc_candles('5m', 100)
    if not candles:
        log("❌ Could not fetch BTC candles")
        return

    prices = [c['close'] for c in candles]
    log(f"  BTC: ${prices[-1]:,.0f} | {len(candles)} candles")

    # 3. Compute indicators
    rsi_arr = compute_rsi(prices)
    current_rsi = rsi_arr[-1]
    direction, strength = detect_btc_direction(candles, len(candles) - 1)
    regime = get_regime(prices)

    # V19 indicators
    ema21 = compute_ema(prices, 21)
    ema50 = compute_ema(prices, 50)
    vwap = compute_vwap(candles)
    atr = compute_atr(candles, 14)
    macd_line, signal_line, macd_hist = compute_macd(prices)
    session = get_session(datetime.now(timezone.utc).hour)
    vol_regime, vol_max_price = classify_volatility(atr, prices[-1])

    # RSI zone label
    if current_rsi < 25:
        zone = "SEVERE_OVERSOLD"
    elif current_rsi < 30:
        zone = "OVERSOLD"
    elif current_rsi < 35:
        zone = "NEAR_OVERSOLD"
    elif current_rsi > 73:
        zone = "SEVERE_OVERBOUGHT"
    elif current_rsi > 70:
        zone = "OVERBOUGHT"
    elif current_rsi > 65:
        zone = "NEAR_OVERBOUGHT"
    else:
        zone = "NEUTRAL"

    log(f"  RSI: {current_rsi:.1f} ({zone}) | Dir: {direction} ({strength:.2f}%) | Regime: {regime}")
    log(f"  EMA21: ${ema21:,.0f} EMA50: ${ema50:,.0f} | VWAP: ${vwap:,.0f} | ATR: {atr:.1f} ({vol_regime}) | MACD_H: {macd_hist:.2f}")
    log(f"  Session: {session[0]} ({session[1]:.1f}) | Price vs VWAP: {'above' if prices[-1] > vwap else 'below'}")

    # 4. Generate V18.8 signal
    signal = generate_signal_v188(prices, candles, len(candles) - 1)

    # V19 CONFLUENCE OVERRIDE: if V18.8 says neutral/FLAT but confluence
    # strongly suggests a direction (≥7/10), override the signal direction
    v18_dir = signal['direction']
    if v18_dir == 'neutral':
        # When direction is FLAT, derive from regime + RSI for confluence scoring
        implied_dir = 'DOWN' if regime in ('trending_down', 'volatile') or current_rsi < 45 else 'UP'
        if current_rsi > 55 and regime in ('trending_up',):
            implied_dir = 'UP'
        
        # Try to derive direction from confluence analysis
        best_dir = None
        best_conf = 0
        for trial_dir in ['UP', 'DOWN']:
            conf, _ = compute_confluence(
                current_rsi, implied_dir, regime, ema21, ema50, vwap, prices[-1],
                macd_hist, session, (vol_regime, vol_max_price), trial_dir
            )
            if conf > best_conf and conf >= 7.0:
                best_conf = conf
                best_dir = trial_dir
        
        if best_dir and best_conf >= 7.0:
            # Confluence override: use confluence-derived direction
            override_strat = f"confluence_{best_dir.lower()}"
            signal = {
                'direction': best_dir.lower(),
                'strategy': override_strat,
                'confidence': min(0.80, best_conf / 10.0),
                'rsi': current_rsi,
            }
            log(f"  🔄 Confluence override: FLAT→{best_dir} (conf={best_conf:.1f}/10)")
        else:
            reason = signal.get('strategy', 'no_signal')
            log(f"  ⏸️ No signal — {reason} (conf={signal.get('confidence', 0):.2f}, best_confluence={best_conf:.1f}, implied={implied_dir})")
            state["last_scan"] = datetime.now(timezone.utc).isoformat()
            save_state(state)
            return

    # 5. V19 CONFLUENCE CHECK
    sig_dir = signal['direction'].upper()
    sig_conf = signal['confidence']
    sig_strategy = signal['strategy']

    confluence, details = compute_confluence(
        current_rsi, direction, regime, ema21, ema50, vwap, prices[-1],
        macd_hist, session, (vol_regime, vol_max_price), sig_dir
    )

    log(f"  ⭐ SIGNAL: BUY_{sig_dir} | {sig_strategy} | Conf: {sig_conf:.1%} | Confluence: {confluence:.1f}/10 | {' '.join(details[:5])}")

    # V19 gate: require minimum confluence
    if confluence < MIN_CONFLUENCE:
        log(f"  ❌ Confluence too low: {confluence:.1f} < {MIN_CONFLUENCE} — skipping")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    # 6. Tier config — V19.1 #3: Confluence-weighted sizing
    tier_cfg = TIER_CONFIG.get(sig_strategy, {"size": 0.03, "max_price": 0.08})
    tier_size = tier_cfg["size"]
    tier_max_price = min(tier_cfg["max_price"], vol_max_price)  # Adapt to volatility
    
    # V19.1 #3: Scale position size by confluence
    if confluence >= 8.0:
        tier_size = min(0.06, tier_size * 1.5)   # 8+/10: boost to 5-6%
    elif confluence >= 7.0:
        tier_size = min(0.05, tier_size * 1.25)  # 7-8/10: boost to 4-5%
    # 6-7/10: use base tier_size (3-4%)
    if confluence < 6.5:
        tier_size = min(tier_size, 0.03)          # Below 6.5: cap at 3%
    
    # V19.1 #4: Dynamic max price by vol regime
    if vol_regime == "high_vol":
        tier_max_price = min(tier_max_price, 0.08)   # High vol: only cheap entries
    elif vol_regime == "low_vol":
        tier_max_price = min(tier_max_price, 0.20)   # Low vol: allow slightly more expensive
    else:
        tier_max_price = min(tier_max_price, 0.15)   # Normal: 15¢ max

    tier_num = 1 if tier_size >= 0.10 else (2 if tier_size >= 0.05 else 3)

    # V19: Vol-adaptive sizing
    if vol_regime == "low_vol" and confluence >= 8:
        tier_size *= 1.3  # Boost size in low-vol high-confluence
    elif vol_regime == "high_vol":
        tier_size *= 0.7  # Reduce size in high vol

    # ── V19.1 REFINEMENTS ──

    # #7: Re-entry cooldown after stop-loss
    last_sl_time = state.get("last_stop_loss_time")
    if last_sl_time:
        try:
            last_sl_dt = datetime.fromisoformat(last_sl_time)
            cooldown_remaining = (timedelta(minutes=COOLDOWN_MINS) - (datetime.now(timezone.utc) - last_sl_dt)).total_seconds() / 60
            if cooldown_remaining > 0:
                log(f"  🧊 Cooldown: {cooldown_remaining:.1f}min after stop-loss — skipping")
                state["last_scan"] = datetime.now(timezone.utc).isoformat()
                save_state(state)
                return
        except:
            pass

    # #2: Correlation limit — max 2 same-direction positions
    open_positions_dict = {k: v for k, v in state.get("positions", {}).items() if v.get("status") == "open"}
    same_dir_count = sum(1 for p in open_positions_dict.values() if p.get("side", "").upper() == sig_dir)
    if same_dir_count >= MAX_SAME_DIRECTION:
        log(f"  ⚠️ Correlation limit: {same_dir_count}/{MAX_SAME_DIRECTION} {sig_dir} positions open — skipping")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    log(f"     Tier {tier_num} | Size: {tier_size:.1%} (vol-adjusted) | Max price: {tier_max_price*100:.0f}¢ | Vol: {vol_regime}")

    # 7. Find matching market
    markets = fetch_5m_15m_markets()
    if not markets:
        log("  ❌ No active 5m/15m markets found")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    now = datetime.now(timezone.utc)
    viable = []
    for m in markets:
        mins_left = m["minutes_left"]
        window = m.get("window_mins", 5)
        config = None
        for c in SERIES_CONFIG:
            if c["slug"] == m.get("series_slug", ""):
                config = c
                break
        if config:
            if not (config["min_remaining"] <= mins_left <= config["max_remaining"]):
                continue
        else:
            if not (1 <= mins_left <= 30):
                continue
        viable.append(m)

    if not viable:
        log(f"  ❌ No viable markets (found {len(markets)} total, none in time window)")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    # V19.1 #6: Time-in-window decay — peak at 7-9min (15m) or 2.5-3.5min (5m), decay at edges
    time_decay = 1.0
    for m in viable[:1]:
        mins_left = m.get("minutes_left", 10)
        window = m.get("window_mins", 15)
        if window >= 15:
            if 7 <= mins_left <= 9:
                time_decay = 1.0
            elif 5 <= mins_left <= 12:
                time_decay = 0.7 + 0.3 * (mins_left - 5) / 2.0 if mins_left < 7 else 1.0 - 0.3 * (mins_left - 9) / 3.0
            else:
                time_decay = 0.5
        else:
            if 2.5 <= mins_left <= 3.5:
                time_decay = 1.0
            elif 2 <= mins_left <= 4:
                time_decay = 0.7 + 0.3 * (mins_left - 2) / 0.5 if mins_left < 2.5 else 1.0 - 0.3 * (mins_left - 3.5) / 0.5
            else:
                time_decay = 0.5

    if time_decay < 1.0:
        confluence *= time_decay
        log(f"     ⏱️ Time decay: ×{time_decay:.2f} (mins_left={mins_left:.1f}) → adjusted confluence: {confluence:.1f}/10")

    # Re-check confluence after time decay
    if confluence < MIN_CONFLUENCE:
        log(f"  ❌ Confluence too low after time decay: {confluence:.1f} < {MIN_CONFLUENCE} — skipping")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    # 8. Find best entry
    best_market = None
    best_price = None
    best_side = None
    best_entry_type = None

    for m in viable:
        up_price = m["up_price"]
        down_price = m["down_price"]
        cheap = m["cheap_side"]
        cheap_p = m["cheap_price"]

        # Direct: signal aligns with cheap side AND price ≤ max
        if sig_dir == "UP" and cheap == "Up" and cheap_p <= tier_max_price * 1.5:
            best_market = m
            best_price = up_price
            best_side = "Up"
            best_entry_type = "direct"
            break
        elif sig_dir == "DOWN" and cheap == "Down" and cheap_p <= tier_max_price * 1.5:
            best_market = m
            best_price = down_price
            best_side = "Down"
            best_entry_type = "direct"
            break

    # Fair-price fallback
    if best_market is None:
        for m in viable:
            up_price = m["up_price"]
            down_price = m["down_price"]
            if min(up_price, down_price) >= 0.35 and max(up_price, down_price) <= 0.65:
                if sig_conf >= MIN_CONFIDENCE_FAIR_PRICE and confluence >= MIN_CONFLUENCE + 1:
                    if best_market is None or m["minutes_left"] < best_market.get("minutes_left", 999):
                        best_market = m
                        best_price = up_price if sig_dir == "UP" else down_price
                        best_side = sig_dir.capitalize()
                        best_entry_type = "fair_price"

    if best_market is None:
        log(f"  ❌ No market for BUY_{sig_dir} — no cheap side or fair-price available")
        for m in viable[:3]:
            log(f"     {m['label']}: {m['question'][:60]} | Up={m['up_price']*100:.1f}¢ Down={m['down_price']*100:.1f}¢ | {m['minutes_left']:.1f}min left")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    # 9. Determine trade side and price
    if best_side == "Up":
        token_id = best_market["up_token_id"]
    else:
        token_id = best_market["down_token_id"]

    entry_price = best_price
    question = best_market["question"]
    end_date = best_market["end_date"]
    minutes_left = best_market["minutes_left"]
    window_label = best_market["label"]

    # V19.1 #1: Dead zone filter — reject entries priced 8¢-60¢ (£8 to <60¢ is dead zone)
    if entry_price > DEAD_ZONE_LOW and entry_price < DEAD_ZONE_HIGH and best_entry_type != "fair_price":
        log(f"  ❌ Dead zone: {entry_price*100:.1f}¢ is in {DEAD_ZONE_LOW*100:.0f}-{DEAD_ZONE_HIGH*100:.0f}¢ dead zone — skipping")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return
    # Also reject fair-price entries in the dead zone
    if entry_price > DEAD_ZONE_LOW and entry_price < DEAD_ZONE_HIGH:
        log(f"  ❌ Dead zone: {entry_price*100:.1f}¢ is in {DEAD_ZONE_LOW*100:.0f}-{DEAD_ZONE_HIGH*100:.0f}¢ dead zone — skipping")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    log(f"  📈 Market: {question[:70]}")
    log(f"     Side: {best_side} @ {entry_price*100:.1f}¢ ({best_entry_type}) | {window_label} window | Expires in {minutes_left:.1f}min")

    # 10. Compute trade
    win_prob = compute_win_probability(sig_strategy, entry_price)
    # V19: Boost win prob by confluence
    confluence_boost = (confluence - 5) * 0.02  # +2% per confluence point above 5
    win_prob = min(0.95, win_prob + confluence_boost)

    edge = win_prob - entry_price
    odds = 1.0 - entry_price

    if edge < 0.03:
        log(f"  ❌ Edge too small: {edge:.3f}")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    # Position sizing
    cal_factor = journal.get_calibration_factor() if hasattr(journal, 'get_calibration_factor') else 0.5
    kelly_bet = kelly_size(edge, odds, state["bankroll"], cal_factor, sig_conf, state.get("updates", 0))
    max_bet = state["bankroll"] * tier_size
    max_cap = state["bankroll"] * MAX_POSITION_PCT
    bet = min(kelly_bet, max_bet, max_cap)
    bet = max(MIN_BET, bet)

    # Position limit
    open_positions = sum(1 for p in state.get("positions", {}).values() if p.get("status") == "open")
    if open_positions >= MAX_OPEN_POSITIONS:
        log(f"  ⚠️ Max positions: {open_positions}/{MAX_OPEN_POSITIONS}")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    # Dedup
    market_slug = best_market.get("slug", best_market.get("event_slug", ""))
    existing = [p for p in state.get("positions", {}).values()
                if p.get("status") == "open" and (p.get("market_slug") == market_slug or p.get("event_slug") == market_slug)]
    if existing:
        log(f"  ⚠️ Already have position in {market_slug}, skipping")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    # Kill switch
    if state["bankroll"] < 5.0:
        log(f"  🛑 Kill switch: bankroll ${state['bankroll']:.2f} < minimum")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    # 11. RECORD TRADE
    log(f"  📝 TRADE: BUY_{best_side} @ {entry_price*100:.1f}¢ ({best_entry_type}) | Bet: ${bet:.2f} ({tier_size:.1%} tier)")
    log(f"     Win prob: {win_prob:.1%} | Edge: {edge:.3f} | Confluence: {confluence:.1f}/10")
    log(f"     Strategy: {sig_strategy} (Tier {tier_num}) | Kelly: ${kelly_bet:.2f}")
    log(f"     Market: {question[:60]} ({window_label}) | Vol: {vol_regime} | Session: {session[0]}")

    trade_id = f"T19{len(state.get('trades', [])) + 1:04d}"
    trade = {
        "id": trade_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "action": f"BUY_{best_side}",
        "strategy": sig_strategy,
        "tier": tier_num,
        "market_type": f"{window_label}_updown",
        "entry_type": best_entry_type,
        "condition_id": best_market.get("condition_id", ""),
        "token_id": token_id,
        "side": best_side,
        "entry_price": entry_price,
        "peak_price": entry_price,
        "bet": round(bet, 2),
        "tier_pct": tier_size,
        "edge": round(edge, 4),
        "win_prob": round(win_prob, 4),
        "confidence": round(sig_conf, 3),
        "confluence": round(confluence, 1),
        "confluence_details": details[:5],
        "rsi": round(current_rsi, 1),
        "ema21": round(ema21, 2),
        "ema50": round(ema50, 2),
        "vwap": round(vwap, 2),
        "atr": round(atr, 2),
        "macd_hist": round(macd_hist, 4),
        "session": session[0],
        "session_weight": session[1],
        "vol_regime": vol_regime,
        "direction": direction,
        "regime": regime,
        "btc_price": prices[-1],
        "bankroll_at_entry": round(state["bankroll"], 2),
        "market_slug": market_slug,
        "event_slug": best_market.get("event_slug", ""),
        "market_question": question,
        "market_end_date": end_date,
        "series_slug": best_market.get("series_slug", ""),
        "window_label": window_label,
        "minutes_left": round(minutes_left, 1),
        "status": "open",
        "outcome": "pending",
    }

    state["bankroll"] -= bet
    state["trades"].append(trade)
    state["positions"][trade_id] = trade
    state["updates"] = state.get("updates", 0) + 1
    state["daily_trades"] = state.get("daily_trades", 0) + 1
    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    log(f"  💰 Bankroll: ${state['bankroll']:.2f} | Open: {open_positions + 1}/{MAX_OPEN_POSITIONS}")
    log(f"     Market expires: {end_date}")


def main_loop():
    log("=" * 70)
    log("V19.1 REFINED 5M/15M BTC UP/DOWN PAPER TRADER")
    log(f"Bankroll: ${BANKROLL} | Min Confidence: {MIN_CONFIDENCE}")
    log(f"Min Confluence: {MIN_CONFLUENCE}/10 | Daily Loss Limit: {DAILY_LOSS_LIMIT}")
    log(f"Dead Zone: {DEAD_ZONE_LOW*100:.0f}-{DEAD_ZONE_HIGH*100:.0f}¢ | Correlation: {MAX_SAME_DIRECTION} same-dir max")
    log(f"Cooldown: {COOLDOWN_MINS}min after stop-loss | Confluence sizing: 6→3%, 7→4%, 8+→5-6%")
    log(f"Exits: SL@{STOP_LOSS_PCT:.0%} | TP@{TAKE_PROFIT_PRICE*100:.0f}¢ | Trail@{TRAILING_STOP_PCT:.0%} after {TRAILING_ACTIVATE_MINS}min")
    log(f"Indicators: RSI + EMA21/50 + VWAP + ATR + MACD + Session + Confluence")
    log(f"Markets: 5m (late 2-4min) + 15m (mid 5-12min)")
    log(f"Scanning every {SCAN_SECONDS}s | Max positions: {MAX_OPEN_POSITIONS}")
    log("=" * 70)

    while True:
        try:
            run_scan()
        except Exception as e:
            log(f"❌ Error: {e}")
            traceback.print_exc(file=sys.stderr)

        time.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Run continuous loop")
    parser.add_argument("--once", action="store_true", help="Single scan only")
    parser.add_argument("--resolve", action="store_true", help="Only resolve open positions")
    parser.add_argument("--exits", action="store_true", help="Only evaluate exits")
    args = parser.parse_args()

    if args.resolve:
        resolve_positions()
    elif args.exits:
        state = load_state()
        evaluate_exits(state)
    elif args.once:
        run_scan()
    else:
        main_loop()