#!/usr/bin/env python3
"""
PMXT Historical Backtest Engine v2 — V18.2 Signals vs Real Price Paths

Replays actual CLOB orderbook data through V18.2's signal logic
and measures historical WR against MC-estimated 84.6%.

Key insight: For BTC short-duration binaries, each market has 2 tokens
(YES/NO). The cheap token (≤15¢) is our entry target. We compute RSI
on the cheap token's OWN price series — low RSI = oversold = buy signal.

Usage:
  python3 pmxt_backtest_v2.py --dir pmxt_data/ --out backtest_results/
"""

import os, sys, json, gc, argparse
from datetime import datetime
from pathlib import Path
import numpy as np
import pandas as pd

# ══════════════════════════════════════════════════════════
# V18.2 Parameters
# ══════════════════════════════════════════════════════════
RSI_PERIOD = 14
MIN_CONFIDENCE = 0.85
CHEAP_SIDE_MAX = 0.20   # Only buy tokens priced ≤ 20¢
BET_FRAC = 0.12         # 12% of cap per trade

# ══════════════════════════════════════════════════════════
# RSI Computation
# ══════════════════════════════════════════════════════════
def compute_rsi_series(prices, period=RSI_PERIOD):
    """Vectorized RSI computation. Returns full series (NaN for warmup)."""
    deltas = np.diff(prices, prepend=prices[0])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    # Wilder smoothing
    avg_gain = np.zeros_like(prices)
    avg_loss = np.zeros_like(prices)
    avg_gain[period] = np.mean(gains[1:period+1])
    avg_loss[period] = np.mean(losses[1:period+1])

    for i in range(period + 1, len(prices)):
        avg_gain[i] = (avg_gain[i-1] * (period - 1) + gains[i]) / period
        avg_loss[i] = (avg_loss[i-1] * (period - 1) + losses[i]) / period

    rs = np.where(avg_loss > 0, avg_gain / avg_loss, 100.0)
    rsi = np.where(avg_loss > 0, 100 - 100 / (1 + rs), 100.0)
    rsi[:period] = np.nan
    return rsi


def classify_signal(rsi_val, entry_price):
    """
    V18.2 signal classification from RSI + entry price.
    Returns (direction, confidence) or (None, 0).
    """
    if np.isnan(rsi_val):
        return None, 0

    # RSI zone → base confidence (matching V18.2 thresholds)
    if rsi_val < 18:
        base = 0.94
    elif rsi_val < 28:
        base = 0.88
    elif rsi_val < 35:
        base = 0.84
    elif rsi_val > 82:
        base = 0.94
    elif rsi_val > 72:
        base = 0.88
    elif rsi_val > 65:
        base = 0.84
    else:
        # Dead zone RSI 35-65
        return None, 0

    confidence = min(0.95, base)

    if confidence < MIN_CONFIDENCE:
        return None, 0

    # Direction: if we're buying a cheap token, RSI<35 means oversold = good entry
    # RSI>65 on the same token means it's running hot = skip
    # For cheap-side: low RSI → token is being sold off → potential reversal → BUY
    #                  high RSI → token is running up → not cheap anymore → might SELL
    # V18.2 thesis: buy cheap tokens when RSI is in extreme zones
    #              (extreme oversold = potential bounce, extreme overbought = momentum)

    direction = "buy"  # We buy the cheap token
    return direction, confidence


# ══════════════════════════════════════════════════════════
# Market Extraction
# ══════════════════════════════════════════════════════════
def decode_market(raw_bytes):
    """Decode fixed_size_binary[66] market field."""
    if isinstance(raw_bytes, bytes):
        return raw_bytes.decode('ascii')
    return str(raw_bytes)


