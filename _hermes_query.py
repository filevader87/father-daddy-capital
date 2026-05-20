#!/usr/bin/env python3
import sqlite3
conn = sqlite3.connect('output/fdc_audit.db')
c = conn.cursor()
c.execute('SELECT COUNT(*) FROM audit_trail')
print('Total rows:', c.fetchone()[0])
c.execute('SELECT event_type, COUNT(*) FROM audit_trail GROUP BY event_type')
print('By type:', dict(c.fetchall()))
c.execute('SELECT MAX(ts_utc) FROM audit_trail')
print('Latest event:', c.fetchone()[0])
conn.close()