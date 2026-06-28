#!/usr/bin/env python3
"""Quick integrity check on the audit chain."""
import sqlite3, hashlib

db_path = '/mnt/c/Users/12035/father_daddy_capital/output/fdc_audit.db'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
c = conn.cursor()

# Count rows
total = c.execute("SELECT COUNT(*) FROM audit_trail").fetchone()[0]
print(f"Total audit rows: {total}")

# Latest 5
rows = c.execute("SELECT id, event_type, ts_utc, signal_direction, signal_price, row_hash, prev_row_hash FROM audit_trail ORDER BY id DESC LIMIT 5").fetchall()
for r in rows:
    print(f"  id={r[0]} type={r[1]} ts={r[2]} dir={r[3]} price={r[4]} hash={r[5][:16]}... prev={r[6][:16]}...")

# Verify chain integrity for last 20 rows
rows_chain = c.execute("SELECT id, row_hash, prev_row_hash FROM audit_trail WHERE event_type='scan' ORDER BY id DESC LIMIT 20").fetchall()
breaks = 0
for i in range(len(rows_chain)-1):
    current = rows_chain[i]
    next_older = rows_chain[i+1]
    if current['prev_row_hash'] != next_older['row_hash']:
        breaks += 1
        print(f"  CHAIN BREAK: id={current['id']} prev_hash != id={next_older['id']} hash")

print(f"\nChain check (last 20 scan rows): {breaks} breaks found")
print("✅ CHAIN INTACT" if breaks == 0 else "🛑 CHAIN BROKEN")

conn.close()