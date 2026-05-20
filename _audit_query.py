#!/usr/bin/env python3
"""Query audit DB for status."""
import sqlite3
conn = sqlite3.connect('output/fdc_audit.db')
r = conn.execute('SELECT COUNT(*) FROM audit_trail')
print(f'Total audit rows: {r.fetchone()[0]}')
r = conn.execute('SELECT event_type, COUNT(*) FROM audit_trail GROUP BY event_type')
print('By type:')
for row in r:
    print(f'  {row[0]}: {row[1]}')
conn.close()