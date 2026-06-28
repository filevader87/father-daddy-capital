#!/usr/bin/env python3
"""
PMXT Backtest v4 — Correct resolution logic.

CRITICAL FIX: Don't classify cheap/rich by final price (the loser is always "cheap" at end).
Instead: track BOTH token price series, compute RSI on each independently,
generate signals on whichever is in the cheap zone (≤20¢), and resolve 
by checking if THAT specific token won (final price > 0.90).
"""

import os, sys, json, gc, argparse, warnings
from datetime import datetime
from pathlib import Path
import numpy as np
import pandas as pd

warnings.filterwarnings('ignore', category=RuntimeWarning)

RSI_PERIOD = 14
MIN_CONFIDENCE = 0.85
CHEAP_SIDE_MAX = 0.20
BET_FRAC = 0.12
BANKROLL = 100

# ══════════════════════════════════════════════════════════
# RSI
# ══════════════════════════════════════════════════════════
def compute_rsi(prices, period=RSI_PERIOD):
    """Vectorized RSI. Returns array same length as prices."""
    n = len(prices)
    if n < period + 1:
        return np.full(n, np.nan)
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
    with np.errstate(divide='ignore', invalid='ignore'):
        rs = np.where(avg_l > 0, avg_g / avg_l, 100.0)
    rsi = np.where(avg_l > 0, 100 - 100/(1+rs), 100.0)
    rsi[:period] = np.nan
    return rsi


def signal_confidence(rsi_val, price):
    """V18.2 confidence from RSI + price zone."""
    if np.isnan(rsi_val) or price > CHEAP_SIDE_MAX:
        return 0
    if rsi_val < 18:    return 0.94
    elif rsi_val < 28:  return 0.88
    elif rsi_val < 35:  return 0.84
    elif rsi_val > 82:  return 0.94
    elif rsi_val > 72:  return 0.88
    elif rsi_val > 65:  return 0.84
    else:               return 0  # dead zone


def decode_market(raw):
    return raw.decode('ascii') if isinstance(raw, bytes) else str(raw)


# ══════════════════════════════════════════════════════════
# Per-Market Backtest
# ══════════════════════════════════════════════════════════
def backtest_market(prices_df, bankroll=BANKROLL):
    """
    Backtest a single binary market using V18.2 signals.
    
    For each token in the market:
      - Compute RSI on its price series
      - When price ≤ 20¢ AND RSI is in extreme zone AND confidence ≥ 0.85 → BUY
      - Resolution: did that token win (last price > 0.90)?
    """
    prices_df = prices_df.sort_values('timestamp_received')
    if prices_df['timestamp_received'].dt.tz is not None:
        prices_df = prices_df.copy()
        prices_df['timestamp_received'] = prices_df['timestamp_received'].dt.tz_localize(None)

    assets = prices_df['asset_id'].unique()
    if len(assets) != 2:
        return None

    # Build aligned price series for each asset
    token_data = {}
    for aid in assets:
        adf = prices_df[prices_df['asset_id'] == aid].sort_values('timestamp_received')
        if len(adf) < RSI_PERIOD + 5:
            return None
        prices = adf['price'].values.astype(float)
        ts = adf['timestamp_received'].values
        
        # Check if this token was ever in our cheap zone
        if np.min(prices) > CHEAP_SIDE_MAX:
            continue  # Too expensive, skip

        # Check resolution
        won = float(prices[-1]) > 0.90

        rsi = compute_rsi(prices, RSI_PERIOD)

        token_data[aid] = {
            'prices': prices,
            'ts': ts,
            'rsi': rsi,
            'won': won,
            'min_p': float(np.min(prices)),
            'max_p': float(np.max(prices)),
        }

    if len(token_data) != 2:
        return None

    # Skip markets where neither token was ever cheap
    if all(t['min_p'] > CHEAP_SIDE_MAX for t in token_data.values()):
        return None

    # Skip markets where both ended > 0.90 (no clear resolution)
    wins = [t['won'] for t in token_data.values()]
    if sum(wins) != 1:
        return None

    # Generate trades across both tokens
    trades = []
    cap = bankroll
    last_trade_ts = -np.inf
    min_gap_ns = 30_000_000_000  # 30 sec cooldown in nanoseconds

    for aid, td in token_data.items():
        for i in range(RSI_PERIOD, len(td['prices'])):
            ep = td['prices'][i]
            conf = signal_confidence(td['rsi'][i], ep)
            if conf < MIN_CONFIDENCE:
                continue
            ts_ns = int(pd.Timestamp(td['ts'][i]).value)  # nanoseconds as int
            if ts_ns - last_trade_ts < min_gap_ns:
                continue

            bet = round(min(cap * BET_FRAC, bankroll * 0.12) * 0.5, 2)
            bet = max(bet, 1.0)
            bet = min(bet, cap * 0.50)
            if bet < 1.0 or cap < 5:
                continue

            won = td['won']
            if won:
                qty = bet / max(ep, 0.001)
                pnl = qty * 1.0 - bet
            else:
                pnl = -bet

            cap += pnl
            trades.append({
                'won': won,
                'pnl': round(pnl, 2),
                'entry_price': round(float(ep), 4),
                'confidence': round(conf, 3),
                'rsi': round(float(td['rsi'][i]), 1),
            })
            last_trade_ts = ts_ns

            if cap < 5:
                break

    if not trades:
        return None

    wins_count = sum(1 for t in trades if t['won'])
    n = len(trades)
    
    return {
        'n_trades': n,
        'wins': wins_count,
        'win_rate': round(wins_count / n * 100, 1),
        'pnl': round(cap - bankroll, 2),
        'cheap_won_by_market': list(token_data.values())[0]['won'] != list(token_data.values())[1]['won'],
    }


