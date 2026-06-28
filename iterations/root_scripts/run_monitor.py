#!/usr/bin/env python3
"""Run the FDC monitoring pipeline."""
from monitoring import MonitoringPipeline

mon = MonitoringPipeline()
result = mon.run_monitored_scan()
print('Scan: %s entries, %s settled, %s contracts' % (result.get('entries', 0), result.get('settled', 0), result.get('contracts', 0)))
print('Audit events: %s, Alerts: %s' % (result['audit_events'], result['alerts_fired']))