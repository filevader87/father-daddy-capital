#!/usr/bin/env python3
"""Quick audit check."""
import sqlite3
conn = sqlite3.connect('/mnt/c/Users/12035/father_daddy_capital/output/fdc_audit.db')
print('Total rows:', conn.execute('SELECT COUNT(*) FROM audit_trail').fetchone()[0])
print('Events by type:')
for row in conn.execute('SELECT event_type, COUNT(*) FROM audit_trail GROUP BY event_type'):
    print(f'  {row[0]}: {row[1]}')
# Show last 5 rows
print('\nLast 5 events:')
for row in conn.execute('SELECT id, event_type, ts_utc, substr(signal_raw_json,1,60) FROM audit_trail ORDER BY id DESC LIMIT 5'):
    print(f'  #{row[0]} {row[1]} @ {row[2]}: {row[3]}')
# Integrity check
rows = conn.execute('SELECT id, row_hash, prev_row_hash FROM audit_trail ORDER BY id').fetchall()
breaks = 0
for i in range(1, len(rows)):
    if rows[i][2] != rows[i-1][1]:
        breaks += 1
        print(f'  CHAIN BREAK at row {rows[i][0]}')
print(f'Chain integrity: {"INTACT" if breaks == 0 else f"BROKEN ({breaks} breaks)"}')
conn.close()