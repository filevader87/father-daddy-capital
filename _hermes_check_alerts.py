#!/usr/bin/env python3
"""Hermes cron: check alerts."""
import json
from pathlib import Path

alerts_path = Path("/mnt/c/Users/12035/father_daddy_capital/output/alerts.json")
alerts = json.loads(alerts_path.read_text())
print(f"Alert type: {type(alerts)}")
if isinstance(alerts, dict):
    keys = list(alerts.keys())[:10]
    print(f"Keys: {keys}")
    for k in keys:
        v = alerts[k]
        print(f"  {k}: {str(v)[:200]}")
elif isinstance(alerts, list):
    for a in alerts[:5]:
        print(json.dumps(a, default=str)[:200])