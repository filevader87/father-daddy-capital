#!/usr/bin/env python3
"""
V18.8 PMXT Backtest — Streamed & Sampled.
Reconstructs YES/NO token price series from PMXT trade data,
then applies V18.8 signal generation logic.

Key insight: group by (market, asset_id) to get token price series.
For binary markets, YES + NO tokens share a market.
We buy the CHEAP side (price < 0.50).
"""

import pyarrow.parquet as pq
import numpy as np
import json, gc, os, sys
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict
import warnings; warnings.filterwarnings('ignore')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_engine_v18_8 as eng

INITIAL_BANKROLL = 100.0

# V18.8 params
TIER_MAX_PRICE = {
    'severe_oversold_down': 0.50,
    'severe_overbought_up': 0.50,
    'oversold_down': 0.20,
    'overbought_up': 0.20,
    'direction_down_cheap': 0.12,
    'direction_up_cheap': 0.12,
}
TIER_SIZE = {
    'severe_oversold_down': 0.10,
    'severe_overbought_up': 0.10,
    'oversold_down': 0.06,
    'overbought_up': 0.05,
    'direction_down_cheap': 0.03,
    'direction_up_cheap': 0.03,
}
MIN_CONFIDENCE = 0.50
MIN_TRADES = 50  # Minimum trades per token to include


def find_binary_tokens(data_dir, min_trades=MIN_TRADES):
    """Scan parquet files to find high-volume binary token pairs."""
    data_dir = Path(data_dir)
    files = sorted(data_dir.glob("*.parquet"))
    
    # Token trade counts across all files
    token_counts = defaultdict(int)
    token_first_price = {}  # first price seen (to determine YES/NO)
    
    print("Scanning for binary tokens...", flush=True)
    for fidx, fpath in enumerate(files[:2]):  # First 2 files only for speed
        pf = pq.ParquetFile(str(fpath))
        if pf.metadata.num_rows < 10000:
            continue
        
        # Sample first 500K rows
        n_groups = min(pf.metadata.num_row_groups, 5)
        for rg in range(n_groups):
            try:
                table = pf.read_row_group(rg, columns=['market', 'asset_id', 'price'])
                for i in range(0, len(table), 100):  # Sample every 100th row
                    m = table.column('market')[i].as_py()
                    a = table.column('asset_id')[i].as_py()
                    p = table.column('price')[i].as_py()
                    if p is not None and m is not None and a is not None:
                        key = (m, a)
                        token_counts[key] += 1
                        if key not in token_first_price:
                            token_first_price[key] = float(p)
            except:
                continue
    
    # Group by market to find pairs (YES + NO token)
    market_tokens = defaultdict(list)
    for (m, a), count in token_counts.items():
        if count >= 10:
            market_tokens[m].append((a, count, token_first_price.get((m, a), 0.5)))
    
    # Find paired tokens (same market, 2 assets)
    binary_markets = []
    for m, tokens in market_tokens.items():
        if len(tokens) >= 2:
            # Sort by price (YES token has higher first-seen price generally)
            tokens.sort(key=lambda x: x[2])
            # The cheaper one is likely the NO or the less likely outcome
            binary_markets.append({
                'market': m,
                'tokens': tokens
            })
    
    print(f"Found {len(binary_markets)} binary markets with paired tokens")
    
    # Filter to cheap-side tokens (price < 0.50 at first observation)
    cheap_tokens = []
    for bm in binary_markets:
        for aid, count, price in bm['tokens']:
            if price < 0.50 and count >= min_trades // 10:
                cheap_tokens.append((bm['market'], aid, price, count))
    
    print(f"Found {len(cheap_tokens)} cheap-side tokens (price < 0.50)")
    
    # Sort by trade count, take top 200
    cheap_tokens.sort(key=lambda x: x[3], reverse=True)
    return cheap_tokens[:200]


def reconstruct_token_series(data_dir, market, asset_id):
    """Reconstruct price time series for a single token from PMXT data."""
    data_dir = Path(data_dir)
    files = sorted(data_dir.glob("*.parquet"))
    
    timestamps = []
    prices = []
    
    for fpath in files:
        pf = pq.ParquetFile(str(fpath))
        if pf.metadata.num_rows < 10000:
            continue
        
        for rg in range(pf.metadata.num_row_groups):
            try:
                table = pf.read_row_group(rg, columns=['market', 'asset_id', 'price', 'timestamp_received', 'side'])
                # Filter to our token
                mask = np.ones(len(table), dtype=bool)
                for i in range(len(table)):
                    m = table.column('market')[i].as_py()
                    a = table.column('asset_id')[i].as_py()
                    if m != market or a != asset_id:
                        mask[i] = False
                
                filtered = table.filter(mask)
                if len(filtered) == 0:
                    continue
                
                ts_col = filtered.column('timestamp_received').to_pylist()
                p_col = filtered.column('price').to_pylist()
                
                for ts, p in zip(ts_col, p_col):
                    if p is not None:
                        timestamps.append(ts)
                        prices.append(float(p))
            except:
                continue
    
    if len(timestamps) == 0:
        return np.array([]), np.array([])
    
    # Sort by timestamp
    order = np.argsort(timestamps)
    return np.array(timestamps)[order], np.array(prices)[order]


def compute_rsi(prices, period=14):
    n = len(prices)
    if n < period + 1:
        return np.full(n, 50.0)
    deltas = np.diff(prices, prepend=prices[0])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g = np.zeros(n); avg_l = np.zeros(n)
    avg_g[period] = np.mean(gains[1:period+1])
    avg_l[period] = np.mean(losses[1:period+1])
    for i in range(period+1, n):
        avg_g[i] = (avg_g[i-1]*(period-1) + gains[i]) / period
        avg_l[i] = (avg_l[i-1]*(period-1) + losses[i]) / period
    rs = np.where(avg_l > 0, avg_g / avg_l, 100.0)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi[:period] = 50.0
    return rsi


