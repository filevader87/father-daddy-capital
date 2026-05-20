#!/usr/bin/env python3
"""Quick audit DB query for Hermes cron."""
import sqlite3
conn = sqlite3.connect("output/fdc_audit.db")
c = conn.cursor()
c.execute("SELECT count(*) FROM audit_trail")
total = c.fetchone()[0]
c.execute("SELECT event_type, count(*) FROM audit_trail GROUP BY event_type")
rows = c.fetchall()
c.execute("SELECT max(ts_utc) FROM audit_trail")
last_ts = c.fetchone()[0]
try:
    c.execute("SELECT count(*) FROM audit_chain")
    chain = c.fetchone()[0]
except Exception:
    chain = "N/A"
print(f"Total audit events: {total}")
for r in rows:
    print(f"  {r[0]}: {r[1]}")
print(f"Latest event: {last_ts}")
print(f"Chain entries: {chain}")
conn.close()