#!/usr/bin/env python3
"""Hermes cron: FDC monitoring pipeline refresh."""
import sys
import traceback

try:
    sys.path.insert(0, "/mnt/c/Users/12035/father_daddy_capital")
    from monitoring import MonitoringPipeline
    mon = MonitoringPipeline()
    result = mon.run_monitored_scan()
    entries = result.get("entries", 0)
    settled = result.get("settled", 0)
    contracts = result.get("contracts", 0)
    audit = result.get("audit_events", 0)
    alerts = result.get("alerts_fired", 0)
    print(f"Scan: {entries} entries, {settled} settled, {contracts} contracts")
    print(f"Audit events: {audit}, Alerts: {alerts}")
    print("Pipeline run completed successfully.")
except Exception as e:
    traceback.print_exc()
    print(f"ERROR: {e}")
    sys.exit(1)