def extract_binaries_from_parquet(filepath):
    """
    Two-pass extraction of binary markets from a Parquet file.
    """
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(filepath)
    binary_cids = set()

    # PASS 1: Identify binary market CIDs (sample first 8 row groups)
    for rg in range(min(pf.num_row_groups, 8)):
        t = pf.read_row_group(rg, columns=['market', 'asset_id', 'price', 'event_type'])
        df = t.to_pandas()
        pc = df[df['event_type'] == 'price_change'].copy()
        if pc.empty:
            del df, pc
            continue

        pc['cid'] = pc['market'].apply(decode_market)
        stats = pc.groupby('cid').agg(
            events=('price', 'count'),
            n_assets=('asset_id', 'nunique'),
            pmin=('price', 'min'),
            pmax=('price', 'max'),
        )
        bins = stats[(stats['n_assets'] == 2) & (stats['pmin'] <= 0.10) &
                     (stats['pmax'] >= 0.90) & (stats['events'] >= 50)]
        binary_cids.update(bins.index.tolist())
        del df, pc

    if not binary_cids:
        return {}

    # PASS 2: Extract price data for identified markets
    market_data = {}
    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg, columns=['market', 'asset_id', 'price',
                                            'timestamp_received', 'event_type'])
        df = t.to_pandas()
        pc = df[df['event_type'] == 'price_change'].copy()
        pc['cid'] = pc['market'].apply(decode_market)
        pc = pc[pc['cid'].isin(binary_cids)]

        if pc.empty:
            del df, pc
            continue

        for cid, grp in pc.groupby('cid'):
            assets = grp['asset_id'].unique()
            if len(assets) != 2:
                continue

            # Identify cheap vs expensive token by last known price
            # (in resolved markets, the winner → $1.00, loser → $0.00)
            last_prices = {}
            for aid in assets:
                aid_df = grp[grp['asset_id'] == aid]
                last_prices[aid] = float(aid_df['price'].iloc[-1])

            # The token with LOWER last price is the LOSER (resolved to $0)
            # During the market's life, the cheap token is whichever is < $0.50
            sorted_a = sorted(last_prices.items(), key=lambda x: x[1])
            cheap_asset = sorted_a[0][0]  # Lower final price = the losing/cheap side
            rich_asset = sorted_a[1][0]   # Higher final price = the winning side

            # Only keep markets where the cheap side was actually cheap at some point
            cheap_prices = grp[grp['asset_id'] == cheap_asset]['price']
            if cheap_prices.min() > 0.15:
                continue  # Never got cheap enough for V18.2

            if cid not in market_data:
                market_data[cid] = {
                    'cheap_asset': cheap_asset,
                    'rich_asset': rich_asset,
                    'prices': grp[['asset_id', 'price', 'timestamp_received']].copy(),
                }
            else:
                market_data[cid]['prices'] = pd.concat([
                    market_data[cid]['prices'],
                    grp[['asset_id', 'price', 'timestamp_received']]
                ], ignore_index=True)

        del df, pc

    return market_data


