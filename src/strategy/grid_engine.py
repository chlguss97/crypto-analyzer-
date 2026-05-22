"""
GridEngine — Minimal DGT Grid Trading (SPEC v4)

BTC 무기한 선물 양방향 그리드. 절대 멈추지 않음.
파라미터 시작 시 1회 설정, 실행 중 불변.
레짐 감지 없음. 안전장치: 서킷브레이커 + BOT_KILL만.

메커닉:
  BUY -1,-2 (open long) + SELL +1,+2 (open short)
  체결 → counter-order(TP) → 사이클 완성 → 재배치
  가격 경계 돌파 → DGT 리빌드 (즉시 center 재설정)
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

GRID_SIZE_BTC = 0.01


class GridEngine:
    """Minimal DGT Grid Trading Engine (SPEC v4)"""

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
        self.leverage = cfg.get("leverage", 10)
        self.atr_mult = cfg.get("atr_mult", 0.6)
        self.spacing_min = cfg.get("spacing_min_pct", 0.15)
        self.spacing_max = cfg.get("spacing_max_pct", 0.50)
        self.atr_period = cfg.get("atr_period", 14)
        self.atr_tf = cfg.get("atr_timeframe", "5m")
        self.maker_fee = (config or {}).get("fees", {}).get("maker", 0.0002)

        safety = (config or {}).get("safety", {})
        self.cb_pct = safety.get("circuit_breaker_pct", 2.0)
        self.cb_window = safety.get("circuit_breaker_window_sec", 10)
        self.cb_freeze = safety.get("circuit_breaker_freeze_sec", 60)
        self.bot_kill_pct = safety.get("bot_kill_drawdown_pct", 20)

        self.symbol = (config or {}).get("exchange", {}).get("symbol", "BTC/USDT:USDT")
        self.state: GridState | None = None
        self._running = True
        self._lock = asyncio.Lock()
        self._last_rest_check = 0
        self._peak_balance = 0.0
        self._cb_frozen_until = 0.0  # 서킷브레이커 동결 해제 시각
        self._price_history: list[tuple[float, float]] = []  # (timestamp, price)

        # 잔고 비례 레벨 수 (시작 시 1회 계산)
        self.num_levels = 2
        self.half_levels = 1

    # ══════════════════════════════════════════
    #  메인 루프
    # ══════════════════════════════════════════

    async def run(self):
        if not self.enabled:
            logger.info("[GRID] 비활성화 (grid.enabled=false)")
            return

        # 시작 시 1회: 잔고 → 레벨 수 계산
        balance = await self.executor.get_balance()
        self._peak_balance = balance
        await self._calc_levels(balance)

        # 레버리지 설정 (10x 고정, 1회)
        try:
            await self.executor.set_leverage(self.leverage, "long")
            await self.executor.set_leverage(self.leverage, "short")
        except Exception as e:
            logger.warning(f"[GRID] 레버리지 설정 실패: {e}")

        # 크래시 복구
        await self._recover()

        # 그리드 없으면 새로 생성
        if not self.state:
            await self._build_grid()

        logger.info("[GRID] 엔진 시작")
        if self.telegram:
            try:
                await self.telegram._send(
                    f"\U0001f4ca <b>Grid Engine 시작</b>\n"
                    f"레벨: {self.num_levels}개 | spacing: {self.state.spacing_pct:.2f}%\n"
                    f"center: ${self.state.center_price:.0f} | 10x"
                )
            except Exception:
                pass

        while self._running:
            try:
                if not self.state:
                    await asyncio.sleep(5)
                    continue

                now = time.time()

                # 서킷브레이커 동결 중이면 해제 대기
                if now < self._cb_frozen_until:
                    await asyncio.sleep(1)
                    continue

                # 동결 해제 직후 → 자동 리빌드
                if self._cb_frozen_until > 0:
                    self._cb_frozen_until = 0.0
                    logger.info("[GRID] 서킷브레이커 해제 → 리빌드")
                    await self._rebuild()

                price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
                if not price_str:
                    await asyncio.sleep(1)
                    continue
                price = float(price_str)

                # 서킷브레이커 체크
                if self._check_circuit_breaker(price, now):
                    await self._on_circuit_breaker(price)
                    await asyncio.sleep(1)
                    continue

                # DGT 경계 돌파 체크
                if self._is_outside_grid(price):
                    logger.info(f"[GRID] DGT 경계 돌파 ${price:.0f} → 리빌드")
                    async with self._lock:
                        await self._rebuild()

                # REST fallback (30초마다)
                if now - self._last_rest_check >= 30:
                    self._last_rest_check = now
                    async with self._lock:
                        await self._rest_fallback()

                # BOT_KILL 체크 (30초마다, REST와 동기)
                if now - self._last_rest_check < 1:
                    await self._check_bot_kill()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[GRID] 루프 에러: {e}", exc_info=True)

            await asyncio.sleep(1)

    # ══════════════════════════════════════════
    #  그리드 생성
    # ══════════════════════════════════════════

    async def _build_grid(self, cancel_all: bool = True):
        price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
        if not price_str:
            logger.warning("[GRID] 가격 없음 → 대기")
            return
        price = float(price_str)

        atr_pct = await self._compute_atr_pct(price)
        if atr_pct <= 0:
            atr_pct = self.spacing_min

        spacing_pct = max(self.spacing_min, min(self.spacing_max, atr_pct * self.atr_mult))
        spacing_abs = price * spacing_pct / 100

        logger.info(f"[GRID] 빌드: center=${price:.0f} spacing={spacing_pct:.2f}% (${spacing_abs:.1f})")

        if cancel_all:
            await self.executor.cancel_all_orders()

        self.state = GridState(
            center_price=price,
            spacing_pct=spacing_pct,
            spacing_abs=spacing_abs,
            is_active=True,
            created_at=time.time(),
            last_rebalance=time.time(),
        )

        ratio = 1 + spacing_pct / 100

        for i in range(1, self.half_levels + 1):
            buy_price = round(price / (ratio ** i), 1)
            buy_level = GridLevel(level_id=-i, side="buy", price=buy_price)
            order = await self.executor.place_limit_order("buy", GRID_SIZE_BTC, buy_price, "long")
            if order:
                buy_level.order_id = order.get("id")
                buy_level.status = "placed"
            self.state.levels[-i] = buy_level

            sell_price = round(price * (ratio ** i), 1)
            sell_level = GridLevel(level_id=i, side="sell", price=sell_price)
            order = await self.executor.place_limit_order("sell", GRID_SIZE_BTC, sell_price, "short")
            if order:
                sell_level.order_id = order.get("id")
                sell_level.status = "placed"
            self.state.levels[i] = sell_level

        await save_grid_state(self.redis, self.state)
        logger.info(f"[GRID] {len(self.state.levels)}개 레벨 배치 완료")

    # ══════════════════════════════════════════
    #  WS 체결 콜백
    # ══════════════════════════════════════════

    async def on_order_update(self, order_info: dict):
        """OrderStream WS에서 호출 — 체결/취소 즉시 처리"""
        if not self.state:
            return

        order_id = order_info.get("id", "")
        status = order_info.get("status", "")
        fill_price = float(order_info.get("price") or 0)

        async with self._lock:
            for lid, lv in self.state.levels.items():
                if lv.status == "placed" and lv.order_id == order_id:
                    if status == "closed":
                        lv.status = "filled"
                        lv.fill_price = fill_price if fill_price > 0 else lv.price
                        lv.fill_time = time.time()
                        logger.info(f"[GRID] WS 체결 Lv{lid} @ ${lv.fill_price:.1f}")
                        await self._place_counter_order(lv)
                        await save_grid_state(self.redis, self.state)
                    elif status == "canceled":
                        lv.status = "cancelled"
                    return

                if lv.counter_status == "placed" and lv.counter_order_id == order_id:
                    if status == "closed":
                        counter_price = fill_price if fill_price > 0 else lv.price
                        await self._complete_cycle(lv, counter_price)
                        await save_grid_state(self.redis, self.state)
                    elif status == "canceled":
                        lv.counter_status = "none"
                        lv.counter_order_id = None
                    return

    # ══════════════════════════════════════════
    #  REST Fallback (30초마다)
    # ══════════════════════════════════════════

    async def _rest_fallback(self):
        if not self.state:
            return

        try:
            open_orders = await self.executor.exchange.fetch_open_orders(self.symbol)
        except Exception as e:
            logger.debug(f"[GRID] fetch_open_orders 실패: {e}")
            return

        open_ids = {o["id"] for o in open_orders}

        for lid, lv in self.state.levels.items():
            if lv.status == "placed" and lv.order_id and lv.order_id not in open_ids:
                try:
                    order = await self.executor.exchange.fetch_order(lv.order_id, self.symbol)
                    if order.get("status") == "closed":
                        fill_price = float(order.get("average") or order.get("price") or lv.price)
                        lv.status = "filled"
                        lv.fill_price = fill_price
                        lv.fill_time = time.time()
                        logger.info(f"[GRID] REST 체결 Lv{lid} @ ${fill_price:.1f}")
                        await self._place_counter_order(lv)
                    elif order.get("status") in ("canceled", "expired"):
                        lv.status = "cancelled"
                except Exception as e:
                    logger.debug(f"[GRID] order fetch 실패 ({lid}): {e}")

            if lv.counter_status == "placed" and lv.counter_order_id and lv.counter_order_id not in open_ids:
                try:
                    order = await self.executor.exchange.fetch_order(lv.counter_order_id, self.symbol)
                    if order.get("status") == "closed":
                        counter_price = float(order.get("average") or order.get("price") or 0)
                        await self._complete_cycle(lv, counter_price)
                    elif order.get("status") in ("canceled", "expired"):
                        lv.counter_status = "none"
                        lv.counter_order_id = None
                except Exception as e:
                    logger.debug(f"[GRID] counter fetch 실패 ({lid}): {e}")

        await save_grid_state(self.redis, self.state)

    # ══════════════════════════════════════════
    #  Counter-order + Cycle 완성
    # ══════════════════════════════════════════

    async def _place_counter_order(self, lv: GridLevel):
        """Counter-order 배치 (최대 3회 재시도, 2초 간격)"""
        ratio = 1 + self.state.spacing_pct / 100
        if lv.side == "buy":
            tp_price = round(lv.fill_price * ratio, 1)
            side, pos_side = "sell", "long"
        else:
            tp_price = round(lv.fill_price / ratio, 1)
            side, pos_side = "buy", "short"

        for attempt in range(3):
            order = await self.executor.place_limit_order(
                side, GRID_SIZE_BTC, tp_price, pos_side, reduce_only=True)
            if order:
                lv.counter_order_id = order.get("id")
                lv.counter_status = "placed"
                logger.info(f"[GRID] counter Lv{lv.level_id}: TP @ ${tp_price:.1f}")
                return
            if attempt < 2:
                await asyncio.sleep(2)

        logger.warning(f"[GRID] counter 3회 실패 Lv{lv.level_id}")

    async def _complete_cycle(self, lv: GridLevel, counter_price: float):
        """사이클 완성: PnL 계산 → DB → 텔레그램 → 재배치"""
        lv.counter_status = "filled"
        lv.counter_fill_price = counter_price

        pnl = self._calc_cycle_pnl(lv, counter_price)
        lv.cycle_count += 1
        lv.cycle_pnl += pnl
        self.state.total_cycles += 1
        self.state.total_pnl += pnl

        logger.info(
            f"[GRID] 사이클 완성 Lv{lv.level_id}: "
            f"${lv.fill_price:.1f}→${counter_price:.1f} "
            f"PnL=${pnl:+.3f} (총 {self.state.total_cycles}사이클 ${self.state.total_pnl:+.2f})"
        )

        await self._record_cycle(lv, counter_price, pnl)

        if self.risk_manager:
            balance = await self.executor.get_balance()
            pnl_pct = (pnl / balance * 100) if balance > 0 else 0
            await self.risk_manager.record_trade_result(pnl_pct, pnl)

        if self.telegram:
            try:
                await self.telegram._send(
                    f"\u2705 Grid #{self.state.total_cycles} | "
                    f"Lv{lv.level_id} ${lv.fill_price:.0f}→${counter_price:.0f} | "
                    f"<b>${pnl:+.3f}</b> | 총 ${self.state.total_pnl:+.2f}"
                )
            except Exception:
                pass

        await self._replace_grid_order(lv)

    async def _replace_grid_order(self, lv: GridLevel):
        lv.status = "pending"
        lv.fill_price = 0
        lv.fill_time = 0
        lv.counter_order_id = None
        lv.counter_status = "none"
        lv.counter_fill_price = 0

        side = "buy" if lv.side == "buy" else "sell"
        pos_side = "long" if lv.side == "buy" else "short"
        order = await self.executor.place_limit_order(side, GRID_SIZE_BTC, lv.price, pos_side)
        if order:
            lv.order_id = order.get("id")
            lv.status = "placed"

    # ══════════════════════════════════════════
    #  PnL + DB
    # ══════════════════════════════════════════

    def _calc_cycle_pnl(self, lv: GridLevel, counter_price: float) -> float:
        price_diff = abs(counter_price - lv.fill_price)
        gross = GRID_SIZE_BTC * price_diff
        fee = GRID_SIZE_BTC * (lv.fill_price + counter_price) * self.maker_fee
        return round(gross - fee, 4)

    async def _record_cycle(self, lv: GridLevel, counter_price: float, pnl: float):
        fee = GRID_SIZE_BTC * (lv.fill_price + counter_price) * self.maker_fee
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
    #  DGT 경계 체크
    # ══════════════════════════════════════════

    def _is_outside_grid(self, price: float) -> bool:
        if not self.state:
            return False
        ratio = 1 + self.state.spacing_pct / 100
        grid_upper = self.state.center_price * (ratio ** self.half_levels)
        grid_lower = self.state.center_price / (ratio ** self.half_levels)
        return price > grid_upper or price < grid_lower

    async def _rebuild(self):
        """DGT 리빌드: 미체결 그리드 취소 (counter 유지), 새 center로 재배치"""
        old_cycles = self.state.total_cycles if self.state else 0
        old_pnl = self.state.total_pnl if self.state else 0.0

        # counter-order가 아닌 그리드 주문만 취소
        if self.state:
            for lid, lv in self.state.levels.items():
                if lv.status == "placed" and lv.order_id:
                    await self.executor.cancel_order_by_id(lv.order_id)
                # counter-order는 유지 (TP 대기 중)

        await self._build_grid(cancel_all=False)  # cancel_all 스킵 (이미 개별 취소)
        if self.state:
            self.state.total_cycles = old_cycles
            self.state.total_pnl = old_pnl

    # ══════════════════════════════════════════
    #  서킷브레이커 (2% in 10s → 60s freeze)
    # ══════════════════════════════════════════

    def _check_circuit_breaker(self, price: float, now: float) -> bool:
        self._price_history.append((now, price))
        cutoff = now - self.cb_window
        self._price_history = [(t, p) for t, p in self._price_history if t >= cutoff]
        if len(self._price_history) < 2:
            return False
        oldest_price = self._price_history[0][1]
        change_pct = abs(price - oldest_price) / oldest_price * 100
        return change_pct >= self.cb_pct

    async def _on_circuit_breaker(self, price: float):
        logger.warning(f"[CB] 서킷브레이커! ${price:.0f} → {self.cb_freeze}초 동결")
        self._cb_frozen_until = time.time() + self.cb_freeze

        # 그리드 주문만 취소 (counter-order는 유지 — TP 대기)
        if self.state:
            for lid, lv in self.state.levels.items():
                if lv.order_id and lv.status == "placed":
                    await self.executor.cancel_order_by_id(lv.order_id)
                    lv.status = "cancelled"
                # counter-order 유지 (포지션 TP 기다림)
            await save_grid_state(self.redis, self.state)

        if self.telegram:
            try:
                await self.telegram._send(
                    f"\U0001f6a8 <b>서킷브레이커!</b> ${price:.0f} | {self.cb_freeze}초 동결 후 자동 재개"
                )
            except Exception:
                pass

    # ══════════════════════════════════════════
    #  BOT_KILL
    # ══════════════════════════════════════════

    async def _check_bot_kill(self):
        try:
            balance = await self.executor.get_balance()
        except Exception:
            return

        if balance > self._peak_balance:
            self._peak_balance = balance

        if self._peak_balance <= 0:
            return

        dd_pct = (self._peak_balance - balance) / self._peak_balance * 100
        if dd_pct >= self.bot_kill_pct:
            logger.critical(f"[KILL] BOT_KILL DD -{dd_pct:.1f}% → 전체 청산")
            self._running = False
            await self.stop()
            # 포지션 시장가 청산
            try:
                positions = await self.executor.get_positions()
                for p in positions:
                    contracts = abs(float(p.get("size", 0)))
                    if contracts <= 0:
                        continue
                    side = "sell" if p.get("direction") == "long" else "buy"
                    pos_side = p.get("direction", "long")
                    await self.executor._market_order(side, contracts * 0.01, pos_side, reduce_only=True)
                    logger.info(f"[KILL] 포지션 청산: {pos_side} {contracts}ct")
            except Exception as e:
                logger.error(f"[KILL] 포지션 청산 실패: {e}")
            if self.telegram:
                try:
                    await self.telegram._send(
                        f"\U0001f6a8 <b>BOT_KILL</b> DD -{dd_pct:.1f}% | 잔고 ${balance:.0f}"
                    )
                except Exception:
                    pass

    # ══════════════════════════════════════════
    #  레벨 수 계산 (시작 시 1회)
    # ══════════════════════════════════════════

    async def _calc_levels(self, balance: float):
        price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
        btc_price = float(price_str) if price_str else 78000

        contract_value = btc_price * GRID_SIZE_BTC
        max_notional = balance * self.leverage
        total = min(10, max(2, int(max_notional / contract_value)))
        if total % 2 != 0:
            total -= 1

        self.num_levels = total
        self.half_levels = total // 2
        logger.info(
            f"[GRID] 사이징: ${balance:.0f} × {self.leverage}x = "
            f"{self.num_levels}레벨 ({self.half_levels}+{self.half_levels})"
        )

    # ══════════════════════════════════════════
    #  크래시 복구
    # ══════════════════════════════════════════

    async def _recover(self):
        self.state = await load_grid_state(self.redis)

        try:
            positions = await self.executor.get_positions()
            has_long = any(p.get("direction") == "long" and abs(float(p.get("size", 0))) > 0 for p in positions)
            has_short = any(p.get("direction") == "short" and abs(float(p.get("size", 0))) > 0 for p in positions)
        except Exception as e:
            logger.error(f"[GRID] 복구 포지션 조회 실패: {e}")
            has_long = has_short = False
            positions = []

        try:
            open_orders = await self.executor.exchange.fetch_open_orders(self.symbol)
            open_ids = {o["id"] for o in open_orders}
        except Exception as e:
            logger.error(f"[GRID] 복구 주문 조회 실패: {e}")
            open_ids = set()
            open_orders = []

        if self.state and self.state.is_active:
            logger.info(
                f"[GRID] 복구: center=${self.state.center_price:.0f} "
                f"cycles={self.state.total_cycles} pnl=${self.state.total_pnl:+.2f}"
            )

            for lid, lv in self.state.levels.items():
                if lv.status == "placed" and lv.order_id and lv.order_id not in open_ids:
                    try:
                        order = await self.executor.exchange.fetch_order(lv.order_id, self.symbol)
                        if order.get("status") == "closed":
                            fill_price = float(order.get("average") or order.get("price") or lv.price)
                            lv.status = "filled"
                            lv.fill_price = fill_price
                            lv.fill_time = time.time()
                            logger.info(f"[GRID] 복구: Lv{lid} 체결 @ ${fill_price:.1f}")
                            await self._place_counter_order(lv)
                        else:
                            lv.status = "cancelled"
                    except Exception:
                        lv.status = "cancelled"

                if lv.counter_status == "placed" and lv.counter_order_id and lv.counter_order_id not in open_ids:
                    try:
                        order = await self.executor.exchange.fetch_order(lv.counter_order_id, self.symbol)
                        if order.get("status") == "closed":
                            lv.counter_status = "filled"
                            logger.info(f"[GRID] 복구: Lv{lid} counter 체결")
                        else:
                            lv.counter_status = "none"
                            if lv.status == "filled":
                                await self._place_counter_order(lv)
                    except Exception:
                        lv.counter_status = "none"

            for lid, lv in self.state.levels.items():
                if lv.status == "cancelled":
                    side = "buy" if lv.side == "buy" else "sell"
                    pos_side = "long" if lv.side == "buy" else "short"
                    order = await self.executor.place_limit_order(side, GRID_SIZE_BTC, lv.price, pos_side)
                    if order:
                        lv.order_id = order.get("id")
                        lv.status = "placed"
                        logger.info(f"[GRID] 복구: Lv{lid} 재배치 @ ${lv.price:.1f}")

            await save_grid_state(self.redis, self.state)
            logger.info("[GRID] 복구 완료")
            return

        # State 없는데 포지션 있음 → 나체 포지션 정리
        if has_long or has_short:
            logger.warning(f"[GRID] 나체 포지션 발견 → 정리")
            for p in positions:
                contracts = abs(float(p.get("size", 0)))
                if contracts <= 0:
                    continue
                side = "sell" if p.get("direction") == "long" else "buy"
                pos_side = p.get("direction", "long")
                try:
                    await self.executor._market_order(side, contracts * 0.01, pos_side, reduce_only=True)
                except Exception as e:
                    logger.error(f"[GRID] 나체 포지션 청산 실패: {e}")

        if open_orders:
            await self.executor.cancel_all_orders()

    # ══════════════════════════════════════════
    #  ATR 계산
    # ══════════════════════════════════════════

    async def _compute_atr_pct(self, current_price: float) -> float:
        try:
            candles = await self.db.get_candles(self.symbol, self.atr_tf, limit=self.atr_period + 1)
            if len(candles) < 2:
                return 0.0

            trs = []
            for i in range(1, len(candles)):
                h, l, c_prev = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
                tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
                trs.append(tr)

            atr = sum(trs) / len(trs) if trs else 0
            return (atr / current_price) * 100 if atr > 0 and current_price > 0 else 0.0
        except Exception as e:
            logger.debug(f"[GRID] ATR 계산 실패: {e}")
            return 0.0

    # ══════════════════════════════════════════
    #  외부 인터페이스
    # ══════════════════════════════════════════

    async def stop(self):
        self._running = False
        if self.state:
            for lid, lv in self.state.levels.items():
                if lv.order_id:
                    await self.executor.cancel_order_by_id(lv.order_id)
                if lv.counter_order_id:
                    await self.executor.cancel_order_by_id(lv.counter_order_id)
            await save_grid_state(self.redis, self.state)
        logger.info("[GRID] 정지 완료")

    def get_status(self) -> dict:
        if not self.state:
            return {"active": False}
        return {
            "active": True,
            "center": self.state.center_price,
            "spacing_pct": self.state.spacing_pct,
            "total_cycles": self.state.total_cycles,
            "total_pnl": round(self.state.total_pnl, 2),
            "levels": {
                lid: {"side": lv.side, "price": lv.price, "status": lv.status,
                      "counter": lv.counter_status, "cycles": lv.cycle_count}
                for lid, lv in self.state.levels.items()
            },
        }
