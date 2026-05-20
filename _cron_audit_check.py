#!/usr/bin/env python3
"""Audit DB check."""
import sqlite3

db = '/mnt/c/Users/12035/father_daddy_capital/output/fdc_audit.db'
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
c = conn.cursor()

c.execute('SELECT COUNT(*) as cnt FROM audit_trail')
total = c.fetchone()['cnt']

c.execute('SELECT event_type, COUNT(*) as cnt FROM audit_trail GROUP BY event_type')
rows = c.fetchall()
print(f'Audit DB: {total} total events')
for r in rows:
    print(f'  {r["event_type"]}: {r["cnt"]}')

c.execute('SELECT ts_utc, event_type, signal_direction, signal_confidence FROM audit_trail ORDER BY id DESC LIMIT 5')
recent = c.fetchall()
print(f'Latest 5 events:')
for r in recent:
    print(f'  {r[0]} | {r[1]} | dir={r[2]} | conf={r[3]}')
conn.close()