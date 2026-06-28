#!/usr/bin/env python3
"""Check monitoring artifacts."""
import os

# Check audit log
audit_dir = 'audit_trail'
if os.path.isdir(audit_dir):
    files = sorted(os.listdir(audit_dir))
    if files:
        latest = os.path.join(audit_dir, files[-1])
        with open(latest) as f:
            lines = f.readlines()
        print(f'Audit trail: {len(lines)} events in latest file ({files[-1]})')
    else:
        print('Audit trail: directory exists but empty')
else:
    print('Audit trail: directory not found')

# Check alerts
alerts_dir = 'alerts'
if os.path.isdir(alerts_dir):
    afiles = sorted(os.listdir(alerts_dir))
    print(f'Alerts directory: {len(afiles)} files')
    for af in afiles[-3:]:
        with open(os.path.join(alerts_dir, af)) as f:
            content = f.read().strip()
        print(f'  {af}: {content[:120]}')
else:
    print('Alerts: directory not found')

# Check dashboard data
dash_dir = 'dashboard_data'
if os.path.isdir(dash_dir):
    dfiles = sorted(os.listdir(dash_dir))
    print(f'Dashboard data: {len(dfiles)} files')
    for df in dfiles[-3:]:
        size = os.path.getsize(os.path.join(dash_dir, df))
        print(f'  {df}: {size} bytes')
else:
    print('Dashboard data: directory not found')