#!/usr/bin/env python3
"""FDC monitoring pipeline cron scan - Hermes Agent"""
import sys
import os
import json
from datetime import datetime

os.chdir("/mnt/c/Users/12035/father_daddy_capital")
sys.path.insert(0, ".")

from monitoring import MonitoringPipeline

def main():
    print(f"[{datetime.now().isoformat()}] Starting monitored scan...")
    mon = MonitoringPipeline()
    result = mon.run_monitored_scan()
    
    entries = result.get("entries", 0)
    settled = result.get("settled", 0)
    contracts = result.get("contracts", 0)
    audit_events = result.get("audit_events", 0)
    alerts_fired = result.get("alerts_fired", 0)
    
    print(f"Scan: {entries} entries, {settled} settled, {contracts} contracts")
    print(f"Audit events: {audit_events}, Alerts: {alerts_fired}")
    print(f"[{datetime.now().isoformat()}] Scan complete.")
    
    # Check dashboard server status
    import urllib.request
    try:
        resp = urllib.request.urlopen("http://localhost:8645/", timeout=5)
        print(f"Dashboard server: online (status {resp.status})")
    except Exception as e:
        print(f"Dashboard server: offline or unreachable ({e})")

if __name__ == "__main__":
    main()