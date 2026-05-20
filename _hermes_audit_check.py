#!/usr/bin/env python3
import sqlite3
db = sqlite3.connect('/mnt/c/Users/12035/father_daddy_capital/output/fdc_audit.db')
db.row_factory = sqlite3.Row
rows = db.execute('SELECT COUNT(*) as cnt FROM audit_trail').fetchone()
print(f'Total audit events: {rows["cnt"]}')
latest = db.execute('SELECT event_type, ts_utc, signal_direction, signal_confidence FROM audit_trail ORDER BY id DESC LIMIT 3').fetchall()
for r in latest:
    print(f'  {r["event_type"]:12s} | {r["ts_utc"]} | dir={r["signal_direction"]} conf={r["signal_confidence"]}')
try:
    integrity = db.execute('SELECT COUNT(*) as cnt FROM audit_integrity').fetchone()
    print(f'Integrity rows: {integrity["cnt"]}')
except:
    print('No audit_integrity table')
db.close()