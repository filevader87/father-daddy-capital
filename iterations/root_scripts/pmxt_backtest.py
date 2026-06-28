#!/usr/bin/env python3
"""
PMXT Historical Backtest Engine — V18.2 Signals vs Real Price Paths

Extracts BTC short-duration binary markets from pmxt archive Parquets,
replays actual orderbook price changes through V18.2 signal generation,
and measures真实的 historical WR vs MC-estimated 84.6%.

Usage:
  python3 pmxt_backtest.py --dir pmxt_data/ --out results/
"""

import os, sys, json, time, argparse
from datetime import datetime, timedelta
from pathlib import Path
import numpy as np
import pandas as pd

# ══════════════════════════════════════════════════════════
# V18.2 Signal Parameters (MUST match pm_engine_v18.py)
# ══════════════════════════════════════════════════════════
RSI_PERIOD = 14
RSI_OVERSOLD = 28
RSI_OVERBOUGHT = 72
SMA_WINDOW = 20
MIN_CONFIDENCE = 0.85
MAX_CONFIDENCE = 0.95

# ══════════════════════════════════════════════════════════
# Market Identification
# ══════════════════════════════════════════════════════════
# Short-duration BTC binary signatures:
# - 2 outcome tokens (UP + DOWN)
# - Price range spanning 0.01-0.99
# - Short duration (<2 hours)
# - High event frequency

def decode_market(raw_bytes):
    """Decode fixed_size_binary[66] market field to condition ID string."""
    if isinstance(raw_bytes, bytes):
        return raw_bytes.decode('ascii')
    return str(raw_bytes)


def identify_binary_markets(df, min_events=200):
    """
    Identify short-duration binary markets from a price_change DataFrame.
    Returns list of (condition_id, asset_up, asset_down) tuples.
    """
    if df.empty:
        return []

    df['cid'] = df['market'].apply(decode_market)

    # Group by market
    stats = df.groupby('cid').agg(
        events=('price', 'count'),
        unique_assets=('asset_id', 'nunique'),
        min_price=('price', 'min'),
        max_price=('price', 'max'),
        first_ts=('timestamp_received', 'min'),
        last_ts=('timestamp_received', 'max'),
    )

    stats['duration_min'] = (
        stats['last_ts'] - stats['first_ts']
    ).dt.total_seconds() / 60

    # Binary market filter: 2 assets, full price range, short duration, enough events
    binary = stats[
        (stats['unique_assets'] == 2) &
        (stats['min_price'] <= 0.10) &
        (stats['max_price'] >= 0.90) &
        (stats['duration_min'] <= 120) &
        (stats['events'] >= min_events)
    ].sort_values('events', ascending=False)

    results = []
    for cid, row in binary.iterrows():
        mkt_df = df[df['cid'] == cid]
        assets = mkt_df['asset_id'].unique()

        # Determine which asset is UP vs DOWN:
        # UP token has higher mean price (market thinks UP is more likely)
        # DOWN token has lower mean price (cheaper = less likely)
        asset_means = {}
        for aid in assets:
            asset_means[aid] = mkt_df[mkt_df['asset_id'] == aid]['price'].mean()

        if len(asset_means) == 2:
            sorted_assets = sorted(asset_means.items(), key=lambda x: x[1], reverse=True)
            asset_up = sorted_assets[0][0]    # Higher mean = UP
            asset_down = sorted_assets[1][1]  # Lower mean = DOWN
            results.append((cid, asset_up, asset_down, row['events'], row['duration_min']))

    return results


# ══════════════════════════════════════════════════════════
# RSI & Signal Computation (matching V18.2 exactly)
# ══════════════════════════════════════════════════════════

def compute_rsi(prices, period=RSI_PERIOD):
    """Compute RSI from a price series. Returns NaN for insufficient data."""
    if len(prices) < period + 1:
        return np.nan

    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def compute_sma(prices, window=SMA_WINDOW):
    """Compute simple moving average."""
    if len(prices) < window:
        return np.nan
    return np.mean(prices[-window:])


