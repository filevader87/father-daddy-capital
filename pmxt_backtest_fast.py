#!/usr/bin/env python3
"""
V18.8 PMXT Backtest — FAST vectorized version.
Uses numpy for RSI + signal logic instead of calling generate_signal_v188.
"""
import pyarrow.parquet as pq
from pathlib import Path
import pandas as pd
import numpy as np
import sys, os, gc, time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BANKROLL = 100.0
RSI_PERIOD = 14
MIN_CONF = 0.50

# V18.8 signal thresholds (from pm_engine_v18_8.py)
RSI_OVERSOLD_SEVERE = 25     # T1 oversold
RSI_OVERBOUGHT_SEVERE = 75   # T1 overbought
RSI_OVERSOLD_MOD = 30        # T2 oversold
RSI_OVERBOUGHT_MOD = 70      # T2 overbought
RSI_NEAR_OVERSOLD = 35       # T3 oversold confirmation
RSI_NEAR_OVERBOUGHT = 65     # T3 overbought confirmation

TIER_MAX_PRICE = {
    'severe_oversold_down': 0.50, 'severe_overbought_up': 0.50,
    'oversold_down': 0.20, 'overbought_up': 0.20,
    'direction_down_cheap': 0.12, 'direction_up_cheap': 0.12,
}
TIER_SIZE = {
    'severe_oversold_down': 0.10, 'severe_overbought_up': 0.10,
    'oversold_down': 0.06, 'overbought_up': 0.05,
    'direction_down_cheap': 0.03, 'direction_up_cheap': 0.03,
}


def compute_rsi_vec(prices, period=14):
    """Vectorized RSI computation."""
    n = len(prices)
    if n < period + 1:
        return np.full(n, 50.0)
    deltas = np.diff(prices, prepend=prices[0])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g = np.zeros(n)
    avg_l = np.zeros(n)
    avg_g[period] = np.mean(gains[1:period+1])
    avg_l[period] = np.mean(losses[1:period+1])
    for i in range(period+1, n):
        avg_g[i] = (avg_g[i-1]*(period-1) + gains[i]) / period
        avg_l[i] = (avg_l[i-1]*(period-1) + losses[i]) / period
    rs = np.where(avg_l > 0, avg_g / avg_l, 100.0)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi[:period] = 50.0
    return rsi


def classify_signal(rsi_val, price, direction_pct):
    """Fast signal classification without calling full engine.
    Returns (tier_name, confidence) or None."""
    # T1: Severe oversold/overbought
    if rsi_val < RSI_OVERSOLD_SEVERE and price < 0.30:
        return ('severe_oversold_down', 0.90)
    if rsi_val > RSI_OVERBOUGHT_SEVERE and price > 0.70:
        return ('severe_overbought_up', 0.90)
    
    # T2: Moderate oversold/overbought with confirmations
    if rsi_val < RSI_OVERSOLD_MOD and price < 0.20:
        return ('oversold_down', 0.78)
    if rsi_val > RSI_OVERBOUGHT_MOD and price > 0.80:
        return ('overbought_up', 0.78)
    
    # T3: Direction + cheap side
    if abs(direction_pct) >= 0.05 and price < 0.12:
        if direction_pct > 0:  # going up → buy cheap YES
            return ('direction_up_cheap', 0.62)
        else:  # going down → buy cheap NO
            return ('direction_down_cheap', 0.62)
    
    return None


