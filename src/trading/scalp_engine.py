#!/usr/bin/env python3
"""
Father Daddy Capital — Scalp Trading Engine
=============================================
Short-duration intraday trades targeting 0.5-2% moves.
RSI(7) divergence + volume spike entries. Tight stops.

Companion to swing engine. Deployed alongside it in paper_engine.py.
All trades feed the neural plasticity layer for continuous learning.

Assets: BTC, ETH, SOL (high liquidity crypto) + SPY, QQQ (equities)
Timeframes: 15min candles for signals, 5min for entry timing
"""

import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field
from pathlib import Path
import json
import sys

# ─── Configuration ───────────────────────────────────────────────────────────

SCALP_SYMBOLS = ["BTC-USD", "ETH-USD", "SOL-USD", "SPY", "QQQ"]

# Position sizing
MAX_SCALP_POSITION_PCT = 0.04    # 4% of capital per scalp position (tighter than swing)
MAX_SCALP_TOTAL_EXPOSURE = 0.20  # 20% max total scalp exposure

# Exit gates (tighter than swing — we're hunting small moves)
SCALP_TAKE_PROFIT_PCT = 0.015    # 1.5% target (was 10% in swing)
SCALP_AGGRESSIVE_TP = 0.025      # 2.5% if momentum is strong
SCALP_STOP_LOSS_PCT = 0.008      # 0.8% stop (was 5% in swing)
SCALP_TIME_STOP_MINUTES = 90     # Exit if no move after 90 min — capital is oxygen

# Signal thresholds
RSI_OVERSOLD = 28                # Lower than standard — we buy panic
RSI_OVERBOUGHT = 72              # Lower too — we sell euphoria early
VOLUME_SPIKE_MULTIPLIER = 1.5    # 1.5x average volume = spike (was 2.0 — too strict)
MIN_SIGNAL_SCORE = 0.30          # Lower threshold — more trades, volume compensates

# Data
SCALP_LOOKBACK_DAYS = 5          # Only need recent data for intraday patterns
CANDLE_PERIOD = "15m"            # Primary signal timeframe
ENTRY_PERIOD = "5m"              # Entry timing timeframe

# ─── Data Structures ─────────────────────────────────────────────────────────

@dataclass
class ScalpSignal:
    symbol: str
    direction: str           # "LONG" or "SHORT"
    score: float             # [-1, 1]
    rsi: float
    volume_ratio: float      # current volume / 20-period avg
    momentum_15m: float
    volatility: float
    confidence: float
    entry_price: float
    stop_loss: float
    take_profit: float
    timestamp: str

@dataclass 
class ScalpPosition:
    symbol: str
    direction: str
    shares: float
    entry_price: float
    entry_time: datetime
    stop_loss: float
    take_profit: float
    signal_score: float
    signal_vector: np.ndarray  # For neural layer
    asset_class: str


# ─── Signal Generation ───────────────────────────────────────────────────────

