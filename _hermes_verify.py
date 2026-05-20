#!/usr/bin/env python3
"""Hermes cron: post-scan verification of audit trail, alerts, and metrics."""
import sqlite3, json
from pathlib import Path

REPO = Path("/mnt/c/Users/12035/father_daddy_capital")

# Audit trail stats
db = REPO / "output" / "fdc_audit.db"
if db.exists():
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as cnt FROM audit_trail")
    total = cur.fetchone()["cnt"]
    cur.execute("SELECT COUNT(*) as cnt FROM audit_trail WHERE event_type='scan'")
    scans = cur.fetchone()["cnt"]
    cur.execute("SELECT COUNT(*) as cnt FROM audit_trail WHERE event_type='entry'")
    entries = cur.fetchone()["cnt"]
    cur.execute("SELECT COUNT(*) as cnt FROM audit_trail WHERE event_type='settlement'")
    settlements = cur.fetchone()["cnt"]
    print(f"Audit DB: {total} total events (scans={scans}, entries={entries}, settlements={settlements})")
    cur.execute("SELECT id FROM audit_trail ORDER BY id DESC LIMIT 1")
    last = cur.fetchone()
    print(f"Latest audit row: id={last['id']}")
    conn.close()
else:
    print("Audit DB not found")

# Alerts
alerts = REPO / "output" / "alerts.json"
if alerts.exists():
    data = json.loads(alerts.read_text())
    print(f"Alerts: {len(data.get('active', []))} active")
else:
    print("No alerts file")

# Metrics
metrics = REPO / "output" / "metrics.json"
if metrics.exists():
    data = json.loads(metrics.read_text())
    print(f"Metrics: last_updated={data.get('last_updated', '?')}")