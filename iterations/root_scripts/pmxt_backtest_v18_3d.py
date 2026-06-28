#!/usr/bin/env python3
"""
V18.3 PMXT Backtest — FAST VECTORIALIZED approach.
Use pyarrow compute + numpy instead of to_pylist().
No pandas, no to_pylist(). Pure arrow table ops + numpy.
"""

import pyarrow.parquet as pq
import pyarrow.compute as pc
import numpy as np
import json
from pathlib import Path
import warnings; warnings.filterwarnings('ignore')
import gc, random, time, sys

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


def analyze_signals(prices_arr):
    signals = []
    if len(prices_arr) < RSI_PERIOD + 5:
        return signals
    final_price = prices_arr[-1]
    won = final_price > 0.90
    rsi = compute_rsi(prices_arr, RSI_PERIOD)
    last_i = -999
    for i in range(RSI_PERIOD+1, len(prices_arr)):
        if i - last_i < 20: continue
        p = prices_arr[i]
        if p < MIN_PRICE or p > MAX_PRICE: continue
        r = rsi[i]
        if np.isnan(r): continue
        if r < 18:
            conf = min(0.95, 0.85+(28-r)/60); zone = 'ultra_oversold'
        elif r < 28:
            conf = min(0.95, 0.85+(28-r)/100); zone = 'oversold'
        elif r < 35:
            conf = min(0.85, 0.82+(35-r)/100); zone = 'near_oversold'
        else:
            continue
        if conf >= MIN_CONF:
            signals.append({'price': float(p), 'rsi': float(r),
                           'conf': float(conf), 'won': bool(won), 'zone': zone})
            last_i = i
    return signals


def hex_bytes(arr):
    """Convert bytes column to hex strings via numpy - fast vectorized."""
    # arr is a pyarrow BinaryArray
    # Convert each 32-byte CID to hex string
    result = []
    for v in arr:
        if isinstance(v, (bytes, bytearray)):
            result.append(v.hex())
        else:
            result.append(str(v))
    return result


