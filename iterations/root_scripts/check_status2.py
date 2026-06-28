#!/usr/bin/env python3
import json

with open('output/alerts.json') as f:
    alerts = json.load(f)
print(json.dumps(alerts, indent=2)[:2000])

import sqlite3
conn = sqlite3.connect('output/fdc_audit.db')
cursor = conn.cursor()
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cursor.fetchall()
print(f'\nAudit DB tables: {tables}')
for t in tables:
    cursor.execute(f'SELECT COUNT(*) FROM {t[0]}')
    count = cursor.fetchone()[0]
    print(f'  {t[0]}: {count} rows')
    if count > 0:
        cursor.execute(f'SELECT * FROM {t[0]} ORDER BY rowid DESC LIMIT 3')
        rows = cursor.fetchall()
        desc = [d[0] for d in cursor.description]
        print(f'  Columns: {desc}')
        for r in rows:
            print(f'  {r}')
conn.close()