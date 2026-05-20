#!/usr/bin/env python3
import sqlite3
conn = sqlite3.connect('/mnt/c/Users/12035/father_daddy_capital/output/fdc_audit.db')
cur = conn.cursor()
cur.execute('SELECT COUNT(*) FROM audit_trail')
total = cur.fetchone()[0]
cur.execute("SELECT event_type, COUNT(*) FROM audit_trail GROUP BY event_type")
types = cur.fetchall()
cur.execute("""SELECT COUNT(*) FROM audit_trail WHERE ts_utc > datetime('now', '-1 hour')""")
recent = cur.fetchone()[0]
print(f'Total events: {total}')
for t, c in types:
    print(f'  {t}: {c}')
print(f'Last hour: {recent}')
conn.close()