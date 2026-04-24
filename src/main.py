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
from src.engine.base import BaseIndicator  # to_dataframe 유틸만 사용

# ── FlowEngine (현재 활성) ──
from src.strategy.flow_engine import FlowEngine
from src.strategy.flow_ml import FlowML
from src.trading.leverage import LeverageCalculator
from src.trading.risk_manager import RiskManager
from src.trading.executor import OrderExecutor
from src.trading.position_manager import PositionManager
from src.monitoring.telegram_bot import TelegramNotifier
from src.monitoring.trade_logger import TradeLogger
from src.strategy.signal_tracker import SignalTracker
from src.strategy.setup_tracker import SetupTracker
from src.engine.regime_detector import MarketRegimeDetector
from src.trading.news_filter import NewsFilter
from src.strategy.paper_trader import PaperTrader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("CryptoAnalyzer")


class CryptoAnalyzer:
    """메인 봇 — FlowEngine v1 (오더플로우 + 레벨) + PaperTrader v2"""

    def __init__(self):
        load_env()
        self.config = load_config()
        self.symbol = self.config["exchange"]["symbol"]

        # 인프라
        self.db = Database()
        self.redis = RedisClient()
        self.candle_collector = CandleCollector(self.db)
        self.ws_stream = WebSocketStream(self.redis)
        self.binance_stream = BinanceStream(self.redis, db=self.db)
        self.oi_funding = OIFundingCollector(self.db, self.redis)

        # ══════ FlowEngine (현재 활성) ══════
        self.flow_ml = FlowML()
        self.trade_engine = FlowEngine(redis=self.redis, flow_ml=self.flow_ml)

        # 통합 모델 상태
        self._unified_streak = 0
        self._unified_daily_pnl = 0.0
        self._unified_cooldown_until = 0
        self._unified_last_dir = None
        self._unified_last_trade_time = 0
        self._unified_last_exit_reason = None
        self._unified_last_entry_price = 0.0
        self._unified_same_dir_count = 0

        # 매매 엔진
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

        # 시그널 기여도 추적
        self.signal_tracker = SignalTracker()

        # 셋업 성과 추적
        self.setup_tracker = SetupTracker()

        # 페이퍼 트레이더
        self.paper_trader = None  # initialize()에서 생성

        # 캐시
        self._current_day = 0
        self._running = False
        self._learning_local = False
        self._last_flow_result = None

    async def initialize(self):
        logger.info("=" * 50)
        logger.info("CryptoAnalyzer — FlowEngine v1 + PaperTrader v2")
        logger.info("=" * 50)

        await self.db.connect()
        await self.redis.connect()
        await self.candle_collector.init_exchange()
        await self.oi_funding.init_exchange()
        await self.telegram.initialize()
        await self.executor.initialize()

        # FlowML 상태 로그
        logger.info(f"FlowML: trained={self.flow_ml.trained} samples={len(self.flow_ml.buffer_X)}")

        # 잔고 + 리스크
        balance = await self.executor.get_balance()
        await self.risk_manager.initialize(balance)
        logger.info(f"계좌 잔고: ${balance:.2f}")

        # 포지션 동기화 + ML 콜백 연결
        self.position_manager.on_trade_closed = self.record_ml_trade
        self.position_manager.telegram = self.telegram
        self.position_manager.trade_logger = self.trade_logger
        self.position_manager.risk_manager = self.risk_manager
        await self.position_manager.sync_positions()

        # 캔들 백필
        logger.info("캔들 백필 시작...")
        await self.candle_collector.backfill_all()
        for tf in ["5m", "1m"]:
            await self.candle_collector.backfill(tf, days=7)
        logger.info("캔들 백필 완료")

        # PaperTrader v2 초기화
        self.paper_trader = PaperTrader(
            db=self.db, redis=self.redis,
            flow_ml=self.flow_ml,
            regime_detector=self.regime_detector,
            signal_tracker=self.signal_tracker,
            setup_tracker=self.setup_tracker,
        )
        await self.paper_trader.restore_from_db()
        logger.info(
            f"📝 페이퍼 트레이딩 모드 — 가상 잔고 ${self.paper_trader.balance:,.0f} | "
            f"실거래 OFF"
        )

    # ══════════════════════════════════════════════════
    # FlowEngine 통합 엔진
    # ══════════════════════════════════════════════════

    async def periodic_unified_eval(self):
        """통합 시그널 평가 — 이벤트 드리븐 + 1초 폴백."""
        await asyncio.sleep(5)

        sub = None
        try:
            if self.redis.connected:
                sub = self.redis._client.pubsub()
                await sub.subscribe("ch:kline:ready")
                logger.info("[TRADE] 이벤트 드리븐 평가 활성화 (ch:kline:ready)")
        except Exception as e:
            logger.info(f"[TRADE] pub/sub 구독 실패 → 1초 폴링 모드: {e}")
            sub = None

        while self._running:
            try:
                triggered = False
                if sub:
                    try:
                        msg = await asyncio.wait_for(
                            sub.get_message(ignore_subscribe_messages=True, timeout=1.0),
                            timeout=1.5,
                        )
                        if msg and msg.get("type") == "message":
                            triggered = True
                    except asyncio.TimeoutError:
                        pass
                    except Exception:
                        sub = None

                await self._evaluate_unified()

                if not triggered:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[TRADE] 평가 에러: {e}", exc_info=True)
                await asyncio.sleep(1)

                if sub is None and self.redis.connected:
                    try:
                        sub = self.redis._client.pubsub()
                        await sub.subscribe("ch:kline:ready")
                    except Exception:
                        sub = None

    async def _evaluate_unified(self):
        """통합 엔진 평가 + 매매"""
        import time as _t

        now = _t.time()
        _gate_log_interval = 60

        # 일일 손실 한도
        if self._unified_daily_pnl <= -10.0:
            if now - getattr(self, "_last_gate_log", 0) >= _gate_log_interval:
                self._last_gate_log = now
                logger.info(f"[TRADE] 게이트: 일일 손실 한도 ({self._unified_daily_pnl:.1f}%) → 스킵")
            return

        # 쿨다운
        if now < self._unified_cooldown_until:
            return

        # 학습 중 진입 차단
        learning = self._learning_local or (await self.redis.get("sys:learning")) == "1"
        if learning:
            if now - getattr(self, "_last_gate_log", 0) >= _gate_log_interval:
                self._last_gate_log = now
                logger.info("[TRADE] 게이트: 학습 중 → 스킵")
            return

        # 이미 포지션 있으면 차단
        if self.position_manager.positions:
            return

        # 자동매매 상태
        autotrading = (await self.redis.get("sys:autotrading") or "off") == "on"

        # 캔들 로드
        candles_1m = await self.db.get_candles(self.symbol, "1m", limit=100)
        candles_5m = await self.db.get_candles(self.symbol, "5m", limit=100)
        candles_15m = await self.db.get_candles(self.symbol, "15m", limit=100)
        candles_1h = await self.db.get_candles(self.symbol, "1h", limit=100)
        candles_4h = await self.db.get_candles(self.symbol, "4h", limit=50)
        candles_1d = await self.db.get_candles(self.symbol, "1d", limit=30)

        if not candles_1m or len(candles_1m) < 30 or not candles_5m or len(candles_5m) < 30:
            if now - getattr(self, "_last_gate_log", 0) >= _gate_log_interval:
                self._last_gate_log = now
                logger.warning(
                    f"[TRADE] 게이트: 캔들 부족 1m={len(candles_1m) if candles_1m else 0} "
                    f"5m={len(candles_5m) if candles_5m else 0} → 스킵"
                )
            return

        df_1m = BaseIndicator.to_dataframe(candles_1m)
        df_5m = BaseIndicator.to_dataframe(candles_5m)
        df_15m = BaseIndicator.to_dataframe(candles_15m) if candles_15m and len(candles_15m) >= 20 else None
        df_1h = BaseIndicator.to_dataframe(candles_1h) if candles_1h and len(candles_1h) >= 20 else None
        df_4h = BaseIndicator.to_dataframe(candles_4h) if candles_4h and len(candles_4h) >= 10 else None
        df_1d = BaseIndicator.to_dataframe(candles_1d) if candles_1d and len(candles_1d) >= 10 else None

        rt_velocity = await self.redis.hgetall("rt:velocity:BTC-USDT-SWAP")

        # FlowEngine 분석
        result = await self.trade_engine.analyze(
            df_1m, df_5m, df_15m, df_1h,
            df_4h=df_4h, df_1d=df_1d, rt_velocity=rt_velocity,
        )
        self._last_flow_result = result

        ctx = result.get("signals", {}).get("context", {})

        # 주기적 로깅 (30초마다)
        if now - getattr(self, "_last_unified_log", 0) >= 30:
            self._last_unified_log = now
            sigs = result.get("signals", {})
            reason = result.get("reason", "?")
            big_trend = sigs.get("big_trend", "?")
            logger.info(
                f"[TRADE] setup={result.get('setup') or 'none'} "
                f"dir={result.get('direction', 'neutral')} "
                f"score={result.get('score', 0):.1f} "
                f"trend_1d={sigs.get('trend_1d', '?')} "
                f"trend_4h={sigs.get('trend_4h', '?')} "
                f"big={big_trend} "
                f"streak={self._unified_streak} "
                f"reason={reason}"
            )

        # 레짐 감지 + 저장
        if df_15m is not None and len(df_15m) >= 20:
            regime_result = self.regime_detector.detect(df_15m)
            self._current_regime = regime_result
            await self.redis.set("sys:regime", regime_result.get("regime", "ranging"), ttl=300)
            await self.redis.set("sys:regime_detail", regime_result, ttl=300)

        # TradeEngine 상태 Redis 저장
        regime_now = self._current_regime["regime"] if self._current_regime else "ranging"
        await self.redis.set("sys:trade_state", {
            "setup": result.get("setup"),
            "direction": result.get("direction", "neutral"),
            "score": result.get("score", 0),
            "trend": ctx.get("big_trend", "neutral"),
            "vol_band": ctx.get("vol_band", "mid"),
            "session": ctx.get("session", "unknown"),
            "regime": regime_now,
            "streak": self._unified_streak,
            "hold_mode": result.get("hold_mode", "standard"),
        }, ttl=30)

        # 셋업 없으면 리턴
        if not result.get("setup"):
            return

        setup = result["setup"]
        direction = result["direction"]
        score = result["score"]

        # 최소 점수 체크
        MIN_ENTRY_SCORE = 5.0
        if score < MIN_ENTRY_SCORE:
            if now - getattr(self, "_last_low_score_log", 0) >= 30:
                self._last_low_score_log = now
                raw = result.get("signals", {}).get("raw_score", score)
                htf = result.get("signals", {}).get("htf_bias_applied", 0)
                logger.info(
                    f"[TRADE] Setup {setup} {direction.upper()} 점수 부족: "
                    f"{score:.1f} < {MIN_ENTRY_SCORE} (raw={raw:.1f} htf={htf:+.1f})"
                )
            return

        # 셋업별 비활성 체크
        if not self.setup_tracker.is_setup_enabled(setup):
            logger.info(f"[TRADE] {setup} 셋업 비활성 (성과 부진) → 스킵")
            return

        self.setup_tracker.record_detection(setup, direction, score, float(df_5m["close"].iloc[-1]))

        if direction == "neutral":
            return

        # 최소 진입 간격
        cooldown_cfg = self.config.get("cooldown", {})
        min_interval = cooldown_cfg.get("min_interval_sec", 60)
        if now - self._unified_last_trade_time < min_interval:
            return

        # 같은 가격대 재진입 방지
        if self._unified_last_exit_reason and "sl" in self._unified_last_exit_reason:
            last_trade_price = self._unified_last_entry_price
            if last_trade_price > 0 and abs(price_now - last_trade_price) / price_now < 0.003:
                logger.info(f"[TRADE] 같은 가격대 재진입 차단")
                return

        # 방향 전환 쿨다운
        if self._unified_last_dir and self._unified_last_dir != direction:
            flip_cd = cooldown_cfg.get("direction_flip_sec", 300)
            if now - self._unified_last_trade_time < flip_cd:
                logger.info(f"[TRADE] 방향 전환 쿨다운 ({flip_cd}s) → 대기")
                return

        # 같은 방향 연속 4회 이상 차단
        MAX_SAME_DIR = 6
        if self._unified_last_dir == direction:
            if self._unified_same_dir_count >= MAX_SAME_DIR:
                logger.info(
                    f"[TRADE] 같은 방향 연속 {self._unified_same_dir_count}회 → "
                    f"{direction.upper()} 차단"
                )
                return

        # 가격 확인
        price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
        price = float(price_str) if price_str else 0
        if price <= 0:
            return

        # 페이퍼 트레이딩: 독립 가상 계좌
        if self.paper_trader:
            await self.paper_trader.try_entry(result, "unified", price)

        # 실거래 (autotrading=off 시 실행 안 됨)
        if autotrading:
            balance = await self.executor.get_balance()
            if balance > 0:
                await self._execute_unified(result, price, balance)

    async def _execute_unified(self, result: dict, price: float, balance: float):
        """통합 엔진 매매 실행"""
        import time as _t

        direction = result["direction"]
        setup = result["setup"]
        score = result["score"]
        hold_mode = result.get("hold_mode", "standard")

        hm_cfg = self.config.get("hold_modes", {}).get(hold_mode, {})
        sl_margin_pct = hm_cfg.get("sl_margin_pct", 8.0)
        tp1_margin_pct = hm_cfg.get("tp1_margin_pct", 12.0)
        tp2_mult = hm_cfg.get("tp2_mult", 2.5)
        tp3_mult = hm_cfg.get("tp3_mult", 4.0)

        # 점수 → 등급 → 레버리지
        if score >= 9.0:
            grade = "A+"
        elif score >= 8.0:
            grade = "A"
        elif score >= 7.0:
            grade = "B+"
        else:
            grade = "B"

        atr_pct = result.get("atr_pct", 0.3)
        lev_result = self.leverage_calc.calculate(grade, atr_pct, self._unified_streak)
        leverage = lev_result["leverage"]

        # SL/TP 거리
        sl_dist = result.get("sl_distance", 0)
        tp_dist = result.get("tp_distance", 0)

        if sl_dist <= 0:
            sl_dist = price * (sl_margin_pct / leverage / 100)
        if tp_dist <= 0:
            tp_dist = price * (tp1_margin_pct / leverage / 100)

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

        # LVL/PB = limit order (maker 수수료), 나머지 = market
        entry_price_limit = round(price, 1) if setup in ("LVL", "PB") else None

        trade_req = {
            "symbol": self.symbol, "direction": direction,
            "grade": grade, "score": score,
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
            if self._unified_last_dir == direction:
                self._unified_same_dir_count += 1
            else:
                self._unified_same_dir_count = 1
            self._unified_last_dir = direction
            self._unified_last_entry_price = pos.entry_price

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

    # ── ML 학습 기록 ──

    async def record_ml_trade(self, mode: str, signals: dict, pnl_pct: float,
                             fee_pct: float = 0.0, direction: str = "",
                             exit_reason: str = "", pnl_usdt: float = 0.0,
                             hold_min: float = 0.0):
        """실거래 결과 → FlowML 학습 + 연패 관리 + 셋업 추적"""
        if self._last_flow_result:
            try:
                self.flow_ml.record_trade(self._last_flow_result, pnl_pct, fee_pct)
            except Exception as e:
                logger.debug(f"FlowML record error: {e}")

        self._unified_record_result(pnl_pct, exit_reason)

        regime = self._current_regime["regime"] if self._current_regime else "ranging"
        if self.signal_tracker:
            self.signal_tracker.record_trade(signals, pnl_pct, mode="unified", regime=regime)

        trend = signals.get("big_trend", "neutral")
        # 셋업 이름: last_flow_result에서 가져옴
        setup_name = self._last_flow_result.get("setup", "LVL") if self._last_flow_result else "LVL"
        self.setup_tracker.record_trade(
            setup=setup_name, direction=direction, pnl_pct=pnl_pct,
            pnl_usdt=pnl_usdt, hold_min=hold_min,
            exit_reason=exit_reason, trend=trend, regime=regime,
        )

        logger.info(f"[실거래] PnL {pnl_pct:+.2f}% 연패:{self._unified_streak} 레짐:{regime}")

    # ── 주기적 루프들 ──

    async def periodic_candle_update(self):
        """캔들 갱신 — Binance WS가 메인, REST는 30초 백업."""
        while self._running:
            try:
                for tf in ["1m", "5m", "15m", "1h", "4h", "1d", "1w"]:
                    candles = await self.candle_collector.fetch_candles(tf, limit=5)
                    if candles:
                        await self.db.insert_candles(self.symbol, tf, candles)
            except Exception as e:
                logger.error(f"캔들 REST 백업 에러: {e}")
            await asyncio.sleep(30)

    async def periodic_position_check(self):
        """포지션 체크 — 실거래 + 가상매매"""
        while self._running:
            try:
                price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
                if price_str:
                    price = float(price_str)

                    # 실거래 포지션 체크
                    if self.position_manager.positions:
                        await self.position_manager.check_positions(price)

                    # 가상매매 포지션 체크
                    if self.paper_trader and (self.paper_trader.positions or self.paper_trader.shadows):
                        await self.paper_trader.check_positions(price)

                # 킬스위치 체크
                bot_status = await self.redis.get("sys:bot_status")
                if bot_status == "stopped":
                    logger.warning("킬스위치 감지 → 전 포지션 청산")
                    await self.position_manager.close_all("kill_switch")
                    await self.telegram.notify_emergency("Kill switch activated")
            except Exception as e:
                logger.error(f"포지션 체크 에러: {e}")

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
        """일일 리셋 (매일 00:00 UTC)"""
        last_reset_date = None
        while self._running:
            now = datetime.now(timezone.utc)
            today = now.date()
            if last_reset_date is None:
                last_reset_date = today

            if today > last_reset_date:
                last_reset_date = today
                self._current_day = today.day
                await self.risk_manager.reset_daily()

                logger.info(f"[TRADE] 일일 리셋 | 어제 P&L: {self._unified_daily_pnl:+.1f}%")
                self._unified_daily_pnl = 0.0
                self._unified_streak = 0
                self._unified_cooldown_until = 0
                self._unified_last_dir = None
                self._unified_same_dir_count = 0

                # 일일 리포트 (실전 + 페이퍼 통합)
                try:
                    import time as _t
                    yesterday_start = int((_t.time() - 86400) * 1000)

                    # 실전 매매 집계
                    cur_real = await self.db._db.execute(
                        "SELECT COUNT(*), "
                        "SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END), "
                        "SUM(CASE WHEN pnl_pct <= 0 THEN 1 ELSE 0 END), "
                        "SUM(pnl_usdt) "
                        "FROM trades "
                        "WHERE entry_time >= ? AND exit_time IS NOT NULL "
                        "AND grade NOT LIKE 'PAPER_%'",
                        (yesterday_start,)
                    )
                    r = await cur_real.fetchone()
                    real_t, real_w, real_l = (r[0] or 0), (r[1] or 0), (r[2] or 0)
                    real_pnl = float(r[3] or 0)

                    # 페이퍼 매매 집계
                    cur_paper = await self.db._db.execute(
                        "SELECT COUNT(*), "
                        "SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END), "
                        "SUM(CASE WHEN pnl_pct <= 0 THEN 1 ELSE 0 END), "
                        "SUM(pnl_usdt) "
                        "FROM trades "
                        "WHERE entry_time >= ? AND exit_time IS NOT NULL "
                        "AND grade LIKE 'PAPER_%'",
                        (yesterday_start,)
                    )
                    p = await cur_paper.fetchone()
                    paper_t, paper_w, paper_l = (p[0] or 0), (p[1] or 0), (p[2] or 0)
                    paper_pnl = float(p[3] or 0)

                    # 셋업별 성과
                    cur_setup = await self.db._db.execute(
                        "SELECT grade, COUNT(*), "
                        "SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END), "
                        "COALESCE(AVG(pnl_pct), 0) "
                        "FROM trades "
                        "WHERE entry_time >= ? AND exit_time IS NOT NULL "
                        "GROUP BY grade",
                        (yesterday_start,)
                    )
                    setup_rows = await cur_setup.fetchall()
                    setup_lines = []
                    for sr in setup_rows:
                        g = sr[0] or "?"
                        st, sw = sr[1] or 0, sr[2] or 0
                        sa = sr[3] or 0
                        wr = sw / st * 100 if st > 0 else 0
                        setup_lines.append(f"  {g}: {st}건 승률{wr:.0f}% avg{sa:+.1f}%")

                    # 전체 누적 건수 (ML 마일스톤)
                    cur_total = await self.db._db.execute(
                        "SELECT COUNT(*) FROM trades WHERE exit_time IS NOT NULL"
                    )
                    all_trades = (await cur_total.fetchone())[0] or 0

                except Exception as e:
                    logger.debug(f"일일 리포트 DB 집계 실패: {e}")
                    real_t, real_w, real_l, real_pnl = 0, 0, 0, 0.0
                    paper_t, paper_w, paper_l, paper_pnl = 0, 0, 0, 0.0
                    setup_lines, all_trades = [], 0

                risk = await self.risk_manager.get_risk_state()
                real_bal = risk.get("balance", 0)
                paper_bal = self.paper_trader.balance if self.paper_trader else 0

                real_wr = real_w / real_t * 100 if real_t > 0 else 0
                paper_wr = paper_w / paper_t * 100 if paper_t > 0 else 0
                r_icon = "\U0001f4c8" if real_pnl >= 0 else "\U0001f4c9"
                p_icon = "\U0001f4c8" if paper_pnl >= 0 else "\U0001f4c9"

                report = (
                    f"\U0001f4ca <b>Daily Report | {now.strftime('%Y-%m-%d')}</b>\n\n"
                    f"<b>실전</b>\n"
                    f"  매매: {real_t}건 | 승률: {real_wr:.0f}%\n"
                    f"  {r_icon} P&L: ${real_pnl:+,.2f} | 잔고: ${real_bal:,.2f}\n\n"
                    f"<b>페이퍼</b>\n"
                    f"  매매: {paper_t}건 | 승률: {paper_wr:.0f}%\n"
                    f"  {p_icon} P&L: ${paper_pnl:+,.2f} | 잔고: ${paper_bal:,.0f}\n"
                )
                if setup_lines:
                    report += f"\n<b>셋업별</b>\n" + "\n".join(setup_lines) + "\n"
                report += f"\n\U0001f4dd 누적 {all_trades}건 | ML: {'Active' if self.flow_ml.trained else f'학습 대기 ({len(self.flow_ml.buffer_X)}/50)'}"

                await self.telegram._send(report)

                # ML 마일스톤 알림
                ml_samples = len(self.flow_ml.buffer_X)
                milestones = [50, 100, 200, 500]
                for ms in milestones:
                    if ml_samples >= ms and not getattr(self, f'_ml_milestone_{ms}', False):
                        setattr(self, f'_ml_milestone_{ms}', True)
                        await self.telegram._send(
                            f"\U0001f3af <b>ML 마일스톤: {ms}건 달성!</b>\n"
                            f"FlowML: trained={self.flow_ml.trained} "
                            f"samples={ml_samples}"
                        )

                # FlowML 주기적 저장
                try:
                    self.flow_ml.save()
                except Exception as e:
                    logger.debug(f"FlowML 저장 실패: {e}")

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
        """헬스체크 (60초마다)"""
        while self._running:
            await self.redis.set("sys:last_heartbeat", str(int(_time.time())))
            # 통합 엔진 streak/pnl → Redis (대시보드용)
            await self.redis.set("risk:streak", str(self._unified_streak))
            await self.redis.set("risk:daily_pnl", f"{self._unified_daily_pnl:.2f}")
            bal = 0
            try:
                bal = await asyncio.wait_for(self.executor.get_balance(), timeout=5.0)
                if bal and bal > 0:
                    await self.redis.set("sys:balance", f"{bal:.2f}")
            except asyncio.TimeoutError:
                logger.debug("잔고 캐시 timeout")
            except Exception as e:
                logger.debug(f"잔고 캐시 실패: {e}")

            # 봇 상태 스냅샷
            try:
                import json as _j
                positions_snap = {}
                for sym, pos in self.position_manager.positions.items():
                    positions_snap[sym] = pos.to_dict()
                regime = await self.redis.get("sys:regime") or "unknown"
                trade_state = await self.redis.get_json("sys:trade_state") or {}
                autotrading = await self.redis.get("sys:autotrading") or "off"

                # 페이퍼 상태 Redis 갱신 (60초마다, TTL 없이 영구)
                if self.paper_trader:
                    await self.paper_trader._update_redis_state()
                paper_stats = self.paper_trader.get_stats() if self.paper_trader else {}

                snapshot = {
                    "ts": int(_time.time()),
                    "ts_iso": datetime.now(timezone.utc).isoformat(),
                    "balance": round(bal, 2) if bal else 0,
                    "autotrading": autotrading,
                    "regime": regime,
                    "trade_state": trade_state,
                    "positions": positions_snap,
                    "streak": self._unified_streak,
                    "daily_pnl": round(self._unified_daily_pnl, 2),
                    "paper": paper_stats,
                }

                # OKX pending algos 조회
                try:
                    inst_id = self.executor.exchange.market(self.symbol)["id"]
                    resp = await self.executor.exchange.private_get_trade_orders_algo_pending(
                        {"instType": "SWAP", "instId": inst_id, "ordType": "trigger"}
                    )
                    algos = resp.get("data", []) if isinstance(resp, dict) else []
                    snapshot["pending_algos"] = [
                        {"id": a.get("algoClOrdId") or a.get("algoId"),
                         "type": a.get("ordType"), "trigger": a.get("triggerPx"),
                         "side": a.get("side"), "sz": a.get("sz")}
                        for a in algos
                    ]
                except Exception:
                    snapshot["pending_algos"] = []

                snap_path = Path("/app/data/logs/bot_snapshot.json") if Path("/app/data/logs").is_dir() \
                    else Path("data/logs/bot_snapshot.json")
                snap_path.parent.mkdir(parents=True, exist_ok=True)
                with open(snap_path, "w") as f:
                    _j.dump(snapshot, f, indent=2, default=str)
            except Exception as e:
                logger.debug(f"스냅샷 저장 실패: {e}")

            await asyncio.sleep(60)

    async def periodic_orphan_algo_sweeper(self):
        """고아 알고 주기 정리 (120초마다)."""
        while self._running:
            await asyncio.sleep(120)
            try:
                if self.position_manager.positions:
                    continue
                try:
                    ex_positions = await asyncio.wait_for(
                        self.executor.get_positions(), timeout=5.0
                    )
                except Exception:
                    continue
                has_ex_position = any(abs(float(p.get("size") or 0)) > 0 for p in ex_positions)
                if has_ex_position:
                    continue
                try:
                    cleaned = await self.executor.cancel_all_algos()
                    if cleaned:
                        logger.warning(f"🧹 고아 알고 {len(cleaned)}개 정리")
                except Exception as e:
                    logger.debug(f"sweeper 정리 실패: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"sweeper 루프 에러: {e}")

    async def periodic_dashboard_commands(self):
        """대시보드 Redis 명령 큐 처리"""
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
                    except Exception:
                        pass
                else:
                    logger.warning(f"[DASH-CMD] 알 수 없는 action: {action}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[DASH-CMD] 루프 에러: {e}")
                await asyncio.sleep(2)

    # ── 메인 ──

    async def run(self):
        await self.initialize()
        self._running = True
        self._current_day = datetime.now(timezone.utc).day

        logger.info("봇 시작 — FlowEngine v2 (6셋업 + PaperTrader)")
        await self.redis.set("sys:bot_status", "running")
        await self.redis.set("sys:autotrading", "on")  # 실거래 ON (페이퍼 병행)

        # 텔레그램 명령어 처리용 주입
        self.telegram.redis = self.redis
        self.telegram.executor = self.executor
        self.telegram.position_manager = self.position_manager
        self.telegram.risk_manager = self.risk_manager

        await self.telegram.notify_bot_status("running")
        try:
            bal = await self.executor.get_balance()
            paper_bal = self.paper_trader.balance if self.paper_trader else 0
            await self.telegram._send(
                "\U0001f7e2 <b>FlowEngine v2 — LIVE + PAPER</b>\n"
                "Mode: OrderFlow (6 Setups)\n"
                "Trading: <b>LIVE ON</b> + Paper 병행\n"
                f"ML: {'Active' if self.flow_ml.trained else 'Cold Start'}\n"
                f"Real Balance: ${bal:,.2f}\n"
                f"Paper Balance: ${paper_bal:,.0f}"
            )
        except Exception:
            await self.telegram._send("\U0001f7e2 <b>FlowEngine v1 Started</b>")

        tasks = [
            asyncio.create_task(self.periodic_candle_update()),
            asyncio.create_task(self.periodic_unified_eval()),
            asyncio.create_task(self.periodic_position_check()),
            asyncio.create_task(self.periodic_oi_funding()),
            asyncio.create_task(self.periodic_daily_reset()),
            asyncio.create_task(self.periodic_heartbeat()),
            asyncio.create_task(self.periodic_orphan_algo_sweeper()),
            asyncio.create_task(self.periodic_dashboard_commands()),
            asyncio.create_task(self.ws_stream.start()),
            asyncio.create_task(self.binance_stream.start()),
            asyncio.create_task(self.telegram.poll_commands()),
        ]

        try:
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
        logger.info("=== Graceful Shutdown 시작 ===")

        try:
            self.signal_tracker.save()
            self.flow_ml.save()
            logger.info("ML 모델 + SignalTracker 저장 완료")
        except Exception as e:
            logger.error(f"종료 시 저장 실패: {e}")

        try:
            for symbol, pos in list(self.position_manager.positions.items()):
                logger.warning(
                    f"종료 시 미청산 포지션: {symbol} {pos.direction.upper()} "
                    f"@ ${pos.entry_price}"
                )
        except Exception:
            pass

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
