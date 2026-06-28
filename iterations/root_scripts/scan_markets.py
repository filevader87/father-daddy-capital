#!/usr/bin/env python3
"""Quick market scanner - check live spread conditions."""
import time, urllib.request, json

GAMMA_URL = 'https://gamma-api.polymarket.com'
CLOB_URL = 'https://clob.polymarket.com'

now_ts = int(time.time())
boundary_5m = (now_ts // 300) * 300
boundary_15m = (now_ts // 900) * 900

for asset in ['btc', 'eth', 'sol', 'xrp']:
    for interval_sec, label in [(300, '5m'), (900, '15m')]:
        boundary = (now_ts // interval_sec) * interval_sec
        for offset in range(0, 3):
            ts = boundary + offset * interval_sec
            time_to_res = (ts + interval_sec) - now_ts
            slug = f'{asset}-updown-{label}-{ts}'
            url = f'{GAMMA_URL}/markets?active=true&closed=false&limit=1&slug={slug}'
            req = urllib.request.Request(url, headers={'User-Agent': 'fdc/v2031'})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    data = json.loads(r.read())
                if not data:
                    continue
                tokens = json.loads(data[0].get('clobTokenIds', '[]'))
                if len(tokens) < 2:
                    continue
                book_url = f'{CLOB_URL}/book?token_id={tokens[0]}'
                req2 = urllib.request.Request(book_url, headers={'User-Agent': 'fdc/v2031'})
                with urllib.request.urlopen(req2, timeout=5) as r2:
                    book = json.loads(r2.read())
                bids = book.get('bids', [])[:2]
                asks = book.get('asks', [])[:2]
                bid_p = float(bids[0]['price']) if bids else 0
                ask_p = float(asks[0]['price']) if asks else 0
                spread = ask_p - bid_p if bids and asks else 999
                in_window = "YES" if 30 < time_to_res < 300 else "no"
                print(f'{asset}-{label} offset={offset:+d} ttr={time_to_res:4d}s  bid={bid_p:.3f} ask={ask_p:.3f} spread={spread:.3f}  enter={in_window}')
            except Exception as e:
                print(f'{asset}-{label} offset={offset:+d}: error {e}')