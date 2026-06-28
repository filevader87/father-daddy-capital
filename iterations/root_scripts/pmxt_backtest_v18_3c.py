#!/usr/bin/env python3
"""
V18.3 PMXT Backtest — CHUNKED approach.
Phase 2: Process sampled markets in chunks of CHUNK_SIZE.
Each chunk: scan all RGs, collect data for N markets, compute signals, discard.
3 chunks of ~667 markets = 3 full RG scans instead of 2000.
"""

import pyarrow.parquet as pq
import numpy as np
import json
from pathlib import Path
import warnings; warnings.filterwarnings('ignore')
import gc, random, time

RSI_PERIOD = 14; MIN_CONF = 0.85; MAX_PRICE = 0.15; MIN_PRICE = 0.01
MAX_MARKETS_PER_HOUR = 2000
CHUNK_SIZE = 700  # markets per chunk

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
    """Compute V18.3 signals from a price array."""
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


def backtest_hour(filepath, max_markets=MAX_MARKETS_PER_HOUR):
    pf = pq.ParquetFile(filepath)
    nrg = pf.num_row_groups
    
    # Phase 1: Find binary CIDs + cheap/rich aid
    print(f"  P1: {nrg} RGs...", end=' ', flush=True)
    global_stats = {}
    for rg in range(nrg):
        t = pf.read_row_group(rg, columns=['market','asset_id','price','event_type'])
        arr_m = t.column('market').to_pylist()
        arr_a = t.column('asset_id').to_pylist()
        arr_p = t.column('price').to_pylist()
        arr_e = t.column('event_type').to_pylist()
        del t
        
        for i in range(len(arr_m)):
            if arr_e[i] != 'price_change': continue
            cid = arr_m[i].hex() if isinstance(arr_m[i], (bytes, bytearray)) else str(arr_m[i])
            aid = str(arr_a[i])
            p = float(arr_p[i])
            if cid not in global_stats: global_stats[cid] = {}
            if aid in global_stats[cid]:
                global_stats[cid][aid][0] += p; global_stats[cid][aid][1] += 1
            else:
                global_stats[cid][aid] = [p, 1]
        del arr_m, arr_a, arr_p, arr_e
    
    bin_markets = []
    for cid, aids in global_stats.items():
        if len(aids) != 2: continue
        means = {aid: v[0]/v[1] for aid, v in aids.items()}
        sa = sorted(means.items(), key=lambda x: x[1])
        cheap_aid, cheap_mean = sa[0]
        rich_aid, rich_mean = sa[1]
        if cheap_mean < 0.30 and rich_mean > 0.70:
            bin_markets.append((cid, cheap_aid, rich_aid, cheap_mean))
    
    del global_stats; gc.collect()
    
    total_bin = len(bin_markets)
    if total_bin > max_markets:
        random.seed(42)
        bin_markets = random.sample(bin_markets, max_markets)
    
    print(f"{total_bin} bin, sampled {len(bin_markets)}", end=' ', flush=True)
    
    if not bin_markets:
        return []
    
    # Phase 2: Process in chunks
    all_signals = []
    n_chunks = (len(bin_markets) + CHUNK_SIZE - 1) // CHUNK_SIZE
    
    for ci in range(n_chunks):
        chunk = bin_markets[ci*CHUNK_SIZE:(ci+1)*CHUNK_SIZE]
        chunk_cids = set(m[0] for m in chunk)
        
        # Build price series for this chunk only
        cid_prices = {}  # cid -> {'cheap': [(ts,p),...], 'rich': [(ts,p),...]}
        aid_map = {}     # cid -> (cheap_aid, rich_aid)
        for cid, ca, ra, _ in chunk:
            cid_prices[cid] = {'cheap': [], 'rich': []}
            aid_map[cid] = (ca, ra)
        
        # Scan all RGs, collect data for chunk markets only
        for rg in range(nrg):
            t = pf.read_row_group(rg, columns=['market','asset_id','price','event_type','timestamp_received'])
            arr_m = t.column('market').to_pylist()
            arr_a = t.column('asset_id').to_pylist()
            arr_p = t.column('price').to_pylist()
            arr_e = t.column('event_type').to_pylist()
            arr_ts = t.column('timestamp_received').to_pylist()
            del t
            
            # Only process rows for chunk CIDs
            for i in range(len(arr_m)):
                if arr_e[i] != 'price_change': continue
                cid = arr_m[i].hex() if isinstance(arr_m[i], (bytes, bytearray)) else str(arr_m[i])
                if cid not in chunk_cids: continue
                aid = str(arr_a[i])
                p = float(arr_p[i])
                ts = arr_ts[i]
                
                ca, ra = aid_map[cid]
                if aid == ca:
                    cid_prices[cid]['cheap'].append((ts, p))
                elif aid == ra:
                    cid_prices[cid]['rich'].append((ts, p))
            
            del arr_m, arr_a, arr_p, arr_e, arr_ts
        
        # Analyze chunk
        for cid, ca, ra, cheap_mean in chunk:
            # Cheap side
            cheap_data = cid_prices[cid]['cheap']
            if len(cheap_data) > RSI_PERIOD + 5:
                cheap_data.sort(key=lambda x: x[0])
                prices = np.array([p for _, p in cheap_data], dtype=float)
                all_signals.extend(analyze_signals(prices))
                del prices
            
            # Rich side (only if cheap enough to be in our range)
            if cheap_mean < 0.15:
                rich_data = cid_prices[cid]['rich']
                if len(rich_data) > RSI_PERIOD + 5:
                    rich_data.sort(key=lambda x: x[0])
                    prices = np.array([p for _, p in rich_data], dtype=float)
                    all_signals.extend(analyze_signals(prices))
                    del prices
        
        del cid_prices, aid_map
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
    
    results = {'version':'V18.3-chunked','sample_per_hour':a.sample,
               'hours':len(valid),'total_signals':t,'wins':w,'win_rate':round(wr,1)}
    with open(out_dir/'v18_3_pmxt_chunked_results.json','w') as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved -> {out_dir/'v18_3_pmxt_chunked_results.json'}")


if __name__ == '__main__':
    main()