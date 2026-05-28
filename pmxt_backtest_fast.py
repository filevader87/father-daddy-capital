#!/usr/bin/env python3
"""
V18.8 PMXT Backtest — Vectorized token price series with BTC RSI signals.
"""
import json, gc, sys, os, time
from pathlib import Path
from collections import defaultdict
import numpy as np
import pyarrow.parquet as pq
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BANKROLL = 100.0
RSI_PERIOD = 14
HOLD_CANDLES = 20

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


def compute_rsi(prices, period=14):
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


def run_backtest(btc_file='btc_may25_2026_1m.json', pmx_dir='pmxt_data/'):
    print("Loading BTC 1-minute candles...")
    with open(btc_file) as f:
        btc_data = json.load(f)
    
    btc_closes = np.array([float(d[4]) for d in btc_data])
    btc_times = np.array([int(d[0]) for d in btc_data])
    n_btc = len(btc_closes)
    print(f"BTC: {n_btc} candles, ${btc_closes.min():,.0f}-${btc_closes.max():,.0f}")
    
    btc_rsi = compute_rsi(btc_closes, RSI_PERIOD)
    
    # Load PMXT token prices — vectorized approach
    pmx_dir = Path(pmx_dir)
    files = sorted(pmx_dir.glob('*.parquet'))
    valid_files = []
    for f in files:
        try:
            pf = pq.ParquetFile(str(f))
            if pf.metadata.num_rows > 10000:
                valid_files.append(f)
        except:
            continue
    print(f"Found {len(valid_files)} valid PMXT files")
    
    # Phase 1: Build token price series using vectorized pandas
    print("Building token price series (vectorized)...")
    all_frames = []
    
    for fidx, fpath in enumerate(valid_files):
        print(f"  [{fidx+1}/{len(valid_files)}] {fpath.name}...", end=" ", flush=True)
        pf = pq.ParquetFile(str(fpath))
        
        chunks = []
        for rg in range(min(10, pf.metadata.num_row_groups)):
            table = pf.read_row_group(rg, columns=['market', 'asset_id', 'price', 'timestamp_received'])
            df = table.to_pandas()
            df['price'] = pd.to_numeric(df['price'], errors='coerce')
            df = df.dropna(subset=['price'])
            # Cheap side only
            df = df[df['price'] < 0.50]
            # Sample every 10th for speed
            df = df.iloc[::10]
            chunks.append(df)
        
        if chunks:
            combined = pd.concat(chunks, ignore_index=True)
            all_frames.append(combined)
            print(f"{len(combined)} rows", flush=True)
        else:
            print("0 rows", flush=True)
        del chunks
        gc.collect()
    
    print("Combining all frames...")
    all_data = pd.concat(all_frames, ignore_index=True)
    print(f"Total cheap-side rows: {len(all_data)}")
    
    # Convert timestamps to epoch seconds for comparison with BTC data
    if pd.api.types.is_datetime64_any_dtype(all_data['timestamp_received']):
        # pandas datetime64 → int64 nanoseconds → convert to seconds
        all_data['ts_sec'] = (all_data['timestamp_received'].astype(np.int64) // 10**9).astype(np.int64)
    else:
        all_data['ts_sec'] = all_data['timestamp_received'].astype(np.int64)
    
    # Convert market/asset_id to string for grouping
    all_data['market_str'] = all_data['market'].astype(str)
    all_data['asset_str'] = all_data['asset_id'].astype(str)
    
    # Group by token and build price arrays
    print("Grouping by token...")
    grouped = all_data.groupby(['market_str', 'asset_str'])
    
    token_arrays = {}
    for (market, asset), group in grouped:
        if len(group) < 30:
            continue
        group = group.sort_values('ts_sec')
        ts = group['ts_sec'].values
        ps = group['price'].values.astype(float)
        if np.max(ps) - np.min(ps) < 0.03:
            continue
        token_arrays[(market, asset)] = (ts, ps)
    
    print(f"Active tokens (>=30 pts, range>3¢): {len(token_arrays)}")
    
    # Phase 2: Run BTC RSI-based signal backtest
    print("\nRunning V18.8 signal backtest...")
    
    total_trades = 0
    total_wins = 0
    bankroll = BANKROLL
    tier_stats = defaultdict(lambda: dict(trades=0, wins=0, pnl=0.0))
    price_bins = defaultdict(lambda: dict(trades=0, wins=0, pnl=0.0))
    
    # Pre-build array of BTC timestamps for fast lookup
    btc_ts_arr = btc_times
    
    # For each BTC candle, check if a V18.8 signal fires
    for i in range(RSI_PERIOD + 10, n_btc - HOLD_CANDLES, 3):  # Check every 3rd candle
        btc_price = float(btc_closes[i])
        btc_rsi_val = float(btc_rsi[i])
        btc_ts = btc_ts_arr[i]
        btc_ts_sec = btc_ts / 1000  # Convert Binance ms to seconds for PMXT comparison
        
        # Direction
        if i >= 5:
            direction_pct = (btc_closes[i] - btc_closes[i-5]) / max(btc_closes[i-5], 0.001) * 100
        else:
            direction_pct = 0.0
        
        # Regime
        if i >= 20:
            sma20 = np.mean(btc_closes[i-20:i])
            if btc_price > sma20 * 1.001:
                regime = 'trending_up'
            elif btc_price < sma20 * 0.999:
                regime = 'trending_down'
            else:
                regime = 'ranging'
        else:
            regime = 'ranging'
        
        # Signal classification
        signal = None
        if btc_rsi_val < 25:
            signal = ('severe_oversold', 'DOWN', 0.90)
        elif btc_rsi_val > 75:
            signal = ('severe_overbought', 'UP', 0.90)
        elif btc_rsi_val < 30:
            signal = ('oversold', 'DOWN', 0.78)
        elif btc_rsi_val > 70:
            signal = ('overbought', 'UP', 0.78)
        elif abs(direction_pct) >= 0.05:
            if direction_pct > 0:
                signal = ('direction_up', 'UP', 0.62)
            else:
                signal = ('direction_down', 'DOWN', 0.62)
        
        if signal is None:
            continue
        
        tier_name_raw, direction, confidence = signal
        if confidence < 0.50:
            continue
        
        # Find cheapest matching token near this timestamp
        best_price = None
        best_token = None
        best_tier = None
        
        for key, (ts_arr, ps_arr) in token_arrays.items():
            # Find closest timestamp to our BTC candle
            idx = np.searchsorted(ts_arr, btc_ts)
            if idx >= len(ts_arr):
                idx = len(ts_arr) - 1
            if idx < 0:
                continue
            
            # Timestamp must be within 5 minutes (PMXT timestamps are in seconds)
            if abs(ts_arr[idx] - btc_ts_sec) > 300:
                continue
            
            price = float(ps_arr[idx])
            if price < 0.01 or price > 0.50:
                continue
            
            # Determine tier and max price
            if btc_rsi_val < 25:
                tier_name = 'severe_oversold_down'
                max_price = 0.50
            elif btc_rsi_val > 75:
                tier_name = 'severe_overbought_up'
                max_price = 0.50
            elif btc_rsi_val < 30:
                tier_name = 'oversold_down'
                max_price = 0.20
            elif btc_rsi_val > 70:
                tier_name = 'overbought_up'
                max_price = 0.20
            elif abs(direction_pct) >= 0.05:
                tier_name = 'direction_down_cheap' if direction_pct < 0 else 'direction_up_cheap'
                max_price = 0.12
            else:
                continue
            
            if price > max_price:
                continue
            
            # Keep cheapest eligible token
            if best_price is None or price < best_price:
                best_price = price
                best_token = key
                best_tier = tier_name
        
        if best_token is None:
            continue
        
        # Found a trade! Compute P&L
        tier_name = best_tier
        tier_size = TIER_SIZE[tier_name]
        
        ts_arr, ps_arr = token_arrays[best_token]
        entry_idx = np.searchsorted(ts_arr, btc_ts)
        exit_idx = min(entry_idx + HOLD_CANDLES, len(ts_arr) - 1)
        
        entry_price = float(ps_arr[entry_idx])
        exit_price = float(ps_arr[exit_idx])
        
        position = tier_size * bankroll
        shares = position / entry_price
        
        # P&L: if cheap token price goes up, we profit
        pnl = shares * (exit_price - entry_price)
        
        bankroll += pnl
        total_trades += 1
        if pnl > 0:
            total_wins += 1
        
        tier_stats[tier_name]['trades'] += 1
        if pnl > 0:
            tier_stats[tier_name]['wins'] += 1
        tier_stats[tier_name]['pnl'] += pnl
        
        if entry_price < 0.05:
            bin_name = '<5¢'
        elif entry_price < 0.10:
            bin_name = '5-10¢'
        elif entry_price < 0.20:
            bin_name = '10-20¢'
        elif entry_price < 0.30:
            bin_name = '20-30¢'
        else:
            bin_name = '30-50¢'
        price_bins[bin_name]['trades'] += 1
        if pnl > 0:
            price_bins[bin_name]['wins'] += 1
        price_bins[bin_name]['pnl'] += pnl
    
    # Final report
    print("\n" + "=" * 70)
    print("V18.8 PMXT BACKTEST RESULTS (BTC RSI + PMXT tokens)")
    print("=" * 70)
    print(f"Total trades: {total_trades}")
    print(f"Total wins: {total_wins}")
    print(f"Overall WR: {total_wins/max(total_trades,1)*100:.1f}%")
    print(f"Final bankroll: ${bankroll:.2f} (from ${BANKROLL:.0f})")
    print(f"ROI: {(bankroll/BANKROLL - 1)*100:.1f}%")
    print()
    print("BY TIER:")
    for tier, stats in sorted(tier_stats.items()):
        if stats['trades'] > 0:
            t_wr = stats['wins'] / stats['trades'] * 100
            print(f"  {tier:30s}: {stats['trades']:5d} trades, {t_wr:.1f}% WR, ${stats['pnl']:.2f} P&L")
    print()
    print("BY PRICE BIN:")
    for tier, stats in sorted(price_bins.items()):
        if stats['trades'] > 0:
            t_wr = stats['wins'] / stats['trades'] * 100
            print(f"  {tier:10s}: {stats['trades']:5d} trades, {t_wr:.1f}% WR, ${stats['pnl']:.2f} P&L")


if __name__ == '__main__':
    run_backtest()