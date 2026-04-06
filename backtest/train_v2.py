"""
학습 백테스트 v2 - 공격적 매매 + 과적합 방지 + 실데이터
- Swing: 월 50% 목표, 레버리지 20~30x, 공격적 진입
- Scalp: 일 5% 목표, 레버리지 25~30x, 고빈도 단타
- 과적합 방지: 최대 일일 거래 수 제한, 임계값 하한
"""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
import logging
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


async def run_swing(df_15m, df_1h, ml: AdaptiveML, rnd: int):
    """Swing - 공격적 설정"""
    agg = SignalAggregator()
    engines_fast = [EMAIndicator(), RSIIndicator(), BollingerIndicator(),
                    VWAPIndicator(), MarketStructureIndicator(), ATRIndicator()]
    engines_slow = [OrderBlockIndicator(), FVGIndicator(), VolumePatternIndicator(),
                    FundingRateIndicator(), OpenInterestIndicator(),
                    LiquidationIndicator(), LongShortRatioIndicator(), CVDIndicator()]

    balance = 10000.0
    peak = 10000.0
    position = None
    trades = []
    daily_trades = 0
    current_day = 0
    streak = 0

    for i in range(300, len(df_15m)):
        bar = df_15m.iloc[i]
        price = bar["close"]
        day = bar["timestamp"] // 86_400_000

        if day != current_day:
            current_day = day
            daily_trades = 0

        # 포지션 관리
        if position:
            if position["dir"] == "long":
                pnl_pct = (price - position["entry"]) / position["entry"] * 100
            else:
                pnl_pct = (position["entry"] - price) / position["entry"] * 100
            hold = i - position["bar"]

            sl_hit = (position["dir"] == "long" and bar["low"] <= position["sl"]) or \
                     (position["dir"] == "short" and bar["high"] >= position["sl"])

            # 공격적 트레일링
            if pnl_pct >= 0.5 and position.get("tier", 0) < 1:
                # 본전 확보
                position["sl"] = position["entry"] * (1.001 if position["dir"] == "long" else 0.999)
                position["tier"] = 1
            if pnl_pct >= 1.5 and position.get("tier", 0) < 2:
                # TP1: 50% 청산 시뮬레이션 (나머지 트레일링)
                partial = position["size"] * 0.5
                partial_pnl = partial * pnl_pct / 100 - partial * 0.001
                balance += partial_pnl
                position["size"] -= partial
                position["tier"] = 2
                if position["dir"] == "long":
                    position["sl"] = position["entry"] * 1.005
                else:
                    position["sl"] = position["entry"] * 0.995

            tp_hit = pnl_pct >= 3.0  # TP2
            time_exit = hold >= 24

            if sl_hit or tp_hit or time_exit:
                exit_p = position["sl"] if sl_hit else price
                reason = "sl" if sl_hit else "tp2" if tp_hit else "time"
                actual = (exit_p - position["entry"]) / position["entry"] * 100 if position["dir"] == "long" \
                    else (position["entry"] - exit_p) / position["entry"] * 100
                pnl_usdt = position["size"] * actual / 100 - position["size"] * 0.001
                balance += pnl_usdt

                total_pnl = pnl_usdt + position.get("partial_realized", 0)
                ml.record_trade(position["signals"], {"atr_pct": position.get("atr_pct", 0.3), "streak": streak}, actual)

                if actual > 0:
                    streak = max(0, streak) + 1
                else:
                    streak = min(0, streak) - 1

                trades.append({"pnl": total_pnl, "pnl_pct": actual, "reason": reason, "dir": position["dir"]})
                if balance > peak:
                    peak = balance
                position = None
            continue

        # 일일 거래 제한
        if daily_trades >= 8:
            continue

        # 드로다운 체크
        if peak > 0 and (peak - balance) / peak > 0.10:
            continue

        # 시그널 분석
        window = df_15m.iloc[max(0, i-300):i+1].reset_index(drop=True)
        ctx = {"htf_trend": "unknown"}
        if df_1h is not None:
            ts_idx = df_1h["timestamp"].searchsorted(bar["timestamp"])
            htf_w = df_1h.iloc[max(0, ts_idx-50):ts_idx+1].reset_index(drop=True)
            if len(htf_w) >= 20:
                ms1h = await MarketStructureIndicator().calculate(htf_w)
                ctx["htf_trend"] = ms1h.get("trend", "unknown")

        fast, slow = {}, {}
        for e in engines_fast:
            try:
                r = await e.calculate(window, ctx)
                fast[r["type"]] = r
                if r["type"] == "bollinger": ctx["bb_position"] = r["bb_position"]
            except: pass
        s_ctx = dict(SLOW_CTX)
        for e in engines_slow:
            try:
                r = await e.calculate(window, s_ctx)
                slow[r["type"]] = r
                if r["type"] == "order_block" and r.get("ob_zone"): s_ctx["ob_zones"] = [r["ob_zone"]]
                if r["type"] == "open_interest": s_ctx["oi_spike"] = r.get("oi_spike", False)
            except: pass

        result = agg.aggregate(fast, slow)
        all_signals = {**fast, **slow}
        adjusted = ml.get_adjusted_score(result["score"], all_signals, {"streak": streak})
        direction = result["direction"]

        if adjusted >= ml.entry_threshold and direction != "neutral":
            atr_pct = fast.get("atr", {}).get("atr_pct", 0.3)
            sl_dist = price * atr_pct / 100 * 1.0  # 타이트한 SL (1.0x ATR)
            sl_dist = max(price * 0.002, min(price * 0.01, sl_dist))
            sl = price - sl_dist if direction == "long" else price + sl_dist

            # 공격적 사이즈: 계좌의 1% 리스크
            leverage = 25
            size = balance * 0.01 / (sl_dist / price)

            position = {
                "dir": direction, "entry": price, "sl": sl,
                "bar": i, "size": size, "signals": all_signals,
                "atr_pct": atr_pct, "tier": 0, "partial_realized": 0,
            }
            daily_trades += 1

    return balance, trades


