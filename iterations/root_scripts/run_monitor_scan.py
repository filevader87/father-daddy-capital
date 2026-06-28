#!/usr/bin/env python3
"""One-shot monitoring pipeline runner for cron."""
import sys
sys.path.insert(0, "/mnt/c/Users/12035/father_daddy_capital")

from monitoring import MonitoringPipeline

mon = MonitoringPipeline()
result = mon.run_monitored_scan()

print(f'Scan: {result.get("entries",0)} entries, {result.get("settled",0)} settled, {result.get("contracts",0)} contracts')
print(f'Audit events: {result["audit_events"]}, Alerts: {result["alerts_fired"]}')