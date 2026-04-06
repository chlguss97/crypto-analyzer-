"""Slow Path 평균값 포함 백테스트"""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
import logging
logging.basicConfig(level=logging.WARNING)

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

# Slow Path 평균 context (BTC 무기한 선물 일반적 수치)
SLOW_CTX = {
    "funding_rate": 0.0001,
    "funding_next_min": 240,
    "funding_history": [{"funding_rate": 0.0001}] * 3,
    "oi_current": 12_000_000_000,
    "oi_history": [{"open_interest": 11_900_000_000 + i * 50_000_000} for i in range(24)],
    "ls_ratio_account": 1.1,
    "ls_ratio_position": 1.0,
    "ls_history": [{"long_short_ratio_account": 1.1}] * 5,
    "cvd_15m": 30.0,
    "cvd_1h": 100.0,
}

ENGINES_FAST = [EMAIndicator(), RSIIndicator(), BollingerIndicator(),
                VWAPIndicator(), MarketStructureIndicator(), ATRIndicator()]
ENGINES_SLOW = [OrderBlockIndicator(), FVGIndicator(), VolumePatternIndicator(),
                FundingRateIndicator(), OpenInterestIndicator(),
                LiquidationIndicator(), LongShortRatioIndicator(), CVDIndicator()]


async def main():
    # 데이터 로드
    db = Database()
    await db.connect()
    candles_15m = await db.get_candles("BTC/USDT:USDT", "15m", limit=50000)
    candles_1h = await db.get_candles("BTC/USDT:USDT", "1h", limit=10000)
    await db.close()

    df = BaseIndicator.to_dataframe(candles_15m)
    df_1h = BaseIndicator.to_dataframe(candles_1h)
    print(f"Data: {len(df)} bars (15m) | {len(df_1h)} bars (1h)")

    agg = SignalAggregator()
    balance = 10000.0
    position = None
    trades = []

    for i in range(300, len(df)):
        window = df.iloc[max(0, i - 300):i + 1].reset_index(drop=True)
        bar = df.iloc[i]
        price = bar["close"]

        # ── 포지션 관리 ──
        if position:
            if position["dir"] == "long":
                pnl_pct = (price - position["entry"]) / position["entry"] * 100
            else:
                pnl_pct = (position["entry"] - price) / position["entry"] * 100
            hold = i - position["bar"]

            sl_hit = (position["dir"] == "long" and bar["low"] <= position["sl"]) or \
                     (position["dir"] == "short" and bar["high"] >= position["sl"])
            tp_hit = pnl_pct >= 1.5
            time_exit = hold >= 24  # 6시간

            if sl_hit or tp_hit or time_exit:
                if sl_hit:
                    exit_p, reason = position["sl"], "sl"
                elif tp_hit:
                    exit_p, reason = price, "tp"
                else:
                    exit_p, reason = price, "time"

                if position["dir"] == "long":
                    actual = (exit_p - position["entry"]) / position["entry"] * 100
                else:
                    actual = (position["entry"] - exit_p) / position["entry"] * 100

                pnl_usdt = position["size"] * actual / 100 - position["size"] * 0.001
                balance += pnl_usdt
                trades.append({
                    "pnl": pnl_usdt, "pnl_pct": actual, "reason": reason,
                    "dir": position["dir"], "hold": hold * 15,
                    "score": position["score"], "grade": position["grade"],
                })
                position = None
            continue

        # ── 시그널 분석 ──
        # 1H 추세
        ctx = {"htf_trend": "unknown"}
        ts_idx = df_1h["timestamp"].searchsorted(bar["timestamp"])
        htf_w = df_1h.iloc[max(0, ts_idx - 50):ts_idx + 1].reset_index(drop=True)
        if len(htf_w) >= 20:
            ms1h = await MarketStructureIndicator().calculate(htf_w)
            ctx["htf_trend"] = ms1h.get("trend", "unknown")

        # Fast Path
        fast = {}
        for e in ENGINES_FAST:
            try:
                r = await e.calculate(window, ctx)
                fast[r["type"]] = r
                if r["type"] == "bollinger":
                    ctx["bb_position"] = r["bb_position"]
            except Exception:
                pass

        # Slow Path (평균값 context)
        slow = {}
        s_ctx = dict(SLOW_CTX)
        for e in ENGINES_SLOW:
            try:
                r = await e.calculate(window, s_ctx)
                slow[r["type"]] = r
                if r["type"] == "order_block" and r.get("ob_zone"):
                    s_ctx["ob_zones"] = [r["ob_zone"]]
                if r["type"] == "open_interest":
                    s_ctx["oi_spike"] = r.get("oi_spike", False)
            except Exception:
                pass

        result = agg.aggregate(fast, slow)
        score = result["score"]
        direction = result["direction"]

        # ── 등급 판정 ──
        if score >= 9.0:
            grade, size_pct = "A+", 1.0
        elif score >= 8.0:
            grade, size_pct = "A", 1.0
        elif score >= 7.5:
            grade, size_pct = "B+", 0.75
        elif score >= 6.5:
            grade, size_pct = "B", 0.5
        elif score >= 6.0:
            grade, size_pct = "B-", 0.3
        else:
            continue  # D/C 등급 → 스킵

        if direction == "neutral":
            continue

        # ── 진입 ──
        atr_pct = fast.get("atr", {}).get("atr_pct", 0.3)
        sl_dist = price * atr_pct / 100 * 1.2
        sl_dist = max(price * 0.003, min(price * 0.015, sl_dist))
        sl = price - sl_dist if direction == "long" else price + sl_dist
        size = balance * 0.005 / (sl_dist / price) * size_pct

        position = {
            "dir": direction, "entry": price, "sl": sl,
            "bar": i, "size": size, "score": score, "grade": grade,
        }

    # 미청산 정리
    if position:
        price = df.iloc[-1]["close"]
        if position["dir"] == "long":
            pnl_pct = (price - position["entry"]) / position["entry"] * 100
        else:
            pnl_pct = (position["entry"] - price) / position["entry"] * 100
        pnl_usdt = position["size"] * pnl_pct / 100 - position["size"] * 0.001
        balance += pnl_usdt
        trades.append({
            "pnl": pnl_usdt, "pnl_pct": pnl_pct, "reason": "end",
            "dir": position["dir"], "hold": (len(df) - position["bar"]) * 15,
            "score": position["score"], "grade": position["grade"],
        })

    # ── 결과 출력 ──
    if not trades:
        print("No trades (score never reached 6.0)")
        return

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in trades)

    max_dd = 0
    peak = running = 10000
    for t in trades:
        running += t["pnl"]
        if running > peak:
            peak = running
        dd = (peak - running) / peak * 100
        if dd > max_dd:
            max_dd = dd

    print()
    print("======= BACKTEST (Slow Path avg) =======")
    print(f"Initial:      $10,000")
    print(f"Final:        ${balance:,.2f}")
    print(f"Return:       {(balance / 10000 - 1) * 100:+.2f}%")
    print(f"Trades:       {len(trades)}")
    print(f"Wins:         {len(wins)} ({len(wins) / len(trades) * 100:.0f}%)")
    print(f"Losses:       {len(losses)}")
    print(f"Total PnL:    ${total_pnl:+,.2f}")
    if wins:
        print(f"Avg Win:      ${np.mean([t['pnl'] for t in wins]):+.2f}")
    if losses:
        print(f"Avg Loss:     ${np.mean([t['pnl'] for t in losses]):+.2f}")
    pf = abs(sum(t["pnl"] for t in wins)) / abs(sum(t["pnl"] for t in losses)) \
        if losses and sum(t["pnl"] for t in losses) != 0 else 0
    print(f"Profit Factor: {pf:.2f}")
    print(f"Max Drawdown: {max_dd:.2f}%")
    print(f"Avg Hold:     {np.mean([t['hold'] for t in trades]):.0f}min")

    print()
    print("--- By Grade ---")
    for g in ["A+", "A", "B+", "B", "B-"]:
        gt = [t for t in trades if t.get("grade") == g]
        if gt:
            wr = len([t for t in gt if t["pnl"] > 0]) / len(gt) * 100
            print(f"  {g:<3} {len(gt):>3} trades | WR {wr:.0f}% | ${sum(t['pnl'] for t in gt):+.2f}")

    print()
    print("--- By Exit Reason ---")
    for reason in ["tp", "sl", "time", "end"]:
        rt = [t for t in trades if t["reason"] == reason]
        if rt:
            print(f"  {reason:<6} {len(rt):>3} trades | avg ${np.mean([t['pnl'] for t in rt]):+.2f}")

    print()
    print("--- By Direction ---")
    for d in ["long", "short"]:
        dt = [t for t in trades if t["dir"] == d]
        if dt:
            wr = len([t for t in dt if t["pnl"] > 0]) / len(dt) * 100
            print(f"  {d:<6} {len(dt):>3} trades | WR {wr:.0f}% | ${sum(t['pnl'] for t in dt):+.2f}")

    print("========================================")


if __name__ == "__main__":
    asyncio.run(main())
