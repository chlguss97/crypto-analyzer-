import asyncio
import logging
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
from src.trading.leverage import LeverageCalculator
from src.trading.risk_manager import RiskManager
from src.trading.executor import OrderExecutor
from src.trading.position_manager import PositionManager
from src.monitoring.telegram_bot import TelegramNotifier
from src.monitoring.trade_logger import TradeLogger
from src.strategy.paper_trader import PaperTrader
from src.strategy.historical_learner import HistoricalLearner
from src.strategy.auto_backtest import AutoBacktest
from src.engine.regime_detector import MarketRegimeDetector
from src.trading.news_filter import NewsFilter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("CryptoAnalyzer")


class CryptoAnalyzer:
    """메인 봇 — Swing + Scalp 듀얼 모델 + AdaptiveML"""

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

        # Scalp 엔진 (1m/5m)
        self.scalp_engine = ScalpEngine()

        # AdaptiveML (듀얼)
        self.ml_swing = AdaptiveML(mode="swing")
        self.ml_scalp = AdaptiveML(mode="scalp")

        # 매매 엔진
        self.leverage_calc = LeverageCalculator()
        self.risk_manager = RiskManager(self.redis)
        self.executor = OrderExecutor()
        self.position_manager = PositionManager(self.executor, self.db, self.redis)

        # 모니터링
        self.telegram = TelegramNotifier()
        self.trade_logger = TradeLogger()

        # 마켓 레짐 감지
        self.regime_detector = MarketRegimeDetector()
        self._current_regime = None

        # 뉴스 필터
        self.news_filter = NewsFilter()

        # 가상매매 엔진 (ML 학습용)
        self.paper_trader = PaperTrader(self.db, self.redis, self.ml_swing, self.ml_scalp, self.regime_detector)

        # 역사 백필 학습 엔진 (candle_collector 연결 → 90일 수집 가능)
        self.hist_learner = HistoricalLearner(
            self.db, self.ml_swing, self.ml_scalp, self.candle_collector
        )

        # 자동 백테스트
        self.auto_backtest = AutoBacktest(self.db, self.ml_swing, self.ml_scalp)
        self._last_backtest = None

        # 스캘핑 리스크 관리
        self._scalp_daily_pnl = 0.0         # 일일 스캘핑 P&L (%)
        self._scalp_streak = 0               # 연패 카운터
        self._scalp_cooldown_until = 0       # 쿨다운 종료 시각 (timestamp)
        self._scalp_pending_signal = None    # 진입 확인 대기 시그널
        self._scalp_pending_price = 0.0

        # 캐시
        self._last_fast = {}
        self._last_slow = {}
        self._last_scalp = {}
        self._current_day = 0
        self._running = False

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

        # ML 모델 로드
        self.ml_swing.load()
        self.ml_scalp.load()
        logger.info(f"ML Swing: {'TRAINED' if self.ml_swing.is_trained else 'LEARNING'} | "
                     f"Scalp: {'TRAINED' if self.ml_scalp.is_trained else 'LEARNING'}")

        # 잔고 + 리스크
        balance = await self.executor.get_balance()
        await self.risk_manager.initialize(balance)
        logger.info(f"계좌 잔고: ${balance:.2f}")

        # 포지션 동기화 + ML 콜백 연결
        self.position_manager.on_trade_closed = self.record_ml_trade
        await self.position_manager.sync_positions()

        # 캔들 백필 (15m, 1h, 5m, 1m)
        logger.info("캔들 백필 시작...")
        await self.candle_collector.backfill_all()
        for tf in ["5m", "1m"]:
            await self.candle_collector.backfill(tf, days=7)
        logger.info("캔들 백필 완료")

        # 역사 백필은 스케줄러(하루 3회)에서 실행 — 시작 시 대시보드 응답 보장
        logger.info("ML 학습은 스케줄러에서 실행됩니다 (UTC 02/10/18)")

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

        # 레짐 감지
        regime_result = self.regime_detector.detect(df)
        self._current_regime = regime_result
        await self.redis.set("sys:regime", regime_result["regime"], ttl=300)
        await self.redis.set("sys:regime_detail", regime_result, ttl=300)

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

        result = await self.scalp_engine.analyze(df_1m, df_5m, df_15m)
        self._last_scalp = result

        # ML 조정
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

            # 가상매매 전수 학습
            price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
            if price_str:
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
        """스캘핑 시그널 평가 + 자동매매 (15초 주기)"""
        import time as _t

        # 일일 손실 한도 체크 (-10%)
        if self._scalp_daily_pnl <= -10.0:
            return

        # 쿨다운 체크
        if _t.time() < self._scalp_cooldown_until:
            return

        scalp_sig = await self.run_scalp_signal()
        if not scalp_sig:
            return

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

        # 가상매매 전수 학습 (항상)
        await self.paper_trader.try_entry(scalp_sig, "scalp", current_price)

        # ── 진입 확인 대기 로직 ──
        # 1차: 시그널 발생 → 대기 상태로 저장
        # 2차 (15초 후): 가격이 같은 방향이면 진입 확정
        if self._scalp_pending_signal:
            pending = self._scalp_pending_signal
            # 방향 확인: 15초 전 시그널 방향과 현재 가격 비교
            confirmed = False
            if pending["direction"] == "long" and current_price > self._scalp_pending_price:
                confirmed = True
            elif pending["direction"] == "short" and current_price < self._scalp_pending_price:
                confirmed = True

            if confirmed:
                # 진입 확정
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
        """스캘핑 결과 기록 → 일일 P&L + 연패 관리"""
        import time as _t
        self._scalp_daily_pnl += pnl_pct

        if pnl_pct <= 0:
            self._scalp_streak += 1
            # 쿨다운 설정
            if self._scalp_streak >= 5:
                self._scalp_cooldown_until = _t.time() + 1800  # 30분
                logger.warning(f"[SCALP] 5연패 → 30분 쿨다운")
            elif self._scalp_streak >= 3:
                self._scalp_cooldown_until = _t.time() + 300  # 5분
                logger.warning(f"[SCALP] 3연패 → 5분 쿨다운")
        else:
            self._scalp_streak = 0

        if self._scalp_daily_pnl <= -10.0:
            logger.warning(f"[SCALP] 일일 손실 한도 도달 ({self._scalp_daily_pnl:.1f}%) → 스캘핑 중단")

    async def _execute_swing(self, grade_result, aggregated, risk_state):
        """Swing 매매 실행"""
        direction = grade_result["direction"]
        atr_pct = self._last_fast.get("atr", {}).get("atr_pct", 0.3)
        lev = self.leverage_calc.calculate(grade_result["grade"], atr_pct, risk_state.get("streak", 0))
        balance = risk_state.get("balance", 0)
        margin = self.leverage_calc.calculate_position_size(
            balance, lev["leverage"], lev["sl_pct"], grade_result["size_pct"])
        if margin <= 0:
            return

        price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
        price = float(price_str) if price_str else 0
        if price <= 0:
            return

        sl_dist = self._last_fast.get("atr", {}).get("sl_distance", price * 0.003)
        tp1_dist = sl_dist * 1.5
        tp2_dist = sl_dist * 2.5

        if direction == "long":
            sl, tp1, tp2 = price - sl_dist, price + tp1_dist, price + tp2_dist
        else:
            sl, tp1, tp2 = price + sl_dist, price - tp1_dist, price - tp2_dist

        logger.info(f"[SWING] {grade_result['grade']} {direction.upper()} @ ${price:.0f} SL ${sl:.0f}")

        trade_req = {
            "symbol": self.symbol, "direction": direction,
            "grade": grade_result["grade"], "score": grade_result["score"],
            "size": round(margin * lev["leverage"] / price, 6),
            "leverage": lev["leverage"],
            "entry_price": None if grade_result["execution"] == "market" else price,
            "sl_price": round(sl, 1), "tp1_price": round(tp1, 1), "tp2_price": round(tp2, 1),
            "signals_snapshot": aggregated.get("signals_detail", {}),
        }
        pos = await self.position_manager.open_position(trade_req)
        if pos:
            await self.telegram.notify_entry(direction, grade_result["grade"], grade_result["score"],
                                             pos.entry_price, pos.sl_price, pos.tp1_price, pos.tp2_price,
                                             lev["leverage"], margin)
            self.trade_logger.log_entry(direction, grade_result["grade"], grade_result["score"],
                                        pos.entry_price, pos.sl_price, lev["leverage"], margin)

    async def _execute_scalp(self, scalp_sig, risk_state):
        """Scalp 매매 실행"""
        direction = scalp_sig["direction"]
        if direction == "neutral":
            return

        price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
        price = float(price_str) if price_str else 0
        if price <= 0:
            return

        balance = risk_state.get("balance", 0)
        sl_dist = scalp_sig["sl_distance"]
        leverage = 25
        margin = balance * 0.008 / (sl_dist / price)

        if direction == "long":
            sl = price - sl_dist
        else:
            sl = price + sl_dist

        tp1 = price + scalp_sig["tp_distance"] if direction == "long" else price - scalp_sig["tp_distance"]

        logger.info(f"[SCALP] {direction.upper()} @ ${price:.0f} SL ${sl:.0f}")

        trade_req = {
            "symbol": self.symbol, "direction": direction,
            "grade": "SCALP", "score": scalp_sig["score"],
            "size": round(margin * leverage / price, 6),
            "leverage": leverage, "entry_price": None,
            "sl_price": round(sl, 1), "tp1_price": round(tp1, 1), "tp2_price": round(tp1, 1),
            "signals_snapshot": scalp_sig.get("signals", {}),
        }
        pos = await self.position_manager.open_position(trade_req)
        if pos:
            await self.telegram.notify_entry(direction, "SCALP", scalp_sig["score"],
                                             pos.entry_price, pos.sl_price, pos.tp1_price, pos.tp2_price,
                                             leverage, margin)

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
        ctx["funding_history"] = oi_history
        ls_val = await self.redis.get("rt:ls_ratio:BTC-USDT-SWAP")
        ctx["ls_ratio_account"] = float(ls_val) if ls_val else 1.0
        ctx["ls_history"] = oi_history
        cvd_15m = await self.redis.get("cvd:15m:BTC-USDT-SWAP")
        cvd_1h = await self.redis.get("cvd:1h:BTC-USDT-SWAP")
        ctx["cvd_15m"] = float(cvd_15m) if cvd_15m else 0
        ctx["cvd_1h"] = float(cvd_1h) if cvd_1h else 0
        return ctx

    # ── ML 학습 기록 ──

    async def record_ml_trade(self, mode: str, signals: dict, pnl_pct: float):
        """실거래 결과 → ML 학습 (레짐 정보 포함)"""
        ml = self.ml_swing if mode == "swing" else self.ml_scalp
        regime = self._current_regime["regime"] if self._current_regime else "ranging"
        meta = {"atr_pct": self._last_fast.get("atr", {}).get("atr_pct", 0.3),
                "hour": datetime.now(timezone.utc).hour,
                "regime": regime}
        ml.record_trade(signals, meta, pnl_pct)
        logger.info(f"[실거래→ML] {mode} PnL {pnl_pct:+.2f}% 레짐:{regime} 학습 완료")

    # ── 역사 학습 ──

    async def _initial_history_learn(self):
        """봇 시작 시 과거 데이터 학습 (백그라운드)"""
        await asyncio.sleep(30)  # 캔들 수집 완료 대기
        try:
            logger.info("[HIST] 초기 역사 백필 학습 시작 (Swing)...")
            await self.hist_learner.run_backfill("15m", lookback=2000, step=5)

            logger.info("[HIST] 초기 역사 백필 학습 시작 (Scalp)...")
            await self.hist_learner.run_scalp_backfill(lookback=2000, step=5)

            self.ml_swing.save()
            self.ml_scalp.save()
            logger.info("[HIST] 초기 학습 완료 (Swing + Scalp)")
        except Exception as e:
            logger.error(f"[HIST] 초기 학습 에러: {e}")

    async def periodic_study_scheduler(self):
        """
        하루 3회 학습 스케줄러
        - UTC 02:00 (한국 11:00) → 일일 대량 학습 (90일 수집 + 파라미터 다양화 + 레짐 집중)
        - UTC 10:00 (한국 19:00) → 세션 경량 학습
        - UTC 18:00 (한국 03:00) → 세션 경량 학습
        """
        _last_run = {}
        while self._running:
            try:
                now = datetime.now(timezone.utc)
                hour = now.hour
                today = now.strftime("%Y-%m-%d")

                # UTC 02:00 — 일일 대량 학습 + 백테스트
                if hour == 2 and _last_run.get("daily") != today:
                    logger.info("[SCHED] 일일 대량 학습 시작 (UTC 02:00)")
                    await self.hist_learner.run_daily_study()

                    # 학습 후 백테스트
                    logger.info("[SCHED] 자동 백테스트 시작")
                    bt = await self.auto_backtest.run(days=30)
                    self._last_backtest = bt
                    await self.redis.set("sys:last_backtest", bt, ttl=86400)

                    _last_run["daily"] = today

                # UTC 10:00 — 세션 경량 학습
                elif hour == 10 and _last_run.get("session1") != today:
                    logger.info("[SCHED] 세션 경량 학습 1 시작 (UTC 10:00)")
                    await self.hist_learner.run_session_study()
                    _last_run["session1"] = today

                # UTC 18:00 — 세션 경량 학습
                elif hour == 18 and _last_run.get("session2") != today:
                    logger.info("[SCHED] 세션 경량 학습 2 시작 (UTC 18:00)")
                    await self.hist_learner.run_session_study()
                    _last_run["session2"] = today

            except Exception as e:
                logger.error(f"[SCHED] 학습 스케줄러 에러: {e}")
            await asyncio.sleep(60)

    # ── 주기적 루프들 ──

    async def periodic_candle_update(self):
        """캔들 갱신 (30초마다, 모든 TF)"""
        while self._running:
            try:
                for tf in ["1m", "5m", "15m", "1h"]:
                    candles = await self.candle_collector.fetch_candles(tf, limit=5)
                    if candles:
                        await self.db.insert_candles(self.symbol, tf, candles)
            except Exception as e:
                logger.error(f"캔들 갱신 에러: {e}")
            await asyncio.sleep(30)

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
        """스캘핑 시그널 평가 (15초마다) — 빠른 진입/탈출"""
        await asyncio.sleep(20)
        while self._running:
            try:
                await self._evaluate_scalp()
            except Exception as e:
                logger.error(f"Scalp 평가 에러: {e}")
            await asyncio.sleep(15)

    async def periodic_position_check(self):
        """포지션 체크 (15초마다) — 실거래 + 가상매매"""
        while self._running:
            try:
                price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
                if price_str:
                    price = float(price_str)

                    # 실거래 포지션 체크
                    if self.position_manager.positions:
                        await self.position_manager.check_positions(price)

                    # 가상매매 포지션 체크
                    if self.paper_trader.positions:
                        await self.paper_trader.check_positions(price)

                # 킬스위치 체크
                bot_status = await self.redis.get("sys:bot_status")
                if bot_status == "stopped":
                    logger.warning("킬스위치 감지 → 전 포지션 청산")
                    await self.position_manager.close_all("kill_switch")
                    await self.telegram.notify_emergency("Kill switch activated")
            except Exception as e:
                logger.error(f"포지션 체크 에러: {e}")
            await asyncio.sleep(15)

    async def periodic_oi_funding(self):
        """OI/펀딩비 수집 (5분마다)"""
        while self._running:
            try:
                await self.oi_funding.collect_all()
            except Exception as e:
                logger.error(f"OI/Funding 에러: {e}")
            await asyncio.sleep(300)

    async def periodic_daily_reset(self):
        """일일 리셋 (매일 00:00 UTC)"""
        while self._running:
            now = datetime.now(timezone.utc)
            day = now.day
            if day != self._current_day:
                self._current_day = day
                await self.risk_manager.reset_daily()

                # 스캘핑 일일 리셋
                logger.info(f"[SCALP] 일일 리셋 | 어제 P&L: {self._scalp_daily_pnl:+.1f}%")
                self._scalp_daily_pnl = 0.0
                self._scalp_streak = 0
                self._scalp_cooldown_until = 0

                # 일일 리포트
                risk = await self.risk_manager.get_risk_state()
                await self.telegram.notify_daily_report(
                    now.strftime("%Y-%m-%d"),
                    risk.get("trade_count_today", 0),
                    0, 0,  # wins/losses는 DB에서 계산 필요
                    risk.get("daily_pnl_pct", 0) * risk.get("balance", 0) / 100,
                    risk.get("balance", 0),
                )

                # ML 모델 주기적 저장
                self.ml_swing.save()
                self.ml_scalp.save()
                logger.info("일일 리셋 + ML 저장 완료")

            await asyncio.sleep(60)

    async def periodic_heartbeat(self):
        """헬스체크 (60초마다)"""
        while self._running:
            await self.redis.set("sys:last_heartbeat", str(int(_time.time())))
            await asyncio.sleep(60)

    # ── 대시보드 서버 ──

    def start_dashboard_thread(self):
        """uvicorn 대시보드를 별도 스레드로 실행 (메인 루프 블로킹 방지)"""
        import uvicorn
        import threading
        from src.monitoring.dashboard import app

        def _run():
            config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="warning")
            server = uvicorn.Server(config)
            server.run()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        logger.info("대시보드 시작 (별도 스레드): http://localhost:8000")

    # ── 메인 ──

    async def run(self):
        await self.initialize()
        self._running = True
        self._current_day = datetime.now(timezone.utc).day

        logger.info("봇 시작 — Swing + Scalp 듀얼 모델 + AdaptiveML + PaperTrading")
        self.start_dashboard_thread()  # 대시보드 별도 스레드
        await self.redis.set("sys:bot_status", "running")
        await self.redis.set("sys:autotrading", "off")  # 초기 OFF (웹에서 켜기)
        await self.redis.set("sys:ml_enabled", "on")
        await self.redis.set("sys:active_model", "both")
        await self.telegram.notify_bot_status("running")

        tasks = [
            asyncio.create_task(self.periodic_candle_update()),
            asyncio.create_task(self.periodic_signal_eval()),
            asyncio.create_task(self.periodic_scalp_eval()),
            asyncio.create_task(self.periodic_position_check()),
            asyncio.create_task(self.periodic_oi_funding()),
            asyncio.create_task(self.periodic_daily_reset()),
            asyncio.create_task(self.periodic_study_scheduler()),
            asyncio.create_task(self.periodic_heartbeat()),
            asyncio.create_task(self.ws_stream.start()),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("봇 종료 중...")
        finally:
            self._running = False
            self.ws_stream.stop()
            self.ml_swing.save()
            self.ml_scalp.save()
            await self.redis.set("sys:bot_status", "stopped")
            await self.telegram.notify_bot_status("stopped")
            await self.cleanup()

    async def cleanup(self):
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
