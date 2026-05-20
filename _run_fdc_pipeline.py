#!/usr/bin/env python3
"""FDC Monitoring Pipeline — scheduled run."""
from monitoring import MonitoringPipeline

mon = MonitoringPipeline()
result = mon.run_monitored_scan()

print(f"Scan: {result.get('entries', 0)} entries, "
      f"{result.get('settled', 0)} settled, "
      f"{result.get('contracts', 0)} contracts")
print(f"Audit events: {result['audit_events']}, "
      f"Alerts: {result['alerts_fired']}")
