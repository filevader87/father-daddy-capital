#!/usr/bin/env python3
"""Hermes cron runner for FDC monitoring pipeline - detailed output."""
import sys, json
sys.path.insert(0, "/mnt/c/Users/12035/father_daddy_capital")

from monitoring import MonitoringPipeline

mon = MonitoringPipeline()
result = mon.run_monitored_scan()
print(json.dumps(result, indent=2, default=str))

if "error" in result:
    print(f"ERROR: {result['error']}")
    sys.exit(1)