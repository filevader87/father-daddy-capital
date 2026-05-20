#!/usr/bin/env python3
"""Run the FDC monitoring pipeline and print results."""
import json
from monitoring import MonitoringPipeline

mon = MonitoringPipeline()
result = mon.run_monitored_scan()

if "error" in result and result.get("entries") is None:
    print(f"ERROR: {result}")
else:
    print(f"Scan: {result.get('entries',0)} entries, {result.get('settled',0)} settled, {result.get('contracts',0)} contracts")
    print(f"Audit events: {result['audit_events']}, Alerts: {result['alerts_fired']}")
    print(f"Dashboard: {result.get('dashboard','')}")