#!/usr/bin/env python3
import sqlite3, os
os.chdir("/mnt/c/Users/12035/father_daddy_capital")
db = sqlite3.connect('output/fdc_audit.db')
cur = db.cursor()
cur.execute('SELECT count(*) FROM audit_trail')
total = cur.fetchone()[0]
cur.execute('SELECT id, event_type, ts_utc FROM audit_trail ORDER BY id DESC LIMIT 5')
rows = cur.fetchall()
print(f'Total audit events: {total}')
for r in rows:
    print(f'  #{r[0]}  {r[1]:12s}  {r[2]}')
db.close()