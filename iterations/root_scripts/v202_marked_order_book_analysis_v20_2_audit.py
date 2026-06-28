
#!/usr/bin/env python3
"""
V20.2 Market Structure Audit - Polymarket CLOB Order Book Analytics


PURPOSE: Gather real-time order book snapshots for BTC up/down (updown-xxxx) markets on https://clob.polymarket.com  
OUTPUT FILE: V20.2_MARKET_STRUCTURE_AUDIT.csv  

REQUIRED MEASUREMENTS PER TASK SPEC
===================================== 
1. For each market discovered via Gamma API with slug starting 'btc-updown-', fetch full order book using CLOB API
   2. Record per-market metrics: [slug, condition_id, up_token_id,...]
3. Compute logged metrics: executable_depth (0.5-<0.6 bucket), spread_persistence (% >1/4 cents threshold = high), 
                        midpoint_stability (#M=exactly_0.50 marks across all readings)  
   4. Repricing_frequency via top-of-book stability analysis within each polling window
5. CSV + summary: total polls, %spreads>threshold, %at_midpoint(Exactly M===1/2), executable volume in (0.5-<0.6): 
                      threshold=low_spread (< 1/4 cents = high liquidity)


USAGE EXAMPLES
========================================
python v202_market_structure_audit.py                              # poll BTC up/down markets continuously until timeout 

"""  

import sys, time  
from datetime import datetime  
from collections 
  