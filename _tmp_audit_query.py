#!/usr/bin/env python3
import sqlite3, sys
sys.path.insert(0, "/mnt/c/Users/12035/father_daddy_capital")
conn = sqlite3.connect("/mnt/c/Users/12035/father_daddy_capital/output/fdc_audit.db")
c = conn.cursor()
c.execute('SELECT COUNT(*) FROM audit_trail')
total = c.fetchone()[0]
c.execute('SELECT event_type, COUNT(*) FROM audit_trail GROUP BY event_type')
rows = c.fetchall()
c.execute('SELECT ts_utc FROM audit_trail ORDER BY id DESC LIMIT 1')
latest = c.fetchone()
conn.close()
print(f'Total audit records: {total}')
for r in rows:
    print(f'  {r[0]}: {r[1]}')
if latest:
    print(f'Latest event: {latest[0]}')