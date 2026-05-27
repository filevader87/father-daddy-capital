#!/usr/bin/env python3
"""
PMXT Backtest v3 — Memory-efficient incremental processing.

Process each hourly Parquet independently, run backtest per market,
then aggregate results. Never hold more than one market's data in memory.
"""

import os, sys, json, gc, argparse
from datetime import datetime
from pathlib import Path
import numpy as np
import pandas as pd

RSI_PERIOD = 14
MIN_CONFIDENCE = 0.85
CHEAP_SIDE_MAX = 0.20
BET_FRAC = 0.12
BANKROLL = 100

def compute_rsi_series(prices, period=RSI_PERIOD):
    deltas = np.diff(prices, prepend=prices[0])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.zeros_like(prices)
    avg_loss = np.zeros_like(prices)
    if len(prices) > period:
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
    if np.isnan(rsi_val):
        return None, 0
    if entry_price > CHEAP_SIDE_MAX:
        return None, 0

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
        return None, 0

    confidence = min(0.95, base)
    if confidence < MIN_CONFIDENCE:
        return None, 0

    return "buy", confidence


def decode_market(raw_bytes):
    if isinstance(raw_bytes, bytes):
        return raw_bytes.decode('ascii')
    return str(raw_bytes)


def process_single_parquet(filepath):
    """Process one hourly Parquet: extract binaries, backtest each, return results."""
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(filepath)
    binary_cids = set()

    # PASS 1: Identify binary CIDs
    for rg in range(min(pf.num_row_groups, 8)):
        t = pf.read_row_group(rg, columns=['market', 'asset_id', 'price', 'event_type'])
        df = t.to_pandas()
        pc = df[df['event_type'] == 'price_change'].copy()
        if pc.empty:
            del df, pc; continue
        pc['cid'] = pc['market'].apply(decode_market)
        stats = pc.groupby('cid').agg(
            events=('price', 'count'), n_assets=('asset_id', 'nunique'),
            pmin=('price', 'min'), pmax=('price', 'max'),
        )
        bins = stats[(stats['n_assets'] == 2) & (stats['pmin'] <= 0.10) &
                     (stats['pmax'] >= 0.90) & (stats['events'] >= 50)]
        binary_cids.update(bins.index.tolist())
        del df, pc

    if not binary_cids:
        return []

    # PASS 2: For each CID, extract and backtest ONE AT A TIME
    results = []
    seen_cids_this_file = set()

    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg, columns=['market', 'asset_id', 'price',
                                            'timestamp_received', 'event_type'])
        df = t.to_pandas()
        pc = df[df['event_type'] == 'price_change'].copy()
        pc['cid'] = pc['market'].apply(decode_market)
        pc = pc[pc['cid'].isin(binary_cids)]

        if pc.empty:
            del df, pc; continue

        # Group by CID, backtest each immediately
        for cid, grp in pc.groupby('cid'):
            assets = grp['asset_id'].unique()
            if len(assets) != 2:
                continue

            # Deduplicate within file
            if cid in seen_cids_this_file:
                continue
            seen_cids_this_file.add(cid)

            # Identify cheap vs rich asset
            last_prices = {}
            for aid in assets:
                aid_df = grp[grp['asset_id'] == aid]
                last_prices[aid] = float(aid_df['price'].iloc[-1])
            sorted_a = sorted(last_prices.items(), key=lambda x: x[1])
            cheap_asset = sorted_a[0][0]
            rich_asset = sorted_a[1][0]

            # Run backtest
            result = backtest_market_inline(grp, cheap_asset, rich_asset)
            if result is not None:
                result['cid'] = cid[:24]
                result['hour'] = filepath.stem.split('T')[-1] if 'T' in filepath.stem else '?'
                results.append(result)

        del df, pc

    return results


def backtest_market_inline(prices_df, cheap_asset, rich_asset, bankroll=BANKROLL):
    """Backtest a single market inline — no extra memory allocation."""
    prices_df = prices_df.sort_values('timestamp_received')

    # Strip tz
    if prices_df['timestamp_received'].dt.tz is not None:
        prices_df = prices_df.copy()
        prices_df['timestamp_received'] = prices_df['timestamp_received'].dt.tz_localize(None)

    cheap_df = prices_df[prices_df['asset_id'] == cheap_asset]
    rich_df = prices_df[prices_df['asset_id'] == rich_asset]

    if len(cheap_df) < RSI_PERIOD + 5 or len(rich_df) < 5:
        return None

    cheap_prices = cheap_df['price'].values.astype(float)

    # Check if cheap side was ever in our target zone
    if np.min(cheap_prices) > 0.15:
        return None

    # Determine resolution
    last_cheap = float(cheap_df['price'].iloc[-1])
    last_rich = float(rich_df['price'].iloc[-1])
    if last_rich > 0.90:
        cheap_won = False
    elif last_cheap > 0.90:
        cheap_won = True
    else:
        return None  # No clear resolution

    # Compute RSI
    rsi = compute_rsi_series(cheap_prices, RSI_PERIOD)

    # Generate trades
    trades = []
    cap = bankroll
    peak = bankroll
    max_dd = 0
    n_trades = 0
    wins = 0
    last_i = -999

    for i in range(RSI_PERIOD, len(cheap_prices)):
        ep = cheap_prices[i]
        direction, confidence = classify_signal(rsi[i], ep)
        if direction is None:
            continue
        if i - last_i < 10:
            continue

        bet = round(min(cap * BET_FRAC, bankroll * 0.12) * 0.5, 2)
        bet = max(bet, 1.0)
        bet = min(bet, cap * 0.50)
        if bet < 1.0 or cap < 5:
            continue

        won = cheap_won
        if won:
            qty = bet / max(ep, 0.001)
            pnl = qty * 1.0 - bet
        else:
            pnl = -bet

        cap += pnl
        peak = max(peak, cap)
        dd = (peak - cap) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
        n_trades += 1
        if won: wins += 1
        last_i = i

        if cap < 5:
            break

    if n_trades == 0:
        return None

    return {
        'n_trades': n_trades,
        'wins': wins,
        'win_rate': round(wins / n_trades * 100, 1),
        'pnl': round(cap - bankroll, 2),
        'cheap_won': cheap_won,
        'cheap_min': round(float(np.min(cheap_prices)), 4),
        'cheap_max': round(float(np.max(cheap_prices)), 4),
    }


