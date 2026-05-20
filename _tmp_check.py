#!/usr/bin/env python3
import sqlite3
conn = sqlite3.connect('output/fdc_audit.db')
c = conn.cursor()
# Count different event types
c.execute('SELECT event_type, count(*) FROM audit_trail GROUP BY event_type')
for r in c.fetchall():
    print(f'{r[0]}: {r[1]}')
# Get latest trade/settle events
c.execute("SELECT id, event_type, ts_utc, order_side, order_bet, settle_pnl, settle_won, bankroll_after, total_pnl FROM audit_trail WHERE event_type != 'scan' ORDER BY id DESC LIMIT 10")
rows = c.fetchall()
print('\nLatest non-scan events:')
for r in rows:
    print(r)
conn.close()