#!/usr/bin/env python3
"""Check audit trail status."""
import sqlite3, sys
sys.path.insert(0, "/mnt/c/Users/12035/father_daddy_capital")

db = sqlite3.connect('/mnt/c/Users/12035/father_daddy_capital/output/fdc_audit.db')
cur = db.cursor()

cur.execute('SELECT COUNT(*) FROM audit_trail')
count = cur.fetchone()[0]
print(f'Total audit events: {count}')

cur.execute('SELECT id, event_type, ts_utc, signal_direction, contract_question FROM audit_trail ORDER BY id DESC LIMIT 5')
for row in cur.fetchall():
    q = (row[4] or '-')[:60]
    print(f'  #{row[0]} | {row[1]} | {row[2]} | dir={row[3]} | {q}')

try:
    cur.execute('SELECT COUNT(*) FROM audit_chain')
    chain_count = cur.fetchone()[0]
    print(f'Chain entries: {chain_count}')
except:
    print('No audit_chain table')

db.close()