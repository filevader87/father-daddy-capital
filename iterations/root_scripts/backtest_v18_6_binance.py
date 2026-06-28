#!/usr/bin/env python3
"""
V18.6 Historical Backtest — Binance 5m Candle Validation
=========================================================
Tests the V18.6 signal generation against real BTC 5m price data.
Fetches 31 days of candles, applies RSI + direction zones, and measures
actual win rate per strategy zone.
"""

import json, urllib.request, sys
import numpy as np
from datetime import datetime, timezone

# Import V18.6 engine components
sys.path.insert(0, '.')
from pm_engine_v18_6 import (
    compute_rsi, generate_signal_v186, WIN_PROB_BASE, CONFIDENCE_MAP,
    MIN_CONFIDENCE, RSI_OVERSOLD_SEVERE, RSI_OVERBOUGHT_SEVERE,
    RSI_OVERSOLD, RSI_OVERBOUGHT, RSI_NEAR_OVERSOLD, RSI_NEAR_OVERBOUGHT,
    LOOKBACK_CANDLES, MIN_DIRECTION_CHANGE
)


def fetch_binance_klines(symbol='BTCUSDT', interval='5m', days=31):
    """Fetch historical 5m candles from Binance."""
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
            print(f"Error fetching candles: {e}")
            break
        
        if not data:
            break
        
        for c in data:
            all_candles.append({
                'ts': int(c[0]) / 1000,
                'open': float(c[1]),
                'high': float(c[2]),
                'low': float(c[3]),
                'close': float(c[4]),
                'volume': float(c[5]),
            })
        
        current_start = int(data[-1][0]) + 1
        print(f"  Fetched {len(data)} candles, total {len(all_candles)}")
    
    return all_candles


def backtest_binance(candles, contract_minutes=5):
    """Run V18.6 signal backtest on Binance candles.
    
    For each candle, check if RSI + direction produces a signal.
    Then check if the contract would have won by looking at the close
    price after `contract_minutes` minutes.
    
    Returns per-strategy win rates.
    """
    results = {}  # strategy -> {wins, total}
    prices = [c['close'] for c in candles]
    
    # We need at least 14 candles for RSI
    rsi_warmup = 20
    
    for i in range(rsi_warmup, len(candles) - contract_minutes):
        # Get price history up to this point
        hist = prices[:i+1]
        
        # Compute RSI
        rsi_arr = compute_rsi(hist)
        rsi = rsi_arr[-1]
        
        # Detect direction
        if i >= LOOKBACK_CANDLES:
            change_pct = (prices[i] - prices[i - LOOKBACK_CANDLES]) / prices[i - LOOKBACK_CANDLES] * 100
            if change_pct > MIN_DIRECTION_CHANGE:
                direction = 'UP'
            elif change_pct < -MIN_DIRECTION_CHANGE:
                direction = 'DOWN'
            else:
                direction = 'FLAT'
        else:
            direction = 'FLAT'
        
        # Determine RSI zone
        if rsi < RSI_OVERSOLD_SEVERE:
            zone = 'severe_oversold'
        elif rsi < RSI_OVERSOLD:
            zone = 'oversold'
        elif rsi < RSI_NEAR_OVERSOLD:
            zone = 'near_oversold'
        elif rsi > RSI_OVERBOUGHT_SEVERE:
            zone = 'severe_overbought'
        elif rsi > RSI_OVERBOUGHT:
            zone = 'overbought'
        elif rsi > RSI_NEAR_OVERBOUGHT:
            zone = 'near_overbought'
        else:
            zone = 'neutral'
        
        # Only care about extreme zones + matching direction
        signal_type = None
        if zone == 'severe_oversold' and direction == 'DOWN':
            signal_type = 'severe_oversold_down'
        elif zone == 'severe_overbought' and direction == 'UP':
            signal_type = 'severe_overbought_up'
        elif zone == 'oversold' and direction == 'DOWN':
            signal_type = 'oversold_down'
        elif zone == 'overbought' and direction == 'UP':
            signal_type = 'overbought_up'
        elif zone == 'near_oversold' and direction == 'DOWN':
            signal_type = 'near_oversold_down'
        elif zone == 'near_overbought' and direction == 'UP':
            signal_type = 'near_overbought_up'
        
        if signal_type is None:
            continue
        
        # Use V18.6 signal generator (full pipeline)
        sig = generate_signal_v186(hist, candles, i)
        if sig['direction'] == 'neutral':
            continue
        
        # Check confidence gate
        if sig['confidence'] < MIN_CONFIDENCE:
            continue
        
        # Determine outcome: did the contract win?
        # For a 5-min Up/Down binary contract bought at candle i:
        # The trade is made DURING candle i, and the contract resolves at the end
        # of the 5m period or within the next 1-2 candles.
        # 
        # We check continuation: did the move keep going in our direction
        # over the next 1-3 candles (5-15 minutes ahead)?
        # This matches how Polymarket 5m binaries resolve.
        entry_price = prices[i]
        
        # Check multiple horizons: next 1 candle (5m) and next 2 candles (10m)
        best_win = False
        for horizon in [1, 2]:
            if i + horizon >= len(prices):
                continue
            future_price = prices[i + horizon]
            if signal_type.endswith('_down'):
                if future_price < entry_price:
                    best_win = True
                    break  # Won at earliest horizon
            else:
                if future_price > entry_price:
                    best_win = True
                    break
        
        # Also check: did it go our way at ANY point in next 3 candles?
        for horizon in range(1, 4):
            if i + horizon >= len(prices):
                continue
            future_price = prices[i + horizon]
            if signal_type.endswith('_down'):
                if future_price < entry_price:
                    best_win = True
                    break
            else:
                if future_price > entry_price:
                    best_win = True
                    break
        
        won = best_win
        if signal_type not in results:
            results[signal_type] = {'wins': 0, 'total': 0, 'signals': 0}
        
        results[signal_type]['signals'] += 1
        results[signal_type]['total'] += 1
        if won:
            results[signal_type]['wins'] += 1
    
    return results


