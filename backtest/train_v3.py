"""학습 백테스트 v3 - 고빈도 + 공격적"""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import pandas as pd, numpy as np, logging
logging.basicConfig(level=logging.WARNING, format="%(message)s")

from src.data.storage import Database
from src.engine.base import BaseIndicator
from src.engine.fast.ema import EMAIndicator
from src.engine.fast.rsi import RSIIndicator
from src.engine.fast.bollinger import BollingerIndicator
from src.engine.fast.market_structure import MarketStructureIndicator
from src.engine.fast.atr import ATRIndicator
from src.engine.fast.vwap import VWAPIndicator
from src.engine.slow.order_block import OrderBlockIndicator
from src.engine.slow.fvg import FVGIndicator
from src.engine.slow.volume_pattern import VolumePatternIndicator
from src.engine.slow.funding_rate import FundingRateIndicator
from src.engine.slow.open_interest import OpenInterestIndicator
from src.engine.slow.liquidation import LiquidationIndicator
from src.engine.slow.long_short_ratio import LongShortRatioIndicator
from src.engine.slow.cvd import CVDIndicator
from src.signal_engine.aggregator import SignalAggregator
from src.strategy.scalp_engine import ScalpEngine
from src.strategy.adaptive_ml import AdaptiveML

SLOW_CTX = {
    "funding_rate": 0.0001, "funding_next_min": 240,
    "funding_history": [{"funding_rate": 0.0001}] * 3,
    "oi_current": 12e9,
    "oi_history": [{"open_interest": 11.9e9 + i * 5e7} for i in range(24)],
    "ls_ratio_account": 1.1, "ls_ratio_position": 1.0,
    "ls_history": [{"long_short_ratio_account": 1.1}] * 5,
    "cvd_15m": 30.0, "cvd_1h": 100.0,
}


async def run_swing(df_15m, df_1h, ml, rnd):
    agg = SignalAggregator()
    ef = [EMAIndicator(), RSIIndicator(), BollingerIndicator(), VWAPIndicator(), MarketStructureIndicator(), ATRIndicator()]
    es = [OrderBlockIndicator(), FVGIndicator(), VolumePatternIndicator(), FundingRateIndicator(),
          OpenInterestIndicator(), LiquidationIndicator(), LongShortRatioIndicator(), CVDIndicator()]

    bal = 10000.0; pk = 10000.0; pos = None; trades = []; dt = 0; cd = 0; streak = 0

    for i in range(200, len(df_15m)):
        bar = df_15m.iloc[i]; price = bar["close"]; day = bar["timestamp"] // 86_400_000
        if day != cd: cd = day; dt = 0

        if pos:
            pp = (price - pos["e"]) / pos["e"] * 100 if pos["d"] == "long" else (pos["e"] - price) / pos["e"] * 100
            hold = i - pos["b"]
            sl = (pos["d"]=="long" and bar["low"]<=pos["s"]) or (pos["d"]=="short" and bar["high"]>=pos["s"])

            # 트레일링
            if pp >= 0.4 and pos.get("t",0) < 1:
                pos["s"] = pos["e"] * (1.0005 if pos["d"]=="long" else 0.9995); pos["t"] = 1
            if pp >= 1.0 and pos.get("t",0) < 2:
                p50 = pos["sz"] * 0.5; bal += p50 * pp / 100 - p50 * 0.001; pos["sz"] -= p50; pos["t"] = 2
                pos["s"] = pos["e"] * (1.003 if pos["d"]=="long" else 0.997)

            tp = pp >= 2.5; te = hold >= 16  # 4시간
            if sl or tp or te:
                ep = pos["s"] if sl else price; r = "sl" if sl else "tp" if tp else "time"
                ap = (ep-pos["e"])/pos["e"]*100 if pos["d"]=="long" else (pos["e"]-ep)/pos["e"]*100
                pu = pos["sz"] * ap / 100 - pos["sz"] * 0.001; bal += pu
                ml.record_trade(pos["sig"], {"atr_pct": pos.get("ap",0.3), "streak": streak}, ap)
                streak = streak + 1 if ap > 0 else -1 if streak >= 0 else streak - 1
                trades.append({"pnl": pu, "pnl_pct": ap, "reason": r, "dir": pos["d"]}); pos = None
                if bal > pk: pk = bal
            continue

        if dt >= 12 or (pk > 0 and (pk-bal)/pk > 0.12): continue
        if streak <= -4: streak += 1; continue

        w = df_15m.iloc[max(0,i-200):i+1].reset_index(drop=True)
        ctx = {"htf_trend": "unknown"}
        if df_1h is not None:
            ti = df_1h["timestamp"].searchsorted(bar["timestamp"])
            hw = df_1h.iloc[max(0,ti-50):ti+1].reset_index(drop=True)
            if len(hw) >= 20:
                m1h = await MarketStructureIndicator().calculate(hw)
                ctx["htf_trend"] = m1h.get("trend","unknown")

        fast, slow = {}, {}
        for e in ef:
            try:
                r = await e.calculate(w, ctx); fast[r["type"]] = r
                if r["type"]=="bollinger": ctx["bb_position"]=r["bb_position"]
            except: pass
        sc = dict(SLOW_CTX)
        for e in es:
            try:
                r = await e.calculate(w, sc); slow[r["type"]] = r
                if r["type"]=="order_block" and r.get("ob_zone"): sc["ob_zones"]=[r["ob_zone"]]
                if r["type"]=="open_interest": sc["oi_spike"]=r.get("oi_spike",False)
            except: pass

        res = agg.aggregate(fast, slow); sigs = {**fast, **slow}
        adj = ml.get_adjusted_score(res["score"], sigs, {"streak": streak})
        d = res["direction"]

        # 낮은 임계값: 3.5
        if adj >= max(3.5, ml.entry_threshold) and d != "neutral":
            ap = fast.get("atr",{}).get("atr_pct", 0.3)
            sd = price * ap / 100 * 0.8  # 타이트 SL
            sd = max(price*0.002, min(price*0.008, sd))
            s = price - sd if d=="long" else price + sd
            sz = bal * 0.01 / (sd/price)  # 1% 리스크
            pos = {"d": d, "e": price, "s": s, "b": i, "sz": sz, "sig": sigs, "ap": ap, "t": 0}
            dt += 1

    return bal, trades


