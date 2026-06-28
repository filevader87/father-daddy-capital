#!/usr/bin/env python3
"""
V18.5 — Binance Direction Backtest

Measures: "If we detect BTC direction from 5m candles,
how often does that direction predict the next 5m candle?"

This validates the core strategy: buy cheap UP token when BTC
trending UP, buy cheap DOWN token when BTC trending DOWN.

Key insight: Polymarket 5-min binaries are binary options.
If BTC direction prediction WR is X%, then our cheap token
strategy WR ≥ X% (because we only buy at ≤20¢, so even an
X% WR gives 80%+ ROI).

Methodology:
  1. For each 5m candle, compute BTC price direction from
     the last N candles (5m, 15m, 1h lookback)
  2. Check if the next 5m candle confirms the direction
  3. Measure WR for different direction thresholds
  4. Measure WR for different cheap token price levels
"""

import json
import sys
import numpy as np
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict


def load_candles(path='btc_5m_candles.json'):
    """Load Binance 5m candles from JSON file."""
    with open(path) as f:
        data = json.load(f)
    
    candles = []
    for c in sorted(data, key=lambda x: x[0]):
        candles.append({
            'ts': int(c[0]) / 1000,
            'open': float(c[1]),
            'high': float(c[2]),
            'low': float(c[3]),
            'close': float(c[4]),
            'volume': float(c[5]),
        })
    return candles


def compute_direction(candles, idx, lookback=3, min_change_pct=0.01):
    """Compute BTC direction from candle history.
    
    Args:
        candles: list of candle dicts
        idx: current candle index
        lookback: number of previous candles to consider
        min_change_pct: minimum % change to declare a direction
    
    Returns:
        'UP', 'DOWN', or 'FLAT'
    """
    if idx < lookback:
        return 'FLAT', 0.0
    
    current_price = candles[idx]['close']
    prev_price = candles[idx - lookback]['close']
    change_pct = (current_price - prev_price) / prev_price * 100
    
    if change_pct > min_change_pct:
        return 'UP', abs(change_pct)
    elif change_pct < -min_change_pct:
        return 'DOWN', abs(change_pct)
    else:
        return 'FLAT', abs(change_pct)


def simulate_polymarket_outcome(btc_direction, entry_price, volatility_bonus=0.0):
    """Simulate Polymarket binary outcome based on BTC direction.
    
    When BTC goes UP in a 5m window:
      - UP token settles at $1 (WIN)
      - DOWN token settles at $0 (LOSE)
    
    When BTC goes DOWN:
      - DOWN token settles at $1 (WIN)  
      - UP token settles at $0 (LOSE)
    
    When BTC is FLAT (within ±0.01%):
      - Outcome is essentially random
      - UP token has ~50% chance (slight bias based on market microstructure)
    
    Args:
        btc_direction: 'UP', 'DOWN', or 'FLAT' 
        entry_price: price paid for the token (0-1)
        volatility_bonus: extra WR for strong signals
    
    Returns:
        (won: bool, payout: float)
    """
    if btc_direction == 'UP':
        # We bought UP token; BTC went UP = WIN
        won = True
    elif btc_direction == 'DOWN':
        # We bought DOWN token; BTC went DOWN = WIN
        won = True
    else:
        # FLAT: 50/50
        won = np.random.random() < 0.50
    
    if won:
        payout = 1.0 - entry_price  # profit
    else:
        payout = -entry_price  # loss
    
    return won, payout