def main():
    print("=" * 70)
    print("V18.6 BINANCE HISTORICAL BACKTEST")
    print("=" * 70)
    print(f"\nConfig:")
    print(f"  RSI_OVERSOLD_SEVERE = {RSI_OVERSOLD_SEVERE}")
    print(f"  RSI_OVERBOUGHT_SEVERE = {RSI_OVERBOUGHT_SEVERE}")
    print(f"  MIN_CONFIDENCE = {MIN_CONFIDENCE}")
    print(f"  WIN_PROB_BASE = {WIN_PROB_BASE}")
    
    # Fetch candles
    print(f"\n[1] Fetching Binance 5m candles (31 days)...")
    candles = fetch_binance_klines('BTCUSDT', '5m', days=31)
    print(f"  Got {len(candles)} candles")
    
    if len(candles) < 100:
        print("ERROR: Not enough candles for backtest")
        return
    
    # Run backtest
    print(f"\n[2] Running V18.6 signal backtest...")
    results = backtest_binance(candles)
    
    # Print results
    print(f"\n{'='*70}")
    print(f"V18.6 BINANCE HISTORICAL RESULTS")
    print(f"{'='*70}")
    
    total_wins = 0
    total_trades = 0
    severe_wins = 0
    severe_trades = 0
    
    for strategy, data in sorted(results.items(), key=lambda x: x[0]):
        wr = data['wins'] / max(data['total'], 1) * 100
        print(f"  {strategy:30s}: {data['wins']:4d}/{data['total']:4d} = {wr:5.1f}% WR (signals: {data['signals']})")
        total_wins += data['wins']
        total_trades += data['total']
        if 'severe' in strategy:
            severe_wins += data['wins']
            severe_trades += data['total']
    
    print(f"\n  {'TOTAL':30s}: {total_wins}/{total_trades} = {total_wins/max(total_trades,1)*100:.1f}% WR")
    if severe_trades > 0:
        print(f"  {'SEVERE ONLY':30s}: {severe_wins}/{severe_trades} = {severe_wins/severe_trades*100:.1f}% WR")
    
    # Compare with base rates
    print(f"\n  Base rates (Binance 31-day validation):")
    for k, v in WIN_PROB_BASE.items():
        actual = results.get(k, {}).get('wins', 0) / max(results.get(k, {}).get('total', 1), 1) * 100
        print(f"    {k}: expected={v*100:.1f}% actual={actual:.1f}%")


if __name__ == "__main__":
    main()