async def run_scalp(df_1m, df_5m, df_15m, ml, rnd):
    scalp = ScalpEngine()
    bal = 10000.0; pk = 10000.0; pos = None; trades = []; dt = 0; cd = 0; streak = 0

    for i in range(60, len(df_5m)):
        bar = df_5m.iloc[i]; price = bar["close"]; ts = bar["timestamp"]
        day = ts // 86_400_000
        if day != cd: cd = day; dt = 0

        if pos:
            pp = (price-pos["e"])/pos["e"]*100 if pos["d"]=="long" else (pos["e"]-price)/pos["e"]*100
            hold = i - pos["b"]
            sl = (pos["d"]=="long" and bar["low"]<=pos["s"]) or (pos["d"]=="short" and bar["high"]>=pos["s"])

            if pp >= 0.15 and pos.get("t",0) < 1:
                pos["s"] = pos["e"] * (1.0003 if pos["d"]=="long" else 0.9997); pos["t"] = 1

            tp = pp >= 0.4; te = hold >= 6
            if sl or tp or te:
                ep = pos["s"] if sl else price; r = "sl" if sl else "tp" if tp else "time"
                ap = (ep-pos["e"])/pos["e"]*100 if pos["d"]=="long" else (pos["e"]-ep)/pos["e"]*100
                pu = pos["sz"] * ap / 100 - pos["sz"] * 0.0005; bal += pu
                ml.record_trade(pos["sig"], {"atr_pct": pos.get("ap",0.2), "streak": streak}, ap)
                streak = streak + 1 if ap > 0 else -1 if streak >= 0 else streak - 1
                trades.append({"pnl": pu, "pnl_pct": ap, "reason": r, "dir": pos["d"]}); pos = None
                if bal > pk: pk = bal
            continue

        if dt >= 25 or (pk>0 and (pk-bal)/pk>0.06): continue
        if streak <= -3: streak += 1; continue

        m1m = df_1m[(df_1m["timestamp"]>=ts-6_000_000)&(df_1m["timestamp"]<=ts)].tail(100).reset_index(drop=True)
        w5m = df_5m.iloc[max(0,i-60):i+1].reset_index(drop=True)
        w15m = None
        if df_15m is not None:
            ti = df_15m["timestamp"].searchsorted(ts)
            w15m = df_15m.iloc[max(0,ti-30):ti+1].reset_index(drop=True)

        if len(m1m) < 15 or len(w5m) < 15: continue

        res = await scalp.analyze(m1m, w5m, w15m)
        adj = ml.get_adjusted_score(res["score"], res["signals"], {"streak": streak})
        d = res["direction"]

        # 낮은 임계값: 3.0
        if adj >= max(3.0, ml.entry_threshold) and d != "neutral":
            sd = res["sl_distance"]; sd = max(price*0.0008, min(price*0.003, sd))
            s = price - sd if d=="long" else price + sd
            sz = bal * 0.008 / (sd/price)  # 0.8% 리스크, 고레버
            pos = {"d": d, "e": price, "s": s, "b": i, "sz": sz, "sig": res["signals"], "ap": res["atr_pct"], "t": 0}
            dt += 1

    return bal, trades


