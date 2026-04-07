"""
HistoricalLearner — 과거 캔들 데이터로 시그널 재현 → 대량 ML 학습
1) DB에 쌓인 캔들로 과거 시점 시그널 계산
2) 이후 실제 가격 변동으로 가상 매매 결과 확인
3) 결과를 ML에 대량 피드백
4) 하루 1회 또는 봇 시작 시 실행
"""
import asyncio
import logging
import time
import numpy as np
import pandas as pd
from datetime import datetime, timezone

from src.engine.base import BaseIndicator
from src.engine.fast.ema import EMAIndicator
from src.engine.fast.rsi import RSIIndicator
from src.engine.fast.bollinger import BollingerIndicator
from src.engine.fast.vwap import VWAPIndicator
from src.engine.fast.market_structure import MarketStructureIndicator
from src.engine.fast.atr import ATRIndicator
from src.engine.fast.fractal import FractalIndicator
from src.engine.slow.order_block import OrderBlockIndicator
from src.engine.slow.fvg import FVGIndicator
from src.engine.slow.volume_pattern import VolumePatternIndicator
from src.signal_engine.aggregator import SignalAggregator
from src.engine.regime_detector import MarketRegimeDetector

logger = logging.getLogger(__name__)

# 캔들을 대상으로 할 수 있는 엔진만 사용 (API 의존 엔진 제외)
FAST_ENGINES = [
    EMAIndicator, RSIIndicator, BollingerIndicator,
    VWAPIndicator, MarketStructureIndicator, ATRIndicator,
    FractalIndicator,
]
SLOW_ENGINES = [
    OrderBlockIndicator, FVGIndicator, VolumePatternIndicator,
]


