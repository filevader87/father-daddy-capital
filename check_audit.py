#!/usr/bin/env python3
"""Audit trail check."""
import sqlite3
con = sqlite3.connect('output/fdc_audit.db')
cur = con.cursor()
cur.execute('SELECT COUNT(*) FROM audit_trail')
total = cur.fetchone()[0]
cur.execute('SELECT event_type, COUNT(*) FROM audit_trail GROUP BY event_type')
by_type = cur.fetchall()
cur.execute('SELECT id, event_type, ts_utc FROM audit_trail ORDER BY id DESC LIMIT 5')
recent = cur.fetchall()
print(f'Total audit events: {total}')
for t,c in by_type:
    print(f'  {t}: {c}')
print('Most recent:')
for r in recent:
    print(f'  #{r[0]} {r[1]} @ {r[2]}')
con.close()