# ══════════════════════════════════════════════════════════
# Parquet Processing
# ══════════════════════════════════════════════════════════
def process_parquet(filepath):
    """Process one hourly Parquet: extract + backtest binary markets."""
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(filepath)
    binary_cids = set()

    # PASS 1: Identify binary CIDs
    for rg in range(min(pf.num_row_groups, 8)):
        t = pf.read_row_group(rg, columns=['market','asset_id','price','event_type'])
        df = t.to_pandas()
        pc = df[df['event_type'] == 'price_change'].copy()
        if pc.empty:
            del df, pc; continue
        pc['cid'] = pc['market'].apply(decode_market)
        stats = pc.groupby('cid').agg(
            events=('price','count'), n_assets=('asset_id','nunique'),
            pmin=('price','min'), pmax=('price','max'),
        )
        bins = stats[(stats['n_assets']==2) & (stats['pmin']<=0.10) &
                     (stats['pmax']>=0.90) & (stats['events']>=50)]
        binary_cids.update(bins.index.tolist())
        del df, pc

    if not binary_cids:
        return []

    # PASS 2: Extract + backtest each CID
    results = []
    processed = set()

    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg, columns=['market','asset_id','price',
                                            'timestamp_received','event_type'])
        df = t.to_pandas()
        pc = df[df['event_type'] == 'price_change'].copy()
        pc['cid'] = pc['market'].apply(decode_market)
        pc = pc[pc['cid'].isin(binary_cids)]

        for cid, grp in pc.groupby('cid'):
            if cid in processed:
                continue
            processed.add(cid)
            if grp['asset_id'].nunique() != 2:
                continue

            result = backtest_market(grp[['asset_id','price','timestamp_received']])
            if result:
                result['cid'] = cid[:24]
                results.append(result)

        del df, pc

    return results


# ══════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════
def run_backtest(data_dir, out_dir):
    data_dir = Path(data_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    parquets = sorted(data_dir.glob('polymarket_orderbook_*.parquet'))
    # Filter out corrupted/empty files
    valid_files = [f for f in parquets if f.stat().st_size > 100_000]  # >100KB
    print(f"Found {len(valid_files)} valid Parquet files ({len(parquets)-len(valid_files)} skipped)")

    all_results = []

    for i, pf_path in enumerate(valid_files):
        hour = pf_path.stem.split('T')[-1] if 'T' in pf_path.stem else str(i)
        size_mb = pf_path.stat().st_size / 1e6
        print(f"\n[{i+1}/{len(valid_files)}] T{hour} ({size_mb:.0f}MB)...", flush=True)

        try:
            results = process_parquet(pf_path)
        except Exception as e:
            print(f"  ERROR: {e}")
            continue

        if results:
            n_t = sum(r['n_trades'] for r in results)
            n_w = sum(r['wins'] for r in results)
            wr = n_w / max(n_t, 1) * 100
            print(f"  Markets: {len(results)} | Trades: {n_t} | WR: {wr:.1f}%")
        else:
            print(f"  No tradeable markets")

        all_results.extend(results)
        gc.collect()

    if not all_results:
        print("\nNo results.")
        return

    # ── Aggregation ──
    tt = sum(r['n_trades'] for r in all_results)
    tw = sum(r['wins'] for r in all_results)
    tpnl = sum(r['pnl'] for r in all_results)
    awr = tw / tt * 100

    qualified = [r for r in all_results if r['n_trades'] >= 3]
    qt = sum(r['n_trades'] for r in qualified)
    qw = sum(r['wins'] for r in qualified)
    qwr = qw / qt * 100 if qt > 0 else 0

    mwrs = [r['win_rate'] for r in all_results]

    print(f"\n{'='*60}")
    print(f"PMXT HISTORICAL BACKTEST — V18.2 on REAL DATA")
    print(f"{'='*60}")
    print(f"  Markets with trades:     {len(all_results)}")
    print(f"  Qualified (≥3 trades):   {len(qualified)}")
    print(f"  Total trades:             {tt}")
    print(f"  Total wins:               {tw}")
    print(f"  Win Rate (all):           {awr:.1f}%")
    print(f"  Win Rate (qualified):    {qwr:.1f}%")
    print(f"  Total P&L:               ${tpnl:+.2f}")
    print(f"")
    print(f"  Market WR dist: mean={np.mean(mwrs):.1f}% median={np.median(mwrs):.1f}%")
    print(f"                  P10={np.percentile(mwrs,10):.1f}% P90={np.percentile(mwrs,90):.1f}%")
    print(f"")
    print(f"  ──── MC vs HISTORICAL ────")
    print(f"  MC hard-mode:          84.6% avg / 90.7% qualified")
    print(f"  Historical:            {awr:.1f}% avg / {qwr:.1f}% qualified")
    print(f"  Delta:                 {awr-84.6:+.1f}% avg / {qwr-90.7:+.1f}% qualified")
    print(f"{'='*60}")

    summary = {
        'timestamp': datetime.utcnow().isoformat(),
        'markets': len(all_results), 'qualified': len(qualified),
        'trades': tt, 'wins': tw,
        'win_rate': round(awr,1), 'qualified_wr': round(qwr,1),
        'pnl': round(tpnl,2),
        'mc': {'avg':84.6, 'qualified':90.7},
        'delta': {'avg': round(awr-84.6,1), 'qualified': round(qwr-90.7,1)},
    }
    with open(out_dir/'backtest_results.json','w') as f:
        json.dump(summary, f, indent=2)
    print(f"\nSaved → {out_dir/'backtest_results.json'}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dir', default='pmxt_data/')
    parser.add_argument('--out', default='backtest_results/')
    args = parser.parse_args()
    run_backtest(args.dir, args.out)