def run_backtest(candles_path='btc_5m_candles.json'):
    """Run the full direction backtest.
    
    Tests multiple configurations:
    1. Different lookback windows (1, 3, 6, 12 candles)
    2. Different direction thresholds (0.01%, 0.02%, 0.05%, 0.10%)
    3. Different entry prices (5¢, 10¢, 15¢, 20¢)
    """
    print("=" * 70)
    print("V18.5 — Binance Direction Backtest")
    print("=" * 70)
    
    candles = load_candles(candles_path)
    print(f"\nLoaded {len(candles)} candles ({len(candles)/288:.1f} days)")
    first = datetime.fromtimestamp(candles[0]['ts'], tz=timezone.utc).strftime('%Y-%m-%d')
    last = datetime.fromtimestamp(candles[-1]['ts'], tz=timezone.utc).strftime('%Y-%m-%d')
    print(f"Range: {first} to {last}")
    print(f"BTC range: ${min(c['close'] for c in candles):,.0f} - ${max(c['close'] for c in candles):,.0f}")
    
    # === TEST 1: Direction prediction accuracy ===
    print(f"\n{'='*70}")
    print("TEST 1: BTC 5-min Direction Prediction Accuracy")
    print(f"{'='*70}")
    print("For each candle, detect direction from last N candles,")
    print("then check if the next candle confirms the direction.\n")
    
    # We simulate: at time T, we detect direction from T-lookback to T
    # Then at T+1, we check: did BTC continue in that direction?
    # The "next 5m candle direction" = was close[T+1] > close[T] (UP) or < (DOWN)?
    
    results = {}
    
    for lookback in [1, 2, 3, 6, 12]:
        for min_change_pct in [0.01, 0.02, 0.05, 0.10, 0.20]:
            key = f"LB{lookback}_THR{min_change_pct}"
            total = 0
            correct = 0
            flat_count = 0
            up_predicted = 0
            down_predicted = 0
            up_correct = 0
            down_correct = 0
            
            for i in range(lookback, len(candles) - 1):
                direction, strength = compute_direction(candles, i, lookback, min_change_pct)
                
                # Actual next candle direction
                next_close = candles[i + 1]['close']
                current_close = candles[i]['close']
                actual_change = (next_close - current_close) / current_close * 100
                
                if actual_change > 0.001:
                    actual = 'UP'
                elif actual_change < -0.001:
                    actual = 'DOWN'
                else:
                    actual = 'FLAT'
                
                if direction == 'FLAT':
                    flat_count += 1
                    continue
                
                total += 1
                if direction == actual:
                    correct += 1
                
                if direction == 'UP':
                    up_predicted += 1
                    if actual == 'UP':
                        up_correct += 1
                else:
                    down_predicted += 1
                    if actual == 'DOWN':
                        down_correct += 1
            
            if total > 0:
                wr = correct / total * 100
            else:
                wr = 0
            
            up_wr = up_correct / up_predicted * 100 if up_predicted > 0 else 0
            down_wr = down_correct / down_predicted * 100 if down_predicted > 0 else 0
            
            # Flatten rate: how often we don't trade (direction=FLAT)
            flat_rate = flat_count / (len(candles) - lookback - 1) * 100
            
            results[key] = {
                'lookback': lookback,
                'threshold': min_change_pct,
                'total': total,
                'correct': correct,
                'wr': wr,
                'up_predicted': up_predicted,
                'up_correct': up_correct,
                'up_wr': up_wr,
                'down_predicted': down_predicted,
                'down_correct': down_correct,
                'down_wr': down_wr,
                'flat_rate': flat_rate,
            }
    
    # Print results table
    print(f"{'Config':<20} {'Trades':>8} {'WR%':>6} {'UP_WR%':>8} {'DN_WR%':>8} {'Flat%':>6}")
    print("-" * 60)
    for key, r in sorted(results.items(), key=lambda x: -x[1]['wr']):
        if r['total'] < 50:
            continue
        print(f"{key:<20} {r['total']:>8} {r['wr']:>6.1f} {r['up_wr']:>8.1f} {r['down_wr']:>8.1f} {r['flat_rate']:>6.1f}")
    
    # Best configs
    best = sorted(results.items(), key=lambda x: -x[1]['wr'] if x[1]['total'] >= 50 else 0)[:5]
    print(f"\nTop 5 configs by WR (min 50 trades):")
    for key, r in best:
        print(f"  {key}: {r['wr']:.1f}% WR ({r['total']} trades, UP={r['up_wr']:.1f}%, DN={r['down_wr']:.1f}%)")
    
    # === TEST 2: Polymarket Strategy Simulation ===
    print(f"\n{'='*70}")
    print("TEST 2: Polymarket Strategy Simulation")
    print(f"{'='*70}")
    print("Simulate buying cheap UP/DOWN tokens aligned with BTC direction.\n")
    
    # Use the best config from Test 1
    best_key = best[0][0]
    best_lb = results[best_key]['lookback']
    best_thr = results[best_key]['threshold']
    
    print(f"Best config: {best_key} (LB={best_lb}, THR={best_thr}%)")
    
    # For each direction signal, simulate Polymarket entry
    # Assumption: in a 5-min UP market, the UP token trades at 50-85¢ (rich side)
    #             and the DOWN token trades at 15-50¢ (cheap side)
    # If BTC is going UP, we buy the UP token. But UP is expensive (50-85¢).
    # If BTC is going DOWN, we buy the DOWN token. DOWN is cheap (15-50¢).
    # 
    # KEY INSIGHT: The profitable trade is:
    #   BTC trending DOWN → buy DOWN token (cheap) → high ROI when correct
    #   BTC trending UP → buy UP token (expensive) → low ROI even when correct
    #
    # But wait — what if BTC is trending UP and DOWN is cheap?
    #   → That means the market already thinks UP is more likely
    #   → But BTC hasn't moved yet, so the UP token is expensive
    #   → DON'T buy UP token at 80¢ (low ROI)
    #   → Instead, wait for a DOWN signal and buy DOWN at 10¢ (high ROI)
    
    # Let's simulate different entry scenarios
    np.random.seed(42)
    
    for entry_price in [0.03, 0.05, 0.08, 0.10, 0.15, 0.20]:
        for lb in [1, 2, 3]:
            for thr in [0.01, 0.05, 0.10, 0.20]:
                total_trades = 0
                wins = 0
                total_pnl = 0
                up_wins = 0
                up_total = 0
                down_wins = 0
                down_total = 0
                
                for i in range(lb, len(candles) - 1):
                    direction, strength = compute_direction(candles, i, lb, thr)
                    if direction == 'FLAT':
                        continue
                    
                    # Check next candle outcome
                    next_close = candles[i + 1]['close']
                    current_close = candles[i]['close']
                    actual_change = (next_close - current_close) / current_close
                    
                    # Did direction continue?
                    if direction == 'UP' and actual_change > 0:
                        won = True
                    elif direction == 'DOWN' and actual_change < 0:
                        won = True
                    else:
                        won = False
                    
                    total_trades += 1
                    
                    if direction == 'UP':
                        up_total += 1
                        if won:
                            up_wins += 1
                    else:
                        down_total += 1
                        if won:
                            down_wins += 1
                    
                    if won:
                        wins += 1
                        total_pnl += entry_price * (1.0 / entry_price - 1)  # profit
                    else:
                        total_pnl -= entry_price  # loss
                
                if total_trades >= 50:
                    wr = wins / total_trades * 100
                    roi = total_pnl / (total_trades * entry_price) * 100
                    
                    # Only print interesting results
                    if wr >= 55 or total_trades >= 500:
                        key = f"EP{entry_price:.2f}_LB{lb}_T{thr}"
                        pass  # Will print below
    
    # Simplified: just show the main strategy scenarios
    print("\nStrategy simulation: Buy cheap token (≤20¢) aligned with BTC direction")
    print(f"{'Entry':>6} {'Dir':>5} {'Trades':>8} {'WR%':>6} {'ROI%':>8} {'PnL':>10}")
    print("-" * 50)
    
    for entry_price in [0.03, 0.05, 0.08, 0.10, 0.15, 0.20]:
        for direction_filter in ['UP', 'DOWN', 'BOTH']:
            total_trades = 0
            wins = 0
            total_pnl = 0
            
            for i in range(3, len(candles) - 1):
                direction, strength = compute_direction(candles, i, 3, 0.05)
                if direction == 'FLAT':
                    continue
                
                # Filter direction
                if direction_filter != 'BOTH' and direction != direction_filter:
                    continue
                
                next_close = candles[i + 1]['close']
                current_close = candles[i]['close']
                actual_change = (next_close - current_close) / current_close
                
                # Did the direction continue for the full 5m candle?
                # We need to check the closing price, not just the direction
                # For Polymarket, the contract settles based on whether BTC
                # is up or down at the END of the 5m window
                # So we use: actual_change sign
                
                if direction == 'UP' and actual_change > 0:
                    won = True
                elif direction == 'DOWN' and actual_change < 0:
                    won = True
                else:
                    won = False
                
                total_trades += 1
                if won:
                    wins += 1
                    total_pnl += (1.0 - entry_price)  # settle at $1
                else:
                    total_pnl -= entry_price  # settle at $0
            
            if total_trades >= 50:
                wr = wins / total_trades * 100
                roi = total_pnl / (total_trades * entry_price) * 100
                
                print(f"${entry_price:.2f}  {direction_filter:>5} {total_trades:>8} {wr:>6.1f} {roi:>8.1f}% ${total_pnl:>10.2f}")
    
    # === TEST 3: RSI + Direction Combined ===
    print(f"\n{'='*70}")
    print("TEST 3: RSI + Direction Combined Strategy")
    print(f"{'='*70}")
    print("Only trade when BTC direction aligns with RSI signal.\n")
    
    # Compute RSI for each candle
    closes = np.array([c['close'] for c in candles])
    deltas = np.diff(closes)
    
    # 14-period RSI
    period = 14
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    
    avg_gains = np.zeros_like(gains, dtype=float)
    avg_losses = np.zeros_like(losses, dtype=float)
    
    # Initialize with SMA
    avg_gains[period] = np.mean(gains[1:period+1])
    avg_losses[period] = np.mean(losses[1:period+1])
    
    for i in range(period + 1, len(deltas)):
        avg_gains[i] = (avg_gains[i-1] * (period - 1) + gains[i]) / period
        avg_losses[i] = (avg_losses[i-1] * (period - 1) + losses[i]) / period
    
    rsi = np.zeros(len(candles))
    rsi[:period+1] = 50  # default
    for i in range(period+1, len(candles)-1):
        if avg_losses[i] == 0:
            rsi[i] = 100
        else:
            rs = avg_gains[i] / avg_losses[i]
            rsi[i] = 100 - (100 / (1 + rs))
    
    print(f"RSI computed for {len(candles)} candles")
    print(f"RSI range: {rsi[period:].min():.1f} - {rsi[period:].max():.1f}")
    print(f"RSI mean: {rsi[period:].mean():.1f}")
    
    # RSI + direction strategy
    entry_price = 0.10  # 10¢ entry
    
    print(f"\nRSI + Direction strategy (entry at ${entry_price:.2f}):")
    print(f"{'RSI Zone':<20} {'Dir':>5} {'Trades':>8} {'WR%':>6} {'PnL':>10}")
    print("-" * 55)
    
    for rsi_min, rsi_max, zone in [
        (0, 25, 'Severe Oversold'),
        (25, 30, 'Oversold'),
        (30, 35, 'Near Oversold'),
        (35, 45, 'Neutral-Low'),
        (45, 55, 'Neutral'),
        (55, 65, 'Neutral-High'),
        (65, 70, 'Near Overbought'),
        (70, 75, 'Overbought'),
        (75, 100, 'Severe Overbought'),
    ]:
        for direction_filter in ['UP', 'DOWN']:
            total = 0
            wins = 0
            pnl = 0
            
            for i in range(period + 1, len(candles) - 1):
                current_rsi = rsi[i]
                if current_rsi < rsi_min or current_rsi > rsi_max:
                    continue
                
                direction, strength = compute_direction(candles, i, 3, 0.05)
                if direction != direction_filter:
                    continue
                if direction == 'FLAT':
                    continue
                
                next_close = candles[i + 1]['close']
                current_close = candles[i]['close']
                actual_change = (next_close - current_close) / current_close
                
                if direction == 'UP' and actual_change > 0:
                    won = True
                elif direction == 'DOWN' and actual_change < 0:
                    won = True
                else:
                    won = False
                
                total += 1
                if won:
                    wins += 1
                    pnl += (1.0 - entry_price)
                else:
                    pnl -= entry_price
            
            if total >= 10:
                wr = wins / total * 100
                print(f"{zone:<20} {direction_filter:>5} {total:>8} {wr:>6.1f} ${pnl:>10.2f}")
    
    # === TEST 4: Monte Carlo Simulation with Full Polymarket Model ===
    print(f"\n{'='*70}")
    print("TEST 4: Monte Carlo — Polymarket 5-min Binary Strategy")
    print(f"{'='*70}")
    print("Simulate the full V18.5 strategy:")
    print("  - Detect BTC direction (Binance 5m candles)")
    print("  - Buy cheap token (UP or DOWN) at ≤20¢")
    print("  - Settle at $1 (win) or $0 (lose)")
    print()
    
    # First, measure direction WR from historical data
    direction_wins = 0
    direction_total = 0
    up_wins_hist = 0
    up_total_hist = 0
    down_wins_hist = 0
    down_total_hist = 0
    
    for i in range(3, len(candles) - 1):
        direction, strength = compute_direction(candles, i, 3, 0.05)
        if direction == 'FLAT':
            continue
        
        next_close = candles[i + 1]['close']
        current_close = candles[i]['close']
        actual_change = (next_close - current_close) / current_close
        
        direction_total += 1
        if (direction == 'UP' and actual_change > 0) or (direction == 'DOWN' and actual_change < 0):
            direction_wins += 1
        
        if direction == 'UP':
            up_total_hist += 1
            if actual_change > 0:
                up_wins_hist += 1
        else:
            down_total_hist += 1
            if actual_change < 0:
                down_wins_hist += 1
    
    dir_wr = direction_wins / direction_total * 100 if direction_total > 0 else 0
    up_wr_hist = up_wins_hist / up_total_hist * 100 if up_total_hist > 0 else 0
    down_wr_hist = down_wins_hist / down_total_hist * 100 if down_total_hist > 0 else 0
    
    print(f"Direction prediction WR: {dir_wr:.1f}% ({direction_wins}/{direction_total})")
    print(f"  UP direction WR: {up_wr_hist:.1f}% ({up_wins_hist}/{up_total_hist})")
    print(f"  DOWN direction WR: {down_wr_hist:.1f}% ({down_wins_hist}/{down_total_hist})")
    print(f"  BTC flat rate: {(len(candles) - 3 - 1 - direction_total) / (len(candles) - 3 - 1) * 100:.1f}%")
    
    # Monte Carlo: simulate 1000 bankrolls × 1000 trades
    np.random.seed(42)
    
    # Direction WR broken down by signal strength
    strength_bins = defaultdict(lambda: {'wins': 0, 'total': 0})
    for i in range(3, len(candles) - 1):
        direction, strength = compute_direction(candles, i, 3, 0.05)
        if direction == 'FLAT':
            continue
        
        next_close = candles[i + 1]['close']
        current_close = candles[i]['close']
        actual_change = (next_close - current_close) / current_close
        
        # Track signal direction + magnitude
        if direction == 'UP':
            key = f"UP_{strength:.3f}"
        else:
            key = f"DOWN_{strength:.3f}"
        
        strength_bins[key]['total'] += 1
        if (direction == 'UP' and actual_change > 0) or (direction == 'DOWN' and actual_change < 0):
            strength_bins[key]['wins'] += 1
    
    # Summarize by strength bucket
    print(f"\nDirection WR by signal strength:")
    print(f"{'Strength':>12} {'Dir':>5} {'Trades':>8} {'WR%':>6}")
    print("-" * 40)
    strength_buckets = [(0.01, 0.05, 'Weak'), (0.05, 0.10, 'Moderate'), (0.10, 0.20, 'Strong'), (0.20, 1.00, 'Very Strong')]
    for lo, hi, label in strength_buckets:
        for d in ['UP', 'DOWN']:
            total = 0
            wins = 0
            for key, data in strength_bins.items():
                dir_label = key.split('_')[0]
                strength = float(key.split('_')[1])
                if dir_label == d and lo <= strength < hi:
                    total += data['total']
                    wins += data['wins']
            if total >= 10:
                wr = wins / total * 100
                print(f"{label:>12} {d:>5} {total:>8} {wr:>6.1f}")
    
    print(f"\n{'='*70}")
    print("BOTTOM LINE: Polymarket Strategy WR Projection")
    print(f"{'='*70}")
    
    # The key metric: when we detect BTC direction and buy the corresponding
    # cheap token on Polymarket, what WR do we achieve?
    # 
    # If direction WR = 55% and we buy cheap token at 10¢:
    #   Win: pay 10¢, get $1 → profit 90¢ (ROI = 900%)
    #   Lose: pay 10¢, get $0 → loss 10¢
    #   Expected PnL per trade = 0.55 * 0.90 - 0.45 * 0.10 = 0.495 - 0.045 = $0.45
    #
    # But wait — the direction WR only tells us if BTC goes UP or DOWN.
    # On Polymarket, the cheap UP token in a DOWN market is 5-15¢.
    # If we buy cheap DOWN token at 10¢ when BTC is going DOWN:
    #   - If BTC continues DOWN (55% WR): we win, token → $1
    #   - If BTC reverses UP (45%): we lose, token → $0
    
    for entry_price in [0.03, 0.05, 0.08, 0.10, 0.15, 0.20]:
        for wr_scenario in [dir_wr, 55, 60, 65, 70, 80]:
            if wr_scenario > 100:
                continue
            win_pnl = 1.0 - entry_price
            lose_pnl = -entry_price
            ev = (wr_scenario / 100) * win_pnl + (1 - wr_scenario / 100) * lose_pnl
            roi = ev / entry_price * 100
            print(f"  Entry: ${entry_price:.2f} | WR: {wr_scenario:.0f}% | EV: ${ev:.3f} | ROI: {roi:.0f}%")
    
    # Final MC simulation
    print(f"\nMonte Carlo: 1000 bankrolls × 500 trades, starting at $100")
    print(f"Entry price: random between 5-20¢ (uniform)")
    print(f"Direction WR: {dir_wr:.1f}%")
    
    bankrolls = []
    for _ in range(1000):
        bankroll = 100.0
        trades = 0
        for _ in range(500):
            if bankroll < 1:
                break  # bankrupt
            
            entry = np.random.uniform(0.03, 0.20)
            bet = min(bankroll * 0.10, 5.0)
            shares = bet / entry
            
            # Win probability based on direction WR
            if np.random.random() < dir_wr / 100:
                # Win
                bankroll += shares * (1.0 - entry) - bet
            else:
                # Lose
                bankroll -= bet
            
            trades += 1
        
        bankrolls.append(bankroll)
    
    bankrolls = np.array(bankrolls)
    profitable = np.sum(bankrolls > 100) / len(bankrolls) * 100
    avg_bankroll = np.mean(bankrolls)
    median_bankroll = np.median(bankrolls)
    max_bankroll = np.max(bankrolls)
    min_bankroll = np.min(bankrolls)
    
    print(f"\n  Profitable: {profitable:.1f}%")
    print(f"  Avg bankroll: ${avg_bankroll:,.2f} (started $100)")
    print(f"  Median bankroll: ${median_bankroll:,.2f}")
    print(f"  Max bankroll: ${max_bankroll:,.2f}")
    print(f"  Min bankroll: ${min_bankroll:,.2f}")
    print(f"  P25: ${np.percentile(bankrolls, 25):,.2f} | P75: ${np.percentile(bankrolls, 75):,.2f}")
    print(f"  P90: ${np.percentile(bankrolls, 90):,.2f} | P99: ${np.percentile(bankrolls, 99):,.2f}")


if __name__ == '__main__':
    run_backtest()