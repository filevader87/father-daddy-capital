#!/usr/bin/env python3
"""Check dashboard data freshness."""
import json, os, glob

# Try to find dashboard data files
for pattern in ['dashboard_data.json', 'audit_trail.json', 'alerts.json', 'dashboard*.json']:
    matches = glob.glob(pattern)
    for f in matches:
        try:
            data = json.load(open(f))
            print(f"\n=== {f} ===")
            if isinstance(data, dict):
                print(f"  Keys: {list(data.keys())[:8]}")
                print(f"  Last updated: {data.get('last_updated', data.get('timestamp', 'N/A'))}")
            elif isinstance(data, list):
                print(f"  Entries: {len(data)}")
                if data and isinstance(data[0], dict):
                    print(f"  First entry keys: {list(data[0].keys())[:6]}")
        except Exception as e:
            print(f"\n=== {f} === (error: {e})")

# Check audit log
for af in ['audit_log.json', 'audit_events.json']:
    if os.path.exists(af):
        try:
            data = json.load(open(af))
            print(f"\n=== {af} ===")
            if isinstance(data, list):
                print(f"  Events: {len(data)}")
            elif isinstance(data, dict):
                print(f"  Events: {len(data.get('events', data.get('entries', [])))}")
        except Exception as e:
            print(f"\n=== {af} === (error: {e})")