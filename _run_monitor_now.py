#!/usr/bin/env python3
"""One-shot FDC monitoring pipeline run."""
import sys, json, traceback
sys.path.insert(0, "/mnt/c/Users/12035/father_daddy_capital")

try:
    from monitoring import MonitoringPipeline
    mon = MonitoringPipeline()
    result = mon.run_monitored_scan()
    print(f'Scan: {result.get("entries",0)} entries, {result.get("settled",0)} settled, {result.get("contracts",0)} contracts')
    print(f'Audit events: {result["audit_events"]}, Alerts: {result["alerts_fired"]}')
except Exception as e:
    traceback.print_exc()
    sys.exit(1)