def generate_signal(rsi, sma_diff, macd_proxy, direction, conf):
    """
    Generate V18.2-style signal from computed indicators.
    Returns (action, confidence) or (None, 0) if no signal.
    """
    if np.isnan(rsi):
        return None, 0

    # RSI zones (matching V18.2)
    if rsi < 18:
        rsi_zone = "ultra_oversold"
        base_prob = 0.94
    elif rsi < 28:
        rsi_zone = "oversold"
        base_prob = 0.88
    elif rsi < 35:
        rsi_zone = "low"
        base_prob = 0.84
    elif rsi > 82:
        rsi_zone = "ultra_overbought"
        base_prob = 0.94
    elif rsi > 72:
        rsi_zone = "overbought"
        base_prob = 0.88
    elif rsi > 65:
        rsi_zone = "high"
        base_prob = 0.84
    else:
        # RSI 35-65: dead zone
        return None, 0

    # Direction determination
    if rsi < 35:
        suggested_dir = "up"    # Cheap side = UP
    elif rsi > 65:
        suggested_dir = "down"  # Cheap side = DOWN
    else:
        return None, 0

    # Confidence boost from confirmation indicators
    conf_bonus = 0
    if not np.isnan(sma_diff) and abs(sma_diff) > 0.005:
        conf_bonus += 0.02  # Price deviation from SMA confirms
    if not np.isnan(macd_proxy) and macd_proxy > 0:
        conf_bonus += 0.02  # Momentum confirms

    confidence = min(MAX_CONFIDENCE, base_prob + conf_bonus)

    if confidence < MIN_CONFIDENCE:
        return None, 0

    return suggested_dir, confidence


# ══════════════════════════════════════════════════════════
# Backtest Engine
# ══════════════════════════════════════════════════════════

