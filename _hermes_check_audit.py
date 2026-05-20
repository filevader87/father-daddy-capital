#!/usr/bin/env python3
"""Check audit database state."""
import sqlite3, json

db = sqlite3.connect('/mnt/c/Users/12035/father_daddy_capital/output/fdc_audit.db')
cur = db.cursor()
tables = cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print('Tables:', [t[0] for t in tables])
for t in tables:
    count = cur.execute(f'SELECT COUNT(*) FROM {t[0]}').fetchone()[0]
    print(f'  {t[0]}: {count} rows')
rows = cur.execute('SELECT id, event_type, ts_utc FROM audit_trail ORDER BY id DESC LIMIT 5').fetchall()
print('Latest audit events:')
for r in rows:
    print(f'  #{r[0]} | {r[1]} | {r[2]}')
db.close()