#!/usr/bin/env python3
"""
V18.5 PMXT Backtest — EARLY PRICE TREND AS DIRECTION PREDICTOR.
Core question: Can the cheap token's price trajectory in the FIRST 30%
of the market's lifetime predict whether it will win?

If cheap token is TRENDING UP in early data → it's likely a DOWN market.
If cheap token is TRENDING DOWN in early data → it's an UP market.

This is the critical predictor for achieving 80%+ WR.
"""

import pyarrow.parquet as pq
import pyarrow.compute as pc
import numpy as np
import json
from pathlib import Path
import warnings; warnings.filterwarnings('ignore')
import gc, random, time
from collections import defaultdict


def backtest_file_v18_5(filepath, max_markets=2000, sample_seed=42):
    """Track cheap token early price trend → predict market direction."""
    fname = Path(filepath).stem.replace('polymarket_orderbook_', '')
    
    pf = pq.ParquetFile(filepath)
    nrg = pf.num_row_groups
    
    # Use first 30% of RGs for direction prediction, remaining 70% for trading
    obs_rgs = max(1, int(nrg * 0.3))
    trade_rgs = nrg - obs_rgs
    
    # Phase 1: Build global stats + detect direction from early RGs
    global_stats = {}
    early_prices = {}  # cid -> {aid: [(price, ts)]} from first 30% RGs
    
    for rg in range(obs_rgs):
        t = pf.read_row_group(rg, columns=['market','asset_id','price','event_type','timestamp_received'])
        mask = pc.equal(t.column('event_type'), 'price_change')
        t2 = t.filter(mask)
        del t; n = len(t2)
        if n == 0: del t2; continue
        
        mkt_col = t2.column('market')
        price_col = t2.column('price').to_numpy().astype(np.float64)
        aid_col = t2.column('asset_id')
        
        for i in range(n):
            mv = mkt_col[i]
            cid = mv.hex() if isinstance(mv, (bytes, bytearray)) else str(mv)
            aid = str(aid_col[i])
            p = float(price_col[i])
            
            if cid not in global_stats: global_stats[cid] = {}
            if aid in global_stats[cid]:
                global_stats[cid][aid][0] += p; global_stats[cid][aid][1] += 1
                global_stats[cid][aid][2] = p
            else:
                global_stats[cid][aid] = [p, 1, p]
            
            if cid not in early_prices: early_prices[cid] = {}
            if aid not in early_prices[cid]:
                early_prices[cid][aid] = [p]
            else:
                early_prices[cid][aid].append(p)
        
        del t2, mkt_col, price_col, aid_col
    
    # Find binary markets + determine direction
    bin_markets = []
    for cid, aids in global_stats.items():
        if len(aids) != 2: continue
        means = {aid: v[0]/v[1] for aid, v in aids.items()}
        last_prices = {aid: v[2] for aid, v in aids.items()}
        sa = sorted(means.items(), key=lambda x: x[1])
        cheap_aid, cheap_mean = sa[0]
        rich_aid, rich_mean = sa[1]
        if cheap_mean < 0.30 and rich_mean > 0.70:
            cheap_won = last_prices[cheap_aid] > 0.90
            
            # Direction from early price trend
            # If cheap token is trending UP in early RGs → likely DOWN market
            cheap_early = early_prices.get(cid, {}).get(cheap_aid, [])
            if len(cheap_early) > 20:
                # Simple trend: compare first quarter vs last quarter
                q = len(cheap_early) // 4
                if q > 2:
                    early_avg = np.mean(cheap_early[:q])
                    late_avg = np.mean(cheap_early[-q:])
                    trend = 'up' if late_avg > early_avg * 1.02 else ('down' if late_avg < early_avg * 0.98 else 'flat')
                else:
                    trend = 'flat'
            else:
                trend = 'flat'
            
            # Also check rich token trend
            rich_early = early_prices.get(cid, {}).get(rich_aid, [])
            if len(rich_early) > 20:
                q = len(rich_early) // 4
                if q > 2:
                    early_avg = np.mean(rich_early[:q])
                    late_avg = np.mean(rich_early[-q:])
                    rich_trend = 'up' if late_avg > early_avg * 1.02 else ('down' if late_avg < early_avg * 0.98 else 'flat')
                else:
                    rich_trend = 'flat'
            else:
                rich_trend = 'flat'
            
            bin_markets.append({
                'cid': cid, 'cheap_aid': cheap_aid, 'rich_aid': rich_aid,
                'cheap_mean': cheap_mean, 'rich_mean': rich_mean,
                'cheap_won': cheap_won, 'cheap_trend': trend, 'rich_trend': rich_trend,
                'n_early_cheap': len(cheap_early), 'n_early_rich': len(rich_early),
            })
    
    del global_stats, early_prices; gc.collect()
    
    # Stats
    total_bin = len(bin_markets)
    n_cheap_won = sum(1 for m in bin_markets if m['cheap_won'])
    
    # Breakdown by trend
    trend_stats = defaultdict(lambda: {'total': 0, 'won': 0})
    for m in bin_markets:
        # Cheap trending up → likely DOWN market
        if m['cheap_trend'] == 'up':
            trend_stats['cheap_up']['total'] += 1
            if m['cheap_won']: trend_stats['cheap_up']['won'] += 1
        elif m['cheap_trend'] == 'down':
            trend_stats['cheap_down']['total'] += 1
            if m['cheap_won']: trend_stats['cheap_down']['won'] += 1
        else:
            trend_stats['cheap_flat']['total'] += 1
            if m['cheap_won']: trend_stats['cheap_flat']['won'] += 1
    
    print(f"  P1: {obs_rgs} obs RGs + {trade_rgs} trade RGs → {total_bin} bin, cheap_WR={n_cheap_won/total_bin*100:.1f}%", flush=True)
    for label, stats in sorted(trend_stats.items()):
        if stats['total'] > 0:
            print(f"    {label}: {stats['total']} markets, {stats['won']/stats['total']*100:.1f}% cheap_won")
    
    if total_bin > max_markets:
        random.seed(sample_seed)
        bin_markets = random.sample(bin_markets, max_markets)
    
    sampled_cids = set(m['cid'] for m in bin_markets)
    cid_map = {m['cid']: m for m in bin_markets}
    
    # Phase 2: Generate signals from trade RGs
    all_signals = []
    
    for rg in range(obs_rgs, nrg):
        t = pf.read_row_group(rg, columns=['market','asset_id','price','event_type'])
        mask = pc.equal(t.column('event_type'), 'price_change')
        t2 = t.filter(mask)
        del t; n = len(t2)
        if n == 0: del t2; continue
        
        mkt_col = t2.column('market')
        price_col = t2.column('price').to_numpy().astype(np.float64)
        aid_col = t2.column('asset_id')
        
        rg_data = {}
        for i in range(n):
            mv = mkt_col[i]
            cid = mv.hex() if isinstance(mv, (bytes, bytearray)) else str(mv)
            if cid not in sampled_cids: continue
            aid = str(aid_col[i])
            p = float(price_col[i])
            if cid not in rg_data: rg_data[cid] = {}
            if aid not in rg_data[cid]:
                rg_data[cid][aid] = [p]
            else:
                rg_data[cid][aid].append(p)
        
        for cid, info in cid_map.items():
            if cid not in rg_data: continue
            
            # Only trade cheap token in markets where cheap is TRENDING UP (likely DOWN market)
            if info['cheap_trend'] == 'up':
                # Buy cheap token (predicted DOWN market)
                aid = info['cheap_aid']
                if aid in rg_data[cid] and len(rg_data[cid][aid]) > 5:
                    prices = sorted(rg_data[cid][aid])
                    entry_price = np.median(prices)  # median price as "entry"
                    if 0.03 < entry_price < 0.30:  # only trade cheap tokens
                        all_signals.append({
                            'entry_price': entry_price,
                            'won': info['cheap_won'],
                            'trend': info['cheap_trend'],
                            'side': 'cheap',
                            'market': 'predicted_DOWN',
                        })
            
            # Also test: buy rich token in markets where rich is TRENDING UP (likely UP market)  
            if info['rich_trend'] == 'up':
                aid = info['rich_aid']
                if aid in rg_data[cid] and len(rg_data[cid][aid]) > 5:
                    prices = sorted(rg_data[cid][aid])
                    entry_price = np.median(prices)
                    if 0.60 < entry_price < 0.95:
                        all_signals.append({
                            'entry_price': entry_price,
                            'won': info['rich_won'],
                            'trend': info['rich_trend'],
                            'side': 'rich',
                            'market': 'predicted_UP',
                        })
        
        del t2, mkt_col, price_col, aid_col, rg_data
    
    n = len(all_signals)
    if n == 0:
        print(f"  P2: 0 signals")
        return all_signals, total_bin
    
    wr = sum(s['won'] for s in all_signals)/n*100
    cheap_sigs = [s for s in all_signals if s['side'] == 'cheap']
    rich_sigs = [s for s in all_signals if s['side'] == 'rich']
    cheap_wr = sum(s['won'] for s in cheap_sigs)/len(cheap_sigs)*100 if cheap_sigs else 0
    rich_wr = sum(s['won'] for s in rich_sigs)/len(rich_sigs)*100 if rich_sigs else 0
    
    print(f"  P2: {n} sig, WR={wr:.1f}% (cheap={len(cheap_sigs)}/{cheap_wr:.0f}%, rich={len(rich_sigs)}/{rich_wr:.0f}%)")
    return all_signals, total_bin


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--dir', default='pmxt_data')
    p.add_argument('--out', default='backtest_results_v18_5')
    p.add_argument('--maxfiles', type=int, default=99)
    p.add_argument('--sample', type=int, default=500)
    a = p.parse_args()
    
    data_dir = Path(a.dir)
    files = sorted(data_dir.glob('polymarket_orderbook_*.parquet'))
    valid = [f for f in files if f.stat().st_size > 100_000_000][:a.maxfiles]
    print(f"Found {len(valid)} valid files")
    
    all_sig = []
    hour_results = []
    
    for fi, f in enumerate(valid):
        t0 = time.time()
        result = backtest_file_v18_5(str(f), max_markets=a.sample)
        sigs, total_bin = result
        if not sigs: continue
        
        n = len(sigs)
        wr = sum(s['won'] for s in sigs)/n*100 if n else 0
        
        cheap_sigs = [s for s in sigs if s['side'] == 'cheap']
        rich_sigs = [s for s in sigs if s['side'] == 'rich']
        
        dt = time.time() - t0
        print(f"  -> [{fi+1}/{len(valid)}] {f.name}: {n} sig, {wr:.1f}% WR "
              f"(cheap={len(cheap_sigs)}/{sum(s['won'] for s in cheap_sigs)/len(cheap_sigs)*100 if cheap_sigs else 0:.0f}%, "
              f"rich={len(rich_sigs)}/{sum(s['won'] for s in rich_sigs)/len(rich_sigs)*100 if rich_sigs else 0:.0f}%) ({dt:.0f}s)")
        all_sig.extend(sigs)
    
    if not all_sig:
        print("No signals!"); return
    
    t = len(all_sig)
    cheap_sigs = [s for s in all_sig if s['side'] == 'cheap']
    rich_sigs = [s for s in all_sig if s['side'] == 'rich']
    
    print(f"\n{'='*70}")
    print(f"V18.5 EARLY TREND PREDICTION RESULTS")
    print(f"{'='*70}")
    print(f"Total: {t} signals")
    if cheap_sigs:
        print(f"  Cheap (predicted DOWN): {len(cheap_sigs)} sig, "
              f"{sum(s['won'] for s in cheap_sigs)/len(cheap_sigs)*100:.1f}% WR")
    if rich_sigs:
        print(f"  Rich (predicted UP): {len(rich_sigs)} sig, "
              f"{sum(s['won'] for s in rich_sigs)/len(rich_sigs)*100:.1f}% WR")
    
    # By price tier
    print(f"\n  Cheap by price tier:")
    for tier, lo, hi in [('<5¢', 0.03, 0.05), ('5-10¢', 0.05, 0.10), ('10-20¢', 0.10, 0.20), ('20-30¢', 0.20, 0.30)]:
        sigs = [s for s in cheap_sigs if lo <= s['entry_price'] < hi]
        if len(sigs) > 0:
            wr = sum(s['won'] for s in sigs)/len(sigs)*100
            print(f"    {tier}: {len(sigs)} sig, {wr:.1f}% WR")
    
    print(f"\n  Rich by price tier:")
    for tier, lo, hi in [('60-70¢', 0.60, 0.70), ('70-80¢', 0.70, 0.80), ('80-90¢', 0.80, 0.90), ('90-95¢', 0.90, 0.95)]:
        sigs = [s for s in rich_sigs if lo <= s['entry_price'] < hi]
        if len(sigs) > 0:
            wr = sum(s['won'] for s in sigs)/len(sigs)*100
            print(f"    {tier}: {len(sigs)} sig, {wr:.1f}% WR")
    
    results = {'version': 'V18.5-trend-prediction', 'total_signals': t,
               'cheap_n': len(cheap_sigs), 'cheap_wr': sum(s['won'] for s in cheap_sigs)/len(cheap_sigs)*100 if cheap_sigs else 0,
               'rich_n': len(rich_sigs), 'rich_wr': sum(s['won'] for s in rich_sigs)/len(rich_sigs)*100 if rich_sigs else 0}
    out_dir = Path(a.out); out_dir.mkdir(exist_ok=True)
    with open(out_dir/'v18_5_trend.json', 'w') as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved -> {out_dir/'v18_5_trend.json'}")


if __name__ == '__main__':
    main()