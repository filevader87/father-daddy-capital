#!/usr/bin/env python3
"""One-shot runner for the FDC monitoring pipeline."""
from monitoring import MonitoringPipeline

def main():
    mon = MonitoringPipeline()
    result = mon.run_monitored_scan()
    print(f'Scan: {result.get("entries",0)} entries, {result.get("settled",0)} settled, {result.get("contracts",0)} contracts')
    print(f'Audit events: {result["audit_events"]}, Alerts: {result["alerts_fired"]}')

if __name__ == "__main__":
    main()