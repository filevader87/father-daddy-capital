#!/usr/bin/env python3
"""Quick market check — see what Polymarket is returning."""
import sys
sys.path.insert(0, '/mnt/c/Users/12035/father_daddy_capital')
from paper_trade_v19_2 import fetch_5m_15m_markets

markets = fetch_5m_15m_markets()
print(f"Total markets: {len(markets)}")
for m in markets[:20]:
    q = m.get('question','')[:55]
    mins = m.get('minutes_left', 0)
    w = m.get('window_mins', '?')
    up = m.get('up_price',0)*100
    down = m.get('down_price',0)*100
    cheap = m.get('cheap_side','?')
    cp = m.get('cheap_price',0)*100
    slug = m.get('series_slug','?')[:20]
    print(f"  {q} | {mins:.1f}min | {w}w | Up={up:.1f}¢ Down={down:.1f}¢ | cheap={cheap}@{cp:.1f}¢ | {slug}")