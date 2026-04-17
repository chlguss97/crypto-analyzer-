import asyncio
import logging
import math
import signal
import sys
import time as _time
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.helpers import load_config, load_env
from src.data.storage import Database, RedisClient
from src.data.candle_collector import CandleCollector
from src.data.ws_stream import WebSocketStream
from src.data.binance_stream import BinanceStream
from src.data.oi_funding import OIFundingCollector
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
from src.engine.slow.funding_rate import FundingRateIndicator
from src.engine.slow.open_interest import OpenInterestIndicator
from src.engine.slow.liquidation import LiquidationIndicator
from src.engine.slow.long_short_ratio import LongShortRatioIndicator
from src.engine.slow.cvd import CVDIndicator
from src.engine.base import BaseIndicator
from src.signal_engine.aggregator import SignalAggregator
from src.signal_engine.grader import SignalGrader
from src.strategy.scalp_engine import ScalpEngine
from src.strategy.adaptive_ml import AdaptiveML
from src.strategy.unified_engine import TradeEngine
from src.trading.leverage import LeverageCalculator
from src.trading.risk_manager import RiskManager
from src.trading.executor import OrderExecutor
from src.trading.position_manager import PositionManager
from src.monitoring.telegram_bot import TelegramNotifier
from src.monitoring.trade_logger import TradeLogger
from src.strategy.paper_trader import PaperTrader
from src.strategy.historical_learner import HistoricalLearner
from src.strategy.auto_backtest import AutoBacktest
from src.strategy.meta_learner import MetaLearner
from src.strategy.signal_tracker import SignalTracker
from src.strategy.setup_tracker import SetupTracker
from src.engine.regime_detector import MarketRegimeDetector
from src.trading.news_filter import NewsFilter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("CryptoAnalyzer")


