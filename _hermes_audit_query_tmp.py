#!/usr/bin/env python3
"""Hermes cron: Query audit trail for pipeline report."""
import sqlite3, json

conn = sqlite3.connect('output/fdc_audit.db')
conn.row_factory = sqlite3.Row

total = conn.execute('SELECT COUNT(*) as cnt FROM audit_trail').fetchone()['cnt']
rows = conn.execute('SELECT event_type, ts_utc, signal_direction, debate_verdict, settle_pnl FROM audit_trail ORDER BY id DESC LIMIT 10').fetchall()
print(f'Total audit events: {total}')
print('--- Recent events ---')
for r in rows:
    ts = r["ts_utc"][:19] if r["ts_utc"] else "?"
    et = r["event_type"] or "?"
    d = r["signal_direction"] or ""
    v = r["debate_verdict"] or ""
    p = r["settle_pnl"]
    print(f'  {ts} | {et:12s} | dir={d} | verdict={v} | pnl={p}')

chained = conn.execute('SELECT COUNT(*) as cnt FROM audit_trail WHERE prev_row_hash IS NOT NULL').fetchone()['cnt']
print(f'Chained rows: {chained}')
conn.close()