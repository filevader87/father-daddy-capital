#!/usr/bin/env python3
"""Check audit DB schema and data."""
import sqlite3, json
from pathlib import Path

DB = Path("/mnt/c/Users/12035/father_daddy_capital/output/fdc_audit.db")
print(f"DB size: {DB.stat().st_size} bytes")

conn = sqlite3.connect(str(DB))

# List all tables
cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in cur.fetchall()]
print(f"\nTables: {tables}")

for table in tables:
    cur = conn.execute(f"SELECT COUNT(*) FROM [{table}]")
    count = cur.fetchone()[0]
    print(f"  {table}: {count} rows")
    cur = conn.execute(f"PRAGMA table_info([{table}])")
    cols = [(r[1], r[2]) for r in cur.fetchall()]
    print(f"    Columns: {cols}")

# Show last 3 entries from each table
for table in tables:
    try:
        cur = conn.execute(f"SELECT * FROM [{table}] ORDER BY rowid DESC LIMIT 3")
        rows = cur.fetchall()
        if rows:
            print(f"\nLast 3 rows from {table}:")
            for r in rows:
                print(f"  {r[:5]}{'...' if len(r) > 5 else ''}")
    except Exception as e:
        print(f"  Error reading {table}: {e}")

conn.close()

# Check alerts
alerts_path = Path("/mnt/c/Users/12035/father_daddy_capital/output/monitoring_alerts.json")
if alerts_path.exists():
    alerts = json.loads(alerts_path.read_text())
    print(f"\n⚠  Active alerts: {len(alerts)}")
    for a in alerts:
        print(f"  [{a.get('level','?')}] {a.get('type','?')}: {a.get('message','')[:80]}")
else:
    print("\n✅ No alerts file (0 active alerts)")