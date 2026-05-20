#!/usr/bin/env python3
"""Check latest FDC dashboard data and scan results."""
import json, os
from datetime import datetime

base = "/mnt/c/Users/12035/father_daddy_capital"

# Check alerts
alerts_path = os.path.join(base, "output/alerts.json")
if os.path.exists(alerts_path):
    alerts = json.load(open(alerts_path))
    print(f"=== Alerts ({type(alerts).__name__}) ===")
    if isinstance(alerts, list):
        print(f"  Total alerts: {len(alerts)}")
        if alerts:
            print(f"  Latest: {alerts[-1] if len(alerts) < 3 else alerts[-1]}")
    elif isinstance(alerts, dict):
        print(f"  Keys: {list(alerts.keys())}")
        for k in list(alerts.keys())[:5]:
            v = alerts[k]
            if isinstance(v, list):
                print(f"  {k}: {len(v)} items")
            else:
                print(f"  {k}: {v}")

# Check latest scan
scan_path = os.path.join(base, "output/scan_20260520_1301.json")
if os.path.exists(scan_path):
    scan = json.load(open(scan_path))
    print(f"\n=== Latest Scan ===")
    if isinstance(scan, dict):
        print(f"  Keys: {list(scan.keys())[:10]}")
        print(f"  Entries: {scan.get('entries', 'N/A')}")
        print(f"  Contracts: {scan.get('contracts', 'N/A')}")
        print(f"  Settled: {scan.get('settled', 'N/A')}")
        print(f"  Timestamp: {scan.get('timestamp', scan.get('scan_time', 'N/A'))}")

# Check dashboard state files
for fname in ['pm_state.json', 'paper_state.json', 'arb_state.json', 'macro_signals.json', 'calibration.json']:
    fpath = os.path.join(base, "output", fname)
    if os.path.exists(fpath):
        data = json.load(open(fpath))
        print(f"\n=== {fname} ===")
        if isinstance(data, dict):
            keys = list(data.keys())[:6]
            print(f"  Keys: {keys}")
            if 'last_updated' in data:
                print(f"  Last updated: {data['last_updated']}")
            if 'timestamp' in data:
                print(f"  Timestamp: {data['timestamp']}")

# Check neural weights
neural_path = os.path.join(base, "neural_weights/bayesian_state.json")
if os.path.exists(neural_path):
    neural = json.load(open(neural_path))
    print(f"\n=== Bayesian State ===")
    if isinstance(neural, dict):
        print(f"  Keys: {list(neural.keys())[:6]}")