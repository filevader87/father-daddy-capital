import sqlite3
conn = sqlite3.connect('output/fdc_audit.db')
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cur.fetchall()
print("Tables:", tables)
for t in tables:
    cur.execute(f'SELECT * FROM {t[0]} ORDER BY rowid DESC LIMIT 3')
    rows = cur.fetchall()
    print(f"\n--- {t[0]} ---")
    for r in rows:
        print(r)
conn.close()