def run_backtest(data_dir='pmxt_data/'):
    data_dir = Path(data_dir)
    files = sorted(data_dir.glob('*.parquet'))
    valid_files = []
    for f in files:
        try:
            pf = pq.ParquetFile(str(f))
            if pf.metadata.num_rows > 10000:
                valid_files.append(f)
        except:
            continue
    
    print(f"Found {len(valid_files)} valid files")
    
    total_trades = 0
    total_wins = 0
    bankroll = BANKROLL
    tier_stats = defaultdict(lambda: dict(trades=0, wins=0, pnl=0.0))
    
    for fidx, fpath in enumerate(valid_files):
        print(f"\n[{fidx+1}/{len(valid_files)}] {fpath.name}...", end=" ", flush=True)
        fstart = time.time()
        pf = pq.ParquetFile(str(fpath))
        n_rgs = min(pf.metadata.num_row_groups, 20)
        
        file_trades = 0
        file_wins = 0
        
        for rg in range(0, n_rgs, 3):  # Process every 3rd RG
            try:
                table = pf.read_row_group(rg, columns=['market', 'asset_id', 'price'])
                df = table.to_pandas()
                df['price'] = pd.to_numeric(df['price'], errors='coerce')
                df = df.dropna(subset=['price'])
                
                # Filter cheap side (<50¢)
                cheap = df[df['price'] < 0.50].iloc[::20].copy()
                
                if len(cheap) < 10:
                    continue
                
                # Group by token — build series
                for (market, asset_id), group in cheap.groupby(['market', 'asset_id']):
                    if len(group) < 40:
                        continue
                    
                    prices = group['price'].values.astype(float)
                    
                    if np.max(prices) - np.min(prices) < 0.03:
                        continue
                    
                    final_price = float(prices[-1])
                    token_wins = final_price > 0.5
                    
                    # Compute RSI
                    rsi = compute_rsi_vec(prices, RSI_PERIOD)
                    n = len(prices)
                    
                    for i in range(RSI_PERIOD + 10, n):
                        price = float(prices[i])
                        if price < 0.01 or price > 0.50:
                            continue
                        
                        # Direction
                        if i >= 5:
                            direction_pct = (prices[i] - prices[i-5]) / max(prices[i-5], 0.001) * 100
                        else:
                            direction_pct = 0.0
                        
                        signal = classify_signal(rsi[i], price, direction_pct)
                        if signal is None:
                            continue
                        
                        tier_name, confidence = signal
                        if confidence < MIN_CONF:
                            continue
                        
                        max_price = TIER_MAX_PRICE.get(tier_name, 0.12)
                        tier_size = TIER_SIZE.get(tier_name, 0.03)
                        
                        if price > max_price:
                            continue
                        
                        position = tier_size * bankroll
                        shares = position / price
                        
                        if token_wins:
                            pnl = shares * (1.0 - price)
                        else:
                            pnl = -position
                        
                        bankroll += pnl
                        total_trades += 1
                        file_trades += 1
                        if pnl > 0:
                            total_wins += 1
                            file_wins += 1
                        
                        tier_stats[tier_name]['trades'] += 1
                        if pnl > 0:
                            tier_stats[tier_name]['wins'] += 1
                        tier_stats[tier_name]['pnl'] += pnl
                
                del df, cheap
                gc.collect()
            except Exception as e:
                print(f"E:{str(e)[:30]}", end=" ", flush=True)
                continue
        
        elapsed = time.time() - fstart
        wr = file_wins / max(file_trades, 1) * 100
        print(f"{file_trades}t, WR={wr:.1f}%, ${bankroll:.2f} ({elapsed:.0f}s)", flush=True)
        gc.collect()
    
    # Final report
    print("\n" + "=" * 70)
    print("V18.8 PMXT BACKTEST RESULTS")
    print("=" * 70)
    print(f"Total trades: {total_trades}")
    print(f"Total wins: {total_wins}")
    print(f"Overall WR: {total_wins/max(total_trades,1)*100:.1f}%")
    print(f"Final bankroll: {bankroll:.2f} (from {BANKROLL:.0f})")
    print(f"ROI: {(bankroll/BANKROLL - 1)*100:.1f}%")
    print()
    for tier, stats in sorted(tier_stats.items()):
        if stats['trades'] > 0:
            t_wr = stats['wins'] / stats['trades'] * 100
            print(f"  {tier:30s}: {stats['trades']:5d} trades, {t_wr:.1f}% WR, ${stats['pnl']:.2f} P&L")


if __name__ == '__main__':
    run_backtest()