#!/usr/bin/env python3
import sys, os, sqlite3, json
PROJECT = "/mnt/c/Users/12035/father_daddy_capital"
os.chdir(PROJECT)
sys.path.insert(0, PROJECT)

from monitoring import DB_PATH, ALERTS_PATH

conn = sqlite3.connect(str(DB_PATH))
cur = conn.cursor()

rows = cur.execute('SELECT * FROM audit_trail ORDER BY rowid DESC LIMIT 3').fetchall()
cols = [d[0] for d in cur.description]
print('Last audit entries:')
for r in rows:
    print(dict(zip(cols, r)))

conn.close()

print()
print(f'Alerts file: {ALERTS_PATH}')
if ALERTS_PATH.exists():
    print(ALERTS_PATH.read_text()[:2000])
else:
    print('(no alert file)')