def backtest_market(mkt_prices, asset_up, asset_down, bankroll=100, verbose=False):
    """
    Run V18.2 backtest on a single market's price history.

    Args:
        mkt_prices: DataFrame with [timestamp, asset_id, price] sorted by time
        asset_up: token ID for UP outcome
        asset_down: token ID for DOWN outcome
        bankroll: starting capital

    Returns:
        dict with trade results
    """
    # Build separate price series for UP and DOWN tokens
    up_prices = mkt_prices[mkt_prices['asset_id'] == asset_up].sort_values('timestamp_received').copy()
    down_prices = mkt_prices[mkt_prices['asset_id'] == asset_down].sort_values('timestamp_received').copy()

    # Normalize timestamps to tz-naive for comparison
    if up_prices['timestamp_received'].dt.tz is not None:
        up_prices['timestamp_received'] = up_prices['timestamp_received'].dt.tz_localize(None)
    if down_prices['timestamp_received'].dt.tz is not None:
        down_prices['timestamp_received'] = down_prices['timestamp_received'].dt.tz_localize(None)

    # ── Identify which token is "cheap" at each point in time ──
    # For BTC 5-min binaries:
    #   If current BTC price is rising → UP token is expensive, DOWN is cheap
    #   If current BTC price is falling → DOWN token is expensive, UP is cheap
    # But we don't have the underlying BTC price from this data.
    #
    # Simpler approach: at any given time, the CHEAP token is whichever is < 0.50
    # V18.2 targets cheap-side entries (0.05-0.15 range)
    # We compute RSI on the COMBINED price = (up_price + down_price)/2 anchored to 0.50
    # When combined < 0.50: market is bearish, we buy the cheap UP token
    # When combined > 0.50: market is bullish, we buy the cheap DOWN token
    #
    # Actually, the simplest approach: compute RSI and signals on the PRICE LEVEL
    # of each token independently. Enter a position when one token is cheap AND
    # RSI suggests it's oversold.

    # Use the cheaper token's price as our signal input
    # At each timestep, the "cheap side" = min(up_price, down_price)
    cheap_prices = []
    cheap_directions = []
    cheap_timestamps = []

    # Align price series by timestamp for combined analysis
    up_ts_idx = {ts: p for ts, p in zip(up_prices['timestamp_received'], up_prices['price'])}
    down_ts_idx = {ts: p for ts, p in zip(down_prices['timestamp_received'], down_prices['price'])}

    # Use down_prices timestamps as primary (our "signal price" series)
    for ts, down_p in zip(down_prices['timestamp_received'].values, down_prices['price'].values):
        # Find nearest UP price at this timestamp
        up_p = None
        if ts in up_ts_idx:
            up_p = float(up_ts_idx[ts])
        else:
            # Find closest UP price before this timestamp
            up_before = up_prices[up_prices['timestamp_received'] <= ts]
            if not up_before.empty:
                up_p = float(up_before['price'].iloc[-1])

        if up_p is None:
            continue

        down_p = float(down_p)
        # The cheap side is whichever token costs less
        # If DOWN is cheap → we might buy DOWN (expecting DOWN resolution)
        # If UP is cheap → we might buy UP (expecting UP resolution)
        cheap_p = min(up_p, down_p)
        cheap_dir = "down" if down_p < up_p else "up"

        cheap_prices.append(cheap_p)
        cheap_directions.append(cheap_dir)
        cheap_timestamps.append(ts)

    if len(cheap_prices) < RSI_PERIOD + 5:
        return None

    price_series = np.array(cheap_prices)
    direction_series = cheap_directions
    timestamps = np.array(cheap_timestamps)

    trades = []
    cap = bankroll
    peak = bankroll
    max_dd = 0
    n_trades = 0
    wins = 0
    position_open = False

    # Minimum bars between trades (cooldown)
    last_trade_idx = -999

    for i in range(RSI_PERIOD + 1, len(price_series)):
        if i >= len(direction_series):
            break

        # Compute RSI on rolling window of cheap-side prices
        window = price_series[max(0, i - RSI_PERIOD * 3):i + 1]
        rsi = compute_rsi(window, RSI_PERIOD)

        # Compute SMA deviation
        sma_window = price_series[max(0, i - SMA_WINDOW):i + 1]
        sma = compute_sma(sma_window, SMA_WINDOW)
        sma_diff = (price_series[i] - sma) / max(sma, 0.01) if not np.isnan(sma) else np.nan

        # MACD proxy: change in price over last 5 bars
        if i >= 5:
            macd_proxy = price_series[i] - price_series[i - 5]
        else:
            macd_proxy = np.nan

        # Current cheap side direction
        current_dir = direction_series[i]

        # Generate signal
        direction, confidence = generate_signal(rsi, sma_diff, macd_proxy, current_dir, 0)

        if direction is None or position_open or (i - last_trade_idx) < 5:
            continue

        # V18.2 thesis: only enter when cheap side price is in our target zone (≤ 0.15)
        entry_price = price_series[i]
        if entry_price > 0.20:
            continue  # Too expensive, skip — V18.2 longshot bias calibration

        # Determine which token to buy and get precise entry price
        if direction == "down":
            # Buy DOWN token (cheap)
            entry_token_prices = down_prices[down_prices['timestamp_received'] <= timestamps[i]]
            if entry_token_prices.empty:
                continue
            entry_price = float(entry_token_prices['price'].iloc[-1])
        else:
            # Buy UP token (cheap)
            entry_token_prices = up_prices[up_prices['timestamp_received'] <= timestamps[i]]
            if entry_token_prices.empty:
                continue
            entry_price = float(entry_token_prices['price'].iloc[-1])

        # Kelly sizing (12% of cap, capped)
        bet_frac = 0.12
        bet = round(min(cap * bet_frac, bankroll * 0.12) * 0.5, 2)
        bet = max(bet, 1.0)  # Min $1 bet
        bet = min(bet, cap * 0.50)  # Can't bet more than 50% of remaining

        if bet < 1.0 or cap < 5:
            continue

        # ── Resolution: determine if this trade won ──
        # For short-duration binaries, token prices converge to $1.00 (winner)
        # or $0.00 (loser) as the market resolves.
        # Look at the LAST known prices in our data window.

        last_up_price = float(up_prices['price'].iloc[-1])
        last_down_price = float(down_prices['price'].iloc[-1])

        # Clear resolution: one token > 0.90 means it resolved
        if last_up_price > 0.90 or last_down_price > 0.90:
            # Market resolved — winner is whichever token is > 0.90
            up_won = last_up_price > 0.90
            if direction == "up":
                won = up_won
            else:
                won = not up_won
        else:
            # Market didn't fully resolve in our data window.
            # Use price at a forward window (5-15 min ahead) as proxy.
            entry_ts = timestamps[i]
            future_up = up_prices[up_prices['timestamp_received'] > pd.Timestamp(entry_ts) + pd.Timedelta(minutes=5)]
            future_down = down_prices[down_prices['timestamp_received'] > entry_ts + pd.Timedelta(minutes=5)]

            if not future_up.empty and not future_down.empty:
                # Use price 5 min ahead as resolution proxy
                fut_up_p = float(future_up['price'].iloc[0])
                fut_down_p = float(future_down['price'].iloc[0])

                if direction == "up":
                    won = fut_up_p > entry_price
                else:
                    won = fut_down_p > entry_price
            else:
                # No forward data — skip this trade
                continue

        # Calculate P&L
        if won:
            pnl = bet * (1.0 - entry_price) / max(entry_price, 0.01)
            wins += 1
        else:
            pnl = -bet

        cap += pnl
        peak = max(peak, cap)
        dd = (peak - cap) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

        trades.append({
            'idx': i,
            'direction': direction,
            'confidence': round(confidence, 3),
            'entry_price': round(entry_price, 4),
            'bet': round(bet, 2),
            'won': won,
            'pnl': round(pnl, 2),
            'cap': round(cap, 2),
            'rsi': round(rsi, 1) if not np.isnan(rsi) else None,
        })

        n_trades += 1
        last_trade_idx = i
        position_open = False

        # Kill switch: stop if capital too low
        if cap < 5:
            break

    if n_trades == 0:
        return None

    wr = wins / n_trades * 100
    total_pnl = cap - bankroll

    return {
        'n_trades': n_trades,
        'wins': wins,
        'win_rate': round(wr, 1),
        'pnl': round(total_pnl, 2),
        'final_cap': round(cap, 2),
        'max_dd': round(max_dd * 100, 1),
        'trades': trades,
    }


