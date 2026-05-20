#!/usr/bin/env python3
import sqlite3
conn = sqlite3.connect('/mnt/c/Users/12035/father_daddy_capital/output/fdc_audit.db')
cur = conn.cursor()
# Count by event type
types = cur.execute('SELECT event_type, COUNT(*) FROM audit_trail GROUP BY event_type').fetchall()
print("Events by type:")
for t in types:
    print(f"  {t[0]:15s} {t[1]}")

# Check chain integrity properly - only where prev_row_hash is NOT NULL
# The LEFT JOIN creates nulls; let's do it correctly
chain_ok = cur.execute('''
    SELECT COUNT(*) FROM audit_trail a
    JOIN audit_trail b ON a.id = b.id + 1
    WHERE b.prev_row_hash IS NOT NULL AND b.prev_row_hash != a.row_hash
''').fetchone()[0]
print(f'Chain integrity violations: {chain_ok}')

# Recent entries (type=entry)
entries = cur.execute('SELECT id, ts_utc, signal_direction, debate_verdict, order_bet, settle_pnl FROM audit_trail WHERE event_type="entry" ORDER BY id DESC LIMIT 10').fetchall()
print(f'\nRecent entries:')
for e in entries:
    print(f'  #{e[0]} {e[1]} dir={e[2]} verdict={e[3]} bet={e[4]} pnl={e[5]}')

# Recent settlements
settles = cur.execute('SELECT id, ts_utc, settle_pnl, settle_won FROM audit_trail WHERE event_type="settlement" ORDER BY id DESC LIMIT 10').fetchall()
print(f'\nRecent settlements:')
for s in settles:
    print(f'  #{s[0]} {s[1]} pnl={s[2]} won={s[3]}')

# Alerts
alerts = cur.execute('SELECT id, ts_utc, signal_raw_json FROM audit_trail WHERE event_type="alert" ORDER BY id DESC LIMIT 5').fetchall()
print(f'\nRecent alerts:')
for a in alerts:
    print(f'  #{a[0]} {a[1]} {a[2][:80] if a[2] else ""}')

conn.close()