def backtest_hour(filepath, max_markets=2000):
    pf = pq.ParquetFile(filepath)
    nrg = pf.num_row_groups
    
    # Phase 1: Find binary CIDs using arrow table groupby
    print(f"  P1: {nrg} RGs...", end=' ', flush=True)
    t0 = time.time()
    
    # Read just the columns we need for Phase 1 across ALL RGs at once
    # Use read() to get a single table, filter with arrow compute
    table = pf.read(columns=['market','asset_id','price','event_type'])
    
    # Filter to price_change only
    mask = pc.equal(table.column('event_type'), 'price_change')
    table_pc = table.filter(mask)
    del table
    
    n_rows = len(table_pc)
    print(f"{n_rows} pc rows", end=' ', flush=True)
    
    # Convert to numpy-ready arrays
    price_np = table_pc.column('price').to_numpy(dtype='float64')
    market_col = table_pc.column('market')
    
    # Build CID as hex string
    cids_hex = np.array([v.hex() if isinstance(v, (bytes, bytearray)) else str(v) for v in market_col], dtype='U64')
    aids = np.array([str(v) for v in table_pc.column('asset_id')], dtype='U64')
    
    del table_pc
    
    # Group by (cid, aid) to get mean prices
    # Use numpy unique + bincount for fast aggregation
    import pandas as pd
    df = pd.DataFrame({'cid': cids_hex, 'aid': aids, 'price': price_np})
    del cids_hex, aids, price_np
    
    stats = df.groupby(['cid','aid'])['price'].agg(['sum','count']).reset_index()
    del df; gc.collect()
    
    # Find binary markets (exactly 2 aids with cheap < 0.30, rich > 0.70)
    cid_aid_count = stats.groupby('cid')['aid'].count()
    binary_cids = cid_aid_count[cid_aid_count == 2].index
    stats_bin = stats[stats['cid'].isin(binary_cids)]
    
    # Compute mean per (cid, aid)
    stats_bin['mean'] = stats_bin['sum'] / stats_bin['count']
    
    bin_markets = []
    for cid, grp in stats_bin.groupby('cid'):
        if len(grp) != 2: continue
        sorted_g = grp.sort_values('mean')
        cheap_aid = sorted_g.iloc[0]['aid']
        cheap_mean = sorted_g.iloc[0]['mean']
        rich_aid = sorted_g.iloc[1]['aid']
        if cheap_mean < 0.30 and sorted_g.iloc[1]['mean'] > 0.70:
            bin_markets.append((cid, cheap_aid, rich_aid, cheap_mean))
    
    del stats, stats_bin; gc.collect()
    
    total_bin = len(bin_markets)
    if total_bin > max_markets:
        random.seed(42)
        bin_markets = random.sample(bin_markets, max_markets)
    
    print(f"{total_bin} bin, sampled {len(bin_markets)} ({time.time()-t0:.0f}s)", end=' ', flush=True)
    
    if not bin_markets:
        return []
    
    # Phase 2: For sampled markets, collect price series using arrow read
    # Process in chunks to limit memory
    CHUNK = 500
    all_signals = []
    sampled_cids = set(m[0] for m in bin_markets)
    aid_map = {m[0]: (m[1], m[2]) for m in bin_markets}
    
    n_chunks = (len(bin_markets) + CHUNK - 1) // CHUNK
    
    for ci in range(n_chunks):
        chunk = bin_markets[ci*CHUNK:(ci+1)*CHUNK]
        chunk_cids = set(m[0] for m in chunk)
        chunk_aid = {m[0]: (m[1], m[2]) for m in chunk}
        
        cid_prices = {cid: {'cheap': [], 'rich': []} for cid, _, _, _ in chunk}
        
        # Read ALL RGs at once with arrow, filter to chunk CIDs
        table = pf.read(columns=['market','asset_id','price','event_type','timestamp_received'])
        mask = pc.equal(table.column('event_type'), 'price_change')
        table_pc = table.filter(mask)
        del table
        
        # Convert columns
        mkt_hex = np.array([v.hex() if isinstance(v, (bytes, bytearray)) else str(v) for v in table_pc.column('market')], dtype='U64')
        aid_np = np.array([str(v) for v in table_pc.column('asset_id')], dtype='U64')
        price_np = table_pc.column('price').to_numpy(dtype='float64')
        ts_np = table_pc.column('timestamp_received').to_numpy()
        del table_pc
        
        # Filter to chunk CIDs using numpy
        cid_set = chunk_cids  # set lookup is O(1)
        
        # Vectorized membership test
        # Build boolean mask
        mask_chunk = np.zeros(len(mkt_hex), dtype=bool)
        for cid in chunk_cids:
            mask_chunk |= (mkt_hex == cid)
        
        idx = np.where(mask_chunk)[0]
        
        for i in idx:
            cid = str(mkt_hex[i])
            aid = str(aid_np[i])
            p = float(price_np[i])
            ts = float(ts_np[i])
            ca, ra = chunk_aid[cid]
            if aid == ca:
                cid_prices[cid]['cheap'].append((ts, p))
            elif aid == ra:
                cid_prices[cid]['rich'].append((ts, p))
        
        del mkt_hex, aid_np, price_np, ts_np, mask_chunk, idx
        
        # Analyze chunk markets
        for cid, ca, ra, cheap_mean in chunk:
            cheap_data = cid_prices[cid]['cheap']
            if len(cheap_data) > RSI_PERIOD + 5:
                cheap_data.sort(key=lambda x: x[0])
                prices = np.array([p for _, p in cheap_data], dtype=float)
                all_signals.extend(analyze_signals(prices))
                del prices
            
            if cheap_mean < 0.15:
                rich_data = cid_prices[cid]['rich']
                if len(rich_data) > RSI_PERIOD + 5:
                    rich_data.sort(key=lambda x: x[0])
                    prices = np.array([p for _, p in rich_data], dtype=float)
                    all_signals.extend(analyze_signals(prices))
                    del prices
        
        del cid_prices, chunk_aid
        gc.collect()
        
        n = len(all_signals)
        wr = sum(s['won'] for s in all_signals)/n*100 if n else 0
        print(f"c{ci+1}/{n_chunks}({n}sig,{wr:.0f}%)", end=' ', flush=True)
    
    n = len(all_signals)
    wr = sum(s['won'] for s in all_signals)/n*100 if n else 0
    print(f"done: {n} sig, WR={wr:.1f}%")
    return all_signals


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--dir', default='pmxt_data')
    p.add_argument('--out', default='backtest_results_v18_3')
    p.add_argument('--maxfiles', type=int, default=99)
    p.add_argument('--sample', type=int, default=2000)
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
        print(f"[{fi+1}/{len(valid)}] {f.name}", flush=True)
        gc.collect()
        sigs = backtest_hour(str(f), max_markets=a.sample)
        n = len(sigs)
        wr = sum(s['won'] for s in sigs)/n*100 if n else 0
        dt = time.time() - t0
        print(f"  -> {n} sig, WR={wr:.1f}% ({dt:.0f}s)", flush=True)
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
    print(f"V18.3 PMXT BACKTEST — OVERSOLD-ONLY")
    print(f"Sample: {a.sample} mkts/hr, {len(valid)} hrs")
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
        ('RSI<28 10-15¢', lambda s: s['rsi']<28 and 0.10<=s['price']<=0.15),
        ('RSI<28 5-15¢', lambda s: s['rsi']<28 and 0.05<=s['price']<=0.15),
        ('RSI<18', lambda s: s['rsi']<18),
        ('RSI<18 10-15¢', lambda s: s['rsi']<18 and 0.10<=s['price']<=0.15),
    ]:
        sub = [s for s in all_sig if check(s)]
        if sub: print(f"    {desc:18s}: n={len(sub):5d} WR={sum(s['won'] for s in sub)/len(sub)*100:.1f}%")
    
    results = {'version':'V18.3-vectorized','sample_per_hour':a.sample,
               'hours':len(valid),'total_signals':t,'wins':w,'win_rate':round(wr,1)}
    with open(out_dir/'v18_3_pmxt_vec_results.json','w') as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved -> {out_dir/'v18_3_pmxt_vec_results.json'}")


if __name__ == '__main__':
    main()