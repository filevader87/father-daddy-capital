#!/usr/bin/env python3
"""Quick chain integrity check + summary."""
import sys
sys.path.insert(0, "/mnt/c/Users/12035/father_daddy_capital")

from monitoring import AuditTrail
audit = AuditTrail()
integrity = audit.verify_integrity()
print(f"Total rows: {integrity['total_rows']}")
print(f"Chain intact: {'YES' if integrity['chain_intact'] else 'NO - BROKEN'}")
if integrity.get('breaks'):
    for b in integrity['breaks']:
        print(f"  Break at row {b['row_id']}")