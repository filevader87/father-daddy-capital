#!/usr/bin/env python3
"""
V18.4e PMXT Backtest — DIRECTIONAL INFERENCE from price patterns.
Key insight: We can't get market metadata (UP/DOWN) from PMXT data alone.
But we CAN infer direction from the outcome:
- If the cheap token settles at ~0 (loses), it was the WRONG side
- If the cheap token settles at ~1 (wins), it was the RIGHT side

For bonereaper-style trading, we want to find:
1. Markets where cheap token = BTC DOWN (wins during BTC drops)
2. Markets where cheap token = BTC UP (but these are rare at 5¢)

The strategy: Trade the RICH side when it's oversold (dip), because rich tokens
that recover = 54% base rate, and with BTC directional filter might be higher.

But our V18.4d showed rich-side RSI entries give WORSE WR than base rate.
So the new hypothesis: TRADE MOMENTUM, not mean-reversion.
- Buy tokens that are TRENDING UP (RSI > 50, price rising)
- Sell/avoid tokens that are trending DOWN (RSI < 30)
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
        o = d['first_open']; c = d['last_close']
        d['btc_direction'] = 'UP' if c >= o else 'DOWN'
        d['btc_change_pct'] = (c / o - 1) * 100
    with open(cache_file, 'w') as f:
        json.dump(directions, f, indent=2)
    return directions


def backtest_file_v18_4e(filepath, btc_info, max_markets=2000, sample_seed=42):
    """Process one hourly file. Focus on MOMENTUM signals."""
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
    
    # Find binary markets and classify
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
            'cheap_won': cheap_won, 'rich_won': rich_won,
            'dir': 'DOWN' if cheap_won else 'UP'  # if cheap won = DOWN market (cheap side is DOWN)
        }
    
    print(f"  P1: {nrg} RGs → {total_bin} bin (cheap_WR={cheap_wr:.1%}, rich_WR={rich_wr:.1%})", end=' ', flush=True)
    
    # Phase 2: Generate MOMENTUM signals
    # Instead of RSI<18 (oversold/reversion), use RSI>50 (momentum/trending)
    # For each market, track which side is trending UP
    
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
        
        for cid, aid_data in rg_data.items():
            info = cid_info[cid]
            
            # Analyze BOTH cheap and rich token series
            for side in ['cheap', 'rich']:
                aid_key = f'{side}_aid'
                aid = info[aid_key]
                if aid not in aid_data or len(aid_data[aid]) < RSI_PERIOD + 5:
                    continue
                prices = np.array(sorted(aid_data[aid]), dtype=float)
                if len(prices) < RSI_PERIOD + 5: continue
                rsi = compute_rsi(prices, RSI_PERIOD)
                won = info[f'{side}_won']
                
                # Sample every 20 data points
                last_i = -999
                for i in range(RSI_PERIOD + 1, len(prices)):
                    if i - last_i < 20: continue
                    p = prices[i]
                    if p < 0.03 or p > 0.97: continue
                    r = float(rsi[i])
                    if np.isnan(r): continue
                    
                    # Direction: UP if this side won (settlement > 0.90)
                    dir_label = info['dir']
                    
                    # Price change over last 14 periods
                    p_start = prices[max(0, i-14)]
                    pct_change = (p - p_start) / p_start if p_start > 0 else 0
                    
                    # Momentum categories
                    if r < 18:
                        momentum = 'ultra_oversold'
                    elif r < 30:
                        momentum = 'oversold'
                    elif r < 50:
                        momentum = 'declining'
                    elif r < 70:
                        momentum = 'rising'
                    else:
                        momentum = 'overbought'
                    
                    all_signals.append({
                        'price': float(p), 'rsi': float(r), 'won': bool(won),
                        'side': side, 'dir': dir_label,
                        'momentum': momentum,
                        'pct_change': float(pct_change),
                        'cheap_mean': float(info['cheap_mean']),
                    })
                    last_i = i
        
        del t2, mkt_col, price_col, aid_col, rg_data
    
    n = len(all_signals)
    return all_signals, total_bin, cheap_wr, rich_wr, btc_direction, btc_change


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--dir', default='pmxt_data')
    p.add_argument('--out', default='backtest_results_v18_4')
    p.add_argument('--maxfiles', type=int, default=99)
    p.add_argument('--sample', type=int, default=2000)
    a = p.parse_args()
    
    btc_dir = fetch_btc_hourly()
    
    data_dirs = [Path(a.dir)]
    all_sig = []
    hour_results = []
    
    for data_dir in data_dirs:
        files = sorted(data_dir.glob('polymarket_orderbook_*.parquet'))
        valid = [f for f in files if f.stat().st_size > 100_000_000][:a.maxfiles]
        print(f"Found {len(valid)} valid files in {data_dir}")
        
        for fi, f in enumerate(valid):
            t0 = time.time()
            fname = Path(f).stem.replace('polymarket_orderbook_', '')
            btc_info = btc_dir.get(fname)
            
            print(f"[{fi+1}/{len(valid)}] {f.name}", flush=True)
            gc.collect()
            
            result = backtest_file_v18_4e(str(f), btc_info, max_markets=a.sample)
            sigs, total_bin, cheap_wr, rich_wr, btc_d, btc_c = result
            if sigs is None: continue
            
            n = len(sigs)
            wr = sum(s['won'] for s in sigs)/n*100 if n else 0
            
            # Split by side and direction
            cheap_up = [s for s in sigs if s['side'] == 'cheap' and s['dir'] == 'UP']
            cheap_down = [s for s in sigs if s['side'] == 'cheap' and s['dir'] == 'DOWN']
            rich_up = [s for s in sigs if s['side'] == 'rich' and s['dir'] == 'UP']
            rich_down = [s for s in sigs if s['side'] == 'rich' and s['dir'] == 'DOWN']
            
            dt = time.time() - t0
            print(f"  -> {n} sig (cheap_up={len(cheap_up)}/{sum(s['won'] for s in cheap_up)/len(cheap_up)*100 if cheap_up else 0:.0f}%, "
                  f"cheap_down={len(cheap_down)}/{sum(s['won'] for s in cheap_down)/len(cheap_down)*100 if cheap_down else 0:.0f}%, "
                  f"rich_up={len(rich_up)}/{sum(s['won'] for s in rich_up)/len(rich_up)*100 if rich_up else 0:.0f}%, "
                  f"rich_down={len(rich_down)}/{sum(s['won'] for s in rich_down)/len(rich_down)*100 if rich_down else 0:.0f}%) "
                  f"BTC={btc_d}({btc_c:+.2f}%) ({dt:.0f}s)")
            
            hour_results.append({
                'file': f.name, 'n': n,
                'cheap_up_n': len(cheap_up), 'cheap_up_wr': sum(s['won'] for s in cheap_up)/len(cheap_up)*100 if cheap_up else 0,
                'cheap_down_n': len(cheap_down), 'cheap_down_wr': sum(s['won'] for s in cheap_down)/len(cheap_down)*100 if cheap_down else 0,
                'rich_up_n': len(rich_up), 'rich_up_wr': sum(s['won'] for s in rich_up)/len(rich_up)*100 if rich_up else 0,
                'rich_down_n': len(rich_down), 'rich_down_wr': sum(s['won'] for s in rich_down)/len(rich_down)*100 if rich_down else 0,
                'btc_dir': btc_d, 'btc_chg': float(btc_c) if btc_c else 0,
            })
            all_sig.extend(sigs)
    
    if not all_sig:
        print("No signals!")
        return
    
    t = len(all_sig); w = sum(s['won'] for s in all_sig)
    
    # Breakdowns
    print(f"\n{'='*70}")
    print(f"V18.4e PMXT — MOMENTUM + DIRECTION ANALYSIS")
    print(f"{'='*70}")
    
    # Side × Direction × Momentum
    for side in ['cheap', 'rich']:
        for dir_label in ['UP', 'DOWN']:
            for mom in ['ultra_oversold', 'oversold', 'declining', 'rising', 'overbought']:
                sigs = [s for s in all_sig if s['side'] == side and s['dir'] == dir_label and s['momentum'] == mom]
                n = len(sigs)
                if n > 0:
                    wr = sum(s['won'] for s in sigs)/n*100
                    print(f"  {side:5s} {dir_label:4s} {mom:16s}: {n:6d} sig, {wr:.1f}% WR")
    
    # Side × Direction (aggregate)
    print(f"\n  Side × Direction:")
    for side in ['cheap', 'rich']:
        for dir_label in ['UP', 'DOWN']:
            sigs = [s for s in all_sig if s['side'] == side and s['dir'] == dir_label]
            n = len(sigs)
            if n > 0:
                wr = sum(s['won'] for s in sigs)/n*100
                print(f"    {side:5s} {dir_label:4s}: {n:6d} sig, {wr:.1f}% WR")
    
    # Best combinations
    print(f"\n  Top 10 combinations (min 100 signals):")
    combos = []
    for side in ['cheap', 'rich']:
        for dir_label in ['UP', 'DOWN']:
            for mom in ['ultra_oversold', 'oversold', 'declining', 'rising', 'overbought']:
                sigs = [s for s in all_sig if s['side'] == side and s['dir'] == dir_label and s['momentum'] == mom]
                n = len(sigs)
                if n >= 100:
                    wr = sum(s['won'] for s in sigs)/n*100
                    combos.append((side, dir_label, mom, n, wr))
    combos.sort(key=lambda x: -x[4])
    for side, dir_label, mom, n, wr in combos:
        print(f"    {side:5s} {dir_label:4s} {mom:16s}: {n:6d} sig, {wr:.1f}% WR")
    
    # Save
    results = {'version': 'V18.4e-momentum-direction', 'total_signals': t, 'wins': w, 'hour_results': hour_results}
    out_dir = Path(a.out); out_dir.mkdir(exist_ok=True)
    with open(out_dir/'v18_4e_momentum.json', 'w') as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved -> {out_dir/'v18_4e_momentum.json'}")


if __name__ == '__main__':
    main()