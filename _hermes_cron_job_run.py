#!/usr/bin/env python3
"""Hermes cron job: Run FDC monitoring pipeline."""
import sys
import traceback

try:
    from monitoring import MonitoringPipeline
    mon = MonitoringPipeline()
    result = mon.run_monitored_scan()
    entries = result.get("entries", 0)
    settled = result.get("settled", 0)
    contracts = result.get("contracts", 0)
    audit_events = result.get("audit_events", 0)
    alerts_fired = result.get("alerts_fired", 0)
    print(f"Scan: {entries} entries, {settled} settled, {contracts} contracts")
    print(f"Audit events: {audit_events}, Alerts: {alerts_fired}")
    print("PIPELINE_OK")
except Exception as e:
    traceback.print_exc()
    print(f"PIPELINE_ERROR: {e}")
    sys.exit(1)