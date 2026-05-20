#!/usr/bin/env python3
"""Hermes cron: check FDC monitoring status - audit DB, alerts, metrics."""
import sys, os, sqlite3, json
sys.path.insert(0, "/mnt/c/Users/12035/father_daddy_capital")
os.chdir("/mnt/c/Users/12035/father_daddy_capital")

from monitoring import DB_PATH, ALERTS_PATH, METRICS_PATH

# Alerts
if ALERTS_PATH.exists():
    print("=== ALERTS ===")
    print(ALERTS_PATH.read_text()[:500])
else:
    print("No alerts file")

# Metrics
if METRICS_PATH.exists():
    print("\n=== METRICS ===")
    print(METRICS_PATH.read_text()[:500])
else:
    print("No metrics file")

# Audit DB stats
if DB_PATH.exists():
    print("\n=== AUDIT DB ===")
    print(f"Size: {DB_PATH.stat().st_size} bytes")
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM audit_trail")
    print(f"Total rows: {cur.fetchone()[0]}")
    cur.execute("SELECT event_type, COUNT(*) FROM audit_trail GROUP BY event_type")
    for row in cur.fetchall():
        print(f"  {row[0]}: {row[1]}")
    conn.close()