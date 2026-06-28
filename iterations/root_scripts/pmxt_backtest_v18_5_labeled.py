#!/usr/bin/env python3
"""
V18.5 PMXT Backtest — WITH DIRECTION LABELS from Gamma API.
Fetches BTC Up/Down market metadata (condition_id → UP/DOWN token IDs),
matches to PMXT data, then measures WR when trading the CORRECT side.

This is the breakthrough: we can now identify which token is "UP" vs "DOWN"
and trade accordingly.
"""

import pyarrow.parquet as pq
import pyarrow.compute as pc
import numpy as np
import json
import urllib.request
from pathlib import Path
from collections import defaultdict
import time
import gc
import warnings; warnings.filterwarnings('ignore')


def fetch_btc_updown_markets():
    """Fetch ALL BTC Up/Down market metadata from Gamma API."""
    all_markets = []
    for offset in range(0, 2000, 100):
        url = f'https://gamma-api.polymarket.com/markets?limit=100&active=false&closed=true&order=volume24hr&ascending=false&offset={offset}'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'FDC/1.0', 'Accept': 'application/json'})
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
            if not data:
                break
            btc_ud = [m for m in data if 'Up' in str(m.get('outcomes','')) and 'Down' in str(m.get('outcomes','')) and ('bitcoin' in m.get('question','').lower() or 'btc' in m.get('question','').lower())]
            all_markets.extend(btc_ud)
        except Exception as e:
            print(f"  Fetch offset {offset}: {e}")
            break
    
    # Build mapping: condition_id → {up_aid, down_aid, question, slug}
    cid_map = {}
    for m in all_markets:
        cid = m.get('conditionId', m.get('condition_id', ''))
        if not cid:
            continue
        # Normalize CID format
        if cid.startswith('0x'):
            cid_hex = cid[2:]  # Remove 0x prefix
        else:
            cid_hex = cid
        cid_hex = cid_hex.lower()
        
        tokens = m.get('tokens', [])
        clob_ids = m.get('clobTokenIds', [])
        outcomes = m.get('outcomes', [])
        
        up_aid = None
        down_aid = None
        
        if tokens and isinstance(tokens[0], dict):
            for tok in tokens:
                outcome = tok.get('outcome', '')
                aid = tok.get('asset_id', tok.get('id', ''))
                if outcome == 'Up':
                    up_aid = aid
                elif outcome == 'Down':
                    down_aid = aid
        elif clob_ids and outcomes:
            for i, outcome in enumerate(outcomes):
                if outcome == 'Up' and i < len(clob_ids):
                    up_aid = clob_ids[i]
                elif outcome == 'Down' and i < len(clob_ids):
                    down_aid = clob_ids[i]
        
        if up_aid and down_aid:
            cid_map[cid_hex] = {
                'up_aid': str(up_aid),
                'down_aid': str(down_aid),
                'question': m.get('question', ''),
                'slug': m.get('slug', ''),
            }
    
    return cid_map


