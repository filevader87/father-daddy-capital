#!/usr/bin/env python3
"""Hermes cron: check audit DB."""
import sqlite3
from pathlib import Path

db_path = Path("/mnt/c/Users/12035/father_daddy_capital/output/fdc_audit.db")
conn = sqlite3.connect(str(db_path))
cur = conn.cursor()

# Get column names
cols = [row[1] for row in cur.execute("PRAGMA table_info(audit_trail)").fetchall()]
print(f"Columns: {cols}")

count = cur.execute("SELECT COUNT(*) FROM audit_trail").fetchone()[0]
recent = cur.execute("SELECT * FROM audit_trail ORDER BY id DESC LIMIT 3").fetchall()
print(f"Audit DB: {count} total events")
for row in recent:
    print(f"  {dict(zip(cols, row))}")
conn.close()