def compute_scalp_signals(hist_15m: pd.DataFrame, hist_5m: pd.DataFrame) -> dict:
    """
    Generate scalp-specific signals from 15m candles.
    Returns None if no valid signal, or a ScalpSignal dict.
    """
    prices_15m = hist_15m['Close']
    volumes_15m = hist_15m['Volume']
    
    if len(prices_15m) < 26:
        return None
    
    current_price = float(prices_15m.iloc[-1])
    
    # ── RSI(7) — faster, catches micro-moves ─────────────────────────────
    delta = prices_15m.diff()
    gain = delta.where(delta > 0, 0.0).rolling(7).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(7).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi_7 = float(100 - (100 / (1 + rs)).iloc[-1])
    
    # ── Volume spike detection ───────────────────────────────────────────
    avg_volume = volumes_15m.rolling(20).mean().iloc[-1]
    current_volume = float(volumes_15m.iloc[-1])
    volume_ratio = current_volume / max(1e-6, avg_volume)
    
    # ── Micro-momentum (3-period ROC) ────────────────────────────────────
    momentum_15m = float((prices_15m.iloc[-1] / prices_15m.iloc[-4] - 1)) if len(prices_15m) >= 4 else 0
    
    # ── Bollinger Band position ──────────────────────────────────────────
    sma_20 = prices_15m.rolling(20).mean().iloc[-1]
    std_20 = prices_15m.rolling(20).std().iloc[-1]
    bb_position = (current_price - sma_20) / max(std_20, 1e-9)  # >2 = extended, <-2 = compressed
    
    # ── Volatility (15m) ─────────────────────────────────────────────────
    returns_15m = prices_15m.pct_change().dropna()
    volatility = float(returns_15m.rolling(14).std().iloc[-1] * np.sqrt(96))  # Annualized from 15m
    
    # ── MACD (5/13/6) — faster parameters ────────────────────────────────
    ema5 = prices_15m.ewm(span=5).mean()
    ema13 = prices_15m.ewm(span=13).mean()
    macd = ema5 - ema13
    signal_line = macd.ewm(span=6).mean()
    macd_bullish = macd.iloc[-1] > signal_line.iloc[-1]
    macd_cross = (macd.iloc[-2] <= signal_line.iloc[-2] and macd.iloc[-1] > signal_line.iloc[-1])
    
    # ── Entry timing from 5m candles ─────────────────────────────────────
    ema9_5m = hist_5m['Close'].ewm(span=9).mean().iloc[-1] if len(hist_5m) >= 9 else current_price
    price_5m = float(hist_5m['Close'].iloc[-1])
    vwap_5m = float((hist_5m['Close'] * hist_5m['Volume']).sum() / max(1e-9, hist_5m['Volume'].sum()))
    
    # ── Composite signal construction ────────────────────────────────────
    
    direction = None
    score = 0.0
    confidence = 0.0
    stop_loss = 0.0
    take_profit = 0.0
    
    # LONG signal: oversold RSI + volume confirmation + reversal momentum
    if rsi_7 < RSI_OVERSOLD and volume_ratio > VOLUME_SPIKE_MULTIPLIER:
        # Panic buy — RSI extreme + heavy volume = capitulation
        rsi_component = (RSI_OVERSOLD - rsi_7) / RSI_OVERSOLD  # More oversold = stronger
        volume_component = min(1.0, volume_ratio / 4.0)
        bb_component = -min(0, bb_position) / 3.0  # Below lower band = good entry
        macd_component = 0.5 if macd_bullish else (-0.3 if not macd_bullish else 0)
        cross_component = 0.3 if macd_cross else 0.0
        
        score = (rsi_component * 0.30 + volume_component * 0.25 + 
                 bb_component * 0.20 + macd_component * 0.15 + cross_component * 0.10)
        direction = "LONG"
        stop_loss = current_price * (1 - SCALP_STOP_LOSS_PCT)
        take_profit = current_price * (1 + SCALP_TAKE_PROFIT_PCT)
        
    # SHORT signal: overbought RSI + volume confirmation + fading momentum  
    elif rsi_7 > RSI_OVERBOUGHT and volume_ratio > VOLUME_SPIKE_MULTIPLIER:
        rsi_component = (rsi_7 - RSI_OVERBOUGHT) / (100 - RSI_OVERBOUGHT)
        volume_component = min(1.0, volume_ratio / 4.0)
        bb_component = max(0, bb_position) / 3.0  # Above upper band = good short
        macd_component = -0.5 if macd_bullish else 0.3
        cross_component = -0.3 if macd_cross else 0.0
        
        score = (rsi_component * 0.30 + volume_component * 0.25 +
                 bb_component * 0.20 + macd_component * 0.15 + cross_component * 0.10)
        direction = "SHORT"
        stop_loss = current_price * (1 + SCALP_STOP_LOSS_PCT)
        take_profit = current_price * (1 - SCALP_TAKE_PROFIT_PCT)
    
    elif abs(bb_position) > 2.2 and volume_ratio > 1.5:
        # Bollinger breakout — momentum continuation play
        if bb_position > 0 and momentum_15m > 0.002:
            direction = "LONG"
            score = 0.35
            stop_loss = current_price * (1 - SCALP_STOP_LOSS_PCT * 0.8)
            take_profit = current_price * (1 + SCALP_AGGRESSIVE_TP)
        elif bb_position < 0 and momentum_15m < -0.002:
            direction = "SHORT"
            score = 0.35
            stop_loss = current_price * (1 + SCALP_STOP_LOSS_PCT * 0.8)
            take_profit = current_price * (1 - SCALP_AGGRESSIVE_TP)
    
    if direction is None or score < MIN_SIGNAL_SCORE:
        return None
    
    # ── Confidence estimation ────────────────────────────────────────────
    base_confidence = 0.45 + abs(score) * 0.35
    vol_penalty = max(0, 0.15 - volatility * 0.2)
    time_quality = 0.05  # Bonus during active market hours — we skip this for now
    confidence = min(0.92, base_confidence + vol_penalty + time_quality)
    
    return {
        "symbol": None,  # Set by caller
        "direction": direction,
        "score": round(float(score), 3),
        "rsi": round(rsi_7, 1),
        "volume_ratio": round(float(volume_ratio), 2),
        "momentum_15m": round(momentum_15m, 4),
        "volatility": round(volatility, 4),
        "confidence": round(confidence, 3),
        "entry_price": round(current_price, 2),
        "stop_loss": round(stop_loss, 2),
        "take_profit": round(take_profit, 2),
        "timestamp": datetime.now().isoformat(),
        # Signal vector for neural plasticity (same shape as swing: 8 dims)
        "_signal_vector": np.array([
            (RSI_OVERSOLD - rsi_7) / 30 if direction == "LONG" else (rsi_7 - RSI_OVERBOUGHT) / 30,
            0.5 if macd_bullish else -0.5,
            min(1.0, max(-1.0, bb_position / 3.0)),
            min(1.0, max(-1.0, momentum_15m * 50)),
            -min(1.0, max(-1.0, bb_position / 3.0)),  # mean reversion proxy
            volatility,
            1.0 if "-USD" in "BTC-USD" else -1.0,  # crypto flag (placeholder, set by caller)
            confidence,
        ], dtype=float),
    }


