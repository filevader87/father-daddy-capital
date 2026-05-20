#!/usr/bin/env python3
"""Audit check for FDC monitoring pipeline."""
import sqlite3

conn = sqlite3.connect('output/fdc_audit.db')
c = conn.cursor()

print('=== Total Events ===')
for row in c.execute('SELECT event_type, COUNT(*) FROM audit_trail GROUP BY event_type'):
    print(f'  {row[0]}: {row[1]}')

print()
print('=== Latest 5 Events ===')
for row in c.execute('SELECT id, event_type, ts_utc, signal_direction, order_bet FROM audit_trail ORDER BY id DESC LIMIT 5'):
    print(f'  id={row[0]} type={row[1]} ts={row[2]} dir={row[3]} bet={row[4]}')

print()
print('=== Integrity Check ===')
rows = c.execute('SELECT id, row_hash, prev_row_hash FROM audit_trail ORDER BY id').fetchall()
for row in rows:
    h = row[1][:16] if row[1] else 'None'
    p = row[2][:16] if row[2] else 'None'
    print(f'  id={row[0]} hash={h}... prev={p}...')

if len(rows) > 1:
    print('  CHAIN: All rows linked ✅' if all(
        rows[i][2] == rows[i-1][1] for i in range(1, len(rows))
    ) else '  CHAIN: BROKEN ❌')

conn.close()