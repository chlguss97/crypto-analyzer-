"""
CryptoAnalyzer v2 — 모멘텀 스캘핑 봇

아키텍처:
  Binance (데이터 100%) → CandidateDetector (3종 후보)
  → ML DecisionEngine (Go/NoGo) → OKX (실행)
  → PositionManager (SL/TP/Adverse Selection)

SPEC v2 (2026-04-28) 기반 전면 재작성.
"""

import asyncio
import json
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
from src.engine.base import BaseIndicator  # to_dataframe 유틸

# ── 새 구조 ──
from src.strategy.candidate_detector import CandidateDetector
from src.strategy.ml_engine import MLDecisionEngine
from src.trading.leverage import LeverageCalculator
from src.trading.risk_manager import RiskManager
from src.trading.executor import OrderExecutor
from src.trading.position_manager import PositionManager
from src.monitoring.telegram_bot import TelegramNotifier
from src.monitoring.trade_logger import TradeLogger
from src.strategy.signal_tracker import SignalTracker
from src.strategy.setup_tracker import SetupTracker
from src.engine.regime_detector import MarketRegimeDetector
from src.strategy.paper_lab import PaperLab

import os
os.environ["TZ"] = "Asia/Seoul"
try:
    import time as _tz_time
    _tz_time.tzset()
except AttributeError:
    pass  # Windows

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
from logging.handlers import TimedRotatingFileHandler as _TRH
from pathlib import Path as _P
_log_dir = _P(__file__).parent.parent / "data" / "logs"
_log_dir.mkdir(parents=True, exist_ok=True)
_fh = _TRH(_log_dir / "bot.log", when="W0", backupCount=520, encoding="utf-8", utc=True)
_fh.suffix = "%Y-W%W"
_fh.setLevel(logging.WARNING)
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logging.getLogger().addHandler(_fh)

logger = logging.getLogger("CryptoAnalyzer")


