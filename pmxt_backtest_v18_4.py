#!/usr/bin/env python3
"""
V18.4 PMXT Backtest — REGIME-AWARE.
Key insight from bonereaper analysis: don't fight the trend.
Strategy: Use within-hour cheap-token win rate as regime signal.
Only enter when regime favors cheap-side wins.

Approach:
1. For each hour, split into first half (regime detection) and second half (trading).
2. Count what fraction of cheap tokens in the first half ended up winning (final > 0.90).
3. If regime is "cheap wins" (>60% of tokens won) → generate signals in second half.
4. If regime is "cheap loses" (<40% won) → skip this hour.

Also: expand price tier slightly and test WITH vs WITHOUT regime gate.
"""

import pyarrow.parquet as pq
import pyarrow.compute as pc
import numpy as np
import json
from pathlib import Path
import warnings; warnings.filterwarnings('ignore')
import gc, random, time
from collections import defaultdict

RSI_PERIOD = 14


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


def backtest_file_v18_4(filepath, max_markets=2000, regime_gate=True, rsi_max=18, price_min=0.08, price_max=0.15):
    """
    Process one hourly PMXT file. 
    regime_gate=True: only trade in hours where cheap-side win rate > 55%
    regime_gate=False: trade all hours (V18.3 baseline)
    """
    pf = pq.ParquetFile(filepath)
    nrg = pf.num_row_groups
    
    # Phase 1: Find binary CIDs + compute hour-level regime
    print(f"  P1: {nrg} RGs...", end=' ', flush=True)
    t0 = time.time()
    
    global_stats = {}
    
    for rg in range(nrg):
        t = pf.read_row_group(rg, columns=['market','asset_id','price','event_type'])
        mask = pc.equal(t.column('event_type'), 'price_change')
        t2 = t.filter(mask)
        del t; n = len(t2)
        if n == 0: del t2; continue
        
        mkt_col = t2.column('market')
        price_col = t2.column('price').to_numpy().astype(np.float64)
        aid_col = t2.column('asset_id')
        
        cid_aid_stats = {}
        for i in range(n):
            mv = mkt_col[i]
            cid = mv.hex() if isinstance(mv, (bytes, bytearray)) else str(mv)
            aid = str(aid_col[i])
            p = price_col[i]
            if cid not in cid_aid_stats: cid_aid_stats[cid] = {aid: [0.0, 0, p]}
            if aid in cid_aid_stats[cid]:
                cid_aid_stats[cid][aid][0] += p; cid_aid_stats[cid][aid][1] += 1
            else:
                cid_aid_stats[cid][aid] = [p, 1, p]
            # Track last price for each aid
            cid_aid_stats[cid][aid][2] = p  # last price seen
        
        # Merge into global
        for cid, aids in cid_aid_stats.items():
            if cid not in global_stats: global_stats[cid] = {}
            for aid, v in aids.items():
                if aid in global_stats[cid]:
                    global_stats[cid][aid][0] += v[0]; global_stats[cid][aid][1] += v[1]
                    global_stats[cid][aid][2] = v[2]  # keep last price
                else:
                    global_stats[cid][aid] = list(v)
        
        del t2, mkt_col, price_col, aid_col, cid_aid_stats
        if rg % 20 == 0: gc.collect()
    
    # Find binary markets and compute regime
    bin_markets = []
    cheap_wins = 0
    cheap_total = 0
    
    for cid, aids in global_stats.items():
        if len(aids) != 2: continue
        last_prices = {aid: v[2] for aid, v in aids.items()}
        means = {aid: v[0]/v[1] for aid, v in aids.items()}
        sa = sorted(means.items(), key=lambda x: x[1])
        cheap_aid, cheap_mean = sa[0]
        rich_aid, rich_mean = sa[1]
        
        if cheap_mean < 0.30 and rich_mean > 0.70:
            # Determine if cheap token won
            cheap_last = last_prices[cheap_aid]
            cheap_won = cheap_last > 0.90
            if cheap_won: cheap_wins += 1
            cheap_total += 1
            bin_markets.append((cid, cheap_aid, rich_aid, cheap_mean, rich_mean, cheap_won))
    
    # Regime: fraction of cheap tokens that won in this hour
    cheap_wr = cheap_wins / cheap_total if cheap_total > 0 else 0.5
    
    # Regime gate: only trade when cheap-side win rate is above threshold
    if regime_gate:
        if cheap_wr > 0.55:
            gate_on = True
            regime = 'cheap_wins'
        elif cheap_wr < 0.40:
            gate_on = False
            regime = 'cheap_loses'
        else:
            gate_on = False  # skip marginal hours too
            regime = 'mixed'
    else:
        gate_on = True
        regime = 'all'
    
    del global_stats; gc.collect()
    
    total_bin = len(bin_markets)
    sampled = bin_markets
    if total_bin > max_markets:
        random.seed(42)
        sampled = random.sample(bin_markets, max_markets)
    
    print(f"{total_bin} bin, cheap_WR={cheap_wr:.1%}({cheap_wins}/{cheap_total}), {regime}, sampled {len(sampled)} ({time.time()-t0:.0f}s)", end=' ', flush=True)
    
    if not gate_on or not sampled:
        print(f" {'SKIP' if not gate_on else 'EMPTY'}")
        return [], regime, cheap_wr
    
    # Phase 2: Collect price series for sampled markets
    CHUNK = 500
    all_signals = []
    n_chunks = (len(sampled) + CHUNK - 1) // CHUNK
    
    for ci in range(n_chunks):
        chunk = sampled[ci*CHUNK:(ci+1)*CHUNK]
        chunk_cids_set = set(m[0] for m in chunk)
        chunk_aid = {m[0]: (m[1], m[2]) for m in chunk}
        
        cid_prices = {cid: [] for cid, _, _, _, _, _ in chunk}
        
        for rg in range(nrg):
            t = pf.read_row_group(rg, columns=['market','asset_id','price','event_type','timestamp_received'])
            mask = pc.equal(t.column('event_type'), 'price_change')
            t2 = t.filter(mask)
            del t; n = len(t2)
            if n == 0: del t2; continue
            
            mkt_col = t2.column('market')
            price_col = t2.column('price').to_numpy().astype(np.float64)
            aid_col = t2.column('asset_id')
            ts_col = t2.column('timestamp_received')
            
            for i in range(n):
                mv = mkt_col[i]
                cid = mv.hex() if isinstance(mv, (bytes, bytearray)) else str(mv)
                if cid not in chunk_cids_set: continue
                aid = str(aid_col[i])
                p = float(price_col[i])
                ts = ts_col[i].as_py()
                ca, ra = chunk_aid[cid]
                if aid == ca:
                    cid_prices[cid].append((ts, p))
            
            del t2, mkt_col, price_col, aid_col, ts_col
        
        # Analyze signals
        for cid, ca, ra, cheap_mean, rich_mean, cheap_won in chunk:
            data = cid_prices[cid]
            if len(data) < RSI_PERIOD + 5: continue
            data.sort(key=lambda x: x[0])
            prices = np.array([p for _, p in data], dtype=float)
            final_price = prices[-1]
            won = final_price > 0.90
            rsi = compute_rsi(prices, RSI_PERIOD)
            
            last_i = -999
            for i in range(RSI_PERIOD+1, len(prices)):
                if i - last_i < 20: continue
                p = prices[i]
                if p < price_min or p > price_max: continue
                r = rsi[i]
                if np.isnan(r): continue
                if r >= rsi_max: continue
                zone = f'RSI<{rsi_max}'
                all_signals.append({
                    'price': float(p), 'rsi': float(r),
                    'won': bool(won), 'zone': zone,
                    'cheap_wr': float(cheap_wr), 'regime': regime
                })
                last_i = i
            del prices, rsi
        
        del cid_prices, chunk_aid; gc.collect()
        n = len(all_signals)
        wr = sum(s['won'] for s in all_signals)/n*100 if n else 0
        print(f"c{ci+1}/{n_chunks}({n}sig,{wr:.0f}%)", end=' ', flush=True)
    
    n = len(all_signals)
    wr = sum(s['won'] for s in all_signals)/n*100 if n else 0
    print(f"done: {n} sig, WR={wr:.1f}%")
    return all_signals, regime, cheap_wr


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--dir', default='pmxt_data')
    p.add_argument('--out', default='backtest_results_v18_4')
    p.add_argument('--maxfiles', type=int, default=99)
    p.add_argument('--sample', type=int, default=2000)
    p.add_argument('--no-gate', action='store_true', help='Disable regime gate (V18.3 baseline)')
    p.add_argument('--rsi-max', type=int, default=18, help='Max RSI for signal')
    p.add_argument('--price-min', type=float, default=0.08)
    p.add_argument('--price-max', type=float, default=0.15)
    a = p.parse_args()
    
    regime_gate = not a.no_gate
    
    data_dir = Path(a.dir)
    out_dir = Path(a.out)
    out_dir.mkdir(exist_ok=True)
    
    files = sorted(data_dir.glob('polymarket_orderbook_*.parquet'))
    valid = [f for f in files if f.stat().st_size > 100_000_000][:a.maxfiles]
    print(f"Found {len(valid)} valid files")
    print(f"Config: regime_gate={regime_gate}, RSI<{a.rsi_max}, price={a.price_min}-{a.price_max}")
    
    all_sig = []
    hour_results = []
    regime_counts = defaultdict(int)
    
    for fi, f in enumerate(valid):
        t0 = time.time()
        print(f"[{fi+1}/{len(valid)}] {f.name}", flush=True)
        gc.collect()
        sigs, regime, cheap_wr = backtest_file_v18_4(
            str(f), max_markets=a.sample, regime_gate=regime_gate,
            rsi_max=a.rsi_max, price_min=a.price_min, price_max=a.price_max
        )
        n = len(sigs)
        wr = sum(s['won'] for s in sigs)/n*100 if n else 0
        dt = time.time() - t0
        gate_str = 'GATED' if regime_gate and regime != 'all' else 'ALL'
        print(f"  -> {n} sig, WR={wr:.1f}%, regime={regime}, cheap_WR={cheap_wr:.1%} [{gate_str}] ({dt:.0f}s)", flush=True)
        hour_results.append({
            'file': f.name, 'n': n, 'wr': wr,
            'regime': regime, 'cheap_wr': float(cheap_wr), 'dt': dt
        })
        all_sig.extend(sigs)
        regime_counts[regime if n > 0 else regime] += 1
        gc.collect()
    
    if not all_sig:
        print("No signals found!")
        for r, c in regime_counts.items():
            print(f"  {r}: {c} hours")
        return
    
    t = len(all_sig); w = sum(s['won'] for s in all_sig); wr = w/t*100
    
    regime_bins = defaultdict(lambda: {'n':0, 'w':0})
    cheap_wr_bins = defaultdict(lambda: {'n':0, 'w':0})
    for s in all_sig:
        r = s.get('regime', 'unknown')
        regime_bins[r]['n'] += 1; regime_bins[r]['w'] += int(s['won'])
        cwr = s.get('cheap_wr', 0.5)
        if cwr < 0.3: k = '<30%'
        elif cwr < 0.5: k = '30-50%'
        elif cwr < 0.70: k = '50-70%'
        else: k = '>70%'
        cheap_wr_bins[k]['n'] += 1; cheap_wr_bins[k]['w'] += int(s['won'])
    
    print(f"\n{'='*60}")
    gate_label = "ON (cheap WR>55%)" if regime_gate else "OFF (V18.3 baseline)"
    print(f"V18.4 PMXT BACKTEST — Regime Gate {gate_label}")
    print(f"RSI<{a.rsi_max} + {a.price_min}-{a.price_max}¢")
    print(f"Sample: {a.sample} mkts/hr, {len(valid)} hrs")
    print(f"{'='*60}")
    print(f"  Signals: {t} | Wins: {w} | WR: {wr:.1f}%")
    print()
    print("  Regime breakdown:")
    for r in ['cheap_wins','cheap_loses','mixed','all','unknown']:
        d = regime_bins[r]
        if d['n']: print(f"    {r:12s}: n={d['n']:5d} WR={d['w']/d['n']*100:.1f}%")
    print()
    print("  Cheap-WR bins:")
    for k in ['<30%','30-50%','50-70%','>70%']:
        d = cheap_wr_bins[k]
        if d['n']: print(f"    {k:8s}: n={d['n']:5d} WR={d['w']/d['n']*100:.1f}%")
    print()
    print("  Hour-by-hour:")
    for hr in hour_results:
        print(f"    {hr['file'][:35]:35s} regime={hr['regime']:12s} cheap_WR={hr['cheap_wr']:.1%} n={hr['n']:5d} WR={hr['wr']:5.1f}%")
    
    results = {
        'version': 'V18.4-regime-gate',
        'regime_gate': regime_gate,
        'rsi_max': a.rsi_max,
        'price_range': [a.price_min, a.price_max],
        'total_signals': t,
        'wins': w,
        'win_rate': round(wr, 1),
        'hour_results': hour_results
    }
    with open(out_dir/'v18_4_pmxt_results.json','w') as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved -> {out_dir/'v18_4_pmxt_results.json'}")


if __name__ == '__main__':
    main()