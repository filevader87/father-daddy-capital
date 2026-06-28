#!/usr/bin/env python3
import sqlite3
conn = sqlite3.connect('output/fdc_audit.db')
cur = conn.cursor()
tables = cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print('Tables:', tables)
for t in tables:
    tn = t[0]
    count = cur.execute(f'SELECT COUNT(*) FROM {tn}').fetchone()[0]
    print(f'  {tn}: {count} rows')
    if count > 0:
        cols = [d[0] for d in cur.execute(f'SELECT * FROM {tn} LIMIT 0').description]
        print(f'    Columns: {cols}')
        rows = cur.execute(f'SELECT * FROM {tn} ORDER BY rowid DESC LIMIT 5').fetchall()
        for r in rows:
            print(f'    {r}')
conn.close()