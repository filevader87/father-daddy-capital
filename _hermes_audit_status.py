#!/usr/bin/env python3
"""Hermes cron: Audit status check."""
import sys, json
sys.path.insert(0, "/mnt/c/Users/12035/father_daddy_capital")

from monitoring import MonitoringPipeline
mon = MonitoringPipeline()
stats = mon.audit.stats()
print('=== AUDIT TRAIL STATS ===')
for k,v in stats.items():
    print(f'  {k}: {v}')