async def run_scalp(df_1m, df_5m, df_15m, ml: AdaptiveML, rnd: int):
    """Scalp - 공격적 고빈도"""
    scalp = ScalpEngine()
    balance = 10000.0
    peak = 10000.0
    position = None
    trades = []
    daily_trades = 0
    current_day = 0
    streak = 0

    for i in range(100, len(df_5m)):
        bar = df_5m.iloc[i]
        price = bar["close"]
        ts = bar["timestamp"]
        day = ts // 86_400_000

        if day != current_day:
            current_day = day
            daily_trades = 0

        # 포지션 관리
        if position:
            if position["dir"] == "long":
                pnl_pct = (price - position["entry"]) / position["entry"] * 100
            else:
                pnl_pct = (position["entry"] - price) / position["entry"] * 100
            hold = i - position["bar"]

            sl_hit = (position["dir"] == "long" and bar["low"] <= position["sl"]) or \
                     (position["dir"] == "short" and bar["high"] >= position["sl"])

            # 빠른 트레일링
            if pnl_pct >= 0.2 and position.get("tier", 0) < 1:
                position["sl"] = position["entry"] * (1.0005 if position["dir"] == "long" else 0.9995)
                position["tier"] = 1

            tp_hit = pnl_pct >= 0.6  # 단타 TP
            time_exit = hold >= 6    # 30분

            if sl_hit or tp_hit or time_exit:
                exit_p = position["sl"] if sl_hit else price
                reason = "sl" if sl_hit else "tp" if tp_hit else "time"
                actual = (exit_p - position["entry"]) / position["entry"] * 100 if position["dir"] == "long" \
                    else (position["entry"] - exit_p) / position["entry"] * 100
                pnl_usdt = position["size"] * actual / 100 - position["size"] * 0.0005
                balance += pnl_usdt

                ml.record_trade(position["signals"], {"atr_pct": position.get("atr_pct", 0.2), "streak": streak}, actual)
                if actual > 0:
                    streak = max(0, streak) + 1
                else:
                    streak = min(0, streak) - 1

                trades.append({"pnl": pnl_usdt, "pnl_pct": actual, "reason": reason, "dir": position["dir"]})
                if balance > peak: peak = balance
                position = None
            continue

        # 일일 거래 제한 (과적합 방지)
        if daily_trades >= 15:
            continue
        # 드로다운 체크
        if peak > 0 and (peak - balance) / peak > 0.08:
            continue
        # 연패 쿨다운
        if streak <= -3:
            streak += 1  # 서서히 복구
            continue

        # 1m 윈도우
        mask_1m = (df_1m["timestamp"] >= ts - 6_000_000) & (df_1m["timestamp"] <= ts)
        window_1m = df_1m[mask_1m].tail(100).reset_index(drop=True)
        window_5m = df_5m.iloc[max(0, i-100):i+1].reset_index(drop=True)

        window_15m = None
        if df_15m is not None:
            ts_idx = df_15m["timestamp"].searchsorted(ts)
            window_15m = df_15m.iloc[max(0, ts_idx-50):ts_idx+1].reset_index(drop=True)

        if len(window_1m) < 20 or len(window_5m) < 20:
            continue

        result = await scalp.analyze(window_1m, window_5m, window_15m)
        adjusted = ml.get_adjusted_score(result["score"], result["signals"], {"streak": streak})
        direction = result["direction"]

        # 진입 임계값 (최소 4.0)
        threshold = max(4.0, ml.entry_threshold)

        if adjusted >= threshold and direction != "neutral":
            sl_dist = result["sl_distance"]
            sl_dist = max(price * 0.001, min(price * 0.005, sl_dist))
            sl = price - sl_dist if direction == "long" else price + sl_dist

            # 공격적: 계좌 1% 리스크, 30x
            size = balance * 0.01 / (sl_dist / price)

            position = {
                "dir": direction, "entry": price, "sl": sl,
                "bar": i, "size": size, "signals": result["signals"],
                "atr_pct": result["atr_pct"], "tier": 0,
            }
            daily_trades += 1

    return balance, trades


