#!/usr/bin/env python3
"""Quick audit integrity check."""
import sqlite3
from pathlib import Path

DB_PATH = Path("/mnt/c/Users/12035/father_daddy_capital/output/fdc_audit.db")

def check():
    if not DB_PATH.exists():
        print("Audit DB does not exist yet")
        return

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Get columns
    cur.execute("PRAGMA table_info(audit_trail)")
    cols = [row["name"] for row in cur.fetchall()]
    print(f"Columns: {len(cols)}")

    # Total events
    cur.execute("SELECT COUNT(*) as cnt FROM audit_trail")
    total = cur.fetchone()["cnt"]
    print(f"Total audit events: {total}")

    # By type
    cur.execute("SELECT event_type, COUNT(*) as cnt FROM audit_trail GROUP BY event_type")
    for row in cur.fetchall():
        print(f"  {row['event_type']}: {row['cnt']}")

    # Latest 5
    cur.execute("SELECT id, event_type, ts_utc FROM audit_trail ORDER BY id DESC LIMIT 5")
    print("\nLatest 5 events:")
    for row in cur.fetchall():
        print(f"  #{row['id']} | {row['event_type']} | {row['ts_utc']}")

    # Integrity: use the AuditTrail class method
    try:
        from monitoring import AuditTrail
        audit = AuditTrail()
        integrity = audit.verify_integrity()
        print(f"\nChain intact: {'✅' if integrity['chain_intact'] else '🛑 BROKEN'}")
        print(f"Total rows: {integrity['total_rows']}")
        if integrity.get("breaks"):
            for b in integrity["breaks"]:
                print(f"  Break at row {b['row_id']}")
    except Exception as e:
        print(f"\nIntegrity check: {e}")

    conn.close()

if __name__ == "__main__":
    check()