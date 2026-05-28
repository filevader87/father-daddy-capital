#!/usr/bin/env python3
"""
V18.6 Fast Binance Backtest — vectorized, no per-candle signal generation.
Directly checks RSI+direction conditions and measures continuation WR.
"""

import json, urllib.request, sys
import numpy as np
from datetime import datetime, timezone

sys.path.insert(0, '.')
from pm_engine_v18_6 import (
    RSI_OVERSOLD_SEVERE, RSI_OVERBOUGHT_SEVERE,
    RSI_OVERSOLD, RSI_OVERBOUGHT, RSI_NEAR_OVERSOLD, RSI_NEAR_OVERBOUGHT,
    LOOKBACK_CANDLES, MIN_DIRECTION_CHANGE, WIN_PROB_BASE
)


def fetch_binance_klines(symbol='BTCUSDT', interval='5m', days=31):
    limit = 1000
    end_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ts = end_ts - (days * 24 * 60 * 60 * 1000)
    all_candles = []
    current_start = start_ts
    while current_start < end_ts:
        url = f'https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&startTime={current_start}&limit={limit}'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'FDC-V186/1.0'})
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
        except Exception as e:
            print(f"Error: {e}")
            break
        if not data:
            break
        for c in data:
            all_candles.append(float(c[4]))  # close price only
        current_start = int(data[-1][0]) + 1
        print(f"  Fetched {len(data)} candles, total {len(all_candles)}")
    return np.array(all_candles)


def compute_rsi_vectorized(prices, period=14):
    """Vectorized RSI."""
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    
    avg_gains = np.zeros_like(gains)
    avg_losses = np.zeros_like(losses)
    
    avg_gains[period] = np.mean(gains[1:period+1])
    avg_losses[period] = np.mean(losses[1:period+1])
    
    for i in range(period+1, len(deltas)):
        avg_gains[i] = (avg_gains[i-1] * (period-1) + gains[i]) / period
        avg_losses[i] = (avg_losses[i-1] * (period-1) + losses[i]) / period
    
    rsi = np.full(len(prices), 50.0)
    for i in range(period+1, len(prices)):
        idx = min(i, len(deltas)-1)
        if avg_losses[idx] == 0:
            rsi[i] = 100.0
        else:
            rs = avg_gains[idx] / avg_losses[idx]
            rsi[i] = 100.0 - (100.0 / (1.0 + rs))
    
    return rsi


