"""
GridEngine — ATR-Adaptive Grid Trading + Leading Regime Detection

BTC 무기한 선물 양방향 그리드 트레이딩.
방향 예측 불필요 — 가격 진동에서 구조적 수익.

메커닉:
  BUY -1,-2 (open long) + SELL +1,+2 (open short)
  체결 → counter-order(TP) → 사이클 완성 → 재배치

ATR 적응:
  spacing = clamp(ATR% × 0.6, 0.15%, 0.50%) 기하식
  1시간마다 재계산

레짐 연동:
  RegimeDetector → ACTIVE/PAUSED/FROZEN
  PAUSED: 미체결 취소, counter 유지
  FROZEN: 전 주문 취소, 60초 동결
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
                 config: dict = None, regime_detector=None):
        self.executor = executor
        self.db = db
        self.redis = redis
        self.telegram = telegram
        self.risk_manager = risk_manager
        self.regime = regime_detector

        cfg = (config or {}).get("grid", {})
        self.enabled = cfg.get("enabled", False)
        self.target_leverage = cfg.get("target_leverage", 8)
        self.atr_mult = cfg.get("atr_mult", 0.6)
        self.spacing_min = cfg.get("spacing_min_pct", 0.15)
        self.spacing_max = cfg.get("spacing_max_pct", 0.50)
        self.rebalance_sec = cfg.get("rebalance_sec", 3600)
        self.drift_pct = cfg.get("drift_rebalance_pct", 50)
        self.monitor_sec = cfg.get("monitor_sec", 1)
        self.atr_period = cfg.get("atr_period", 14)
        self.atr_tf = cfg.get("atr_timeframe", "5m")
        self.maker_fee = (config or {}).get("fees", {}).get("maker", 0.0002)

        self.symbol = (config or {}).get("exchange", {}).get("symbol", "BTC/USDT:USDT")
        self.state: GridState | None = None
        self._running = True
        self._lock = asyncio.Lock()
        self._regime_mode = "ACTIVE"  # 마지막으로 확인한 레짐 모드

        # 잔고 비례 레벨 수 (초기화 시 계산)
        self.num_levels = 2
        self.half_levels = 1

    # ══════════════════════════════════════════
    #  메인 루프
    # ══════════════════════════════════════════

    async def run(self):
        """그리드 메인 루프"""
        if not self.enabled:
            logger.info("[GRID] 비활성화 (grid.enabled=false)")
            return

        # 잔고 기반 레벨 수 + 레버리지 계산
        balance = await self.executor.get_balance()
        await self._calc_auto_levels(balance)

        # 레버리지 설정
        try:
            await self.executor.set_leverage(self.target_leverage, "long")
            await self.executor.set_leverage(self.target_leverage, "short")
        except Exception as e:
            logger.warning(f"[GRID] 레버리지 설정 실패: {e}")

        # RegimeDetector 콜백 등록
        if self.regime:
            self.regime.on_mode_change = self._on_regime_change

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
                if not self.state:
                    await asyncio.sleep(5)
                    continue

                # 레짐 모드 확인 (RegimeDetector가 콜백으로 전환)
                if not self.state.is_active:
                    await asyncio.sleep(3)
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

        # 레벨 생성 + 주문 배치 (기하식: level_n = center × (1+spacing%)^n)
        ratio = 1 + spacing_pct / 100  # e.g. 1.0015

        for i in range(1, self.half_levels + 1):
            # BUY 레벨 (음수) — center 아래
            buy_price = round(price / (ratio ** i), 1)
            buy_level = GridLevel(level_id=-i, side="buy", price=buy_price)
            order = await self.executor.place_limit_order(
                "buy", GRID_SIZE_BTC, buy_price, "long"
            )
            if order:
                buy_level.order_id = order.get("id")
                buy_level.status = "placed"
            self.state.levels[-i] = buy_level

            # SELL 레벨 (양수) — center 위
            sell_price = round(price * (ratio ** i), 1)
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
        if now - getattr(self, "_last_status_log", 0) >= 10:
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

                        # DB 기록 + 리스크 매니저 갱신
                        await self._record_cycle(lv, counter_price, pnl)
                        if self.risk_manager:
                            balance = await self.executor.get_balance()
                            pnl_pct = (pnl / balance * 100) if balance > 0 else 0
                            await self.risk_manager.record_trade_result(pnl_pct, pnl)

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
        fee = GRID_SIZE_BTC * lv.fill_price * self.maker_fee + GRID_SIZE_BTC * counter_price * self.maker_fee
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
    #  레짐 연동 (RegimeDetector 콜백)
    # ══════════════════════════════════════════

    async def _on_regime_change(self, new_mode: str, crs: float):
        """RegimeDetector가 모드 전환 시 호출"""
        old_mode = self._regime_mode
        self._regime_mode = new_mode

        if new_mode == "PAUSED" and self.state and self.state.is_active:
            logger.warning(f"[GRID] 레짐 PAUSED (CRS={crs:.3f}) → 미체결 취소")
            await self._pause("regime_paused")
            if self.telegram:
                try:
                    await self.telegram._send(
                        f"\u26a0\ufe0f <b>Grid PAUSED</b> | CRS={crs:.3f}\n추세 감지 → 미체결 주문 취소"
                    )
                except Exception:
                    pass

        elif new_mode == "FROZEN" and self.state:
            logger.warning(f"[GRID] 서킷브레이커 → FROZEN")
            await self._freeze()
            if self.telegram:
                try:
                    await self.telegram._send(
                        f"\U0001f6a8 <b>서킷브레이커!</b> 전 주문 취소 + 60초 동결"
                    )
                except Exception:
                    pass

        elif new_mode == "ACTIVE" and self.state and not self.state.is_active:
            if self.state.pause_reason in ("regime_paused", "frozen"):
                logger.info(f"[GRID] 레짐 ACTIVE → 그리드 재개")
                self.state.is_active = True
                self.state.pause_reason = None
                await self._rebuild()
                if self.telegram:
                    try:
                        await self.telegram._send(
                            f"\u2705 <b>Grid ACTIVE</b> | 그리드 재개"
                        )
                    except Exception:
                        pass

    async def _freeze(self):
        """서킷브레이커: 전 주문 취소 (counter 포함)"""
        if not self.state:
            return
        self.state.is_active = False
        self.state.pause_reason = "frozen"

        for lid, lv in self.state.levels.items():
            if lv.order_id:
                await self.executor.cancel_order_by_id(lv.order_id)
                if lv.status == "placed":
                    lv.status = "cancelled"
            if lv.counter_order_id:
                await self.executor.cancel_order_by_id(lv.counter_order_id)
                lv.counter_status = "none"
                lv.counter_order_id = None

        await save_grid_state(self.redis, self.state)

    # ══════════════════════════════════════════
    #  잔고 비례 자동 레벨 계산
    # ══════════════════════════════════════════

    async def _calc_auto_levels(self, balance: float):
        """잔고 기반 레벨 수 + 레버리지 자동 계산"""
        price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
        btc_price = float(price_str) if price_str else 78000

        # ATR 기반 레버리지 조절
        atr_pct = await self._compute_atr_pct(btc_price)
        if atr_pct < 0.10:
            self.target_leverage = 10  # 저변동
        elif atr_pct > 0.30:
            self.target_leverage = 6   # 고변동
        else:
            self.target_leverage = 8   # 평시

        contract_value = btc_price * GRID_SIZE_BTC
        max_notional = balance * self.target_leverage
        total = min(10, max(2, int(max_notional / contract_value)))

        # 짝수 강제
        if total % 2 != 0:
            total -= 1

        self.num_levels = total
        self.half_levels = total // 2

        logger.info(
            f"[GRID] 자동 사이징: ${balance:.0f} × {self.target_leverage}x = "
            f"{self.num_levels}레벨 ({self.half_levels} buy + {self.half_levels} sell)"
        )

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
        """시작 시 복구 — Redis state + OKX 포지션/주문 대조"""

        # 1. Redis에서 state 복원
        self.state = await load_grid_state(self.redis)

        # 2. OKX 포지션 확인 (나체 포지션 감지)
        try:
            positions = await self.executor.get_positions()
            has_long = any(p.get("direction") == "long" and abs(float(p.get("size", 0))) > 0 for p in positions)
            has_short = any(p.get("direction") == "short" and abs(float(p.get("size", 0))) > 0 for p in positions)
        except Exception as e:
            logger.error(f"[GRID] 복구 포지션 조회 실패: {e}")
            has_long = has_short = False

        # 3. OKX 오픈 주문 확인
        try:
            open_orders = await self.executor.exchange.fetch_open_orders(self.symbol)
            open_ids = {o["id"] for o in open_orders}
        except Exception as e:
            logger.error(f"[GRID] 복구 주문 조회 실패: {e}")
            open_ids = set()

        # 4-A. State 있으면 → 주문 대조 + 나체 포지션 처리
        if self.state and self.state.is_active:
            logger.info(
                f"[GRID] 복구 시작: center=${self.state.center_price:.0f} "
                f"cycles={self.state.total_cycles} pnl=${self.state.total_pnl:+.2f}"
            )

            for lid, lv in self.state.levels.items():
                # 그리드 주문이 사라졌는지 확인
                if lv.status == "placed" and lv.order_id and lv.order_id not in open_ids:
                    # 체결됐을 가능성 → fetch_order로 확인
                    try:
                        order = await self.executor.exchange.fetch_order(lv.order_id, self.symbol)
                        if order.get("status") == "closed":
                            fill_price = float(order.get("average") or order.get("price") or lv.price)
                            lv.status = "filled"
                            lv.fill_price = fill_price
                            lv.fill_time = time.time()
                            logger.info(f"[GRID] 복구: Lv{lid} 다운타임 중 체결 @ ${fill_price:.1f}")
                            # counter-order 즉시 배치
                            await self._place_counter_order(lv)
                        else:
                            lv.status = "cancelled"
                            logger.info(f"[GRID] 복구: Lv{lid} 주문 사라짐 → 재배치 예정")
                    except Exception:
                        lv.status = "cancelled"

                # counter-order가 사라졌는지 확인
                if lv.counter_status == "placed" and lv.counter_order_id and lv.counter_order_id not in open_ids:
                    try:
                        order = await self.executor.exchange.fetch_order(lv.counter_order_id, self.symbol)
                        if order.get("status") == "closed":
                            lv.counter_status = "filled"
                            logger.info(f"[GRID] 복구: Lv{lid} counter 다운타임 중 체결")
                        else:
                            # counter 사라짐 → 재배치
                            lv.counter_status = "none"
                            if lv.status == "filled":
                                logger.warning(f"[GRID] 복구: Lv{lid} counter 소실 → 재배치")
                                await self._place_counter_order(lv)
                    except Exception:
                        lv.counter_status = "none"

            # 취소된 레벨 재배치
            for lid, lv in self.state.levels.items():
                if lv.status == "cancelled":
                    if lv.side == "buy":
                        order = await self.executor.place_limit_order("buy", GRID_SIZE_BTC, lv.price, "long")
                    else:
                        order = await self.executor.place_limit_order("sell", GRID_SIZE_BTC, lv.price, "short")
                    if order:
                        lv.order_id = order.get("id")
                        lv.status = "placed"
                        logger.info(f"[GRID] 복구: Lv{lid} 재배치 @ ${lv.price:.1f}")

            await save_grid_state(self.redis, self.state)
            logger.info("[GRID] 복구 완료")
            return

        # 4-B. State 없는데 포지션 있음 → 나체 포지션 정리
        if has_long or has_short:
            logger.warning(f"[GRID] State 없는데 포지션 발견 (long={has_long} short={has_short}) → 정리")
            for p in positions:
                contracts = abs(float(p.get("size", 0)))
                if contracts <= 0:
                    continue
                side = "sell" if p.get("direction") == "long" else "buy"
                pos_side = p.get("direction", "long")
                try:
                    await self.executor._market_order(side, contracts * 0.01, pos_side, reduce_only=True)
                    logger.info(f"[GRID] 나체 포지션 청산: {pos_side} {contracts}ct")
                except Exception as e:
                    logger.error(f"[GRID] 나체 포지션 청산 실패: {e}")

        # 미체결 주문도 정리
        if open_orders:
            await self.executor.cancel_all_orders()
            logger.info(f"[GRID] 미체결 {len(open_orders)}개 정리")

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