def backtest_file_v18_5(filepath, cid_map, max_markets=2000, sample_seed=42):
    """Backtest with direction labels — trade the SIDE aligned with BTC direction."""
    fname = Path(filepath).stem.replace('polymarket_orderbook_', '')
    pf = pq.ParquetFile(filepath)
    nrg = pf.num_row_groups
    
    import random
    random.seed(sample_seed)
    
    # Phase 1: Find binary markets in this file that match our CID map
    global_stats = {}
    n_matched = 0
    n_unmatched = 0
    
    for rg in range(min(5, nrg)):  # Sample first 5 RGs for CID discovery
        t = pf.read_row_group(rg, columns=['market', 'asset_id', 'price', 'event_type'])
        mask = pc.equal(t.column('event_type'), 'price_change')
        t2 = t.filter(mask)
        del t
        n = len(t2)
        if n == 0:
            del t2
            continue
        
        mkt_col = t2.column('market')
        aid_col = t2.column('asset_id')
        price_col = t2.column('price').to_numpy().astype(np.float64)
        
        for i in range(n):
            mv = mkt_col[i]
            cid_bytes = bytes(mv)
            cid_hex = cid_bytes.hex()
            # The PMXT market column stores hex string as bytes
            # E.g., b'0x003f...' stored as FixedSizeBinary(66)
            try:
                cid_str = cid_bytes.decode('ascii', errors='replace')
                if cid_str.startswith('0x'):
                    cid_hex_norm = cid_str[2:].lower()
                else:
                    cid_hex_norm = cid_hex.lower()
            except:
                cid_hex_norm = cid_hex.lower()
            
            aid = str(aid_col[i])
            p = float(price_col[i])
            
            if cid_hex_norm not in global_stats:
                global_stats[cid_hex_norm] = {}
            if aid not in global_stats[cid_hex_norm]:
                global_stats[cid_hex_norm][aid] = [0.0, 0, 0.0]  # [sum_price, count, last_price]
            global_stats[cid_hex_norm][aid][0] += p
            global_stats[cid_hex_norm][aid][1] += 1
            global_stats[cid_hex_norm][aid][2] = p
        
        del t2, mkt_col, aid_col, price_col
    
    # Find matched CIDs
    matched_cids = {}
    for cid_hex, aids in global_stats.items():
        if cid_hex in cid_map:
            meta = cid_map[cid_hex]
            # Validate: UP and DOWN token IDs exist in this market
            up_aid = meta['up_aid']
            down_aid = meta['down_aid']
            if up_aid in aids and down_aid in aids:
                matched_cids[cid_hex] = meta
                n_matched += 1
            elif len(aids) == 2:
                # Market exists but aid format might differ
                matched_cids[cid_hex] = meta
                n_matched += 1
    
    n_unmatched = len(global_stats) - n_matched
    
    print(f"  [{nrg} RGs] {len(global_stats)} CIDs, {n_matched} matched to BTC Up/Down, {n_unmatched} unmatched", flush=True)
    
    if n_matched == 0:
        print(f"  No matched markets in {fname} — skipping direction analysis")
        # Fall back to cheap/rich analysis
        return None, len(global_stats)
    
    # Phase 2: For matched markets, compute direction-labeled WR
    up_wr_count = 0
    up_wr_total = 0
    down_wr_count = 0
    down_wr_total = 0
    cheap_up_wr_count = 0
    cheap_up_wr_total = 0
    rich_up_wr_count = 0
    rich_up_wr_total = 0
    cheap_down_wr_count = 0
    cheap_down_wr_total = 0
    rich_down_wr_count = 0
    rich_down_wr_total = 0
    
    for cid_hex, meta in matched_cids.items():
        up_aid = meta['up_aid']
        down_aid = meta['down_aid']
        aids = global_stats[cid_hex]
        
        # Determine UP and DOWN token mean prices
        up_mean = aids.get(up_aid, [0, 1, 0])[0] / max(1, aids.get(up_aid, [0, 1, 0])[1])
        down_mean = aids.get(down_aid, [0, 1, 0])[0] / max(1, aids.get(down_aid, [0, 1, 0])[1])
        up_last = aids.get(up_aid, [0, 1, 0])[2]
        down_last = aids.get(down_aid, [0, 1, 0])[2]
        
        # Which is cheap and which is rich?
        if up_mean < down_mean:
            # UP token is cheap, DOWN token is rich
            cheap_aid = up_aid
            rich_aid = down_aid
            up_is_cheap = True
        else:
            # DOWN token is cheap, UP token is rich
            cheap_aid = down_aid
            rich_aid = up_aid
            up_is_cheap = False
        
        cheap_mean = min(up_mean, down_mean)
        rich_mean = max(up_mean, down_mean)
        cheap_last = aids.get(cheap_aid, [0, 1, 0])[2]
        rich_last = aids.get(rich_aid, [0, 1, 0])[2]
        
        # "WIN" definition: final_price > 0.90
        up_won = up_last > 0.90
        down_won = down_last > 0.90
        
        # Direction-labeled WR
        up_wr_total += 1
        down_wr_total += 1
        if up_won: up_wr_count += 1
        if down_won: down_wr_count += 1
        
        # Side × Direction WR
        if up_is_cheap:
            cheap_up_wr_total += 1
            if up_won: cheap_up_wr_count += 1
            rich_down_wr_total += 1
            if down_won: rich_down_wr_count += 1
        else:
            cheap_down_wr_total += 1
            if down_won: cheap_down_wr_count += 1
            rich_up_wr_total += 1
            if up_won: rich_up_wr_count += 1
    
    print(f"  Matched: {n_matched} BTC Up/Down markets", flush=True)
    print(f"  UP token wins:   {up_wr_count}/{up_wr_total} = {up_wr_count/max(1,up_wr_total)*100:.1f}%", flush=True)
    print(f"  DOWN token wins: {down_wr_count}/{down_wr_total} = {down_wr_count/max(1,down_wr_total)*100:.1f}%", flush=True)
    if cheap_up_wr_total > 0:
        print(f"  cheap_UP:   {cheap_up_wr_count}/{cheap_up_wr_total} = {cheap_up_wr_count/cheap_up_wr_total*100:.1f}%", flush=True)
    if cheap_down_wr_total > 0:
        print(f"  cheap_DOWN:  {cheap_down_wr_count}/{cheap_down_wr_total} = {cheap_down_wr_count/cheap_down_wr_total*100:.1f}%", flush=True)
    if rich_up_wr_total > 0:
        print(f"  rich_UP:     {rich_up_wr_count}/{rich_up_wr_total} = {rich_up_wr_count/rich_up_wr_total*100:.1f}%", flush=True)
    if rich_down_wr_total > 0:
        print(f"  rich_DOWN:   {rich_down_wr_count}/{rich_down_wr_total} = {rich_down_wr_count/rich_down_wr_total*100:.1f}%", flush=True)
    
    return {
        'n_matched': n_matched,
        'up_wr': (up_wr_count, up_wr_total),
        'down_wr': (down_wr_count, down_wr_total),
        'cheap_up_wr': (cheap_up_wr_count, cheap_up_wr_total),
        'cheap_down_wr': (cheap_down_wr_count, cheap_down_wr_total),
        'rich_up_wr': (rich_up_wr_count, rich_up_wr_total),
        'rich_down_wr': (rich_down_wr_count, rich_down_wr_total),
    }, len(global_stats)


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--dir', default='pmxt_data')
    p.add_argument('--out', default='backtest_results_v18_5')
    a = p.parse_args()
    
    # Step 1: Fetch BTC Up/Down market metadata
    print("Fetching BTC Up/Down market metadata from Gamma API...")
    cid_map = fetch_btc_updown_markets()
    print(f"Found {len(cid_map)} BTC Up/Down markets with UP/DOWN token IDs")
    
    # Save the mapping
    out_dir = Path(a.out)
    out_dir.mkdir(exist_ok=True)
    with open(out_dir / 'btc_updown_cid_map.json', 'w') as f:
        json.dump(cid_map, f, indent=2, default=str)
    
    # Step 2: Process PMXT files
    data_dir = Path(a.dir)
    files = sorted(data_dir.glob('polymarket_orderbook_*.parquet'))
    valid = [f for f in files if f.stat().st_size > 100_000]
    print(f"Found {len(valid)} valid files")
    
    all_results = []
    total_matched = 0
    
    for fi, f in enumerate(valid):
        t0 = time.time()
        result, total_cids = backtest_file_v18_5(str(f), cid_map)
        dt = time.time() - t0
        if result:
            all_results.append(result)
            total_matched += result['n_matched']
            print(f"  [{fi+1}/{len(valid)}] {f.name}: {result['n_matched']} matched ({dt:.0f}s)")
        else:
            print(f"  [{fi+1}/{len(valid)}] {f.name}: 0 matched, {total_cids} CIDs ({dt:.0f}s)")
    
    # Aggregate
    print(f"\n{'='*70}")
    print(f"V18.5 DIRECTION-LABELED BACKTEST RESULTS")
    print(f"{'='*70}")
    print(f"Total matched BTC Up/Down markets: {total_matched}")
    print(f"Total CID map entries: {len(cid_map)}")
    
    if all_results:
        agg = defaultdict(lambda: [0, 0])
        for r in all_results:
            for key in ['up_wr', 'down_wr', 'cheap_up_wr', 'cheap_down_wr', 'rich_up_wr', 'rich_down_wr']:
                c, t = r[key]
                agg[key][0] += c
                agg[key][1] += t
        
        for key, label in [('up_wr', 'UP token'), ('down_wr', 'DOWN token'),
                           ('cheap_up_wr', 'cheap=UP'), ('cheap_down_wr', 'cheap=DOWN'),
                           ('rich_up_wr', 'rich=UP'), ('rich_down_wr', 'rich=DOWN')]:
            c, t = agg[key]
            if t > 0:
                print(f"  {label:15s}: {c:5d}/{t:5d} = {c/t*100:5.1f}%")
    
    with open(out_dir / 'v18_5_direction_results.json', 'w') as f:
        json.dump({'all_results': all_results, 'cid_map_size': len(cid_map)}, f, indent=2, default=str)
    print(f"\nSaved -> {out_dir / 'v18_5_direction_results.json'}")


if __name__ == '__main__':
    main()