class CryptoAnalyzer:
    """메인 봇 — CandidateDetector + ML DecisionEngine + PaperLab"""

    def __init__(self):
        load_env()
        self.config = load_config()
        self.symbol = self.config["exchange"]["symbol"]

        # 인프라
        self.db = Database()
        self.redis = RedisClient()
        self.candle_collector = CandleCollector(self.db)
        self.ws_stream = WebSocketStream(self.redis, db=self.db)  # OKX WS (전체 데이터)
        self.binance_stream = BinanceStream(self.redis)  # Binance REST (청산만)

        # ── 새 구조 ──
        self.detector = CandidateDetector(redis=self.redis, config=self.config)
        self.ml_engine = MLDecisionEngine(config=self.config)
        from src.strategy.adaptive_params import AdaptiveParams
        self.adaptive = AdaptiveParams(config=self.config, redis=self.redis)

        # 매매 엔진
        self.leverage_calc = LeverageCalculator()
        self.executor = OrderExecutor()
        self.risk_manager = RiskManager(self.redis, executor=self.executor)
        self.position_manager = PositionManager(self.executor, self.db, self.redis)

        # 모니터링
        self.telegram = TelegramNotifier()
        self.trade_logger = TradeLogger()

        # 레짐/추적
        self.regime_detector = MarketRegimeDetector()
        self._current_regime = None
        self.signal_tracker = SignalTracker()
        self.setup_tracker = SetupTracker()

        # 페이퍼 랩 (A/B 테스터)
        self.paper_lab = None

        # 상태
        self._running = False
        self._last_trade_time = 0
        self._last_candidate = None

    async def initialize(self):
        logger.info("=" * 50)
        logger.info("CryptoAnalyzer v2 — Momentum Scalping + ML")
        logger.info("=" * 50)

        await self.db.connect()
        await self.redis.connect()
        await self.candle_collector.init_exchange()
        await self.telegram.initialize()
        await self.executor.initialize()

        # ML 상태 + Phase 전환 알림 콜백
        self.ml_engine.on_phase_change = self._on_ml_phase_change
        logger.info(f"ML: Phase {self.ml_engine.phase}, labeled={self.ml_engine.total_labeled}")

        # AdaptiveParams 복원
        await self.adaptive.load_state()
        logger.info(f"Adaptive: {self.adaptive.total_trades}건, tp_mult={self.adaptive.tp_cal.current_mult:.3f}")

        # 잔고 + 리스크
        balance = await self.executor.get_balance()
        await self.risk_manager.initialize(balance)
        logger.info(f"계좌 잔고: ${balance:.2f}")

        # 포지션 동기화
        self.position_manager.on_trade_closed = self._on_trade_closed
        self.position_manager.telegram = self.telegram
        self.position_manager.trade_logger = self.trade_logger
        self.position_manager.risk_manager = self.risk_manager
        await self.position_manager.sync_positions()

        # 캔들 백필 (backfill_all이 전체 TF 처리)
        logger.info("캔들 백필 시작...")
        await self.candle_collector.backfill_all()
        logger.info("캔들 백필 완료")

        # PaperLab (A/B 파라미터 테스터)
        self.paper_lab = PaperLab(config=self.config, adaptive=self.adaptive)
        logger.info(f"PaperLab: {len(self.paper_lab.variants)} variants 활성")

    # ══════════════════════════════════════════════════
    #  메인 평가 루프 — 새 구조
    # ══════════════════════════════════════════════════

    async def periodic_eval(self):
        """후보 감지 → ML → 실행 루프"""
        await asyncio.sleep(5)

        sub = None
        try:
            if self.redis.connected:
                sub = self.redis._client.pubsub()
                await sub.subscribe("ch:kline:ready")
                logger.info("[EVAL] 이벤트 드리븐 활성화")
        except Exception:
            sub = None

        while self._running:
            try:
                if sub:
                    try:
                        msg = await asyncio.wait_for(
                            sub.get_message(ignore_subscribe_messages=True, timeout=1.0),
                            timeout=1.5,
                        )
                    except (asyncio.TimeoutError, Exception):
                        pass

                await self._evaluate()

                await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[EVAL] 에러: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def _evaluate(self):
        """메인 평가 — 후보 감지 → 리스크 → ML → 실행"""
        now = _time.time()

        # ── 리스크 게이트 ──

        # 1~3. 일일/주간 손실 + DD + 쿨다운 (risk_manager 통합)
        daily_pnl = self.risk_manager.get_daily_pnl()
        allowed, reason = self.risk_manager.is_trading_allowed()
        if not allowed:
            return

        # 4. 포지션 1개 제한
        if self.position_manager.positions:
            return

        # 5. 최소 진입 간격 30초
        min_interval = self.config.get("risk", {}).get("min_entry_interval_sec", 30)
        if now - self._last_trade_time < min_interval:
            return

        # ── 캔들 로드 ──
        candles_1m = await self.db.get_candles(self.symbol, "1m", limit=100)
        candles_5m = await self.db.get_candles(self.symbol, "5m", limit=100)
        candles_15m = await self.db.get_candles(self.symbol, "15m", limit=100)
        candles_1h = await self.db.get_candles(self.symbol, "1h", limit=100)
        candles_4h = await self.db.get_candles(self.symbol, "4h", limit=50)
        candles_1d = await self.db.get_candles(self.symbol, "1d", limit=30)

        if not candles_5m or len(candles_5m) < 30:
            if now - getattr(self, "_last_candle_warn", 0) >= 60:
                self._last_candle_warn = now
                logger.warning(
                    f"[EVAL] 캔들 부족: 1m={len(candles_1m) if candles_1m else 0} "
                    f"5m={len(candles_5m) if candles_5m else 0} → 스킵"
                )
            return

        df_1m = BaseIndicator.to_dataframe(candles_1m) if candles_1m else None
        df_5m = BaseIndicator.to_dataframe(candles_5m)
        df_15m = BaseIndicator.to_dataframe(candles_15m) if candles_15m and len(candles_15m) >= 20 else None
        df_1h = BaseIndicator.to_dataframe(candles_1h) if candles_1h and len(candles_1h) >= 20 else None
        df_4h = BaseIndicator.to_dataframe(candles_4h) if candles_4h and len(candles_4h) >= 10 else None
        df_1d = BaseIndicator.to_dataframe(candles_1d) if candles_1d and len(candles_1d) >= 10 else None

        # ── 레짐 감지 ──
        if df_15m is not None and len(df_15m) >= 50:
            regime_result = self.regime_detector.detect(df_15m)
            self._current_regime = regime_result
            await self.redis.set("sys:regime", regime_result.get("regime", "ranging"), ttl=300)
            await self.redis.set("sys:regime_detail", regime_result, ttl=300)

        regime_now = self._current_regime["regime"] if self._current_regime else "unknown"

        # ── 후보 감지 (1분 고속 → 5분 정규) ──
        # 1분: 강한 모멘텀 즉시 포착 (ATR_1m × 1.5, 엄격)
        candidate = self.detector.detect_fast(df_1m, df_5m)
        if candidate:
            # 1분 후보에 features_raw 추가 (ML용)
            atr_5m = self.detector._atr(df_5m, 14) if df_5m is not None and len(df_5m) >= 14 else 0
            vol_20avg = float(df_5m["volume"].astype(float).tail(20).mean()) if df_5m is not None and len(df_5m) >= 20 else 1.0
            flow = await self.detector._get_flow_data()
            candidate["features_raw"] = await self.detector._build_raw_features(
                df_5m, df_15m, df_1h, df_4h, df_1d,
                candidate["price"], atr_5m, candidate["atr_pct"],
                flow, vol_20avg, candidate["direction"], df_1m=df_1m
            )
            logger.info(f"[FAST] 1분 모멘텀 감지: {candidate['direction']} str={candidate['strength']:.1f} ATR_1m={candidate['atr_pct']:.3f}%")

        # 5분: 정규 평가 (1분에서 못 잡으면)
        if not candidate:
            candidate = await self.detector.detect(
                df_1m, df_5m, df_15m, df_1h, df_4h=df_4h, df_1d=df_1d
            )

        if not candidate:
            if now - getattr(self, "_last_no_candidate_log", 0) >= 60:
                self._last_no_candidate_log = now
                logger.info(f"[EVAL] 후보 없음 (5m={len(candles_5m)}건, regime={regime_now})")
            return

        # 동일 캔들 중복 시그널 방지
        dedup_key = (candidate["type"], candidate["direction"], int(candidate["price"] * 10))
        if dedup_key == getattr(self, "_last_dedup_key", None):
            return

        # 약한 후보는 shadow 전용 (진입 안 함, ML 데이터만 수집)
        is_weak = candidate.get("weak", False)
        if is_weak:
            signal_record = {
                "ts": int(now),
                "candidate_type": candidate["type"],
                "direction": candidate["direction"],
                "strength": candidate["strength"],
                "price": candidate["price"],
                "features": json.dumps(candidate.get("features_raw", {}), default=str),
                "ml_go": 0,
                "ml_prob": 0.0,
                "entry_executed": 0,
                "reject_reason": "weak_candidate",
                "regime": regime_now,
            }
            await self.db.insert_signal(signal_record)
            from src.monitoring.trade_logger import _append_jsonl
            _append_jsonl({
                "type": "candidate", "candidate_type": candidate["type"],
                "direction": candidate["direction"], "strength": round(candidate["strength"], 2),
                "price": round(candidate["price"], 1), "ml_go": 0, "weak": True,
                "regime": regime_now,
            })
            return
        self._last_dedup_key = dedup_key

        self._last_candidate = candidate
        direction = candidate["direction"]
        ctype = candidate["type"]
        strength = candidate["strength"]
        features = candidate.get("features_raw", {})

        # ── signals 테이블 기록 (전수) ──
        signal_record = {
            "ts": int(now),
            "candidate_type": ctype,
            "direction": direction,
            "strength": strength,
            "price": candidate["price"],
            "features": json.dumps(features, default=str),
            "ml_go": -1,
            "ml_prob": 0.0,
            "entry_executed": 0,
            "reject_reason": None,
            "regime": regime_now,
        }

        # ── ML Go/NoGo ──
        go, prob = self.ml_engine.decide(features)
        signal_record["ml_go"] = 1 if go else 0
        signal_record["ml_prob"] = round(prob, 4) if prob >= 0 else 0.0

        # 추세 캐시 (JSONL + 확신도에서 공유)
        from src.monitoring.trade_logger import _append_jsonl
        self._cached_h1_trend = self._get_tf_trend(df_1h)
        self._cached_h4_trend = self._get_tf_trend(df_4h)
        h1_trend = self._cached_h1_trend
        h4_trend = self._cached_h4_trend
        _append_jsonl({
            "type": "candidate",
            "candidate_type": ctype,
            "direction": direction,
            "strength": round(strength, 2),
            "price": round(candidate["price"], 1),
            "ml_go": 1 if go else 0,
            "ml_phase": self.ml_engine.phase,
            "regime": regime_now,
            "h1_trend": h1_trend,
            "h4_trend": h4_trend,
            "atr_pct": round(candidate.get("atr_pct", 0), 4),
            "cvd_matches": features.get("cvd_matches", 0),
            "vol_ratio": round(features.get("vol_ratio", 0), 2),
        })

        # reject_reason 사전 설정 (DB insert 전에)
        if not go:
            signal_record["reject_reason"] = "ml_nogo"

        # ── 기록 ──
        sig_id = await self.db.insert_signal(signal_record)

        # PaperLab (A/B 테스트: ML/게이트 무관하게 모든 후보 진입)
        if self.paper_lab:
            await self.paper_lab.on_candidate(candidate, regime_now)

        if not go:
            return

        # ── 수익 보호 ──
        profit_protect = self.config.get("risk", {}).get("profit_protect_pct", 0.03) * 100
        profit_stop = self.config.get("risk", {}).get("profit_stop_pct", 0.05) * 100
        if daily_pnl >= profit_stop:
            return

        # ── 자동매매 확인 ──
        autotrading = (await self.redis.get("sys:autotrading") or "off") == "on"

        # ── 확신도 점수 (0~5점 → 사이즈 비율) ──
        conviction, conv_detail = self._calc_conviction(
            regime_now, direction, ctype, strength, candidate, df_1h, df_4h
        )

        if conviction <= 0:
            logger.info(f"[GATE] 확신도 0점 차단: {direction} {ctype} | {conv_detail}")
            _append_jsonl({
                "type": "gate_block",
                "reason": "conviction_0",
                "direction": direction,
                "candidate_type": ctype,
                "detail": conv_detail,
                "regime": regime_now,
                "h1_trend": self._cached_h1_trend,
                "h4_trend": self._cached_h4_trend,
            })
            return

        # 실거래
        if autotrading:
            balance = await self.executor.get_balance()
            if balance > 0:
                executed = await self._execute(
                    candidate, balance, regime_now, daily_pnl,
                    conviction=conviction
                )
                if executed and sig_id:
                    await self.db.update_signal_entry(sig_id)

        # 주기적 상태 로깅
        if now - getattr(self, "_last_eval_log", 0) >= 30:
            self._last_eval_log = now
            await self.redis.set("sys:trade_state", {
                "candidate": ctype,
                "direction": direction,
                "strength": strength,
                "ml_phase": self.ml_engine.phase,
                "ml_prob": round(prob, 2) if prob >= 0 else "rule",
                "regime": regime_now,
                "streak": self.risk_manager.get_streak(),
                "daily_pnl": round(daily_pnl, 1),
            }, ttl=30)

    def _is_regime_aligned(self, regime: str, direction: str, ctype: str = "") -> bool:
        """레짐과 방향/셋업이 일치하는지 확인"""
        # trending에서 역방향: breakout은 전환 시그널이므로 허용
        if regime == "trending_up" and direction == "short" and ctype != "breakout":
            return False
        if regime == "trending_down" and direction == "long" and ctype != "breakout":
            return False
        # 횡보에서 breakout: 가짜 돌파 확률 높음
        if regime == "ranging" and ctype == "breakout":
            return False
        return True

    def _get_tf_trend(self, df) -> str:
        """DataFrame에서 EMA20 기반 추세 방향 반환"""
        if df is None or len(df) < 25:
            return "unknown"
        closes = df["close"].astype(float).values
        ema20 = closes[-20:].mean()
        price = closes[-1]
        slope = (closes[-1] - closes[-5]) / closes[-5] * 100 if closes[-5] > 0 else 0
        if price > ema20 and slope > 0.05:
            return "UP"
        elif price < ema20 and slope < -0.05:
            return "DOWN"
        return "FLAT"

    def _check_htf_trend(self, df_1h, df_4h, direction: str) -> tuple[bool, str]:
        """1h/4h EMA20 기반 추세 방향 체크. 역행 시 (True, reason) 반환."""
        reasons = []
        for label, df in [("1h", df_1h), ("4h", df_4h)]:
            if df is None or len(df) < 25:
                continue
            closes = df["close"].astype(float).values
            ema20 = closes[-20:].mean()  # SMA 근사 (빠른 계산)
            price = closes[-1]
            slope = (closes[-1] - closes[-5]) / closes[-5] * 100 if closes[-5] > 0 else 0

            if direction == "long" and price < ema20 and slope < -0.05:
                reasons.append(f"{label}=DOWN")
            elif direction == "short" and price > ema20 and slope > 0.05:
                reasons.append(f"{label}=UP")

        if reasons:
            return True, " ".join(reasons)
        return False, ""

    def _calc_conviction(self, regime: str, direction: str, ctype: str,
                         strength: float, candidate: dict,
                         df_1h=None, df_4h=None) -> tuple[int, str]:
        """확신도 점수 계산 (0~5점). 0점=차단, 1점+=진입(사이즈 비례)."""
        score = 0
        details = []

        # +1: 1h 추세 일치
        h1 = self._cached_h1_trend
        expected = "UP" if direction == "long" else "DOWN"
        if h1 == expected:
            score += 1
            details.append("1h:O")
        elif h1 != "FLAT" and h1 != "unknown" and h1 != expected:
            details.append("1h:X")
        else:
            details.append("1h:-")

        # +1: 4h 추세 일치
        h4 = self._cached_h4_trend
        if h4 == expected:
            score += 1
            details.append("4h:O")
        elif h4 != "FLAT" and h4 != "unknown" and h4 != expected:
            details.append("4h:X")
        else:
            details.append("4h:-")

        # +1: 15m 레짐 일치
        regime_ok = self._is_regime_aligned(regime, direction, ctype)
        if regime_ok:
            score += 1
            details.append("reg:O")
        else:
            details.append("reg:X")

        # +1: strength >= 0.8
        if strength >= 0.8:
            score += 1
            details.append(f"str:{strength:.1f}")
        else:
            details.append(f"str:{strength:.1f}")

        # +1: CVD 방향 지지
        cvd_matches = candidate.get("features_raw", {}).get("cvd_matches", 0)
        if cvd_matches >= 1:
            score += 1
            details.append("cvd:O")
        else:
            details.append("cvd:X")

        # 완전 역행 차단 (1h AND 4h 모두 반대)
        if h1 == ("DOWN" if direction == "long" else "UP") and \
           h4 == ("DOWN" if direction == "long" else "UP"):
            score = 0  # 강제 0점

        detail_str = " ".join(details) + f" → {score}점"
        logger.info(f"[CONVICTION] {direction} {ctype}: {detail_str}")
        return score, detail_str

    # 확신도 → 사이즈 배수 변환
    CONVICTION_MULT = {0: 0.0, 1: 0.15, 2: 0.30, 3: 0.60, 4: 0.80, 5: 1.0}

    async def _execute(self, candidate: dict, balance: float,
                       regime: str, daily_pnl: float,
                       conviction: int = 5) -> bool:
        """후보 → OKX 주문 실행"""
        direction = candidate["direction"]
        ctype = candidate["type"]
        strength = candidate["strength"]
        hold_mode = candidate.get("hold_mode", "momentum")
        price = candidate["price"]

        hm_cfg = self.config.get("hold_modes", {}).get(hold_mode, {})
        sl_margin_pct = hm_cfg.get("sl_margin_pct", 5.0)
        tp1_margin_pct = hm_cfg.get("tp1_margin_pct", 10.0)
        tp2_mult = hm_cfg.get("tp2_mult", 2.5)
        tp3_mult = hm_cfg.get("tp3_mult", 4.0)

        # 등급 (strength 기반)
        if strength >= 2.0:
            grade = "A+"
        elif strength >= 1.5:
            grade = "A"
        elif strength >= 1.0:
            grade = "B+"
        else:
            grade = "B"

        atr_pct = candidate.get("atr_pct", 0.3)
        lev_result = self.leverage_calc.calculate(grade, atr_pct, self.risk_manager.get_streak())
        leverage = lev_result["leverage"]

        # SL/TP 거리 (AdaptiveParams 보정 적용)
        adaptive_sl = self.adaptive.get_sl_margin_pct()
        sl_margin_used = adaptive_sl if adaptive_sl != 5.0 else sl_margin_pct
        sl_dist = price * (sl_margin_used / leverage / 100)
        min_sl = price * 0.005
        sl_dist = max(sl_dist, min_sl)

        # TP1: ATR 기반 (AdaptiveParams 보정 적용)
        adaptive_tp_mult = self.adaptive.get_tp_mult(regime)
        atr_tp1 = price * min(max(atr_pct * adaptive_tp_mult / 100, 0.0025), 0.008)
        tp1_dist = atr_tp1
        if tp1_dist < sl_dist * 1.3:
            tp1_dist = sl_dist * 1.3

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

        # 모멘텀 소진 체크: 최근 3캔들 이동이 TP1의 50% 이상이면 스킵
        recent_move_pct = candidate.get("recent_move_pct", 0)
        tp1_pct = tp1_dist / price * 100
        if recent_move_pct > tp1_pct * 0.5 and recent_move_pct > 0:
            logger.info(f"[EXEC] 모멘텀 소진: 최근이동 {recent_move_pct:.3f}% > TP1의 50% ({tp1_pct*0.5:.3f}%) → 차단")
            return False

        # 수수료 필터
        maker_fee = self.config.get("fees", {}).get("maker", 0.0002)
        fee_cost = maker_fee * 2 * leverage * 100
        tp1_gain = tp1_dist / price * leverage * 100
        if tp1_gain <= fee_cost:
            logger.info(f"[EXEC] TP1({tp1_gain:.1f}%) <= 수수료({fee_cost:.1f}%) → 차단")
            return False

        # 마진 계산
        risk_cfg = self.config.get("risk", {})
        margin_pct = risk_cfg.get("margin_pct", 0.40)

        # 연패 축소
        streak_sizing = risk_cfg.get("streak_sizing", {})
        size_mult = 1.0
        for threshold, mult in sorted(streak_sizing.items(), key=lambda x: int(x[0]), reverse=True):
            if self.risk_manager.get_streak() >= int(threshold):
                size_mult = mult
                break

        # 수익 보호 축소
        profit_protect = risk_cfg.get("profit_protect_pct", 0.03) * 100
        daily_pnl_now = self.risk_manager.get_daily_pnl()
        if daily_pnl_now >= profit_protect:
            size_mult *= 0.5
            logger.info(f"[EXEC] 수익 보호 발동 (+{daily_pnl_now:.1f}%) → 마진 50%")

        # 확신도 사이즈 배수
        conviction_mult = self.CONVICTION_MULT.get(conviction, 0.15)
        # AdaptiveParams Phase 1+ 보정 (데이터 기반 override)
        adaptive_mult = self.adaptive.get_entry_size_mult(
            direction, self._cached_h1_trend, self._cached_h4_trend, regime
        )
        if adaptive_mult < 1.0 and adaptive_mult > 0:
            conviction_mult = min(conviction_mult, adaptive_mult)

        margin = balance * margin_pct * size_mult * conviction_mult
        if margin <= 0:
            return False

        # OKX 사이즈 (최소 0.01 BTC 보장)
        raw_size = margin * leverage / price
        size_btc = math.floor(raw_size / 0.01) * 0.01
        size_btc = max(round(size_btc, 4), 0.01)  # 확신 낮아도 최소 진입

        logger.info(f"[SIZE] conviction={conviction}({conviction_mult:.0%}) "
                    f"adaptive={adaptive_mult:.2f} → {size_btc} BTC (${margin:.0f})")

        # strength >= 1.5: market 허용 (체결 우선, taker 0.05%)
        # strength < 1.5: post-only (maker 0.02%, 미체결 시 포기)
        use_market = strength >= 1.5
        entry_price_limit = None

        logger.info(
            f"[EXEC] {ctype.upper()} {direction.upper()} @ ${price:.0f} | "
            f"SL ${sl:.0f} TP1 ${tp1:.0f} | {leverage}x | "
            f"margin ${margin:.1f} | str={strength:.1f} | {grade}"
        )

        trade_req = {
            "symbol": self.symbol, "direction": direction,
            "grade": grade, "score": strength,
            "size": size_btc,
            "leverage": leverage,
            "entry_price": entry_price_limit,
            "use_market": use_market,
            "sl_price": round(sl, 1),
            "tp1_price": round(tp1, 1),
            "tp2_price": round(tp2, 1),
            "tp3_price": round(tp3, 1),
            "signals_snapshot": candidate.get("features_raw", {}),
        }

        pos = await self.position_manager.open_position(trade_req)
        if pos:
            # AdaptiveParams 추적 데이터 세팅
            pos.entry_atr = atr_pct
            pos.entry_h1_trend = getattr(self, "_cached_h1_trend", "unknown")
            pos.entry_h4_trend = getattr(self, "_cached_h4_trend", "unknown")
            pos.params_snapshot = {
                "regime": regime, "atr_mult": atr_pct * 1.5,
                "sl_margin_pct": sl_margin_pct, "tp1_dist": round(tp1_dist, 1),
            }
            self._last_trade_time = _time.time()

            try:
                self.trade_logger.log_entry(
                    direction, ctype.upper(), strength,
                    pos.entry_price, pos.sl_price, leverage, margin,
                    tp1_price=pos.tp1_price,
                    conviction=conviction,
                    conviction_mult=self.CONVICTION_MULT.get(conviction, 1.0),
                    h1_trend=self._cached_h1_trend,
                    h4_trend=self._cached_h4_trend,
                    regime=regime,
                )
            except Exception as e:
                logger.error(f"trade_logger 실패: {e}")

            await self.telegram.notify_entry(
                direction, ctype.upper(), strength,
                pos.entry_price, pos.sl_price, pos.tp1_price, pos.tp2_price,
                leverage, margin, tp3_price=pos.tp3_price,
                conviction=conviction,
                conviction_mult=self.CONVICTION_MULT.get(conviction, 1.0),
            )
            return True

        return False

    # ── ML Phase 전환 알림 ──

    async def _on_ml_phase_change(self, old_phase: str, new_phase: str, details: str):
        """ML Phase 전환 시 텔레그램 알림"""
        icons = {"A": "\U0001f7e1", "B": "\U0001f7e2", "B+": "\U0001f4a1"}
        icon = icons.get(new_phase, "\u26a0\ufe0f")
        msg = (
            f"{icon} <b>ML Phase 전환: {old_phase} → {new_phase}</b>\n\n"
            f"{details}\n\n"
        )
        if new_phase == "B":
            msg += "ML이 이제 Go/NoGo 결정을 합니다."
        elif new_phase == "B+":
            msg += "마이크로스트럭처 피처가 학습에 추가되었습니다."
        elif new_phase == "A":
            msg += "ML 성능 부족 → 룰 기반으로 복귀합니다."

        try:
            await self.telegram._send(msg)
        except Exception:
            pass

    # ── 거래 결과 콜백 ──

    async def _on_trade_closed(self, mode: str, signals: dict, pnl_pct: float,
                               fee_pct: float = 0.0, direction: str = "",
                               exit_reason: str = "", pnl_usdt: float = 0.0,
                               hold_min: float = 0.0, pos_data: dict = None):
        """실거래 결과 → 리스크 + ML + AdaptiveParams"""
        await self.risk_manager.record_trade_result(pnl_pct, pnl_usdt)

        # ML 결과 기록
        label = 1 if pnl_pct > 0 else 0
        self.ml_engine.record_decision_result(True, label)

        # ML 재학습 체크
        try:
            labeled = await self.db.get_labeled_signals(self.ml_engine.window_size)
            self.ml_engine.check_and_train(labeled)
        except Exception as e:
            logger.debug(f"ML retrain check 실패: {e}")

        # AdaptiveParams 결과 기록
        if pos_data:
            ep = pos_data.get("entry_price", 0)
            tp1 = pos_data.get("tp1_price", 0)
            best = pos_data.get("best_price", 0)
            worst = pos_data.get("worst_price", ep)
            tp1_dist = abs(tp1 - ep) if tp1 > 0 else 1
            if direction == "long":
                reach = (best - ep) / tp1_dist * 100 if tp1_dist > 0 else 0
                mae_pct = (ep - worst) / ep * 100
            else:
                reach = (ep - best) / tp1_dist * 100 if tp1_dist > 0 else 0
                mae_pct = (worst - ep) / ep * 100

            ttp = pos_data.get("first_profit_ts", 0)
            ttp_sec = ttp - pos_data.get("entry_time", 0) if ttp > 0 else 0

            await self.adaptive.record_trade({
                "direction": direction,
                "pnl_pct": pnl_pct,
                "hold_min": hold_min,
                "exit_reason": exit_reason,
                "tp1_reach_pct": round(reach, 1),
                "mae_pct": round(mae_pct, 4),
                "time_to_first_profit_sec": round(ttp_sec, 0),
                "entry_atr": pos_data.get("entry_atr", 0.3),
                "entry_h1_trend": pos_data.get("entry_h1_trend", "unknown"),
                "entry_h4_trend": pos_data.get("entry_h4_trend", "unknown"),
                "regime": pos_data.get("regime", "unknown"),
                "entry_ts": pos_data.get("entry_time", 0),
                "leverage": pos_data.get("leverage", 15),
            })

        regime = self._current_regime["regime"] if self._current_regime else "unknown"

        if self.signal_tracker:
            self.signal_tracker.record_trade(signals, pnl_pct, mode="unified", regime=regime)

        ctype = self._last_candidate.get("type", "momentum") if self._last_candidate else "momentum"
        self.setup_tracker.record_trade(
            setup=ctype, direction=direction, pnl_pct=pnl_pct,
            pnl_usdt=pnl_usdt, hold_min=hold_min,
            exit_reason=exit_reason, trend="", regime=regime,
        )

        # 쿨다운
        cooldown_cfg = self.config.get("cooldown", {})
        cd = cooldown_cfg.get("after_loss_sec", 60) if pnl_pct < 0 else cooldown_cfg.get("after_win_sec", 20)
        new_cd = _time.time() + cd
        if new_cd > self.risk_manager.get_cooldown_until():
            self.risk_manager._state["cooldown_until"] = int(new_cd)
            await self.redis.set("risk:cooldown_until", str(int(new_cd)))

        logger.info(
            f"[TRADE] 결과: PnL {pnl_pct:+.2f}% ${pnl_usdt:+.2f} | "
            f"연패:{self.risk_manager.get_streak()} | 레짐:{regime}"
        )

    # ── Shadow 추적 ──

    async def periodic_shadow_check(self):
        """모든 시그널의 Triple Barrier 라벨링 + 연속값 추적"""
        from src.monitoring.trade_logger import _append_jsonl
        # 시그널별 best/worst 가격 추적 (메모리)
        shadow_tracking: dict[int, dict] = {}

        while self._running:
            try:
                price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
                if not price_str:
                    await asyncio.sleep(5)
                    continue
                price = float(price_str)

                pending = await self.db.get_pending_shadows()
                now = _time.time()

                for sig in pending:
                    sig_id = sig["id"]
                    sig_ts = sig["ts"]
                    sig_price = sig["price"]
                    sig_dir = sig["direction"]
                    ctype = sig["candidate_type"]

                    # best/worst 추적 초기화
                    if sig_id not in shadow_tracking:
                        shadow_tracking[sig_id] = {
                            "best": sig_price, "worst": sig_price
                        }
                    track = shadow_tracking[sig_id]

                    # best/worst 업데이트
                    if sig_dir == "long":
                        track["best"] = max(track["best"], price)
                        track["worst"] = min(track["worst"], price)
                    else:
                        track["best"] = min(track["best"], price)
                        track["worst"] = max(track["worst"], price)

                    # barrier 계산 (fast_momentum → quick 매핑)
                    hold_key = "quick" if ctype == "fast_momentum" else ctype
                    hm_cfg = self.config.get("hold_modes", {}).get(hold_key, {})
                    sl_pct = hm_cfg.get("sl_margin_pct", 5.0)
                    max_hold = hm_cfg.get("max_hold_min", 240) * 60  # default 4시간

                    leverage = 15
                    sl_dist = sig_price * (sl_pct / leverage / 100)

                    try:
                        feat = json.loads(sig.get("features", "{}"))
                        atr_pct = feat.get("atr_pct", 0.3)
                    except Exception:
                        atr_pct = 0.3
                    tp_dist = sig_price * min(max(atr_pct * 1.5 / 100, 0.0025), 0.008)
                    if tp_dist < sl_dist * 1.3:
                        tp_dist = sl_dist * 1.3

                    if sig_dir == "long":
                        tp_price = sig_price + tp_dist
                        sl_price = sig_price - sl_dist
                        hit_tp = price >= tp_price
                        hit_sl = price <= sl_price
                    else:
                        tp_price = sig_price - tp_dist
                        sl_price = sig_price + sl_dist
                        hit_tp = price <= tp_price
                        hit_sl = price >= sl_price

                    elapsed = now - sig_ts

                    label = -1
                    barrier = None
                    pnl = 0.0

                    if hit_tp:
                        label = 1
                        barrier = "tp"
                        pnl = tp_dist / sig_price * leverage * 100
                    elif hit_sl:
                        label = 0
                        barrier = "sl"
                        pnl = -(sl_dist / sig_price * leverage * 100)
                    elif elapsed >= max_hold:
                        barrier = "time"
                        if sig_dir == "long":
                            pnl = (price - sig_price) / sig_price * leverage * 100
                        else:
                            pnl = (sig_price - price) / sig_price * leverage * 100
                        label = 1 if pnl > 0 else 0

                    if label >= 0:
                        # 연속값 계산
                        if sig_dir == "long":
                            best_move = (track["best"] - sig_price) / sig_price * 100
                            mae = (sig_price - track["worst"]) / sig_price * 100
                        else:
                            best_move = (sig_price - track["best"]) / sig_price * 100
                            mae = (track["worst"] - sig_price) / sig_price * 100
                        reach = best_move / (tp_dist / sig_price * 100) * 100 if tp_dist > 0 else 0

                        await self.db.update_signal_label(
                            sig_id, label, barrier, round(pnl, 2), int(now),
                            reach_pct=round(reach, 1),
                            mae_pct=round(mae, 4),
                            best_move_pct=round(best_move, 4),
                        )
                        self.ml_engine.record_decision_result(False, label)

                        # AdaptiveParams TP/SL 데이터
                        await self.adaptive.record_trade({
                            "direction": sig_dir,
                            "pnl_pct": round(pnl, 2),
                            "hold_min": round(elapsed / 60, 1),
                            "exit_reason": f"shadow_{barrier}",
                            "tp1_reach_pct": round(reach, 1),
                            "mae_pct": round(mae, 4),
                            "time_to_first_profit_sec": 0,
                            "entry_atr": atr_pct,
                            "entry_h1_trend": "unknown",
                            "entry_h4_trend": "unknown",
                            "regime": sig.get("regime", "unknown"),
                            "entry_ts": sig_ts,
                            "leverage": leverage,
                        })

                        _append_jsonl({
                            "type": "shadow_result",
                            "signal_id": sig_id,
                            "candidate_type": ctype,
                            "direction": sig_dir,
                            "label": label,
                            "barrier": barrier,
                            "pnl_pct": round(pnl, 2),
                            "entry_price": round(sig_price, 1),
                            "exit_price": round(price, 1),
                            "elapsed_sec": round(elapsed, 0),
                            "reach_pct": round(reach, 1),
                            "mae_pct": round(mae, 4),
                            "best_move_pct": round(best_move, 4),
                        })

                        # 추적 정리
                        shadow_tracking.pop(sig_id, None)

                # 오래된 추적 정리 (resolve된 시그널)
                active_ids = {s["id"] for s in pending}
                for sid in list(shadow_tracking):
                    if sid not in active_ids:
                        shadow_tracking.pop(sid, None)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"shadow check 에러: {e}", exc_info=True)

            await asyncio.sleep(self.config.get("polling", {}).get("shadow_check_sec", 5))

    # ── 주기적 ML 재학습 ──

    async def periodic_ml_retrain(self):
        """ML 재학습 (5분마다 체크)"""
        while self._running:
            try:
                labeled = await self.db.get_labeled_signals(self.ml_engine.window_size)
                self.ml_engine.check_and_train(labeled)
            except Exception as e:
                logger.error(f"ML retrain 에러: {e}", exc_info=True)
            await asyncio.sleep(300)

    # ── 기존 주기적 루프들 ──

    async def periodic_candle_update(self):
        """캔들 REST 백업 (30초마다)"""
        while self._running:
            try:
                for tf in ["1m", "5m", "15m", "1h", "4h", "1d", "1w"]:
                    candles = await self.candle_collector.fetch_candles(tf, limit=5)
                    if candles:
                        await self.db.insert_candles(self.symbol, tf, candles)
            except Exception as e:
                logger.error(f"캔들 REST 에러: {e}")
            await asyncio.sleep(30)

    async def periodic_position_check(self):
        """포지션 SL/TP 체크"""
        while self._running:
            try:
                price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
                if not price_str:
                    price_str = None  # OKX WS가 유일한 가격 소스
                if price_str:
                    price = float(price_str)
                    if self.position_manager.positions:
                        await self.position_manager.check_positions(price)
                    if self.paper_lab and self.paper_lab.has_positions:
                        await self.paper_lab.check_positions(price)

                # 킬스위치
                bot_status = await self.redis.get("sys:bot_status")
                if bot_status == "stopped":
                    logger.warning("킬스위치 → 전 포지션 청산")
                    await self.position_manager.close_all("kill_switch")
            except Exception as e:
                logger.error(f"포지션 체크 에러: {e}")

            interval = 1 if self.position_manager.positions else 5
            await asyncio.sleep(interval)

    async def periodic_daily_reset(self):
        """일일 리셋"""
        last_reset_date = None
        while self._running:
            now_dt = datetime.now(timezone.utc)
            today = now_dt.date()
            if last_reset_date is None:
                last_reset_date = today

            if today > last_reset_date:
                last_reset_date = today
                await self.risk_manager.reset_daily()
                logger.info(f"[DAILY] 리셋 | 어제 P&L: {self.risk_manager.get_daily_pnl():+.1f}%")

                # 일일 리포트
                try:
                    ml_stats = self.ml_engine.get_stats()
                    bal = await self.executor.get_balance()
                    paper_bal = self.paper_lab.balance if self.paper_lab else 0
                    sig_count = await self.db.get_signal_count(labeled_only=True)

                    report = (
                        f"\U0001f4ca <b>Daily Report | {now_dt.strftime('%Y-%m-%d')}</b>\n\n"
                        f"Real Balance: ${bal:,.2f}\n"
                        f"Paper Balance: ${paper_bal:,.0f}\n\n"
                        f"<b>ML Status</b>\n"
                        f"  Phase: {ml_stats['phase']} | OOS: {ml_stats['oos_accuracy']}%\n"
                        f"  Labeled: {sig_count}건 | Go Acc: {ml_stats['recent_go_accuracy']}%\n"
                    )
                    await self.telegram._send(report)
                except Exception as e:
                    logger.debug(f"일일 리포트 실패: {e}")

            await asyncio.sleep(60)

    async def periodic_heartbeat(self):
        """헬스체크 (60초)"""
        while self._running:
            await self.redis.set("sys:last_heartbeat", str(int(_time.time())))
            try:
                bal = await asyncio.wait_for(self.executor.get_balance(), timeout=5.0)
                if bal and bal > 0:
                    await self.redis.set("sys:balance", f"{bal:.2f}")
            except Exception:
                pass

            # 스냅샷 (페이퍼 포함)
            try:
                if self.paper_lab:
                    await self.redis.set("lab:stats", json.dumps(self.paper_lab.get_stats()), ttl=300)

                # 1시간마다 JSONL 기록
                if _time.time() - getattr(self, "_last_snap_jsonl", 0) >= 3600:
                    self._last_snap_jsonl = _time.time()
                    from src.monitoring.trade_logger import _append_jsonl
                    lab_stats = self.paper_lab.get_stats() if self.paper_lab else {}
                    adaptive_stats = self.adaptive.get_stats() if self.adaptive else {}
                    _append_jsonl({
                        "type": "hourly_snapshot",
                        "balance": round(bal, 2) if bal else 0,
                        "autotrading": await self.redis.get("sys:autotrading") or "off",
                        "regime": await self.redis.get("sys:regime") or "unknown",
                        "streak": self.risk_manager.get_streak(),
                        "daily_pnl": round(self.risk_manager.get_daily_pnl(), 2),
                        "positions": len(self.position_manager.positions),
                        "ml_phase": self.ml_engine.phase,
                        "ml_labeled": self.ml_engine.total_labeled,
                        "lab_total": lab_stats.get("total_trades", 0),
                        "lab_best": lab_stats.get("best", {}).get("name", "-") if lab_stats.get("best") else "-",
                        "adaptive_phase": adaptive_stats.get("phase", "collect"),
                        "adaptive_tp_mult": adaptive_stats.get("tp_mult", 1.5),
                    })
            except Exception as e:
                logger.debug(f"스냅샷 에러: {e}")

            await asyncio.sleep(60)

    async def periodic_orphan_algo_sweeper(self):
        """고아 알고 정리 (120초)"""
        while self._running:
            await asyncio.sleep(120)
            try:
                if self.position_manager.positions:
                    continue
                ex_positions = await asyncio.wait_for(self.executor.get_positions(), timeout=5.0)
                has_pos = any(abs(float(p.get("size") or 0)) > 0 for p in ex_positions)
                if has_pos:
                    continue
                cleaned = await self.executor.cancel_all_algos()
                if cleaned:
                    logger.warning(f"고아 알고 {len(cleaned)}개 정리")
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    async def periodic_dashboard_commands(self):
        """대시보드 Redis 명령 큐"""
        while self._running:
            try:
                if not self.redis._client:
                    await asyncio.sleep(5)
                    continue
                raw = await self.redis._client.blpop("cmd:bot", timeout=5)
                if not raw:
                    continue
                _, payload = raw
                cmd = json.loads(payload)
                action = cmd.get("action")
                logger.info(f"[DASH-CMD] {action}: {cmd}")

                if action == "close_all":
                    await self.position_manager.close_all(cmd.get("reason", "dashboard"))
                elif action == "update_sl":
                    await self.position_manager.manual_update_sl(cmd["symbol"], float(cmd["price"]))
                elif action == "update_tp":
                    await self.position_manager.manual_update_tp(cmd["symbol"], float(cmd["price"]))
                elif action == "notify":
                    await self.telegram._send(cmd.get("msg", ""))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[DASH-CMD] 에러: {e}")
                await asyncio.sleep(2)

    # ── 메인 ──

    async def run(self):
        await self.initialize()
        self._running = True

        logger.info("봇 시작 — CandidateDetector v1 + ML Phase " + self.ml_engine.phase)
        await self.redis.set("sys:bot_status", "running")
        await self.redis.set("sys:autotrading", "on")

        # 텔레그램 주입
        self.telegram.redis = self.redis
        self.telegram.executor = self.executor
        self.telegram.position_manager = self.position_manager
        self.telegram.risk_manager = self.risk_manager

        await self.telegram.notify_bot_status("running")
        try:
            bal = await self.executor.get_balance()
            await self.telegram._send(
                f"\U0001f7e2 <b>CryptoAnalyzer v2 — Momentum Scalping</b>\n"
                f"ML: Phase {self.ml_engine.phase} ({self.ml_engine.total_labeled} labeled)\n"
                f"Real: ${bal:,.2f} | Lab: {sum(v.trades for v in self.paper_lab.variants)} trades\n"
                f"Signals: Momentum + Breakout + Cascade"
            )
        except Exception:
            pass

        tasks = [
            asyncio.create_task(self.periodic_candle_update()),
            asyncio.create_task(self.periodic_eval()),
            asyncio.create_task(self.periodic_position_check()),
            asyncio.create_task(self.periodic_shadow_check()),
            asyncio.create_task(self.periodic_ml_retrain()),
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
        logger.info("=== Graceful Shutdown ===")
        try:
            self.signal_tracker.save()
            self.ml_engine._save()
        except Exception as e:
            logger.error(f"종료 저장 실패: {e}")

        for symbol, pos in list(self.position_manager.positions.items()):
            logger.warning(f"미청산: {symbol} {pos.direction.upper()} @ ${pos.entry_price}")

        await self.candle_collector.close()
        await self.executor.close()
        await self.redis.close()
        await self.db.close()
        logger.info("=== Shutdown 완료 ===")


def main():
    bot = CryptoAnalyzer()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown(sig, frame):
        logger.info(f"시그널 {sig} → 종료")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        loop.run_until_complete(bot.run())
    except KeyboardInterrupt:
        logger.info("키보드 인터럽트")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
