#!/usr/bin/env python3
"""Hermes cron: run FDC monitoring pipeline and print summary."""
import sys
sys.path.insert(0, '.')
from monitoring import MonitoringPipeline

mon = MonitoringPipeline()
result = mon.run_monitored_scan()
print(f'Scan: {result.get("entries",0)} entries, {result.get("settled",0)} settled, {result.get("contracts",0)} contracts')
print(f'Audit events: {result["audit_events"]}, Alerts: {result["alerts_fired"]}')