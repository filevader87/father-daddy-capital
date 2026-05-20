#!/usr/bin/env python3
"""Hermes cron job: Refresh FDC monitoring pipeline, dashboard, audit trail, and alerts."""
import sys, json, traceback
sys.path.insert(0, "/mnt/c/Users/12035/father_daddy_capital")

try:
    from monitoring import MonitoringPipeline, DB_PATH, DASHBOARD_PATH, ALERTS_PATH
    mon = MonitoringPipeline()
    result = mon.run_monitored_scan()
    print(f'Scan: {result.get("entries",0)} entries, {result.get("settled",0)} settled, {result.get("contracts",0)} contracts')
    print(f'Audit events: {result["audit_events"]}, Alerts: {result["alerts_fired"]}')
    print(f'Dashboard: {result["dashboard"]}')
    print("PIPELINE_OK")
except Exception as e:
    traceback.print_exc()
    print(f"PIPELINE_ERROR: {e}")
    sys.exit(1)