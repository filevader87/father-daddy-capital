#!/usr/bin/env python3
"""
V18.4d PMXT Backtest — DIRECTIONAL ANALYSIS with BTC price gate.
Analyzes BOTH cheap and rich tokens.
Key question: which side wins when BTC goes UP vs DOWN?

Strategy hypothesis (bonereaper-style):
- BTC UP hour → buy EXPENSIVE side (wins ~95% of time)
- BTC DOWN hour → buy CHEAP side on DOWN markets (but we can't identify DOWN markets from PMXT alone)

This version: measure per-hour cheap_WR and rich_WR, correlate with BTC direction.
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


def fetch_btc_hourly(cache_file='btc_hourly_directions.json'):
    """Fetch BTC 5-min OHLCV and compute hourly direction."""
    try:
        with open(cache_file) as f:
            return json.load(f)
    except FileNotFoundError:
        pass
    
    import ccxt
    exchange = ccxt.binance({'enableRateLimit': True})
    since = exchange.parse8601('2026-05-20T00:00:00Z')
    
    all_candles = []
    while True:
        ohlcv = exchange.fetch_ohlcv('BTC/USDT', '5m', since=since, limit=1000)
        if not ohlcv: break
        all_candles.extend(ohlcv)
        since = ohlcv[-1][0] + 1
        if len(ohlcv) < 1000: break
    
    directions = {}
    for c in all_candles:
        hour_key = time.strftime('%Y-%m-%dT%H', time.gmtime(c[0]//1000))
        if hour_key not in directions:
            directions[hour_key] = {'candles': [], 'first_open': c[1], 'last_close': c[4]}
        directions[hour_key]['candles'].append(c)
        directions[hour_key]['last_close'] = c[4]
    
    for hk, d in directions.items():
        o = d['first_open']
        c = d['last_close']
        d['btc_direction'] = 'UP' if c >= o else 'DOWN'
        d['btc_change_pct'] = (c / o - 1) * 100
        highs = [x[2] for x in d['candles']]
        lows = [x[3] for x in d['candles']]
        d['btc_range_pct'] = (max(highs) - min(lows)) / o * 100
        d['n_candles'] = len(d['candles'])
    
    with open(cache_file, 'w') as f:
        json.dump(directions, f, indent=2)
    return directions


def backtest_file_v18_4d(filepath, btc_info, max_markets=2000, sample_seed=42):
    """Process one hourly file. Analyze both cheap and rich tokens separately."""
    fname = Path(filepath).stem.replace('polymarket_orderbook_', '')
    btc_direction = btc_info['btc_direction'] if btc_info else 'UNKNOWN'
    btc_change = btc_info['btc_change_pct'] if btc_info else 0
    
    pf = pq.ParquetFile(filepath)
    nrg = pf.num_row_groups
    
    # Phase 1: Build global stats
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
                global_stats[cid][aid][2] = p
            else:
                global_stats[cid][aid] = [p, 1, p]
        del t2, mkt_col, price_col, aid_col
    
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
            rich_won = last_prices[rich_aid] > 0.90
            bin_markets.append((cid, cheap_aid, rich_aid, cheap_mean, rich_mean, cheap_won, rich_won))
    
    del global_stats; gc.collect()
    total_bin = len(bin_markets)
    n_cheap_won = sum(1 for m in bin_markets if m[5])
    n_rich_won = sum(1 for m in bin_markets if m[6])
    cheap_wr = n_cheap_won / total_bin if total_bin else 0
    rich_wr = n_rich_won / total_bin if total_bin else 0
    
    if total_bin > max_markets:
        random.seed(sample_seed)
        bin_markets = random.sample(bin_markets, max_markets)
    
    sampled_cids = set(m[0] for m in bin_markets)
    cid_info = {}
    for m in bin_markets:
        cid, cheap_aid, rich_aid, cheap_mean, rich_mean, cheap_won, rich_won = m
        cid_info[cid] = {
            'cheap_aid': cheap_aid, 'rich_aid': rich_aid,
            'cheap_mean': cheap_mean, 'rich_mean': rich_mean,
            'cheap_won': cheap_won, 'rich_won': rich_won
        }
    
    print(f"  P1: {nrg} RGs → {total_bin} bin (cheap_WR={cheap_wr:.1%}, rich_WR={rich_wr:.1%})", end=' ', flush=True)
    
    # Phase 2: Generate signals (both cheap and rich)
    all_signals = []
    
    for rg in range(nrg):
        t = pf.read_row_group(rg, columns=['market','asset_id','price','event_type'])
        mask = pc.equal(t.column('event_type'), 'price_change')
        t2 = t.filter(mask)
        del t; n = len(t2)
        if n == 0: del t2; continue
        
        mkt_col = t2.column('market')
        price_col = t2.column('price').to_numpy().astype(np.float64)
        aid_col = t2.column('asset_id')
        
        # Group by CID within this RG
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
        
        # For each market, analyze both cheap and rich token price series
        for cid, aid_data in rg_data.items():
            info = cid_info[cid]
            cheap_aid = info['cheap_aid']
            rich_aid = info['rich_aid']
            
            # Cheap token analysis
            if cheap_aid in aid_data and len(aid_data[cheap_aid]) > RSI_PERIOD + 5:
                prices = np.array(sorted(aid_data[cheap_aid]), dtype=float)
                if len(prices) < RSI_PERIOD + 5: continue
                rsi = compute_rsi(prices, RSI_PERIOD)
                won = info['cheap_won']
                last_i = -999
                for i in range(RSI_PERIOD + 1, len(prices)):
                    if i - last_i < 20: continue
                    p = prices[i]
                    if p < 0.03 or p > 0.97: continue
                    r = rsi[i]
                    if np.isnan(r): continue
                    
                    side = 'cheap'
                    price_tier = '<10¢' if p < 0.10 else ('10-20¢' if p < 0.20 else ('20-40¢' if p < 0.40 else '40+¢'))
                    zone = f'RSI<{int(r)}' if r < 30 else f'RSI{int(r)}'
                    
                    all_signals.append({
                        'price': float(p), 'rsi': float(r), 'won': bool(won),
                        'side': side, 'zone': zone, 'tier': price_tier,
                        'cheap_mean': float(info['cheap_mean']),
                    })
                    last_i = i
            
            # Rich token analysis
            if rich_aid in aid_data and len(aid_data[rich_aid]) > RSI_PERIOD + 5:
                prices = np.array(sorted(aid_data[rich_aid]), dtype=float)
                if len(prices) < RSI_PERIOD + 5: continue
                rsi = compute_rsi(prices, RSI_PERIOD)
                won = info['rich_won']
                last_i = -999
                for i in range(RSI_PERIOD + 1, len(prices)):
                    if i - last_i < 20: continue
                    p = prices[i]
                    if p < 0.03 or p > 0.97: continue
                    r = rsi[i]
                    if np.isnan(r): continue
                    
                    side = 'rich'
                    price_tier = '<10¢' if p < 0.10 else ('10-20¢' if p < 0.20 else ('20-40¢' if p < 0.40 else '40+¢'))
                    zone = f'RSI<{int(r)}' if r < 30 else f'RSI{int(r)}'
                    
                    all_signals.append({
                        'price': float(p), 'rsi': float(r), 'won': bool(won),
                        'side': side, 'zone': zone, 'tier': price_tier,
                        'rich_mean': float(info['rich_mean']),
                    })
                    last_i = i
        
        del t2, mkt_col, price_col, aid_col, rg_data
    
    n = len(all_signals)
    wr = sum(s['won'] for s in all_signals)/n*100 if n else 0
    return all_signals, total_bin, cheap_wr, rich_wr, btc_direction, btc_change


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--dir', default='pmxt_data')
    p.add_argument('--out', default='backtest_results_v18_4')
    p.add_argument('--maxfiles', type=int, default=99)
    p.add_argument('--sample', type=int, default=2000)
    a = p.parse_args()
    
    print("Fetching BTC hourly direction data...")
    btc_dir = fetch_btc_hourly()
    
    up_hours = sum(1 for v in btc_dir.values() if v['btc_direction'] == 'UP')
    down_hours = sum(1 for v in btc_dir.values() if v['btc_direction'] == 'DOWN')
    print(f"BTC hours: {up_hours} UP, {down_hours} DOWN, {len(btc_dir)} total")
    
    data_dir = Path(a.dir)
    files = sorted(data_dir.glob('polymarket_orderbook_*.parquet'))
    valid = [f for f in files if f.stat().st_size > 100_000_000][:a.maxfiles]
    print(f"Found {len(valid)} valid files, sample={a.sample}")
    
    all_sig = []
    hour_results = []
    
    for fi, f in enumerate(valid):
        t0 = time.time()
        fname = Path(f).stem.replace('polymarket_orderbook_', '')
        btc_info = btc_dir.get(fname)
        
        print(f"[{fi+1}/{len(valid)}] {f.name}", flush=True)
        gc.collect()
        
        result = backtest_file_v18_4d(str(f), btc_info, max_markets=a.sample)
        sigs, total_bin, cheap_wr, rich_wr, btc_d, btc_c = result
        
        if sigs is None:
            print(f"  SKIP (no BTC data for {fname})")
            continue
        
        n = len(sigs)
        wr = sum(s['won'] for s in sigs)/n*100 if n else 0
        
        # Split by side
        cheap_sigs = [s for s in sigs if s['side'] == 'cheap']
        rich_sigs = [s for s in sigs if s['side'] == 'rich']
        cheap_wr_sig = sum(s['won'] for s in cheap_sigs)/len(cheap_sigs)*100 if cheap_sigs else 0
        rich_wr_sig = sum(s['won'] for s in rich_sigs)/len(rich_sigs)*100 if rich_sigs else 0
        
        dt = time.time() - t0
        print(f"  -> {n} sig (cheap={len(cheap_sigs)}/{cheap_wr_sig:.0f}%, rich={len(rich_sigs)}/{rich_wr_sig:.0f}%) "
              f"BTC={btc_d}({btc_c:+.2f}%) cheap_base={cheap_wr:.1%} rich_base={rich_wr:.1%} ({dt:.0f}s)")
        
        hour_results.append({
            'file': f.name, 'n': n, 'wr': wr,
            'cheap_n': len(cheap_sigs), 'cheap_wr': cheap_wr_sig,
            'rich_n': len(rich_sigs), 'rich_wr': rich_wr_sig,
            'total_bin': total_bin, 'cheap_base_wr': float(cheap_wr),
            'rich_base_wr': float(rich_wr),
            'btc_dir': btc_d, 'btc_chg': float(btc_c) if btc_c else 0,
        })
        all_sig.extend(sigs)
    
    if not all_sig:
        print("No signals!")
        return
    
    t = len(all_sig); w = sum(s['won'] for s in all_sig); wr = w/t*100
    
    # Breakdown by side, BTC direction, RSI zone, price tier
    cheap_sigs = [s for s in all_sig if s['side'] == 'cheap']
    rich_sigs = [s for s in all_sig if s['side'] == 'rich']
    
    print(f"\n{'='*70}")
    print(f"V18.4d PMXT — DIRECTIONAL ANALYSIS")
    print(f"{'='*70}")
    print(f"  Total: {t} sig, {w} wins, {wr:.1f}% WR")
    print(f"  Cheap: {len(cheap_sigs)} sig, {sum(s['won'] for s in cheap_sigs)/len(cheap_sigs)*100:.1f}% WR")
    print(f"  Rich:  {len(rich_sigs)} sig, {sum(s['won'] for s in rich_sigs)/len(rich_sigs)*100:.1f}% WR")
    
    # Cheap side by RSI zone
    print(f"\n  Cheap side by RSI zone:")
    for rsi_zone, rsi_max in [('RSI<18', 18), ('RSI<28', 28), ('RSI<35', 35), ('RSI<50', 50), ('RSI50+', 999)]:
        if rsi_max == 999:
            zone_sigs = [s for s in cheap_sigs if s['rsi'] >= 50]
        else:
            zone_sigs = [s for s in cheap_sigs if s['rsi'] < rsi_max and (rsi_max <= 18 or s['rsi'] >= rsi_max-10)]
        # Simplify: just bucket by RSI ranges
        if rsi_max < 999:
            zone_sigs = [s for s in cheap_sigs if s['rsi'] < rsi_max]
        else:
            zone_sigs = [s for s in cheap_sigs if s['rsi'] >= 50]
        n = len(zone_sigs)
        if n > 0:
            zone_wr = sum(s['won'] for s in zone_sigs)/n*100
            print(f"    RSI<{rsi_zone if rsi_max<999 else '50+'}: {n} sig, {zone_wr:.1f}% WR")
    
    # Rich side by RSI zone  
    print(f"\n  Rich side by RSI zone:")
    for rsi_bound in [18, 28, 35, 50]:
        zone_sigs = [s for s in rich_sigs if s['rsi'] < rsi_bound]
        n = len(zone_sigs)
        if n > 0:
            zone_wr = sum(s['won'] for s in zone_sigs)/n*100
            print(f"    RSI<{rsi_bound}: {n} sig, {zone_wr:.1f}% WR")
    
    # Rich side oversold (RSI<30) by price tier
    print(f"\n  Rich side RSI<30 by price tier:")
    for tier in ['<10¢', '10-20¢', '20-40¢', '40+¢']:
        tier_sigs = [s for s in rich_sigs if s['rsi'] < 30 and s['tier'] == tier]
        n = len(tier_sigs)
        if n > 0:
            tier_wr = sum(s['won'] for s in tier_sigs)/n*100
            print(f"    {tier}: {n} sig, {tier_wr:.1f}% WR")
    
    # Hour-by-hour with BTC direction
    print(f"\n  Hour-by-hour (BTC direction):")
    for hr in hour_results:
        print(f"    {hr['file'][:35]:35s} BTC={hr['btc_dir']:4s}({hr['btc_chg']:+.2f}%) "
              f"cheap={hr['cheap_n']:4d}/{hr['cheap_wr']:.0f}% rich={hr['rich_n']:4d}/{hr['rich_wr']:.0f}% "
              f"base=cheap/{hr['cheap_base_wr']:.0%}/rich/{hr['rich_base_wr']:.0%}")
    
    # Key comparison: cheap WR in BTC UP vs BTC DOWN hours
    up_hours_data = [hr for hr in hour_results if hr['btc_dir'] == 'UP']
    down_hours_data = [hr for hr in hour_results if hr['btc_dir'] == 'DOWN']
    
    if up_hours_data:
        avg_cheap_base_up = np.mean([hr['cheap_base_wr'] for hr in up_hours_data])
        avg_rich_base_up = np.mean([hr['rich_base_wr'] for hr in up_hours_data])
        print(f"\n  BTC UP hours ({len(up_hours_data)}): avg cheap_base_WR={avg_cheap_base_up:.1%}, rich_base_WR={avg_rich_base_up:.1%}")
    if down_hours_data:
        avg_cheap_base_down = np.mean([hr['cheap_base_wr'] for hr in down_hours_data])
        avg_rich_base_down = np.mean([hr['rich_base_wr'] for hr in down_hours_data])
        print(f"  BTC DOWN hours ({len(down_hours_data)}): avg cheap_base_WR={avg_cheap_base_down:.1%}, rich_base_WR={avg_rich_base_down:.1%}")
    
    # Save results
    results = {
        'version': 'V18.4d-directional',
        'total_signals': t, 'wins': w, 'win_rate': round(wr,1),
        'cheap_n': len(cheap_sigs), 'cheap_wr': round(sum(s['won'] for s in cheap_sigs)/len(cheap_sigs)*100,1) if cheap_sigs else 0,
        'rich_n': len(rich_sigs), 'rich_wr': round(sum(s['won'] for s in rich_sigs)/len(rich_sigs)*100,1) if rich_sigs else 0,
        'hour_results': hour_results,
    }
    out_dir = Path(a.out)
    out_dir.mkdir(exist_ok=True)
    with open(out_dir/'v18_4d_directional.json','w') as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved -> {out_dir/'v18_4d_directional.json'}")


if __name__ == '__main__':
    main()