def show(mode, rnd, bal, trades, ml):
    if not trades: print(f"  [{mode} R{rnd}] No trades"); return
    wins = [t for t in trades if t["pnl"]>0]; losses = [t for t in trades if t["pnl"]<=0]
    wr = len(wins)/len(trades)*100
    pf = abs(sum(t["pnl"] for t in wins))/max(0.01,abs(sum(t["pnl"] for t in losses)))
    days = max(1, len(set(t.get("day",0) for t in trades)) or 1)
    # 거래일수 추정
    if mode == "SWING": days = 30
    else: days = 7
    daily_avg = len(trades) / days

    md = 0; p = r = 10000
    for t in trades:
        r += t["pnl"]
        if r > p: p = r
        dd = (p-r)/p*100
        if dd > md: md = dd

    s = ml.get_stats()
    tp = len([t for t in trades if t["reason"]=="tp"]); sl = len([t for t in trades if t["reason"]=="sl"])
    tm = len([t for t in trades if t["reason"]=="time"])

    print(f"  [{mode} R{rnd}] ${bal:,.0f} ({(bal/10000-1)*100:+.1f}%) | "
          f"{len(trades)} trades ({daily_avg:.1f}/day) | WR {wr:.0f}% | PF {pf:.2f} | DD {md:.1f}%")
    if wins and losses:
        print(f"         TP:{tp} SL:{sl} TIME:{tm} | "
              f"avgW ${np.mean([t['pnl'] for t in wins]):+.0f} avgL ${np.mean([t['pnl'] for t in losses]):+.0f} | "
              f"thr={s['entry_threshold']:.1f}")


async def main():
    db = Database()
    await db.connect()
    df_15m = BaseIndicator.to_dataframe(await db.get_candles("BTC/USDT:USDT", "15m", limit=50000))
    df_1h = BaseIndicator.to_dataframe(await db.get_candles("BTC/USDT:USDT", "1h", limit=10000))
    df_5m = BaseIndicator.to_dataframe(await db.get_candles("BTC/USDT:USDT", "5m", limit=50000))
    df_1m = BaseIndicator.to_dataframe(await db.get_candles("BTC/USDT:USDT", "1m", limit=50000))
    await db.close()
    print(f"Data: 1m={len(df_1m)} | 5m={len(df_5m)} | 15m={len(df_15m)} | 1h={len(df_1h)}")

    ROUNDS = 8
    ml_sw = AdaptiveML(mode="swing"); ml_sw.entry_threshold = 3.5
    ml_sc = AdaptiveML(mode="scalp"); ml_sc.entry_threshold = 3.0; ml_sc.min_trades_to_train = 15

    print("\n" + "="*65 + "\n  SWING (target: 50%/month, 3-5 trades/day)\n" + "="*65)
    for r in range(1, ROUNDS+1):
        b, t = await run_swing(df_15m, df_1h, ml_sw, r)
        show("SWING", r, b, t, ml_sw)

    print("\n" + "="*65 + "\n  SCALP (target: 5%/day, 10-25 trades/day)\n" + "="*65)
    for r in range(1, ROUNDS+1):
        b, t = await run_scalp(df_1m, df_5m, df_15m, ml_sc, r)
        show("SCALP", r, b, t, ml_sc)

    ml_sw.save(); ml_sc.save()
    print(f"\n  Swing threshold: {ml_sw.entry_threshold:.2f} | Scalp threshold: {ml_sc.entry_threshold:.2f}")
    print(f"  Models saved!")

if __name__ == "__main__":
    asyncio.run(main())
