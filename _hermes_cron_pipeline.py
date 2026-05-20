#!/usr/bin/env python3
"""Hermes cron job: FDC monitoring pipeline scan."""
import json, sys, traceback
sys.path.insert(0, "/mnt/c/Users/12035/father_daddy_capital")

try:
    from monitoring import MonitoringPipeline
    mon = MonitoringPipeline()
    result = mon.run_monitored_scan()
    print("Scan: {} entries, {} settled, {} contracts".format(
        result.get("entries", 0), result.get("settled", 0), result.get("contracts", 0)))
    print("Audit events: {}, Alerts: {}".format(
        result["audit_events"], result["alerts_fired"]))
    print("Dashboard: {}".format(result.get("dashboard", "N/A")))

    # Also output the dashboard file content if it exists
    from monitoring import DASHBOARD_PATH
    if DASHBOARD_PATH.exists():
        print("\n--- Dashboard Preview ---")
        print(DASHBOARD_PATH.read_text()[:2000])

    # Check alerts
    from monitoring import ALERTS_PATH
    if ALERTS_PATH.exists():
        alerts_data = json.loads(ALERTS_PATH.read_text())
        print("\n--- Active Alerts ---")
        print(json.dumps(alerts_data, indent=2)[:1500])

except Exception as e:
    traceback.print_exc()
    print("ERROR: {}".format(e))
    sys.exit(1)