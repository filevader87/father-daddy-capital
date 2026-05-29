#!/usr/bin/env python3
"""Check FDC audit DB status."""
import sqlite3, json

db = sqlite3.connect('/mnt/c/Users/12035/father_daddy_capital/output/fdc_audit.db')
total = db.execute('SELECT COUNT(*) FROM audit_trail').fetchone()[0]
types = db.execute('SELECT event_type, COUNT(*) FROM audit_trail GROUP BY event_type').fetchall()
rows = db.execute('SELECT id, row_hash, prev_row_hash FROM audit_trail ORDER BY id').fetchall()
breaks = 0
for i in range(1, len(rows)):
    if rows[i][2] != rows[i-1][1]:
        breaks += 1
latest = db.execute('SELECT ts_utc, event_type FROM audit_trail ORDER BY id DESC LIMIT 1').fetchone()
db.close()
print(f'Total events: {total}')
for t, c in types:
    print(f'  {t}: {c}')
print(f'Chain integrity: {"INTACT" if breaks == 0 else f"BROKEN ({breaks} breaks)"}')
print(f'Latest event: {latest[1]} at {latest[0]}')