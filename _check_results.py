#!/usr/bin/env python3
"""Check latest scan results, dashboard, alerts, and dashboard server status."""
import json, os, subprocess, sys

base = "/mnt/c/Users/12035/father_daddy_capital"

# Latest scan
scans = sorted([f for f in os.listdir(f"{base}/output") if f.startswith("scan_") and f.endswith(".json")])
if scans:
    with open(f"{base}/output/{scans[-1]}") as f:
        data = json.load(f)
    print(f"Latest scan: {scans[-1]}")
    contracts = data.get("contracts", [])
    print(f"Contracts: {len(contracts)}")
    settled = [c for c in contracts if c.get("settled", False)]
    print(f"Settled: {len(settled)}")
    print(f"Alerts in scan: {len(data.get('alerts', []))}")
else:
    print("No scan files found")

# Dashboard
with open(f"{base}/output/dashboard.txt") as f:
    dash = f.read()
print("\n--- Dashboard (first 2000 chars) ---")
print(dash[:2000])

# Alerts
with open(f"{base}/output/alerts.json") as f:
    alerts = json.load(f)
print(f"\n--- Active Alerts: {len(alerts)} ---")
for a in alerts[:10]:
    print(f"  {a.get('level','?')} | {a.get('type','?')} | {str(a.get('message',''))[:80]}")

# Check if dashboard server is running
try:
    result = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "http://localhost:8645/"], 
                          capture_output=True, timeout=5)
    print(f"\nDashboard server at localhost:8645: HTTP {result.stdout.decode().strip()}")
except Exception as e:
    print(f"\nDashboard server check failed: {e}")