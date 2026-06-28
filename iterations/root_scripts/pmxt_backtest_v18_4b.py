#!/usr/bin/env python3
"""
V18.4b PMXT Backtest — PER-MARKET MOMENTUM GATE.
Bonereaper insight: buy cheap tokens that are TRENDING UP (winning side).
Skip cheap tokens that are trending down (losing side).

Method:
1. Split hour into first 30% (observation) and remaining 70% (trading).
2. For each binary market, observe cheap token price trend in first 30%.
3. Only trade cheap tokens where early price trend is UP or STABLE.
4. Apply RSI<18 + 8-15¢ filter on the trading window data.
5. This simulates: "I watch the first 18 min, see which cheap tokens are being bought,
   then enter those tokens when they dip (RSI oversold) in the remaining 42 min."
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
    avg_l = np.mean(losses[1:period+1])
    for i in range(period+1, n):
        avg_g[i] = (avg_g[i-1]*(period-1) + gains[i]) / period
        avg_l[i] = (avg_l[i-1]*(period-1) + losses[i]) / period
    rs = np.where(avg_l > 0, avg_g / avg_l, 100.0)
    rsi = np.where(avg_l > 0, 100 - 100/(1+rs), 100.0)
    rsi[:period] = np.nan
    return rsi


def compute_trend(prices):
    """Compute price trend: positive = rising, negative = falling."""
    if len(prices) < 5: return 0.0
    # Linear regression slope, normalized by mean price
    x = np.arange(len(prices))
    y = np.array(prices)
    slope = np.polyfit(x, y, 1)[0]
    mean_p = np.mean(y)
    return slope / mean_p if mean_p > 0 else 0.0


def backtest_file_v18_4b(filepath, max_markets=2000, rsi_max=18,
                           price_min=0.08, price_max=0.15,
                           obs_pct=0.30, trend_threshold=0.0):
    """
    Process one hourly PMXT file with PER-MARKET momentum gate.
    obs_pct: fraction of RGs for observation (trend detection)
    trend_threshold: min trend slope to consider 'rising' (0.0 = flat or rising)
    """
    pf = pq.ParquetFile(filepath)
    nrg = pf.num_row_groups
    obs_rgs = max(1, int(nrg * obs_pct))
    
    # Phase 1a: Observe cheap token trends in first 30% of RGs
    # First pass: collect global market/aid stats to identify cheap vs rich
    print(f"  P1a: {obs_rgs}/{nrg} RGs (identify + observe)...", end=' ', flush=True)
    t0 = time.time()
    
    # Two-pass in observation window:
    # Pass 1: Identify binary CIDs and their cheap/rich aid assignment
    # Pass 2: Collect per-RG cheap token prices for trend computation
    
    # Pass 1: Identify binary markets from ALL observation RGs
    obs_global = {}
    for rg in range(obs_rgs):
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
            if cid not in obs_global: obs_global[cid] = {}
            if aid in obs_global[cid]:
                obs_global[cid][aid][0] += p; obs_global[cid][aid][1] += 1
            else:
                obs_global[cid][aid] = [p, 1]
        
        del t2, mkt_col, price_col, aid_col
    
    # Identify binary CIDs with consistent cheap/rich aid assignment
    bin_markets = {}  # cid -> (cheap_aid, rich_aid, cheap_mean, rich_mean)
    for cid, aids in obs_global.items():
        if len(aids) != 2: continue
        means = {aid: v[0]/v[1] for aid, v in aids.items()}
        sa = sorted(means.items(), key=lambda x: x[1])
        cheap_aid = sa[0][0]; cheap_mean = sa[0][1]
        rich_aid = sa[1][0]; rich_mean = sa[1][1]
        if cheap_mean < 0.30 and rich_mean > 0.70:
            bin_markets[cid] = (cheap_aid, rich_aid, cheap_mean, rich_mean)
    
    print(f"  DEBUG: bin_markets={len(bin_markets)}, obs_global_cids={len(obs_global)}, with_2aids={sum(1 for a in obs_global.values() if len(a)==2)}", end=' ', flush=True)
    del obs_global; gc.collect()
    
    # Pass 2: Collect per-RG cheap token mean prices for trend detection
    cid_price_series = {}  # cid -> [(rg_idx, cheap_mean_this_rg)]
    rg_hit_count = 0
    
    for rg in range(obs_rgs):
        t = pf.read_row_group(rg, columns=['market','asset_id','price','event_type'])
        mask = pc.equal(t.column('event_type'), 'price_change')
        t2 = t.filter(mask)
        del t; n = len(t2)
        if n == 0: del t2; continue
        
        mkt_col = t2.column('market')
        price_col = t2.column('price').to_numpy().astype(np.float64)
        aid_col = t2.column('asset_id')
        
        # Accumulate per CID+aid for this RG
        rg_stats = {}
        hits = 0
        for i in range(n):
            mv = mkt_col[i]
            cid = mv.hex() if isinstance(mv, (bytes, bytearray)) else str(mv)
            if cid not in bin_markets: continue
            hits += 1
            aid = str(aid_col[i])
            p = price_col[i]
            if cid not in rg_stats: rg_stats[cid] = {}
            if aid in rg_stats[cid]:
                rg_stats[cid][aid].append(p)
            else:
                rg_stats[cid][aid] = [p]
        
        # Compute cheap mean for this RG
        for cid, aid_prices in rg_stats.items():
            cheap_aid = bin_markets[cid][0]
            if cheap_aid in aid_prices:
                cheap_mean_rg = np.mean(aid_prices[cheap_aid])
                if cid not in cid_price_series:
                    cid_price_series[cid] = []
                cid_price_series[cid].append((rg, cheap_mean_rg))
        
        if hits > 0:
            rg_hit_count += 1
        del t2, mkt_col, price_col, aid_col, rg_stats
    
    # Compute trend for each market
    rising_markets = []
    falling_markets = []
    
    for cid, series in cid_price_series.items():
        if len(series) < 3: continue
        cheap_series = [p for _, p in sorted(series)]
        trend = compute_trend(cheap_series)
        cheap_aid, rich_aid, _, _ = bin_markets[cid]
        
        if trend >= trend_threshold:
            rising_markets.append((cid, cheap_aid, rich_aid, trend))
        else:
            falling_markets.append((cid, cheap_aid, rich_aid, trend))
    
    n_rising = len(rising_markets)
    n_falling = len(falling_markets)
    n_total_bin = n_rising + n_falling
    
    print(f"{n_total_bin} bin (rising={n_rising}, falling={n_falling}, series={len(cid_price_series)}, rg_hits={rg_hit_count}/{obs_rgs}) ({time.time()-t0:.0f}s)", end=' ', flush=True)
    
    # Phase 1b: Full market stats for sampled markets
    # Compute final settlement for rising markets
    print(f"  P1b: {nrg} RGs (stats)...", end=' ', flush=True)
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
    
    # Compute final settlement for rising markets
    rising_with_settlement = []
    for cid, cheap_aid, rich_aid, trend in rising_markets:
        if cid not in global_stats: continue
        aids = global_stats[cid]
        if cheap_aid in aids and rich_aid in aids:
            cheap_last = aids[cheap_aid][2]
            cheap_won = cheap_last > 0.90
            rising_with_settlement.append((cid, cheap_aid, rich_aid, trend, cheap_won))
    
    del global_stats; gc.collect()
    
    # Sample if too many
    if len(rising_with_settlement) > max_markets:
        random.seed(42)
        sampled = random.sample(rising_with_settlement, max_markets)
    else:
        sampled = rising_with_settlement
    
    # Compute actual WR for rising markets
    n_rising_wins = sum(1 for _, _, _, _, won in rising_with_settlement if won)
    n_rising_total = len(rising_with_settlement)
    rising_wr = n_rising_wins / n_rising_total if n_rising_total > 0 else 0
    
    # Also compute for falling markets
    n_falling_wins = 0; n_falling_total = 0
    for cid, cheap_aid, rich_aid, trend in falling_markets:
        if cid not in global_stats: continue  # already deleted... need to recompute
    
    print(f"rising_WR={rising_wr:.1%}({n_rising_wins}/{n_rising_total}), sampled {len(sampled)} ({time.time()-t0:.0f}s)", end=' ', flush=True)
    
    if not sampled:
        print(" EMPTY")
        return [], n_rising, n_falling, rising_wr
    
    # Phase 2: Generate signals from RISING markets in trading window
    CHUNK = 500
    all_signals = []
    n_chunks = (len(sampled) + CHUNK - 1) // CHUNK
    trade_start_rg = obs_rgs  # Only use data from trading window (after observation)
    
    for ci in range(n_chunks):
        chunk = sampled[ci*CHUNK:(ci+1)*CHUNK]
        chunk_cids_set = set(m[0] for m in chunk)
        chunk_aid = {m[0]: (m[1], m[2]) for m in chunk}
        cid_prices_data = {cid: [] for cid, _, _, _, _ in chunk}
        
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
                    cid_prices_data[cid].append((ts, p))
            
            del t2, mkt_col, price_col, aid_col, ts_col
        
        # Analyze signals
        for cid, cheap_aid, rich_aid, trend, cheap_won in chunk:
            data = cid_prices_data[cid]
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
                    'early_trend': float(trend),
                    'market_type': 'rising'
                })
                last_i = i
            del prices, rsi
        
        del cid_prices_data, chunk_aid; gc.collect()
        n = len(all_signals)
        wr = sum(s['won'] for s in all_signals)/n*100 if n else 0
        print(f"c{ci+1}/{n_chunks}({n}sig,{wr:.0f}%)", end=' ', flush=True)
    
    n = len(all_signals)
    wr = sum(s['won'] for s in all_signals)/n*100 if n else 0
    print(f"done: {n} sig, WR={wr:.1f}%")
    return all_signals, n_rising, n_falling, rising_wr


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
    p.add_argument('--obs-pct', type=float, default=0.30, help='Fraction of hour for observation')
    p.add_argument('--trend-threshold', type=float, default=0.0, help='Min trend slope to trade')
    a = p.parse_args()
    
    data_dir = Path(a.dir)
    out_dir = Path(a.out)
    out_dir.mkdir(exist_ok=True)
    
    files = sorted(data_dir.glob('polymarket_orderbook_*.parquet'))
    valid = [f for f in files if f.stat().st_size > 100_000_000][:a.maxfiles]
    print(f"Found {len(valid)} valid files")
    print(f"Config: RSI<{a.rsi_max}, {a.price_min}-{a.price_max}¢, obs={a.obs_pct:.0%}, trend>={a.trend_threshold}")
    
    all_sig = []
    hour_results = []
    
    for fi, f in enumerate(valid):
        t0 = time.time()
        print(f"[{fi+1}/{len(valid)}] {f.name}", flush=True)
        gc.collect()
        sigs, n_rising, n_falling, rising_wr = backtest_file_v18_4b(
            str(f), max_markets=a.sample,
            rsi_max=a.rsi_max, price_min=a.price_min, price_max=a.price_max,
            obs_pct=a.obs_pct, trend_threshold=a.trend_threshold
        )
        n = len(sigs)
        wr = sum(s['won'] for s in sigs)/n*100 if n else 0
        dt = time.time() - t0
        print(f"  -> {n} sig, WR={wr:.1f}%, rising={n_rising}/{n_rising+n_falling}, rising_wr={rising_wr:.1%} ({dt:.0f}s)", flush=True)
        hour_results.append({
            'file': f.name, 'n': n, 'wr': wr,
            'rising': n_rising, 'falling': n_falling,
            'rising_wr': float(rising_wr), 'dt': dt
        })
        all_sig.extend(sigs)
        gc.collect()
    
    if not all_sig:
        print("No signals!")
        return
    
    t = len(all_sig); w = sum(s['won'] for s in all_sig); wr = w/t*100
    
    # Trend quality bins
    trend_bins = defaultdict(lambda: {'n':0, 'w':0})
    for s in all_sig:
        t_val = s.get('early_trend', 0)
        if t_val < 0.001: k = '<0.1%'
        elif t_val < 0.005: k = '0.1-0.5%'
        elif t_val < 0.01: k = '0.5-1%'
        elif t_val < 0.05: k = '1-5%'
        else: k = '>5%'
        trend_bins[k]['n'] += 1; trend_bins[k]['w'] += int(s['won'])
    
    print(f"\n{'='*60}")
    print(f"V18.4b PMXT — PER-MARKET MOMENTUM GATE")
    print(f"RSI<{a.rsi_max} + {a.price_min}-{a.price_max}¢ + rising-only")
    print(f"Observe first {a.obs_pct:.0%} of hour, trade remaining")
    print(f"Sample: {a.sample} mkts/hr, {len(valid)} hrs")
    print(f"{'='*60}")
    print(f"  Signals: {t} | Wins: {w} | WR: {wr:.1f}%")
    print()
    
    print("  Trend quality:")
    for k in ['<0.1%','0.1-0.5%','0.5-1%','1-5%','>5%']:
        d = trend_bins[k]
        if d['n']: print(f"    {k:8s}: n={d['n']:5d} WR={d['w']/d['n']*100:.1f}%")
    
    print()
    print("  Hour-by-hour:")
    for hr in hour_results:
        print(f"    {hr['file'][:35]:35s} rising={hr['rising']:>4d}/{hr['rising']+hr['falling']:>4d} rising_wr={hr['rising_wr']:.1%} n={hr['n']:5d} WR={hr['wr']:5.1f}%")
    
    results = {
        'version': 'V18.4b-momentum-gate',
        'obs_pct': a.obs_pct,
        'trend_threshold': a.trend_threshold,
        'rsi_max': a.rsi_max,
        'price_range': [a.price_min, a.price_max],
        'total_signals': t, 'wins': w, 'win_rate': round(wr,1),
        'hour_results': hour_results
    }
    with open(out_dir/'v18_4b_results.json','w') as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved -> {out_dir/'v18_4b_results.json'}")


if __name__ == '__main__':
    main()