def show(mode, rnd, bal, trades, ml):
    if not trades:
        print(f"  [{mode} R{rnd}] No trades")
        return
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in trades)
    wr = len(wins) / len(trades) * 100
    pf = abs(sum(t["pnl"] for t in wins)) / max(0.01, abs(sum(t["pnl"] for t in losses)))
    max_dd = 0
    pk = rb = 10000
    for t in trades:
        rb += t["pnl"]
        if rb > pk: pk = rb
        dd = (pk - rb) / pk * 100
        if dd > max_dd: max_dd = dd

    s = ml.get_stats()
    exit_tp = len([t for t in trades if t["reason"] in ("tp","tp2")])
    exit_sl = len([t for t in trades if t["reason"] == "sl"])
    exit_time = len([t for t in trades if t["reason"] == "time"])

    print(f"  [{mode} R{rnd}] ${bal:,.0f} ({(bal/10000-1)*100:+.1f}%) | "
          f"{len(trades)} trades | WR {wr:.0f}% | PF {pf:.2f} | DD {max_dd:.1f}%")
    print(f"         TP:{exit_tp} SL:{exit_sl} TIME:{exit_time} | "
          f"avgW ${np.mean([t['pnl'] for t in wins]):+.0f} avgL ${np.mean([t['pnl'] for t in losses]):+.0f} | "
          f"thr={s['entry_threshold']:.1f}" if wins and losses else
          f"         thr={s['entry_threshold']:.1f}")


async def main():
    db = Database()
    await db.connect()
    df_15m = BaseIndicator.to_dataframe(await db.get_candles("BTC/USDT:USDT", "15m", limit=50000))
    df_1h = BaseIndicator.to_dataframe(await db.get_candles("BTC/USDT:USDT", "1h", limit=10000))
    df_5m = BaseIndicator.to_dataframe(await db.get_candles("BTC/USDT:USDT", "5m", limit=50000))
    df_1m = BaseIndicator.to_dataframe(await db.get_candles("BTC/USDT:USDT", "1m", limit=50000))
    await db.close()

    print(f"Data: 1m={len(df_1m)} | 5m={len(df_5m)} | 15m={len(df_15m)} | 1h={len(df_1h)}")
    print()

    ROUNDS = 8
    ml_swing = AdaptiveML(mode="swing")
    ml_scalp = AdaptiveML(mode="scalp")

    # Swing 초기 설정: 공격적
    ml_swing.entry_threshold = 4.5

    # Scalp 초기 설정
    ml_scalp.entry_threshold = 4.0
    ml_scalp.min_trades_to_train = 20

    print("=" * 65)
    print("  SWING MODEL (target: 50%/month)")
    print("=" * 65)
    for r in range(1, ROUNDS + 1):
        bal, trades = await run_swing(df_15m, df_1h, ml_swing, r)
        show("SWING", r, bal, trades, ml_swing)

    print()
    print("=" * 65)
    print("  SCALP MODEL (target: 5%/day)")
    print("=" * 65)
    for r in range(1, ROUNDS + 1):
        bal, trades = await run_scalp(df_1m, df_5m, df_15m, ml_scalp, r)
        show("SCALP", r, bal, trades, ml_scalp)

    ml_swing.save()
    ml_scalp.save()

    print()
    print("=" * 65)
    print("  FINAL OPTIMIZED WEIGHTS")
    print("=" * 65)
    print(f"\n  Swing (threshold: {ml_swing.entry_threshold:.2f}):")
    for k, v in sorted(ml_swing.weights.items(), key=lambda x: -x[1])[:8]:
        print(f"    {k:<20} {v:.2f}")
    print(f"\n  Scalp (threshold: {ml_scalp.entry_threshold:.2f}):")
    for k, v in sorted(ml_scalp.weights.items(), key=lambda x: -x[1]):
        print(f"    {k:<20} {v:.2f}")
    print(f"\n  Models saved!")


if __name__ == "__main__":
    asyncio.run(main())
