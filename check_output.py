import json
from pathlib import Path

OUT = Path("/mnt/c/Users/12035/father_daddy_capital/output")

# Dashboard
dash = OUT / "dashboard.txt"
if dash.exists():
    print("=== DASHBOARD ===")
    print(dash.read_text()[:2000])

# Alerts
alerts = OUT / "alerts.json"
if alerts.exists():
    print("\n=== ALERTS ===")
    print(alerts.read_text()[:1000])

# Metrics
metrics = OUT / "metrics.json"
if metrics.exists():
    print("\n=== METRICS ===")
    print(metrics.read_text()[:1000])