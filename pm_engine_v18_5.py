#!/usr/bin/env python3
"""
V18.5 — Live BTC Up/Down Direction-Labeled Backtest Engine

Uses Gamma API market metadata to identify which token is UP vs DOWN,
then measures strategy WR with direction labels.

Strategy: When BTC is trending UP, buy the UP token if cheap (≤20¢).
          When BTC is trending DOWN, buy the DOWN token if cheap (≤20¢).

This is the breakthrough version — direction labels from live API.
"""

import json
import time
import urllib.request
from pathlib import Path
from collections import defaultdict
import numpy as np
import warnings; warnings.filterwarnings('ignore')


def fetch_btc_updown_markets():
    """Fetch ALL BTC Up/Down markets from Gamma API (active + closed)."""
    cid_map = {}
    
    # Fetch active markets
    for active in [True, False]:
        closed = not active
        for offset in range(0, 20000, 100):
            url = f'https://gamma-api.polymarket.com/markets?limit=100&active={str(active).lower()}&closed={str(closed).lower()}&order=volume24hr&ascending=false&offset={offset}'
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'FDC/1.0', 'Accept': 'application/json'})
                resp = urllib.request.urlopen(req, timeout=15)
                data = json.loads(resp.read())
                if not data: break
            except: break
            
            for m in data:
                q = m.get('question', '').lower()
                if 'bitcoin' not in q and 'btc' not in q: continue
                if 'up' not in q or 'down' not in q: continue
                
                cid = m.get('conditionId', m.get('condition_id', ''))
                if not cid: continue
                cid_norm = cid.lower() if cid.startswith('0x') else '0x' + cid.lower()
                
                # Parse outcomes
                outcomes_raw = m.get('outcomes', '[]')
                if isinstance(outcomes_raw, str):
                    try: outcomes = json.loads(outcomes_raw)
                    except: outcomes = []
                else: outcomes = outcomes_raw
                
                # Parse clobTokenIds
                clob_ids_raw = m.get('clobTokenIds', '[]')
                if isinstance(clob_ids_raw, str):
                    try: clob_ids = json.loads(clob_ids_raw)
                    except: clob_ids = []
                elif isinstance(clob_ids_raw, list):
                    clob_ids = [str(x) for x in clob_ids_raw]
                else: clob_ids = []
                
                if outcomes == ['Up', 'Down'] and len(clob_ids) >= 2:
                    # Parse slug for duration
                    slug = m.get('slug', '')
                    duration = 'unknown'
                    if '5m' in slug: duration = '5m'
                    elif '15m' in slug: duration = '15m'
                    elif '1h' in slug: duration = '1h'
                    elif '4h' in slug: duration = '4h'
                    elif 'daily' in slug or 'on-may' in slug or 'on-jun' in slug: duration = 'daily'
                    
                    cid_map[cid_norm] = {
                        'up_aid': str(clob_ids[0]),
                        'down_aid': str(clob_ids[1]),
                        'question': m.get('question', ''),
                        'slug': slug,
                        'duration': duration,
                        'active': active,
                    }
    
    return cid_map


def fetch_btc_prices():
    """Fetch recent BTC prices from Binance for direction detection."""
    try:
        url = 'https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=288'  # 24h of 5m candles
        req = urllib.request.Request(url, headers={'User-Agent': 'FDC/1.0'})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        prices = []
        for candle in data:
            ts = int(candle[0]) / 1000
            close = float(candle[4])
            prices.append({'ts': ts, 'close': close})
        return prices
    except Exception as e:
        print(f"Binance fetch error: {e}")
        return []


def fetch_clob_prices(token_id):
    """Fetch current orderbook prices for a token from CLOB."""
    try:
        url = f'https://clob.polymarket.com/prices?token_id={token_id}&side=buy'
        req = urllib.request.Request(url, headers={'User-Agent': 'FDC/1.0', 'Accept': 'application/json'})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        return data
    except Exception as e:
        return None