# ══════════════════════════════════════════════════════════
# Backtest Single Market
# ══════════════════════════════════════════════════════════
def backtest_market(market_info, bankroll=100):
    """
    Run V18.2 backtest on a single market.
    
    Logic:
    - Track the CHEAP token's price over time
    - Compute RSI on cheap token price series
    - When RSI hits extreme zone + cheap price ≤ 20¢ → BUY
    - Resolution: did the cheap token win (price → $1.00)?
    """
    prices_df = market_info['prices'].sort_values('timestamp_received').copy()
    cheap_asset = market_info['cheap_asset']
    rich_asset = market_info['rich_asset']

    # Strip timezone
    if prices_df['timestamp_received'].dt.tz is not None:
        prices_df['timestamp_received'] = prices_df['timestamp_received'].dt.tz_localize(None)

    # Extract cheap token price series
    cheap_df = prices_df[prices_df['asset_id'] == cheap_asset].copy()
    if len(cheap_df) < RSI_PERIOD + 5:
        return None

    cheap_prices = cheap_df['price'].values.astype(float)
    cheap_ts = cheap_df['timestamp_received'].values

    # Compute RSI on cheap token prices
    rsi = compute_rsi_series(cheap_prices, RSI_PERIOD)

    # Determine resolution: did the cheap token win?
    # Winner = token whose last known price > 0.90
    rich_df = prices_df[prices_df['asset_id'] == rich_asset]
    if rich_df.empty:
        return None

    last_cheap = float(cheap_df['price'].iloc[-1])
    last_rich = float(rich_df['price'].iloc[-1])

    # Resolution: if one token ended > 0.90, it won
    if last_rich > 0.90:
        cheap_won = False  # Rich token won
    elif last_cheap > 0.90:
        cheap_won = True   # Cheap token won!
    else:
        # Market didn't resolve cleanly in our window — skip
        return None

    # Run through price series and generate signals
    trades = []
    cap = bankroll
    peak = bankroll
    max_dd = 0
    last_trade_i = -999

    for i in range(RSI_PERIOD, len(cheap_prices)):
        entry_price = cheap_prices[i]
        rsi_val = rsi[i]
        direction, confidence = classify_signal(rsi_val, entry_price)

        if direction is None:
            continue
        if entry_price > CHEAP_SIDE_MAX:
            continue
        if i - last_trade_i < 10:  # Cooldown
            continue

        # Bet sizing
        bet = round(min(cap * BET_FRAC, bankroll * 0.12) * 0.5, 2)
        bet = max(bet, 1.0)
        bet = min(bet, cap * 0.50)
        if bet < 1.0 or cap < 5:
            continue

        # Resolve: was the cheap token the winner?
        won = cheap_won

        # P&L: if won, payout = $1 per token, profit = (1 - entry_price) * qty
        #       if lost, lose entry_price * qty
        # Notional = bet (the $ we risk)
        # We're buying 'bet/entry_price' tokens at 'entry_price' each
        # Total cost = bet
        if won:
            qty = bet / max(entry_price, 0.001)
            payout = qty * 1.0  # Each winning token pays $1
            pnl = payout - bet
        else:
            pnl = -bet  # Tokens go to $0

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
            'rsi': round(rsi_val, 1) if not np.isnan(rsi_val) else None,
        })

        last_trade_i = i

        if cap < 5:
            break

    if not trades:
        return None

    wins = sum(1 for t in trades if t['won'])
    n_trades = len(trades)
    wr = wins / n_trades * 100

    return {
        'n_trades': n_trades,
        'wins': wins,
        'win_rate': round(wr, 1),
        'pnl': round(cap - bankroll, 2),
        'final_cap': round(cap, 2),
        'max_dd': round(max_dd * 100, 1),
        'cheap_won': cheap_won,
        'trades': trades,
    }