def scan_scalps(state: dict) -> list[dict]:
    """
    Scan all scalp symbols and return ranked signals.
    Filters out symbols already in swing positions to avoid overlap.
    """
    results = []
    end = datetime.now()
    start = end - timedelta(days=SCALP_LOOKBACK_DAYS)
    
    # Skip symbols already in swing positions
    swing_symbols = set(state.get("swing_positions", {}).keys())
    available = [s for s in SCALP_SYMBOLS if s not in swing_symbols]
    
    for symbol in available:
        try:
            ticker = yf.Ticker(symbol)
            hist_15m = ticker.history(start=start, end=end, interval=CANDLE_PERIOD)
            hist_5m = ticker.history(start=start, end=end, interval=ENTRY_PERIOD)
            
            if len(hist_15m) < 26 or len(hist_5m) < 9:
                continue
            
            signal = compute_scalp_signals(hist_15m, hist_5m)
            if signal is None:
                continue
            
            signal["symbol"] = symbol
            signal["_signal_vector"][6] = 1.0 if "-USD" in symbol else -1.0
            signal["asset_class"] = "crypto" if "-USD" in symbol else "equity"
            
            results.append(signal)
            
        except Exception as e:
            print(f"  ⚠ scalp scan {symbol}: {e}", file=sys.stderr)
    
    # Sort by score
    results.sort(key=lambda x: abs(x["score"]), reverse=True)
    return results


# ─── Position Sizing (Scalp-Specific) ────────────────────────────────────────

def calculate_scalp_size(
    signal_score: float,
    confidence: float,
    price: float,
    available_capital: float,
) -> float:
    """Tighter sizing for scalp positions — quick in, quick out."""
    base = available_capital * MAX_SCALP_POSITION_PCT
    scaled = base * abs(signal_score) * confidence
    
    # Even tighter for equities (less intraday vol)
    shares = scaled / price
    if price > 1000:
        shares = round(shares, 6)
    else:
        shares = max(1, int(shares))
    
    return shares


# ─── Exit Management ─────────────────────────────────────────────────────────

