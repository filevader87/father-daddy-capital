#!/usr/bin/env python3
import sqlite3
con = sqlite3.connect('output/fdc_audit.db')
cur = con.cursor()
cur.execute('SELECT count(*) FROM audit_trail')
print('Total audit rows:', cur.fetchone()[0])
cur.execute('SELECT event_type, ts_utc FROM audit_trail ORDER BY id DESC LIMIT 5')
for r in cur.fetchall():
    print(r)
cur.execute("SELECT verification_hash FROM audit_trail ORDER BY id DESC LIMIT 1")
h = cur.fetchone()
print('Latest hash:', h[0][:32] if h else 'none')
con.close()