# ══════════════════════════════════════════════════════════
# Main Pipeline
# ══════════════════════════════════════════════════════════
def run_backtest(data_dir, out_dir):
    data_dir = Path(data_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    parquets = sorted(data_dir.glob('polymarket_orderbook_*.parquet'))
    print(f"Found {len(parquets)} Parquet files ({sum(f.stat().st_size for f in parquets)/1e9:.1f}GB)")

    # Accumulate markets across ALL hours
    all_markets = {}  # cid → market_info (with merged prices)
    total_hours = 0

    for i, pf_path in enumerate(parquets):
        hour = pf_path.stem.split('T')[-1] if 'T' in pf_path.stem else str(i)
        print(f"\n[{i+1}/{len(parquets)}] T{hour} ({pf_path.stat().st_size/1e6:.0f}MB)...")

        market_data = extract_binaries_from_parquet(pf_path)
        new_count = len(market_data)
        total_hours += 1

        # Merge — accumulate prices across hours for the same CID
        for cid, info in market_data.items():
            if cid in all_markets:
                # Market exists from a previous hour — append prices
                existing = all_markets[cid]
                existing['prices'] = pd.concat([
                    existing['prices'],
                    info['prices']
                ], ignore_index=True).sort_values('timestamp_received')
                # Keep the first-seen asset assignment
            else:
                all_markets[cid] = info

        print(f"  New binaries: {new_count} | Cumulative unique: {len(all_markets)}")
        gc.collect()

    print(f"\n{'='*60}")
    print(f"TOTAL UNIQUE BINARY MARKETS: {len(all_markets)}")
    print(f"{'='*60}")

    # ── Run Backtest ──
    results = []
    trade_journal = []

    print(f"\nRunning V18.2 backtest on {len(all_markets)} markets...")

    for j, (cid, info) in enumerate(all_markets.items()):
        if (j + 1) % 500 == 0:
            print(f"  [{j+1}/{len(all_markets)}]...")
        result = backtest_market(info, bankroll=100)
        if result is not None:
            result['cid'] = cid[:24]
            results.append(result)
            trade_journal.extend(result['trades'])

    if not results:
        print("\nNo tradeable markets found.")
        return

    # ── Aggregate ──
    total_trades = sum(r['n_trades'] for r in results)
    total_wins = sum(r['wins'] for r in results)
    total_pnl = sum(r['pnl'] for r in results)
    avg_wr = total_wins / total_trades * 100 if total_trades > 0 else 0

    qualified = [r for r in results if r['n_trades'] >= 3]
    q_trades = sum(r['n_trades'] for r in qualified)
    q_wins = sum(r['wins'] for r in qualified)
    q_wr = q_wins / q_trades * 100 if q_trades > 0 else 0

    # RSI zone breakdown
    rsi_zones = {}
    for t in trade_journal:
        rsi_v = t.get('rsi')
        if rsi_v is None:
            continue
        if rsi_v < 18:
            zone = "ultra_oversold"
        elif rsi_v < 28:
            zone = "oversold"
        elif rsi_v < 35:
            zone = "low"
        elif rsi_v > 82:
            zone = "ultra_overbought"
        elif rsi_v > 72:
            zone = "overbought"
        elif rsi_v > 65:
            zone = "high"
        else:
            zone = "dead_zone"
        rsi_zones.setdefault(zone, {'wins': 0, 'trades': 0})
        rsi_zones[zone]['trades'] += 1
        if t['won']:
            rsi_zones[zone]['wins'] += 1

    # Entry price zone breakdown
    price_zones = {}
    for t in trade_journal:
        ep = t.get('entry_price', 0)
        if ep <= 0.05:
            zone = "ultra_cheap(≤5¢)"
        elif ep <= 0.10:
            zone = "cheap(5-10¢)"
        elif ep <= 0.15:
            zone = "moderate(10-15¢)"
        elif ep <= 0.20:
            zone = "mid(15-20¢)"
        else:
            zone = "expensive(>20¢)"
        price_zones.setdefault(zone, {'wins': 0, 'trades': 0})
        price_zones[zone]['trades'] += 1
        if t['won']:
            price_zones[zone]['wins'] += 1

    # Market-level WR distribution
    market_wrs = [r['win_rate'] for r in results]
    cheap_won_count = sum(1 for r in results if r['cheap_won'])

    # ── Print Results ──
    print(f"\n{'='*60}")
    print(f"PMXT HISTORICAL BACKTEST — V18.2 on REAL DATA")
    print(f"{'='*60}")
    print(f"  Markets analyzed:        {len(all_markets)}")
    print(f"  Markets with trades:     {len(results)}")
    print(f"  Markets qualified (≥3t): {len(qualified)}")
    print(f"  Cheap-side won:          {cheap_won_count}/{len(results)} ({cheap_won_count/max(len(results),1)*100:.1f}%)")
    print(f"")
    print(f"  Total trades:            {total_trades}")
    print(f"  Total wins:              {total_wins}")
    print(f"  Win Rate (all):          {avg_wr:.1f}%")
    print(f"  Win Rate (qualified):    {q_wr:.1f}%")
    print(f"  Total P&L:              ${total_pnl:+.2f}")
    print(f"")
    print(f"  RSI Zone Breakdown:")
    for zone in ['ultra_oversold', 'oversold', 'low', 'dead_zone', 'high', 'overbought', 'ultra_overbought']:
        if zone in rsi_zones:
            z = rsi_zones[zone]
            wr = z['wins'] / z['trades'] * 100 if z['trades'] > 0 else 0
            print(f"    {zone:20s}: {z['wins']}/{z['trades']} ({wr:.0f}%)")

    print(f"")
    print(f"  Entry Price Breakdown:")
    for zone in ['ultra_cheap(≤5¢)', 'cheap(5-10¢)', 'moderate(10-15¢)', 'mid(15-20¢)', 'expensive(>20¢)']:
        if zone in price_zones:
            z = price_zones[zone]
            wr = z['wins'] / z['trades'] * 100 if z['trades'] > 0 else 0
            print(f"    {zone:22s}: {z['wins']}/{z['trades']} ({wr:.0f}%)")

    print(f"")
    print(f"  Market WR distribution:")
    print(f"    Mean:     {np.mean(market_wrs):.1f}%")
    print(f"    Median:   {np.median(market_wrs):.1f}%")
    print(f"    P10:      {np.percentile(market_wrs, 10):.1f}%")
    print(f"    P90:      {np.percentile(market_wrs, 90):.1f}%")
    print(f"")
    print(f"  ─── MC vs HISTORICAL ───")
    print(f"  MC hard-mode avg:       84.6%")
    print(f"  MC hard-mode qualified: 90.7%")
    print(f"  Historical avg:          {avg_wr:.1f}%")
    print(f"  Historical qualified:    {q_wr:.1f}%")
    print(f"  Delta avg:              {avg_wr - 84.6:+.1f}%")
    print(f"  Delta qualified:        {q_wr - 90.7:+.1f}%")
    print(f"{'='*60}")

    # Save
    summary = {
        'timestamp': datetime.utcnow().isoformat(),
        'total_markets': len(all_markets),
        'tradable_markets': len(results),
        'qualified_markets': len(qualified),
        'cheap_won_rate': round(cheap_won_count / max(len(results), 1) * 100, 1),
        'total_trades': total_trades,
        'total_wins': total_wins,
        'win_rate': round(avg_wr, 1),
        'qualified_win_rate': round(q_wr, 1),
        'total_pnl': round(total_pnl, 2),
        'rsi_zones': {k: {'wins': v['wins'], 'trades': v['trades'],
                         'wr': round(v['wins']/v['trades']*100, 1) if v['trades'] > 0 else 0}
                     for k, v in rsi_zones.items()},
        'price_zones': {k: {'wins': v['wins'], 'trades': v['trades'],
                           'wr': round(v['wins']/v['trades']*100, 1) if v['trades'] > 0 else 0}
                       for k, v in price_zones.items()},
        'mc_comparison': {
            'mc_avg_wr': 84.6, 'mc_qualified_wr': 90.7,
            'hist_avg_wr': round(avg_wr, 1), 'hist_qualified_wr': round(q_wr, 1),
            'delta_avg': round(avg_wr - 84.6, 1),
            'delta_qualified': round(q_wr - 90.7, 1),
        },
        'top_markets': sorted([{
            'cid': r['cid'], 'trades': r['n_trades'], 'wins': r['wins'],
            'wr': r['win_rate'], 'pnl': r['pnl'], 'cheap_won': r['cheap_won'],
        } for r in results], key=lambda x: -x['trades'])[:50],
    }

    with open(out_dir / 'backtest_results.json', 'w') as f:
        json.dump(summary, f, indent=2)
    print(f"\nSaved → {out_dir / 'backtest_results.json'}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='PMXT Historical Backtest v2')
    parser.add_argument('--dir', default='pmxt_data/')
    parser.add_argument('--out', default='backtest_results/')
    args = parser.parse_args()
    run_backtest(args.dir, args.out)