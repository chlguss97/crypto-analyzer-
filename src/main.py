import asyncio
import logging
import signal
import sys
from pathlib import Path

# 프로젝트 루트를 Python path에 추가
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.helpers import load_config, load_env
from src.data.storage import Database, RedisClient
from src.data.candle_collector import CandleCollector
from src.data.ws_stream import WebSocketStream
from src.data.oi_funding import OIFundingCollector
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
from src.engine.base import BaseIndicator
from src.signal.aggregator import SignalAggregator
from src.signal.grader import SignalGrader
from src.signal.ml_model import MLEngine
from src.trading.leverage import LeverageCalculator
from src.trading.risk_manager import RiskManager
from src.trading.executor import OrderExecutor
from src.trading.position_manager import PositionManager
from src.monitoring.telegram_bot import TelegramNotifier
from src.monitoring.trade_logger import TradeLogger

# ── 로깅 설정 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("CryptoAnalyzer")


class CryptoAnalyzer:
    """메인 봇 클래스"""

    def __init__(self):
        load_env()
        self.config = load_config()
        self.symbol = self.config["exchange"]["symbol"]

        # 인프라
        self.db = Database()
        self.redis = RedisClient()
        self.candle_collector = CandleCollector(self.db)
        self.ws_stream = WebSocketStream(self.redis)
        self.oi_funding = OIFundingCollector(self.db, self.redis)

        # Fast Path 엔진
        self.fast_engines: list[BaseIndicator] = [
            EMAIndicator(),
            RSIIndicator(),
            BollingerIndicator(),
            VWAPIndicator(),
            MarketStructureIndicator(),
            ATRIndicator(),
        ]

        # Slow Path 엔진
        self.slow_engines: list[BaseIndicator] = [
            OrderBlockIndicator(),
            FVGIndicator(),
            VolumePatternIndicator(),
            FundingRateIndicator(),
            OpenInterestIndicator(),
            LiquidationIndicator(),
            LongShortRatioIndicator(),
            CVDIndicator(),
        ]

        # 시그널 합산 + 등급 + ML
        self.aggregator = SignalAggregator()
        self.grader = SignalGrader()
        self.ml_engine = MLEngine()

        # 매매 엔진
        self.leverage_calc = LeverageCalculator()
        self.risk_manager = RiskManager(self.redis)
        self.executor = OrderExecutor()
        self.position_manager = PositionManager(self.executor, self.db, self.redis)

        # 모니터링
        self.telegram = TelegramNotifier()
        self.trade_logger = TradeLogger()

        # 최신 시그널 캐시
        self._last_fast = {}
        self._last_slow = {}

        self._running = False

    async def initialize(self):
        """초기화"""
        logger.info("=" * 50)
        logger.info("CryptoAnalyzer v1.0 시작")
        logger.info("=" * 50)

        await self.db.connect()
        await self.redis.connect()
        await self.candle_collector.init_exchange()
        await self.oi_funding.init_exchange()

        # ML 모델 로드 (있으면)
        self.ml_engine.load_model()

        # 텔레그램 초기화
        await self.telegram.initialize()

        # 매매 엔진 초기화
        await self.executor.initialize()

        # 잔고 조회 + 리스크 매니저 초기화
        balance = await self.executor.get_balance()
        await self.risk_manager.initialize(balance)
        logger.info(f"계좌 잔고: ${balance:.2f}")

        # 기존 포지션 동기화
        await self.position_manager.sync_positions()

        # 캔들 백필
        logger.info("캔들 데이터 백필 시작...")
        await self.candle_collector.backfill_all()
        logger.info("캔들 데이터 백필 완료")

    async def run_fast_path(self):
        """Fast Path: 15m봉 완성 시마다 실행"""
        candles_raw = await self.db.get_candles(self.symbol, "15m", limit=300)
        if not candles_raw or len(candles_raw) < 50:
            logger.warning("캔들 데이터 부족 (최소 50개 필요)")
            return {}

        df = BaseIndicator.to_dataframe(candles_raw)

        # 1H 추세 (상위 TF context)
        candles_1h = await self.db.get_candles(self.symbol, "1h", limit=100)
        htf_trend = "unknown"
        if candles_1h and len(candles_1h) >= 20:
            df_1h = BaseIndicator.to_dataframe(candles_1h)
            ms = MarketStructureIndicator()
            htf_result = await ms.calculate(df_1h)
            htf_trend = htf_result.get("trend", "unknown")

        context = {"htf_trend": htf_trend}
        results = {}

        for engine in self.fast_engines:
            try:
                result = await engine.calculate(df, context)
                results[result["type"]] = result
                # BB position을 RSI context로 전달
                if result["type"] == "bollinger":
                    context["bb_position"] = result["bb_position"]
            except Exception as e:
                logger.error(f"Fast Path 에러 [{engine.__class__.__name__}]: {e}")

        self._last_fast = results
        logger.info(f"Fast Path 완료: {len(results)}개 시그널")
        return results

    async def run_slow_path(self):
        """Slow Path: 1~5분 주기로 실행"""
        candles_raw = await self.db.get_candles(self.symbol, "15m", limit=300)
        if not candles_raw or len(candles_raw) < 50:
            return {}

        df = BaseIndicator.to_dataframe(candles_raw)

        # context 구성 (Redis에서 실시간 데이터)
        context = {}

        # OI
        oi_val = await self.redis.get(f"rt:oi:BTC-USDT-SWAP")
        context["oi_current"] = float(oi_val) if oi_val else 0
        oi_history = await self.db.get_oi_funding(self.symbol, limit=24)
        context["oi_history"] = oi_history

        # 펀딩비
        fr_val = await self.redis.get(f"rt:funding:BTC-USDT-SWAP")
        context["funding_rate"] = float(fr_val) if fr_val else 0
        fn_min = await self.redis.get(f"rt:funding_next_min:BTC-USDT-SWAP")
        context["funding_next_min"] = int(fn_min) if fn_min else 999
        context["funding_history"] = oi_history

        # 롱숏비율
        ls_val = await self.redis.get(f"rt:ls_ratio:BTC-USDT-SWAP")
        context["ls_ratio_account"] = float(ls_val) if ls_val else 1.0
        context["ls_history"] = oi_history

        # CVD
        cvd_15m = await self.redis.get("cvd:15m:BTC-USDT-SWAP")
        cvd_1h = await self.redis.get("cvd:1h:BTC-USDT-SWAP")
        context["cvd_15m"] = float(cvd_15m) if cvd_15m else 0
        context["cvd_1h"] = float(cvd_1h) if cvd_1h else 0

        results = {}
        for engine in self.slow_engines:
            try:
                result = await engine.calculate(df, context)
                results[result["type"]] = result
                # OB 결과를 FVG context로 전달
                if result["type"] == "order_block" and result.get("ob_zone"):
                    context["ob_zones"] = [result["ob_zone"]]
                if result["type"] == "open_interest":
                    context["oi_spike"] = result.get("oi_spike", False)
            except Exception as e:
                logger.error(f"Slow Path 에러 [{engine.__class__.__name__}]: {e}")

        self._last_slow = results
        logger.info(f"Slow Path 완료: {len(results)}개 시그널")
        return results

    async def evaluate_signal(self):
        """시그널 합산 → 등급 판정 (Fast + Slow 결합)"""
        if not self._last_fast:
            return None

        # ML 예측
        all_signals = {**self._last_fast, **self._last_slow}
        ml_result = self.ml_engine.predict(all_signals)

        # 합산
        aggregated = self.aggregator.aggregate(
            self._last_fast, self._last_slow, ml_result
        )

        # 리스크 상태 조회 (실제 데이터)
        open_positions = list(self.position_manager.positions.values())
        risk_state = await self.risk_manager.get_risk_state(
            [p.to_dict() for p in open_positions]
        )

        # 펀딩비 블랙아웃 체크
        fn_min = await self.redis.get("rt:funding_next_min:BTC-USDT-SWAP")
        if fn_min and int(fn_min) <= 15:
            risk_state["funding_blackout"] = True

        # 같은 심볼 포지션 체크
        risk_state["has_same_symbol"] = self.symbol in self.position_manager.positions

        # 등급 판정
        grade_result = self.grader.grade(aggregated, risk_state)

        # Redis에 최종 결과 저장
        await self.redis.set(
            f"sig:aggregated:{self.symbol}",
            {
                "aggregated": aggregated,
                "grade": grade_result,
            },
            ttl=900 * 2,
        )

        # 매매 실행
        if grade_result["tradeable"]:
            await self._execute_trade(grade_result, aggregated, risk_state)

        return grade_result

    async def _execute_trade(self, grade_result: dict, aggregated: dict, risk_state: dict):
        """등급 기반 자동매매 실행"""
        direction = grade_result["direction"]
        grade = grade_result["grade"]
        atr_signal = self._last_fast.get("atr", {})
        atr_pct = atr_signal.get("atr_pct", 0.3)

        # 레버리지 계산
        lev_result = self.leverage_calc.calculate(
            grade=grade,
            atr_pct=atr_pct,
            streak=risk_state.get("streak", 0),
        )

        # 포지션 사이즈 계산
        balance = risk_state.get("balance", 0)
        position_margin = self.leverage_calc.calculate_position_size(
            balance=balance,
            leverage=lev_result["leverage"],
            sl_pct=lev_result["sl_pct"],
            size_pct=grade_result["size_pct"],
        )

        if position_margin <= 0:
            logger.warning("포지션 사이즈 0 → 진입 스킵")
            return

        # 현재가 조회
        price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
        current_price = float(price_str) if price_str else 0
        if current_price <= 0:
            logger.warning("현재가 조회 실패 → 진입 스킵")
            return

        # BTC 수량 계산 (마진 × 레버리지 / 현재가)
        size_usdt = position_margin * lev_result["leverage"]
        size_btc = size_usdt / current_price

        # SL/TP 계산
        sl_distance = atr_signal.get("sl_distance", current_price * 0.003)
        tp1_distance = atr_signal.get("tp1_distance", sl_distance * 1.5)
        tp2_distance = atr_signal.get("tp2_distance", sl_distance * 2.5)

        if direction == "long":
            sl_price = current_price - sl_distance
            tp1_price = current_price + tp1_distance
            tp2_price = current_price + tp2_distance
            entry_price = current_price  # 시장가일 때
        else:
            sl_price = current_price + sl_distance
            tp1_price = current_price - tp1_distance
            tp2_price = current_price - tp2_distance
            entry_price = current_price

        # OB 영역 진입가 (지정가일 때)
        if grade_result["execution"] == "limit":
            ob_signal = self._last_slow.get("order_block", {})
            ob_zone = ob_signal.get("ob_zone")
            if ob_zone and ob_signal.get("direction") == direction:
                entry_price = (ob_zone[0] + ob_zone[1]) / 2  # OTE

        logger.info(
            f"★ 매매 실행: {grade} {direction.upper()} | "
            f"진입 ${entry_price:.0f} | SL ${sl_price:.0f} | "
            f"TP1 ${tp1_price:.0f} TP2 ${tp2_price:.0f} | "
            f"{lev_result['leverage']}x | 마진 ${position_margin:.0f}"
        )

        trade_request = {
            "symbol": self.symbol,
            "direction": direction,
            "grade": grade,
            "score": grade_result["score"],
            "size": round(size_btc, 6),
            "leverage": lev_result["leverage"],
            "entry_price": entry_price if grade_result["execution"] == "limit" else None,
            "sl_price": round(sl_price, 1),
            "tp1_price": round(tp1_price, 1),
            "tp2_price": round(tp2_price, 1),
            "signals_snapshot": aggregated.get("signals_detail", {}),
        }

        pos = await self.position_manager.open_position(trade_request)
        if pos:
            # 텔레그램 알림
            await self.telegram.notify_entry(
                direction, grade, grade_result["score"],
                pos.entry_price, pos.sl_price, pos.tp1_price, pos.tp2_price,
                lev_result["leverage"], position_margin,
            )
            # 로그
            self.trade_logger.log_entry(
                direction, grade, grade_result["score"],
                pos.entry_price, pos.sl_price, lev_result["leverage"],
                position_margin, aggregated.get("signals_detail", {}),
            )
        else:
            logger.warning("포지션 오픈 실패")

    async def periodic_candle_update(self):
        """주기적 캔들 갱신 (1분마다)"""
        while self._running:
            try:
                await self.candle_collector.fetch_all_latest()
            except Exception as e:
                logger.error(f"캔들 갱신 에러: {e}")
            await asyncio.sleep(60)

    async def periodic_slow_path(self):
        """Slow Path 주기적 실행"""
        interval = self.config.get("data", {}).get("slow_path_interval_sec", 180)
        while self._running:
            try:
                slow_results = await self.run_slow_path()
                if slow_results:
                    await self.redis.set(
                        f"sig:slow:{self.symbol}", slow_results, ttl=interval * 2
                    )
            except Exception as e:
                logger.error(f"Slow Path 주기 실행 에러: {e}")
            await asyncio.sleep(interval)

    async def periodic_oi_funding(self):
        """OI/펀딩비 주기적 수집 (5분마다)"""
        while self._running:
            try:
                await self.oi_funding.collect_all()
            except Exception as e:
                logger.error(f"OI/Funding 수집 에러: {e}")
            await asyncio.sleep(300)

    async def periodic_position_check(self):
        """활성 포지션 체크 (15초마다)"""
        while self._running:
            try:
                if self.position_manager.positions:
                    price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
                    if price_str:
                        current_price = float(price_str)
                        await self.position_manager.check_positions(current_price)
            except Exception as e:
                logger.error(f"포지션 체크 에러: {e}")
            await asyncio.sleep(15)

    async def periodic_fast_path(self):
        """Fast Path 주기적 실행 (15m봉 기준, 1분마다 체크)"""
        last_run_ts = 0
        while self._running:
            try:
                latest = await self.db.get_latest_candle_time(self.symbol, "15m")
                if latest and latest != last_run_ts:
                    last_run_ts = latest
                    fast_results = await self.run_fast_path()
                    if fast_results:
                        await self.redis.set(
                            f"sig:fast:{self.symbol}", fast_results, ttl=900 * 2
                        )
                        logger.info(
                            f"시그널 요약 - "
                            + ", ".join(
                                f"{k}: {v.get('direction','?')}({v.get('strength',0):.1f})"
                                for k, v in fast_results.items()
                                if v.get("strength", 0) > 0
                            )
                        )
                        # 시그널 합산 + 등급 판정 + 매매 실행
                        grade = await self.evaluate_signal()
            except Exception as e:
                logger.error(f"Fast Path 주기 실행 에러: {e}")
            await asyncio.sleep(60)

    async def run(self):
        """메인 실행 루프"""
        await self.initialize()
        self._running = True

        logger.info("봇 시작 — 데이터 수집 + 시그널 분석 + 자동매매")
        await self.redis.set("sys:bot_status", "running")
        await self.telegram.notify_bot_status("running")

        tasks = [
            asyncio.create_task(self.periodic_candle_update()),
            asyncio.create_task(self.periodic_fast_path()),
            asyncio.create_task(self.periodic_slow_path()),
            asyncio.create_task(self.periodic_oi_funding()),
            asyncio.create_task(self.periodic_position_check()),
            asyncio.create_task(self.ws_stream.start()),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("봇 종료 중...")
        finally:
            self._running = False
            self.ws_stream.stop()
            await self.redis.set("sys:bot_status", "stopped")
            await self.telegram.notify_bot_status("stopped")
            await self.cleanup()

    async def cleanup(self):
        """리소스 정리"""
        await self.candle_collector.close()
        await self.oi_funding.close()
        await self.executor.close()
        await self.redis.close()
        await self.db.close()
        logger.info("리소스 정리 완료")


def main():
    bot = CryptoAnalyzer()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown(sig, frame):
        logger.info(f"시그널 수신: {sig} → 종료")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        loop.run_until_complete(bot.run())
    except KeyboardInterrupt:
        logger.info("키보드 인터럽트 → 종료")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
