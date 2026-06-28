#!/usr/bin/env python3
"""Check audit trail and contracts DBs."""
import sqlite3

# Check audit trail
try:
    conn = sqlite3.connect('audit_trail.db')
    c = conn.cursor()
    c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = c.fetchall()
    print('Audit DB tables:', tables)
    for t in tables:
        tname = t[0]
        c.execute(f'SELECT COUNT(*) FROM {tname}')
        cnt = c.fetchone()[0]
        print(f'  {tname}: {cnt} rows')
        c.execute(f'SELECT * FROM {tname} ORDER BY rowid DESC LIMIT 3')
        rows = c.fetchall()
        for r in rows:
            print(f'    {r}')
    conn.close()
except Exception as e:
    print(f'Audit DB error: {e}')

# Check contracts
try:
    conn = sqlite3.connect('contracts.db')
    c = conn.cursor()
    c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = c.fetchall()
    print('\nContracts DB tables:', tables)
    for t in tables:
        tname = t[0]
        c.execute(f'SELECT COUNT(*) FROM {tname}')
        cnt = c.fetchone()[0]
        print(f'  {tname}: {cnt} rows')
        c.execute(f'SELECT * FROM {tname} ORDER BY rowid DESC LIMIT 3')
        rows = c.fetchall()
        for r in rows:
            print(f'    {r}')
    conn.close()
except Exception as e:
    print(f'Contracts DB error: {e}')