import sqlite3
conn = sqlite3.connect('cows.db')
print(conn.execute('SELECT * FROM cows').fetchall())