class CryptoAnalyzer:
    """메인 봇 — TradeEngine v1 (Setup ABC) + SetupTracker 자기개선"""

    def __init__(self):
        load_env()
        self.config = load_config()
        self.symbol = self.config["exchange"]["symbol"]

        # 인프라
        self.db = Database()
        self.redis = RedisClient()
        self.candle_collector = CandleCollector(self.db)
        self.ws_stream = WebSocketStream(self.redis)
        self.binance_stream = BinanceStream(self.redis)
        self.oi_funding = OIFundingCollector(self.db, self.redis)

        # Swing 엔진 (15m)
        self.fast_engines = [
            EMAIndicator(), RSIIndicator(), BollingerIndicator(),
            VWAPIndicator(), MarketStructureIndicator(), ATRIndicator(),
            FractalIndicator(),
        ]
        self.slow_engines = [
            OrderBlockIndicator(), FVGIndicator(), VolumePatternIndicator(),
            FundingRateIndicator(), OpenInterestIndicator(),
            LiquidationIndicator(), LongShortRatioIndicator(), CVDIndicator(),
        ]
        self.aggregator = SignalAggregator()
        self.grader = SignalGrader()

        # Scalp 엔진 (레거시 — 비활성)
        self.scalp_engine = ScalpEngine()

        # TradeEngine (04-15 전면 개편)
        self.trade_engine = TradeEngine(redis=self.redis)

        # AdaptiveML (레거시 유지, 통합 모델에서는 미사용 — 콜드스타트)
        self.ml_swing = AdaptiveML(mode="swing")
        self.ml_scalp = AdaptiveML(mode="scalp")

        # 통합 모델 상태
        self._unified_streak = 0
        self._unified_daily_pnl = 0.0
        self._unified_cooldown_until = 0
        self._unified_last_dir = None
        self._unified_last_trade_time = 0
        self._unified_last_exit_reason = None
        self._unified_last_entry_price = 0.0
        self._unified_same_dir_count = 0  # 04-17: 같은 방향 연속 진입 카운터

        # 매매 엔진 (executor 먼저 생성 → risk_manager 가 참조)
        self.leverage_calc = LeverageCalculator()
        self.executor = OrderExecutor()
        self.risk_manager = RiskManager(self.redis, executor=self.executor)
        self.position_manager = PositionManager(self.executor, self.db, self.redis)

        # 모니터링
        self.telegram = TelegramNotifier()
        self.trade_logger = TradeLogger()

        # 마켓 레짐 감지
        self.regime_detector = MarketRegimeDetector()
        self._current_regime = None
        self._regime_transition = None  # "to_trending" | "to_ranging" | None
        self._regime_transition_time = 0  # 전환 감지 시각

        # 뉴스 필터
        self.news_filter = NewsFilter()

        # 가상매매 엔진 (ML 학습용 + 시그널 추적)
        self.paper_trader = PaperTrader(
            self.db, self.redis, self.ml_swing, self.ml_scalp,
            self.regime_detector, signal_tracker=None  # 아래에서 할당
        )

        # 역사 백필 학습 엔진 (candle_collector 연결 → 90일 수집 가능)
        self.hist_learner = HistoricalLearner(
            self.db, self.ml_swing, self.ml_scalp, self.candle_collector
        )

        # 자동 백테스트
        self.auto_backtest = AutoBacktest(self.db, self.ml_swing, self.ml_scalp)
        self._last_backtest = None

        # 메타 러너 (자가 업그레이드)
        self.meta_learner = MetaLearner(self.ml_swing, self.ml_scalp)
        self._last_meta = None

        # 시그널 기여도 추적
        self.signal_tracker = SignalTracker()
        self.paper_trader.signal_tracker = self.signal_tracker

        # 셋업 성과 추적 (자기개선)
        self.setup_tracker = SetupTracker()
        self.paper_trader.setup_tracker = self.setup_tracker

        # 스캘핑 리스크 관리
        self._scalp_daily_pnl = 0.0         # 일일 스캘핑 P&L (%)
        self._scalp_streak = 0               # 연패 카운터
        self._scalp_cooldown_until = 0       # 쿨다운 종료 시각 (timestamp)
        # 04-13: Swing/Scalp SL 쿨다운 분리 (H4)
        self._swing_last_sl_dir = None
        self._swing_last_sl_time = 0
        self._scalp_last_sl_dir = None
        self._scalp_last_sl_time = 0
        self._scalp_pending_signal = None    # 진입 확인 대기 시그널
        self._scalp_pending_price = 0.0

        # 캐시
        self._last_fast = {}
        self._last_slow = {}
        self._last_scalp = {}
        self._current_day = 0
        self._running = False

        # 학습-매매 격리 메모리 fallback (Redis 일시 끊김 대비)
        # — sys:learning 키 set/get 실패 시에도 동일 프로세스 내에서는 차단 보장
        self._learning_local = False

    async def initialize(self):
        logger.info("=" * 50)
        logger.info("CryptoAnalyzer v2.0 — Dual Model + AdaptiveML")
        logger.info("=" * 50)

        await self.db.connect()
        await self.redis.connect()
        await self.candle_collector.init_exchange()
        await self.oi_funding.init_exchange()
        await self.telegram.initialize()
        await self.executor.initialize()

        # ML 콜드스타트 (04-15: 기존 모델 폐기, 빈 모델로 시작)
        # 기존 pkl은 서버에 백업으로 남아있음
        logger.info("ML: 콜드스타트 (통합 모델 — 기존 데이터 폐기, raw 시그널로 매매)")

        # 잔고 + 리스크
        balance = await self.executor.get_balance()
        await self.risk_manager.initialize(balance)
        logger.info(f"계좌 잔고: ${balance:.2f}")

        # 포지션 동기화 + ML 콜백 연결 + 청산 알림 주입
        self.position_manager.on_trade_closed = self.record_ml_trade
        self.position_manager.telegram = self.telegram
        self.position_manager.trade_logger = self.trade_logger
        self.position_manager.risk_manager = self.risk_manager
        await self.position_manager.sync_positions()

        # 캔들 백필 (15m, 1h, 5m, 1m)
        logger.info("캔들 백필 시작...")
        await self.candle_collector.backfill_all()
        for tf in ["5m", "1m"]:
            await self.candle_collector.backfill(tf, days=7)
        logger.info("캔들 백필 완료")

        # 04-15: ML 콜드스타트 — 역사 학습 비활성
        logger.info("통합 엔진 v1 — ML 비활성 (페이퍼 데이터 축적 후 학습 예정)")

    # ── Swing 시그널 ──

    async def run_swing_signal(self):
        """Swing: 15m Fast + Slow → 합산 → ML 조정"""
        candles = await self.db.get_candles(self.symbol, "15m", limit=300)
        if not candles or len(candles) < 50:
            return None

        df = BaseIndicator.to_dataframe(candles)

        # 1H 추세
        candles_1h = await self.db.get_candles(self.symbol, "1h", limit=100)
        htf_trend = "unknown"
        if candles_1h and len(candles_1h) >= 20:
            df_1h = BaseIndicator.to_dataframe(candles_1h)
            htf = await MarketStructureIndicator().calculate(df_1h)
            htf_trend = htf.get("trend", "unknown")

        ctx = {"htf_trend": htf_trend}
        fast = {}
        for e in self.fast_engines:
            try:
                r = await e.calculate(df, ctx)
                fast[r["type"]] = r
                if r["type"] == "bollinger":
                    ctx["bb_position"] = r["bb_position"]
            except Exception as ex:
                logger.error(f"Fast [{e.__class__.__name__}]: {ex}")

        # Slow context (실시간 데이터)
        slow_ctx = await self._build_slow_context()
        slow = {}
        for e in self.slow_engines:
            try:
                r = await e.calculate(df, slow_ctx)
                slow[r["type"]] = r
                if r["type"] == "order_block" and r.get("ob_zone"):
                    slow_ctx["ob_zones"] = [r["ob_zone"]]
                if r["type"] == "open_interest":
                    slow_ctx["oi_spike"] = r.get("oi_spike", False)
            except Exception as ex:
                logger.error(f"Slow [{e.__class__.__name__}]: {ex}")

        self._last_fast = fast
        self._last_slow = slow

        # 레짐 감지 + 변경 알림
        prev_regime = self._current_regime["regime"] if self._current_regime else None
        regime_result = self.regime_detector.detect(df)
        self._current_regime = regime_result
        await self.redis.set("sys:regime", regime_result["regime"], ttl=300)
        await self.redis.set("sys:regime_detail", regime_result, ttl=300)
        # 레짐 변경 시 텔레그램 알림 (안정화는 regime_detector 내부에서 처리)
        new_regime = regime_result.get("regime")
        if prev_regime and new_regime and prev_regime != new_regime:
            logger.info(f"📊 레짐 변경: {prev_regime} → {new_regime}")
            # 레짐 전환 상태 기록 (04-10: 전환 초기 부스트 / 횡보 전환 즉시 중단)
            import time as _t
            if new_regime in ("trending_up", "trending_down") and prev_regime == "ranging":
                self._regime_transition = "to_trending"
                self._regime_transition_time = _t.time()
                logger.info(f"📈 ranging → trending 전환 감지 → 30분간 레버리지 1.5x 부스트")
            elif new_regime == "ranging":
                self._regime_transition = "to_ranging"
                self._regime_transition_time = _t.time()
                logger.info(f"📉 trending → ranging 전환 감지 → 신규 진입 즉시 중단")
            else:
                self._regime_transition = None
            if self.telegram:
                try:
                    await self.telegram.notify_regime_change(
                        prev_regime, new_regime,
                        confidence=regime_result.get("confidence", 0),
                    )
                except Exception:
                    pass
        # 전환 부스트/블록 만료 체크
        import time as _t
        if self._regime_transition and hasattr(self, '_regime_transition_time'):
            elapsed = _t.time() - self._regime_transition_time
            if self._regime_transition == "to_trending" and elapsed > 1800:
                self._regime_transition = None
            elif self._regime_transition == "to_ranging" and elapsed > 600:
                # 04-13: to_ranging 블록도 10분 후 자동 만료
                self._regime_transition = None

        # 합산
        aggregated = self.aggregator.aggregate(fast, slow)
        all_signals = {**fast, **slow}

        # ML 조정 (레짐 정보 포함)
        ml_meta = {
            "atr_pct": fast.get("atr", {}).get("atr_pct", 0.3),
            "hour": datetime.now(timezone.utc).hour,
            "regime": regime_result["regime"],
        }
        ml_enabled = (await self.redis.get("sys:ml_enabled") or "on") == "on"
        if ml_enabled:
            adjusted = self.ml_swing.get_adjusted_score(aggregated["score"], all_signals, ml_meta)
        else:
            adjusted = aggregated["score"]

        aggregated["score"] = adjusted

        # Redis 저장
        await self.redis.set(f"sig:fast:{self.symbol}", fast, ttl=1800)
        await self.redis.set(f"sig:slow:{self.symbol}", slow, ttl=1800)

        return aggregated

    # ── Scalp 시그널 ──

    async def run_scalp_signal(self):
        """Scalp: 1m/5m → ScalpEngine → ML 조정"""
        candles_1m = await self.db.get_candles(self.symbol, "1m", limit=100)
        candles_5m = await self.db.get_candles(self.symbol, "5m", limit=100)
        candles_15m = await self.db.get_candles(self.symbol, "15m", limit=50)

        if not candles_1m or len(candles_1m) < 20 or not candles_5m or len(candles_5m) < 20:
            return None

        df_1m = BaseIndicator.to_dataframe(candles_1m)
        df_5m = BaseIndicator.to_dataframe(candles_5m)
        df_15m = BaseIndicator.to_dataframe(candles_15m) if candles_15m and len(candles_15m) >= 20 else None

        # 실시간 가격 변속도 데이터 (급등락 감지용)
        rt_velocity = await self.redis.hgetall("rt:velocity:BTC-USDT-SWAP")

        result = await self.scalp_engine.analyze(df_1m, df_5m, df_15m, rt_velocity)
        self._last_scalp = result

        # ML 조정 (raw 점수 보존 — 디버깅용)
        result["raw_score"] = result["score"]
        ml_enabled = (await self.redis.get("sys:ml_enabled") or "on") == "on"
        if ml_enabled:
            adjusted = self.ml_scalp.get_adjusted_score(result["score"], result["signals"])
        else:
            adjusted = result["score"]

        result["score"] = adjusted
        return result

    # ── 매매 판단 + 실행 ──

    async def _evaluate_swing(self):
        """Swing 시그널 평가 + 자동매매 (60초 주기)"""
        autotrading = (await self.redis.get("sys:autotrading") or "off") == "on"
        active_model = await self.redis.get("sys:active_model") or "both"

        open_positions = list(self.position_manager.positions.values())
        risk_state = await self.risk_manager.get_risk_state([p.to_dict() for p in open_positions])
        fn_min = await self.redis.get("rt:funding_next_min:BTC-USDT-SWAP")
        if fn_min and int(fn_min) <= 15:
            risk_state["funding_blackout"] = True
        risk_state["has_same_symbol"] = self.symbol in self.position_manager.positions

        swing_agg = await self.run_swing_signal()
        if swing_agg:
            swing_grade = self.grader.grade(swing_agg, risk_state)
            await self.redis.set(f"sig:aggregated:{self.symbol}",
                                 {"aggregated": swing_agg, "grade": swing_grade}, ttl=1800)

            # 가상매매 전수 학습 (학습 중엔 스킵 — CPU 절약 + 실거래 보호 우선)
            price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
            learning = self._learning_local or (await self.redis.get("sys:learning")) == "1"
            if price_str and not learning:
                agg = swing_agg.copy()
                agg["atr_pct"] = self._last_fast.get("atr", {}).get("atr_pct", 0.3)
                agg["signals_detail"] = {**self._last_fast, **self._last_slow}
                await self.paper_trader.try_entry(agg, "swing", float(price_str))

            # 실거래 (뉴스 필터 + 리스크 체크)
            if autotrading and active_model in ("swing", "both") and swing_grade["tradeable"]:
                blocked, reason = self.news_filter.is_news_blackout()
                if blocked:
                    logger.info(f"[NEWS] Swing 매매 차단: {reason}")
                else:
                    allowed, _ = self.risk_manager.is_trading_allowed()
                    if allowed:
                        await self._execute_swing(swing_grade, swing_agg, risk_state)

    async def _evaluate_scalp(self):
        """스캘핑 시그널 평가 + 자동매매 (5초 주기)"""
        import time as _t

        # 일일 손실 한도 체크 (-10%)
        if self._scalp_daily_pnl <= -10.0:
            return

        # 쿨다운 체크
        if _t.time() < self._scalp_cooldown_until:
            return

        scalp_sig = await self.run_scalp_signal()
        if not scalp_sig:
            logger.debug("[SCALP] run_scalp_signal None — 캔들 부족 또는 ScalpEngine 오류")
            return

        # 디버깅용 INFO 로그 (60초마다 한 번만 — INFO 폭주 방지)
        now_ts = _t.time()
        if now_ts - getattr(self, "_last_scalp_log", 0) >= 60:
            self._last_scalp_log = now_ts
            raw = scalp_sig.get("raw_score", scalp_sig.get("score", 0))
            adj = scalp_sig.get("score", 0)
            thr = getattr(self.ml_scalp, "entry_threshold", 0)
            logger.info(
                f"[SCALP] raw: {raw:.2f} → ML조정: {adj:.2f} (Δ{adj-raw:+.2f}) | "
                f"임계값: {thr:.2f} | "
                f"방향: {scalp_sig.get('direction', 'neutral')} | "
                f"SMC: {scalp_sig.get('smc_entry', False)} | "
                f"급변동: {scalp_sig.get('explosive_mode', False)} | "
                f"세션: {scalp_sig.get('session', '?')}"
            )

        price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
        current_price = float(price_str) if price_str else 0
        if current_price <= 0:
            return

        # 스캘핑 상태 Redis 저장 (대시보드용)
        await self.redis.set("sys:scalp_state", {
            "daily_pnl": round(self._scalp_daily_pnl, 2),
            "streak": self._scalp_streak,
            "cooldown": self._scalp_cooldown_until > _t.time(),
            "score": scalp_sig["score"],
            "direction": scalp_sig["direction"],
            "explosive": scalp_sig.get("explosive_mode", False),
            "smc": scalp_sig.get("smc_entry", False),
            "session": scalp_sig.get("session", "unknown"),
        }, ttl=30)

        # 가상매매 전수 학습 (학습 중엔 스킵)
        learning = self._learning_local or (await self.redis.get("sys:learning")) == "1"
        if not learning:
            await self.paper_trader.try_entry(scalp_sig, "scalp", current_price)

        # ── 진입 확인 대기 로직 ──
        # 1차: 시그널 발생 → 대기 상태로 저장
        # 2차 (5초 후): 가격이 같은 방향이면 진입 확정
        if self._scalp_pending_signal:
            pending = self._scalp_pending_signal
            # 방향 확인: 5초 전 시그널 방향과 현재 가격 비교
            confirmed = False
            if pending["direction"] == "long" and current_price > self._scalp_pending_price:
                confirmed = True
            elif pending["direction"] == "short" and current_price < self._scalp_pending_price:
                confirmed = True

            # 04-13 개선: 강한 시그널(score 5.0+)이면 가격 역행해도 진입 허용
            # 폭락 시 5초 내 되돌림으로 모든 시그널 폐기되던 문제 해결
            if not confirmed and pending.get("score", 0) >= 5.0:
                confirmed = True
                logger.info(f"[SCALP] 가격 역행이지만 점수 {pending['score']:.1f} >= 5.0 → 강제 진입")

            if confirmed:
                await self._try_scalp_entry(pending, current_price)
            # 확인 실패 또는 완료 → 대기 초기화
            self._scalp_pending_signal = None
            self._scalp_pending_price = 0.0
            return

        # 새 시그널이 임계값 이상이면 대기 상태로
        if scalp_sig["score"] >= self.ml_scalp.entry_threshold and scalp_sig["direction"] != "neutral":
            self._scalp_pending_signal = scalp_sig
            self._scalp_pending_price = current_price

    async def _try_scalp_entry(self, scalp_sig: dict, current_price: float):
        """스캘핑 진입 실행 (확인 완료 후)"""
        autotrading = (await self.redis.get("sys:autotrading") or "off") == "on"
        active_model = await self.redis.get("sys:active_model") or "both"

        if not autotrading or active_model not in ("scalp", "both"):
            return

        # 뉴스 차단 체크
        blocked, reason = self.news_filter.is_news_blackout()
        if blocked:
            logger.info(f"[NEWS] Scalp 매매 차단: {reason}")
            return

        # 포지션 크기 차등
        if scalp_sig.get("smc_entry"):
            size_mult = 1.2  # SMC: 120%
        elif scalp_sig.get("explosive_mode"):
            size_mult = 1.0  # 급변동: 100%
        else:
            size_mult = 0.8  # 일반: 80%

        scalp_sig["size_mult"] = size_mult

        risk_state = await self.risk_manager.get_risk_state()
        if self.symbol not in self.position_manager.positions:
            allowed, _ = self.risk_manager.is_trading_allowed()
            if allowed:
                await self._execute_scalp(scalp_sig, risk_state)

    def _scalp_record_result(self, pnl_pct: float):
        """스캘핑 결과 기록 → 일일 P&L + 연패 관리 + 텔레그램 알림"""
        import time as _t
        self._scalp_daily_pnl += pnl_pct

        if pnl_pct <= 0:
            self._scalp_streak += 1
            # 쿨다운 강화 (04-10: 연속 SL → 즉시 재진입 방지)
            if self._scalp_streak >= 5:
                self._scalp_cooldown_until = _t.time() + 7200  # 120분
                logger.warning(f"[SCALP] 5연패 → 120분 쿨다운")
                if self.telegram:
                    asyncio.create_task(self.telegram.notify_cooldown(self._scalp_streak, 120))
            elif self._scalp_streak >= 3:
                self._scalp_cooldown_until = _t.time() + 1800  # 30분
                logger.warning(f"[SCALP] 3연패 → 30분 쿨다운")
                if self.telegram:
                    asyncio.create_task(self.telegram.notify_cooldown(self._scalp_streak, 30))
            elif self._scalp_streak >= 2:
                self._scalp_cooldown_until = _t.time() + 600  # 10분
                logger.warning(f"[SCALP] 2연패 → 10분 쿨다운")
        else:
            self._scalp_streak = 0

        # 일일 손실 -8% 임박 경고 (한도 -10% 도달 전 사전 알림, 1일 1회만)
        if self._scalp_daily_pnl <= -8.0 and not getattr(self, "_loss_warning_sent", False):
            self._loss_warning_sent = True
            logger.warning(f"[SCALP] 일일 손실 -8% 임박 ({self._scalp_daily_pnl:.1f}%)")
            if self.telegram:
                remaining = abs(self._scalp_daily_pnl + 10)
                asyncio.create_task(self.telegram.notify_warning(
                    f"일일 손실 {self._scalp_daily_pnl:.1f}% 도달 — "
                    f"한도(-10%)까지 {remaining:.1f}% 남음\n"
                    f"연패: {self._scalp_streak}회"
                ))

        if self._scalp_daily_pnl <= -10.0:
            logger.warning(f"[SCALP] 일일 손실 한도 도달 ({self._scalp_daily_pnl:.1f}%) → 스캘핑 중단")
            if self.telegram:
                asyncio.create_task(self.telegram.notify_emergency(
                    f"일일 손실 -10% 도달 ({self._scalp_daily_pnl:.1f}%) → 스캘핑 자동 중단"
                ))

    async def _execute_swing(self, grade_result, aggregated, risk_state):
        """Swing 매매 실행"""
        # 04-13 개선: 학습 중에도 실거래 허용 (학습 락은 가상매매만 차단)
        # 기존: 학습 1~2시간 동안 모든 매매 차단 → 폭락 시 기회 놓침
        if self._learning_local or await self.redis.get("sys:learning") == "1":
            logger.info("[SWING] 학습 중이지만 실거래는 허용")

        # 04-13 개선: ranging 레짐에서도 고점수 시그널은 진입 허용
        # (폭락 전 횡보→폭락 시 레짐 전환 지연으로 모든 매매 차단되던 문제 해결)
        is_ranging = self._current_regime and self._current_regime.get("regime") == "ranging"
        swing_score = grade_result.get("score", 0)
        if is_ranging and swing_score < 7.0:
            logger.info(f"[SWING] ranging 레짐 + 점수 {swing_score:.1f} < 7.0 → 차단")
            return

        # 04-13 개선: to_ranging 전환 블록 10분 제한 (기존: 무기한)
        import time as _t
        if self._regime_transition == "to_ranging":
            elapsed = _t.time() - getattr(self, '_regime_transition_time', 0)
            if elapsed < 600 and swing_score < 7.0:  # 10분 이내 + 낮은 점수만 차단
                logger.info(f"[SWING] trending→ranging 전환 {elapsed:.0f}s < 600s → 차단")
                return

        direction = grade_result["direction"]

        # 04-13: Swing 전용 SL 쿨다운 30분
        import time as _t
        if (self._swing_last_sl_dir == direction
                and _t.time() - self._swing_last_sl_time < 1800):
            logger.info(f"[SWING] {direction} SL 후 30분 미경과 → 같은 방향 재진입 차단")
            return

        atr_pct = self._last_fast.get("atr", {}).get("atr_pct", 0.3)
        lev = self.leverage_calc.calculate(grade_result["grade"], atr_pct, risk_state.get("streak", 0))
        balance = risk_state.get("balance", 0)
        if balance <= 0:
            return

        price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
        price = float(price_str) if price_str else 0
        if price <= 0:
            return

        leverage = lev["leverage"]
        # ranging → trending 전환 초기 30분: 레버리지 1.5x 부스트
        if self._regime_transition == "to_trending":
            max_lev = self.config["risk"]["leverage_range"][1]
            leverage = min(int(leverage * 1.5), max_lev)
            logger.info(f"[SWING] 🚀 trending 전환 부스트 → 레버리지 {leverage}x")
        sizing_mode = self.config["risk"].get("sizing_mode", "margin_loss_cap")

        # ── 사이즈 + SL/TP 계산 ──
        risk_cfg = self.config["risk"]

        if sizing_mode == "margin_loss_cap":
            # 사용자 의도: SL/TP 모두 마진 손익 % 기준
            # 가격 거리 = 마진 % / leverage
            max_loss_pct = risk_cfg.get("max_margin_loss_pct", 10.0)
            tp1_gain_pct = risk_cfg.get("tp1_margin_gain_pct", 15.0)
            margin_pct = risk_cfg.get("margin_pct", 0.95)
            use_indicator = risk_cfg.get("use_indicator_sl", True)
            min_ind_pct = risk_cfg.get("min_indicator_sl_price_pct", 0.05)

            # SL 거리: 마진 한도
            margin_limit_dist = price * (max_loss_pct / leverage / 100)
            if use_indicator:
                indicator_dist = self._last_fast.get("atr", {}).get("sl_distance", margin_limit_dist)
                # 매물대가 너무 가까우면 (즉시 청산 위험) 무시
                if indicator_dist < price * (min_ind_pct / 100):
                    indicator_dist = margin_limit_dist
                sl_dist = min(indicator_dist, margin_limit_dist)
            else:
                sl_dist = margin_limit_dist

            # TP 거리: 마진 손익 % / leverage
            tp1_dist = price * (tp1_gain_pct / leverage / 100)
            # TP2/TP3 는 러너 모드에서 미사용 (호환용으로 약간 큰 값)
            tp2_dist = tp1_dist * 2
            tp3_dist = tp1_dist * 4

            margin = balance * margin_pct
            logger.info(
                f"[SWING-SIZING] balance ${balance:.2f} × {margin_pct*100:.0f}% = "
                f"margin ${margin:.2f} | {leverage}x | "
                f"SL ${sl_dist:.1f} ({sl_dist/price*100:.3f}% 가격 = "
                f"{sl_dist/price*100*leverage:.1f}% 마진 손실) | "
                f"TP1 ${tp1_dist:.1f} ({tp1_dist/price*100*leverage:.1f}% 마진 익절)"
            )
        else:
            # 옛 방식: risk_per_trade 기반
            margin = self.leverage_calc.calculate_position_size(
                balance, leverage, lev["sl_pct"], grade_result["size_pct"])
            if margin <= 0:
                return
            sl_dist = price * lev["sl_pct"] / 100
            if sl_dist <= 0:
                sl_dist = price * 0.003
            tp1_rr = risk_cfg.get("tp1_rr", 1.5)
            tp2_rr = risk_cfg.get("tp2_rr", 2.5)
            tp3_rr = risk_cfg.get("tp3_rr", 4.0)
            tp1_dist = sl_dist * tp1_rr
            tp2_dist = sl_dist * tp2_rr
            tp3_dist = sl_dist * tp3_rr

        if direction == "long":
            sl = price - sl_dist
            tp1 = price + tp1_dist
            tp2 = price + tp2_dist
            tp3 = price + tp3_dist
        else:
            sl = price + sl_dist
            tp1 = price - tp1_dist
            tp2 = price - tp2_dist
            tp3 = price - tp3_dist

        logger.info(
            f"[SWING] {grade_result['grade']} {direction.upper()} @ ${price:.0f} "
            f"SL ${sl:.0f} TP1 ${tp1:.0f} TP2 ${tp2:.0f} TP3 ${tp3:.0f}"
        )

        # OKX BTC-USDT-SWAP: 1 contract = 0.01 BTC, sz 는 contracts 단위
        # 0.01 단위 down rounding (math.floor) — round() 의 banker's rounding 회피
        raw_size = margin * leverage / price  # 04-13: 부스트된 leverage 사용 (H5)
        size_btc = math.floor(raw_size / 0.01) * 0.01
        size_btc = round(size_btc, 4)

        trade_req = {
            "symbol": self.symbol, "direction": direction,
            "grade": grade_result["grade"], "score": grade_result["score"],
            "size": size_btc,
            "leverage": lev["leverage"],
            "entry_price": None if grade_result["execution"] == "market" else price,
            "sl_price": round(sl, 1),
            "tp1_price": round(tp1, 1),
            "tp2_price": round(tp2, 1),
            "tp3_price": round(tp3, 1),
            "signals_snapshot": aggregated.get("signals_detail", {}),
        }
        pos = await self.position_manager.open_position(trade_req)
        if pos:
            await self.telegram.notify_entry(direction, grade_result["grade"], grade_result["score"],
                                             pos.entry_price, pos.sl_price, pos.tp1_price, pos.tp2_price,
                                             lev["leverage"], margin, tp3_price=pos.tp3_price)
            self.trade_logger.log_entry(direction, grade_result["grade"], grade_result["score"],
                                        pos.entry_price, pos.sl_price, lev["leverage"], margin)

    async def _execute_scalp(self, scalp_sig, risk_state):
        """Scalp 매매 실행"""
        # 04-13 개선: 학습 중에도 실거래 허용
        if self._learning_local or await self.redis.get("sys:learning") == "1":
            logger.info("[SCALP] 학습 중이지만 실거래는 허용")

        direction = scalp_sig["direction"]
        if direction == "neutral":
            return

        # 04-13: Scalp 전용 SL 쿨다운 15분
        import time as _t
        if (self._scalp_last_sl_dir == direction
                and _t.time() - self._scalp_last_sl_time < 900):
            logger.info(f"[SCALP] {direction} SL 후 15분 미경과 → 같은 방향 재진입 차단")
            return

        # ── 모드 분기: explosive (변동성 폭발) vs ranging (박스권) vs 일반 ──
        regime = self._current_regime.get("regime") if self._current_regime else None
        is_ranging = (regime == "ranging")
        is_explosive = bool(scalp_sig.get("explosive_mode", False))
        is_smc = bool(scalp_sig.get("smc_entry", False))
        score = scalp_sig.get("score", 0)

        # 04-13 개선: to_ranging 전환 블록 5분 제한 + 고점수 우회
        import time as _t2
        if self._regime_transition == "to_ranging" and not is_explosive:
            elapsed = _t2.time() - getattr(self, '_regime_transition_time', 0)
            if elapsed < 300 and score < 5.0:
                logger.info(f"[SCALP] trending→ranging 전환 {elapsed:.0f}s → 차단")
                return

        # 🚀 EXPLOSIVE MODE
        if is_explosive:
            logger.info(f"[SCALP-EXPLOSIVE] 🚀 변동성 폭발 감지 → Quick Mode 진입 시도")
        elif is_ranging:
            # 04-13 개선: ranging에서도 점수 4.0+면 진입 허용 (기존: SMC/explosive만)
            if not is_smc and score < 4.0:
                logger.info(f"[SCALP] ranging 레짐 + 점수 {score:.1f} < 4.0 → 차단")
                return

        price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
        price = float(price_str) if price_str else 0
        if price <= 0:
            return

        balance = risk_state.get("balance", 0)
        sl_dist_indicator = scalp_sig["sl_distance"]
        # 04-15: 고정 25x → 동적 레버리지 (연패 감소 적용)
        atr_pct = scalp_sig.get("atr_pct", 0.3)
        lev_result = self.leverage_calc.calculate("B+", atr_pct, self._scalp_streak)
        leverage = lev_result["leverage"]
        # ranging → trending 전환 초기: 레버리지 부스트
        if self._regime_transition == "to_trending":
            max_lev = self.config["risk"]["leverage_range"][1]
            leverage = min(max_lev, int(leverage * 1.2))
            logger.info(f"[SCALP] trending 전환 부스트 → 레버리지 {leverage}x")
        if balance <= 0 or sl_dist_indicator <= 0 or price <= 0:
            return

        risk_cfg = self.config["risk"]
        sizing_mode = risk_cfg.get("sizing_mode", "margin_loss_cap")

        if sizing_mode == "margin_loss_cap":
            margin_pct = risk_cfg.get("margin_pct", 0.95)

            # ── EXPLOSIVE Quick Mode: SL/TP + 5분 timeout ──
            if is_explosive:
                # 04-15 수정: SL 5% / TP 10% (RR 2.0) — 기존 SL 3%/TP 5% (RR 1.67 but 수수료 후 <1)
                sl_dist = price * (5.0 / leverage / 100)   # 마진 5%
                tp1_dist = price * (10.0 / leverage / 100)  # 마진 10% (RR 2.0)
                tp2_dist = tp1_dist * 1.5
                tp3_dist = tp1_dist * 2
                margin = balance * margin_pct
                logger.info(
                    f"[SCALP-EXPLOSIVE] SL 마진-5% (${sl_dist:.1f}) | "
                    f"TP1 마진+10% (${tp1_dist:.1f}) | lev {leverage}x | 5분 timeout"
                )
            else:
                # ── 일반 Mode (옛 방식) ──
                max_loss_pct = risk_cfg.get("max_margin_loss_pct", 10.0)
                tp1_gain_pct = risk_cfg.get("tp1_margin_gain_pct", 15.0)
                use_indicator = risk_cfg.get("use_indicator_sl", True)
                min_ind_pct = risk_cfg.get("min_indicator_sl_price_pct", 0.30)

                margin_limit_dist = price * (max_loss_pct / leverage / 100)
                if use_indicator:
                    ind_dist = sl_dist_indicator
                    if ind_dist < price * (min_ind_pct / 100):
                        ind_dist = margin_limit_dist
                    sl_dist = min(ind_dist, margin_limit_dist)
                else:
                    sl_dist = margin_limit_dist

                tp1_dist = price * (tp1_gain_pct / leverage / 100)
                tp2_dist = tp1_dist * 2
                tp3_dist = tp1_dist * 4

                margin = balance * margin_pct

                logger.info(
                    f"[SCALP-SIZING] balance ${balance:.2f} × {margin_pct*100:.0f}% = "
                    f"margin ${margin:.2f} | {leverage}x | "
                    f"SL ${sl_dist:.1f} ({sl_dist/price*100*leverage:.1f}% 마진 손실) | "
                    f"TP1 ${tp1_dist:.1f} ({tp1_dist/price*100*leverage:.1f}% 마진 익절)"
                )
        else:
            risk_per_trade = 0.008
            sl_pct_decimal = sl_dist_indicator / price
            margin = (balance * risk_per_trade) / (leverage * sl_pct_decimal)
            margin = min(margin, balance * 0.3)
            sl_dist = sl_dist_indicator
            tp1_rr = risk_cfg.get("tp1_rr_scalp", 1.0)
            tp2_rr = risk_cfg.get("tp2_rr_scalp", 1.6)
            tp3_rr = risk_cfg.get("tp3_rr_scalp", 2.5)
            tp1_dist = sl_dist * tp1_rr
            tp2_dist = sl_dist * tp2_rr
            tp3_dist = sl_dist * tp3_rr

        if margin <= 0:
            return

        if direction == "long":
            sl = price - sl_dist
            tp1 = price + tp1_dist
            tp2 = price + tp2_dist
            tp3 = price + tp3_dist
        else:
            sl = price + sl_dist
            tp1 = price - tp1_dist
            tp2 = price - tp2_dist
            tp3 = price - tp3_dist

        logger.info(
            f"[SCALP] {direction.upper()} @ ${price:.0f} SL ${sl:.0f} "
            f"TP1 ${tp1:.0f} TP2 ${tp2:.0f} TP3 ${tp3:.0f} | lev {leverage}x"
        )

        # 04-15: 수수료 대비 기대수익 필터 — TP1 도달 시 수수료 뺀 순수익이 양수인지 확인
        taker_fee = self.config.get("fees", {}).get("taker", 0.0005)
        fee_cost_pct = taker_fee * 2 * leverage  # 진입+청산 수수료 (마진 대비 %)
        tp1_gain_margin_pct = tp1_dist / price * leverage * 100  # TP1 마진 수익%
        if tp1_gain_margin_pct <= fee_cost_pct * 100:
            logger.info(
                f"[SCALP] TP1 수익({tp1_gain_margin_pct:.1f}%) <= 수수료({fee_cost_pct*100:.1f}%) → 진입 차단"
            )
            return

        # OKX 0.01 BTC 단위 floor 스냅
        raw_size = margin * leverage / price
        size_btc = math.floor(raw_size / 0.01) * 0.01
        size_btc = round(size_btc, 4)

        trade_req = {
            "symbol": self.symbol, "direction": direction,
            "grade": "SCALP", "score": scalp_sig["score"],
            "size": size_btc,
            "leverage": leverage, "entry_price": None,
            "sl_price": round(sl, 1),
            "tp1_price": round(tp1, 1),
            "tp2_price": round(tp2, 1),
            "tp3_price": round(tp3, 1),
            "signals_snapshot": scalp_sig.get("signals", {}),
        }
        pos = await self.position_manager.open_position(trade_req)
        if pos:
            await self.telegram.notify_entry(direction, "SCALP", scalp_sig["score"],
                                             pos.entry_price, pos.sl_price, pos.tp1_price, pos.tp2_price,
                                             leverage, margin, tp3_price=pos.tp3_price)

    # ── Context 빌더 ──

    async def _build_slow_context(self) -> dict:
        ctx = {}
        oi_val = await self.redis.get("rt:oi:BTC-USDT-SWAP")
        ctx["oi_current"] = float(oi_val) if oi_val else 0
        oi_history = await self.db.get_oi_funding(self.symbol, limit=24)
        ctx["oi_history"] = oi_history
        fr_val = await self.redis.get("rt:funding:BTC-USDT-SWAP")
        ctx["funding_rate"] = float(fr_val) if fr_val else 0
        fn_min = await self.redis.get("rt:funding_next_min:BTC-USDT-SWAP")
        ctx["funding_next_min"] = int(fn_min) if fn_min else 999
        # 04-13: 각 히스토리를 올바른 데이터로 조회 (H6: 전부 oi_history 쓰던 버그)
        funding_history = await self.db.get_funding_history(self.symbol, limit=24) \
            if hasattr(self.db, 'get_funding_history') else oi_history
        ctx["funding_history"] = funding_history
        ls_val = await self.redis.get("rt:ls_ratio:BTC-USDT-SWAP")
        ctx["ls_ratio_account"] = float(ls_val) if ls_val else 1.0
        ls_history = await self.db.get_ls_history(self.symbol, limit=24) \
            if hasattr(self.db, 'get_ls_history') else oi_history
        ctx["ls_history"] = ls_history
        # CVD 는 진행 중 윈도우 (cvd:15m:current) 우선, 없으면 직전 윈도우 합계 (cvd:15m) fallback
        # → 시그널 엔진이 1봉 lag 없이 현재 누적값을 본다 (BUG #1 fix)
        cvd_15m_cur = await self.redis.get("cvd:15m:current:BTC-USDT-SWAP")
        cvd_15m_prev = await self.redis.get("cvd:15m:BTC-USDT-SWAP")
        cvd_1h_cur = await self.redis.get("cvd:1h:current:BTC-USDT-SWAP")
        cvd_1h_prev = await self.redis.get("cvd:1h:BTC-USDT-SWAP")
        ctx["cvd_15m"] = float(cvd_15m_cur) if cvd_15m_cur else (float(cvd_15m_prev) if cvd_15m_prev else 0)
        ctx["cvd_1h"] = float(cvd_1h_cur) if cvd_1h_cur else (float(cvd_1h_prev) if cvd_1h_prev else 0)
        return ctx

    # ── ML 학습 기록 ──

    async def record_ml_trade(self, mode: str, signals: dict, pnl_pct: float,
                             fee_pct: float = 0.0, direction: str = "",
                             exit_reason: str = "", pnl_usdt: float = 0.0,
                             hold_min: float = 0.0):
        """실거래 결과 → 연패 관리 + 셋업 추적 (ML 비활성)"""
        # 통합 모델 연패/쿨다운 관리
        self._unified_record_result(pnl_pct, exit_reason)

        # 시그널 기여도 추적
        regime = self._current_regime["regime"] if self._current_regime else "ranging"
        if self.signal_tracker:
            self.signal_tracker.record_trade(signals, pnl_pct, mode="unified", regime=regime)

        # 셋업 성과 추적
        setup = None
        for key in ("setup_a", "setup_b", "setup_c"):
            if key in signals:
                setup = key[-1].upper()
                break
        if setup:
            trend = signals.get("context", {}).get("trend", "neutral")
            self.setup_tracker.record_trade(
                setup=setup, direction=direction, pnl_pct=pnl_pct,
                pnl_usdt=pnl_usdt, hold_min=hold_min,
                exit_reason=exit_reason, trend=trend,
            )

        logger.info(f"[실거래] PnL {pnl_pct:+.2f}% 연패:{self._unified_streak} 레짐:{regime}")

    # ── 역사 학습 ──

    async def _initial_history_learn(self):
        """봇 시작 시 과거 데이터 학습 (백그라운드)"""
        import time as _time
        await asyncio.sleep(30)  # 캔들 수집 완료 대기
        _t0 = _time.time()
        try:
            self._learning_local = True
            await self.redis.set("sys:learning", "1", ttl=3600)
            logger.info("[HIST] 🔒 초기 역사 백필 학습 시작 (Swing) — 신규 진입 일시 정지")
            try:
                await self.telegram.notify_study_start("초기 백필학습")
            except Exception:
                pass
            await self.hist_learner.run_backfill("15m", lookback=2000, step=5)

            logger.info("[HIST] 초기 역사 백필 학습 시작 (Scalp)...")
            await self.hist_learner.run_scalp_backfill(lookback=2000, step=5)

            await asyncio.to_thread(self.ml_swing.save)
            await asyncio.to_thread(self.ml_scalp.save)
            logger.info("[HIST] 초기 학습 완료 (Swing + Scalp)")
        except Exception as e:
            logger.error(f"[HIST] 초기 학습 에러: {e}")
        finally:
            elapsed = _time.time() - _t0
            self._learning_local = False
            await self.redis.delete("sys:learning")
            logger.info("[HIST] 🔓 초기 학습 종료 — 매매 재개")
            try:
                await self.telegram.notify_study_done(
                    "초기 백필학습", 0, elapsed_sec=elapsed,
                    swing_oos=getattr(self.ml_swing, 'oos_accuracy', 0),
                    scalp_oos=getattr(self.ml_scalp, 'oos_accuracy', 0),
                    swing_buf=len(getattr(self.ml_swing, 'X_buffer', [])),
                    scalp_buf=len(getattr(self.ml_scalp, 'X_buffer', [])),
                )
            except Exception:
                pass

    async def periodic_study_scheduler(self):
        """
        하루 3회 학습 스케줄러 (BTC 시장 조용한 시간대 선택)
        - UTC 22:00 (한국 07:00) → 일일 대량 학습 — 글로벌 최저 활동
        - UTC 04:00 (한국 13:00) → 세션 경량 학습 — Asia 점심
        - UTC 11:00 (한국 20:00) → 세션 경량 학습 — EU/US 사이 짧은 조용

        🔒 학습 중에는 redis sys:learning=1 flag set → _evaluate_swing/scalp 가
           신규 진입을 스킵 (asyncio 블로킹/CPU 점유 race 방지). 잔존 포지션 보호는 정상 작동.
        """
        _last_run = {}

        async def _guarded_study(label: str, coro):
            """학습 작업을 메모리 + redis flag 로 감싸서 실행 — Redis 끊김에도 안전"""
            import time as _time
            _t0 = _time.time()
            result = None
            try:
                self._learning_local = True
                await self.redis.set("sys:learning", "1", ttl=3600)
                logger.info(f"[SCHED] 🔒 학습 모드 ON ({label}) — 신규 진입 일시 정지")
                try:
                    await self.telegram.notify_study_start(label)
                except Exception:
                    pass
                result = await coro
            finally:
                elapsed = _time.time() - _t0
                self._learning_local = False
                await self.redis.delete("sys:learning")
                logger.info(f"[SCHED] 🔓 학습 모드 OFF ({label}) — 매매 재개")
                try:
                    await self.telegram.notify_study_done(
                        label, result, elapsed_sec=elapsed,
                        swing_oos=getattr(self.ml_swing, 'oos_accuracy', 0),
                        scalp_oos=getattr(self.ml_scalp, 'oos_accuracy', 0),
                        swing_buf=len(getattr(self.ml_swing, 'X_buffer', [])),
                        scalp_buf=len(getattr(self.ml_scalp, 'X_buffer', [])),
                    )
                except Exception:
                    pass

        while self._running:
            try:
                now = datetime.now(timezone.utc)
                hour = now.hour
                today = now.strftime("%Y-%m-%d")

                # UTC 22:00 (KST 07:00) — 일일 대량 학습 + 백테스트
                if hour == 22 and _last_run.get("daily") != today:
                    await _guarded_study("일일 대량학습", self.hist_learner.run_daily_study())

                    # 학습 후 백테스트 (백테스트도 무거우니 같이 보호)
                    async def _bt_block():
                        logger.info("[SCHED] 자동 백테스트 시작")
                        bt = await self.auto_backtest.run(days=30)
                        self._last_backtest = bt
                        await self.redis.set("sys:last_backtest", bt, ttl=86400)
                    await _guarded_study("백테스트30일", _bt_block())

                    # 일요일이면 메타 학습 추가 실행
                    if now.weekday() == 6:
                        async def _meta_block():
                            logger.info("[SCHED] 주간 메타 학습 시작 (자가 업그레이드)")
                            meta = await self.meta_learner.run_meta_learning()
                            self._last_meta = meta
                            await self.redis.set("sys:last_meta", meta, ttl=604800)
                        await _guarded_study("주간메타", _meta_block())

                    _last_run["daily"] = today

                # UTC 04:00 (KST 13:00) — 세션 경량 학습 (Asia 점심)
                elif hour == 4 and _last_run.get("session1") != today:
                    await _guarded_study("세션학습1", self.hist_learner.run_session_study())
                    _last_run["session1"] = today

                # UTC 11:00 (KST 20:00) — 세션 경량 학습 (EU/US 사이)
                elif hour == 11 and _last_run.get("session2") != today:
                    await _guarded_study("세션학습2", self.hist_learner.run_session_study())
                    _last_run["session2"] = today

            except Exception as e:
                logger.error(f"[SCHED] 학습 스케줄러 에러: {e}", exc_info=True)
                # 예외로 빠져나간 경우에도 flag 해제 (이중 안전망)
                try:
                    await self.redis.delete("sys:learning")
                except Exception:
                    pass
            await asyncio.sleep(60)

    # ══════════════════════════════════════════════════
    # 통합 엔진 (04-15 전면 개편)
    # ══════════════════════════════════════════════════

    async def periodic_unified_eval(self):
        """통합 시그널 평가 (3초마다) — 셋업 ABC 매칭. Binance 캔들 기반."""
        await asyncio.sleep(5)  # 캔들 백필 대기
        while self._running:
            try:
                await self._evaluate_unified()
            except Exception as e:
                logger.error(f"[TRADE] 평가 에러: {e}", exc_info=True)
            poll_sec = self.config.get("polling", {}).get("signal_eval_sec", 5)
            await asyncio.sleep(poll_sec)

    async def _evaluate_unified(self):
        """통합 엔진 평가 + 매매"""
        import time as _t

        # 일일 손실 한도
        if self._unified_daily_pnl <= -10.0:
            return

        # 쿨다운
        now = _t.time()
        if now < self._unified_cooldown_until:
            return

        # 학습 중 진입 차단
        learning = self._learning_local or (await self.redis.get("sys:learning")) == "1"
        if learning:
            return

        # 이미 포지션 있으면 진입 차단 (max_positions: 1)
        if self.position_manager.positions:
            return

        # 자동매매 상태
        autotrading = (await self.redis.get("sys:autotrading") or "off") == "on"

        # 캔들 로드
        candles_1m = await self.db.get_candles(self.symbol, "1m", limit=100)
        candles_5m = await self.db.get_candles(self.symbol, "5m", limit=100)
        candles_15m = await self.db.get_candles(self.symbol, "15m", limit=100)
        candles_1h = await self.db.get_candles(self.symbol, "1h", limit=100)

        if not candles_1m or len(candles_1m) < 30 or not candles_5m or len(candles_5m) < 30:
            return

        df_1m = BaseIndicator.to_dataframe(candles_1m)
        df_5m = BaseIndicator.to_dataframe(candles_5m)
        df_15m = BaseIndicator.to_dataframe(candles_15m) if candles_15m and len(candles_15m) >= 20 else None
        df_1h = BaseIndicator.to_dataframe(candles_1h) if candles_1h and len(candles_1h) >= 20 else None

        # 실시간 가격 변속도
        rt_velocity = await self.redis.hgetall("rt:velocity:BTC-USDT-SWAP")

        # 통합 엔진 분석
        result = await self.trade_engine.analyze(df_1m, df_5m, df_15m, df_1h, rt_velocity)

        # 컨텍스트 추출
        ctx = result.get("signals", {}).get("context", {})

        # 주기적 로깅 (30초마다)
        if now - getattr(self, "_last_unified_log", 0) >= 30:
            self._last_unified_log = now
            sigs = result.get("signals", {})
            rej_a = sigs.get("reject_a", "")
            rej_b = sigs.get("reject_b", "")
            rej_c = sigs.get("reject_c", "")
            reject_info = f" | A:{rej_a} B:{rej_b} C:{rej_c}" if not result.get("setup") else ""
            logger.info(
                f"[TRADE] setup={result.get('setup') or 'none'} "
                f"dir={result.get('direction', 'neutral')} "
                f"score={result.get('score', 0):.1f} "
                f"trend={ctx.get('trend', '?')} "
                f"structure={ctx.get('structure', '?')} "
                f"streak={self._unified_streak}"
                f"{reject_info}"
            )

        # TradeEngine 상태 Redis 저장 (대시보드 + 텔레그램)
        await self.redis.set("sys:trade_state", {
            "setup": result.get("setup"),
            "direction": result.get("direction", "neutral"),
            "score": result.get("score", 0),
            "trend": ctx.get("trend", "neutral"),
            "structure": ctx.get("structure", "unknown"),
            "streak": self._unified_streak,
            "hold_mode": result.get("hold_mode", "standard"),
        }, ttl=30)

        # 셋업 없으면 리턴
        if not result.get("setup"):
            return

        setup = result["setup"]
        direction = result["direction"]
        score = result["score"]

        # 셋업 자동 비활성 체크
        if not self.setup_tracker.is_setup_enabled(setup):
            logger.info(f"[TRADE] Setup {setup} 비활성 (성과 부진) → 스킵")
            return

        # 셋업 감지 기록
        self.setup_tracker.record_detection(setup, direction, score, float(df_5m["close"].iloc[-1]))

        # 셋업 감지 텔레그램 알림
        price_now = float(df_5m["close"].iloc[-1])
        await self.telegram.notify_setup_detected(
            setup, direction, score, price_now, result.get("reason", "")
        )

        if direction == "neutral":
            return

        # 최소 진입 간격
        cooldown_cfg = self.config.get("cooldown", {})
        min_interval = cooldown_cfg.get("min_interval_sec", 60)
        if now - self._unified_last_trade_time < min_interval:
            elapsed = now - self._unified_last_trade_time
            logger.debug(f"[TRADE] 최소진입간격 쿨다운 → 대기 ({elapsed:.0f}s < {min_interval}s)")
            return

        # 같은 가격대 재진입 방지 (SL 맞고 같은 자리 재진입 차단)
        if self._unified_last_exit_reason and "sl" in self._unified_last_exit_reason:
            last_trade_price = getattr(self, "_unified_last_entry_price", 0)
            if last_trade_price > 0 and abs(price - last_trade_price) / price < 0.003:
                logger.info(f"[TRADE] 같은 가격대 재진입 차단 (${price:.0f} ≈ ${last_trade_price:.0f})")
                return

        # 방향 전환 쿨다운
        if self._unified_last_dir and self._unified_last_dir != direction:
            flip_cd = cooldown_cfg.get("direction_flip_sec", 300)
            if now - self._unified_last_trade_time < flip_cd:
                logger.info(f"[TRADE] 방향 전환 쿨다운 ({flip_cd}s) → 대기")
                return

        # 04-17: 같은 방향 연속 4회 이상 차단 (LONG 편향 86% 사태 방지)
        MAX_SAME_DIR = 4
        if self._unified_last_dir == direction:
            if self._unified_same_dir_count >= MAX_SAME_DIR:
                logger.info(
                    f"[TRADE] 같은 방향 연속 {self._unified_same_dir_count}회 → "
                    f"{direction.upper()} 차단 (반대 방향만 허용)"
                )
                return

        # 가격 확인
        price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
        price = float(price_str) if price_str else 0
        if price <= 0:
            return

        # 리스크 체크
        balance = await self.executor.get_balance()
        if balance <= 0:
            return

        # 가상매매 기록
        if not learning:
            await self.paper_trader.try_entry(result, "unified", price)

        # 실거래
        if autotrading:
            await self._execute_unified(result, price, balance)

    async def _execute_unified(self, result: dict, price: float, balance: float):
        """통합 엔진 매매 실행"""
        import time as _t

        direction = result["direction"]
        setup = result["setup"]
        score = result["score"]
        hold_mode = result.get("hold_mode", "standard")

        # hold_mode별 SL/TP 설정
        hm_cfg = self.config.get("hold_modes", {}).get(hold_mode, {})
        sl_margin_pct = hm_cfg.get("sl_margin_pct", 8.0)
        tp1_margin_pct = hm_cfg.get("tp1_margin_pct", 12.0)
        tp2_mult = hm_cfg.get("tp2_mult", 2.5)
        tp3_mult = hm_cfg.get("tp3_mult", 4.0)

        # 레버리지 (동적)
        atr_pct = result.get("atr_pct", 0.3)
        lev_result = self.leverage_calc.calculate("B+", atr_pct, self._unified_streak)
        leverage = lev_result["leverage"]

        # SL/TP 거리 계산
        # 셋업이 자체 SL/TP 제공하면 사용, 아니면 마진% 기준
        sl_dist = result.get("sl_distance", 0)
        tp_dist = result.get("tp_distance", 0)

        if sl_dist <= 0:
            sl_dist = price * (sl_margin_pct / leverage / 100)
        if tp_dist <= 0:
            tp_dist = price * (tp1_margin_pct / leverage / 100)

        # 최소 SL 0.35%
        min_sl = price * 0.0035
        sl_dist = max(sl_dist, min_sl)

        tp1_dist = tp_dist
        tp2_dist = sl_dist * tp2_mult
        tp3_dist = sl_dist * tp3_mult

        if direction == "long":
            sl = price - sl_dist
            tp1 = price + tp1_dist
            tp2 = price + tp2_dist
            tp3 = price + tp3_dist
        else:
            sl = price + sl_dist
            tp1 = price - tp1_dist
            tp2 = price - tp2_dist
            tp3 = price - tp3_dist

        # 수수료 필터
        taker_fee = self.config.get("fees", {}).get("taker", 0.0005)
        fee_cost = taker_fee * 2 * leverage * 100
        tp1_gain = tp1_dist / price * leverage * 100
        if tp1_gain <= fee_cost:
            logger.info(f"[TRADE] TP1({tp1_gain:.1f}%) <= 수수료({fee_cost:.1f}%) → 차단")
            return

        # 마진 계산
        risk_cfg = self.config.get("risk", {})
        margin_pct = risk_cfg.get("margin_pct", 0.30)

        # 연패 사이즈 축소
        streak_sizing = risk_cfg.get("streak_sizing", {})
        size_mult = 1.0
        for threshold, mult in sorted(streak_sizing.items(), key=lambda x: int(x[0]), reverse=True):
            if self._unified_streak >= int(threshold):
                size_mult = mult
                break

        margin = balance * margin_pct * size_mult
        if margin <= 0:
            return

        logger.info(
            f"[TRADE] SETUP {setup} | {direction.upper()} @ ${price:.0f} | "
            f"SL ${sl:.0f} TP1 ${tp1:.0f} TP2 ${tp2:.0f} TP3 ${tp3:.0f} | "
            f"lev {leverage}x | margin ${margin:.1f} | mode={hold_mode} | "
            f"score={score:.1f} | streak={self._unified_streak}"
        )

        # OKX 사이즈 스냅
        raw_size = margin * leverage / price
        size_btc = math.floor(raw_size / 0.01) * 0.01
        size_btc = round(size_btc, 4)

        if size_btc < 0.01:
            logger.info(f"[TRADE] 사이즈 부족 ({size_btc} BTC) → 차단")
            return

        # 셋업 B(OB 리테스트) = 리밋 오더 (maker 0.02%), 나머지 = 마켓
        # executor: grade B+이하 + entry_price 있으면 리밋, 없으면 마켓
        entry_price_limit = round(price, 1) if setup == "B" else None

        trade_req = {
            "symbol": self.symbol, "direction": direction,
            "grade": "B+", "score": score,
            "size": size_btc,
            "leverage": leverage,
            "entry_price": entry_price_limit,
            "sl_price": round(sl, 1),
            "tp1_price": round(tp1, 1),
            "tp2_price": round(tp2, 1),
            "tp3_price": round(tp3, 1),
            "signals_snapshot": result.get("signals", {}),
        }

        pos = await self.position_manager.open_position(trade_req)
        if pos:
            self._unified_last_trade_time = _t.time()
            # 04-17: 같은 방향 연속 카운터
            if self._unified_last_dir == direction:
                self._unified_same_dir_count += 1
            else:
                self._unified_same_dir_count = 1
            self._unified_last_dir = direction
            self._unified_last_entry_price = pos.entry_price

            # trades.jsonl 진입 기록
            try:
                self.trade_logger.log_entry(
                    direction, f"SETUP-{setup}", score,
                    pos.entry_price, pos.sl_price,
                    leverage, margin
                )
            except Exception as e:
                logger.error(f"trade_logger.log_entry 실패: {e}")

            await self.telegram.notify_entry(
                direction, f"SETUP-{setup}", score,
                pos.entry_price, pos.sl_price, pos.tp1_price, pos.tp2_price,
                leverage, margin, tp3_price=pos.tp3_price
            )

    def _unified_record_result(self, pnl_pct: float, exit_reason: str = ""):
        """통합 엔진 매매 결과 기록"""
        import time as _t

        self._unified_daily_pnl += pnl_pct

        if pnl_pct < 0:
            self._unified_streak += 1
        else:
            self._unified_streak = 0

        self._unified_last_exit_reason = exit_reason

        # 조건부 쿨다운
        cooldown_cfg = self.config.get("cooldown", {})
        if pnl_pct < 0:
            cd = cooldown_cfg.get("after_loss_sec", 180)
        else:
            cd = cooldown_cfg.get("after_win_sec", 60)

        self._unified_cooldown_until = _t.time() + cd
        logger.info(
            f"[TRADE] 결과: PnL {pnl_pct:+.2f}% | 연패:{self._unified_streak} | "
            f"쿨다운:{cd}s | 일일:{self._unified_daily_pnl:+.1f}%"
        )

    # ── 주기적 루프들 (레거시) ──

    async def periodic_candle_update(self):
        """캔들 갱신 — Binance 선물 기준 (04-17)
        1m/5m: 2초마다 (Binance rate limit 2400req/min 여유)
        15m/1h: 6초마다 (3사이클)
        """
        _cycle = 0
        while self._running:
            try:
                # 매 사이클: 1m + 5m (Binance 기본, 실패 시 OKX 폴백)
                for tf in ["1m", "5m"]:
                    candles = await self.candle_collector.fetch_candles(tf, limit=5)
                    if candles:
                        await self.db.insert_candles(self.symbol, tf, candles)
                # 3사이클(6초)마다: 15m + 1h
                if _cycle % 3 == 0:
                    for tf in ["15m", "1h"]:
                        candles = await self.candle_collector.fetch_candles(tf, limit=5)
                        if candles:
                            await self.db.insert_candles(self.symbol, tf, candles)
                _cycle += 1
            except Exception as e:
                logger.error(f"캔들 갱신 에러: {e}")
            await asyncio.sleep(2)  # 3초→2초 (Binance는 rate limit 여유)

    async def periodic_signal_eval(self):
        """Swing 시그널 평가 + 매매 (60초마다)"""
        await asyncio.sleep(15)
        while self._running:
            try:
                await self._evaluate_swing()
            except Exception as e:
                logger.error(f"Swing 평가 에러: {e}")
            await asyncio.sleep(60)

    async def periodic_scalp_eval(self):
        """스캘핑 시그널 평가 (3초마다)"""
        await asyncio.sleep(2)
        while self._running:
            try:
                await self._evaluate_scalp()
            except Exception as e:
                logger.error(f"Scalp 평가 에러: {e}")
            await asyncio.sleep(3)  # 5초→3초

    async def periodic_position_check(self):
        """
        포지션 체크 — 실거래 + 가상매매
        - 평상시: 15초 주기
        - 학습 중 (sys:learning=1) + 활성 실거래 포지션: 5초 주기 (SL 본절 이동 지연 최소화)
        - 학습 중에는 paper_trader 폴링 스킵 (CPU 경합 방지, 실거래 보호 우선)
        """
        while self._running:
            # 학습 상태 매 루프 확인 (메모리 fallback 우선)
            try:
                learning = self._learning_local or (await self.redis.get("sys:learning")) == "1"
            except Exception:
                learning = self._learning_local

            try:
                price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
                if price_str:
                    price = float(price_str)

                    # 실거래 포지션 체크 (항상)
                    if self.position_manager.positions:
                        await self.position_manager.check_positions(price)

                    # 가상매매 포지션 체크 — 학습 중엔 스킵 (CPU 절약, 실거래 우선)
                    if self.paper_trader.positions and not learning:
                        await self.paper_trader.check_positions(price)

                # 킬스위치 체크
                bot_status = await self.redis.get("sys:bot_status")
                if bot_status == "stopped":
                    logger.warning("킬스위치 감지 → 전 포지션 청산")
                    await self.position_manager.close_all("kill_switch")
                    await self.telegram.notify_emergency("Kill switch activated")
            except Exception as e:
                logger.error(f"포지션 체크 에러: {e}")

            # 04-15: 포지션 체크 1초 (활성) / 5초 (비활성)
            if self.position_manager.positions:
                await asyncio.sleep(1)
            else:
                await asyncio.sleep(5)

    async def periodic_oi_funding(self):
        """OI/펀딩비 수집 (5분마다)"""
        while self._running:
            try:
                await self.oi_funding.collect_all()
            except Exception as e:
                logger.error(f"OI/Funding 에러: {e}")
            await asyncio.sleep(300)

    async def periodic_daily_reset(self):
        """일일 리셋 (매일 00:00 UTC) — date 비교로 월 변경 안전"""
        last_reset_date = None
        while self._running:
            now = datetime.now(timezone.utc)
            today = now.date()  # date 객체로 비교 (월 변경 안전)
            if last_reset_date is None:
                last_reset_date = today

            if today > last_reset_date:
                last_reset_date = today
                self._current_day = today.day
                await self.risk_manager.reset_daily()

                # TradeEngine 일일 리셋
                logger.info(f"[TRADE] 일일 리셋 | 어제 P&L: {self._unified_daily_pnl:+.1f}%")
                self._unified_daily_pnl = 0.0
                self._unified_streak = 0
                self._unified_cooldown_until = 0
                self._unified_last_dir = None
                self._unified_same_dir_count = 0
                self._loss_warning_sent = False  # 일일 손실 경고 플래그 리셋

                # 일일 리포트 — DB에서 어제 거래 집계 (정확)
                try:
                    import time as _t
                    yesterday_start = int((_t.time() - 86400) * 1000)
                    cursor = await self.db._db.execute(
                        "SELECT COUNT(*), "
                        "SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END), "
                        "SUM(CASE WHEN pnl_pct <= 0 THEN 1 ELSE 0 END), "
                        "SUM(pnl_usdt) "
                        "FROM trades "
                        "WHERE entry_time >= ? AND exit_time IS NOT NULL "
                        "AND grade NOT LIKE 'PAPER_%'",
                        (yesterday_start,)
                    )
                    row = await cursor.fetchone()
                    total_t = (row[0] or 0) if row else 0
                    wins = (row[1] or 0) if row else 0
                    losses = (row[2] or 0) if row else 0
                    total_pnl = (row[3] or 0.0) if row else 0.0
                except Exception as e:
                    logger.debug(f"일일 리포트 DB 집계 실패: {e}")
                    total_t, wins, losses, total_pnl = 0, 0, 0, 0.0

                risk = await self.risk_manager.get_risk_state()
                await self.telegram.notify_daily_report(
                    now.strftime("%Y-%m-%d"),
                    total_t, wins, losses,
                    float(total_pnl),
                    risk.get("balance", 0),
                )

                # ML 모델 주기적 저장
                self.ml_swing.save()
                self.ml_scalp.save()

                # 오래된 가상매매 기록 정리 (30일 이상)
                try:
                    import time as _t
                    cutoff = int((_t.time() - 30 * 86400) * 1000)
                    cursor = await self.db._db.execute(
                        "DELETE FROM trades WHERE entry_time < ? AND grade LIKE 'PAPER_%'",
                        (cutoff,)
                    )
                    await self.db._db.commit()
                    deleted = cursor.rowcount if hasattr(cursor, 'rowcount') else 0
                    logger.info(f"[CLEAN] 30일 이상 가상매매 {deleted}건 삭제")
                except Exception as e:
                    logger.error(f"DB 정리 에러: {e}")

                logger.info("일일 리셋 + ML 저장 + DB 정리 완료")

            await asyncio.sleep(30)

    async def periodic_heartbeat(self):
        """헬스체크 (60초마다) — heartbeat + 잔고 캐시 + 봇 스냅샷 저장"""
        while self._running:
            await self.redis.set("sys:last_heartbeat", str(int(_time.time())))
            bal = 0
            try:
                bal = await asyncio.wait_for(self.executor.get_balance(), timeout=5.0)
                if bal and bal > 0:
                    await self.redis.set("sys:balance", f"{bal:.2f}")
            except asyncio.TimeoutError:
                logger.debug("잔고 캐시 timeout (이전 값 유지)")
            except Exception as e:
                logger.debug(f"잔고 캐시 실패 (이전 값 유지): {e}")

            # 04-17: 봇 상태 스냅샷 — Claude 가 git fetch 로 현재 상태 즉시 확인
            try:
                import json as _j
                positions_snap = {}
                for sym, pos in self.position_manager.positions.items():
                    positions_snap[sym] = pos.to_dict()
                regime = await self.redis.get("sys:regime") or "unknown"
                regime_detail = await self.redis.get_json("sys:regime_detail") or {}
                trade_state = await self.redis.get_json("sys:trade_state") or {}
                autotrading = await self.redis.get("sys:autotrading") or "off"

                snapshot = {
                    "ts": int(_time.time()),
                    "ts_iso": datetime.now(timezone.utc).isoformat(),
                    "balance": round(bal, 2) if bal else 0,
                    "autotrading": autotrading,
                    "regime": regime,
                    "regime_detail": regime_detail,
                    "trade_state": trade_state,
                    "positions": positions_snap,
                    "streak": self._unified_streak,
                    "daily_pnl": round(self._unified_daily_pnl, 2),
                    "same_dir_count": self._unified_same_dir_count,
                    "last_dir": self._unified_last_dir,
                    "pending_algos": [],
                }
                # OKX pending algos 조회 (stale 알고 감지)
                try:
                    inst_id = self.executor.exchange.market(self.symbol)["id"]
                    resp = await self.executor.exchange.private_get_trade_orders_algo_pending(
                        {"instType": "SWAP", "instId": inst_id}
                    )
                    algos = resp.get("data", []) if isinstance(resp, dict) else []
                    snapshot["pending_algos"] = [
                        {"id": a.get("algoClOrdId") or a.get("algoId"),
                         "type": a.get("ordType"), "trigger": a.get("triggerPx"),
                         "side": a.get("side"), "sz": a.get("sz")}
                        for a in algos
                    ]
                except Exception:
                    pass

                snap_path = Path("/app/data/logs/bot_snapshot.json") if Path("/app/data/logs").is_dir() \
                    else Path("data/logs/bot_snapshot.json")
                snap_path.parent.mkdir(parents=True, exist_ok=True)
                with open(snap_path, "w") as f:
                    _j.dump(snapshot, f, indent=2, default=str)
            except Exception as e:
                logger.debug(f"스냅샷 저장 실패: {e}")

            await asyncio.sleep(60)

    async def periodic_orphan_algo_sweeper(self):
        """
        고아 알고 주기 정리 (120초마다).
        봇 메모리 + OKX 양쪽에 포지션 0 인데 알고가 남아있으면 → 전량 취소.
        네트워크 일시 장애로 finalize 정리가 실패했을 때 백스톱 역할.
        """
        while self._running:
            await asyncio.sleep(120)
            try:
                # 봇 메모리에 포지션이 있으면 스킵 (활성 알고는 정상)
                if self.position_manager.positions:
                    continue
                # OKX 실제 포지션 확인
                try:
                    ex_positions = await asyncio.wait_for(
                        self.executor.get_positions(), timeout=5.0
                    )
                except Exception as e:
                    logger.debug(f"sweeper 포지션 조회 실패: {e}")
                    continue
                has_ex_position = any(abs(float(p.get("size") or 0)) > 0 for p in ex_positions)
                if has_ex_position:
                    continue
                # 포지션 완전 0 — 알고 있으면 고아
                try:
                    cleaned = await self.executor.cancel_all_algos()
                    if cleaned:
                        logger.warning(
                            f"🧹 고아 알고 {len(cleaned)}개 발견 + 정리 (포지션 없음, sweeper)"
                        )
                except Exception as e:
                    logger.debug(f"sweeper 정리 실패: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"sweeper 루프 에러: {e}")

    async def periodic_dashboard_commands(self):
        """대시보드(별도 컨테이너)가 Redis 큐로 보낸 명령 처리 — BLPOP 5초 블로킹"""
        import json as _json
        while self._running:
            try:
                if not self.redis._client:
                    await asyncio.sleep(5)
                    continue
                raw = await self.redis._client.blpop("cmd:bot", timeout=5)
                if not raw:
                    continue
                _, payload = raw
                cmd = _json.loads(payload)
                action = cmd.get("action")
                logger.info(f"[DASH-CMD] {action}: {cmd}")

                if action == "close_all":
                    try:
                        await self.position_manager.close_all(cmd.get("reason", "dashboard"))
                    except Exception as e:
                        logger.error(f"[DASH-CMD] close_all 실패: {e}")
                elif action == "close":
                    try:
                        direction = cmd["direction"]
                        close_pct = float(cmd.get("close_pct", 1.0))
                        positions = await self.executor.get_positions()
                        target = next((p for p in positions if p["direction"] == direction), None)
                        if target:
                            await self.executor.close_position(
                                direction, target["size"] * close_pct, "dashboard_manual"
                            )
                    except Exception as e:
                        logger.error(f"[DASH-CMD] close 실패: {e}")
                elif action == "update_sl":
                    try:
                        result = await self.position_manager.manual_update_sl(
                            cmd["symbol"], float(cmd["price"])
                        )
                        logger.info(f"[DASH-CMD] update_sl → {result}")
                    except Exception as e:
                        logger.error(f"[DASH-CMD] update_sl 실패: {e}")
                elif action == "update_tp":
                    try:
                        result = await self.position_manager.manual_update_tp(
                            cmd["symbol"], float(cmd["price"])
                        )
                        logger.info(f"[DASH-CMD] update_tp → {result}")
                    except Exception as e:
                        logger.error(f"[DASH-CMD] update_tp 실패: {e}")
                elif action == "notify":
                    try:
                        await self.telegram._send(cmd.get("msg", ""))
                    except Exception as e:
                        logger.debug(f"[DASH-CMD] notify 실패: {e}")
                elif action == "open":
                    # 수동 진입은 복잡 — 로깅만, 필요 시 추후 구현
                    logger.warning(f"[DASH-CMD] open 액션 미구현 (Telegram 사용 권장): {cmd}")
                else:
                    logger.warning(f"[DASH-CMD] 알 수 없는 action: {action}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[DASH-CMD] 루프 에러: {e}")
                await asyncio.sleep(2)

    # ── 대시보드 서버 ──

    def start_dashboard_thread(self):
        """uvicorn 대시보드를 별도 프로세스로 실행 (이벤트 루프 충돌 근절)"""
        import subprocess
        import os

        env = os.environ.copy()
        env["PYTHONPATH"] = "/app"

        # uvicorn을 별도 프로세스로 직접 실행 — asyncio 루프 완전 격리
        log_path = "/app/data/logs/dashboard.log" if os.path.isdir("/app/data/logs") else None
        stderr_target = open(log_path, "w") if log_path else subprocess.DEVNULL
        proc = subprocess.Popen(
            ["python", "-m", "uvicorn", "src.monitoring.dashboard:app",
             "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"],
            env=env,
            stdout=stderr_target,
            stderr=stderr_target,
        )
        self._dashboard_proc = proc
        logger.info(f"대시보드 시작 (별도 프로세스 PID={proc.pid}): http://localhost:8000")

    # ── 메인 ──

    async def run(self):
        await self.initialize()
        self._running = True
        self._current_day = datetime.now(timezone.utc).day

        logger.info("봇 시작 — TradeEngine v1 (Setup ABC)")
        # 대시보드는 별도 컨테이너(docker-compose dashboard 서비스)에서 실행
        # self.start_dashboard_thread()
        await self.redis.set("sys:bot_status", "running")
        await self.redis.set("sys:autotrading", "on")  # 초기 ON (텔레그램 /off 로 끄기)
        await self.redis.set("sys:ml_enabled", "on")
        # 사용자 의도: 스캘핑 중점
        await self.redis.set("sys:active_model", "both")

        # 텔레그램 명령어 처리용 주입 (양방향 통신)
        self.telegram.redis = self.redis
        self.telegram.executor = self.executor
        self.telegram.position_manager = self.position_manager
        self.telegram.risk_manager = self.risk_manager

        await self.telegram.notify_bot_status("running")
        try:
            bal = await self.executor.get_balance()
            await self.telegram._send(
                "\U0001f7e2 <b>TradeEngine v1</b>\n"
                "Mode: Setup ABC (Trend+OB+Breakout)\n"
                "ML: Cold Start (Paper Only)\n"
                f"Balance: ${bal:,.2f}"
            )
        except Exception:
            await self.telegram._send("\U0001f7e2 <b>TradeEngine v1 Started</b>")

        tasks = [
            asyncio.create_task(self.periodic_candle_update()),
            # 레거시 스윙/스캘핑 루프 비활성 — 통합 엔진으로 대체
            # asyncio.create_task(self.periodic_signal_eval()),
            # asyncio.create_task(self.periodic_scalp_eval()),
            asyncio.create_task(self.periodic_unified_eval()),  # 통합 엔진
            asyncio.create_task(self.periodic_position_check()),
            asyncio.create_task(self.periodic_oi_funding()),
            asyncio.create_task(self.periodic_daily_reset()),
            # asyncio.create_task(self.periodic_study_scheduler()),  # ML 콜드스타트 — 학습 비활성
            asyncio.create_task(self.periodic_heartbeat()),
            asyncio.create_task(self.periodic_orphan_algo_sweeper()),  # 고아 알고 주기 정리 (120s)
            asyncio.create_task(self.periodic_dashboard_commands()),  # 대시보드 → bot 명령 큐
            asyncio.create_task(self.ws_stream.start()),
            asyncio.create_task(self.binance_stream.start()),  # Binance CVD + 대형체결
            asyncio.create_task(self.telegram.poll_commands()),  # 텔레그램 명령어 polling
        ]

        try:
            # return_exceptions=True: 한 태스크 죽어도 다른 태스크 계속 실행
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, r in enumerate(results):
                if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                    logger.error(f"태스크 {i} 종료: {r}", exc_info=r)
        except asyncio.CancelledError:
            logger.info("봇 종료 중...")
        finally:
            self._running = False
            self.ws_stream.stop()
            self.binance_stream.stop()
            self.setup_tracker.save()
            self.signal_tracker.save()
            await self.redis.set("sys:bot_status", "stopped")
            await self.telegram.notify_bot_status("stopped")
            await self.cleanup()

    async def cleanup(self):
        # Graceful Shutdown 강화
        logger.info("=== Graceful Shutdown 시작 ===")

        # 대시보드 프로세스 종료
        if hasattr(self, '_dashboard_proc') and self._dashboard_proc:
            try:
                self._dashboard_proc.terminate()
                self._dashboard_proc.wait(timeout=5)
            except Exception:
                try:
                    self._dashboard_proc.kill()
                except Exception:
                    pass

        # 1) ML 모델 + 시그널 트래커 저장 (가장 먼저, 데이터 손실 방지)
        try:
            self.ml_swing.save()
            self.ml_scalp.save()
            self.signal_tracker.save()
            logger.info("ML 모델 + SignalTracker 저장 완료")
        except Exception as e:
            logger.error(f"종료 시 저장 실패: {e}")

        # 2) 진행 중 포지션 로그 (사용자가 거래소에서 직접 처리)
        try:
            for symbol, pos in list(self.position_manager.positions.items()):
                logger.warning(
                    f"종료 시 미청산 포지션: {symbol} {pos.direction.upper()} "
                    f"@ ${pos.entry_price} (거래소에서 수동 처리 필요)"
                )
        except Exception:
            pass

        # 3) 리소스 정리
        await self.candle_collector.close()
        await self.oi_funding.close()
        await self.executor.close()
        await self.redis.close()
        await self.db.close()
        logger.info("=== Graceful Shutdown 완료 ===")


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
