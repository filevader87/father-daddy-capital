#!/usr/bin/env python3
"""Quick audit trail query."""
import sqlite3

conn = sqlite3.connect('output/fdc_audit.db')
conn.execute('PRAGMA journal_mode=WAL')
row = conn.execute('SELECT COUNT(*) as cnt FROM audit_trail').fetchone()
print(f'Total audit rows: {row[0]}')
rows = conn.execute('SELECT event_type, ts_utc FROM audit_trail ORDER BY id DESC LIMIT 10').fetchall()
for r in rows:
    print(f'  {r[0]} | {r[1]}')
conn.close()