# ══════════════════════════════════════════════════════════
# Main Pipeline
# ══════════════════════════════════════════════════════════

def process_parquet(filepath):
    """
    Process a single hourly Parquet file.
    Two-pass approach:
      Pass 1: Scan row groups to identify binary market CIDs (lightweight — only key cols)
      Pass 2: Extract full price data only for identified CIDs
    """
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(filepath)
    fname = os.path.basename(filepath)
    binary_cids = set()

    # ── PASS 1: Identify binary market CIDs ──
    for rg_idx in range(min(pf.num_row_groups, 10)):  # Sample first 10 RGs
        try:
            t = pf.read_row_group(rg_idx, columns=[
                'market', 'asset_id', 'price', 'timestamp_received', 'event_type'
            ])
            df = t.to_pandas()
            pc = df[df['event_type'] == 'price_change'].copy()

            if pc.empty:
                continue

            pc['cid'] = pc['market'].apply(decode_market)

            # Quick binary filter
            stats = pc.groupby('cid').agg(
                events=('price', 'count'),
                unique_assets=('asset_id', 'nunique'),
                min_price=('price', 'min'),
                max_price=('price', 'max'),
            )

            binaries = stats[
                (stats['unique_assets'] == 2) &
                (stats['min_price'] <= 0.10) &
                (stats['max_price'] >= 0.90) &
                (stats['events'] >= 100)
            ]
            binary_cids.update(binaries.index.tolist())

            del df, pc
        except Exception as e:
            continue

    if not binary_cids:
        return [], {}

    # ── PASS 2: Extract full data for identified markets ──
    market_data = {}
    for rg_idx in range(pf.num_row_groups):
        try:
            # Only read the columns we need, filter by market
            t = pf.read_row_group(rg_idx, columns=[
                'market', 'asset_id', 'price', 'timestamp_received', 'event_type'
            ])
            df = t.to_pandas()
            pc = df[df['event_type'] == 'price_change'].copy()
            pc['cid'] = pc['market'].apply(decode_market)

            # Filter to only our binary markets
            pc = pc[pc['cid'].isin(binary_cids)]

            if pc.empty:
                del df, pc
                continue

            for cid in pc['cid'].unique():
                mkt_df = pc[pc['cid'] == cid][['asset_id', 'price', 'timestamp_received']]
                assets = mkt_df['asset_id'].unique()

                if len(assets) != 2:
                    continue

                # Determine UP/DOWN by mean price
                asset_means = {}
                for aid in assets:
                    asset_means[aid] = mkt_df[mkt_df['asset_id'] == aid]['price'].mean()

                sorted_assets = sorted(asset_means.items(), key=lambda x: x[1], reverse=True)
                asset_up = sorted_assets[0][0]
                asset_down = sorted_assets[1][0]

                if cid not in market_data:
                    market_data[cid] = {
                        'asset_up': asset_up,
                        'asset_down': asset_down,
                        'prices': mkt_df,
                    }
                else:
                    market_data[cid]['prices'] = pd.concat([
                        market_data[cid]['prices'],
                        mkt_df
                    ], ignore_index=True)

            del df, pc
        except Exception as e:
            continue

    binaries = [(cid, d['asset_up'], d['asset_down'],
                 len(d['prices']), 0)
                for cid, d in market_data.items()]

    return binaries, market_data


