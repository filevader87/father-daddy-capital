#!/usr/bin/env python3
import sqlite3
db = "/mnt/c/Users/12035/father_daddy_capital/output/fdc_audit.db"
conn = sqlite3.connect(db)
row = conn.execute('SELECT COUNT(*) FROM audit_trail WHERE event_type="scan"').fetchone()
print(f'Scan events: {row[0]}')
row = conn.execute('SELECT COUNT(*) FROM audit_trail WHERE event_type="entry"').fetchone()
print(f'Entry events: {row[0]}')
row = conn.execute('SELECT COUNT(*) FROM audit_trail WHERE event_type="settlement"').fetchone()
print(f'Settlement events: {row[0]}')
row = conn.execute('SELECT COUNT(*) FROM audit_trail').fetchone()
print(f'Total events: {row[0]}')
conn.close()