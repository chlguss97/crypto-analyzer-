"""
학습 백테스트 - Scalp/Swing 듀얼 모델 반복 학습
1. 백테스트 돌리면서 거래 결과 수집
2. AdaptiveML이 가중치/임계값 자동 조정
3. 조정된 모델로 다시 백테스트
4. 반복하여 최적화
"""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
import logging

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger("TrainBacktest")
logger.setLevel(logging.INFO)

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

# Slow Path 평균 context
SLOW_CTX = {
    "funding_rate": 0.0001, "funding_next_min": 240,
    "funding_history": [{"funding_rate": 0.0001}] * 3,
    "oi_current": 12e9,
    "oi_history": [{"open_interest": 11.9e9 + i * 5e7} for i in range(24)],
    "ls_ratio_account": 1.1, "ls_ratio_position": 1.0,
    "ls_history": [{"long_short_ratio_account": 1.1}] * 5,
    "cvd_15m": 30.0, "cvd_1h": 100.0,
}

REALISTIC_MAX = 12.0


async def run_swing_backtest(df_15m, df_1h, ml: AdaptiveML, iteration: int):
    """Swing 백테스트 1회 (ML 학습 포함)"""
    agg = SignalAggregator()
    engines_fast = [EMAIndicator(), RSIIndicator(), BollingerIndicator(),
                    VWAPIndicator(), MarketStructureIndicator(), ATRIndicator()]
    engines_slow = [OrderBlockIndicator(), FVGIndicator(), VolumePatternIndicator(),
                    FundingRateIndicator(), OpenInterestIndicator(),
                    LiquidationIndicator(), LongShortRatioIndicator(), CVDIndicator()]

    balance = 10000.0
    position = None
    trades = []

    for i in range(300, len(df_15m)):
        window = df_15m.iloc[max(0, i-300):i+1].reset_index(drop=True)
        bar = df_15m.iloc[i]
        price = bar["close"]

        # 포지션 관리
        if position:
            if position["dir"] == "long":
                pnl_pct = (price - position["entry"]) / position["entry"] * 100
            else:
                pnl_pct = (position["entry"] - price) / position["entry"] * 100
            hold = i - position["bar"]

            sl_hit = (position["dir"] == "long" and bar["low"] <= position["sl"]) or \
                     (position["dir"] == "short" and bar["high"] >= position["sl"])
            tp_hit = pnl_pct >= 1.5
            time_exit = hold >= 24

            if sl_hit or tp_hit or time_exit:
                exit_p = position["sl"] if sl_hit else price
                reason = "sl" if sl_hit else "tp" if tp_hit else "time"
                actual = (exit_p - position["entry"]) / position["entry"] * 100 if position["dir"] == "long" \
                    else (position["entry"] - exit_p) / position["entry"] * 100
                pnl_usdt = position["size"] * actual / 100 - position["size"] * 0.001
                balance += pnl_usdt

                # ML 학습
                ml.record_trade(position["signals"], {"atr_pct": position.get("atr_pct", 0.3)}, actual)

                trades.append({"pnl": pnl_usdt, "pnl_pct": actual, "reason": reason, "dir": position["dir"]})
                position = None
            continue

        # 시그널 분석
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
        raw_score = result["score"]
        direction = result["direction"]
        all_signals = {**fast, **slow}

        # ML 조정 점수
        adjusted_score = ml.get_adjusted_score(raw_score, all_signals)

        if adjusted_score >= ml.entry_threshold and direction != "neutral":
            atr_pct = fast.get("atr", {}).get("atr_pct", 0.3)
            sl_dist = price * atr_pct / 100 * 1.2
            sl_dist = max(price * 0.003, min(price * 0.015, sl_dist))
            sl = price - sl_dist if direction == "long" else price + sl_dist
            size = balance * 0.005 / (sl_dist / price) * 0.5
            position = {
                "dir": direction, "entry": price, "sl": sl,
                "bar": i, "size": size, "signals": all_signals, "atr_pct": atr_pct,
            }

    return balance, trades