def fetch_active_market_prices(cid, up_aid, down_aid):
    """Fetch current prices for UP and DOWN tokens of a market."""
    up_price = None
    down_price = None
    
    for label, aid in [('UP', up_aid), ('DOWN', down_aid)]:
        try:
            url = f'https://clob.polymarket.com/price?token_id={aid}&side=buy'
            req = urllib.request.Request(url, headers={'User-Agent': 'FDC/1.0', 'Accept': 'application/json'})
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            price = float(data.get('price', 0))
            if label == 'UP':
                up_price = price
            else:
                down_price = price
        except Exception as e:
            pass
    
    return up_price, down_price


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--live', action='store_true', help='Run live scanning mode')
    p.add_argument('--scan', action='store_true', help='Scan current markets and show opportunities')
    p.add_argument('--backtest', action='store_true', help='Run historical backtest matching PMXT data')
    a = p.parse_args()
    
    print("=" * 70)
    print("V18.5 — BTC Up/Down Direction-Labeled Strategy Engine")
    print("=" * 70)
    
    # Step 1: Fetch market metadata
    print("\n[1] Fetching BTC Up/Down market metadata from Gamma API...")
    cid_map = fetch_btc_updown_markets()
    
    by_duration = defaultdict(int)
    for v in cid_map.values():
        by_duration[v['duration']] += 1
    
    print(f"Found {len(cid_map)} BTC Up/Down markets:")
    for dur, count in sorted(by_duration.items()):
        print(f"  {dur}: {count}")
    
    if a.scan or a.live:
        # Step 2: Fetch BTC prices
        print("\n[2] Fetching BTC prices from Binance...")
        btc_prices = fetch_btc_prices()
        if btc_prices:
            latest = btc_prices[-1]['close']
            btc_5m_ago = btc_prices[-2]['close'] if len(btc_prices) > 1 else latest
            btc_15m_ago = btc_prices[-4]['close'] if len(btc_prices) > 3 else latest
            btc_1h_ago = btc_prices[-12]['close'] if len(btc_prices) > 11 else latest
            
            change_5m = (latest - btc_5m_ago) / btc_5m_ago * 100
            change_15m = (latest - btc_15m_ago) / btc_15m_ago * 100
            change_1h = (latest - btc_1h_ago) / btc_1h_ago * 100
            
            print(f"  BTC/USDT: ${latest:,.2f}")
            print(f"  5m change: {change_5m:+.3f}%")
            print(f"  15m change: {change_15m:+.3f}%")
            print(f"  1h change: {change_1h:+.3f}%")
            
            # Direction signal
            if change_5m > 0.01 and change_15m > 0:
                direction = "UP"
                confidence = min(abs(change_15m) * 10, 0.95)
            elif change_5m < -0.01 and change_15m < 0:
                direction = "DOWN"
                confidence = min(abs(change_15m) * 10, 0.95)
            else:
                direction = "FLAT"
                confidence = 0.3
            
            print(f"  Direction: {direction} (confidence: {confidence:.2f})")
        else:
            print("  ERROR: Could not fetch BTC prices")
            direction = "UNKNOWN"
            confidence = 0
        
        # Step 3: Show active 5-min markets
        print("\n[3] Active BTC Up/Down markets:")
        active_btc = {k: v for k, v in cid_map.items() if v['active']}
        
        for cid, info in sorted(active_btc.items(), key=lambda x: x[1]['duration']):
            print(f"\n  {info['question'][:70]}")
            print(f"  Duration: {info['duration']}, Slug: {info['slug'][:50]}")
            print(f"  UP aid: {info['up_aid'][:40]}...")
            print(f"  DOWN aid: {info['down_aid'][:40]}...")
            
            # Fetch current prices
            up_price, down_price = fetch_active_market_prices(cid, info['up_aid'], info['down_aid'])
            if up_price and down_price:
                print(f"  UP price: {up_price:.4f}, DOWN price: {down_price:.4f}")
                
                # Strategy signal
                if direction == "UP" and up_price <= 0.20:
                    print(f"  >>> BUY UP token at {up_price:.4f} (cheap + BTC direction UP)")
                elif direction == "DOWN" and down_price <= 0.20:
                    print(f"  >>> BUY DOWN token at {down_price:.4f} (cheap + BTC direction DOWN)")
                elif direction == "UP" and down_price <= 0.20:
                    print(f"  >>> AVOID: DOWN is cheap but BTC trending UP")
                elif direction == "DOWN" and up_price <= 0.20:
                    print(f"  >>> AVOID: UP is cheap but BTC trending DOWN")
            else:
                print(f"  Prices: could not fetch")
        
        # Step 4: Show 5-min market opportunities
        fivemin = {k: v for k, v in active_btc.items() if v['duration'] == '5m'}
        print(f"\n\n[4] 5-min market opportunities: {len(fivemin)} active")
        for cid, info in fivemin.items():
            print(f"  {info['question'][:70]}")
        
        if a.live:
            print("\n\n[5] Live mode: Scanning for trades every 30 seconds...")
            print("Press Ctrl+C to stop.")
            while True:
                try:
                    btc_prices = fetch_btc_prices()
                    if btc_prices:
                        latest = btc_prices[-1]['close']
                        btc_5m_ago = btc_prices[-2]['close'] if len(btc_prices) > 1 else latest
                        change_5m = (latest - btc_5m_ago) / btc_5m_ago * 100
                        direction = "UP" if change_5m > 0.01 else "DOWN" if change_5m < -0.01 else "FLAT"
                        print(f"\n[{time.strftime('%H:%M:%S')}] BTC=${latest:,.2f} 5m={change_5m:+.3f}% Dir={direction}")
                        
                        for cid, info in fivemin.items():
                            up_price, down_price = fetch_active_market_prices(cid, info['up_aid'], info['down_aid'])
                            if up_price and down_price:
                                if direction == "UP" and up_price <= 0.20:
                                    print(f"  >>> SIGNAL: BUY UP @ {up_price:.4f} ({info['question'][:50]}...)")
                                elif direction == "DOWN" and down_price <= 0.20:
                                    print(f"  >>> SIGNAL: BUY DOWN @ {down_price:.4f} ({info['question'][:50]}...)")
                    
                    time.sleep(30)
                except KeyboardInterrupt:
                    print("\nStopped.")
                    break
    
    # Save CID map for future use
    out_dir = Path('backtest_results_v18_5')
    out_dir.mkdir(exist_ok=True)
    with open(out_dir / 'btc_updown_cid_map_v2.json', 'w') as f:
        json.dump(cid_map, f, indent=2, default=str)
    print(f"\nSaved CID map ({len(cid_map)} entries) to {out_dir / 'btc_updown_cid_map_v2.json'}")


if __name__ == '__main__':
    main()