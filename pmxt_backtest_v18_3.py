#!/usr/bin/env python3
"""
V18.3 PMXT Backtest — FAST version.
Avoids row-by-row Python loops. Uses pyarrow->numpy directly,
pandas groupby for aggregation, numpy for RSI.
"""

import pyarrow.parquet as pq
import numpy as np
import json
from pathlib import Path
from datetime import datetime
import warnings; warnings.filterwarnings('ignore')
import gc

RSI_PERIOD = 14; MIN_CONF = 0.85; MAX_PRICE = 0.15; MIN_PRICE = 0.01

def compute_rsi(prices, period=14):
    n = len(prices)
    if n < period + 1: return np.full(n, np.nan)
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
    rsi = np.where(avg_l > 0, 100 - 100/(1+rs), 100.0)
    rsi[:period] = np.nan
    return rsi


def backtest_hour(filepath):
    """Process one hourly Parquet. Fast: pandas groupby, numpy RSI."""
    import pandas as pd
    
    # Read ALL row groups but only 3 cols for phase 1
    pf = pq.ParquetFile(filepath)
    
    # Phase 1: Find binary CIDs — read minimal columns, one RG at a time
    # Accumulate sum/count using pandas groupby on each RG
    sum_df = None
    
    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg, columns=['market','asset_id','price','event_type'])
        df = t.to_pandas()
        pc = df[df['event_type'] == 'price_change'].copy()
        if pc.empty:
            del df, t; continue
        
        # Decode market to string hash for groupby
        pc['cid'] = pc['market'].apply(lambda r: r.hex() if isinstance(r, (bytes, bytearray)) else str(r))
        pc['price_f'] = pc['price'].astype(float)
        
        # Groupby on this RG — fast vectorized
        agg = pc.groupby(['cid','asset_id'])['price_f'].agg(['sum','count']).reset_index()
        
        if sum_df is None:
            sum_df = agg
        else:
            # Merge: add sums and counts
            merged = sum_df.set_index(['cid','asset_id']).add(
                agg.set_index(['cid','asset_id']), fill_value=0
            ).reset_index()
            # Fix: count should be int sum, not float
            merged['count'] = merged['count'].astype(int)
            sum_df = merged
        
        del df, t, pc, agg
    
    # Compute means and find binary CIDs
    sum_df['mean'] = sum_df['sum'] / sum_df['count']
    
    # For each CID, check if it has exactly 2 assets with means straddling 0.30/0.70
    cid_groups = sum_df.groupby('cid')
    bin_cids = set()
    cid_aids = {}
    
    for cid, grp in cid_groups:
        if len(grp) != 2: continue
        means = grp['mean'].values
        if min(means) < 0.30 and max(means) > 0.70:
            bin_cids.add(cid)
            cid_aids[cid] = grp['asset_id'].tolist()
    
    del sum_df; gc.collect()
    
    if not bin_cids:
        return []
    
    print(f"{len(bin_cids)} bin markets...", end=' ', flush=True)
    
    # Phase 2: For binary CIDs only, collect price series
    # Read only the binary CID data across all RGs
    cid_prices = {}  # cid -> {aid: [(ts, price), ...]}
    for cid in bin_cids:
        cid_prices[cid] = {}
        for aid in cid_aids[cid]:
            cid_prices[cid][aid] = []
    
    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg, columns=['market','asset_id','price','event_type','timestamp_received'])
        df = t.to_pandas()
        pc = df[df['event_type'] == 'price_change'].copy()
        if pc.empty:
            del df, t; continue
        
        pc['cid'] = pc['market'].apply(lambda r: r.hex() if isinstance(r, (bytes, bytearray)) else str(r))
        
        # Filter to binary CIDs only
        pc_bin = pc[pc['cid'].isin(bin_cids)].copy()
        if pc_bin.empty:
            del df, t, pc; continue
        
        pc_bin['price_f'] = pc_bin['price_f'] if 'price_f' in pc_bin.columns else pc_bin['price'].astype(float)
        pc_bin['aid'] = pc_bin['asset_id'].astype(str)
        
        # Fast: groupby and collect
        for (cid, aid), grp in pc_bin.groupby(['cid','aid']):
            ts_arr = grp['timestamp_received'].values
            p_arr = grp['price_f'].values
            pairs = list(zip(ts_arr.tolist(), p_arr.tolist()))
            if aid in cid_prices.get(cid, {}):
                cid_prices[cid][aid].extend(pairs)
            else:
                if cid not in cid_prices:
                    cid_prices[cid] = {}
                cid_prices[cid][aid] = pairs
        
        del df, t, pc
    
    print("analyzing...", end=' ', flush=True)
    
    # Phase 3: RSI analysis per market
    signals = []
    for cid, assets in cid_prices.items():
        aids = cid_aids.get(cid, [])
        if len(aids) != 2: continue
        
        # Resolution
        finals = {}
        for aid in aids:
            data = assets.get(aid, [])
            if data:
                finals[aid] = sorted(data, key=lambda x: x[0])[-1][1]
        if not finals or max(finals.values()) < 0.90: continue
        
        for aid in aids:
            data = sorted(assets.get(aid, []), key=lambda x: x[0])
            if len(data) < RSI_PERIOD + 5: continue
            prices = np.array([d[1] for d in data], dtype=float)
            won = finals.get(aid, 0) > 0.90
            rsi = compute_rsi(prices, RSI_PERIOD)
            
            last_i = -999
            for i in range(RSI_PERIOD+1, len(prices)):
                if i - last_i < 20: continue
                p = prices[i]
                if p < MIN_PRICE or p > MAX_PRICE: continue
                r = rsi[i]
                if np.isnan(r): continue
                
                if r < 18:
                    conf = min(0.95, 0.85+(28-r)/60); zone = 'ultra_oversold'
                elif r < 28:
                    conf = min(0.95, 0.85+(28-r)/100); zone = 'oversold'
                elif r < 35:
                    conf = min(0.85, 0.82+(35-r)/100); zone = 'near_oversold'
                else: continue  # V18.3b: RSI 35+ dead zone
                
                if conf >= MIN_CONF:
                    signals.append({'price': float(p), 'rsi': float(r),
                                   'conf': float(conf), 'won': bool(won), 'zone': zone})
                    last_i = i
    
    del cid_prices; gc.collect()
    return signals


