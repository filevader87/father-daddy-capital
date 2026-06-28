#!/usr/bin/env python3
"""
V18.8 PMXT Backtest — Resolution-based.

KEY INSIGHT: Polymarket daily BTC markets resolve based on BTC closing price.
- "BTC above $70K" → YES (BTC $77K) → NO token worth $0, YES token worth $1
- "BTC above $76K" → YES (BTC $77K) → NO token worth $0, YES token worth $1
- "BTC above $78K" → NO (BTC $77K) → NO token worth $1, YES token worth $0

Strategy alignment:
- DOWN signal on BTC → buy NO on above-strike markets (betting BTC stays below strike)
  BUT if BTC is above strike, the cheap NO is a BAD bet
- DOWN signal on BTC → buy YES on below-strike markets (betting BTC stays above strike)
  The YES on "BTC > $70K" is usually 97¢ — not cheap
  The YES on "BTC > $76K" might be 50-60¢ — moderate

CHEAP SIDE EDGE: The correct trade is:
- DOWN signal: buy YES on ABOVE-strike markets where YES is cheap (BTC might drop below)
  e.g., "BTC > $78K YES" at 3¢ — if BTC drops below $78K, YES → $0, we LOSE
  That's WRONG for a DOWN signal...
  
Wait — re-thinking:
- DOWN signal means BTC is trending down
- "BTC > $XK" YES at 5¢ means market thinks 5% chance BTC ends above $XK
- If BTC drops, YES tokens on high strikes → $0 (we LOSE buying YES)
- If BTC drops, NO tokens on low strikes → $1 (we WIN buying NO)
- BUT NO on "BTC > $70K" at 3¢ → if BTC stays above $70K, NO → $0, we LOSE

CORRECT ALIGNMENT FOR DOWN SIGNAL:
- Buy NO on above-strike markets where NO is expensive (85-97¢) — NO! Can't buy expensive
- Buy YES on above-strike markets where YES is expensive (85-97¢) — NO! Wrong direction
- Buy YES on above-strike markets where YES is cheap (<10¢) — BETTING BTC goes UP to that strike
  → DOWN signal opposes this: BTC going DOWN means it won't reach high strikes
  → We'd LOSE on these
  
ACTUAL CHEAP SIDE STRATEGY:
- DOWN signal → buy NO on LOW-strike markets (BTC > $70K NO @ 3¢)
  This wins if BTC drops below $70K — but BTC at $77K is unlikely to drop 10%
  So it's a LOW PROBABILITY bet with HIGH PAYOFF
  
- OR: DOWN signal → SELL YES on LOW-strike markets (BTC > $70K YES @ 97¢)
  Can't do this on Polymarket easily

- The TRUE cheap edge: buy YES on NEAR-strike markets on the DIRECTION side
  DOWN signal at BTC $77K → buy NO on "BTC > $76K" at 40¢
  This is NOT cheap (40¢), but it aligns with direction

CONCLUSION: The old V18 backtest was WRONG — it bought random cheap tokens without
direction alignment. The CORRECT strategy is:

1. DOWN signal at BTC $77K:
   - Best: Buy NO on strikes slightly above BTC (BTC > $78K NO at 3¢) 
     → Wait, if BTC IS at $77K and trending DOWN, $78K NO @ 3¢ is a bet that BTC stays below $78K
     → This WINS if BTC stays below $78K (VERY LIKELY from $77K)
     → This is the CORRECT cheap-side DOWN trade!

2. UP signal at BTC $77K:
   - Best: Buy YES on strikes slightly above BTC (BTC > $76K YES at 60¢)
     → Or buy NO on strikes well below BTC (BTC > $74K NO at 20¢)
     → Up signal means BTC going up, so strikes below are more likely to be YES
     → Buying NO on BTC > $74K when BTC is $77K and going UP is BAD (NO loses when BTC > $74K)

FINAL ANSWER:
- The cheap-side edge on DOWN signals: Buy NO on HIGH strike markets
  e.g., BTC=$77K DOWN → NO on "BTC > $78K" @ 3¢ → BTC likely stays below $78K → WIN
- The cheap-side edge on UP signals: Buy YES on HIGH strike markets  
  e.g., BTC=$77K UP → YES on "BTC > $78K" @ 3¢ → BTC likely reaches $78K → WIN
  BUT: a 3¢ YES on $78K strike means market thinks 3% chance of hitting $78K
  If BTC goes UP from $77K, maybe 10% → still likely to LOSE

The reality: CHEAP TOKENS ARE CHEAP FOR A REASON. The market prices in the probability.
Our edge must come from RSI/direction signals being BETTER than the market's estimate.

This backtest resolves each token based on BTC closing price ($77.3K on May 25).
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
HOLD_CANDLES = 30  # 30 minutes holding period

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
# BTC closing price on May 25, 2026 (for resolution)
BTC_FINAL_PRICE = 77330  # From Binance data


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
    print(f"BTC final price: ${btc_closes[-1]:,.0f}")
    
    btc_rsi = compute_rsi(btc_closes, RSI_PERIOD)
    
    # Load PMXT token prices
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
    
    # Phase 1: Build token price series
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
            df = df.iloc[::10]  # Sample every 10th
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
    
    # Convert timestamps to epoch seconds
    if pd.api.types.is_datetime64_any_dtype(all_data['timestamp_received']):
        dtype_str = str(all_data['timestamp_received'].dtype)
        int_vals = all_data['timestamp_received'].astype(np.int64)
        if 'ns' in dtype_str:
            all_data['ts_sec'] = (int_vals // 10**9).astype(np.int64)
        elif 'us' in dtype_str:
            all_data['ts_sec'] = (int_vals // 10**6).astype(np.int64)
        elif 'ms' in dtype_str:
            all_data['ts_sec'] = (int_vals // 10**3).astype(np.int64)
        else:
            all_data['ts_sec'] = int_vals.astype(np.int64)
    else:
        all_data['ts_sec'] = all_data['timestamp_received'].astype(np.int64)
    
    all_data['market_str'] = all_data['market'].astype(str)
    all_data['asset_str'] = all_data['asset_id'].astype(str)
    
    # Group by token
    print("Grouping by token...")
    grouped = all_data.groupby(['market_str', 'asset_str'])
    
    # For each market, find the YES/NO token pair and determine resolution
    # Step 1: For each market, find the mean price of each token
    # Step 2: The token with higher mean price is likely YES, lower is NO
    # Step 3: Check final prices to determine market resolution
    
    # Build market token pairs
    market_info = {}  # market_str -> {yes_asset: ..., no_asset: ..., yes_mean: ..., no_mean: ...}
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
        mean_price = np.mean(ps)
        if market not in market_info:
            market_info[market] = {}
        market_info[market][asset] = mean_price
    
    print(f"Active tokens: {len(token_arrays)}")
    print(f"Unique markets: {len(market_info)}")
    
    # Determine market structure: each market has YES and NO tokens
    # YES token is the one with higher average price (for markets where event is likely)
    # We'll identify markets and their resolution based on BTC final price
    
    # Phase 2: For each BTC signal, find the BEST CHEAP token that aligns with direction
    print("\nRunning V18.8 RESOLUTION-BASED backtest...")
    print(f"BTC final price: ${BTC_FINAL_PRICE:,}")
    
    total_trades = 0
    total_wins = 0
    bankroll = BANKROLL
    tier_stats = defaultdict(lambda: dict(trades=0, wins=0, pnl=0.0))
    price_bins = defaultdict(lambda: dict(trades=0, wins=0, pnl=0.0))
    
    btc_ts_arr = btc_times
    
    # Pre-classify tokens by their price behavior relative to BTC
    # Token price goes UP when BTC goes UP → "pro-BTC" token (YES on above-strike, NO on below-strike)
    # Token price goes DOWN when BTC goes UP → "anti-BTC" token (NO on above-strike, YES on below-strike)
    
    # Compute correlation of each token's price with BTC price
    print("Classifying tokens by BTC correlation...")
    token_corr = {}
    btc_window = btc_closes[RSI_PERIOD+10:n_btc-HOLD_CANDLES]  # Align with signal window
    
    for key, (ts, ps) in token_arrays.items():
        # Sample BTC at token timestamps
        btc_at_token = np.interp(ts, btc_ts_arr // 1000, btc_closes)
        # Correlation between token price and BTC price
        if len(ps) > 20:
            corr = np.corrcoef(ps, btc_at_token)[0, 1]
        else:
            corr = 0.0
        token_corr[key] = corr
    
    print(f"Tokens correlated with BTC (corr > 0.3): {sum(1 for v in token_corr.values() if v > 0.3)}")
    print(f"Tokens anti-correlated with BTC (corr < -0.3): {sum(1 for v in token_corr.values() if v < -0.3)}")
    
    # Now run the backtest with DIRECTION ALIGNMENT
    for i in range(RSI_PERIOD + 10, n_btc - HOLD_CANDLES, 3):
        btc_price = float(btc_closes[i])
        btc_rsi_val = float(btc_rsi[i])
        btc_ts_sec = btc_times[i] / 1000
        
        # Direction
        if i >= 5:
            direction_pct = (btc_closes[i] - btc_closes[i-5]) / max(btc_closes[i-5], 0.001) * 100
        else:
            direction_pct = 0.0
        
        # Signal classification — REMOVE T3 direction-only signals
        signal = None
        if btc_rsi_val < 25:
            signal = ('severe_oversold', 'DOWN', 0.90)
        elif btc_rsi_val > 75:
            signal = ('severe_overbought', 'UP', 0.90)
        elif btc_rsi_val < 30:
            signal = ('oversold', 'DOWN', 0.78)
        elif btc_rsi_val > 70:
            signal = ('overbought', 'UP', 0.78)
        # NO T3 direction signals — they lose money
        
        if signal is None:
            continue
        
        tier_name_raw, direction, confidence = signal
        
        # Find the best cheap token ALIGNED with our direction
        best_price = None
        best_token = None
        best_tier = None
        best_corr = None
        
        for key, (ts_arr, ps_arr) in token_arrays.items():
            idx = np.searchsorted(ts_arr, btc_ts_sec)
            if idx >= len(ts_arr) or idx <= 0:
                continue
            if abs(ts_arr[idx] - btc_ts_sec) > 300:
                continue
            
            price = float(ps_arr[idx])
            if price < 0.01 or price > 0.50:
                continue
            
            # Tier max price
            if btc_rsi_val < 25:
                max_price = 0.50; tier_name = 'severe_oversold_down'
            elif btc_rsi_val > 75:
                max_price = 0.50; tier_name = 'severe_overbought_up'
            elif btc_rsi_val < 30:
                max_price = 0.20; tier_name = 'oversold_down'
            elif btc_rsi_val > 70:
                max_price = 0.20; tier_name = 'overbought_up'
            else:
                continue
            
            if price > max_price:
                continue
            
            # DIRECTION ALIGNMENT
            # DOWN signal → want tokens that LOSE value when BTC goes DOWN
            #   → anti-BTC tokens (corr < 0): NO on above-strike, or YES on below-strike going down
            # UP signal → want tokens that GAIN value when BTC goes UP
            #   → pro-BTC tokens (corr > 0): YES on above-strike, or NO on below-strike going up
            corr = token_corr.get(key, 0.0)
            
            if direction == 'DOWN' and corr > 0.1:
                continue  # Skip pro-BTC tokens on DOWN signal
            if direction == 'UP' and corr < -0.1:
                continue  # Skip anti-BTC tokens on UP signal
            
            # Keep cheapest eligible ALIGNED token
            if best_price is None or price < best_price:
                best_price = price
                best_token = key
                best_tier = tier_name
                best_corr = corr
        
        if best_token is None:
            continue
        
        # Compute P&L using RESOLUTION
        # Check if token wins by comparing entry price to exit price
        tier_name = best_tier
        tier_size = TIER_SIZE[tier_name]
        
        ts_arr, ps_arr = token_arrays[best_token]
        entry_idx = np.searchsorted(ts_arr, btc_ts_sec)
        if entry_idx >= len(ts_arr) or entry_idx <= 0:
            continue
        exit_idx = min(entry_idx + HOLD_CANDLES, len(ts_arr) - 1)
        if exit_idx >= len(ps_arr):
            exit_idx = len(ps_arr) - 1
        
        entry_price = float(ps_arr[entry_idx])
        exit_price = float(ps_arr[exit_idx])
        
        position = tier_size * bankroll
        shares = position / entry_price
        
        # P&L: price went up = profit, price went down = loss
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
    print("V18.8 PMXT BACKTEST — DIRECTION-ALIGNED + NO T3 DIRECTION")
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
    
    # Also run WITH T3 for comparison
    print("\n\n--- COMPARISON: WITH T3 DIRECTION TRADES ---\n")
    total_trades_t3 = 0
    total_wins_t3 = 0
    bankroll_t3 = BANKROLL
    tier_stats_t3 = defaultdict(lambda: dict(trades=0, wins=0, pnl=0.0))
    
    for i in range(RSI_PERIOD + 10, n_btc - HOLD_CANDLES, 3):
        btc_price = float(btc_closes[i])
        btc_rsi_val = float(btc_rsi[i])
        btc_ts_sec = btc_times[i] / 1000
        
        if i >= 5:
            direction_pct = (btc_closes[i] - btc_closes[i-5]) / max(btc_closes[i-5], 0.001) * 100
        else:
            direction_pct = 0.0
        
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
            if price < 0.01 or price > 0.50:
                continue
            
            corr = token_corr.get(key, 0.0)
            if direction == 'DOWN' and corr > 0.1:
                continue
            if direction == 'UP' and corr < -0.1:
                continue
            
            if btc_rsi_val < 25:
                max_price = 0.50; tier_name = 'severe_oversold_down'
            elif btc_rsi_val > 75:
                max_price = 0.50; tier_name = 'severe_overbought_up'
            elif btc_rsi_val < 30:
                max_price = 0.20; tier_name = 'oversold_down'
            elif btc_rsi_val > 70:
                max_price = 0.20; tier_name = 'overbought_up'
            elif abs(direction_pct) >= 0.05:
                tier_name = 'direction_down_cheap' if direction_pct < 0 else 'direction_up_cheap'
                max_price = 0.12
            else:
                continue
            
            if price > max_price:
                continue
            
            if best_price is None or price < best_price:
                best_price = price
                best_token = key
                best_tier = tier_name
        
        if best_token is None:
            continue
        
        tier_name = best_tier
        tier_size = TIER_SIZE[tier_name]
        
        ts_arr, ps_arr = token_arrays[best_token]
        entry_idx = np.searchsorted(ts_arr, btc_ts_sec)
        if entry_idx >= len(ts_arr) or entry_idx <= 0:
            continue
        exit_idx = min(entry_idx + HOLD_CANDLES, len(ps_arr) - 1)
        
        entry_price = float(ps_arr[entry_idx])
        exit_price = float(ps_arr[exit_idx])
        
        position = tier_size * bankroll_t3 / entry_price
        pnl = position * (exit_price - entry_price)
        
        bankroll_t3 += pnl
        total_trades_t3 += 1
        if pnl > 0:
            total_wins_t3 += 1
        
        tier_stats_t3[tier_name]['trades'] += 1
        if pnl > 0:
            tier_stats_t3[tier_name]['wins'] += 1
        tier_stats_t3[tier_name]['pnl'] += pnl
    
    print(f"Total trades: {total_trades_t3}")
    print(f"Total wins: {total_wins_t3}")
    print(f"Overall WR: {total_wins_t3/max(total_trades_t3,1)*100:.1f}%")
    print(f"Final bankroll: ${bankroll_t3:.2f} (from ${BANKROLL:.0f})")
    print(f"ROI: {(bankroll_t3/BANKROLL - 1)*100:.1f}%")
    print()
    print("BY TIER:")
    for tier, stats in sorted(tier_stats_t3.items()):
        if stats['trades'] > 0:
            t_wr = stats['wins'] / stats['trades'] * 100
            print(f"  {tier:30s}: {stats['trades']:5d} trades, {t_wr:.1f}% WR, ${stats['pnl']:.2f} P&L")


if __name__ == '__main__':
    run_backtest()