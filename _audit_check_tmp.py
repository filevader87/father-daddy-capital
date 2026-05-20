#!/usr/bin/env python3
import sqlite3
db = sqlite3.connect("output/fdc_audit.db")
c = db.execute("SELECT COUNT(*) FROM audit_trail")
print("Total audit events:", c.fetchone()[0])
c = db.execute("SELECT event_type, COUNT(*) FROM audit_trail GROUP BY event_type")
for r in c.fetchall():
    print(f"  {r[0]}: {r[1]}")
c = db.execute("SELECT MAX(ts_utc) FROM audit_trail")
print("Latest:", c.fetchone()[0])
db.close()