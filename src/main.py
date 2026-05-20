"""
ScalpEngine v3 — BTC 마이크로스트럭처 스캘핑 엔진

4계층 파이프라인:
  [1] Raw Data (Binance aggTrade + OKX trades/books/tickers)
  [2] Feature Engine (OFI, CVD, VPIN, Hurst, Welford Z-Score)
  [3] Regime Gate (Hurst → momentum/mean_revert/random_walk)
  [4] ML Scorer (XGBoost → 신뢰도 ≥ 0.55 게이팅)
  → Scalp Execute (TP 0.20% / SL 0.15% / Time 3~5분)
"""

import asyncio
import json
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
from src.data.binance_stream import BinanceStream
from src.strategy.scalp_detector import ScalpDetector
from src.strategy.scalp_manager import ScalpManager
from src.strategy.ml_engine import MLDecisionEngine
from src.strategy.adaptive_params import AdaptiveParams
from src.trading.risk_manager import RiskManager
from src.trading.executor import OrderExecutor
from src.monitoring.telegram_bot import TelegramNotifier
from src.monitoring.trade_logger import TradeLogger, _append_jsonl

import os
os.environ["TZ"] = "Asia/Seoul"
try:
    _time.tzset()
except AttributeError:
    pass

# ── 로깅 ──
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
from logging.handlers import TimedRotatingFileHandler as _TRH
_P = Path
_log_dir = _P(__file__).parent.parent / "data" / "logs"
_log_dir.mkdir(parents=True, exist_ok=True)
_fh = _TRH(_log_dir / "bot.log", when="W0", backupCount=520, encoding="utf-8", utc=True)
_fh.suffix = "%Y-W%W"
_fh.setLevel(logging.WARNING)
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logging.getLogger().addHandler(_fh)

logger = logging.getLogger("ScalpEngine")