def run_backtest(data_dir, out_dir):
    """
    Main backtest pipeline: process all Parquets, extract markets, run V18.2 backtest.
    """
    data_dir = Path(data_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    parquet_files = sorted(data_dir.glob('polymarket_orderbook_*.parquet'))
    print(f"Found {len(parquet_files)} Parquet files")

    all_markets = {}  # cid -> market data (accumulated across hours)
    total_binaries = 0

    for i, pf_path in enumerate(parquet_files):
        hour = pf_path.stem.split('T')[-1] if 'T' in pf_path.stem else str(i)
        print(f"\n[{i+1}/{len(parquet_files)}] Processing hour T{hour}...")

        binaries, market_data = process_parquet(pf_path)
        total_binaries += len(binaries)
        print(f"  Found {len(binaries)} binary markets (total: {total_binaries})")

        # Merge into accumulated data
        for cid, data in market_data.items():
            if cid in all_markets:
                all_markets[cid]['prices'] = pd.concat([
                    all_markets[cid]['prices'],
                    data['prices']
                ], ignore_index=True).sort_values('timestamp_received')
            else:
                all_markets[cid] = data

    print(f"\n{'='*60}")
    print(f"TOTAL UNIQUE BINARY MARKETS: {len(all_markets)}")
    print(f"TOTAL BINARY IDENTIFICATIONS: {total_binaries}")
    print(f"{'='*60}")

    # Run backtest on each market
    results = []
    trade_journal = []

    print(f"\nRunning V18.2 backtest on {len(all_markets)} markets...")

    for cid, data in all_markets.items():
        prices = data['prices'].sort_values('timestamp_received')

        if len(prices) < RSI_PERIOD + 10:
            continue

        result = backtest_market(
            prices,
            data['asset_up'],
            data['asset_down'],
            bankroll=100,
            verbose=False
        )

        if result is not None:
            result['cid'] = cid[:20]
            result['asset_up'] = data['asset_up'][:20]
            result['asset_down'] = data['asset_down'][:20]
            results.append(result)
            trade_journal.extend(result['trades'])

    # Aggregate
    if not results:
        print("\nNo tradeable markets found in the data.")
        return

    total_trades = sum(r['n_trades'] for r in results)
    total_wins = sum(r['wins'] for r in results)
    total_pnl = sum(r['pnl'] for r in results)
    avg_wr = total_wins / total_trades * 100 if total_trades > 0 else 0

    # Qualified WR (markets with ≥5 trades)
    qualified = [r for r in results if r['n_trades'] >= 5]
    qual_trades = sum(r['n_trades'] for r in qualified)
    qual_wins = sum(r['wins'] for r in qualified)
    qual_wr = qual_wins / qual_trades * 100 if qual_trades > 0 else 0

    # RSI zone breakdown
    rsi_zones = {}
    for t in trade_journal:
        rsi = t.get('rsi')
        if rsi is None:
            continue
        if rsi < 18:
            zone = "ultra_low"
        elif rsi < 28:
            zone = "low"
        elif rsi < 35:
            zone = "mid_low"
        elif rsi > 82:
            zone = "ultra_high"
        elif rsi > 72:
            zone = "high"
        elif rsi > 65:
            zone = "mid_high"
        else:
            zone = "dead_zone"

        if zone not in rsi_zones:
            rsi_zones[zone] = {'wins': 0, 'trades': 0}
        rsi_zones[zone]['trades'] += 1
        if t['won']:
            rsi_zones[zone]['wins'] += 1

    # Price zone breakdown (entry price zones matching V18.2 cheap-side thesis)
    price_zones = {}
    for t in trade_journal:
        ep = t.get('entry_price', 0)
        if ep <= 0.05:
            zone = "ultra_cheap"
        elif ep <= 0.15:
            zone = "cheap"
        elif ep <= 0.30:
            zone = "moderate"
        elif ep <= 0.50:
            zone = "expensive"
        else:
            zone = "very_expensive"

        if zone not in price_zones:
            price_zones[zone] = {'wins': 0, 'trades': 0}
        price_zones[zone]['trades'] += 1
        if t['won']:
            price_zones[zone]['wins'] += 1

    # Print results
    print(f"\n{'='*60}")
    print(f"PMXT HISTORICAL BACKTEST RESULTS — V18.2 SIGNALS")
    print(f"{'='*60}")
    print(f"  Markets analyzed: {len(all_markets)}")
    print(f"  Markets with trades: {len(results)}")
    print(f"  Qualified markets (≥5 trades): {len(qualified)}")
    print(f"")
    print(f"  Total trades: {total_trades}")
    print(f"  Total wins: {total_wins}")
    print(f"  Win Rate: {avg_wr:.1f}%")
    print(f"  Qualified WR: {qual_wr:.1f}% (≥5 trades)")
    print(f"  Total P&L: ${total_pnl:+.2f}")
    print(f"  Avg P&L/market: ${total_pnl/max(len(results),1):+.2f}")
    print(f"")
    print(f"  RSI Zone Breakdown:")
    for zone in ['ultra_low', 'low', 'mid_low', 'dead_zone', 'mid_high', 'high', 'ultra_high']:
        if zone in rsi_zones:
            z = rsi_zones[zone]
            wr = z['wins'] / z['trades'] * 100 if z['trades'] > 0 else 0
            print(f"    {zone:15s}: {z['wins']}/{z['trades']} ({wr:.0f}%)")

    print(f"")
    print(f"  Entry Price Zone Breakdown:")
    for zone in ['ultra_cheap', 'cheap', 'moderate', 'expensive', 'very_expensive']:
        if zone in price_zones:
            z = price_zones[zone]
            wr = z['wins'] / z['trades'] * 100 if z['trades'] > 0 else 0
            print(f"    {zone:15s}: {z['wins']}/{z['trades']} ({wr:.0f}%)")

    print(f"")
    print(f"  MC ESTIMATE (hard-mode): 84.6% avg, 90.7% qualified")
    print(f"  HISTORICAL BACKTEST:       {avg_wr:.1f}% avg, {qual_wr:.1f}% qualified")
    print(f"  DELTA:                     {avg_wr - 84.6:+.1f}% avg, {qual_wr - 90.7:+.1f}% qualified")
    print(f"{'='*60}")

    # Save detailed results
    summary = {
        'timestamp': datetime.utcnow().isoformat(),
        'markets_total': len(all_markets),
        'markets_with_trades': len(results),
        'markets_qualified': len(qualified),
        'total_trades': total_trades,
        'total_wins': total_wins,
        'win_rate': round(avg_wr, 1),
        'qualified_wr': round(qual_wr, 1),
        'total_pnl': round(total_pnl, 2),
        'rsi_zones': {k: {'wins': v['wins'], 'trades': v['trades'],
                         'wr': round(v['wins']/v['trades']*100, 1) if v['trades'] > 0 else 0}
                     for k, v in rsi_zones.items()},
        'price_zones': {k: {'wins': v['wins'], 'trades': v['trades'],
                           'wr': round(v['wins']/v['trades']*100, 1) if v['trades'] > 0 else 0}
                       for k, v in price_zones.items()},
        'mc_comparison': {
            'mc_avg_wr': 84.6,
            'mc_qualified_wr': 90.7,
            'delta_avg': round(avg_wr - 84.6, 1),
            'delta_qualified': round(qual_wr - 90.7, 1),
        },
        'per_market_results': [{
            'cid': r['cid'],
            'trades': r['n_trades'],
            'wins': r['wins'],
            'wr': r['win_rate'],
            'pnl': r['pnl'],
        } for r in sorted(results, key=lambda x: -x['n_trades'])]
    }

    with open(out_dir / 'backtest_results.json', 'w') as f:
        json.dump(summary, f, indent=2)

    print(f"\nResults saved to {out_dir / 'backtest_results.json'}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='PMXT Historical Backtest')
    parser.add_argument('--dir', default='pmxt_data/', help='Directory with pmxt Parquets')
    parser.add_argument('--out', default='backtest_results/', help='Output directory')
    args = parser.parse_args()

    run_backtest(args.dir, args.out)