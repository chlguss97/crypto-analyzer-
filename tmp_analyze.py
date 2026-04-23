import json
real, paper = [], []
with open("data/logs/trades.jsonl") as f:
    for line in f:
        d = json.loads(line.strip())
        if d.get("type") == "exit":
            real.append(d)
        elif d.get("type") == "paper_exit":
            paper.append(d)

print(f"=== Real trades: {len(real)} ===")
tp, tf, w, l = 0, 0, 0, 0
for t in real:
    p = t["pnl_usdt"]
    fe = t.get("fee", 0)
    tp += p
    tf += fe
    if p > 0: w += 1
    elif p < 0: l += 1
    tag = "WIN" if p > 0 else "LOSS" if p < 0 else "BE"
    iso = t["ts_iso"][:16]
    dr = t["direction"]
    er = t["exit_reason"]
    print(f"  {iso} {dr:5} {er:25} PnL {p:+8.2f} fee {fe:6.2f} {tag}")

print(f"\nTotal PnL: {tp:+.2f}")
print(f"Total fee: {tf:.2f}")
print(f"Net: {tp - tf:+.2f}")
if w + l > 0:
    print(f"W/L: {w}/{l} = {w/(w+l)*100:.0f}%")

print(f"\n=== Paper: {len(paper)} ===")
pp = sum(x["pnl_usdt"] for x in paper)
pw = sum(1 for x in paper if x["pnl_usdt"] > 0)
pl = sum(1 for x in paper if x["pnl_usdt"] < 0)
print(f"PnL: {pp:+.2f} W/L: {pw}/{pl}")
