#!/usr/bin/env python3
"""Quick diagnostic: check book data availability for BTC contracts."""
import sys
sys.path.insert(0, '.')
sys.path.insert(0, 'src')
from pm_engine_v19_8 import discover_contracts_multi, get_clob_book_depth

contracts_dict = discover_contracts_multi(asset_key='BTC')
contracts = []
for k, v in contracts_dict.items():
    if isinstance(v, list):
        contracts.extend(v)

print(f"Total contracts: {len(contracts)}")

for c in contracts:
    slug = c.get('slug', '?')
    cond_id = c.get('conditionId', '')
    up_tid = c.get('up_token_id', '')
    down_tid = c.get('down_token_id', '')
    ask = c.get('ask', c.get('up_price', 0))
    
    # Only check 0.50-0.60 bucket
    if not (0.50 <= float(ask) < 0.60):
        print(f"  {slug}: ask={ask} OUTSIDE bucket, skipping")
        continue
    
    print(f"{slug}: ask={ask}")
    print(f"  cond_id={cond_id[:24]}...")
    print(f"  up_tid={up_tid[:24] if up_tid else 'EMPTY'}...")
    
    try:
        if up_tid:
            book = get_clob_book_depth(cond_id, token_id=up_tid)
        else:
            book = get_clob_book_depth(cond_id)
        if book:
            print(f"  Book OK: depth=${book.get('depth_usd', 0):.0f} spread={book.get('spread', 0)} bid={book.get('best_bid', 0)} ask={book.get('best_ask', 0)}")
        else:
            print(f"  Book: None returned (no data)")
    except Exception as e:
        print(f"  Book error: {e}")