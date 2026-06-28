#!/usr/bin/env python3
"""
V18.4c PMXT Backtest — PER-MARKET WITHIN-RG MOMENTUM.
Key insight: PMXT row groups contain DIFFERENT markets (no CID overlap).
Each RG has ~1M rows for ~30 markets, ~33K data points per market per RG.
Compute cheap-token price WITHIN each RG, then:
- If cheap token price is RISING within RG → momentum UP → enter
- If cheap token price is FALLING within RG → momentum DOWN → skip
Then check final settlement across ALL RGs.
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
    deltas = np.diff(prices, prepend=prices[0]).astype(float)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g = np.zeros(n, dtype=float); avg_l = np.zeros(n, dtype=float)
    avg_g[period] = np.mean(gains[1:period+1])
    avg_l[period] = np.mean(losses[1:period+1])
    for i in range(period+1, n):
        avg_g[i] = (avg_g[i-1]*(period-1) + gains[i]) / period
        avg_l[i] = (avg_l[i-1]*(period-1) + losses[i]) / period
    rs = np.where(avg_l > 0, avg_g / avg_l, 100.0)
    rsi = np.where(avg_l > 0, 100 - 100/(1+rs), 100.0)
    rsi[:period] = np.nan
    return rsi


def backtest_file_v18_4c(filepath, max_markets=2000, rsi_max=18,
                          price_min=0.08, price_max=0.15,
                          rising_only=True, sample_seed=42):
    """
    Process one hourly PMXT file.
    For each binary market in each RG:
    1. Compute cheap-token within-RG price series
    2. Check if cheap token is rising (momentum) — if rising_only, skip falling
    3. Check RSI<max at entry point
    4. Check price in range
    5. Determine win from final settlement price across ALL RGs
    """
    pf = pq.ParquetFile(filepath)
    nrg = pf.num_row_groups
    
    # Phase 1: Identify binary markets and get cheap/rich aid assignment + settlement
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
        for i in range(n):
            mv = mkt_col[i]
            cid = mv.hex() if isinstance(mv, (bytes, bytearray)) else str(mv)
            aid = str(aid_col[i])
            p = price_col[i]
            if cid not in global_stats: global_stats[cid] = {}
            if aid in global_stats[cid]:
                global_stats[cid][aid][0] += p; global_stats[cid][aid][1] += 1
                global_stats[cid][aid][2] = p  # last price
            else:
                global_stats[cid][aid] = [p, 1, p]
        del t2, mkt_col, price_col, aid_col
        if rg % 20 == 0: gc.collect()
    
    # Find binary markets
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
            bin_markets.append((cid, cheap_aid, rich_aid, cheap_mean, rich_mean, cheap_won))
    
    del global_stats; gc.collect()
    total_bin = len(bin_markets)
    n_win = sum(1 for m in bin_markets if m[5])
    cheap_wr = n_win / total_bin if total_bin else 0
    
    if total_bin > max_markets:
        random.seed(sample_seed)
        bin_markets = random.sample(bin_markets, max_markets)
    
    print(f"{total_bin} bin (cheap_WR={cheap_wr:.1%}), sampled {len(bin_markets)} ({time.time()-t0:.0f}s)", end=' ', flush=True)
    
    if not bin_markets:
        print(" EMPTY")
        return [], 0, 0, cheap_wr
    
    # Phase 2: Per-RG analysis — check cheap token trend WITHIN each RG
    # Since RGs contain different markets, we process EVERY RG and look for
    # our sampled markets within each RG
    print(f"  P2: {nrg} RGs (signals)...", end=' ', flush=True)
    
    sampled_cids = set(m[0] for m in bin_markets)
    aid_map = {m[0]: (m[1], m[2]) for m in bin_markets}
    cid_won = {m[0]: m[5] for m in bin_markets}
    
    all_signals = []
    rising_count = 0
    falling_count = 0
    flat_count = 0
    
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
        
        # Group by CID within this RG
        rg_data = {}  # cid -> {aid: [(ts, price)]}
        for i in range(n):
            mv = mkt_col[i]
            cid = mv.hex() if isinstance(mv, (bytes, bytearray)) else str(mv)
            if cid not in sampled_cids: continue
            aid = str(aid_col[i])
            p = float(price_col[i])
            ts = ts_col[i].as_py()
            if cid not in rg_data: rg_data[cid] = {}
            if aid not in rg_data[cid]:
                rg_data[cid][aid] = [(ts, p)]
            else:
                rg_data[cid][aid].append((ts, p))
        
        # For each CID in this RG, compute within-RG cheap token trend
        for cid, aid_data in rg_data.items():
            cheap_aid = aid_map[cid][0]
            if cheap_aid not in aid_data: continue
            
            cheap_series = sorted(aid_data[cheap_aid], key=lambda x: x[0])
            if len(cheap_series) < 20: continue  # Need minimum data points
            
            prices = np.array([p for _, p in cheap_series], dtype=float)
            rsi = compute_rsi(prices, RSI_PERIOD)
            
            # Compute within-RG price trend (first half vs second half)
            half = len(prices) // 2
            first_half_mean = np.mean(prices[:half])
            second_half_mean = np.mean(prices[half:])
            within_trend = second_half_mean - first_half_mean
            
            if within_trend > 0.001:
                momentum = 'rising'
                rising_count += 1
            elif within_trend < -0.001:
                momentum = 'falling'
                falling_count += 1
            else:
                momentum = 'flat'
                flat_count += 1
            
            # Generate signals
            won = cid_won[cid]
            last_i = -999
            for i in range(RSI_PERIOD + 1, len(prices)):
                if i - last_i < 20: continue
                p = prices[i]
                if p < price_min or p > price_max: continue
                r = rsi[i]
                if np.isnan(r): continue
                if r >= rsi_max: continue
                
                if rising_only and momentum != 'rising':
                    continue  # Skip non-rising markets
                
                all_signals.append({
                    'price': float(p), 'rsi': float(r),
                    'won': bool(won), 'zone': f'RSI<{rsi_max}',
                    'momentum': momentum,
                    'within_trend': float(within_trend)
                })
                last_i = i
        
        del t2, mkt_col, price_col, aid_col, ts_col, rg_data
    
    n = len(all_signals)
    wr = sum(s['won'] for s in all_signals)/n*100 if n else 0
    print(f"sig={n} WR={wr:.1f}% (R={rising_count} F={falling_count} flat={flat_count}) ({time.time()-t0:.0f}s)")
    return all_signals, rising_count, falling_count, cheap_wr


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--dir', default='pmxt_data')
    p.add_argument('--out', default='backtest_results_v18_4')
    p.add_argument('--maxfiles', type=int, default=99)
    p.add_argument('--sample', type=int, default=2000)
    p.add_argument('--rsi-max', type=int, default=18)
    p.add_argument('--price-min', type=float, default=0.08)
    p.add_argument('--price-max', type=float, default=0.15)
    p.add_argument('--rising-only', action='store_true', default=True)
    p.add_argument('--all-momentum', action='store_true', default=False)
    a = p.parse_args()
    
    rising_only = not a.all_momentum
    
    data_dir = Path(a.dir)
    out_dir = Path(a.out)
    out_dir.mkdir(exist_ok=True)
    
    files = sorted(data_dir.glob('polymarket_orderbook_*.parquet'))
    valid = [f for f in files if f.stat().st_size > 100_000_000][:a.maxfiles]
    print(f"Found {len(valid)} valid files")
    print(f"Config: RSI<{a.rsi_max}, {a.price_min}-{a.price_max}¢, rising_only={rising_only}")
    
    all_sig = []
    hour_results = []
    
    for fi, f in enumerate(valid):
        t0 = time.time()
        print(f"[{fi+1}/{len(valid)}] {f.name}", flush=True)
        gc.collect()
        sigs, n_rise, n_fall, cheap_wr = backtest_file_v18_4c(
            str(f), max_markets=a.sample,
            rsi_max=a.rsi_max, price_min=a.price_min, price_max=a.price_max,
            rising_only=rising_only
        )
        n = len(sigs)
        wr = sum(s['won'] for s in sigs)/n*100 if n else 0
        dt = time.time() - t0
        print(f"  -> {n} sig, WR={wr:.1f}%, rising={n_rise}, falling={n_fall}, cheap_wr={cheap_wr:.1%} ({dt:.0f}s)", flush=True)
        hour_results.append({
            'file': f.name, 'n': n, 'wr': wr,
            'rising': n_rise, 'falling': n_fall,
            'cheap_wr': float(cheap_wr), 'dt': dt
        })
        all_sig.extend(sigs)
        gc.collect()
    
    if not all_sig:
        print("No signals!")
        return
    
    t = len(all_sig); w = sum(s['won'] for s in all_sig); wr = w/t*100
    
    # Momentum breakdown
    mom_bins = defaultdict(lambda: {'n':0, 'w':0})
    for s in all_sig:
        mom_bins[s['momentum']]['n'] += 1
        mom_bins[s['momentum']]['w'] += int(s['won'])
    
    print(f"\n{'='*60}")
    print(f"V18.4c PMXT — WITHIN-RG MOMENTUM {'(RISING ONLY)' if rising_only else '(ALL)'}")
    print(f"RSI<{a.rsi_max} + {a.price_min}-{a.price_max}¢")
    print(f"Sample: {a.sample} mkts/hr, {len(valid)} hrs")
    print(f"{'='*60}")
    print(f"  Signals: {t} | Wins: {w} | WR: {wr:.1f}%")
    print()
    print("  Momentum breakdown:")
    for mom in ['rising', 'falling', 'flat']:
        d = mom_bins[mom]
        if d['n']: print(f"    {mom:8s}: n={d['n']:5d} WR={d['w']/d['n']*100:.1f}%")
    
    print()
    print("  Hour-by-hour:")
    for hr in hour_results:
        print(f"    {hr['file'][:35]:35s} R={hr['rising']:>4d} F={hr['falling']:>4d} n={hr['n']:5d} WR={hr['wr']:5.1f}%")
    
    results = {
        'version': 'V18.4c-within-rg-momentum',
        'rising_only': rising_only,
        'rsi_max': a.rsi_max,
        'price_range': [a.price_min, a.price_max],
        'total_signals': t, 'wins': w, 'win_rate': round(wr,1),
        'hour_results': hour_results
    }
    with open(out_dir/'v18_4c_results.json','w') as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved -> {out_dir/'v18_4c_results.json'}")


if __name__ == '__main__':
    main()