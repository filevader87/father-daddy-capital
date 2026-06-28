#!/usr/bin/env python3
"""Check FDC audit DB and alert status."""
import sqlite3
import os

db_path = '/mnt/c/Users/12035/father_daddy_capital/output/fdc_audit.db'
if os.path.exists(db_path):
    db = sqlite3.connect(db_path)
    total = db.execute('SELECT COUNT(*) FROM audit_trail').fetchone()[0]
    types = db.execute('SELECT event_type, COUNT(*) FROM audit_trail GROUP BY event_type').fetchall()
    latest = db.execute('SELECT id, event_type, ts_utc FROM audit_trail ORDER BY id DESC LIMIT 5').fetchall()
    print(f"Audit DB: {total} total events")
    print(f"Event types: {dict(types)}")
    print(f"Latest events:")
    for row in latest:
        print(f"  id={row[0]} type={row[1]} ts={row[2]}")
    
    # Check chain integrity
    rows = db.execute('SELECT id, row_hash, prev_row_hash FROM audit_trail ORDER BY id').fetchall()
    chain_ok = True
    for i in range(1, len(rows)):
        if rows[i][2] != rows[i-1][1]:
            chain_ok = False
            break
    print(f"Chain integrity: {'INTACT' if chain_ok else 'BROKEN'}")
    db.close()
else:
    print("No audit DB found")

# Check for alerts
alert_files = [
    '/mnt/c/Users/12035/father_daddy_capital/output/alerts.json',
    '/mnt/c/Users/12035/father_daddy_capital/alerts.json',
]
for af in alert_files:
    if os.path.exists(af):
        import json
        with open(af) as f:
            alerts = json.load(f)
        print(f"Alerts file {af}: {len(alerts)} alerts")