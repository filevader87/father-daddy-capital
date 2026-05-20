import json
from pathlib import Path

REPO = Path("/mnt/c/Users/12035/father_daddy_capital")

alerts = REPO / "output" / "alerts.json"
if alerts.exists():
    data = json.loads(alerts.read_text())
    if isinstance(data, list):
        print(f"Alerts file: {len(data)} alerts")
        for a in data[-5:]:
            print(f"  [{a.get('level','?')}] {a.get('message','?')}")
    elif isinstance(data, dict):
        print(f"Alerts file: dict keys={list(data.keys())}")
        for k,v in list(data.items())[:5]:
            print(f"  {k}: {v}")
else:
    print("No alerts file")

metrics = REPO / "output" / "metrics.json"
if metrics.exists():
    m = json.loads(metrics.read_text())
    print(f"Metrics: {list(m.keys())[:10]}")
else:
    print("No metrics file")