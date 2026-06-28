#!/usr/bin/env python3
"""
V18.3 PMXT Backtest — SAMPLED approach.
To avoid OOM: randomly sample binary markets per hour.
Still statistically valid with enough samples.
"""

import pyarrow.parquet as pq
import numpy as np
import json
from pathlib import Path
from datetime import datetime
import warnings; warnings.filterwarnings('ignore')
import gc, random

RSI_PERIOD = 14; MIN_CONF = 0.85; MAX_PRICE = 0.15; MIN_PRICE = 0.01
MAX_MARKETS_PER_HOUR = 2000  # Sample at most this many binary markets

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


def backtest_hour(filepath, max_markets=MAX_MARKETS_PER_HOUR):
    """Process one hourly Parquet with memory limit."""
    import pandas as pd
    from collections import defaultdict
    
    pf = pq.ParquetFile(filepath)
    nrg = pf.num_row_groups
    
    # Phase 1: Find binary CIDs and their cheap-side asset IDs
    print(f"  Phase 1: {nrg} RGs...", end=' ', flush=True)
    
    # Accumulate sum/count per (cid, aid)
    global_stats = {}  # cid -> {aid: [sum, count]}
    
    for rg in range(nrg):
        t = pf.read_row_group(rg, columns=['market','asset_id','price','event_type'])
        df = t.to_pandas()
        pc = df[df['event_type'] == 'price_change'].copy()
        if pc.empty:
            del df, t; continue
        
        pc['cid'] = pc['market'].apply(lambda r: r.hex() if isinstance(r, (bytes, bytearray)) else str(r))
        pc['aid'] = pc['asset_id'].astype(str)
        pc['pf'] = pc['price'].astype(float)
        
        for (cid, aid), grp in pc.groupby(['cid','aid']):
            s = grp['pf'].sum()
            c = len(grp)
            if cid not in global_stats:
                global_stats[cid] = {}
            if aid in global_stats[cid]:
                global_stats[cid][aid][0] += s
                global_stats[cid][aid][1] += c
            else:
                global_stats[cid][aid] = [s, c]
        
        del df, t, pc
    
    # Find binary CIDs with cheap side
    bin_markets = []  # [(cid, cheap_aid, rich_aid, cheap_mean)]
    for cid, aids in global_stats.items():
        if len(aids) != 2: continue
        items = list(aids.items())
        means = {aid: s_c[0]/s_c[1] for aid, s_c in items}
        sorted_aids = sorted(means.items(), key=lambda x: x[1])
        cheap_aid, cheap_mean = sorted_aids[0]
        rich_aid, rich_mean = sorted_aids[1]
        if cheap_mean < 0.30 and rich_mean > 0.70:
            bin_markets.append((cid, cheap_aid, rich_aid, cheap_mean))
    
    del global_stats; gc.collect()
    
    # Sample if too many
    total_bin = len(bin_markets)
    if total_bin > max_markets:
        random.seed(42)
        bin_markets = random.sample(bin_markets, max_markets)
    
    print(f"{total_bin} binary, sampling {len(bin_markets)}", end=' ', flush=True)
    
    if not bin_markets:
        return []
    
    # Phase 2: Collect price series for sampled markets only
    # Key: only store data for sampled CIDs
    sampled_cids = set(m[0] for m in bin_markets)
    cid_prices = {}  # cid -> {aid: [(ts, price), ...]}
    for cid, cheap_aid, rich_aid, _ in bin_markets:
        cid_prices[cid] = {cheap_aid: [], rich_aid: []}
    
    for rg in range(nrg):
        t = pf.read_row_group(rg, columns=['market','asset_id','price','event_type','timestamp_received'])
        df = t.to_pandas()
        pc = df[df['event_type'] == 'price_change'].copy()
        if pc.empty:
            del df, t; continue
        
        pc['cid'] = pc['market'].apply(lambda r: r.hex() if isinstance(r, (bytes, bytearray)) else str(r))
        pc_bin = pc[pc['cid'].isin(sampled_cids)].copy()
        
        if pc_bin.empty:
            del df, t, pc; continue
        
        pc_bin['aid'] = pc_bin['asset_id'].astype(str)
        pc_bin['pf'] = pc_bin['price'].astype(float)
        
        # Use numpy groupby for speed
        for (cid, aid), grp in pc_bin.groupby(['cid','aid']):
            if cid in cid_prices and aid in cid_prices[cid]:
                ts = grp['timestamp_received'].values.tolist()
                ps = grp['pf'].values.tolist()
                cid_prices[cid][aid].extend(zip(ts, ps))
        
        del df, t, pc
    
    print(f"analyzing...", end=' ', flush=True)
    
    # Phase 3: RSI analysis per sampled market
    signals = []
    for cid, cheap_aid, rich_aid, _ in bin_markets:
        for aid in [cheap_aid, rich_aid]:
            data = cid_prices.get(cid, {}).get(aid, [])
            if len(data) < RSI_PERIOD + 5: continue
            
            data.sort(key=lambda x: x[0])
            prices = np.array([p for _, p in data], dtype=float)
            del data
            
            final_price = prices[-1]
            won = final_price > 0.90
            
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
                else:
                    continue  # RSI 35+ dead zone
                
                if conf >= MIN_CONF:
                    signals.append({'price': float(p), 'rsi': float(r),
                                   'conf': float(conf), 'won': bool(won), 'zone': zone})
                    last_i = i
            
            del prices, rsi
    
    del cid_prices; gc.collect()
    return signals


def main():
    import argparse, time
    p = argparse.ArgumentParser()
    p.add_argument('--dir', default='pmxt_data')
    p.add_argument('--out', default='backtest_results_v18_3')
    p.add_argument('--maxfiles', type=int, default=99)
    p.add_argument('--sample', type=int, default=2000, help='Max markets per hour')
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
        sigs = backtest_hour(f, max_markets=a.sample)
        n = len(sigs)
        wr = sum(s['won'] for s in sigs)/n*100 if n else 0
        dt = time.time() - t0
        print(f"  → {n} sig, WR={wr:.1f}% ({dt:.0f}s)", flush=True)
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
    print(f"Sample: {a.sample} markets/hour, {len(valid)} hours")
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
    
    print(f"\n  V18.2 MC: 84.6% | V18.3 MC: 77.3% | V18.3 PMXT: {wr:.1f}%")
    
    results = {'version':'V18.3-sampled','sample_per_hour':a.sample,
               'hours':len(valid),'total_signals':t,'wins':w,'win_rate':round(wr,1)}
    with open(out_dir/'v18_3_pmxt_results.json','w') as f:
        json.dump(results, f, indent=2)
    print(f"Saved → {out_dir/'v18_3_pmxt_results.json'}")


if __name__ == '__main__':
    main()