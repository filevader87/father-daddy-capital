#!/usr/bin/env python3
"""One-shot cron script to run the FDC monitoring pipeline."""
import sys, os
sys.path.insert(0, "/mnt/c/Users/12035/father_daddy_capital")
os.chdir("/mnt/c/Users/12035/father_daddy_capital")

from monitoring import MonitoringPipeline
mon = MonitoringPipeline()
result = mon.run_monitored_scan()
print(f'Scan: {result.get("entries",0)} entries, {result.get("settled",0)} settled, {result.get("contracts",0)} contracts')
print(f'Audit events: {result.get("audit_events",0)}, Alerts: {result.get("alerts_fired",0)}')