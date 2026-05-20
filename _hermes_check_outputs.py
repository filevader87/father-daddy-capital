#!/usr/bin/env python3
"""Check FDC monitoring outputs."""
import json
from pathlib import Path

REPO = Path("/mnt/c/Users/12035/father_daddy_capital")
output = REPO / "output"

# Check dashboard
dash = output / "dashboard.txt"
if dash.exists():
    print("=== DASHBOARD ===")
    print(dash.read_text()[:2000])
else:
    print("No dashboard.txt found")

# Check metrics
met = output / "metrics.json"
if met.exists():
    print("=== METRICS ===")
    data = json.loads(met.read_text())
    print(json.dumps(data, indent=2)[:2000])
else:
    print("No metrics.json found")

# Check alerts
alt = output / "alerts.json"
if alt.exists():
    print("=== ALERTS ===")
    data = json.loads(alt.read_text())
    print(json.dumps(data, indent=2)[:1000])
else:
    print("No alerts.json found")

# Check audit DB
db = output / "fdc_audit.db"
if db.exists():
    import sqlite3
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM audit_trail")
    count = cur.fetchone()[0]
    print(f"\n=== AUDIT DB: {count} total events ===")
    cur.execute("SELECT event_type, ts_utc, signal_direction, signal_confidence FROM audit_trail ORDER BY id DESC LIMIT 5")
    rows = cur.fetchall()
    for r in rows:
        print(f"  {r}")
    conn.close()
else:
    print("No fdc_audit.db found")