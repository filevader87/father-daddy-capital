#!/usr/bin/env python3
"""
V18.8 PMXT Backtest — T3 Strict Filters Validation.

Tests T3 with tightened parameters:
- MIN_DIR: 0.15% (was 0.05%)
- RSI zone gate: DOWN only below 45, UP only above 55
- Regime enforcement: DOWN needs trending_down, UP needs trending_up  
- Confidence floor: 0.70 (was 0.62)
- Max entry price: 8¢ (was 12¢)
- Direction alignment: UP buys pro-BTC tokens, DOWN buys anti-BTC tokens
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
HOLD_CANDLES = 30

TIER_MAX_PRICE = {
    'severe_oversold_down': 0.50, 'severe_overbought_up': 0.50,
    'oversold_down': 0.20, 'overbought_up': 0.20,
    'direction_down_cheap': 0.08, 'direction_up_cheap': 0.08,  # T3 tightened to 8¢
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
    
    # Load PMXT
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
            df = df[df['price'] < 0.50]
            df = df.iloc[::10]
            chunks.append(df)
        if chunks:
            combined = pd.concat(chunks, ignore_index=True)
            all_frames.append(combined)
            print(f"{len(combined)} rows", flush=True)
        del chunks; gc.collect()
    
    print("Combining all frames...")
    all_data = pd.concat(all_frames, ignore_index=True)
    print(f"Total cheap-side rows: {len(all_data)}")
    
    # Convert timestamps
    if pd.api.types.is_datetime64_any_dtype(all_data['timestamp_received']):
        dtype_str = str(all_data['timestamp_received'].dtype)
        int_vals = all_data['timestamp_received'].astype(np.int64)
        if 'ms' in dtype_str:
            all_data['ts_sec'] = (int_vals // 10**3).astype(np.int64)
        elif 'us' in dtype_str:
            all_data['ts_sec'] = (int_vals // 10**6).astype(np.int64)
        elif 'ns' in dtype_str:
            all_data['ts_sec'] = (int_vals // 10**9).astype(np.int64)
        else:
            all_data['ts_sec'] = int_vals.astype(np.int64)
    else:
        all_data['ts_sec'] = all_data['timestamp_received'].astype(np.int64)
    
    all_data['market_str'] = all_data['market'].astype(str)
    all_data['asset_str'] = all_data['asset_id'].astype(str)
    
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
    
    print(f"Active tokens: {len(token_arrays)}")
    
    # Classify tokens by BTC correlation
    print("Classifying tokens by BTC correlation...")
    token_corr = {}
    for key, (ts, ps) in token_arrays.items():
        btc_at_token = np.interp(ts, btc_times // 1000, btc_closes)
        if len(ps) > 20:
            corr = np.corrcoef(ps, btc_at_token)[0, 1]
        else:
            corr = 0.0
        token_corr[key] = corr
    
    print(f"Pro-BTC (corr>0.3): {sum(1 for v in token_corr.values() if v > 0.3)}")
    print(f"Anti-BTC (corr<-0.3): {sum(1 for v in token_corr.values() if v < -0.3)}")
    
    # ══════════════════════════════════════════════════════════
    # RUN THREE SCENARIOS
    # ══════════════════════════════════════════════════════════
    scenarios = {
        'T1_T2_only': True,      # No T3
        'T3_loose': True,        # T3 with old loose filters
        'T3_strict': True,       # T3 with new strict filters
    }
    
    for scenario_name, enabled in scenarios.items():
        if not enabled:
            continue
        print(f"\n{'='*70}")
        print(f"SCENARIO: {scenario_name}")
        print(f"{'='*70}")
        
        total_trades = 0
        total_wins = 0
        bankroll = BANKROLL
        tier_stats = defaultdict(lambda: dict(trades=0, wins=0, pnl=0.0))
        
        for i in range(RSI_PERIOD + 10, n_btc - HOLD_CANDLES, 3):
            btc_price = float(btc_closes[i])
            btc_rsi_val = float(btc_rsi[i])
            btc_ts_sec = btc_times[i] / 1000
            
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
            
            # Direction
            if direction_pct > 0.05:
                direction = 'UP'
            elif direction_pct < -0.05:
                direction = 'DOWN'
            else:
                direction = 'FLAT'
            
            # Signal classification per scenario
            signal = None
            
            # T1/T2 — same for all scenarios
            if btc_rsi_val < 25:
                signal = ('severe_oversold', 'DOWN', 0.90)
            elif btc_rsi_val > 75:
                signal = ('severe_overbought', 'UP', 0.90)
            elif btc_rsi_val < 30:
                signal = ('oversold', 'DOWN', 0.78)
            elif btc_rsi_val > 70:
                signal = ('overbought', 'UP', 0.78)
            
            # Add MIN_DIR 3-candle lookback
            if i >= 3:
                dir_3 = (btc_closes[i] - btc_closes[i-3]) / max(btc_closes[i-3], 0.001) * 100
                if abs(dir_3) < 0.15:
                    direction = 'FLAT'
            
            # T3 — scenario-specific
            if signal is None and scenario_name == 'T3_loose':
                # OLD: MIN_DIR 0.05%, any RSI, any regime
                if abs(direction_pct) >= 0.05:
                    if direction_pct > 0:
                        signal = ('direction_up', 'UP', 0.62)
                    else:
                        signal = ('direction_down', 'DOWN', 0.64)
            
            elif signal is None and scenario_name == 'T3_strict':
                # NEW: MIN_DIR 0.15%, RSI zone gate, regime enforcement
                if abs(direction_pct) >= 0.15:
                    if direction == 'DOWN' and regime == 'trending_down' and btc_rsi_val < 45:
                        signal = ('direction_down', 'DOWN', 0.68)
                    elif direction == 'UP' and regime == 'trending_up' and btc_rsi_val > 55:
                        signal = ('direction_up', 'UP', 0.70)
            
            if signal is None:
                continue
            
            tier_name_raw, direction, confidence = signal
            
            # Find cheapest aligned token
            best_price = None
            best_token = None
            best_tier = None
            
            for key, (ts_arr, ps_arr) in token_arrays.items():
                idx = np.searchsorted(ts_arr, btc_ts_sec)
                if idx >= len(ts_arr) or idx <= 0:
                    continue
                if abs(ts_arr[idx] - btc_ts_sec) > 300:
                    continue
                
                price = float(ps_arr[idx])
                if price < 0.01:
                    continue
                
                # Max price per tier
                if btc_rsi_val < 25:
                    max_price = 0.50; tier_name = 'severe_oversold_down'
                elif btc_rsi_val > 75:
                    max_price = 0.50; tier_name = 'severe_overbought_up'
                elif btc_rsi_val < 30:
                    max_price = 0.20; tier_name = 'oversold_down'
                elif btc_rsi_val > 70:
                    max_price = 0.20; tier_name = 'overbought_up'
                elif scenario_name == 'T3_strict':
                    max_price = 0.08  # Tightened to 8¢
                    if direction == 'DOWN':
                        tier_name = 'direction_down_cheap'
                    else:
                        tier_name = 'direction_up_cheap'
                else:
                    max_price = 0.12  # Old loose 12¢
                    if direction == 'DOWN':
                        tier_name = 'direction_down_cheap'
                    else:
                        tier_name = 'direction_up_cheap'
                
                if price > max_price:
                    continue
                
                # Direction alignment
                corr = token_corr.get(key, 0.0)
                if direction == 'DOWN' and corr > 0.1:
                    continue  # Don't buy pro-BTC on DOWN signal
                if direction == 'UP' and corr < -0.1:
                    continue  # Don't buy anti-BTC on UP signal
                
                if best_price is None or price < best_price:
                    best_price = price
                    best_token = key
                    best_tier = tier_name
            
            if best_token is None:
                continue
            
            # Compute P&L
            tier_size = TIER_SIZE.get(best_tier, 0.03)
            ts_arr, ps_arr = token_arrays[best_token]
            entry_idx = np.searchsorted(ts_arr, btc_ts_sec)
            if entry_idx >= len(ts_arr) or entry_idx <= 0:
                continue
            exit_idx = min(entry_idx + HOLD_CANDLES, len(ps_arr) - 1)
            
            entry_price = float(ps_arr[entry_idx])
            exit_price = float(ps_arr[exit_idx])
            
            position = tier_size * bankroll
            shares = position / entry_price
            pnl = shares * (exit_price - entry_price)
            
            bankroll += pnl
            total_trades += 1
            if pnl > 0:
                total_wins += 1
            
            tier_stats[best_tier]['trades'] += 1
            if pnl > 0:
                tier_stats[best_tier]['wins'] += 1
            tier_stats[best_tier]['pnl'] += pnl
        
        print(f"Total trades: {total_trades}")
        print(f"Total wins: {total_wins}")
        print(f"Overall WR: {total_wins/max(total_trades,1)*100:.1f}%")
        print(f"Final bankroll: ${bankroll:.2f} (from ${BANKROLL:.0f})")
        print(f"ROI: {(bankroll/BANKROLL - 1)*100:.1f}%")
        print()
        for tier, stats in sorted(tier_stats.items()):
            if stats['trades'] > 0:
                t_wr = stats['wins'] / stats['trades'] * 100
                print(f"  {tier:30s}: {stats['trades']:5d} trades, {t_wr:.1f}% WR, ${stats['pnl']:.2f} P&L")


if __name__ == '__main__':
    run_backtest()