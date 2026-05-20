#!/usr/bin/env python3
"""Hermes cron: check FDC outputs after pipeline."""
import json
from pathlib import Path

repo = Path("/mnt/c/Users/12035/father_daddy_capital")

# Check metrics
metrics_path = repo / "output" / "metrics.json"
if metrics_path.exists():
    metrics = json.loads(metrics_path.read_text())
    print("=== Metrics ===")
    print(json.dumps(metrics, indent=2, default=str)[:2000])

# Check dashboard
dash_path = repo / "output" / "dashboard.txt"
if dash_path.exists():
    print("\n=== Dashboard (last 30 lines) ===")
    lines = dash_path.read_text().splitlines()
    for line in lines[-30:]:
        print(line)

# Check alerts
alerts_path = repo / "output" / "alerts.json"
if alerts_path.exists():
    alerts = json.loads(alerts_path.read_text())
    print(f"\n=== Alerts: {len(alerts)} active ===")
    if alerts:
        print(json.dumps(alerts[:5], indent=2, default=str)[:1000])
    else:
        print("None")

# Check audit DB
db_path = repo / "output" / "fdc_audit.db"
if db_path.exists():
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    count = cur.execute("SELECT COUNT(*) FROM audit_trail").fetchone()[0]
    recent = cur.execute("SELECT event_type, ts_utc, summary FROM audit_trail ORDER BY id DESC LIMIT 5").fetchall()
    print(f"\n=== Audit DB: {count} total events ===")
    for row in recent:
        print(f"  {row[0]} | {row[1]} | {row[2][:80] if row[2] else 'N/A'}")
    conn.close()