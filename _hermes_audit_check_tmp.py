#!/usr/bin/env python3
import sqlite3
from pathlib import Path

db = Path("/mnt/c/Users/12035/father_daddy_capital/output/fdc_audit.db")
if db.exists():
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) as cnt FROM audit_trail')
    total = cur.fetchone()['cnt']
    cur.execute('SELECT event_type, ts_utc, signal_direction, contract_question FROM audit_trail ORDER BY id DESC LIMIT 5')
    rows = cur.fetchall()
    print(f'Audit DB: {total} total events')
    for r in rows:
        q = (r["contract_question"] or "")[:40]
        print(f'  {r["event_type"]:12s} | {r["ts_utc"][:19]} | dir={r["signal_direction"]} | q={q}')
    conn.close()
else:
    print('Audit DB not found')

# Check alerts and scan output
import json
alerts = Path("/mnt/c/Users/12035/father_daddy_capital/output/alerts.json")
if alerts.exists():
    a = json.loads(alerts.read_text())
    print(f'\nAlerts: {a["active_alerts"]} active, {a["total_fired"]} fired, {a["consecutive_losses"]} consec losses')
    print(f'  Trading paused: {a["trading_paused"]}')

# Check scan output
import glob
scans = sorted(glob.glob(str(Path("/mnt/c/Users/12035/father_daddy_capital/output/scan_*.json"))))
if scans:
    latest = scans[-1]
    s = json.loads(Path(latest).read_text())
    print(f'\nLatest scan: {Path(latest).name}')
    print(f'  Entries: {s.get("entries",0)}, Settled: {s.get("settled",0)}')