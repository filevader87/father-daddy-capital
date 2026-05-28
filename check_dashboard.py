#!/usr/bin/env python3
import json

f = open('dashboard_data.json')
d = json.load(f)
f.close()

print('Dashboard keys:', list(d.keys()))
print('Last updated:', d.get('last_updated', 'N/A'))
contracts = d.get('contracts', {})
print(f'Contracts: {len(contracts)}')
settled = [c for c in contracts.values() if c.get('status') == 'settled']
print(f'Settled: {len(settled)}')
alerts = d.get('alerts', [])
print(f'Alerts: {len(alerts)}')
audit = d.get('audit_trail', [])
print(f'Audit entries: {len(audit)}')