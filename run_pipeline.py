#!/usr/bin/env python3
"""Run the FDC monitoring pipeline and report results."""
from monitoring import MonitoringPipeline

mon = MonitoringPipeline()
result = mon.run_monitored_scan()

entries = result.get("entries", 0)
settled = result.get("settled", 0)
contracts = result.get("contracts", 0)
audit_events = result["audit_events"]
alerts_fired = result["alerts_fired"]

print(f'Scan: {entries} entries, {settled} settled, {contracts} contracts')
print(f'Audit events: {audit_events}, Alerts: {alerts_fired}')