def run_backtest_v2(data_dir):
    """Simplified backtest: scan cheap tokens, apply V18.8 signals."""
    data_dir = Path(data_dir)
    files = sorted(data_dir.glob("*.parquet"))
    valid_files = []
    for f in files:
        try:
            pf = pq.ParquetFile(str(f))
            if pf.metadata.num_rows > 10000:
                valid_files.append(f)
        except Exception:
            continue
    print(f"Found {len(valid_files)} valid files")
    
    bankroll = INITIAL_BANKROLL
    total_trades = 0
    total_wins = 0
    tier_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0})
    token_results = []
    
    for fidx, fpath in enumerate(valid_files):
        print(f"\n[{fidx+1}/{len(valid_files)}] {fpath.name}...", flush=True)
        start_time = datetime.now()
        
        pf = pq.ParquetFile(str(fpath))
        
        # Read ALL data in chunks, group by (market, asset_id)
        market_data = defaultdict(list)  # (market, asset_id) -> [(timestamp, price, side)]
        
        for rg in range(min(pf.metadata.num_row_groups, 20)):  # Limit RGs for speed
            try:
                # Only read needed columns
                table = pf.read_row_group(rg, columns=['market', 'asset_id', 'price', 'timestamp_received'])
                n = len(table)
                
                # Batch process
                markets = table.column('market').to_pylist()
                assets = table.column('asset_id').to_pylist()
                prices = table.column('price').to_pylist()
                timestamps = table.column('timestamp_received').to_pylist()
                
                for i in range(n):
                    m = markets[i]
                    a = assets[i]
                    p = prices[i]
                    t = timestamps[i]
                    if p is not None and m is not None and a is not None:
                        # Only keep cheap-side tokens (price < 0.50)
                        p_float = float(p)
                        if p_float < 0.50:
                            market_data[(m, a)].append((t, p_float))
            except Exception as e:
                print(f"  RG {rg} error: {e}")
                continue
            
            if rg % 5 == 0:
                gc.collect()
        
        print(f"  Found {len(market_data)} cheap tokens", flush=True)
        
        # Process each token's price series
        file_trades = 0
        file_wins = 0
        
        for (m, a), data_points in market_data.items():
            if len(data_points) < 30:
                continue
            
            # Sort by timestamp
            data_points.sort(key=lambda x: x[0])
            prices = np.array([p for _, p in data_points])
            
            # Skip if price range is too narrow (not a real binary)
            if np.max(prices) - np.min(prices) < 0.05:
                continue
            
            # Determine resolution: final price
            final_price = prices[-1]
            token_wins = final_price > 0.5  # YES token wins if final > 0.5
            
            # Compute RSI on the token price series
            rsi = compute_rsi(prices, 14)
            
            # Generate signals
            n = len(prices)
            for i in range(20, n, 5):  # Sample every 5th point
                window = prices[:i]
                
                # Skip if price is too expensive for cheap-side entry
                price = prices[i]
                if price > 0.50 or price < 0.01:
                    continue
                
                # Use V18.8 engine
                signal = eng.generate_signal_v188(window, candles=None, idx=i-1)
                
                if signal.get('strategy', 'no_signal') == 'no_signal' or signal.get('signal', 'no_signal') == 'no_signal':
                    continue
                
                strategy = signal.get('strategy', 'unknown')
                confidence = signal.get('confidence', 0.0)
                direction = signal.get('direction', 'FLAT')
                
                if confidence < MIN_CONFIDENCE:
                    continue
                
                # Determine entry price and position size
                tier_name = strategy
                for k in TIER_SIZE:
                    if k.replace('_', '') in strategy.replace('_', '').lower():
                        tier_name = k
                        break
                else:
                    tier_name = 'direction_down_cheap' if 'down' in direction.lower() else 'direction_up_cheap'
                
                max_price = TIER_MAX_PRICE.get(tier_name, 0.12)
                tier_size = TIER_SIZE.get(tier_name, 0.03)
                
                if price > max_price:
                    continue
                
                # Execute trade
                position = tier_size * bankroll
                shares = position / price
                
                if token_wins:  # YES token resolves to $1
                    payout = shares * 1.0
                else:  # YES token resolves to $0
                    payout = 0
                
                pnl = payout - position
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
        
        elapsed = (datetime.now() - start_time).total_seconds()
        wr = file_wins / max(file_trades, 1) * 100
        print(f"  {file_trades} trades, {wr:.1f}% WR, bankroll=${bankroll:.2f} ({elapsed:.0f}s)")
        gc.collect()
    
    # Final report
    print("\n" + "=" * 70)
    print("V18.8 PMXT BACKTEST RESULTS (Cheap-Side Tokens)")
    print("=" * 70)
    print(f"Total trades: {total_trades}")
    print(f"Total wins: {total_wins}")
    print(f"Overall WR: {total_wins/max(total_trades,1)*100:.1f}%")
    print(f"Final bankroll: ${bankroll:.2f} (from ${INITIAL_BANKROLL:.0f})")
    if bankroll > 0:
        print(f"ROI: {(bankroll/INITIAL_BANKROLL - 1)*100:.1f}%")
    print()
    print("Per-tier breakdown:")
    for tier, stats in sorted(tier_stats.items()):
        if stats['trades'] > 0:
            wr = stats['wins'] / stats['trades'] * 100
            print(f"  {tier:30s}: {stats['trades']:5d} trades, {stats['wins']:5d} wins, {wr:.1f}% WR, ${stats['pnl']:.2f} P&L")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--dir', default='pmxt_data/')
    args = parser.parse_args()
    run_backtest_v2(args.dir)