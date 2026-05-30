#!/usr/bin/env python3
import sqlite3
db = sqlite3.connect("/mnt/c/Users/12035/father_daddy_capital/output/fdc_audit.db")
cur = db.cursor()
cur.execute("SELECT COUNT(*) FROM audit_trail")
total = cur.fetchone()[0]
cur.execute("SELECT event_type, COUNT(*) FROM audit_trail GROUP BY event_type")
breakdown = cur.fetchall()
cur.execute("SELECT MAX(ts_utc) FROM audit_trail")
last_ts = cur.fetchone()[0]
print(f"Total audit rows: {total}")
for et, cnt in breakdown:
    print(f"  {et}: {cnt}")
print(f"Last event: {last_ts}")
db.close()