class ScalpEngine:
    """BTC 마이크로스트럭처 스캘핑 엔진"""

    def __init__(self):
        load_env()
        self.config = load_config()
        self.symbol = self.config["exchange"]["symbol"]
        scalp_cfg = self.config.get("scalp", {})

        # 인프라
        self.db = Database()
        self.redis = RedisClient()
        self.candle_collector = CandleCollector(self.db)
        self.ws_stream = WebSocketStream(self.redis, db=self.db)
        self.binance_stream = BinanceStream(self.redis)

        # 스캘핑 전략
        self.scalp_detector = ScalpDetector(redis=self.redis, config=self.config)
        self.scalp_manager = None  # initialize()에서 생성
        self.ml_engine = MLDecisionEngine(config=self.config)
        self.adaptive = AdaptiveParams(config=self.config, redis=self.redis)

        # 매매 엔진
        self.executor = OrderExecutor()
        self.risk_manager = RiskManager(self.redis, executor=self.executor)

        # 모니터링
        self.telegram = TelegramNotifier()
        self.trade_logger = TradeLogger()

        # 상태
        self._running = False
        self._last_trade_time = 0
        self._trades_this_hour = 0
        self._hour_reset_ts = 0

        # 리스크 설정
        self.min_entry_interval = self.config.get("risk", {}).get("min_entry_interval_sec", 60)

        # Shadow phase
        self.shadow_mode = scalp_cfg.get("shadow_mode", True)  # 초기: Shadow only

    async def initialize(self):
        logger.info("=" * 50)
        logger.info("ScalpEngine v3 — Microstructure Scalping")
        logger.info("=" * 50)

        await self.db.connect()
        await self.redis.connect()
        await self.candle_collector.init_exchange()
        await self.telegram.initialize()
        await self.executor.initialize()

        # ML
        self.ml_engine.on_phase_change = self._on_ml_phase_change
        logger.info(f"ML: Phase {self.ml_engine.phase}, labeled={self.ml_engine.total_labeled}")

        # AdaptiveParams
        await self.adaptive.load_state()

        # 잔고 + 리스크
        balance = await self.executor.get_balance()
        await self.risk_manager.initialize(balance)
        logger.info(f"잔고: ${balance:.2f}")

        # ScalpManager
        self.scalp_manager = ScalpManager(
            executor=self.executor,
            db=self.db,
            redis=self.redis,
            telegram=self.telegram,
            config=self.config,
        )

        # 캔들 백필 (Hurst 계산용)
        logger.info("캔들 백필 시작...")
        await self.candle_collector.backfill_all()
        logger.info("캔들 백필 완료")

        mode = "SHADOW" if self.shadow_mode else "LIVE"
        logger.info(f"스캘핑 모드: {mode}")

    # ══════════════════════════════════════════════════
    #  스캘핑 평가 루프 (500ms)
    # ══════════════════════════════════════════════════

    async def periodic_scalp_eval(self):
        """500ms — 시그널 감지 + ML + 실행"""
        await asyncio.sleep(10)  # WS 안정화 대기
        logger.info("[SCALP] 평가 루프 시작 (500ms)")

        while self._running:
            try:
                await self._scalp_evaluate()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[SCALP] eval 에러: {e}", exc_info=True)
            await asyncio.sleep(0.5)

    async def _scalp_evaluate(self):
        now = _time.time()

        # 포지션 있으면 스킵
        if self.scalp_manager.has_position():
            return

        # autotrading 체크
        autotrading = (await self.redis.get("sys:autotrading") or "off") == "on"

        # 리스크 게이트
        allowed, reason = self.risk_manager.is_trading_allowed()
        if not allowed:
            return

        # 진입 간격
        if now - self._last_trade_time < self.min_entry_interval:
            return

        # ── Redis 상태 갱신 (텔레그램/대시보드용, 30초마다) ──
        if now - getattr(self, "_last_state_flush", 0) >= 30:
            self._last_state_flush = now
            try:
                hurst_val = await self.redis.get("rt:regime:hurst")
                vpin_val = await self.redis.get("rt:micro:vpin")
                regime = "scalp"
                if hurst_val:
                    h = float(hurst_val)
                    if h > 0.6: regime = "momentum"
                    elif h < 0.4: regime = "mean_revert"
                    elif 0.45 <= h <= 0.55: regime = "dead_zone"
                    else: regime = "neutral"
                await self.redis.set("sys:regime", regime, ttl=60)
                await self.redis.set("sys:trade_state", json.dumps({
                    "mode": "shadow" if self.shadow_mode else "live",
                    "regime": regime,
                    "hurst": hurst_val or "N/A",
                    "vpin": vpin_val or "N/A",
                    "streak": self.risk_manager.get_streak(),
                    "trades_hour": self._trades_this_hour,
                }), ttl=60)
            except Exception:
                pass

        # ── 시그널 감지 ──
        signal = await self.scalp_detector.evaluate()
        if not signal:
            # 60초마다 상태 로그
            if now - getattr(self, "_last_eval_debug", 0) >= 60:
                self._last_eval_debug = now
                try:
                    burst = await self.redis.get("rt:micro:trade_burst")
                    hurst = await self.redis.get("rt:regime:hurst")
                    vel = await self.redis.hgetall("rt:velocity:BTC-USDT-SWAP")
                    move10 = vel.get("move_10s", "0")
                    price = await self.redis.get("rt:price:BTC-USDT-SWAP")
                    logger.info(
                        f"[EVAL] 시그널 없음 | price=${price} move10s=${move10} "
                        f"burst={burst} hurst={hurst or 'N/A'}"
                    )
                except Exception:
                    pass
            return

        direction = signal["direction"]
        sig_type = signal["type"]
        strength = signal.get("strength", 1.0)
        price = signal["price"]
        features = signal.get("features", {})

        # ── DB 기록 (모든 시그널) ──
        sig_record = {
            "ts": int(now),
            "signal_type": sig_type,
            "direction": direction,
            "price": price,
            "features": json.dumps(features),
            "regime": signal.get("regime", "unknown"),
            "hurst": signal.get("hurst", 0.5),
            "vpin": signal.get("vpin", 0.3),
            "ml_prob": -1.0,
            "ml_go": -1,
            "entry_executed": 0,
            "reject_reason": None,
        }

        # ── ML Go/NoGo ──
        go, prob = self.ml_engine.decide(features)
        sig_record["ml_go"] = 1 if go else 0
        sig_record["ml_prob"] = round(prob, 4) if prob >= 0 else -1.0

        if not go:
            sig_record["reject_reason"] = "ml_nogo"

        sig_id = await self.db.insert_scalp_signal(sig_record)
        signal["signal_id"] = sig_id

        # JSONL
        _append_jsonl({
            "type": "candidate",
            "signal_type": sig_type,
            "direction": direction,
            "strength": round(strength, 2),
            "price": round(price, 1),
            "ml_go": 1 if go else 0,
            "ml_prob": round(prob, 4) if prob >= 0 else -1,
            "regime": signal.get("regime", "unknown"),
            "hurst": round(signal.get("hurst", 0.5), 4),
            "vpin": round(signal.get("vpin", 0.3), 4),
        })

        if not go:
            return

        # ── Shadow 모드: 진입 안 함 ──
        if self.shadow_mode:
            return

        # ── Shadow WR 게이트 ──
        if now - getattr(self, "_last_shadow_wr_check", 0) >= 300:
            self._last_shadow_wr_check = now
            try:
                wr, cnt = await self.db.get_recent_shadow_wr(hours=4)
                self._cached_shadow_wr = wr
                self._cached_shadow_cnt = cnt
            except Exception:
                pass

        shadow_wr = getattr(self, "_cached_shadow_wr", 50.0)
        shadow_cnt = getattr(self, "_cached_shadow_cnt", 0)
        if shadow_cnt >= 30 and shadow_wr < 20:
            logger.info(f"[GATE] Shadow WR {shadow_wr:.1f}% < 20% → 매매 정지")
            _append_jsonl({
                "type": "gate_block", "reason": "shadow_wr_low",
                "detail": f"WR={shadow_wr:.1f}% cnt={shadow_cnt}",
                "direction": direction, "signal_type": sig_type,
            })
            return

        # ── 실거래 실행 ──
        if not autotrading:
            return

        balance = await self.executor.get_balance()
        if balance <= 0:
            return

        pos = await self.scalp_manager.open_scalp(signal, balance)
        if pos:
            self._last_trade_time = now
            self._trades_this_hour += 1
            await self.db.update_signal_entry(sig_id)

            # 텔레그램
            try:
                await self.telegram._send(
                    f"⚡ <b>SCALP {pos.direction.upper()}</b>\n"
                    f"${pos.entry_price:,.0f} | TP ${pos.tp_price:,.0f} SL ${pos.sl_price:,.0f}\n"
                    f"Size: {pos.size} BTC | {pos.leverage}x"
                )
            except Exception:
                pass

    # ══════════════════════════════════════════════════
    #  스캘핑 포지션 관리 루프 (500ms)
    # ══════════════════════════════════════════════════

    async def periodic_scalp_position(self):
        """500ms — 포지션 관리 (시간정지 + failsafe + self-heal)"""
        while self._running:
            try:
                if self.scalp_manager.has_position():
                    price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
                    if price_str:
                        price = float(price_str)
                        prev_pos = self.scalp_manager.position
                        await self.scalp_manager.check_position(price)

                        # 포지션이 청산됐으면 후처리
                        if prev_pos and not self.scalp_manager.has_position():
                            await self._on_scalp_closed(prev_pos)

                    # 킬스위치
                    bot_status = await self.redis.get("sys:bot_status")
                    if bot_status == "stopped" and self.scalp_manager.has_position():
                        logger.warning("킬스위치 → 스캘핑 포지션 청산")
                        pos = self.scalp_manager.position
                        hold_sec = _time.time() - pos.entry_time
                        await self.scalp_manager._close_and_finalize(pos, "kill_switch", hold_sec)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[SCALP] position 에러: {e}", exc_info=True)

            interval = 0.5 if self.scalp_manager.has_position() else 2.0
            await asyncio.sleep(interval)

    async def _on_scalp_closed(self, pos):
        """스캘핑 청산 후처리 — 리스크 + ML + 쿨다운"""
        # PnL은 ScalpManager._finalize에서 이미 DB/JSONL 기록됨
        # 여기서는 리스크 매니저 + ML + 쿨다운만 처리

        # 쿨다운
        pnl = 0  # 추정 (pos는 이미 None이므로 DB에서 조회)
        try:
            # 마지막 거래 조회
            cursor = await self.db._db.execute(
                "SELECT pnl_pct, pnl_usdt, exit_reason FROM scalp_trades ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            if row:
                pnl_pct = row["pnl_pct"] or 0
                pnl_usdt = row["pnl_usdt"] or 0
                exit_reason = row["exit_reason"] or ""

                await self.risk_manager.record_trade_result(pnl_pct, pnl_usdt)

                # ML 결과 기록
                label = 1 if pnl_pct > 0 else 0
                self.ml_engine.record_decision_result(True, label)

                logger.info(
                    f"[SCALP] 후처리: PnL {pnl_pct:+.1f}% ${pnl_usdt:+.2f} | "
                    f"연패:{self.risk_manager.get_streak()}"
                )
        except Exception as e:
            logger.error(f"청산 후처리 실패: {e}")

    # ══════════════════════════════════════════════════
    #  Shadow 추적
    # ══════════════════════════════════════════════════

    async def periodic_shadow_check(self):
        """모든 시그널의 Triple Barrier 라벨링 (스캘핑 배리어)"""
        shadow_tracking: dict[int, dict] = {}
        scalp_cfg = self.config.get("scalp", {})
        tp_pct = scalp_cfg.get("tp_price_pct", 0.20) / 100
        sl_pct = scalp_cfg.get("sl_price_pct", 0.15) / 100
        max_hold = scalp_cfg.get("time_stop_max_sec", 300)
        leverage = scalp_cfg.get("leverage", 20)

        while self._running:
            try:
                price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
                if not price_str:
                    await asyncio.sleep(5)
                    continue
                price = float(price_str)
                now = _time.time()

                pending = await self.db.get_pending_shadows()

                for sig in pending:
                    sig_id = sig["id"]
                    sig_ts = sig["ts"]
                    sig_price = sig["price"]
                    sig_dir = sig["direction"]

                    if sig_id not in shadow_tracking:
                        shadow_tracking[sig_id] = {"best": sig_price, "worst": sig_price}
                    track = shadow_tracking[sig_id]

                    if sig_dir == "long":
                        track["best"] = max(track["best"], price)
                        track["worst"] = min(track["worst"], price)
                    else:
                        track["best"] = min(track["best"], price)
                        track["worst"] = max(track["worst"], price)

                    # 스캘핑 배리어
                    tp_dist = sig_price * tp_pct
                    sl_dist = sig_price * sl_pct

                    if sig_dir == "long":
                        hit_tp = price >= sig_price + tp_dist
                        hit_sl = price <= sig_price - sl_dist
                    else:
                        hit_tp = price <= sig_price - tp_dist
                        hit_sl = price >= sig_price + sl_dist

                    elapsed = now - sig_ts
                    label = -1
                    barrier = None
                    pnl = 0.0

                    if hit_tp:
                        label = 1
                        barrier = "tp"
                        pnl = tp_pct * leverage * 100
                    elif hit_sl:
                        label = 0
                        barrier = "sl"
                        pnl = -(sl_pct * leverage * 100)
                    elif elapsed >= max_hold:
                        barrier = "time"
                        if sig_dir == "long":
                            pnl = (price - sig_price) / sig_price * leverage * 100
                        else:
                            pnl = (sig_price - price) / sig_price * leverage * 100
                        label = 1 if pnl > 0 else 0

                    if label >= 0:
                        if sig_dir == "long":
                            best_move = (track["best"] - sig_price) / sig_price * 100
                            mae = (sig_price - track["worst"]) / sig_price * 100
                        else:
                            best_move = (sig_price - track["best"]) / sig_price * 100
                            mae = (track["worst"] - sig_price) / sig_price * 100
                        reach = best_move / (tp_pct * 100) * 100 if tp_pct > 0 else 0

                        await self.db.update_signal_label(
                            sig_id, label, barrier, round(pnl, 2), int(now),
                            reach_pct=round(reach, 1), mae_pct=round(mae, 4),
                        )

                        _append_jsonl({
                            "type": "shadow_result",
                            "signal_id": sig_id,
                            "signal_type": sig.get("signal_type", "unknown"),
                            "direction": sig_dir,
                            "label": label,
                            "barrier": barrier,
                            "pnl_pct": round(pnl, 2),
                            "entry_price": round(sig_price, 1),
                            "exit_price": round(price, 1),
                            "elapsed_sec": round(elapsed, 0),
                            "reach_pct": round(reach, 1),
                            "mae_pct": round(mae, 4),
                        })

                        shadow_tracking.pop(sig_id, None)

                # 오래된 추적 정리
                active_ids = {s["id"] for s in pending}
                for sid in list(shadow_tracking):
                    if sid not in active_ids:
                        shadow_tracking.pop(sid, None)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"shadow check 에러: {e}", exc_info=True)

            await asyncio.sleep(self.config.get("polling", {}).get("shadow_check_sec", 5))

    # ══════════════════════════════════════════════════
    #  ML 재학습
    # ══════════════════════════════════════════════════

    async def periodic_ml_retrain(self):
        while self._running:
            try:
                labeled = await self.db.get_labeled_signals(self.ml_engine.window_size)
                self.ml_engine.check_and_train(labeled)
            except Exception as e:
                logger.error(f"ML retrain 에러: {e}")
            await asyncio.sleep(300)

    # ══════════════════════════════════════════════════
    #  지원 루프들
    # ══════════════════════════════════════════════════

    async def periodic_candle_update(self):
        """캔들 REST 백업 (30초, Hurst/Parkinson용)"""
        while self._running:
            try:
                for tf in ["1m", "5m", "15m", "1h", "4h", "1d", "1w"]:
                    candles = await self.candle_collector.fetch_candles(tf, limit=5)
                    if candles:
                        await self.db.insert_candles(self.symbol, tf, candles)
            except Exception as e:
                logger.error(f"캔들 REST 에러: {e}")
            await asyncio.sleep(30)

    async def periodic_daily_reset(self):
        last_reset_date = None
        while self._running:
            now_dt = datetime.now(timezone.utc)
            today = now_dt.date()
            if last_reset_date is None:
                last_reset_date = today

            if today > last_reset_date:
                last_reset_date = today
                await self.risk_manager.reset_daily()

                try:
                    bal = await self.executor.get_balance()
                    ml_stats = self.ml_engine.get_stats()
                    sig_count = await self.db.get_signal_count(labeled_only=True)
                    shadow_wr, shadow_cnt = await self.db.get_recent_shadow_wr(hours=24)

                    report = (
                        f"\U0001f4ca <b>Daily Report | {now_dt.strftime('%Y-%m-%d')}</b>\n\n"
                        f"Balance: ${bal:,.2f}\n"
                        f"ML: Phase {ml_stats['phase']} | OOS {ml_stats['oos_accuracy']}%\n"
                        f"Shadow: {sig_count}건 labeled | 24h WR {shadow_wr:.1f}% ({shadow_cnt}건)\n"
                        f"Mode: {'SHADOW' if self.shadow_mode else 'LIVE'}"
                    )
                    await self.telegram._send(report)
                except Exception:
                    pass

            await asyncio.sleep(60)

    async def periodic_heartbeat(self):
        while self._running:
            await self.redis.set("sys:last_heartbeat", str(int(_time.time())), ttl=120)
            try:
                bal = await asyncio.wait_for(self.executor.get_balance(), timeout=5.0)
                if bal and bal > 0:
                    await self.redis.set("sys:balance", f"{bal:.2f}", ttl=300)
            except Exception:
                pass

            # 1시간마다 스냅샷
            if _time.time() - getattr(self, "_last_snap", 0) >= 3600:
                self._last_snap = _time.time()
                try:
                    bal = float(await self.redis.get("sys:balance") or 0)
                    hurst = await self.redis.get("rt:regime:hurst")
                    _append_jsonl({
                        "type": "hourly_snapshot",
                        "balance": round(bal, 2),
                        "regime": "scalp",
                        "hurst": round(float(hurst), 4) if hurst else 0.5,
                        "ml_phase": self.ml_engine.phase,
                        "ml_labeled": self.ml_engine.total_labeled,
                        "streak": self.risk_manager.get_streak(),
                        "mode": "shadow" if self.shadow_mode else "live",
                        "trades_hour": self._trades_this_hour,
                    })
                except Exception:
                    pass

            await asyncio.sleep(60)

    async def periodic_orphan_algo_sweeper(self):
        while self._running:
            await asyncio.sleep(120)
            try:
                if self.scalp_manager.has_position():
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
                logger.info(f"[CMD] {action}: {cmd}")

                if action == "close_all" and self.scalp_manager.has_position():
                    pos = self.scalp_manager.position
                    hold_sec = _time.time() - pos.entry_time
                    await self.scalp_manager._close_and_finalize(pos, "dashboard", hold_sec)
                elif action == "notify":
                    await self.telegram._send(cmd.get("msg", ""))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[CMD] 에러: {e}")
                await asyncio.sleep(2)

    # ── ML Phase 전환 알림 ──

    async def _on_ml_phase_change(self, old_phase: str, new_phase: str, details: str):
        icons = {"A": "\U0001f7e1", "B": "\U0001f7e2", "B+": "\U0001f4a1"}
        icon = icons.get(new_phase, "\u26a0\ufe0f")
        msg = f"{icon} <b>ML Phase: {old_phase} → {new_phase}</b>\n{details}"
        try:
            await self.telegram._send(msg)
        except Exception:
            pass

    # ══════════════════════════════════════════════════
    #  메인
    # ══════════════════════════════════════════════════

    async def run(self):
        await self.initialize()
        self._running = True

        logger.info("봇 시작 — ScalpEngine v3")
        await self.redis.set("sys:bot_status", "running")
        await self.redis.set("sys:autotrading", "on")

        self.telegram.redis = self.redis
        self.telegram.executor = self.executor
        self.telegram.scalp_manager = self.scalp_manager
        self.telegram.risk_manager = self.risk_manager

        await self.telegram.notify_bot_status("running")
        try:
            bal = await self.executor.get_balance()
            mode = "SHADOW" if self.shadow_mode else "LIVE"
            await self.telegram._send(
                f"\U0001f7e2 <b>ScalpEngine v3 — Microstructure Scalping</b>\n"
                f"Mode: {mode} | ML Phase {self.ml_engine.phase}\n"
                f"Balance: ${bal:,.2f} | Leverage: {self.config.get('scalp', {}).get('leverage', 20)}x\n"
                f"TP: +{self.config.get('scalp', {}).get('tp_price_pct', 0.20)}% | "
                f"SL: -{self.config.get('scalp', {}).get('sl_price_pct', 0.15)}%"
            )
        except Exception:
            pass

        tasks = [
            # 데이터
            asyncio.create_task(self.ws_stream.start()),
            asyncio.create_task(self.binance_stream.start()),
            asyncio.create_task(self.periodic_candle_update()),
            # 스캘핑
            asyncio.create_task(self.periodic_scalp_eval()),
            asyncio.create_task(self.periodic_scalp_position()),
            # Shadow + ML
            asyncio.create_task(self.periodic_shadow_check()),
            asyncio.create_task(self.periodic_ml_retrain()),
            # 지원
            asyncio.create_task(self.periodic_daily_reset()),
            asyncio.create_task(self.periodic_heartbeat()),
            asyncio.create_task(self.periodic_orphan_algo_sweeper()),
            asyncio.create_task(self.periodic_dashboard_commands()),
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
            await self.redis.set("sys:bot_status", "stopped")
            await self.telegram.notify_bot_status("stopped")
            await self.cleanup()

    async def cleanup(self):
        logger.info("=== Graceful Shutdown ===")
        try:
            self.ml_engine._save()
        except Exception as e:
            logger.error(f"종료 저장 실패: {e}")
        await self.candle_collector.close()
        await self.executor.close()
        await self.redis.close()
        await self.db.close()


# ── 엔트리포인트 ──

async def main():
    engine = ScalpEngine()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown(engine)))
        except NotImplementedError:
            pass

    await engine.run()


async def shutdown(engine):
    logger.info("종료 신호 수신")
    engine._running = False


if __name__ == "__main__":
    asyncio.run(main())
