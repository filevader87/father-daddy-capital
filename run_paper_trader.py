#!/usr/bin/env python3
"""V19.7h Paper Trading Runner — loops every 30s for high-frequency scanning.

Runs for 4.5 minutes (9 cycles at 30s), called by 5-minute cron.
"""

import sys, time
sys.path.insert(0, '/mnt/c/Users/12035/father_daddy_capital')
from paper_trader_v19_7h import run_paper_cycle

CYCLE_INTERVAL = 30  # seconds
MAX_CYCLES = 9  # ~4.5 minutes per cron invocation

for i in range(MAX_CYCLES):
    try:
        print(f"\n{'─'*70}")
        print(f"Cycle {i+1}/{MAX_CYCLES}")
        run_paper_cycle()
    except Exception as e:
        print(f"CYCLE ERROR: {e}")
        import traceback
        traceback.print_exc()
    
    if i < MAX_CYCLES - 1:
        time.sleep(CYCLE_INTERVAL)