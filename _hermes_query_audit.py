#!/usr/bin/env python3
"""Query audit DB for Hermes report."""
import sqlite3
from pathlib import Path

db = Path("/mnt/c/Users/12035/father_daddy_capital/output/fdc_audit.db")
conn = sqlite3.connect(str(db))
conn.row_factory = sqlite3.Row
rows = conn.execute('SELECT id, event_type, ts_utc, signal_direction, contract_question, settle_pnl FROM audit_trail ORDER BY id DESC LIMIT 10').fetchall()
for r in rows:
    print(dict(r))
total = conn.execute('SELECT COUNT(*) as c FROM audit_trail').fetchone()[0]
print(f'Total audit events: {total}')
conn.close()