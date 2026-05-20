#!/usr/bin/env python3
import sqlite3
db = '/mnt/c/Users/12035/father_daddy_capital/output/fdc_audit.db'
conn = sqlite3.connect(db)
total = conn.execute('SELECT COUNT(*) FROM audit_trail').fetchone()[0]
print(f'Total audit events: {total}')

rows = conn.execute('SELECT id, event_type, ts_utc, signal_direction, signal_price, debate_verdict FROM audit_trail ORDER BY id DESC LIMIT 5').fetchall()
print('\nLatest events:')
for r in rows:
    ts = r[2][:19] if r[2] else ''
    print(f'  #{r[0]} | {r[1]:10s} | {ts} | dir={r[3]} | price={r[4]} | verdict={r[5]}')

row = conn.execute('SELECT COUNT(*) FROM audit_trail WHERE row_hash IS NOT NULL').fetchone()[0]
print(f'\nRows with hashes: {row}')
conn.close()