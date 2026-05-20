import sqlite3
conn = sqlite3.connect('output/fdc_audit.db')
cur = conn.cursor()
cur.execute('SELECT * FROM audit_log ORDER BY rowid DESC LIMIT 5')
for row in cur.fetchall():
    print(row)
conn.close()
