import sqlite3

c = sqlite3.connect("/app/data/candles.db")

q = "SELECT timestamp,volume FROM candles WHERE timeframe='5m' ORDER BY timestamp DESC LIMIT 20"
print("=== 5m candle volumes (recent 20) ===")
for r in c.execute(q).fetchall():
    print(f"  ts={r[0]} vol={r[1]:.1f}")

q2 = "SELECT timestamp,open,high,low,close,volume FROM candles WHERE timeframe='5m' ORDER BY timestamp DESC LIMIT 5"
print("\n=== 5m candle detail (recent 5) ===")
rows = c.execute(q2).fetchall()
trs = []
for i in range(1, len(rows)):
    h = rows[i][1]
    l = rows[i][2]
    pc = rows[i-1][3]
    trs.append(max(h - l, abs(h - pc), abs(l - pc)))
atr = sum(trs) / len(trs) if trs else 0
print(f"  ATR(4)={atr:.1f}  threshold={atr*0.4:.1f}")
for r in rows[:5]:
    body = abs(r[4] - r[1])
    rng = r[2] - r[3]
    ratio = body / rng if rng > 0 else 0
    print(f"  body={body:.1f} rng={rng:.1f} ratio={ratio:.2f} vol={r[5]:.1f}")