def check_scalp_exits(the_positions: dict, current_prices: dict, now: datetime) -> list[dict]:
    """
    Check all scalp positions for exits: stop-loss, take-profit, time-stop.
    Returns list of exit orders. Modifies positions dict in place.
    """
    exits = []
    
    for symbol in list(the_positions.keys()):
        pos = the_positions[symbol]
        if symbol not in current_prices:
            continue
        
        current_price = current_prices[symbol]
        entry_price = pos["entry_price"]
        elapsed = (now - datetime.fromisoformat(pos["entry_time"])).total_seconds() / 60
        
        pnl_pct = (current_price - entry_price) / entry_price
        if pos["direction"] == "SHORT":
            pnl_pct = -pnl_pct
        
        reason = None
        
        # Stop loss
        if pos["direction"] == "LONG" and current_price <= pos["stop_loss"]:
            reason = f"SCALP_STOP ({pnl_pct*100:.1f}%)"
        elif pos["direction"] == "SHORT" and current_price >= pos["stop_loss"]:
            pnl_pct_actual = (pos["entry_price"] - current_price) / pos["entry_price"]
            reason = f"SCALP_STOP ({pnl_pct_actual*100:.1f}%)"
        
        # Take profit
        elif pos["direction"] == "LONG" and current_price >= pos["take_profit"]:
            reason = f"SCALP_TP ({pnl_pct*100:.1f}%)"
        elif pos["direction"] == "SHORT" and current_price <= pos["take_profit"]:
            pnl_pct_actual = (pos["entry_price"] - current_price) / pos["entry_price"]
            reason = f"SCALP_TP ({pnl_pct_actual*100:.1f}%)"
        
        # Time stop — capital is oxygen
        elif elapsed > SCALP_TIME_STOP_MINUTES:
            reason = f"TIME_STOP ({elapsed:.0f}min)"
        
        if reason:
            pnl = (current_price - entry_price) * pos["shares"]
            if pos["direction"] == "SHORT":
                pnl = (entry_price - current_price) * pos["shares"]
            
            exits.append({
                "action": "SELL" if pos["direction"] == "LONG" else "COVER",
                "symbol": symbol,
                "shares": pos["shares"],
                "price": current_price,
                "reason": reason,
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct * 100, 2),
                "hold_minutes": round(elapsed, 1),
                "entry_price": entry_price,
                "direction": pos["direction"],
                "signal_score": pos.get("signal_score", 0),
                "timestamp": now.isoformat(),
            })
            del the_positions[symbol]
    
    return exits


# ─── Reporting ───────────────────────────────────────────────────────────────

def scalp_summary(positions: dict, scan_count: int, recent_exits: list[dict]) -> str:
    """Generate scalp-specific section for the main report."""
    total_positions = len(positions)
    if total_positions == 0 and not recent_exits:
        return ""
    
    lines = ["\n⚡ SCALP TRACK:"]
    
    # Show recent exits first — they're the action
    if recent_exits:
        total_pnl = sum(e["pnl"] for e in recent_exits)
        wins = sum(1 for e in recent_exits if e["pnl"] > 0)
        lines.append(f"  Recent exits: {len(recent_exits)} ({wins} wins) | P&L: ${total_pnl:+,.2f}")
        for e in recent_exits[-5:]:  # Last 5 exits
            emoji = "🟢" if e["pnl"] > 0 else "🔴"
            lines.append(
                f"    {emoji} {e['action']} {e['symbol']} {e['direction']} "
                f"${e['pnl']:+,.2f} ({e['pnl_pct']:+.1f}%) — {e['reason']} "
                f"[{e['hold_minutes']:.0f}m hold]"
            )
    
    # Open positions
    if positions:
        lines.append(f"  Open: {total_positions}")
        for sym, pos in positions.items():
            lines.append(
                f"    {pos['direction']} {sym} ×{pos['shares']} @ ${pos['entry_price']:.2f} "
                f"| TP: ${pos['take_profit']:.2f} | SL: ${pos['stop_loss']:.2f}"
            )
    
    if not positions and not recent_exits:
        lines.append("  No signals this scan — waiting for RSI extremes + volume.")
    
    lines.append(f"  Scans: {scan_count}")
    return "\n".join(lines)


# ─── Integration — Feed to Neural Layer ──────────────────────────────────────

def scalp_to_neural_input(signal: dict) -> np.ndarray:
    """Extract signal vector for neural plasticity learning."""
    return signal.get("_signal_vector", np.zeros(8))


# ─── Main Entry Point for Paper Engine ───────────────────────────────────────

