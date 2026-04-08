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
from src.strategy.meta_learner import MetaLearner
from src.strategy.signal_tracker import SignalTracker
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

        # 포지션 동기화 + ML 콜백 연결 + 청산 알림 주입
        self.position_manager.on_trade_closed = self.record_ml_trade
        self.position_manager.telegram = self.telegram
        self.position_manager.trade_logger = self.trade_logger
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

            # 가상매매 전수 학습 (학습 중엔 스킵 — CPU 절약 + 실거래 보호 우선)
            price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
            learning = (await self.redis.get("sys:learning")) == "1"
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

        # 가상매매 전수 학습 (학습 중엔 스킵)
        if (await self.redis.get("sys:learning")) != "1":
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
        # 🔒 학습 중에는 신규 진입 스킵 (asyncio 블로킹 race 방지)
        if await self.redis.get("sys:learning") == "1":
            logger.info("[SWING] 학습 중 → 신규 진입 스킵")
            return

        direction = grade_result["direction"]
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
        raw_size = margin * lev["leverage"] / price
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
        # 🔒 학습 중에는 신규 진입 스킵
        if await self.redis.get("sys:learning") == "1":
            logger.info("[SCALP] 학습 중 → 신규 진입 스킵")
            return

        direction = scalp_sig["direction"]
        if direction == "neutral":
            return

        price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
        price = float(price_str) if price_str else 0
        if price <= 0:
            return

        balance = risk_state.get("balance", 0)
        sl_dist_indicator = scalp_sig["sl_distance"]
        leverage = 25
        if balance <= 0 or sl_dist_indicator <= 0 or price <= 0:
            return

        risk_cfg = self.config["risk"]
        sizing_mode = risk_cfg.get("sizing_mode", "margin_loss_cap")

        if sizing_mode == "margin_loss_cap":
            max_loss_pct = risk_cfg.get("max_margin_loss_pct", 10.0)
            tp1_gain_pct = risk_cfg.get("tp1_margin_gain_pct", 15.0)
            margin_pct = risk_cfg.get("margin_pct", 0.95)
            use_indicator = risk_cfg.get("use_indicator_sl", True)
            min_ind_pct = risk_cfg.get("min_indicator_sl_price_pct", 0.05)

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
            f"TP1 ${tp1:.0f} TP2 ${tp2:.0f} TP3 ${tp3:.0f}"
        )

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
        """실거래 결과 → ML 학습 + 시그널 기여도 추적"""
        ml = self.ml_swing if mode == "swing" else self.ml_scalp
        regime = self._current_regime["regime"] if self._current_regime else "ranging"
        meta = {"atr_pct": self._last_fast.get("atr", {}).get("atr_pct", 0.3),
                "hour": datetime.now(timezone.utc).hour,
                "regime": regime}
        ml.record_trade(signals, meta, pnl_pct)

        # 시그널 기여도 추적
        if self.signal_tracker:
            self.signal_tracker.record_trade(signals, pnl_pct, mode=mode, regime=regime)

        logger.info(f"[실거래→ML] {mode} PnL {pnl_pct:+.2f}% 레짐:{regime} 학습 완료")

    # ── 역사 학습 ──

    async def _initial_history_learn(self):
        """봇 시작 시 과거 데이터 학습 (백그라운드)"""
        await asyncio.sleep(30)  # 캔들 수집 완료 대기
        try:
            await self.redis.set("sys:learning", "1", ttl=3600)
            logger.info("[HIST] 🔒 초기 역사 백필 학습 시작 (Swing) — 신규 진입 일시 정지")
            await self.hist_learner.run_backfill("15m", lookback=2000, step=5)

            logger.info("[HIST] 초기 역사 백필 학습 시작 (Scalp)...")
            await self.hist_learner.run_scalp_backfill(lookback=2000, step=5)

            await asyncio.to_thread(self.ml_swing.save)
            await asyncio.to_thread(self.ml_scalp.save)
            logger.info("[HIST] 초기 학습 완료 (Swing + Scalp)")
        except Exception as e:
            logger.error(f"[HIST] 초기 학습 에러: {e}")
        finally:
            await self.redis.delete("sys:learning")
            logger.info("[HIST] 🔓 초기 학습 종료 — 매매 재개")

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
            """학습 작업을 redis flag 로 감싸서 실행 — 중간 예외도 안전하게 unset"""
            try:
                await self.redis.set("sys:learning", "1", ttl=3600)
                logger.info(f"[SCHED] 🔒 학습 모드 ON ({label}) — 신규 진입 일시 정지")
                await coro
            finally:
                await self.redis.delete("sys:learning")
                logger.info(f"[SCHED] 🔓 학습 모드 OFF ({label}) — 매매 재개")

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

    # ── 주기적 루프들 ──

    async def periodic_candle_update(self):
        """캔들 갱신 (10초마다, 모든 TF) — 스캘핑 빠른 반응"""
        while self._running:
            try:
                # 1m/5m은 자주, 큰 TF는 덜 자주
                for tf in ["1m", "5m", "15m", "1h"]:
                    candles = await self.candle_collector.fetch_candles(tf, limit=5)
                    if candles:
                        await self.db.insert_candles(self.symbol, tf, candles)
            except Exception as e:
                logger.error(f"캔들 갱신 에러: {e}")
            await asyncio.sleep(10)

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
        """스캘핑 시그널 평가 (5초마다) — 초고속 반응"""
        await asyncio.sleep(20)
        while self._running:
            try:
                await self._evaluate_scalp()
            except Exception as e:
                logger.error(f"Scalp 평가 에러: {e}")
            await asyncio.sleep(5)

    async def periodic_position_check(self):
        """
        포지션 체크 — 실거래 + 가상매매
        - 평상시: 15초 주기
        - 학습 중 (sys:learning=1) + 활성 실거래 포지션: 5초 주기 (SL 본절 이동 지연 최소화)
        - 학습 중에는 paper_trader 폴링 스킵 (CPU 경합 방지, 실거래 보호 우선)
        """
        while self._running:
            # 학습 상태 매 루프 확인
            try:
                learning = (await self.redis.get("sys:learning")) == "1"
            except Exception:
                learning = False

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

            # 학습 중 + 활성 실거래 포지션 → 5초 폴링
            if learning and self.position_manager.positions:
                await asyncio.sleep(5)
            else:
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
        """헬스체크 (60초마다) — heartbeat + 잔고 캐시"""
        while self._running:
            await self.redis.set("sys:last_heartbeat", str(int(_time.time())))
            try:
                bal = await self.executor.get_balance()
                if bal and bal > 0:
                    await self.redis.set("sys:balance", f"{bal:.2f}")
            except Exception as e:
                logger.warning(f"잔고 캐시 실패: {e}")
            await asyncio.sleep(60)

    # ── 대시보드 서버 ──

    def start_dashboard_thread(self):
        """uvicorn 대시보드를 별도 스레드로 실행 (메인 루프 블로킹 방지)"""
        import uvicorn
        import threading
        import src.monitoring.dashboard as dash_module
        from src.monitoring.dashboard import app

        # 다른 스레드에서 PositionManager 호출용 — 이벤트 루프 + 인스턴스 주입
        dash_module.position_manager = self.position_manager
        dash_module.executor = self.executor
        dash_module.main_event_loop = asyncio.get_running_loop()

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
        # 사용자 의도: 스캘핑 중점
        await self.redis.set("sys:active_model", "scalp")
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
            self.ml_swing.save()
            self.ml_scalp.save()
            await self.redis.set("sys:bot_status", "stopped")
            await self.telegram.notify_bot_status("stopped")
            await self.cleanup()

    async def cleanup(self):
        # Graceful Shutdown 강화
        logger.info("=== Graceful Shutdown 시작 ===")

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
