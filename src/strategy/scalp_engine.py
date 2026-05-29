"""
ScalpEngine — Jay 단타법 (볼린저밴드 + StochRSI + MACD)

BTC 무기한 선물 단타 매매. 후행 확인 후 진입, 먹고 나감.
파라미터 시작 시 1회 설정.

매매 로직:
  타점: 볼린저밴드 상단(숏) / 하단(롱) 에서만 진입
  신호: StochRSI 크로스 + MACD 크로스
  금지: BB 중간(20이평) 근처 매매 금지
  청산: StochRSI 반대편 도달
  SL: BB 밴드 이탈 (롱→하단 이탈, 숏→상단 이탈)
"""

import asyncio
import json
import logging
import time

import numpy as np

from src.strategy.indicators import stoch_rsi, macd, atr, bollinger_bands
from src.strategy.scalp_state import (
    ScalpState, save_scalp_state, load_scalp_state,
)
from src.trading.executor import OrderExecutor
from src.data.storage import Database, RedisClient
from src.monitoring.trade_logger import _append_jsonl

logger = logging.getLogger(__name__)


class ScalpEngine:
    """Jay 단타법 — StochRSI + MACD 후행 확인 매매"""

    def __init__(self, executor: OrderExecutor, db: Database,
                 redis: RedisClient, telegram=None, risk_manager=None,
                 config: dict = None):
        self.executor = executor
        self.db = db
        self.redis = redis
        self.telegram = telegram
        self.risk_manager = risk_manager

        cfg = (config or {}).get("scalp", {})
        self.enabled = cfg.get("enabled", False)
        self.leverage = cfg.get("leverage", 10)
        self.timeframe = cfg.get("timeframe", "1h")
        self.size_btc = cfg.get("size_btc", 0.01)
        self.signal_timeout = cfg.get("signal_timeout_candles", 3)
        self.cooldown_sec = cfg.get("cooldown_sec", 0)

        # StochRSI
        self.srsi_period = cfg.get("stoch_rsi_period", 14)
        self.srsi_k_smooth = cfg.get("stoch_rsi_k", 3)
        self.srsi_d_smooth = cfg.get("stoch_rsi_d", 3)
        self.srsi_ob = cfg.get("stoch_rsi_ob", 80)
        self.srsi_os = cfg.get("stoch_rsi_os", 20)

        # MACD (Jay: fast=8)
        self.macd_fast = cfg.get("macd_fast", 8)
        self.macd_slow = cfg.get("macd_slow", 26)
        self.macd_signal = cfg.get("macd_signal", 9)

        # 볼린저밴드 (타점)
        self.bb_period = cfg.get("bb_period", 20)
        self.bb_std = cfg.get("bb_std", 2.0)
        self.bb_mid_avoid_pct = cfg.get("bb_mid_avoid_pct", 30)  # BB 중간 ±30% 구간 매매 금지

        # BB 밴드 값 (SL 체크용, 캔들 닫힐 때 갱신)
        self._bb_upper = 0.0
        self._bb_lower = 0.0
        self._bb_middle = 0.0

        # 안전장치
        safety = (config or {}).get("safety", {})
        self.cb_pct = safety.get("circuit_breaker_pct", 2.0)
        self.cb_window = safety.get("circuit_breaker_window_sec", 10)
        self.cb_freeze = safety.get("circuit_breaker_freeze_sec", 60)
        self.bot_kill_pct = safety.get("bot_kill_drawdown_pct", 20)

        self.symbol = (config or {}).get("exchange", {}).get("symbol", "BTC/USDT:USDT")
        self.taker_fee = (config or {}).get("fees", {}).get("taker", 0.0005)

        self.state: ScalpState | None = None
        self._running = True
        self._lock = asyncio.Lock()
        self._peak_balance = 0.0
        self._last_kill_check = 0.0
        self._cb_frozen_until = 0.0
        self._price_history: list[tuple[float, float]] = []
        self._last_candle_ts = 0

    # ══════════════════════════════════════════
    #  메인 루프
    # ══════════════════════════════════════════

    async def run(self):
        if not self.enabled:
            logger.info("[SCALP] 비활성화 (scalp.enabled=false)")
            return

        balance = await self.executor.get_balance()
        self._peak_balance = balance

        # 크래시 복구 (고아 포지션 먼저 정리)
        await self._recover()
        if not self.state:
            self.state = ScalpState(
                is_active=True, created_at=time.time(),
                peak_balance=balance,
            )

        # 레버리지 설정 (복구 후 — 포지션 있으면 설정 불가)
        try:
            await self.executor.set_leverage(self.leverage, "long")
            await self.executor.set_leverage(self.leverage, "short")
        except Exception as e:
            logger.error(f"[SCALP] 레버리지 설정 실패: {e}")

        logger.info(
            f"[SCALP] 엔진 시작 | TF={self.timeframe} | {self.leverage}x | "
            f"size={self.size_btc}BTC | StochRSI({self.srsi_period},{self.srsi_k_smooth},{self.srsi_d_smooth}) | "
            f"MACD({self.macd_fast},{self.macd_slow},{self.macd_signal})"
        )

        # Redis pub/sub 구독 — 캔들 닫힘 이벤트
        pubsub = self.redis._client.pubsub()
        await pubsub.subscribe("ch:kline:ready")

        while self._running:
            try:
                # 캔들 닫힘 이벤트 수신
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if msg and msg["type"] == "message":
                    try:
                        data = json.loads(msg["data"])
                    except (json.JSONDecodeError, TypeError):
                        data = {}

                    if data.get("tf") == self.timeframe:
                        candle_ts = data.get("ts", 0)
                        if candle_ts != self._last_candle_ts:
                            self._last_candle_ts = candle_ts
                            async with self._lock:
                                await self._on_candle_close()

                now = time.time()

                # BOT_KILL 체크 (30초)
                if now - self._last_kill_check >= 30:
                    self._last_kill_check = now
                    await self._check_bot_kill()

                # SL 체크 — 서킷브레이커 동결 중에도 항상 동작
                if self.state and self.state.position != "flat":
                    await self._check_stop_loss()

                # 서킷브레이커 가격 모니터링
                await self._update_price_history()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[SCALP] loop error: {e}", exc_info=True)

            await asyncio.sleep(0.5)

        try:
            await pubsub.unsubscribe("ch:kline:ready")
        except Exception:
            pass

    # ══════════════════════════════════════════
    #  신호 평가 (캔들 닫힐 때마다)
    # ══════════════════════════════════════════

    async def _on_candle_close(self):
        """캔들 닫힐 때 BB + StochRSI + MACD 평가"""
        # 서킷브레이커 동결 중 신규 진입 차단 (SL/청산은 별도 루프에서 동작)
        if time.time() < self._cb_frozen_until:
            logger.info("[SCALP] 서킷브레이커 동결 중 — 신호 평가 스킵")
            return

        candles = await self.db.get_candles(self.symbol, self.timeframe, limit=100)
        if len(candles) < 60:
            logger.warning(f"[SCALP] 캔들 부족: {len(candles)}개 (최소 60)")
            return

        closes = np.array([c["close"] for c in candles], dtype=float)
        highs = np.array([c["high"] for c in candles], dtype=float)
        lows = np.array([c["low"] for c in candles], dtype=float)
        price = closes[-1]

        # ── 지표 계산 ──
        k_line, d_line = stoch_rsi(
            closes, self.srsi_period, self.srsi_period,
            self.srsi_k_smooth, self.srsi_d_smooth
        )
        macd_line, signal_line, _ = macd(
            closes, self.macd_fast, self.macd_slow, self.macd_signal
        )
        bb_upper, bb_middle, bb_lower = bollinger_bands(
            closes, self.bb_period, self.bb_std
        )

        # NaN 체크
        if np.isnan(k_line[-1]) or np.isnan(k_line[-2]):
            logger.debug("[SCALP] 지표 NaN — 데이터 부족")
            return
        if np.isnan(macd_line[-1]) or np.isnan(macd_line[-2]):
            return
        if np.isnan(signal_line[-1]) or np.isnan(signal_line[-2]):
            return
        if np.isnan(bb_upper[-1]) or np.isnan(bb_lower[-1]):
            return

        k_now, k_prev = k_line[-1], k_line[-2]
        d_now, d_prev = d_line[-1], d_line[-2]
        m_now, m_prev = macd_line[-1], macd_line[-2]
        s_now, s_prev = signal_line[-1], signal_line[-2]

        # BB 값 저장 (SL 체크용)
        self._bb_upper = bb_upper[-1]
        self._bb_middle = bb_middle[-1]
        self._bb_lower = bb_lower[-1]

        # 크로스 감지
        srsi_golden = (k_prev <= d_prev) and (k_now > d_now)
        srsi_death = (k_prev >= d_prev) and (k_now < d_now)
        macd_golden = (m_prev <= s_prev) and (m_now > s_now)
        macd_death = (m_prev >= s_prev) and (m_now < s_now)

        srsi_bottom = k_now < self.srsi_os  # < 20
        srsi_top = k_now > self.srsi_ob     # > 80

        # BB 위치 판단
        bb_range = self._bb_upper - self._bb_lower
        if bb_range > 0:
            bb_position = (price - self._bb_lower) / bb_range * 100  # 0=하단, 100=상단
        else:
            bb_position = 50

        # BB 중간 구간 = 매매 금지 (Jay: "20이평만 피해서 매매하면 돈을 번다")
        mid_low = 50 - self.bb_mid_avoid_pct / 2   # 35
        mid_high = 50 + self.bb_mid_avoid_pct / 2   # 65
        in_bb_middle = mid_low < bb_position < mid_high

        # BB 상하단 근처 판단
        near_bb_lower = bb_position < mid_low      # 하단 구간 (롱 타점)
        near_bb_upper = bb_position > mid_high     # 상단 구간 (숏 타점)

        logger.info(
            f"[SCALP] 지표 | K={k_now:.1f} D={d_now:.1f} "
            f"MACD={m_now:.1f} Sig={s_now:.1f} | "
            f"BB={bb_position:.0f}% (${self._bb_lower:,.0f}-${self._bb_upper:,.0f}) | "
            f"pos={self.state.position} pending={self.state.pending_signal}"
        )

        # ── 청산 체크 (진입보다 우선) ──
        if self.state.position == "long" and srsi_top:
            await self._close_position("stoch_rsi_top")
            return

        if self.state.position == "short" and srsi_bottom:
            await self._close_position("stoch_rsi_bottom")
            return

        # ── 이미 포지션 있으면 진입 불가 ──
        if self.state.position != "flat":
            await save_scalp_state(self.redis, self.state)
            return

        # 쿨다운 체크
        if self.cooldown_sec > 0:
            if time.time() - self.state.last_trade_time < self.cooldown_sec:
                return

        # BB 중간 매매 금지
        if in_bb_middle:
            # 대기 신호도 BB 중간이면 진입 안 함 (등록만 유지)
            if self.state.pending_signal:
                self.state.signal_candle_count += 1
                if self.state.signal_candle_count > self.signal_timeout:
                    self.state.pending_signal = None
            await save_scalp_state(self.redis, self.state)
            return

        # ── 롱 신호 (BB 하단 근처에서만) ──
        if near_bb_lower:
            if self._check_long_entry(srsi_golden, srsi_bottom, macd_golden, k_now):
                return

        # ── 숏 신호 (BB 상단 근처에서만) ──
        if near_bb_upper:
            if self._check_short_entry(srsi_death, srsi_top, macd_death, k_now):
                return

        # ── 대기 신호 타임아웃 ──
        if self.state.pending_signal:
            self.state.signal_candle_count += 1
            if self.state.signal_candle_count > self.signal_timeout:
                logger.info(f"[SCALP] 대기 신호 타임아웃: {self.state.pending_signal}")
                self.state.pending_signal = None

        await save_scalp_state(self.redis, self.state)

    def _check_long_entry(self, srsi_golden: bool, srsi_bottom: bool,
                          macd_golden: bool, k_now: float) -> bool:
        """롱 진입 조건 체크. 진입 시 True 반환."""
        # 소진 필터: K가 이미 70+ 이면 스킵
        exhausted = k_now > 70

        # 동시 크로스
        if srsi_golden and srsi_bottom and macd_golden and not exhausted:
            asyncio.create_task(self._open_position("long"))
            return True

        # StochRSI 먼저 → MACD 대기 중 → MACD 크로스
        if (self.state.pending_signal == "long_wait_macd"
                and macd_golden and not exhausted):
            self.state.pending_signal = None
            asyncio.create_task(self._open_position("long"))
            return True

        # MACD 먼저 → StochRSI 대기 중 → StochRSI 크로스
        if (self.state.pending_signal == "long_wait_srsi"
                and srsi_golden and srsi_bottom and not exhausted):
            self.state.pending_signal = None
            asyncio.create_task(self._open_position("long"))
            return True

        # 대기 신호 등록
        if srsi_golden and srsi_bottom and not macd_golden:
            self.state.pending_signal = "long_wait_macd"
            self.state.signal_candle_count = 0
            logger.info("[SCALP] StochRSI 롱 크로스 감지 → MACD 대기")

        if macd_golden and not (srsi_golden and srsi_bottom):
            # 기존 숏 대기가 있으면 덮어쓰지 않음
            if not self.state.pending_signal or "long" in (self.state.pending_signal or ""):
                self.state.pending_signal = "long_wait_srsi"
                self.state.signal_candle_count = 0
                logger.info("[SCALP] MACD 롱 크로스 감지 → StochRSI 대기")

        return False

    def _check_short_entry(self, srsi_death: bool, srsi_top: bool,
                           macd_death: bool, k_now: float) -> bool:
        """숏 진입 조건 체크. 진입 시 True 반환."""
        exhausted = k_now < 30

        if srsi_death and srsi_top and macd_death and not exhausted:
            asyncio.create_task(self._open_position("short"))
            return True

        if (self.state.pending_signal == "short_wait_macd"
                and macd_death and not exhausted):
            self.state.pending_signal = None
            asyncio.create_task(self._open_position("short"))
            return True

        if (self.state.pending_signal == "short_wait_srsi"
                and srsi_death and srsi_top and not exhausted):
            self.state.pending_signal = None
            asyncio.create_task(self._open_position("short"))
            return True

        if srsi_death and srsi_top and not macd_death:
            self.state.pending_signal = "short_wait_macd"
            self.state.signal_candle_count = 0
            logger.info("[SCALP] StochRSI 숏 크로스 감지 → MACD 대기")

        if macd_death and not (srsi_death and srsi_top):
            if not self.state.pending_signal or "short" in (self.state.pending_signal or ""):
                self.state.pending_signal = "short_wait_srsi"
                self.state.signal_candle_count = 0
                logger.info("[SCALP] MACD 숏 크로스 감지 → StochRSI 대기")

        return False

    # ══════════════════════════════════════════
    #  포지션 관리
    # ══════════════════════════════════════════

    async def _open_position(self, direction: str):
        """시장가 진입"""
        async with self._lock:
            if self.state.position != "flat":
                return

            side = "buy" if direction == "long" else "sell"
            pos_side = direction

            order = await self.executor._market_order(
                side, self.size_btc, pos_side
            )
            if not order:
                logger.error(f"[SCALP] 진입 실패: {direction}")
                return

            fill_price = float(order.get("average") or order.get("price") or 0)
            self.state.position = direction
            self.state.entry_price = fill_price
            self.state.entry_time = time.time()
            self.state.entry_size_btc = self.size_btc
            self.state.entry_order_id = order.get("id")
            self.state.pending_signal = None

            await save_scalp_state(self.redis, self.state)

            sl_ref = f"BB {'하단' if direction == 'long' else '상단'}"
            sl_price = self._bb_lower if direction == "long" else self._bb_upper

            logger.info(
                f"[SCALP] ENTRY {direction.upper()} @ ${fill_price:,.1f} | "
                f"SL={sl_ref} ${sl_price:,.0f}"
            )

            if self.telegram:
                icon = "\U0001f7e2" if direction == "long" else "\U0001f534"
                await self.telegram._send(
                    f"{icon} <b>Scalp {direction.upper()}</b> @ ${fill_price:,.1f}\n"
                    f"Size: {self.size_btc} BTC | SL: {sl_ref} ${sl_price:,.0f}"
                )

            _append_jsonl({
                "type": "scalp_entry",
                "direction": direction,
                "price": fill_price,
                "size_btc": self.size_btc,
                "bb_upper": round(self._bb_upper, 1),
                "bb_lower": round(self._bb_lower, 1),
                "sl": f"BB {'하단' if direction == 'long' else '상단'} ${sl_price:,.0f}",
            })

    async def _close_position(self, reason: str):
        """시장가 청산"""
        direction = self.state.position
        if direction == "flat":
            return

        side = "sell" if direction == "long" else "buy"
        pos_side = direction

        order = await self.executor._market_order(
            side, self.state.entry_size_btc, pos_side, reduce_only=True
        )
        if not order or order.get("already_closed"):
            self.state.position = "flat"
            self.state.entry_price = 0
            self.state.pending_signal = None
            await save_scalp_state(self.redis, self.state)
            return

        exit_price = float(order.get("average") or order.get("price") or 0)

        # PnL 계산
        if direction == "long":
            pnl_gross = self.state.entry_size_btc * (exit_price - self.state.entry_price)
        else:
            pnl_gross = self.state.entry_size_btc * (self.state.entry_price - exit_price)

        fee = self.state.entry_size_btc * (self.state.entry_price + exit_price) * self.taker_fee
        pnl = round(pnl_gross - fee, 4)

        # 상태 업데이트
        self.state.total_trades += 1
        self.state.total_pnl += pnl
        if pnl > 0:
            self.state.winning_trades += 1
        else:
            self.state.losing_trades += 1
        self.state.last_trade_time = time.time()

        entry_price = self.state.entry_price
        entry_time = self.state.entry_time
        size_btc = self.state.entry_size_btc

        self.state.position = "flat"
        self.state.entry_price = 0
        self.state.entry_time = 0
        self.state.entry_order_id = None
        self.state.pending_signal = None

        # 리스크 매니저 기록
        if self.risk_manager:
            try:
                balance = await self.executor.get_balance()
                pnl_pct = (pnl / balance * 100) if balance > 0 else 0
                await self.risk_manager.record_trade_result(pnl_pct, pnl)
            except Exception:
                pass

        # DB 기록
        try:
            await self.db.insert_scalp_trade({
                "direction": direction,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "size_btc": size_btc,
                "pnl_usdt": pnl,
                "fee_total": round(fee, 4),
                "entry_time": int(entry_time * 1000),
                "exit_time": int(time.time() * 1000),
                "exit_reason": reason,
                "timeframe": self.timeframe,
            })
        except Exception as e:
            logger.error(f"[SCALP] DB 기록 실패: {e}")

        await save_scalp_state(self.redis, self.state)

        # 레버리지 기준 수익률
        lev_pnl_pct = (pnl / (size_btc * entry_price / self.leverage)) * 100 if entry_price > 0 else 0

        logger.info(
            f"[SCALP] EXIT {direction.upper()} @ ${exit_price:,.1f} | "
            f"PnL ${pnl:+.3f} ({lev_pnl_pct:+.1f}%) | reason={reason} | "
            f"총 {self.state.total_trades}건 WR={self.state.win_rate:.0f}%"
        )

        if self.telegram:
            icon = "\u2705" if pnl > 0 else "\u274c"
            await self.telegram._send(
                f"{icon} <b>Scalp Exit</b> {direction.upper()}\n"
                f"${entry_price:,.0f} \u2192 ${exit_price:,.0f}\n"
                f"PnL: <b>${pnl:+.3f}</b> ({lev_pnl_pct:+.1f}%)\n"
                f"Reason: {reason}\n"
                f"Total: {self.state.total_trades}건 WR {self.state.win_rate:.0f}%"
            )

        _append_jsonl({
            "type": "scalp_exit",
            "direction": direction,
            "entry": entry_price,
            "exit": exit_price,
            "pnl": pnl,
            "lev_pnl_pct": round(lev_pnl_pct, 2),
            "reason": reason,
            "total_trades": self.state.total_trades,
            "win_rate": round(self.state.win_rate, 1),
        })

    # ══════════════════════════════════════════
    #  안전장치
    # ══════════════════════════════════════════

    async def _check_stop_loss(self):
        """BB 밴드 이탈 SL — 0.5초 간격 체크

        Jay: "볼밴이 터지면 틀린거. 내 손절은 하단이니까"
        롱 → 가격이 BB 하단 이탈 시 청산
        숏 → 가격이 BB 상단 이탈 시 청산
        BB 값이 없으면 ATR 기반 fallback
        """
        if not self.state or self.state.position == "flat":
            return

        price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
        if not price_str:
            return
        price = float(price_str)

        # BB 밴드 이탈 체크 (primary)
        if self._bb_lower > 0 and self._bb_upper > 0:
            if self.state.position == "long" and price < self._bb_lower:
                logger.warning(
                    f"[SCALP] BB SL: price ${price:,.0f} < BB하단 ${self._bb_lower:,.0f}"
                )
                async with self._lock:
                    if self.state.position != "flat":
                        await self._close_position("bb_lower_breach")
                return

            if self.state.position == "short" and price > self._bb_upper:
                logger.warning(
                    f"[SCALP] BB SL: price ${price:,.0f} > BB상단 ${self._bb_upper:,.0f}"
                )
                async with self._lock:
                    if self.state.position != "flat":
                        await self._close_position("bb_upper_breach")
                return

        # ATR fallback (BB 값 없을 때)
        if self.state.position == "long":
            loss_pct = (self.state.entry_price - price) / self.state.entry_price * 100
        else:
            loss_pct = (price - self.state.entry_price) / self.state.entry_price * 100

        if loss_pct >= self.state.sl_pct:
            logger.warning(
                f"[SCALP] ATR SL fallback {loss_pct:.2f}% >= {self.state.sl_pct:.2f}%"
            )
            async with self._lock:
                if self.state.position != "flat":
                    await self._close_position(f"stop_loss_{loss_pct:.1f}%")

    async def _check_bot_kill(self):
        """DD -20% → 전체 청산 + 정지"""
        try:
            balance = await self.executor.get_balance()
            if balance <= 0:
                return
            if balance > self._peak_balance:
                self._peak_balance = balance
            dd_pct = (self._peak_balance - balance) / self._peak_balance * 100

            if dd_pct >= self.bot_kill_pct:
                logger.critical(
                    f"[SCALP] BOT_KILL DD -{dd_pct:.1f}% | "
                    f"peak=${self._peak_balance:.2f} now=${balance:.2f}"
                )
                # 포지션 청산
                if self.state and self.state.position != "flat":
                    await self._close_position("bot_kill")
                # 미체결 주문 취소
                await self.executor.cancel_all_orders()
                self._running = False
                if self.telegram:
                    await self.telegram.notify_emergency(
                        f"BOT_KILL DD -{dd_pct:.1f}% | 잔고 ${balance:.2f}"
                    )
        except Exception as e:
            logger.error(f"[SCALP] BOT_KILL 체크 에러: {e}")

    async def _update_price_history(self):
        """서킷브레이커용 가격 이력"""
        try:
            price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
            if not price_str:
                return
            price = float(price_str)
            now = time.time()

            self._price_history.append((now, price))
            # 오래된 데이터 정리
            cutoff = now - self.cb_window - 5
            self._price_history = [
                (t, p) for t, p in self._price_history if t >= cutoff
            ]

            # 서킷브레이커 체크
            if len(self._price_history) >= 2:
                oldest_in_window = [
                    (t, p) for t, p in self._price_history
                    if t >= now - self.cb_window
                ]
                if oldest_in_window:
                    first_price = oldest_in_window[0][1]
                    move_pct = abs(price - first_price) / first_price * 100
                    if move_pct >= self.cb_pct:
                        logger.warning(
                            f"[SCALP] 서킷브레이커! {move_pct:.2f}% in {self.cb_window}s"
                        )
                        self._cb_frozen_until = now + self.cb_freeze
                        if self.telegram:
                            await self.telegram.notify_warning(
                                f"서킷브레이커: {move_pct:.2f}% ({self.cb_window}초) "
                                f"→ {self.cb_freeze}초 동결"
                            )
        except Exception:
            pass

    # ══════════════════════════════════════════
    #  크래시 복구
    # ══════════════════════════════════════════

    async def _recover(self):
        """Redis + OKX 실제 상태 대조 복구"""
        self.state = await load_scalp_state(self.redis)

        positions = await self.executor.get_positions()
        has_long = any(
            p.get("direction") == "long" and abs(float(p.get("size", 0))) > 0
            for p in positions
        )
        has_short = any(
            p.get("direction") == "short" and abs(float(p.get("size", 0))) > 0
            for p in positions
        )

        if self.state:
            # 상태는 long인데 실제 포지션 없음
            if self.state.position == "long" and not has_long:
                logger.warning("[SCALP] 복구: state=long but no position → flat")
                self.state.position = "flat"
                self.state.entry_price = 0
            elif self.state.position == "short" and not has_short:
                logger.warning("[SCALP] 복구: state=short but no position → flat")
                self.state.position = "flat"
                self.state.entry_price = 0
            # 상태는 flat인데 포지션 있음 → 그대로 유지 (수동 매매일 수 있음)
            logger.info(
                f"[SCALP] 복구 완료: pos={self.state.position} "
                f"trades={self.state.total_trades} pnl=${self.state.total_pnl:+.2f}"
            )
        elif has_long or has_short:
            # 상태 없는데 포지션 있음 → 고아 포지션 청산
            logger.warning("[SCALP] 고아 포지션 감지 → 시장가 청산")
            for p in positions:
                sz = abs(float(p.get("size", 0)))
                if sz <= 0:
                    continue
                direction = p.get("direction", "long")
                side = "sell" if direction == "long" else "buy"
                # size는 contracts 단위일 수 있으므로 BTC 변환
                size_btc = sz * 0.01  # OKX: 1 contract = 0.01 BTC
                await self.executor._market_order(
                    side, size_btc, direction, reduce_only=True
                )
            await self.executor.cancel_all_orders()

    # ══════════════════════════════════════════
    #  콜백 + 상태
    # ══════════════════════════════════════════

    async def on_order_update(self, order_info: dict):
        """OrderStream 콜백 — 시장가 주문은 즉시 체결되므로 최소 처리"""
        if not self.state or self.state.position == "flat":
            return
        if (order_info.get("id") == self.state.entry_order_id
                and order_info.get("status") == "canceled"):
            logger.warning("[SCALP] 진입 주문 외부 취소됨")
            async with self._lock:
                self.state.position = "flat"
                self.state.entry_price = 0
                await save_scalp_state(self.redis, self.state)

    def get_status(self) -> dict:
        if not self.state:
            return {"active": False}
        return {
            "active": self.state.is_active and self._running,
            "position": self.state.position,
            "entry_price": self.state.entry_price,
            "sl_pct": self.state.sl_pct,
            "total_trades": self.state.total_trades,
            "total_pnl": round(self.state.total_pnl, 2),
            "win_rate": round(self.state.win_rate, 1),
            "winning": self.state.winning_trades,
            "losing": self.state.losing_trades,
            "pending_signal": self.state.pending_signal,
        }

    async def stop(self):
        """그레이스풀 종료"""
        logger.info("[SCALP] 엔진 정지")
        self._running = False
        if self.state and self.state.position != "flat":
            await self._close_position("manual_stop")
        await self.executor.cancel_all_orders()
        if self.state:
            self.state.is_active = False
            await save_scalp_state(self.redis, self.state)