def main():
    import argparse, time
    p = argparse.ArgumentParser()
    p.add_argument('--dir', default='pmxt_data')
    p.add_argument('--out', default='backtest_results_v18_3')
    p.add_argument('--maxfiles', type=int, default=99)
    a = p.parse_args()
    
    from collections import defaultdict
    
    data_dir = Path(a.dir)
    out_dir = Path(a.out)
    out_dir.mkdir(exist_ok=True)
    
    files = sorted(data_dir.glob('polymarket_orderbook_*.parquet'))
    valid = [f for f in files if f.stat().st_size > 100_000_000][:a.maxfiles]
    print(f"Found {len(valid)} valid files")
    
    all_sig = []
    for fi, f in enumerate(valid):
        t0 = time.time()
        print(f"[{fi+1}/{len(valid)}] {f.name}...", end=' ', flush=True)
        gc.collect()
        sigs = backtest_hour(f)
        n = len(sigs)
        wr = sum(s['won'] for s in sigs)/n*100 if n else 0
        dt = time.time() - t0
        print(f"{n} sig, WR={wr:.1f}% ({dt:.0f}s)")
        all_sig.extend(sigs)
        gc.collect()
    
    if not all_sig:
        print("No signals found!"); return
    
    t = len(all_sig); w = sum(s['won'] for s in all_sig); wr = w/t*100
    
    zones = defaultdict(lambda: {'n':0, 'w':0})
    price_bins = defaultdict(lambda: {'n':0, 'w':0})
    for s in all_sig:
        zones[s['zone']]['n'] += 1; zones[s['zone']]['w'] += int(s['won'])
        if s['price'] < 0.05: k = '1-5¢'
        elif s['price'] < 0.10: k = '5-10¢'
        else: k = '10-15¢'
        price_bins[k]['n'] += 1; price_bins[k]['w'] += int(s['won'])
    
    print(f"\n{'='*60}")
    print(f"V18.3 PMXT BACKTEST — OVERSOLD-ONLY (RSI < 35)")
    print(f"{'='*60}")
    print(f"  Signals: {t} | Wins: {w} | WR: {wr:.1f}%")
    print()
    for zone in ['ultra_oversold','oversold','near_oversold']:
        d = zones[zone]
        if d['n']: print(f"  {zone:18s}: n={d['n']:5d} WR={d['w']/d['n']*100:.1f}%")
    print()
    for k in ['1-5¢','5-10¢','10-15¢']:
        d = price_bins[k]
        if d['n']: print(f"  {k:8s}: n={d['n']:5d} WR={d['w']/d['n']*100:.1f}%")
    
    print("\n  Best combos:")
    for desc, check in [
        ('RSI<28', lambda s: s['rsi']<28),
        ('RSI<28 ≤10¢', lambda s: s['rsi']<28 and s['price']<=0.10),
        ('RSI<28 ≤15¢', lambda s: s['rsi']<28 and s['price']<=0.15),
        ('RSI<35 ≤10¢', lambda s: s['rsi']<35 and s['price']<=0.10),
    ]:
        sub = [s for s in all_sig if check(s)]
        if sub: print(f"    {desc:18s}: n={len(sub):5d} WR={sum(s['won'] for s in sub)/len(sub)*100:.1f}%")
    
    print(f"\n  V18.2 MC: 84.6% | V18.3 MC: 77.6% | V18.3 PMXT: {wr:.1f}%")
    
    results = {'version':'V18.3-oversold-only','timestamp':datetime.now().isoformat(),
               'total_signals':t,'wins':w,'win_rate':round(wr,1)}
    with open(out_dir/'v18_3_pmxt_results.json','w') as f:
        json.dump(results, f, indent=2)
    print(f"Saved → {out_dir/'v18_3_pmxt_results.json'}")


if __name__ == '__main__':
    main()