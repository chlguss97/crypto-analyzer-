import sqlite3
c = sqlite3.connect("/app/data/candles.db")
print("=== Tables ===")
for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
    print(f"  {r[0]}")

print("\n=== Counts ===")
for t in ["candles", "trades", "signals", "daily_summary"]:
    try:
        n = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t}: {n}")
    except Exception as e:
        print(f"  {t}: NOT FOUND ({e})")
