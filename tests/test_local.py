"""
OKX 연결 없이 가짜 데이터로 전체 파이프라인 테스트
"""
import asyncio
import sys
import numpy as np
import pandas as pd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.engine.fast.ema import EMAIndicator
from src.engine.fast.rsi import RSIIndicator
from src.engine.fast.bollinger import BollingerIndicator
from src.engine.fast.vwap import VWAPIndicator
from src.engine.fast.market_structure import MarketStructureIndicator
from src.engine.fast.atr import ATRIndicator
from src.engine.slow.order_block import OrderBlockIndicator
from src.engine.slow.fvg import FVGIndicator
from src.engine.slow.volume_pattern import VolumePatternIndicator
from src.engine.slow.funding_rate import FundingRateIndicator
from src.engine.slow.open_interest import OpenInterestIndicator
from src.engine.slow.liquidation import LiquidationIndicator
from src.engine.slow.long_short_ratio import LongShortRatioIndicator
from src.engine.slow.cvd import CVDIndicator
from src.signal_engine.aggregator import SignalAggregator
from src.signal_engine.grader import SignalGrader
from src.trading.leverage import LeverageCalculator


def generate_fake_candles(bars: int = 500, start_price: float = 65000) -> pd.DataFrame:
    """가짜 BTC 15m 캔들 생성 (랜덤 워크 + 추세)"""
    np.random.seed(42)

    timestamps = []
    opens, highs, lows, closes, volumes = [], [], [], [], []

    price = start_price
    base_ts = 1712000000000  # 임의 시작

    # 상승 추세 → 횡보 → 하락 패턴
    for i in range(bars):
        ts = base_ts + i * 900_000  # 15분 간격

        # 추세 바이어스
        if i < 150:
            bias = 0.02   # 상승
        elif i < 300:
            bias = 0.0    # 횡보
        else:
            bias = -0.015  # 하락

        # 변동성
        volatility = price * 0.003
        change = np.random.normal(bias, 1) * volatility

        o = price
        c = price + change
        h = max(o, c) + abs(np.random.normal(0, 0.3)) * volatility
        l = min(o, c) - abs(np.random.normal(0, 0.3)) * volatility
        v = abs(np.random.normal(100, 30)) + (20 if abs(change) > volatility * 1.5 else 0)

        timestamps.append(ts)
        opens.append(o)
        highs.append(h)
        lows.append(l)
        closes.append(c)
        volumes.append(v)

        price = c

    return pd.DataFrame({
        "timestamp": timestamps,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


async def test_all_engines():
    """전체 엔진 테스트"""
    print("=" * 60)
    print("  OKX 연결 없이 로컬 테스트")
    print("=" * 60)

    # 1. 가짜 캔들 생성
    df = generate_fake_candles(500)
    print(f"\n[1] 가짜 캔들 생성: {len(df)}봉")
    print(f"    시작가: ${df['open'].iloc[0]:,.0f}")
    print(f"    최종가: ${df['close'].iloc[-1]:,.0f}")
    print(f"    최고가: ${df['high'].max():,.0f}")
    print(f"    최저가: ${df['low'].min():,.0f}")

    # 2. Fast Path 엔진 테스트
    print(f"\n[2] Fast Path 엔진 테스트")
    print("-" * 50)

    fast_engines = [
        EMAIndicator(),
        RSIIndicator(),
        BollingerIndicator(),
        VWAPIndicator(),
        MarketStructureIndicator(),
        ATRIndicator(),
    ]

    fast_signals = {}
    context = {"htf_trend": "bullish"}  # 1H 상승 추세 가정

    for engine in fast_engines:
        try:
            result = await engine.calculate(df, context)
            fast_signals[result["type"]] = result

            direction = result.get("direction", "?")
            strength = result.get("strength", 0)

            icon = "O" if strength > 0.5 else "." if strength > 0 else "X"
            print(f"  [{icon}] {result['type']:<20} → {direction:<8} (강도: {strength:.2f})", end="")

            # 추가 정보
            if result["type"] == "ema":
                print(f"  정배열: {result['alignment']}", end="")
            elif result["type"] == "rsi":
                print(f"  RSI: {result['rsi_14']:.1f} {result['zone']}", end="")
            elif result["type"] == "bollinger":
                print(f"  패턴: {result['pattern']} BB위치: {result['bb_position']:.2f}", end="")
            elif result["type"] == "market_structure":
                print(f"  추세: {result['trend']} 이벤트: {result['last_event']}", end="")
            elif result["type"] == "atr":
                print(f"  ATR: ${result['atr_14']:.0f} ({result['atr_pct']:.2f}%) {result['volatility']}", end="")
            elif result["type"] == "vwap":
                print(f"  VWAP: ${result['session_vwap']:.0f} ({result['price_vs_vwap']})", end="")

            print()

            if result["type"] == "bollinger":
                context["bb_position"] = result["bb_position"]

        except Exception as e:
            print(f"  [!] {engine.__class__.__name__}: {e}")

    # 3. Slow Path 엔진 테스트
    print(f"\n[3] Slow Path 엔진 테스트")
    print("-" * 50)

    slow_context = {
        "funding_rate": 0.0003,
        "funding_next_min": 120,
        "oi_current": 12000000000,
        "oi_history": [{"open_interest": 11800000000}] * 24,
        "ls_ratio_account": 1.5,
        "ls_history": [{"long_short_ratio_account": 1.4}] * 5,
        "cvd_15m": 50.0,
        "cvd_1h": 200.0,
        "funding_history": [{"funding_rate": 0.0002}] * 3,
    }

    slow_engines = [
        OrderBlockIndicator(),
        FVGIndicator(),
        VolumePatternIndicator(),
        FundingRateIndicator(),
        OpenInterestIndicator(),
        LiquidationIndicator(),
        LongShortRatioIndicator(),
        CVDIndicator(),
    ]

    slow_signals = {}
    for engine in slow_engines:
        try:
            result = await engine.calculate(df, slow_context)
            slow_signals[result["type"]] = result

            direction = result.get("direction", "?")
            strength = result.get("strength", 0)
            icon = "O" if strength > 0.5 else "." if strength > 0 else "X"
            print(f"  [{icon}] {result['type']:<20} → {direction:<8} (강도: {strength:.2f})", end="")

            if result["type"] == "order_block":
                print(f"  활성OB: {result.get('active_count',0)}개", end="")
            elif result["type"] == "fvg":
                print(f"  활성FVG: {result.get('active_count',0)}개", end="")
            elif result["type"] == "funding_rate":
                print(f"  펀딩비: {result['current_rate']:.4f}%", end="")
            elif result["type"] == "volume":
                print(f"  패턴: {result['pattern']} spike: {result['spike_ratio']:.1f}x", end="")

            print()

            if result["type"] == "order_block" and result.get("ob_zone"):
                slow_context["ob_zones"] = [result["ob_zone"]]
            if result["type"] == "open_interest":
                slow_context["oi_spike"] = result.get("oi_spike", False)

        except Exception as e:
            print(f"  [!] {engine.__class__.__name__}: {e}")

    # 4. 시그널 합산
    print(f"\n[4] 시그널 합산")
    print("-" * 50)

    aggregator = SignalAggregator()
    aggregated = aggregator.aggregate(fast_signals, slow_signals)

    print(f"  방향:     {aggregated['direction'].upper()}")
    print(f"  점수:     {aggregated['score']:.1f} / 10")
    print(f"  롱 점수:  {aggregated['long_score']:.1f}")
    print(f"  숏 점수:  {aggregated['short_score']:.1f}")
    print(f"  보너스:   {aggregated['confluence_bonus']:.1f}")
    if aggregated["confluence_details"]:
        for detail in aggregated["confluence_details"]:
            print(f"    + {detail}")

    # 5. 등급 판정
    print(f"\n[5] 등급 판정")
    print("-" * 50)

    grader = SignalGrader()
    risk_state = {
        "daily_pnl_pct": 0,
        "current_drawdown_pct": 0,
        "open_positions": 0,
        "same_direction_count": 0,
        "streak": 0,
        "cooldown_active": False,
        "funding_blackout": False,
        "has_same_symbol": False,
    }
    grade_result = grader.grade(aggregated, risk_state)

    print(f"  등급:     {grade_result['grade']}")
    print(f"  매매가능: {'YES' if grade_result['tradeable'] else 'NO'}")
    if grade_result["reject_reason"]:
        print(f"  거부사유: {grade_result['reject_reason']}")
    if grade_result["tradeable"]:
        print(f"  레버리지: ~{grade_result['max_leverage']}x")
        print(f"  사이즈:   {grade_result['size_pct']*100:.0f}%")
        print(f"  실행방식: {grade_result['execution']}")

    # 6. 레버리지 계산
    if grade_result["tradeable"]:
        print(f"\n[6] 레버리지 + 포지션 사이즈")
        print("-" * 50)

        atr_pct = fast_signals.get("atr", {}).get("atr_pct", 0.3)
        lev_calc = LeverageCalculator()
        lev = lev_calc.calculate(grade_result["grade"], atr_pct, streak=0)

        balance = 10000  # 가상 잔고
        margin = lev_calc.calculate_position_size(
            balance, lev["leverage"], lev["sl_pct"], grade_result["size_pct"]
        )

        print(f"  최종 레버리지: {lev['leverage']}x")
        print(f"    등급 상한: {lev['grade_limit']}x")
        print(f"    ATR 제한:  {lev['atr_limit']}x")
        print(f"  SL 거리:     {lev['sl_pct']:.2f}%")
        print(f"  잔고:        ${balance:,}")
        print(f"  마진:        ${margin:,.0f}")
        print(f"  포지션 크기: ${margin * lev['leverage']:,.0f}")
        print(f"  1회 리스크:  ${balance * 0.005:,.1f} (0.5%)")

    print(f"\n{'=' * 60}")
    print(f"  테스트 완료!")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(test_all_engines())