async def run_scalp_backtest(df_1m, df_5m, df_15m, ml: AdaptiveML, iteration: int):
    """Scalp 백테스트 1회 (ML 학습 포함)"""
    scalp = ScalpEngine()
    balance = 10000.0
    position = None
    trades = []

    for i in range(100, len(df_5m)):
        window_5m = df_5m.iloc[max(0, i-100):i+1].reset_index(drop=True)
        bar = df_5m.iloc[i]
        price = bar["close"]

        # 1m 윈도우 (5m 봉 1개 = 1m 5개)
        ts = bar["timestamp"]
        mask_1m = (df_1m["timestamp"] >= ts - 300_000 * 100) & (df_1m["timestamp"] <= ts)
        window_1m = df_1m[mask_1m].tail(100).reset_index(drop=True)

        # 15m 윈도우
        window_15m = None
        if df_15m is not None:
            ts_idx = df_15m["timestamp"].searchsorted(ts)
            window_15m = df_15m.iloc[max(0, ts_idx-50):ts_idx+1].reset_index(drop=True)

        # 포지션 관리
        if position:
            if position["dir"] == "long":
                pnl_pct = (price - position["entry"]) / position["entry"] * 100
            else:
                pnl_pct = (position["entry"] - price) / position["entry"] * 100
            hold = i - position["bar"]

            sl_hit = (position["dir"] == "long" and bar["low"] <= position["sl"]) or \
                     (position["dir"] == "short" and bar["high"] >= position["sl"])
            tp_hit = pnl_pct >= 0.5
            time_exit = hold >= 6  # 30분 (5m × 6)

            if sl_hit or tp_hit or time_exit:
                exit_p = position["sl"] if sl_hit else price
                reason = "sl" if sl_hit else "tp" if tp_hit else "time"
                actual = (exit_p - position["entry"]) / position["entry"] * 100 if position["dir"] == "long" \
                    else (position["entry"] - exit_p) / position["entry"] * 100
                pnl_usdt = position["size"] * actual / 100 - position["size"] * 0.001
                balance += pnl_usdt

                ml.record_trade(position["signals"], {"atr_pct": position.get("atr_pct", 0.2)}, actual)
                trades.append({"pnl": pnl_usdt, "pnl_pct": actual, "reason": reason, "dir": position["dir"]})
                position = None
            continue

        # 시그널
        if len(window_1m) < 30 or len(window_5m) < 30:
            continue

        result = await scalp.analyze(window_1m, window_5m, window_15m)
        raw_score = result["score"]
        direction = result["direction"]

        adjusted_score = ml.get_adjusted_score(raw_score, result["signals"])

        if adjusted_score >= ml.entry_threshold and direction != "neutral":
            sl_dist = result["sl_distance"]
            sl = price - sl_dist if direction == "long" else price + sl_dist
            size = balance * 0.005 / (sl_dist / price) * 0.5
            position = {
                "dir": direction, "entry": price, "sl": sl,
                "bar": i, "size": size, "signals": result["signals"],
                "atr_pct": result["atr_pct"],
            }

    return balance, trades


def print_result(mode, iteration, balance, trades):
    if not trades:
        print(f"  [{mode} R{iteration}] No trades")
        return

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in trades)
    wr = len(wins) / len(trades) * 100
    pf = abs(sum(t["pnl"] for t in wins)) / abs(sum(t["pnl"] for t in losses)) \
        if losses and sum(t["pnl"] for t in losses) != 0 else 999

    print(f"  [{mode} R{iteration}] ${balance:,.0f} ({(balance/10000-1)*100:+.1f}%) | "
          f"{len(trades)} trades | WR {wr:.0f}% | PF {pf:.2f} | "
          f"W${np.mean([t['pnl'] for t in wins]):+.1f} L${np.mean([t['pnl'] for t in losses]):+.1f}"
          if wins and losses else
          f"  [{mode} R{iteration}] ${balance:,.0f} | {len(trades)} trades | WR {wr:.0f}%")


