"""
GridEngine — ATR-Adaptive Grid Trading

BTC 무기한 선물 양방향 그리드 트레이딩.
방향 예측 불필요 — 가격 진동에서 구조적 수익.

메커닉:
  BUY -1,-2 (open long) + SELL +1,+2 (open short)
  체결 → counter-order(TP) → 사이클 완성 → 재배치

ATR 적응:
  spacing = clamp(ATR% × 0.6, 0.10%, 0.50%)
  1시간마다 재계산

안전장치:
  Hurst > 0.7 → 일시정지 (추세장)
  BOT_KILL -20% DD → 정지
  가격 drift > range 50% → 리밸런스
"""

import asyncio
import logging
import time
import uuid

from src.trading.executor import OrderExecutor
from src.trading.grid_state import (
    GridLevel, GridState, save_grid_state, load_grid_state,
)
from src.data.storage import Database, RedisClient
from src.monitoring.trade_logger import _append_jsonl

logger = logging.getLogger(__name__)

GRID_SIZE_BTC = 0.01  # 레벨당 1계약


class GridEngine:
    """ATR-Adaptive Grid Trading Engine"""

    def __init__(self, executor: OrderExecutor, db: Database,
                 redis: RedisClient, telegram=None, risk_manager=None,
                 config: dict = None):
        self.executor = executor
        self.db = db
        self.redis = redis
        self.telegram = telegram
        self.risk_manager = risk_manager

        cfg = (config or {}).get("grid", {})
        self.enabled = cfg.get("enabled", False)
        self.leverage = cfg.get("leverage", 20)
        self.num_levels = cfg.get("levels", 4)  # 총 레벨 (buy+sell)
        self.half_levels = self.num_levels // 2
        self.atr_mult = cfg.get("atr_mult", 0.6)
        self.spacing_min = cfg.get("spacing_min_pct", 0.10)
        self.spacing_max = cfg.get("spacing_max_pct", 0.50)
        self.rebalance_sec = cfg.get("rebalance_sec", 3600)
        self.drift_pct = cfg.get("drift_rebalance_pct", 50)
        self.hurst_pause = cfg.get("hurst_pause", 0.70)
        self.monitor_sec = cfg.get("monitor_sec", 3)
        self.atr_period = cfg.get("atr_period", 14)
        self.atr_tf = cfg.get("atr_timeframe", "5m")

        self.symbol = (config or {}).get("exchange", {}).get("symbol", "BTC/USDT:USDT")
        self.state: GridState | None = None
        self._running = True
        self._lock = asyncio.Lock()  # 상태 변경 동시성 보호

    # ══════════════════════════════════════════
    #  메인 루프
    # ══════════════════════════════════════════

    async def run(self):
        """그리드 메인 루프"""
        if not self.enabled:
            logger.info("[GRID] 비활성화 (grid.enabled=false)")
            return

        # 레버리지 설정
        try:
            await self.executor.set_leverage(self.leverage, "long")
            await self.executor.set_leverage(self.leverage, "short")
        except Exception as e:
            logger.warning(f"[GRID] 레버리지 설정 실패: {e}")

        # 크래시 복구
        await self._recover()

        # 그리드 없으면 새로 생성
        if not self.state or not self.state.is_active:
            await self._build_grid()

        logger.info("[GRID] 그리드 엔진 시작")
        if self.telegram:
            try:
                await self.telegram._send(
                    f"\U0001f4ca <b>Grid Engine 시작</b>\n"
                    f"레벨: {self.num_levels}개 | spacing: {self.state.spacing_pct:.2f}%\n"
                    f"center: ${self.state.center_price:.0f}"
                )
            except Exception:
                pass

        rebalance_check = 0
        while self._running:
            try:
                if not self.state or not self.state.is_active:
                    await asyncio.sleep(5)
                    continue

                # 레짐 게이트
                await self._check_regime()

                # 리스크 게이트
                if self.risk_manager:
                    allowed, reason = self.risk_manager.is_trading_allowed()
                    if not allowed:
                        logger.warning(f"[GRID] 리스크 정지: {reason}")
                        await self._pause("risk_gate")
                        continue

                if self.state.is_active:
                    async with self._lock:
                        await self._monitor_tick()

                    # 리밸런스 (60초마다)
                    now = time.time()
                    if now - rebalance_check >= 60:
                        rebalance_check = now
                        async with self._lock:
                            await self._check_rebalance()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[GRID] 루프 에러: {e}", exc_info=True)

            await asyncio.sleep(self.monitor_sec)

    # ══════════════════════════════════════════
    #  그리드 생성
    # ══════════════════════════════════════════

    async def _build_grid(self):
        """현재가 기준으로 그리드 주문 배치"""
        price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
        if not price_str:
            logger.warning("[GRID] 가격 없음 → 대기")
            return
        price = float(price_str)

        # ATR 계산
        atr_pct = await self._compute_atr_pct(price)
        if atr_pct <= 0:
            atr_pct = 0.15  # fallback

        spacing_pct = max(self.spacing_min, min(self.spacing_max, atr_pct * self.atr_mult))
        spacing_abs = price * spacing_pct / 100

        logger.info(
            f"[GRID] 빌드: center=${price:.0f} ATR={atr_pct:.3f}% "
            f"spacing={spacing_pct:.2f}% (${spacing_abs:.1f})"
        )

        # 기존 주문 전부 취소
        await self.executor.cancel_all_orders()

        # 그리드 상태 생성
        grid_id = str(uuid.uuid4())[:8]
        self.state = GridState(
            center_price=price,
            spacing_pct=spacing_pct,
            spacing_abs=spacing_abs,
            is_active=True,
            created_at=time.time(),
            last_rebalance=time.time(),
        )

        # 레벨 생성 + 주문 배치
        for i in range(1, self.half_levels + 1):
            # BUY 레벨 (음수)
            buy_price = round(price - i * spacing_abs, 1)
            buy_level = GridLevel(level_id=-i, side="buy", price=buy_price)
            order = await self.executor.place_limit_order(
                "buy", GRID_SIZE_BTC, buy_price, "long"
            )
            if order:
                buy_level.order_id = order.get("id")
                buy_level.status = "placed"
            self.state.levels[-i] = buy_level

            # SELL 레벨 (양수)
            sell_price = round(price + i * spacing_abs, 1)
            sell_level = GridLevel(level_id=i, side="sell", price=sell_price)
            order = await self.executor.place_limit_order(
                "sell", GRID_SIZE_BTC, sell_price, "short"
            )
            if order:
                sell_level.order_id = order.get("id")
                sell_level.status = "placed"
            self.state.levels[i] = sell_level

        await save_grid_state(self.redis, self.state)
        logger.info(f"[GRID] {len(self.state.levels)}개 레벨 배치 완료")

    # ══════════════════════════════════════════
    #  주문 모니터링 (3초마다)
    # ══════════════════════════════════════════

    async def _monitor_tick(self):
        """오픈 주문 확인 → 체결 감지 → counter-order 배치"""
        if not self.state:
            return

        # 30초마다 상태 로그
        now = time.time()
        if now - getattr(self, "_last_status_log", 0) >= 5:
            self._last_status_log = now
            price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
            price = float(price_str) if price_str else 0
            levels_summary = " ".join(
                f"Lv{lid}:{lv.status[0]}" + (f"→{lv.counter_status[0]}" if lv.counter_status != "none" else "")
                for lid, lv in sorted(self.state.levels.items())
            )
            logger.info(
                f"[GRID] 모니터 | ${price:.0f} | center=${self.state.center_price:.0f} | "
                f"{levels_summary} | cycles={self.state.total_cycles} pnl=${self.state.total_pnl:+.2f}"
            )

        try:
            open_orders = await self.executor.exchange.fetch_open_orders(self.symbol)
        except Exception as e:
            logger.debug(f"[GRID] fetch_open_orders 실패: {e}")
            return

        open_ids = {o["id"] for o in open_orders}

        for lid, lv in self.state.levels.items():
            # ── 그리드 주문 체결 감지 ──
            if lv.status == "placed" and lv.order_id and lv.order_id not in open_ids:
                # 주문이 사라짐 → 체결 확인
                try:
                    order = await self.executor.exchange.fetch_order(lv.order_id, self.symbol)
                    if order.get("status") == "closed":
                        fill_price = float(order.get("average") or order.get("price") or lv.price)
                        lv.status = "filled"
                        lv.fill_price = fill_price
                        lv.fill_time = time.time()
                        logger.info(f"[GRID] 레벨 {lid} 체결 @ ${fill_price:.1f}")

                        # counter-order 배치 (TP)
                        await self._place_counter_order(lv)
                    elif order.get("status") in ("canceled", "expired"):
                        lv.status = "cancelled"
                except Exception as e:
                    logger.debug(f"[GRID] order fetch 실패 ({lid}): {e}")

            # ── Counter-order 체결 감지 (사이클 완성) ──
            if lv.counter_status == "placed" and lv.counter_order_id and lv.counter_order_id not in open_ids:
                try:
                    order = await self.executor.exchange.fetch_order(lv.counter_order_id, self.symbol)
                    if order.get("status") == "closed":
                        counter_price = float(order.get("average") or order.get("price") or 0)
                        lv.counter_status = "filled"
                        lv.counter_fill_price = counter_price

                        # PnL 계산
                        pnl = self._calc_cycle_pnl(lv, counter_price)
                        lv.cycle_count += 1
                        lv.cycle_pnl += pnl
                        self.state.total_cycles += 1
                        self.state.total_pnl += pnl

                        logger.info(
                            f"[GRID] 사이클 완성 레벨{lid}: "
                            f"${lv.fill_price:.1f}→${counter_price:.1f} "
                            f"PnL=${pnl:+.3f} (총 {self.state.total_cycles}사이클 ${self.state.total_pnl:+.2f})"
                        )

                        # DB 기록
                        await self._record_cycle(lv, counter_price, pnl)

                        # 텔레그램 알림
                        if self.telegram:
                            try:
                                await self.telegram._send(
                                    f"\u2705 Grid 사이클 #{self.state.total_cycles} | "
                                    f"Lv{lid} ${lv.fill_price:.0f}→${counter_price:.0f} | "
                                    f"<b>${pnl:+.3f}</b> | 총 ${self.state.total_pnl:+.2f}"
                                )
                            except Exception:
                                pass

                        # 원래 그리드 주문 재배치
                        await self._replace_grid_order(lv)

                    elif order.get("status") in ("canceled", "expired"):
                        lv.counter_status = "none"
                        lv.counter_order_id = None
                except Exception as e:
                    logger.debug(f"[GRID] counter fetch 실패 ({lid}): {e}")

        await save_grid_state(self.redis, self.state)

    async def _place_counter_order(self, lv: GridLevel):
        """체결된 그리드 주문에 대한 TP counter-order 배치"""
        spacing = self.state.spacing_abs
        if lv.side == "buy":
            # long 포지션 → sell로 청산
            tp_price = round(lv.fill_price + spacing, 1)
            order = await self.executor.place_limit_order(
                "sell", GRID_SIZE_BTC, tp_price, "long", reduce_only=True
            )
        else:
            # short 포지션 → buy로 청산
            tp_price = round(lv.fill_price - spacing, 1)
            order = await self.executor.place_limit_order(
                "buy", GRID_SIZE_BTC, tp_price, "short", reduce_only=True
            )

        if order:
            lv.counter_order_id = order.get("id")
            lv.counter_status = "placed"
            logger.info(f"[GRID] counter-order Lv{lv.level_id}: TP @ ${tp_price:.1f}")
        else:
            logger.warning(f"[GRID] counter-order 실패 Lv{lv.level_id}")

    async def _replace_grid_order(self, lv: GridLevel):
        """사이클 완성 후 원래 그리드 주문 재배치"""
        lv.status = "pending"
        lv.fill_price = 0
        lv.fill_time = 0
        lv.counter_order_id = None
        lv.counter_status = "none"
        lv.counter_fill_price = 0

        if lv.side == "buy":
            order = await self.executor.place_limit_order(
                "buy", GRID_SIZE_BTC, lv.price, "long"
            )
        else:
            order = await self.executor.place_limit_order(
                "sell", GRID_SIZE_BTC, lv.price, "short"
            )

        if order:
            lv.order_id = order.get("id")
            lv.status = "placed"

    # ══════════════════════════════════════════
    #  PnL 계산
    # ══════════════════════════════════════════

    def _calc_cycle_pnl(self, lv: GridLevel, counter_price: float) -> float:
        """사이클 PnL = 가격차 × 사이즈 - 수수료"""
        price_diff = abs(counter_price - lv.fill_price)
        gross = GRID_SIZE_BTC * price_diff
        # maker 수수료 양쪽
        fee = GRID_SIZE_BTC * lv.fill_price * 0.0002 + GRID_SIZE_BTC * counter_price * 0.0002
        return round(gross - fee, 4)

    async def _record_cycle(self, lv: GridLevel, counter_price: float, pnl: float):
        """DB에 사이클 기록"""
        fee = GRID_SIZE_BTC * lv.fill_price * 0.0002 + GRID_SIZE_BTC * counter_price * 0.0002
        try:
            await self.db.insert_grid_trade({
                "grid_id": str(self.state.created_at)[:8],
                "level_id": lv.level_id,
                "cycle_num": lv.cycle_count,
                "side": lv.side,
                "entry_price": lv.fill_price,
                "exit_price": counter_price,
                "size_btc": GRID_SIZE_BTC,
                "pnl_usdt": pnl,
                "fee_total": round(fee, 4),
                "entry_time": int(lv.fill_time * 1000),
                "exit_time": int(time.time() * 1000),
                "spacing_pct": self.state.spacing_pct,
            })
        except Exception as e:
            logger.error(f"[GRID] DB 기록 실패: {e}")

        _append_jsonl({
            "type": "grid_cycle",
            "level": lv.level_id,
            "entry": round(lv.fill_price, 1),
            "exit": round(counter_price, 1),
            "pnl": pnl,
            "total_pnl": round(self.state.total_pnl, 2),
            "total_cycles": self.state.total_cycles,
        })

    # ══════════════════════════════════════════
    #  리밸런스
    # ══════════════════════════════════════════

    async def _check_rebalance(self):
        """가격 drift 또는 시간 기반 리밸런스"""
        if not self.state:
            return

        # 열린 포지션(counter-order 대기 중)이 있으면 리밸런스 스킵
        has_open = any(
            lv.status == "filled" and lv.counter_status in ("placed", "none")
            for lv in self.state.levels.values()
        )
        if has_open:
            return

        price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
        if not price_str:
            return
        price = float(price_str)
        now = time.time()

        drift = abs(price - self.state.center_price) / self.state.center_price * 100
        range_pct = self.state.spacing_pct * self.half_levels
        drift_ratio = drift / range_pct * 100 if range_pct > 0 else 0

        # 가격이 그리드 범위를 크게 벗어남
        if drift_ratio > self.drift_pct:
            logger.info(f"[GRID] 리밸런스 (drift {drift:.2f}%)")
            await self._rebuild()
            return

        # 1시간 주기 ATR 재계산
        if now - self.state.last_rebalance > self.rebalance_sec:
            atr_pct = await self._compute_atr_pct(price)
            if atr_pct > 0:
                new_spacing = max(self.spacing_min, min(self.spacing_max, atr_pct * self.atr_mult))
                change = abs(new_spacing - self.state.spacing_pct) / self.state.spacing_pct * 100
                if change > 15:
                    logger.info(f"[GRID] ATR 리밸런스: spacing {self.state.spacing_pct:.2f}%→{new_spacing:.2f}%")
                    await self._rebuild()
                    return
            self.state.last_rebalance = now

    async def _rebuild(self):
        """그리드 전체 재구성"""
        # 체결된 레벨의 counter-order는 유지
        # 미체결 그리드 주문만 취소 후 재배치
        for lid, lv in self.state.levels.items():
            if lv.status == "placed" and lv.order_id:
                await self.executor.cancel_order_by_id(lv.order_id)
            # counter-order는 유지 (이미 포지션 보유 중)

        # 새 그리드 빌드 (기존 state의 cycles/pnl 유지)
        old_cycles = self.state.total_cycles
        old_pnl = self.state.total_pnl
        await self._build_grid()
        if self.state:
            self.state.total_cycles = old_cycles
            self.state.total_pnl = old_pnl

    # ══════════════════════════════════════════
    #  레짐 게이트
    # ══════════════════════════════════════════

    async def _check_regime(self):
        """Hurst > 0.7 → 그리드 일시정지"""
        hurst_str = await self.redis.get("rt:regime:hurst")
        if not hurst_str:
            return

        hurst = float(hurst_str)

        if hurst > self.hurst_pause and self.state and self.state.is_active:
            if self.state.pause_reason != "hurst_gate":
                logger.warning(f"[GRID] Hurst {hurst:.2f} > {self.hurst_pause} → 일시정지")
                await self._pause("hurst_gate")

        elif hurst <= self.hurst_pause and self.state and self.state.pause_reason == "hurst_gate":
            logger.info(f"[GRID] Hurst {hurst:.2f} ≤ {self.hurst_pause} → 재개")
            self.state.is_active = True
            self.state.pause_reason = None
            await self._rebuild()

    async def _pause(self, reason: str):
        """그리드 일시정지 (counter-order는 유지)"""
        if not self.state:
            return
        self.state.is_active = False
        self.state.pause_reason = reason

        # 미체결 그리드 주문 취소 (counter는 유지)
        for lid, lv in self.state.levels.items():
            if lv.status == "placed" and lv.order_id:
                await self.executor.cancel_order_by_id(lv.order_id)
                lv.status = "cancelled"

        await save_grid_state(self.redis, self.state)

    # ══════════════════════════════════════════
    #  크래시 복구
    # ══════════════════════════════════════════

    async def _recover(self):
        """시작 시 Redis에서 그리드 상태 복원"""
        self.state = await load_grid_state(self.redis)
        if not self.state or not self.state.is_active:
            return

        logger.info(
            f"[GRID] 복구: center=${self.state.center_price:.0f} "
            f"cycles={self.state.total_cycles} pnl=${self.state.total_pnl:+.2f}"
        )

        # OKX 오픈 주문과 대조
        try:
            open_orders = await self.executor.exchange.fetch_open_orders(self.symbol)
            open_ids = {o["id"] for o in open_orders}

            for lid, lv in self.state.levels.items():
                if lv.status == "placed" and lv.order_id not in open_ids:
                    # 주문 사라짐 → 체결됐거나 취소됨
                    lv.status = "cancelled"  # monitor_tick에서 재처리
                if lv.counter_status == "placed" and lv.counter_order_id not in open_ids:
                    lv.counter_status = "none"

        except Exception as e:
            logger.error(f"[GRID] 복구 주문 대조 실패: {e}")

    # ══════════════════════════════════════════
    #  ATR 계산
    # ══════════════════════════════════════════

    async def _compute_atr_pct(self, current_price: float) -> float:
        """5m 캔들에서 ATR% 계산"""
        try:
            candles = await self.db.get_candles(self.symbol, self.atr_tf, limit=self.atr_period + 1)
            if len(candles) < 2:
                return 0.0

            trs = []
            for i in range(1, len(candles)):
                h = candles[i]["high"]
                l = candles[i]["low"]
                c_prev = candles[i - 1]["close"]
                tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
                trs.append(tr)

            atr = sum(trs) / len(trs) if trs else 0
            if atr <= 0 or current_price <= 0:
                return 0.0
            return (atr / current_price) * 100

        except Exception as e:
            logger.debug(f"[GRID] ATR 계산 실패: {e}")
            return 0.0

    # ══════════════════════════════════════════
    #  외부 인터페이스
    # ══════════════════════════════════════════

    async def stop(self):
        """그리드 정지 + 전 주문 취소"""
        self._running = False
        if self.state:
            self.state.is_active = False
            for lid, lv in self.state.levels.items():
                if lv.order_id:
                    await self.executor.cancel_order_by_id(lv.order_id)
                if lv.counter_order_id:
                    await self.executor.cancel_order_by_id(lv.counter_order_id)
            await save_grid_state(self.redis, self.state)
        logger.info("[GRID] 정지 완료")

    def get_status(self) -> dict:
        """상태 조회 (대시보드/텔레그램용)"""
        if not self.state:
            return {"active": False}
        return {
            "active": self.state.is_active,
            "center": self.state.center_price,
            "spacing_pct": self.state.spacing_pct,
            "total_cycles": self.state.total_cycles,
            "total_pnl": round(self.state.total_pnl, 2),
            "pause_reason": self.state.pause_reason,
            "levels": {
                lid: {"side": lv.side, "price": lv.price, "status": lv.status,
                      "counter": lv.counter_status, "cycles": lv.cycle_count}
                for lid, lv in self.state.levels.items()
            },
        }
