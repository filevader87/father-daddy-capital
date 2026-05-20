#!/usr/bin/env python3
"""Hermes cron: run monitoring pipeline scan."""
import json, sys, traceback
from datetime import datetime

print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Starting monitoring pipeline scan...")

try:
    from monitoring import MonitoringPipeline
    mon = MonitoringPipeline()
    result = mon.run_monitored_scan()
    print(f'Scan: {result.get("entries",0)} entries, {result.get("settled",0)} settled, {result.get("contracts",0)} contracts')
    print(f'Audit events: {result["audit_events"]}, Alerts: {result["alerts_fired"]}')
    print(json.dumps(result, indent=2, default=str))
except Exception as e:
    traceback.print_exc()
    sys.exit(1)