async def main():
    # 데이터 로드
    db = Database()
    await db.connect()
    candles_15m = await db.get_candles("BTC/USDT:USDT", "15m", limit=50000)
    candles_1h = await db.get_candles("BTC/USDT:USDT", "1h", limit=10000)
    await db.close()

    df_15m = BaseIndicator.to_dataframe(candles_15m)
    df_1h = BaseIndicator.to_dataframe(candles_1h)

    # 5m, 1m은 15m에서 근사 생성 (실제로는 OKX에서 수집해야 함)
    df_5m = resample_down(df_15m, 3)   # 15m → 5m (근사)
    df_1m = resample_down(df_15m, 15)  # 15m → 1m (근사)

    print(f"Data: 15m={len(df_15m)} | 1h={len(df_1h)} | 5m~={len(df_5m)} | 1m~={len(df_1m)}")
    print()

    # 반복 학습
    ROUNDS = 5
    ml_swing = AdaptiveML(mode="swing")
    ml_scalp = AdaptiveML(mode="scalp")

    print("=" * 60)
    print("  TRAINING BACKTEST - Swing Model")
    print("=" * 60)
    for r in range(1, ROUNDS + 1):
        balance, trades = await run_swing_backtest(df_15m, df_1h, ml_swing, r)
        print_result("SWING", r, balance, trades)
        stats = ml_swing.get_stats()
        print(f"         threshold={stats['entry_threshold']:.1f} | WR={stats['recent_win_rate']:.0f}% | trades_learned={stats['trade_count']}")

    print()
    print("=" * 60)
    print("  TRAINING BACKTEST - Scalp Model")
    print("=" * 60)
    for r in range(1, ROUNDS + 1):
        balance, trades = await run_scalp_backtest(df_1m, df_5m, df_15m, ml_scalp, r)
        print_result("SCALP", r, balance, trades)
        stats = ml_scalp.get_stats()
        print(f"         threshold={stats['entry_threshold']:.1f} | WR={stats['recent_win_rate']:.0f}% | trades_learned={stats['trade_count']}")

    # 최종 모델 저장
    ml_swing.save()
    ml_scalp.save()

    print()
    print("=" * 60)
    print("  FINAL WEIGHTS")
    print("=" * 60)
    print(f"\n  Swing weights:")
    for k, v in sorted(ml_swing.weights.items(), key=lambda x: -x[1]):
        print(f"    {k:<20} {v:.2f}")
    print(f"  Swing threshold: {ml_swing.entry_threshold:.2f}")

    print(f"\n  Scalp weights:")
    for k, v in sorted(ml_scalp.weights.items(), key=lambda x: -x[1]):
        print(f"    {k:<20} {v:.2f}")
    print(f"  Scalp threshold: {ml_scalp.entry_threshold:.2f}")

    print()
    print("Models saved to data/adaptive_swing.pkl, data/adaptive_scalp.pkl")


def resample_down(df_15m: pd.DataFrame, factor: int) -> pd.DataFrame:
    """15m 캔들을 보간하여 더 짧은 TF 근사 생성"""
    rows = []
    for _, bar in df_15m.iterrows():
        ts = bar["timestamp"]
        interval = 900_000 // factor  # ms
        o, h, l, c, v = bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]

        # 선형 보간으로 sub-candle 생성
        prices = np.linspace(o, c, factor + 1)
        for j in range(factor):
            sub_o = prices[j]
            sub_c = prices[j + 1]
            noise = (h - l) * 0.1 * np.random.randn()
            sub_h = max(sub_o, sub_c) + abs(noise) * 0.5
            sub_l = min(sub_o, sub_c) - abs(noise) * 0.5
            sub_h = min(sub_h, h)
            sub_l = max(sub_l, l)
            rows.append({
                "timestamp": int(ts + j * interval),
                "open": sub_o, "high": sub_h, "low": sub_l,
                "close": sub_c, "volume": v / factor,
            })

    return pd.DataFrame(rows)


if __name__ == "__main__":
    asyncio.run(main())
