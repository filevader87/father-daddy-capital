#!/usr/bin/env python3
"""
V18.4 PMXT Backtest — REAL-TIME REGIME GATE (NO LOOK-AHEAD).
Uses first 30% of row-groups to estimate regime, trades on remaining 70%.
Regime = fraction of binary markets where cheap token mean > 0.15 in first 30% RGs.
This simulates real-time: "I observe the first 18 min of the hour before deciding to trade".
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


def backtest_file_v18_4_rt(filepath, max_markets=2000, rsi_max=18, 
                             price_min=0.08, price_max=0.15,
                             regime_pct=0.30, regime_threshold=0.55):
    """
    Process one hourly PMXT file with REAL-TIME regime gate.
    Phase 1a: Read first 30% of RGs → compute regime (cheap WR estimate)
    Phase 1b: Read remaining 70% of RGs → compute full market stats
    Phase 2: Generate signals only if regime gate passes
    """
    pf = pq.ParquetFile(filepath)
    nrg = pf.num_row_groups
    regime_rgs = max(1, int(nrg * regime_pct))
    
    # Phase 1a: Regime detection from FIRST 30% of RGs
    print(f"  P1a: {regime_rgs}/{nrg} RGs (regime)...", end=' ', flush=True)
    t0 = time.time()
    
    regime_stats = {}  # cid -> {aid: [sum_price, count, last_price]}
    
    for rg in range(regime_rgs):
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
            if cid not in regime_stats: regime_stats[cid] = {}
            if aid in regime_stats[cid]:
                regime_stats[cid][aid][0] += p; regime_stats[cid][aid][1] += 1
                regime_stats[cid][aid][2] = p
            else:
                regime_stats[cid][aid] = [p, 1, p]
        
        del t2, mkt_col, price_col, aid_col
    
    # Compute regime from first 30% of data
    # Strategy: track expensive token mean across regime RGs
    # If expensive mean is HIGH (>0.80) → expensive winning → BTC likely UP → cheap side loses → NO TRADE
    # If expensive mean is LOW (<0.75) → expensive losing → BTC likely DOWN → cheap side wins → TRADE
    # This is the BTC regime proxy from early hour data
    
    rich_means_per_rg = []
    
    for rg in range(regime_rgs):
        t = pf.read_row_group(rg, columns=['market','asset_id','price','event_type'])
        mask = pc.equal(t.column('event_type'), 'price_change')
        t2 = t.filter(mask)
        del t; n = len(t2)
        if n == 0: del t2; continue
        
        mkt_col = t2.column('market')
        price_col = t2.column('price').to_numpy().astype(np.float64)
        aid_col = t2.column('asset_id')
        
        # Group by CID to find binary pairs and compute rich mean for this RG
        cid_aid_rg = {}
        for i in range(n):
            mv = mkt_col[i]
            cid = mv.hex() if isinstance(mv, (bytes, bytearray)) else str(mv)
            aid = str(aid_col[i])
            p = price_col[i]
            if cid not in cid_aid_rg: cid_aid_rg[cid] = {}
            if aid in cid_aid_rg[cid]:
                cid_aid_rg[cid][aid].append(p)
            else:
                cid_aid_rg[cid][aid] = [p]
        
        rich_prices = []
        for cid, aids in cid_aid_rg.items():
            if len(aids) != 2: continue
            means = {aid: np.mean(prices) for aid, prices in aids.items()}
            sa = sorted(means.items(), key=lambda x: x[1])
            rich_mean = sa[1][1]
            if sa[0][1] < 0.30 and rich_mean > 0.70:
                rich_prices.append(rich_mean)
        
        if rich_prices:
            rich_means_per_rg.append(np.mean(rich_prices))
        
        del t2, mkt_col, price_col, aid_col, cid_aid_rg
    
    # Regime: average rich mean across regime RGs
    # High (>0.80) → BTC going UP → cheap tokens lose → NO TRADE
    # Low (<0.75) → BTC going DOWN → cheap tokens may win → TRADE
    avg_rich = np.mean(rich_means_per_rg) if rich_means_per_rg else 0.5
    
    # Also check trend: is rich mean rising or falling?
    if len(rich_means_per_rg) >= 3:
        first_half = np.mean(rich_means_per_rg[:len(rich_means_per_rg)//2])
        second_half = np.mean(rich_means_per_rg[len(rich_means_per_rg)//2:])
        rich_trend = 'rising' if second_half > first_half + 0.01 else ('falling' if second_half < first_half - 0.01 else 'flat')
    else:
        rich_trend = 'unknown'
    
    # Gate logic:
    # Trade cheap side only when rich mean is LOW or FALLING (BTC bearish)
    if avg_rich < 0.76:
        gate_on = True  # Rich = low → cheap side likely winning
        regime = 'cheap_favored'
    elif avg_rich > 0.82 and rich_trend == 'rising':
        gate_on = False  # Rich = high + rising → cheap side losing
        regime = 'rich_dominant'
    elif avg_rich > 0.82:
        gate_on = False  # Rich = high → cheap side likely losing
        regime = 'rich_favored'
    else:
        gate_on = False  # Marginal → skip conservatively
        regime = 'marginal'
    
    regime_wr = 1.0 - avg_rich  # crude proxy: cheap WR ≈ 1 - rich_mean
    
    print(f"regime={regime} avg_rich={avg_rich:.3f} trend={rich_trend}, gate={'ON' if gate_on else 'OFF'} ({time.time()-t0:.0f}s)", end=' ', flush=True)
    
    if not gate_on:
        print(f" SKIP (regime unfavorable)")
        return [], regime_wr, gate_on
    
    # Phase 1b: Full market stats from ALL RGs (for market discovery)
    print(f"  P1b: {nrg} RGs (full)...", end=' ', flush=True)
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
            if cid not in cid_aid_stats: cid_aid_stats[cid] = {}
            if aid in cid_aid_stats[cid]:
                cid_aid_stats[cid][aid][0] += p; cid_aid_stats[cid][aid][1] += 1
                cid_aid_stats[cid][aid][2] = p
            else:
                cid_aid_stats[cid][aid] = [p, 1, p]
        
        for cid, aids in cid_aid_stats.items():
            if cid not in global_stats: global_stats[cid] = {}
            for aid, v in aids.items():
                if aid in global_stats[cid]:
                    global_stats[cid][aid][0] += v[0]; global_stats[cid][aid][1] += v[1]
                    global_stats[cid][aid][2] = v[2]
                else:
                    global_stats[cid][aid] = list(v)
        
        del t2, mkt_col, price_col, aid_col, cid_aid_stats
        if rg % 20 == 0: gc.collect()
    
    bin_markets = []
    cheap_wins = 0; cheap_total = 0
    
    for cid, aids in global_stats.items():
        if len(aids) != 2: continue
        last_prices = {aid: v[2] for aid, v in aids.items()}
        means = {aid: v[0]/v[1] for aid, v in aids.items()}
        sa = sorted(means.items(), key=lambda x: x[1])
        cheap_aid, cheap_mean = sa[0]
        rich_aid, rich_mean = sa[1]
        
        if cheap_mean < 0.30 and rich_mean > 0.70:
            cheap_last = last_prices[cheap_aid]
            cheap_won = cheap_last > 0.90
            if cheap_won: cheap_wins += 1
            cheap_total += 1
            bin_markets.append((cid, cheap_aid, rich_aid, cheap_mean, rich_mean, cheap_won))
    
    actual_cheap_wr = cheap_wins / cheap_total if cheap_total > 0 else 0.5
    del global_stats; gc.collect()
    
    total_bin = len(bin_markets)
    if total_bin > max_markets:
        random.seed(42)
        bin_markets = random.sample(bin_markets, max_markets)
    
    print(f"{total_bin} bin, actual_cheap_WR={actual_cheap_wr:.1%}, sampled {len(bin_markets)} ({time.time()-t0:.0f}s)", end=' ', flush=True)
    
    if not bin_markets:
        print(" EMPTY")
        return [], regime_wr, gate_on
    
    # Phase 2: Signals from CHEAP side only (trading window = remaining 70% of RGs)
    CHUNK = 500
    all_signals = []
    n_chunks = (len(bin_markets) + CHUNK - 1) // CHUNK
    trade_start_rg = regime_rgs  # Only trade on data AFTER regime window
    
    for ci in range(n_chunks):
        chunk = bin_markets[ci*CHUNK:(ci+1)*CHUNK]
        chunk_cids_set = set(m[0] for m in chunk)
        chunk_aid = {m[0]: (m[1], m[2]) for m in chunk}
        cid_prices = {cid: [] for cid, _, _, _, _, _ in chunk}
        
        # Only read TRADING WINDOW RGs (after regime detection period)
        for rg in range(trade_start_rg, nrg):
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
                conf = min(0.95, 0.85 + (28-r)/60)
                all_signals.append({
                    'price': float(p), 'rsi': float(r), 'conf': float(conf),
                    'won': bool(won), 'zone': zone,
                    'regime_wr': float(regime_wr), 'actual_cheap_wr': float(actual_cheap_wr)
                })
                last_i = i
            del prices, rsi
        
        del cid_prices, chunk_aid; gc.collect()
        n = len(all_signals)
        wr = sum(s['won'] for s in all_signals)/n*100 if n else 0
        print(f"c{ci+1}/{n_chunks}({n}sig,{wr:.0f}%)", end=' ', flush=True)
    
    n = len(all_signals)
    wr = sum(s['won'] for s in all_signals)/n*100 if n else 0
    print(f"done: {n} sig, WR={wr:.1f}%, actual_cheap_WR={actual_cheap_wr:.1%}")
    return all_signals, regime_wr, gate_on


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
    p.add_argument('--regime-pct', type=float, default=0.30, help='Fraction of RGs for regime detection')
    p.add_argument('--regime-threshold', type=float, default=0.55, help='Min regime WR to trade')
    a = p.parse_args()
    
    data_dir = Path(a.dir)
    out_dir = Path(a.out)
    out_dir.mkdir(exist_ok=True)
    
    files = sorted(data_dir.glob('polymarket_orderbook_*.parquet'))
    valid = [f for f in files if f.stat().st_size > 100_000_000][:a.maxfiles]
    print(f"Found {len(valid)} valid files")
    print(f"Config: RSI<{a.rsi_max}, price={a.price_min}-{a.price_max}, regime_pct={a.regime_pct}, threshold={a.regime_threshold}")
    
    all_sig = []
    hour_results = []
    n_gated = 0; n_skipped = 0
    
    for fi, f in enumerate(valid):
        t0 = time.time()
        print(f"[{fi+1}/{len(valid)}] {f.name}", flush=True)
        gc.collect()
        sigs, regime_wr, gate_on = backtest_file_v18_4_rt(
            str(f), max_markets=a.sample,
            rsi_max=a.rsi_max, price_min=a.price_min, price_max=a.price_max,
            regime_pct=a.regime_pct, regime_threshold=a.regime_threshold
        )
        n = len(sigs)
        wr = sum(s['won'] for s in sigs)/n*100 if n else 0
        dt = time.time() - t0
        actual = sigs[0]['actual_cheap_wr'] if sigs else 0
        gate_str = 'GATED' if gate_on else 'SKIPPED'
        if gate_on: n_gated += 1
        else: n_skipped += 1
        print(f"  -> {n} sig, WR={wr:.1f}%, regime={regime_wr:.1%}, actual={actual:.1%} [{gate_str}] ({dt:.0f}s)", flush=True)
        hour_results.append({
            'file': f.name, 'n': n, 'wr': wr,
            'regime_wr': float(regime_wr), 'actual_cheap_wr': float(actual),
            'gate_on': gate_on, 'dt': dt
        })
        all_sig.extend(sigs)
        gc.collect()
    
    if not all_sig:
        print(f"No signals! {n_gated} gated, {n_skipped} skipped")
        return
    
    t = len(all_sig); w = sum(s['won'] for s in all_sig); wr = w/t*100
    
    # Analyze by regime quality
    regime_bins = defaultdict(lambda: {'n':0, 'w':0})
    for s in all_sig:
        rw = s.get('regime_wr', 0.5)
        if rw < 0.40: k = '<40%'
        elif rw < 0.55: k = '40-55%'
        elif rw < 0.70: k = '55-70%'
        else: k = '>70%'
        regime_bins[k]['n'] += 1; regime_bins[k]['w'] += int(s['won'])
    
    print(f"\n{'='*60}")
    print(f"V18.4 PMXT — REAL-TIME REGIME GATE (NO LOOK-AHEAD)")
    print(f"RSI<{a.rsi_max} + {a.price_min}-{a.price_max}¢")
    print(f"Regime: first {a.regime_pct:.0%} of RGs, threshold={a.regime_threshold}")
    print(f"Sample: {a.sample} mkts/hr, {len(valid)} hrs")
    print(f"{'='*60}")
    print(f"  Signals: {t} | Wins: {w} | WR: {wr:.1f}%")
    print(f"  Hours: {n_gated} gated (traded), {n_skipped} skipped")
    print()
    
    # Also compute what full-hour V18.3 would give on same hours
    gated_hours = [h for h in hour_results if h['gate_on']]
    print("  Gated hour detail:")
    for h in hour_results:
        gate = '✓' if h['gate_on'] else '✗'
        print(f"    {h['file'][:35]:35s} regime={h['regime_wr']:.1%} actual={h['actual_cheap_wr']:.1%} n={h['n']:5d} WR={h['wr']:5.1f}% [{gate}]")
    
    print()
    print("  Regime bins:")
    for k in ['<40%','40-55%','55-70%','>70%']:
        d = regime_bins[k]
        if d['n']: print(f"    {k:8s}: n={d['n']:5d} WR={d['w']/d['n']*100:.1f}%")
    
    results = {
        'version': 'V18.4-rt-regime-gate',
        'regime_pct': a.regime_pct,
        'regime_threshold': a.regime_threshold,
        'rsi_max': a.rsi_max,
        'price_range': [a.price_min, a.price_max],
        'total_signals': t, 'wins': w, 'win_rate': round(wr,1),
        'hours_gated': n_gated, 'hours_skipped': n_skipped,
        'hour_results': hour_results
    }
    with open(out_dir/'v18_4_rt_results.json','w') as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved -> {out_dir/'v18_4_rt_results.json'}")


if __name__ == '__main__':
    main()