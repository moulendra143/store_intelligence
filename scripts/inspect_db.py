"""Inspect the store_intelligence.db file."""
import sqlite3

conn = sqlite3.connect("data/store_intelligence.db")
cursor = conn.cursor()

# List tables
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cursor.fetchall()
print("Tables:", [t[0] for t in tables])

for t in tables:
    tname = t[0]
    print(f"\n=== {tname} ===")
    cursor.execute(f"PRAGMA table_info({tname})")
    cols = cursor.fetchall()
    print("Columns:", [(c[1], c[2]) for c in cols])
    cursor.execute(f"SELECT COUNT(*) FROM {tname}")
    print("Row count:", cursor.fetchone()[0])
    cursor.execute(f"SELECT * FROM {tname} LIMIT 3")
    rows = cursor.fetchall()
    for row in rows:
        print("  ", row)

conn.close()
