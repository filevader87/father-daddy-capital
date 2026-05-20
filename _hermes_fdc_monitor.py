#!/usr/bin/env python3
"""Hermes cron: run FDC monitoring pipeline to refresh dashboard, audit trail, and alerts."""
import sys, os, json, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

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
except Exception as e:
    traceback.print_exc()
    print(f"ERROR: {e}")
    sys.exit(1)