class HistoricalLearner:
    """과거 데이터 기반 대량 학습 엔진 v2"""

    FEE_RATE = 0.0005
    DEFAULT_LEVERAGE = 15
    MAX_HOLD_BARS = 24

    # SL 배수 다양화 (같은 시그널을 여러 파라미터로 시뮬)
    SL_MULTIPLIERS = [0.8, 1.0, 1.2, 1.5]

    def __init__(self, db, ml_swing, ml_scalp, candle_collector=None):
        self.db = db
        self.ml_swing = ml_swing
        self.ml_scalp = ml_scalp
        self.candle_collector = candle_collector  # 90일 캔들 수집용
        self.aggregator = SignalAggregator()
        self.regime_detector = MarketRegimeDetector()
        self._stats = {"total": 0, "wins": 0, "losses": 0, "skipped": 0}

    async def run_backfill(self, timeframe: str = "15m", lookback: int = 500,
                           step: int = 5, symbol: str = "BTC/USDT:USDT"):
        """
        과거 캔들로 시그널 재현 + 가상매매 결과 → ML 학습

        Args:
            timeframe: 캔들 TF
            lookback: 최근 N개 캔들 사용
            step: N봉마다 시그널 계산 (매 봉 하면 너무 느림)
            symbol: 심볼
        """
        start_time = time.time()
        logger.info(f"[HIST] 역사 백필 학습 시작: {timeframe} / {lookback}봉 / step={step}")

        # 전체 캔들 로드
        candles = await self.db.get_candles(symbol, timeframe, limit=lookback)
        if not candles or len(candles) < 200:
            logger.warning(f"[HIST] 캔들 부족: {len(candles) if candles else 0}개")
            return self._stats

        df_full = BaseIndicator.to_dataframe(candles)
        total_bars = len(df_full)

        # 엔진 인스턴스 생성
        fast_engines = [cls() for cls in FAST_ENGINES]
        slow_engines = [cls() for cls in SLOW_ENGINES]

        trades_learned = 0

        # 슬라이딩 윈도우로 과거 시점 재현
        window_size = 150  # 최소 150봉 필요 (지표 계산용)

        for i in range(window_size, total_bars - self.MAX_HOLD_BARS, step):
            try:
                # 현재 시점까지의 캔들
                df_slice = df_full.iloc[:i + 1].copy().reset_index(drop=True)
                entry_price = float(df_slice["close"].iloc[-1])
                entry_hour = datetime.fromtimestamp(
                    df_slice["timestamp"].iloc[-1] / 1000, tz=timezone.utc
                ).hour

                # 미래 캔들 (결과 확인용)
                future = df_full.iloc[i + 1:i + 1 + self.MAX_HOLD_BARS]
                if len(future) < 4:
                    continue

                # 시그널 계산
                signals = await self._calc_signals(df_slice, fast_engines, slow_engines)
                if not signals:
                    self._stats["skipped"] += 1
                    continue

                # 합산
                aggregated = self.aggregator.aggregate(
                    {k: v for k, v in signals.items() if k in
                     ["ema", "rsi", "bollinger", "vwap", "market_structure", "atr", "fractal"]},
                    {k: v for k, v in signals.items() if k in
                     ["order_block", "fvg", "volume"]},
                )

                direction = aggregated["direction"]
                score = aggregated["score"]

                if direction == "neutral":
                    self._stats["skipped"] += 1
                    continue

                # 레짐 감지
                regime_result = self.regime_detector.detect(df_slice)

                base_meta = {"atr_pct": signals.get("atr", {}).get("atr_pct", 0.3),
                             "hour": entry_hour,
                             "regime": regime_result["regime"]}

                # 파라미터 다양화: 여러 SL 배수로 시뮬 → 각각 학습
                for sl_mult in self.SL_MULTIPLIERS:
                    pnl_pct = self._simulate_trade(
                        direction, entry_price, future, score, sl_mult=sl_mult
                    )

                    if pnl_pct is None:
                        continue

                    meta = {**base_meta, "sl_mult": sl_mult}
                    self.ml_swing.record_trade(signals, meta, pnl_pct)
                    if timeframe in ("5m", "1m"):
                        self.ml_scalp.record_trade(signals, meta, pnl_pct)
                    trades_learned += 1
                    self._stats["total"] += 1
                    if pnl_pct > 0:
                        self._stats["wins"] += 1
                    else:
                        self._stats["losses"] += 1

            except Exception as e:
                logger.debug(f"[HIST] bar {i} 에러: {e}")
                continue

            # CPU 양보 (매 10건마다 — 대시보드 응답 보장)
            if trades_learned % 10 == 0 and trades_learned > 0:
                await asyncio.sleep(0.05)

        elapsed = time.time() - start_time
        win_rate = self._stats["wins"] / max(self._stats["total"], 1) * 100

        logger.info(
            f"[HIST] 역사 백필 완료: {trades_learned}건 학습 | "
            f"승률 {win_rate:.1f}% | {elapsed:.1f}초 | "
            f"ML Swing 버퍼: {len(self.ml_swing.X_buffer)} | "
            f"ML Scalp 버퍼: {len(self.ml_scalp.X_buffer)}"
        )

        return self._stats

    async def _calc_signals(self, df: pd.DataFrame,
                            fast_engines: list, slow_engines: list) -> dict:
        """시그널 계산 (캔들 기반 엔진만)"""
        signals = {}
        context = {}

        for engine in fast_engines:
            try:
                result = await engine.calculate(df, context)
                signals[result["type"]] = result
                if result["type"] == "bollinger":
                    context["bb_position"] = result.get("bb_position")
            except Exception:
                pass

        slow_ctx = {
            "funding_rate": 0, "funding_next_min": 999,
            "oi_current": 0, "oi_history": [],
            "ls_ratio_account": 1.0, "ls_history": [],
            "cvd_15m": 0, "cvd_1h": 0, "funding_history": [],
        }

        for engine in slow_engines:
            try:
                result = await engine.calculate(df, slow_ctx)
                signals[result["type"]] = result
                if result["type"] == "order_block" and result.get("ob_zone"):
                    slow_ctx["ob_zones"] = [result["ob_zone"]]
            except Exception:
                pass

        return signals

    def _simulate_trade(self, direction: str, entry_price: float,
                        future: pd.DataFrame, score: float,
                        sl_mult: float = 1.2) -> float | None:
        """미래 캔들로 가상매매 시뮬레이션 (SL 배수 지정 가능)"""
        atr_data = future["high"].values - future["low"].values
        avg_atr = np.mean(atr_data[:4]) if len(atr_data) >= 4 else entry_price * 0.003

        sl_dist = avg_atr * sl_mult
        tp1_dist = sl_dist * 1.5
        tp2_dist = sl_dist * 2.5

        if direction == "long":
            sl = entry_price - sl_dist
            tp1 = entry_price + tp1_dist
            tp2 = entry_price + tp2_dist
        else:
            sl = entry_price + sl_dist
            tp1 = entry_price - tp1_dist
            tp2 = entry_price - tp2_dist

        leverage = self.DEFAULT_LEVERAGE
        exit_price = None

        for bar_idx in range(len(future)):
            high = float(future["high"].iloc[bar_idx])
            low = float(future["low"].iloc[bar_idx])

            if direction == "long":
                if low <= sl:
                    exit_price = sl; break
                if high >= tp2:
                    exit_price = tp2; break
                if high >= tp1:
                    sl = entry_price
            else:
                if high >= sl:
                    exit_price = sl; break
                if low <= tp2:
                    exit_price = tp2; break
                if low <= tp1:
                    sl = entry_price

        if exit_price is None:
            exit_price = float(future["close"].iloc[-1])

        if direction == "long":
            raw_pnl = (exit_price - entry_price) / entry_price * 100 * leverage
        else:
            raw_pnl = (entry_price - exit_price) / entry_price * 100 * leverage

        fee_pct = self.FEE_RATE * 2 * leverage * 100
        return raw_pnl - fee_pct

    async def run_scalp_backfill(self, lookback: int = 1000, step: int = 5,
                                symbol: str = "BTC/USDT:USDT"):
        """
        Scalp 전용 백필: 5m+1m 캔들로 ScalpEngine 시그널 → ml_scalp 학습

        Args:
            lookback: 5m 캔들 수
            step: N봉마다 시그널 계산
        """
        from src.strategy.scalp_engine import ScalpEngine

        start_time = time.time()
        logger.info(f"[HIST-SCALP] 스캘핑 백필 학습 시작: 5m {lookback}봉 / step={step}")

        candles_5m = await self.db.get_candles(symbol, "5m", limit=lookback)
        candles_1m = await self.db.get_candles(symbol, "1m", limit=lookback * 5)

        if not candles_5m or len(candles_5m) < 100:
            logger.warning(f"[HIST-SCALP] 5m 캔들 부족: {len(candles_5m) if candles_5m else 0}")
            return {"total": 0, "wins": 0, "losses": 0}

        if not candles_1m or len(candles_1m) < 100:
            logger.warning(f"[HIST-SCALP] 1m 캔들 부족: {len(candles_1m) if candles_1m else 0}")
            return {"total": 0, "wins": 0, "losses": 0}

        from src.engine.base import BaseIndicator
        df_5m = BaseIndicator.to_dataframe(candles_5m)
        df_1m = BaseIndicator.to_dataframe(candles_1m)

        scalp_engine = ScalpEngine()
        scalp_stats = {"total": 0, "wins": 0, "losses": 0}

        window = 60  # 최소 60봉
        max_hold = 6  # 5m × 6 = 30분

        for i in range(window, len(df_5m) - max_hold, step):
            try:
                df_5m_slice = df_5m.iloc[:i + 1].copy().reset_index(drop=True)
                entry_price = float(df_5m_slice["close"].iloc[-1])
                ts_5m = float(df_5m_slice["timestamp"].iloc[-1])
                entry_hour = datetime.fromtimestamp(ts_5m / 1000, tz=timezone.utc).hour

                # 대응하는 1m 캔들 슬라이스 (5m 시점 기준 최근 100봉)
                mask = df_1m["timestamp"] <= ts_5m
                df_1m_slice = df_1m[mask].tail(100).copy().reset_index(drop=True)

                if len(df_1m_slice) < 20:
                    continue

                # ScalpEngine 분석
                result = await scalp_engine.analyze(df_1m_slice, df_5m_slice, None)
                direction = result.get("direction", "neutral")
                score = result.get("score", 0)

                if direction == "neutral" or score < 1.0:
                    continue

                # 미래 5m 봉으로 결과 시뮬
                future = df_5m.iloc[i + 1:i + 1 + max_hold]
                if len(future) < 2:
                    continue

                sl_dist = result.get("sl_distance", entry_price * 0.002)
                tp_dist = result.get("tp_distance", sl_dist * 2)
                leverage = 25
                is_explosive = result.get("explosive_mode", False)

                if direction == "long":
                    sl = entry_price - sl_dist
                    tp = entry_price + tp_dist
                else:
                    sl = entry_price + sl_dist
                    tp = entry_price - tp_dist

                exit_price = None
                for bar_idx in range(len(future)):
                    h = float(future["high"].iloc[bar_idx])
                    l = float(future["low"].iloc[bar_idx])

                    if direction == "long":
                        if l <= sl:
                            exit_price = sl
                            break
                        if h >= tp:
                            exit_price = tp
                            break
                    else:
                        if h >= sl:
                            exit_price = sl
                            break
                        if l <= tp:
                            exit_price = tp
                            break

                if exit_price is None:
                    exit_price = float(future["close"].iloc[-1])

                if direction == "long":
                    raw_pnl = (exit_price - entry_price) / entry_price * 100 * leverage
                else:
                    raw_pnl = (entry_price - exit_price) / entry_price * 100 * leverage

                fee_pct = self.FEE_RATE * 2 * leverage * 100
                net_pnl = raw_pnl - fee_pct

                # 레짐 감지
                regime_result = self.regime_detector.detect(df_5m_slice)

                # ML Scalp 학습
                meta = {"atr_pct": result.get("atr_pct", 0.2),
                        "hour": entry_hour,
                        "regime": regime_result["regime"]}
                self.ml_scalp.record_trade(result.get("signals", {}), meta, net_pnl)

                scalp_stats["total"] += 1
                if net_pnl > 0:
                    scalp_stats["wins"] += 1
                else:
                    scalp_stats["losses"] += 1

            except Exception as e:
                logger.debug(f"[HIST-SCALP] bar {i} 에러: {e}")
                continue

            if scalp_stats["total"] % 10 == 0 and scalp_stats["total"] > 0:
                await asyncio.sleep(0.05)

        elapsed = time.time() - start_time
        wr = scalp_stats["wins"] / max(scalp_stats["total"], 1) * 100
        logger.info(
            f"[HIST-SCALP] 스캘핑 백필 완료: {scalp_stats['total']}건 | "
            f"승률 {wr:.1f}% | {elapsed:.1f}초 | Scalp 버퍼: {len(self.ml_scalp.X_buffer)}"
        )
        return scalp_stats

    async def _fetch_long_history(self):
        """OKX에서 90일치 캔들 수집 (학습 전 데이터 보강)"""
        if not self.candle_collector:
            return

        logger.info("[HIST] 90일 캔들 수집 시작...")
        try:
            for tf in ["15m", "1h", "5m"]:
                await self.candle_collector.backfill(tf, days=90)
                await asyncio.sleep(1)
            # 1m은 양이 많으므로 30일만
            await self.candle_collector.backfill("1m", days=30)
            logger.info("[HIST] 90일 캔들 수집 완료")
        except Exception as e:
            logger.error(f"[HIST] 캔들 수집 실패: {e}")

    async def _find_regime_candles(self, target_regime: str, timeframe: str = "15m",
                                   symbol: str = "BTC/USDT:USDT") -> list[int]:
        """특정 레짐에 해당하는 캔들 구간 인덱스 찾기"""
        candles = await self.db.get_candles(symbol, timeframe, limit=5000)
        if not candles or len(candles) < 200:
            return []

        df = BaseIndicator.to_dataframe(candles)
        detector = MarketRegimeDetector()
        indices = []

        for i in range(150, len(df), 10):
            df_slice = df.iloc[:i + 1]
            result = detector.detect(df_slice)
            if result["regime"] == target_regime:
                indices.append(i)

        return indices

    async def _regime_focused_learn(self, symbol: str = "BTC/USDT:USDT"):
        """부족한 레짐 집중 학습 — 데이터 적은 레짐의 과거 구간을 찾아서 학습"""
        from src.engine.regime_detector import REGIMES

        logger.info("[HIST] 레짐 집중 학습 시작...")

        for regime in REGIMES:
            # Swing 버퍼에서 레짐별 데이터 수 확인
            buf_size = len(self.ml_swing.regime_buffers.get(regime, {}).get("X", []))
            if buf_size >= 100:
                continue  # 충분하면 스킵

            logger.info(f"[HIST] {regime} 부족 ({buf_size}건) → 집중 학습")

            # 해당 레짐 구간 찾기
            indices = await self._find_regime_candles(regime)
            if not indices:
                logger.info(f"[HIST] {regime} 구간 없음 → 스킵")
                continue

            # 해당 구간에서 백필
            candles = await self.db.get_candles(symbol, "15m", limit=5000)
            if not candles:
                continue

            df_full = BaseIndicator.to_dataframe(candles)
            fast_engines = [cls() for cls in FAST_ENGINES]
            slow_engines = [cls() for cls in SLOW_ENGINES]
            learned = 0

            for idx in indices[:100]:  # 최대 100개 구간
                if idx + self.MAX_HOLD_BARS >= len(df_full):
                    continue

                try:
                    df_slice = df_full.iloc[:idx + 1].copy().reset_index(drop=True)
                    entry_price = float(df_slice["close"].iloc[-1])
                    entry_hour = datetime.fromtimestamp(
                        df_slice["timestamp"].iloc[-1] / 1000, tz=timezone.utc
                    ).hour

                    future = df_full.iloc[idx + 1:idx + 1 + self.MAX_HOLD_BARS]
                    if len(future) < 4:
                        continue

                    signals = await self._calc_signals(df_slice, fast_engines, slow_engines)
                    if not signals:
                        continue

                    aggregated = self.aggregator.aggregate(
                        {k: v for k, v in signals.items() if k in
                         ["ema", "rsi", "bollinger", "vwap", "market_structure", "atr", "fractal"]},
                        {k: v for k, v in signals.items() if k in
                         ["order_block", "fvg", "volume"]},
                    )

                    direction = aggregated["direction"]
                    if direction == "neutral":
                        continue

                    meta = {"atr_pct": signals.get("atr", {}).get("atr_pct", 0.3),
                            "hour": entry_hour, "regime": regime}

                    for sl_mult in self.SL_MULTIPLIERS:
                        pnl = self._simulate_trade(direction, entry_price, future,
                                                   aggregated["score"], sl_mult)
                        if pnl is not None:
                            self.ml_swing.record_trade(signals, meta, pnl)
                            learned += 1

                except Exception:
                    continue

                if learned % 50 == 0 and learned > 0:
                    await asyncio.sleep(0.1)

            logger.info(f"[HIST] {regime} 집중 학습: {learned}건")

    async def _explosive_focused_learn(self, symbol: str = "BTC/USDT:USDT"):
        """급변동 구간 집중 학습 — ATR 급등 구간을 찾아서 Scalp ML 강화"""
        from src.strategy.scalp_engine import ScalpEngine

        logger.info("[HIST] 급변동 구간 집중 학습 시작...")

        candles_5m = await self.db.get_candles(symbol, "5m", limit=5000)
        candles_1m = await self.db.get_candles(symbol, "1m", limit=10000)

        if not candles_5m or len(candles_5m) < 200 or not candles_1m or len(candles_1m) < 200:
            logger.info("[HIST] 급변동 학습: 캔들 부족")
            return

        df_5m = BaseIndicator.to_dataframe(candles_5m)
        df_1m = BaseIndicator.to_dataframe(candles_1m)

        # ATR 급등 구간 찾기 (5m 기준)
        high = df_5m["high"].values
        low = df_5m["low"].values
        close = df_5m["close"].values
        tr = np.maximum(high[1:] - low[1:],
                        np.maximum(np.abs(high[1:] - close[:-1]),
                                   np.abs(low[1:] - close[:-1])))

        explosive_indices = []
        for i in range(20, len(tr)):
            recent = np.mean(tr[i-3:i])
            avg = np.mean(tr[i-20:i])
            if avg > 0 and recent / avg >= 2.0:
                explosive_indices.append(i + 1)  # df index (tr은 1부터)

        if not explosive_indices:
            logger.info("[HIST] 급변동 구간 없음")
            return

        logger.info(f"[HIST] 급변동 구간 {len(explosive_indices)}개 발견")

        scalp_engine = ScalpEngine()
        learned = 0
        max_hold = 6

        for idx in explosive_indices:
            if idx + max_hold >= len(df_5m) or idx < 60:
                continue

            try:
                df_5m_slice = df_5m.iloc[:idx + 1].copy().reset_index(drop=True)
                entry_price = float(df_5m_slice["close"].iloc[-1])
                ts = float(df_5m_slice["timestamp"].iloc[-1])
                entry_hour = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).hour

                mask = df_1m["timestamp"] <= ts
                df_1m_slice = df_1m[mask].tail(100).copy().reset_index(drop=True)
                if len(df_1m_slice) < 25:
                    continue

                result = await scalp_engine.analyze(df_1m_slice, df_5m_slice, None)
                direction = result.get("direction", "neutral")
                if direction == "neutral":
                    continue

                future = df_5m.iloc[idx + 1:idx + 1 + max_hold]
                if len(future) < 2:
                    continue

                sl_dist = result.get("sl_distance", entry_price * 0.002)
                tp_dist = result.get("tp_distance", sl_dist * 2)

                exit_price = None
                for bar_idx in range(len(future)):
                    h = float(future["high"].iloc[bar_idx])
                    l = float(future["low"].iloc[bar_idx])
                    if direction == "long":
                        if l <= entry_price - sl_dist:
                            exit_price = entry_price - sl_dist; break
                        if h >= entry_price + tp_dist:
                            exit_price = entry_price + tp_dist; break
                    else:
                        if h >= entry_price + sl_dist:
                            exit_price = entry_price + sl_dist; break
                        if l <= entry_price - tp_dist:
                            exit_price = entry_price - tp_dist; break

                if exit_price is None:
                    exit_price = float(future["close"].iloc[-1])

                if direction == "long":
                    raw_pnl = (exit_price - entry_price) / entry_price * 100 * 25
                else:
                    raw_pnl = (entry_price - exit_price) / entry_price * 100 * 25
                net_pnl = raw_pnl - self.FEE_RATE * 2 * 25 * 100

                regime = self.regime_detector.detect(df_5m_slice)
                meta = {"atr_pct": result.get("atr_pct", 0.3), "hour": entry_hour,
                        "regime": regime["regime"]}
                self.ml_scalp.record_trade(result.get("signals", {}), meta, net_pnl)
                learned += 1

            except Exception:
                continue

            if learned % 10 == 0 and learned > 0:
                await asyncio.sleep(0.05)

        logger.info(f"[HIST] 급변동 집중 학습 완료: {learned}건 (Scalp)")

    async def run_daily_study(self, symbol: str = "BTC/USDT:USDT"):
        """
        일일 학습 v2 — 90일 캔들 수집 + 파라미터 다양화 + 레짐 집중 + 성능 자동 조정
        """
        logger.info("[HIST] ═══ 일일 학습 v2 시작 ═══")
        total_learned = 0

        # 0) OKX에서 90일 캔들 수집
        await self._fetch_long_history()

        # 1) 15m Swing 학습 (파라미터 다양화 포함 → 기존 대비 4배)
        self._stats = {"total": 0, "wins": 0, "losses": 0, "skipped": 0}
        stats1 = await self.run_backfill("15m", lookback=5000, step=5)
        total_learned += stats1["total"]

        # 2) 1h 큰 추세 학습
        self._stats = {"total": 0, "wins": 0, "losses": 0, "skipped": 0}
        stats2 = await self.run_backfill("1h", lookback=1000, step=3)
        total_learned += stats2["total"]

        # 3) Scalp 학습
        stats3 = await self.run_scalp_backfill(lookback=5000, step=5)
        total_learned += stats3["total"]

        # 4) 부족한 레짐 집중 학습
        await self._regime_focused_learn()

        # 5) 급변동 구간 집중 학습 (Scalp 강화)
        await self._explosive_focused_learn()

        # 6) 성능 자동 조정 — OOS가 낮으면 step 줄여서 추가 학습
        swing_oos = self.ml_swing.oos_accuracy
        scalp_oos = self.ml_scalp.oos_accuracy

        if swing_oos < 0.6:
            logger.info(f"[HIST] Swing OOS 낮음 ({swing_oos:.3f}) → 추가 학습")
            self._stats = {"total": 0, "wins": 0, "losses": 0, "skipped": 0}
            extra = await self.run_backfill("15m", lookback=5000, step=3)
            total_learned += extra["total"]

        if scalp_oos < 0.6:
            logger.info(f"[HIST] Scalp OOS 낮음 ({scalp_oos:.3f}) → 추가 학습")
            extra = await self.run_scalp_backfill(lookback=5000, step=3)
            total_learned += extra["total"]

        # 저장
        self.ml_swing.save()
        self.ml_scalp.save()

        logger.info(
            f"[HIST] ═══ 일일 학습 완료: 총 {total_learned}건 ═══ | "
            f"Swing 버퍼: {len(self.ml_swing.X_buffer)} OOS:{swing_oos:.3f} | "
            f"Scalp 버퍼: {len(self.ml_scalp.X_buffer)} OOS:{scalp_oos:.3f}"
        )
        return total_learned

    async def run_session_study(self, symbol: str = "BTC/USDT:USDT"):
        """
        세션별 경량 학습 (하루 3회 중 2회 — 가볍게)
        최근 데이터만 빠르게 학습
        """
        logger.info("[HIST] 세션 경량 학습 시작...")
        total = 0

        self._stats = {"total": 0, "wins": 0, "losses": 0, "skipped": 0}
        s1 = await self.run_backfill("15m", lookback=500, step=5)
        total += s1["total"]

        s2 = await self.run_scalp_backfill(lookback=500, step=5)
        total += s2["total"]

        self.ml_swing.save()
        self.ml_scalp.save()

        logger.info(f"[HIST] 세션 학습 완료: {total}건")
        return total