def run_scalp_cycle(state: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """
    One complete scalp cycle.
    
    Returns:
        new_entries: list of entry orders placed
        exits: list of exit orders executed this cycle
        signals: raw signals generated (for neural learning)
    """
    now = datetime.now()
    scalp_positions = state.setdefault("scalp_positions", {})
    scalp_exits_all = state.setdefault("scalp_exits", [])
    scalp_scans = state.setdefault("scalp_scans", 0)
    
    # ── Phase 1: Check exits on existing positions ───────────────────────
    signals_raw = scan_scalps({**state, "swing_positions": state.get("swing_positions", {})})
    current_prices = {s["symbol"]: s["entry_price"] for s in signals_raw}
    
    exits = check_scalp_exits(scalp_positions, current_prices, now)
    
    # Process exits — return capital
    for exit_order in exits:
        state["capital"] += exit_order["price"] * exit_order["shares"]
        if exit_order["direction"] == "SHORT":
            # Short profit = sold high, bought back lower
            pnl = (exit_order["entry_price"] - exit_order["price"]) * exit_order["shares"]
        else:
            pnl = exit_order["pnl"]
        state["total_pnl"] += pnl
        today = now.strftime("%Y-%m-%d")
        state["daily_pnl"][today] = state["daily_pnl"].get(today, 0) + pnl
        scalp_exits_all.append(exit_order)
    
    # ── Phase 2: Generate new entries ────────────────────────────────────
    entries = []
    
    # Calculate available
    invested_scalp = sum(
        scalp_positions[s]["shares"] * current_prices.get(s, scalp_positions[s]["entry_price"])
        for s in scalp_positions
    )
    total_value = state["capital"] + invested_scalp
    available = min(state["capital"], total_value * MAX_SCALP_TOTAL_EXPOSURE)
    
    max_positions = 6
    current_count = len(scalp_positions)
    
    for signal in signals_raw:
        if current_count >= max_positions:
            break
        if signal["symbol"] in scalp_positions:
            continue
        
        shares = calculate_scalp_size(
            signal_score=signal["score"],
            confidence=signal["confidence"],
            price=signal["entry_price"],
            available_capital=available,
        )
        if shares <= 0:
            continue
        
        cost = shares * signal["entry_price"]
        if cost > available * 0.5:
            continue
        
        if signal["direction"] == "LONG":
            scalp_positions[signal["symbol"]] = {
                "shares": shares,
                "entry_price": signal["entry_price"],
                "entry_time": now.isoformat(),
                "stop_loss": signal["stop_loss"],
                "take_profit": signal["take_profit"],
                "direction": signal["direction"],
                "signal_score": signal["score"],
                "asset_class": signal["asset_class"],
            }
            state["capital"] -= cost
            available -= cost
            current_count += 1
        
        entries.append({
            "action": "BUY" if signal["direction"] == "LONG" else "SHORT",
            "symbol": signal["symbol"],
            "shares": shares,
            "price": signal["entry_price"],
            "reason": f"SCALP ({signal['direction']}, score={signal['score']:.2f}, RSI={signal['rsi']})",
            "direction": signal["direction"],
            "stop_loss": signal["stop_loss"],
            "take_profit": signal["take_profit"],
        })
    
    scalp_scans += 1
    state["scalp_scans"] = scalp_scans
    
    return entries, exits, signals_raw


# ─── Self-Test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("═" * 56)
    print("  Father Daddy Capital — Scalp Engine Test")
    print("═" * 56)
    
    test_state = {
        "capital": 100000.0,
        "total_pnl": 0.0,
        "daily_pnl": {},
        "swing_positions": {},
        "scalp_positions": {},
        "scalp_exits": [],
        "scalp_scans": 0,
    }
    
    print(f"\n  Scanning {len(SCALP_SYMBOLS)} scalp symbols...")
    entries, exits, signals = run_scalp_cycle(test_state)
    
    print(f"\n  New entries: {len(entries)}")
    for e in entries:
        print(f"    {e['action']} {e['symbol']} ×{e['shares']} @ ${e['price']:.2f}")
        print(f"      TP: ${e['take_profit']:.2f} | SL: ${e['stop_loss']:.2f}")
    
    print(f"\n  Exits: {len(exits)}")
    for x in exits:
        print(f"    {x['action']} {x['symbol']} — {x['reason']}")
    
    print(f"\n  Open scalp positions: {len(test_state['scalp_positions'])}")
    print(f"  Capital: ${test_state['capital']:,.2f}")
    print("═" * 56)