def run_backtest(data_dir, out_dir):
    data_dir = Path(data_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    parquets = sorted(data_dir.glob('polymarket_orderbook_*.parquet'))
    print(f"Found {len(parquets)} Parquet files")

    all_results = []
    total_binaries = 0

    for i, pf_path in enumerate(parquets):
        hour = pf_path.stem.split('T')[-1] if 'T' in pf_path.stem else str(i)
        size_mb = pf_path.stat().st_size / 1e6
        print(f"\n[{i+1}/{len(parquets)}] T{hour} ({size_mb:.0f}MB)...", flush=True)

        results = process_single_parquet(pf_path)
        total_binaries += len(results)

        if results:
            wrs = [r['win_rate'] for r in results]
            avg_wr = np.mean(wrs)
            n_wins = sum(r['wins'] for r in results)
            n_total = sum(r['n_trades'] for r in results)
            hour_wr = n_wins / max(n_total, 1) * 100
            print(f"  Markets: {len(results)} | Trades: {n_total} | WR: {hour_wr:.1f}% | Avg/market: {avg_wr:.1f}%")
        else:
            print(f"  No tradeable markets")

        all_results.extend(results)
        gc.collect()

    # ── Final Aggregation ──
    if not all_results:
        print("\nNo results.")
        return

    total_trades = sum(r['n_trades'] for r in all_results)
    total_wins = sum(r['wins'] for r in all_results)
    total_pnl = sum(r['pnl'] for r in all_results)
    avg_wr = total_wins / total_trades * 100

    qualified = [r for r in all_results if r['n_trades'] >= 3]
    q_trades = sum(r['n_trades'] for r in qualified)
    q_wins = sum(r['wins'] for r in qualified)
    q_wr = q_wins / q_trades * 100 if q_trades > 0 else 0

    cheap_won_count = sum(1 for r in all_results if r['cheap_won'])
    market_wrs = [r['win_rate'] for r in all_results]

    print(f"\n{'='*60}")
    print(f"PMXT HISTORICAL BACKTEST — V18.2 on REAL DATA")
    print(f"{'='*60}")
    print(f"  Markets with trades:     {len(all_results)}")
    print(f"  Qualified markets (≥3t):  {len(qualified)}")
    print(f"  Cheap-side won:           {cheap_won_count}/{len(all_results)} ({cheap_won_count/max(len(all_results),1)*100:.1f}%)")
    print(f"")
    print(f"  Total trades:             {total_trades}")
    print(f"  Total wins:               {total_wins}")
    print(f"  Win Rate (all):           {avg_wr:.1f}%")
    print(f"  Win Rate (qualified):    {q_wr:.1f}%")
    print(f"  Total P&L:               ${total_pnl:+.2f}")
    print(f"")
    print(f"  Market WR distribution:")
    print(f"    Mean:   {np.mean(market_wrs):.1f}%")
    print(f"    Median: {np.median(market_wrs):.1f}%")
    print(f"    P10:    {np.percentile(market_wrs, 10):.1f}%")
    print(f"    P90:    {np.percentile(market_wrs, 90):.1f}%")
    print(f"")
    print(f"  ──── MC vs HISTORICAL ────")
    print(f"  MC hard-mode avg:        84.6%")
    print(f"  MC hard-mode qualified: 90.7%")
    print(f"  Historical avg:         {avg_wr:.1f}%")
    print(f"  Historical qualified:   {q_wr:.1f}%")
    print(f"  Delta avg:              {avg_wr - 84.6:+.1f}%")
    print(f"  Delta qualified:        {q_wr - 90.7:+.1f}%")
    print(f"{'='*60}")

    # Save
    summary = {
        'timestamp': datetime.utcnow().isoformat(),
        'total_markets': len(all_results),
        'qualified_markets': len(qualified),
        'cheap_won_pct': round(cheap_won_count / max(len(all_results), 1) * 100, 1),
        'total_trades': total_trades,
        'total_wins': total_wins,
        'win_rate': round(avg_wr, 1),
        'qualified_win_rate': round(q_wr, 1),
        'total_pnl': round(total_pnl, 2),
        'mc_comparison': {
            'mc_avg': 84.6, 'mc_qualified': 90.7,
            'hist_avg': round(avg_wr, 1), 'hist_qualified': round(q_wr, 1),
            'delta_avg': round(avg_wr - 84.6, 1), 'delta_qualified': round(q_wr - 90.7, 1),
        },
    }

    with open(out_dir / 'backtest_results.json', 'w') as f:
        json.dump(summary, f, indent=2)
    print(f"\nSaved → {out_dir / 'backtest_results.json'}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dir', default='pmxt_data/')
    parser.add_argument('--out', default='backtest_results/')
    args = parser.parse_args()
    run_backtest(args.dir, args.out)