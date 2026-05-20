#!/usr/bin/env python3
"""FDC Monitoring Pipeline cron refresh."""
import sys, json, traceback
sys.path.insert(0, "/mnt/c/Users/12035/father_daddy_capital")

try:
    from monitoring import MonitoringPipeline
    mon = MonitoringPipeline()
    result = mon.run_monitored_scan()
    print(f"Scan: {result.get('entries',0)} entries, {result.get('settled',0)} settled, {result.get('contracts',0)} contracts")
    print(f"Audit events: {result.get('audit_events', '-')}, Alerts: {result.get('alerts_fired', '-')}")
    if 'error' in result:
        print(f"Error: {result['error']}")
    # Print key details
    print(f"Dashboard: {result.get('dashboard', '-')}")
except Exception as e:
    print(f"Pipeline error: {e}")
    traceback.print_exc()