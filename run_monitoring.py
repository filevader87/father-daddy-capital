#!/usr/bin/env python3
"""One-shot FDC monitoring pipeline runner for cron."""
import sys
sys.path.insert(0, "/mnt/c/Users/12035/father_daddy_capital")

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