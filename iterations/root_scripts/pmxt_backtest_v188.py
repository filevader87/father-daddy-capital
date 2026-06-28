#!/usr/bin/env python3
"""Quick V18.8 PMXT backtest — single file, 6 row groups."""
import pyarrow.parquet as pq
from pathlib import Path
import pandas as pd
import numpy as np
import sys, os, gc, time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_engine_v18_8 as eng

BANKROLL = 100.0
RSI_PERIOD = 14
MIN_CONF = 0.50

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

start = time.time()

files = sorted(Path('pmxt_data').glob('*.parquet'))
# Filter valid files
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
    
    for rg in range(0, n_rgs, 2):  # Skip every other RG
        try:
            table = pf.read_row_group(rg, columns=['market', 'asset_id', 'price'])
            df = table.to_pandas()
            df['price'] = pd.to_numeric(df['price'], errors='coerce')
            df = df.dropna(subset=['price'])
            
            # Filter cheap side (<50¢)
            cheap = df[df['price'] < 0.50].iloc[::20]  # Sample every 20th
            
            if len(cheap) < 10:
                continue
            
            grouped = cheap.groupby(['market', 'asset_id'])
            
            for (market, asset_id), group in grouped:
                if len(group) < 30:
                    continue
                
                prices = group['price'].values.astype(float)
                if len(prices) < 30 or np.max(prices) - np.min(prices) < 0.03:
                    continue
                
                final_price = float(prices[-1])
                token_wins = final_price > 0.5
                
                # Compute RSI
                n = len(prices)
                deltas = np.diff(prices, prepend=prices[0])
                gains = np.where(deltas > 0, deltas, 0.0)
                losses = np.where(deltas < 0, -deltas, 0.0)
                avg_g = np.zeros(n)
                avg_l = np.zeros(n)
                avg_g[14] = np.mean(gains[1:15])
                avg_l[14] = np.mean(losses[1:15])
                for i in range(15, n):
                    avg_g[i] = (avg_g[i-1]*13 + gains[i]) / 14
                    avg_l[i] = (avg_l[i-1]*13 + losses[i]) / 14
                rs = np.where(avg_l > 0, avg_g / avg_l, 100.0)
                rsi = 100.0 - (100.0 / (1.0 + rs))
                rsi[:14] = 50.0
                
                for i in range(24, n, 5):
                    price = float(prices[i])
                    if price < 0.01 or price > 0.50:
                        continue
                    
                    window = prices[:i]
                    signal = eng.generate_signal_v188(window, candles=None, idx=i-1)
                    
                    if signal.get('strategy', 'no_signal') == 'no_signal':
                        continue
                    
                    confidence = signal.get('confidence', 0.0)
                    if confidence < MIN_CONF:
                        continue
                    
                    strategy = signal.get('strategy', 'unknown')
                    
                    tier_name = 'direction_down_cheap'
                    for k in TIER_SIZE:
                        if k.replace('_', '') in strategy.replace('_', '').lower():
                            tier_name = k
                            break
                    
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
            print(f"RG{rg}err:{str(e)[:40]}", end=" ", flush=True)
            continue
    
    elapsed = time.time() - fstart
    wr = file_wins / max(file_trades, 1) * 100
    print(f"{file_trades} trades, WR={wr:.1f}%, ${bankroll:.2f} ({elapsed:.0f}s)")
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
total_elapsed = time.time() - start
print(f"\nTotal time: {total_elapsed:.0f}s")