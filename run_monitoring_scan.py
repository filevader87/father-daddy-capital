#!/usr/bin/env python3
"""One-shot FDC monitoring pipeline run."""
from monitoring import MonitoringPipeline

mon = MonitoringPipeline()
result = mon.run_monitored_scan()

print(f"Scan: {result.get('entries',0)} entries, {result.get('settled',0)} settled, {result.get('contracts',0)} contracts")
print(f"Audit events: {result.get('audit_events',0)}, Alerts: {result.get('alerts_fired',0)}")