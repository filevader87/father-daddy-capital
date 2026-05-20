#!/usr/bin/env python3
import sqlite3
conn = sqlite3.connect('output/fdc_audit.db')
cur = conn.cursor()
cur.execute('SELECT COUNT(*) FROM audit_trail')
total = cur.fetchone()[0]
cur.execute("SELECT event_type, COUNT(*) FROM audit_trail GROUP BY event_type")
types = cur.fetchall()
cur.execute('SELECT MAX(ts_utc) FROM audit_trail')
last_ts = cur.fetchone()[0]
cur.execute("SELECT id, event_type, ts_utc, signal_direction, signal_confidence, debate_verdict FROM audit_trail ORDER BY id DESC LIMIT 5")
recent = cur.fetchall()
conn.close()
print(f'Total audit events: {total}')
print(f'Event types: {types}')
print(f'Last event: {last_ts}')
print(f'Recent 5 events:')
for r in recent:
    print(f'  id={r[0]} type={r[1]} ts={r[2]} dir={r[3]} conf={r[4]} verdict={r[5]}')