def main():
    print("=" * 70)
    print("V18.6 FAST BINANCE BACKTEST")
    print("=" * 70)
    
    print("\n[1] Fetching candles...")
    prices = fetch_binance_klines()
    print(f"  Got {len(prices)} candles")
    
    if len(prices) < 100:
        print("ERROR: Not enough data")
        return
    
    print("\n[2] Computing RSI...")
    rsi = compute_rsi_vectorized(prices)
    
    print("\n[3] Testing signal zones...")
    
    # Direction: pct change over LOOKBACK candles
    lookback = LOOKBACK_CANDLES
    changes = np.zeros(len(prices))
    for i in range(lookback, len(prices)):
        changes[i] = (prices[i] - prices[i-lookback]) / prices[i-lookback] * 100
    
    # Define zones and check WR at different horizons
    zones = {
        'severe_oversold_down': (rsi < RSI_OVERSOLD_SEVERE) & (changes < -MIN_DIRECTION_CHANGE),
        'oversold_down': (rsi >= RSI_OVERSOLD_SEVERE) & (rsi < RSI_OVERSOLD) & (changes < -MIN_DIRECTION_CHANGE),
        'near_oversold_down': (rsi >= RSI_OVERSOLD) & (rsi < RSI_NEAR_OVERSOLD) & (changes < -MIN_DIRECTION_CHANGE),
        'severe_overbought_up': (rsi > RSI_OVERBOUGHT_SEVERE) & (changes > MIN_DIRECTION_CHANGE),
        'overbought_up': (rsi > RSI_OVERBOUGHT) & (rsi <= RSI_OVERBOUGHT_SEVERE) & (changes > MIN_DIRECTION_CHANGE),
        'near_overbought_up': (rsi > RSI_NEAR_OVERBOUGHT) & (rsi <= RSI_OVERBOUGHT) & (changes > MIN_DIRECTION_CHANGE),
    }
    
    warmup = 20  # RSI warmup
    
    for horizon_name, horizon in [("1 candle (5m)", 1), ("2 candles (10m)", 2), ("3 candles (15m)", 3)]:
        print(f"\n  Horizon: {horizon_name}")
        for zone_name, mask in zones.items():
            # Count signals after warmup and with enough future data
            valid = mask.copy()
            valid[:warmup] = False
            valid[len(prices)-horizon:] = False
            
            signal_indices = np.where(valid)[0]
            if len(signal_indices) == 0:
                print(f"    {zone_name:30s}: 0 signals")
                continue
            
            # Check outcome
            wins = 0
            for idx in signal_indices:
                future_price = prices[idx + horizon]
                entry_price = prices[idx]
                
                # Also check intermediate horizons (touch target)
                touched = False
                for h in range(1, horizon + 1):
                    fp = prices[idx + h]
                    if zone_name.endswith('_down'):
                        if fp < entry_price:
                            touched = True
                            break
                    else:
                        if fp > entry_price:
                            touched = True
                            break
                
                # Primary: does price go our way at horizon?
                if zone_name.endswith('_down'):
                    won = future_price < entry_price
                else:
                    won = future_price > entry_price
                
                # Use touch-win OR horizon-win
                if touched or won:
                    wins += 1
            
            wr = wins / len(signal_indices) * 100
            expected = WIN_PROB_BASE.get(zone_name, 0.65) * 100
            print(f"    {zone_name:30s}: {wins}/{len(signal_indices)} = {wr:.1f}% WR (expected: {expected:.1f}%)")
    
    # SEVERE ZONES ONLY — the V18.6 signals
    print(f"\n{'='*70}")
    print(f"SEVERE ZONES ONLY (V18.6 actual signals)")
    print(f"{'='*70}")
    
    severe_signals = 0
    severe_wins = 0
    oversold_signals = 0
    oversold_wins = 0
    overbought_signals = 0
    overbought_wins = 0
    
    # Best horizon: 1-3 candles (5-15 min), same as Polymarket 5m binary window
    for i in range(warmup, len(prices) - 3):
        won = False
        
        # Severe oversold + DOWN
        if rsi[i] < RSI_OVERSOLD_SEVERE and changes[i] < -MIN_DIRECTION_CHANGE:
            oversold_signals += 1
            # Check: does BTC continue dropping over next 1-3 candles?
            for h in [1, 2, 3]:
                if i + h < len(prices):
                    if prices[i + h] < prices[i]:
                        won = True
                        break
            if won:
                oversold_wins += 1
        
        won = False
        # Severe overbought + UP
        if rsi[i] > RSI_OVERBOUGHT_SEVERE and changes[i] > MIN_DIRECTION_CHANGE:
            overbought_signals += 1
            for h in [1, 2, 3]:
                if i + h < len(prices):
                    if prices[i + h] > prices[i]:
                        won = True
                        break
            if won:
                overbought_wins += 1
    
    severe_signals = oversold_signals + overbought_signals
    severe_wins = oversold_wins + overbought_wins
    
    print(f"\n  Oversold+DOWN (RSI<{RSI_OVERSOLD_SEVERE}):  {oversold_wins}/{oversold_signals} = {oversold_wins/max(oversold_signals,1)*100:.1f}% WR")
    print(f"  Overbought+UP (RSI>{RSI_OVERBOUGHT_SEVERE}):  {overbought_wins}/{overbought_signals} = {overbought_wins/max(overbought_signals,1)*100:.1f}% WR")
    print(f"  COMBINED: {severe_wins}/{severe_signals} = {severe_wins/max(severe_signals,1)*100:.1f}% WR")
    
    # Also check pure continuation (strict: next candle closes our way)
    strict_od_wins = 0
    strict_ou_wins = 0
    for i in range(warmup, len(prices) - 1):
        if rsi[i] < RSI_OVERSOLD_SEVERE and changes[i] < -MIN_DIRECTION_CHANGE:
            if prices[i + 1] < prices[i]:
                strict_od_wins += 1
        if rsi[i] > RSI_OVERBOUGHT_SEVERE and changes[i] > MIN_DIRECTION_CHANGE:
            if prices[i + 1] > prices[i]:
                strict_ou_wins += 1
    
    print(f"\n  STRICT (single candle):")
    print(f"  Oversold+DOWN: {strict_od_wins}/{oversold_signals} = {strict_od_wins/max(oversold_signals,1)*100:.1f}% WR")
    print(f"  Overbought+UP: {strict_ou_wins}/{overbought_signals} = {strict_ou_wins/max(overbought_signals,1)*100:.1f}% WR")


if __name__ == "__main__":
    main()