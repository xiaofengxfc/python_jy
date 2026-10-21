import sqlite3, json
con = sqlite3.connect(r"C:\Users\27815\AppData\Roaming\biTrader\bitrader.db")
cur = con.cursor()
tables = [r[0] for r in cur.execute("select name from sqlite_master where type='table'")]
print("TABLES", tables)
for name in tables:
    cols = [c[1] for c in cur.execute("pragma table_info(%s)" % name)]
    print("==", name, cols)
    for row in cur.execute("select * from %s limit 5" % name):
        print(str(row)[:600])
