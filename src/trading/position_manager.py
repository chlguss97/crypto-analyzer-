import asyncio
import logging
import math
import time
import json
from src.data.storage import Database, RedisClient
from src.trading.executor import OrderExecutor
from src.utils.helpers import load_config

logger = logging.getLogger(__name__)


class Position:
    """활성 포지션 상태"""

    def __init__(self, trade_id: int, symbol: str, direction: str,
                 entry_price: float, size: float, leverage: int,
                 sl_price: float, tp1_price: float, tp2_price: float,
                 tp3_price: float,
                 grade: str, score: float, signals_snapshot: dict = None):
        self.trade_id = trade_id
        self.symbol = symbol
        self.direction = direction
        self.entry_price = entry_price
        self.size = size                  # 원래 사이즈
        self.remaining_size = size        # 잔여 사이즈
        self.leverage = leverage
        self.sl_price = sl_price          # 진입 시 원본 SL (참조용)
        self.current_sl = sl_price        # 본절/TP 이동으로 갱신
        self.tp1_price = tp1_price
        self.tp2_price = tp2_price
        self.tp3_price = tp3_price
        self.grade = grade
        self.score = score
        self.entry_time = int(time.time())
        self.total_fee = 0.0
        self.funding_cost = 0.0
        self.signals_snapshot = signals_snapshot or {}

        # TP 체결 상태
        self.tp1_filled = False
        self.tp2_filled = False  # 러너 모드에서는 미사용 (호환용)
        self.tp3_filled = False  # 러너 모드에서는 미사용

        # 러너 트레일링 (TP1 익절 후 활성화)
        self.runner_mode = False
        self.best_price = 0.0       # 러너 모드 진입 후 최고/최저가
        self.trail_distance = 0.0   # 러너 트레일 거리 (가격 단위, 절대값)

        # 사용자 수동 SL/TP 수정 → self_heal/트레일이 안 덮음
        self.manual_sl_override = False
        self.manual_tp_override = False
        # 04-13: 시간 청산 반복 방지 플래그 (H12)
        self.time_1h_done = False
        self.time_2h_done = False

        # OKX 알고 주문 ID 추적 (cancel/replace 용)
        # 러너 모드에서는 tp2/tp3 사용 안 함 — 호환용으로만 유지
        self.algo_ids: dict[str, str | None] = {
            "sl": None, "tp1": None, "tp2": None, "tp3": None
        }

        # 청산 시도 횟수 (cap 으로 무한 보류 방지 — BUG #C2)
        self.close_attempts = 0

    @property
    def hold_minutes(self) -> int:
        return (int(time.time()) - self.entry_time) // 60

    @property
    def hold_hours(self) -> float:
        return (int(time.time()) - self.entry_time) / 3600

    def price_pnl_pct(self, current_price: float) -> float:
        """가격 변동률 (레버리지 미적용)"""
        if self.direction == "long":
            return (current_price - self.entry_price) / self.entry_price * 100
        else:
            return (self.entry_price - current_price) / self.entry_price * 100

    def pnl_pct(self, current_price: float) -> float:
        """계좌 PnL % (레버리지 적용) — 사용자가 보는 PnL"""
        return self.price_pnl_pct(current_price) * self.leverage

    def to_dict(self) -> dict:
        """
        Redis hset 안전 형태 — 모든 값은 str/int/float (dict/bool 금지).
        entry_time 은 DB/JS 호환 위해 ms 로 변환.
        """
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "size": self.size,
            "remaining_size": self.remaining_size,
            "leverage": self.leverage,
            "sl_price": self.current_sl,
            "tp1_price": self.tp1_price,
            "tp2_price": self.tp2_price,
            "tp3_price": self.tp3_price,
            "grade": self.grade,
            "entry_time": self.entry_time * 1000,  # 초 → ms (JS Date 호환)
            # bool → int (Redis hset 호환)
            "tp1_filled": 1 if self.tp1_filled else 0,
            "tp2_filled": 1 if self.tp2_filled else 0,
            "tp3_filled": 1 if self.tp3_filled else 0,
            "runner_mode": 1 if self.runner_mode else 0,
            "best_price": self.best_price,
            "trail_distance": self.trail_distance,
            "manual_sl_override": 1 if self.manual_sl_override else 0,
            "manual_tp_override": 1 if self.manual_tp_override else 0,
            # dict → JSON 문자열
            "algo_ids": json.dumps(self.algo_ids),
            "hold_minutes": self.hold_minutes,
        }


class PositionManager:
    """포지션 관리: 트레일링, 시간청산, TP, 청산 우선순위"""

    def __init__(self, executor: OrderExecutor, db: Database, redis: RedisClient):
        self.executor = executor
        self.db = db
        self.redis = redis
        self.config = load_config()
        self.trailing_cfg = self.config["trailing"]
        self.risk_cfg = self.config["risk"]

        self.positions: dict[str, Position] = {}  # symbol → Position
        self.on_trade_closed = None  # 콜백: async def(mode, signals, pnl_pct, fee_pct)
        self.telegram = None  # main.py 에서 주입 — 청산 알림용
        self.trade_logger = None  # main.py 에서 주입 — 청산 로그용
        self.risk_manager = None  # main.py 에서 주입 — 매매 결과 리스크 갱신용
        # 동일 심볼 동시 처리 방지 (check_positions vs signal_exit vs close_all)
        self._symbol_locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, symbol: str) -> asyncio.Lock:
        if symbol not in self._symbol_locks:
            self._symbol_locks[symbol] = asyncio.Lock()
        return self._symbol_locks[symbol]

    # 러너 트레일링: TP1 50% 익절 + 잔여 50% 는 트레일링 SL 로 추세 끝까지
    TP1_CLOSE_PCT = 0.5
    # 옛 ratio (호환용) — config 의 trail_margin_pct 가 우선
    RUNNER_TRAIL_RATIO = 0.5

    def _get_trail_distance(self, pos: "Position", price: float) -> float:
        """
        러너 트레일 거리 계산 — 마진 % 기반 + 노이즈 floor

        현재: 옵션 A — dist_min 0.5% (25x leverage 에선 항상 0.5% floor)
              BTC 5분 ATR 활발 시간대 0.25% 의 2배 → 노이즈 방어 + 추세 추격 균형

        ── 진화 backlog ──
        옵션 C (ATR 기반 동적): 시장 변동성 자동 적응
            atr_dist = price * (atr_pct / 100) * 0.8
            return max(atr_dist, price * 0.003)
            장점: 죽은 시간 작게, 활발 시간 크게
            단점: ATR 폭주 시 trail 폭주 (cap 필요), 호출 경로에 atr_pct 전달 필요
            발동 조건: 운영 데이터 1~2주 모인 후, trail SL 발동 패턴 분석 후 도입

        옵션 D (ATR + margin + min/max cap): 가장 정교
            atr_dist    = price * atr_pct/100 * 0.8
            margin_dist = price * (10 / leverage / 100)  # 마진 10% 기반
            dist        = max(atr_dist, margin_dist, price * 0.003)
            dist        = min(dist, price * 0.012)  # 최대 1.2% (마진 30% cap)
            발동 조건: 옵션 C 안정화 후
        """
        risk_cfg = self.config.get("risk", {})
        trail_margin_pct = risk_cfg.get("trail_margin_pct", 5.0)
        min_price_pct = risk_cfg.get("trail_min_price_pct", 0.5)  # 옛 0.2 → 0.5

        # 마진 손실 % / leverage = 가격 변동 %
        dist_from_margin = price * (trail_margin_pct / pos.leverage / 100)
        # 최소 노이즈 보호
        dist_min = price * (min_price_pct / 100)
        dist = max(dist_from_margin, dist_min)

        # 04-13: TP1 거리 대비 cap — trail이 TP1 이익보다 크면 러��가 무의미
        # trail_distance <= TP1 거리 × 0.7 (TP1 이익의 30%는 보전)
        tp1_dist = abs(pos.tp1_price - pos.entry_price)
        if tp1_dist > 0:
            max_trail = tp1_dist * 0.7
            if dist > max_trail:
                logger.info(
                    f"트레일 거리 ${dist:.1f} > TP1거리 ${tp1_dist:.1f}×0.7 "
                    f"→ cap ${max_trail:.1f}"
                )
                dist = max_trail

        return dist

    # OKX BTC-USDT-SWAP 최소 주문: 1 contract = 0.01 BTC
    MIN_ORDER_SIZE_BTC = 0.01

    async def open_position(self, trade_request: dict) -> Position | None:
        """새 포지션 진입 + SL/TP1 서버사이드 등록 (러너 트레일링)"""
        symbol = trade_request["symbol"]

        if symbol in self.positions:
            logger.warning(f"이미 {symbol} 포지션 존재 → 진입 거부")
            return None

        # 최소 주문 사이즈 체크 (계좌 너무 작으면 OKX 가 거부함)
        req_size = float(trade_request.get("size", 0) or 0)
        if req_size < self.MIN_ORDER_SIZE_BTC:
            logger.warning(
                f"⚠️  요청 사이즈 {req_size:.6f} BTC < 최소 {self.MIN_ORDER_SIZE_BTC} BTC "
                f"→ 진입 스킵 (계좌 잔고 부족)"
            )
            return None

        # 주문 실행 (진입만, 보호 주문은 별도)
        order = await self.executor.open_position(
            direction=trade_request["direction"],
            size=trade_request["size"],
            grade=trade_request["grade"],
            entry_price=trade_request.get("entry_price"),
            sl_price=trade_request["sl_price"],
            leverage=trade_request["leverage"],
        )

        if not order:
            return None

        direction = trade_request["direction"]
        fill_price = float(order.get("average") or order.get("price") or 0)
        filled_size = float(order.get("filled") or 0)
        # fee 가 None 일 수 있음 — null-safe
        fee = (order.get("fee") or {}).get("cost", 0) or 0

        # 🔒 fill_price/size 가 즉시 안 오는 경우 (ccxt OKX 시장가 응답 일부)
        # → fetch_positions 로 정확한 entry 와 size 확보 (재시도 포함)
        if fill_price <= 0 or filled_size <= 0:
            for attempt in range(3):  # 최대 3회 재시도
                await asyncio.sleep(0.5 * (attempt + 1))
                try:
                    ex_entry, ex_size = await self.executor.get_position_entry(symbol)
                    if ex_entry > 0 and ex_size > 0:
                        fill_price = ex_entry
                        filled_size = ex_size
                        logger.info(
                            f"진입 정보 fetch 보정 (시도 {attempt+1}): "
                            f"entry=${fill_price:.1f} size={filled_size:.6f}"
                        )
                        break
                except Exception as e:
                    logger.debug(f"fetch_positions 시도 {attempt+1} 실패: {e}")

            if fill_price <= 0 or filled_size <= 0:
                # 3회 재시도해도 못 찾으면 → 거래소엔 진입 됐을 가능성 ↑
                # → close_position 으로 정리 시도, 실패 시 매매 일시 정지 (좀비 방지)
                logger.error(
                    f"🚨 진입 가격/사이즈 확인 3회 실패 → 강제 청산 시도 "
                    f"(ccxt order={order})"
                )
                close_ok = False
                for cattempt in range(3):
                    try:
                        close_order = await self.executor.close_position(
                            direction, float(trade_request["size"]), "fill_price_unknown"
                        )
                        if close_order:
                            close_ok = True
                            break
                    except Exception as e:
                        logger.error(f"강제 청산 시도 {cattempt+1} 실패: {e}")
                    await asyncio.sleep(1)

                if not close_ok:
                    # 좀비 위험 → 매매 차단 (사용자 수동 개입 필요)
                    logger.critical(
                        f"💀 강제 청산 3회 실패 — 거래소에 좀비 포지션 가능. "
                        f"매매 자동 정지. 수동 확인 필요"
                    )
                    try:
                        await self.redis.set("sys:autotrading", "off")
                        if self.telegram:
                            await self.telegram.notify_emergency(
                                f"🚨 좀비 포지션 의심 — 매매 정지. "
                                f"OKX 에서 {symbol} 포지션 수동 확인 필요"
                            )
                    except Exception:
                        pass
                return None

        sl_price = float(trade_request["sl_price"])
        tp1_price = float(trade_request["tp1_price"])
        tp2_price = float(trade_request["tp2_price"])
        tp3_price = float(trade_request.get("tp3_price", tp2_price))

        # 🔒 진입 직후 체결가 vs SL 거리 검증 — 0분 즉사 방지 (04-17)
        # 체결가가 SL보다 이미 나쁜 방향이거나, SL까지 거리 < 0.15% 이면
        # 진입해봤자 바로 sl_failsafe 발동 → 즉시 청산해서 손실만 확정
        MIN_SL_DIST_PCT = 0.15  # 체결가 대비 SL 최소 거리 (0.15% = lev 20x 에서 마진 3%)
        if fill_price > 0 and sl_price > 0:
            if direction == "long":
                sl_dist_pct = (fill_price - sl_price) / fill_price * 100
            else:
                sl_dist_pct = (sl_price - fill_price) / fill_price * 100

            if sl_dist_pct < MIN_SL_DIST_PCT:
                logger.error(
                    f"🚫 SL 거리 부족 → 즉시 청산 | {direction.upper()} "
                    f"fill=${fill_price:.1f} SL=${sl_price:.1f} "
                    f"dist={sl_dist_pct:.3f}% < {MIN_SL_DIST_PCT}% (0분 즉사 방지)"
                )
                try:
                    await self.executor.close_position(direction, filled_size, "sl_too_close")
                except Exception as e:
                    logger.error(f"SL 거리 부족 청산 실패: {e}")
                if self.telegram:
                    try:
                        await self.telegram._send(
                            f"🚫 진입 취소 — SL 거리 부족\n"
                            f"{direction.upper()} ${fill_price:,.0f} → SL ${sl_price:,.0f}\n"
                            f"거리 {sl_dist_pct:.3f}% < 최소 {MIN_SL_DIST_PCT}%"
                        )
                    except Exception:
                        pass
                return None

        # 🔒 진입 직후 SL + TP1 만 서버사이드 등록 (러너 트레일링 모드)
        # 단, 사이즈가 OKX 최소단위 × 2 미만이면 50% 분할 시 0.5 contract 가 되어 거부됨
        # → 1 contract 만 가질 때는 TP1 = 100% 청산 (러너 모드 비활성)
        tp1_fraction = self.TP1_CLOSE_PCT
        will_runner = True
        if filled_size < self.MIN_ORDER_SIZE_BTC * 2:
            # 50% 분할 불가능 — TP1 에서 100% 청산
            tp1_fraction = 1.0
            will_runner = False
            logger.warning(
                f"⚠️  사이즈 {filled_size:.4f} BTC < {self.MIN_ORDER_SIZE_BTC*2} BTC "
                f"→ TP1 에서 100% 청산 (러너 비활성)"
            )

        tp_levels = [
            (tp1_price, tp1_fraction),
        ]
        algo_ids = await self.executor.set_protection(
            direction=direction,
            total_size=float(filled_size),
            sl_price=sl_price,
            tp_levels=tp_levels,
        )

        # 🔒 SL 등록 검증 — OKX에 실제 등록됐는지 확인 (04-17: 0분 즉사 근본 수정)
        # set_protection 이 ID를 반환해도 OKX가 실제로 갖고 있는지 확신 못함
        # → 0.5초 대기 후 pending algos 조회로 검증, 미발견 시 재등록 2회
        if algo_ids.get("sl"):
            await asyncio.sleep(0.5)
            for verify_attempt in range(3):
                try:
                    inst_id = self.executor.exchange.market(symbol)["id"]
                    resp = await self.executor.exchange.private_get_trade_orders_algo_pending(
                        {"instType": "SWAP", "instId": inst_id, "ordType": "trigger"}
                    )
                    pending = resp.get("data", []) if isinstance(resp, dict) else []
                    sl_found = any(
                        (item.get("algoClOrdId") == algo_ids["sl"] or item.get("algoId") == algo_ids["sl"])
                        for item in pending
                    )
                    if sl_found:
                        logger.info(f"✅ SL 등록 검증 OK: id={algo_ids['sl']} (시도 {verify_attempt+1})")
                        break
                    else:
                        logger.warning(
                            f"⚠️  SL 등록 검증 실패 ({verify_attempt+1}/3) — OKX pending에 미발견. 재등록 시도"
                        )
                        new_sl = await self.executor.set_stop_loss(direction, filled_size, sl_price)
                        if new_sl:
                            algo_ids["sl"] = new_sl
                            await asyncio.sleep(0.3)
                        else:
                            algo_ids["sl"] = None
                            break
                except Exception as e:
                    logger.debug(f"SL 검증 조회 예외 ({verify_attempt+1}): {e}")
                    break

        # 🚨 SL 등록 실패 시 진입을 즉시 되돌림 (보호장치 없는 포지션 금지)
        if not algo_ids.get("sl"):
            logger.error(
                f"🚨 SL 알고 등록 실패 → 포지션 즉시 청산 "
                f"({direction.upper()} {filled_size} @ ${fill_price})"
            )
            # 등록된 TP1 부터 정리 (남기면 stale 알고가 됨)
            if algo_ids.get("tp1"):
                await self.executor.cancel_algo_order(algo_ids["tp1"])

            close_ok = False
            for cattempt in range(3):
                try:
                    close_order = await self.executor.close_position(
                        direction, float(filled_size), "sl_protect_failed"
                    )
                    if close_order:
                        close_ok = True
                        break
                except Exception as e:
                    logger.error(f"SL 보호 실패 강제 청산 시도 {cattempt+1} 에러: {e}")
                await asyncio.sleep(1)

            if not close_ok:
                logger.critical(
                    f"💀 SL 등록 실패 + 청산도 실패 — 좀비 위험. 매매 자동 정지"
                )
                try:
                    await self.redis.set("sys:autotrading", "off")
                    if self.telegram:
                        await self.telegram.notify_emergency(
                            f"🚨 SL 보호 실패 + 청산 실패 — 매매 정지. "
                            f"OKX 에서 {symbol} 포지션 수동 확인"
                        )
                except Exception:
                    pass
            return None

        # DB 기록 (실패해도 메모리에는 기록 — 좀비 방지)
        try:
            trade_id = await self.db.insert_trade({
                "symbol": symbol,
                "direction": direction,
                "grade": trade_request["grade"],
                "score": trade_request["score"],
                "entry_price": fill_price,
                "entry_time": int(time.time() * 1000),
                "leverage": trade_request["leverage"],
                "position_size": filled_size * fill_price,
                "signals_snapshot": json.dumps(trade_request.get("signals_snapshot", {})),
            })
        except Exception as e:
            logger.error(f"DB insert_trade 실패: {e} → 임시 ID 로 진행")
            trade_id = -int(time.time())  # 음수 임시 ID (DB 갱신 시도 안 함)

        pos = Position(
            trade_id=trade_id,
            symbol=symbol,
            direction=direction,
            entry_price=float(fill_price),
            size=float(filled_size),
            leverage=trade_request["leverage"],
            sl_price=sl_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            tp3_price=tp3_price,
            grade=trade_request["grade"],
            score=trade_request["score"],
            signals_snapshot=trade_request.get("signals_snapshot", {}),
        )
        pos.total_fee = float(fee)
        pos.algo_ids = algo_ids
        self.positions[symbol] = pos

        await self.redis.hset(f"pos:active:{symbol}", pos.to_dict(), ttl=86400)

        if not algo_ids.get("tp1"):
            logger.warning("⚠️  TP1 등록 실패 → 봇 폴링이 가격 기반으로 백업")

        # 검증용 상세 로그
        notional = pos.size * pos.entry_price
        margin = notional / pos.leverage
        sl_pnl_pct = pos.pnl_pct(pos.sl_price)
        tp1_pnl_pct = pos.pnl_pct(pos.tp1_price)
        logger.info(
            f"포지션 오픈: {pos.direction.upper()} {symbol} | "
            f"진입 ${pos.entry_price:,.1f} | size {pos.size:.6f} BTC "
            f"(노션 ${notional:,.0f}, 마진 ${margin:,.2f}, {pos.leverage}x) | "
            f"SL ${pos.sl_price:,.1f} ({sl_pnl_pct:+.1f}% 계좌) | "
            f"TP1 ${pos.tp1_price:,.1f} ({tp1_pnl_pct:+.1f}% 계좌, 50% 익절 후 러너) | "
            f"등급 {pos.grade} | "
            f"algo: sl={algo_ids.get('sl')} tp1={algo_ids.get('tp1')}"
        )

        return pos

    async def check_positions(self, current_price: float):
        """
        활성 포지션 체크 (15초마다 호출)
        우선순위:
          1. SL 강제 청산 (failsafe — OKX 알고가 못 뛰는 경우 대비)
          2. 거래소 사이즈 동기화 (서버사이드 TP 체결 감지)
          3. 가격 기반 TP1/TP2/TP3 도달 → 부분익절 + SL 끌어올리기 (반익본절)
          4. 시간 기반 청산
        """
        if not current_price or current_price <= 0:
            return

        for symbol, pos in list(self.positions.items()):
            if not pos.entry_price or pos.entry_price <= 0:
                continue

            # 동일 심볼 동시 처리 방지 (signal_exit/close_all 과 race 차단)
            lock = self._get_lock(symbol)
            if lock.locked():
                continue  # 다른 코루틴이 처리 중 → 다음 폴링에서
            async with lock:
                # 락 획득 후 재확인 (그 사이 청산됐을 수 있음)
                if symbol not in self.positions:
                    continue
                pos = self.positions[symbol]
                await self._process_position(symbol, pos, current_price)

    async def _process_position(self, symbol: str, pos: "Position", current_price: float):
        """단일 포지션 처리 — lock 안에서 호출됨"""
        # 1. SL failsafe — 가격이 봇 내부 SL을 넘었으면 즉시 청산
        sl_breached = (
            (pos.direction == "long" and current_price <= pos.current_sl) or
            (pos.direction == "short" and current_price >= pos.current_sl)
        )
        if sl_breached:
            logger.warning(
                f"🛑 SL failsafe 발동: {pos.direction.upper()} "
                f"가격 ${current_price:.0f} vs SL ${pos.current_sl:.0f}"
            )
            await self._cancel_all_algos(pos)
            await self._full_close(pos, "sl_failsafe")
            return

        # 2. 거래소 사이즈 동기화 — 서버사이드 TP/SL 체결 감지
        try:
            ex_size = await self.executor.get_position_size(symbol)
            if 0 <= ex_size < 1e-8:
                # 외부에서 전량 청산됨 (서버 SL/TP, 강제청산, 수동 등)
                logger.warning(f"포지션 외부 종료 감지 (사이즈≈0) → 정리: {symbol}")
                await self._cancel_all_algos(pos)
                await self._reconcile_external_close(pos, current_price)
                return
            elif ex_size > 0 and ex_size < pos.remaining_size * 0.95:
                # 부분 체결 감지 (서버사이드 TP1 이 발동한 경우)
                closed_amount = pos.remaining_size - ex_size
                pct_closed = closed_amount / pos.remaining_size
                logger.info(
                    f"부분 체결 감지: 봇={pos.remaining_size:.6f} → "
                    f"거래소={ex_size:.6f} ({pct_closed*100:.0f}%)"
                )
                pos.remaining_size = ex_size

                # 잔여가 최소 주문 미만이면 러너 모드 비활성 + 즉시 전체 청산
                if ex_size < self.MIN_ORDER_SIZE_BTC:
                    logger.warning(
                        f"잔여 {ex_size:.6f} < 최소 {self.MIN_ORDER_SIZE_BTC} → "
                        f"러너 비활성 + 잔여 청산"
                    )
                    await self._cancel_all_algos(pos)
                    await self._full_close(pos, "tp1_partial_residual")
                    return

                # 🔒 서버 TP1 발동 → 이중 처리 방지 마킹
                if not pos.tp1_filled:
                    pos.tp1_filled = True
                    pos.algo_ids["tp1"] = None

                    fee_offset = pos.entry_price * 0.001
                    new_sl = (pos.entry_price + fee_offset) if pos.direction == "long" \
                        else (pos.entry_price - fee_offset)
                    await self._move_sl(pos, new_sl, label="본절(서버TP)")

                    pos.runner_mode = True
                    # 러너 모드 전환 → TP2/TP3 미사용 → 즉시 정리 (OKX 잔존 방지)
                    await self._cancel_unused_tps(pos)
                    # 04-13: best_price를 최소 TP1 가격으로 설정
                    # 서버 TP1은 TP1가 이상에서 체결됐지만, 폴링 시점 가격은 이미 하락했을 수 있음
                    if pos.direction == "long":
                        pos.best_price = max(current_price, pos.tp1_price)
                    else:
                        pos.best_price = min(current_price, pos.tp1_price) if current_price > 0 else pos.tp1_price
                    pos.trail_distance = self._get_trail_distance(pos, pos.best_price)
                    logger.info(
                        f"✅ 서버 TP1 자동 체결 감지 → SL 본전 ${new_sl:.0f} | "
                        f"🏃 러너 모드 ON (best ${pos.best_price:.0f}, 트레일 ${pos.trail_distance:.1f})"
                    )
                    # 텔레그램 알림 — TP1 hit + 본절 이동 + 러너 ON
                    if self.telegram:
                        try:
                            await self.telegram.notify_tp1_hit(
                                pos.direction, pos.tp1_price, new_sl,
                                runner_active=True,
                                trail_distance=pos.trail_distance,
                            )
                        except Exception:
                            pass
        except Exception as e:
            logger.error(f"포지션 사이즈 동기화 실패: {e}")

        # 3. 가격 기반 TP 도달 처리 + 반익본절 SL 끌어올리기
        await self._handle_tp_progression(pos, current_price)

        # 종료됐으면 종료
        if symbol not in self.positions:
            return

        # 4. 시간 청산
        await self._check_time_exit(pos, current_price)

        if symbol not in self.positions:
            return

        # 5. Self-heal — SL/TP 알고가 None 이면 재등록 시도 (네트워크 복구 시 자동 복원)
        await self._self_heal_algos(pos)

        # 6. Redis 상태 갱신 (대시보드 hold_minutes/current_sl 실시간 표시)
        try:
            await self.redis.hset(f"pos:active:{symbol}", pos.to_dict(), ttl=86400)
        except Exception as e:
            logger.debug(f"Redis 포지션 갱신 실패: {e}")

    async def _self_heal_algos(self, pos: "Position"):
        """SL/TP 알고가 등록 실패해서 None 이면 재등록 시도 (수동 override 도 존중)"""
        if pos.remaining_size <= 0:
            return

        # SL 자동 복구 — 사용자 수동 수정한 경우에도 None 이면 그 가격으로 재등록
        if not pos.algo_ids.get("sl"):
            try:
                new_id = await self.executor.set_stop_loss(
                    pos.direction, pos.remaining_size, pos.current_sl
                )
                if new_id:
                    pos.algo_ids["sl"] = new_id
                    logger.info(f"🔧 SL 알고 자동 복구: ${pos.current_sl:.0f} id={new_id}")
            except Exception as e:
                logger.debug(f"SL 자동 복구 실패: {e}")

        # TP1 자동 복구 (러너 모드 아니고 tp1_filled 아닐 때만)
        if not pos.runner_mode and not pos.tp1_filled and not pos.algo_ids.get("tp1"):
            try:
                small = pos.size < self.MIN_ORDER_SIZE_BTC * 2
                tp1_size = pos.remaining_size if small else (pos.remaining_size * self.TP1_CLOSE_PCT)
                # 0.01 floor
                tp1_size = math.floor(tp1_size / self.MIN_ORDER_SIZE_BTC) * self.MIN_ORDER_SIZE_BTC
                tp1_size = round(tp1_size, 4)
                if tp1_size >= self.MIN_ORDER_SIZE_BTC:
                    new_id = await self.executor.set_take_profit(
                        pos.direction, tp1_size, pos.tp1_price, level=1
                    )
                    if new_id:
                        pos.algo_ids["tp1"] = new_id
                        logger.info(f"🔧 TP1 알고 자동 복구: ${pos.tp1_price:.0f} id={new_id}")
            except Exception as e:
                logger.debug(f"TP1 자동 복구 실패: {e}")

    def _tp_reached(self, pos: Position, current_price: float, tp_price: float) -> bool:
        if pos.direction == "long":
            return current_price >= tp_price
        return current_price <= tp_price

    async def _handle_tp_progression(self, pos: Position, current_price: float):
        """
        러너 트레일링 진행:
          - TP1 미체결: 가격이 TP1 닿으면 50% 익절 + SL 본절 + 러너 모드 활성화
          - 러너 모드: 가격이 새 고/저 갱신할 때마다 트레일링 SL 끌어올림
        """

        # TP1 도달 처리
        if not pos.tp1_filled and self._tp_reached(pos, current_price, pos.tp1_price):
            # 사이즈가 최소단위 × 2 미만이면 분할 불가 → 직접 전량 청산
            small_position = pos.size < self.MIN_ORDER_SIZE_BTC * 2
            if small_position:
                pos.tp1_filled = True
                logger.info(f"✅ TP1 도달 @ ${pos.tp1_price:.0f} → 100% 청산 (소형 포지션)")
                await self._cancel_all_algos(pos)
                await self._full_close(pos, "tp1_full")
                return

            await self._on_tp_hit(pos, level=1, close_pct=self.TP1_CLOSE_PCT)
            if pos.symbol not in self.positions:
                return
            if not pos.tp1_filled:
                return  # 부분청산 실패 → 다음 폴링 재시도

            # 50% 청산 후 잔여가 너무 작으면 (부동소수 오차) 종료
            if pos.remaining_size < 1e-8:
                await self._cancel_all_algos(pos)
                await self._full_close(pos, "tp1_residual")
                return

            # SL → 본전 + 수수료 보상 (manual override 도 본절은 봇이 진행 — 안전)
            fee_offset = pos.entry_price * 0.001
            new_sl = pos.entry_price + fee_offset if pos.direction == "long" \
                else pos.entry_price - fee_offset
            await self._move_sl(pos, new_sl, label="본절")
            pos.manual_sl_override = False  # TP1 후 본절은 봇 자동 진행

            # 러너 모드 활성화 — 트레일 거리는 마진 % 기준
            pos.runner_mode = True
            # 04-13: best_price를 최소 TP1 가격으로 설정
            # 폴링 지연으로 current_price가 TP1보다 낮을 수 있음 → 트레일 시작점 손해 방지
            if pos.direction == "long":
                pos.best_price = max(current_price, pos.tp1_price)
            else:
                pos.best_price = min(current_price, pos.tp1_price) if current_price > 0 else pos.tp1_price
            pos.trail_distance = self._get_trail_distance(pos, pos.best_price)

            # 러너 모드 전환 → TP2/TP3 미사용 → 즉시 정리 (OKX 잔존 방지)
            await self._cancel_unused_tps(pos)

            logger.info(
                f"✅ TP1 익절 50% @ ${pos.tp1_price:.0f} → SL 본전 ${new_sl:.0f} | "
                f"🏃 러너 모드 ON (best ${pos.best_price:.0f}, 트레일 ${pos.trail_distance:.1f} = "
                f"{pos.trail_distance/pos.best_price*100*pos.leverage:.1f}% 마진)"
            )
            # 텔레그램 알림 — TP1 hit + 본절 이동 + 러너 ON
            if self.telegram:
                try:
                    await self.telegram.notify_tp1_hit(
                        pos.direction, pos.tp1_price, new_sl,
                        runner_active=True,
                        trail_distance=pos.trail_distance,
                    )
                except Exception:
                    pass
            # 04-13: return 제거 → 즉시 아래 러너 트레일링 실행
            # (best_price=TP1 기준으로 SL을 본전보다 높게 올릴 수 있음)

        # 러너 모드 트레일링 — 가격이 새 고/저 갱신 시 SL 추격
        if pos.runner_mode:
            await self._update_runner_trail(pos, current_price)

    async def _update_runner_trail(self, pos: Position, current_price: float):
        """
        러너 모드: 가격이 새 고/저 갱신 시 트레일링 SL 끌어올림
        - 사용자가 SL 수동 수정한 경우 (manual_sl_override) 트레일 OFF
        """
        if pos.manual_sl_override:
            # 수동 SL 우선 — 단지 best_price 만 업데이트
            if pos.direction == "long":
                if current_price > pos.best_price:
                    pos.best_price = current_price
            else:
                if current_price < pos.best_price or pos.best_price == 0:
                    pos.best_price = current_price
            return

        moved = False
        if pos.direction == "long":
            if current_price > pos.best_price:
                pos.best_price = current_price
                new_sl = current_price - pos.trail_distance
                if new_sl > pos.current_sl:
                    await self._move_sl(pos, new_sl, label="러너트레일")
                    moved = True
        else:
            if current_price < pos.best_price or pos.best_price == 0:
                pos.best_price = current_price
                new_sl = current_price + pos.trail_distance
                if new_sl < pos.current_sl:
                    await self._move_sl(pos, new_sl, label="러너트레일")
                    moved = True

        if moved:
            logger.info(
                f"🏃 러너 트레일: 신고점 ${pos.best_price:.0f} → SL ${pos.current_sl:.0f}"
            )

    async def _on_tp_hit(self, pos: Position, level: int, close_pct: float):
        """봇이 TP 가격 도달을 먼저 감지한 경우 — 부분 청산 + 해당 알고 취소"""
        # 해당 TP 알고 취소 (서버가 동시에 발동하기 전)
        tp_key = f"tp{level}"
        algo_id = pos.algo_ids.get(tp_key)
        if algo_id:
            await self.executor.cancel_algo_order(algo_id)
            pos.algo_ids[tp_key] = None

        # 부분 청산 (시장가 reduceOnly)
        size_before = pos.remaining_size
        await self._partial_close(pos, close_pct, tp_key)

        # 거래소 사이즈로 보정 (race / 부분 fail 대비)
        try:
            ex_size = await self.executor.get_position_size(pos.symbol)
            if 0 <= ex_size < pos.remaining_size:
                pos.remaining_size = max(0.0, ex_size)
        except Exception as e:
            logger.debug(f"_on_tp_hit 사이즈 동기화 실패: {e}")

        # 청산이 실제로 일어난 경우만 filled 마킹
        if pos.remaining_size < size_before * 0.95:
            setattr(pos, f"{tp_key}_filled", True)
        else:
            logger.warning(
                f"⚠️  TP{level} 부분 청산 실패 (size 변화 없음: {size_before}→{pos.remaining_size}) "
                f"— 다음 폴링에서 재시도"
            )

    async def _move_sl(self, pos: Position, new_sl: float, label: str):
        """SL 알고 cancel + 새로 등록 (잔여 사이즈 기준)"""
        old_id = pos.algo_ids.get("sl")
        new_id = await self.executor.update_stop_loss(
            pos.direction, pos.remaining_size, new_sl, old_id
        )
        pos.algo_ids["sl"] = new_id
        pos.current_sl = new_sl
        if not new_id:
            logger.warning(f"⚠️  SL 갱신 실패 ({label}) → 봇 내부 SL만 적용 (failsafe로 동작)")

    async def _cancel_all_algos(self, pos: Position):
        """포지션의 모든 SL/TP 알고 주문 취소 + OKX 잔존 알고 정리"""
        # 1. pos.algo_ids 기반 취소 (알려진 ID)
        for key in ("sl", "tp1", "tp2", "tp3"):
            algo_id = pos.algo_ids.get(key)
            if algo_id:
                await self.executor.cancel_algo_order(algo_id)
                pos.algo_ids[key] = None

        # 2. OKX에 남아있는 모든 알고도 정리 (ID 모르는 잔존 알고 방지)
        try:
            await self.executor.cancel_all_algos()
        except Exception as e:
            logger.debug(f"잔존 알고 전체 정리 실패 (무시): {e}")

    async def _cancel_unused_tps(self, pos: Position):
        """
        러너 모드 활성 or TP1 체결 후 미사용이 된 TP2/TP3 알고 정리.
        봇이 TP2/TP3을 쓰지 않는 상태에서 이들이 OKX 서버에 살아있으면 '잔존 미체결 주문'
        으로 남아 재진입 / 레버리지 변경 시 충돌 유발.
        """
        for key in ("tp2", "tp3"):
            algo_id = pos.algo_ids.get(key)
            if algo_id:
                try:
                    await self.executor.cancel_algo_order(algo_id)
                except Exception as e:
                    logger.debug(f"{key} 취소 예외: {e}")
                pos.algo_ids[key] = None
                logger.info(f"🧹 미사용 {key.upper()} 정리 (러너/TP1후)")

    async def _resize_protection(self, pos: Position, label: str):
        """
        부분 청산 후 사이즈가 달라졌을 때 — SL/TP1 을 새 사이즈로 재등록.
        기존 알고는 이전 사이즈로 등록되어 있어 reduce-only 충돌 가능.
        TP2/TP3 는 러너 모드 or TP1 체결 후에는 미사용 → 정리.
        """
        if pos.remaining_size <= 0:
            return

        # SL 재등록 (새 사이즈)
        old_sl = pos.algo_ids.get("sl")
        try:
            new_sl_id = await self.executor.update_stop_loss(
                pos.direction, pos.remaining_size, pos.current_sl, old_sl
            )
            pos.algo_ids["sl"] = new_sl_id
            if not new_sl_id:
                logger.warning(f"⚠️  SL 사이즈 갱신 실패 ({label})")
        except Exception as e:
            logger.error(f"SL 사이즈 갱신 예외 ({label}): {e}")

        # TP1 재등록 — 러너 아니고 TP1 미체결 시에만
        if not pos.runner_mode and not pos.tp1_filled:
            old_tp1 = pos.algo_ids.get("tp1")
            if old_tp1:
                try:
                    await self.executor.cancel_algo_order(old_tp1)
                except Exception:
                    pass
                pos.algo_ids["tp1"] = None
            # 새 사이즈로 TP1 재등록
            try:
                small = pos.size < self.MIN_ORDER_SIZE_BTC * 2
                tp1_size = pos.remaining_size if small else (pos.remaining_size * self.TP1_CLOSE_PCT)
                tp1_size = math.floor(tp1_size / self.MIN_ORDER_SIZE_BTC) * self.MIN_ORDER_SIZE_BTC
                tp1_size = round(tp1_size, 4)
                if tp1_size >= self.MIN_ORDER_SIZE_BTC:
                    new_tp1 = await self.executor.set_take_profit(
                        pos.direction, tp1_size, pos.tp1_price, level=1
                    )
                    if new_tp1:
                        pos.algo_ids["tp1"] = new_tp1
                        logger.info(f"🔧 TP1 사이즈 갱신 ({label}): ${pos.tp1_price:.0f} sz={tp1_size}")
            except Exception as e:
                logger.error(f"TP1 재등록 예외 ({label}): {e}")

        # TP2/TP3 는 항상 미사용 → 정리 (러너든 아니든 TP1 방식에서 더 이상 안 씀)
        await self._cancel_unused_tps(pos)

    async def _reconcile_external_close(self, pos: Position, last_price: float):
        """외부에서 포지션이 전량 청산된 경우 DB/콜백 정리"""
        # 04-15: OKX 최근 체결에서 실제 exit price 조회 시도
        try:
            trades = await self.executor.exchange.fetch_my_trades(pos.symbol, limit=5)
            if trades:
                last_trade = trades[-1]
                last_price = float(last_trade.get("price", last_price))
        except Exception:
            pass  # 실패 시 폴링 가격 사용
        pnl_now = pos.pnl_pct(last_price) if last_price > 0 else 0
        small_position = pos.size < self.MIN_ORDER_SIZE_BTC * 2

        if pos.runner_mode:
            reason = "runner_trail_hit" if pnl_now >= 0 else "runner_sl_hit"
        elif pos.tp1_filled:
            reason = "breakeven_hit"
        elif small_position and pnl_now > 0:
            reason = "tp1_full_server"
        else:
            reason = "sl_or_forced"

        # 정리는 _finalize_position 으로 일관화 (텔레그램/ML/DB/Redis)
        await self._finalize_position(pos, reason, last_price)

    async def _check_time_exit(self, pos: Position, current_price: float) -> bool:
        """
        시간 기반 청산 (계좌 PnL % 기준)
        러너 모드 활성화 시 = 큰 추세 잡고 있는 중이므로 시간 청산 완화 (트레일링 SL 에 위임)
        """
        hours = pos.hold_hours
        pnl = pos.pnl_pct(current_price)  # 계좌 PnL %

        # 러너 모드: 추세가 살아있는 한 트레일링 SL 에 맡기고, 8시간 hard limit 만 적용
        if pos.runner_mode:
            if hours >= 8:
                await self._cancel_all_algos(pos)
                await self._full_close(pos, "time_8h_runner")
                return True
            return False

        # 6시간 → 무조건 전량 청산
        if hours >= 6:
            await self._cancel_all_algos(pos)
            await self._full_close(pos, "time_6h")
            return True

        # 2시간 → TP1 미체결 시 75% 청산 (1회만)
        if hours >= 2 and not pos.tp1_filled and not pos.time_2h_done:
            # 잔여가 최소단위 미만 되면 전체 청산
            close_pct = 0.75
            if pos.remaining_size * (1 - close_pct) < self.MIN_ORDER_SIZE_BTC:
                await self._cancel_all_algos(pos)
                await self._full_close(pos, "time_2h_full")
                return True
            await self._partial_close(pos, close_pct, "time_2h")
            pos.time_2h_done = True  # 04-13: 반복 방지 (H12)
            if pos.remaining_size > 0:
                # SL/TP1 전부 새 사이즈로 재등록 + TP2/TP3 정리
                await self._resize_protection(pos, label="time_2h")
            return False

        # 1시간 → 수익 < 3% 시 50% 청산 (1회만)
        if hours >= 1 and not pos.tp1_filled and pnl < 3.0 and not pos.time_1h_done:
            close_pct = 0.5
            if pos.remaining_size * (1 - close_pct) < self.MIN_ORDER_SIZE_BTC:
                await self._cancel_all_algos(pos)
                await self._full_close(pos, "time_1h_full")
                return True
            await self._partial_close(pos, close_pct, "time_1h")
            pos.time_1h_done = True  # 04-13: 반복 방지 (H12)
            if pos.remaining_size > 0:
                # SL/TP1 전부 새 사이즈로 재등록 + TP2/TP3 정리
                await self._resize_protection(pos, label="time_1h")
            return False

        return False

    async def signal_exit(self, symbol: str, reason: str):
        """시그널 기반 청산 (1H CHoCH, 반대 Grade A 등)"""
        async with self._get_lock(symbol):
            if symbol in self.positions:
                pos = self.positions[symbol]
                await self._cancel_all_algos(pos)
                await self._full_close(pos, reason)

    # ── 사용자 수동 SL/TP 수정 (대시보드에서 호출) ──

    async def manual_update_sl(self, symbol: str, new_sl_price: float) -> dict:
        """
        사용자가 대시보드에서 SL 가격 직접 수정.
        - sanity check: 방향 일치 (long: SL < entry, short: SL > entry)
        - cancel old SL algo + 새 SL 등록
        - manual_override 플래그 set → self_heal 이 덮어쓰지 않음
        """
        async with self._get_lock(symbol):
            if symbol not in self.positions:
                return {"ok": False, "reason": "no_position"}
            pos = self.positions[symbol]

            # sanity: SL 가격이 방향에 맞는지
            if pos.direction == "long" and new_sl_price >= pos.entry_price:
                return {"ok": False, "reason": "long_sl_must_be_below_entry"}
            if pos.direction == "short" and new_sl_price <= pos.entry_price:
                return {"ok": False, "reason": "short_sl_must_be_above_entry"}

            # 너무 먼 SL (마진 50% 이상 손실 위험) 거부
            sl_loss_pct = abs(new_sl_price - pos.entry_price) / pos.entry_price * 100 * pos.leverage
            if sl_loss_pct > 50:
                return {"ok": False, "reason": f"sl_too_far ({sl_loss_pct:.0f}% margin loss)"}

            await self._move_sl(pos, new_sl_price, label="manual")
            pos.manual_sl_override = True  # self_heal 이 안 덮음
            try:
                await self.redis.hset(f"pos:active:{symbol}", pos.to_dict(), ttl=86400)
            except Exception:
                pass
            logger.info(
                f"📝 사용자 SL 수정: {symbol} {pos.direction.upper()} → ${new_sl_price:.1f} "
                f"({sl_loss_pct:.1f}% 마진 손실)"
            )
            return {"ok": True, "new_sl": new_sl_price, "margin_loss_pct": sl_loss_pct}

    async def manual_update_tp(self, symbol: str, new_tp_price: float) -> dict:
        """
        사용자가 대시보드에서 TP1 가격 직접 수정.
        - sanity check + cancel old TP1 + 새 TP1 등록
        """
        async with self._get_lock(symbol):
            if symbol not in self.positions:
                return {"ok": False, "reason": "no_position"}
            pos = self.positions[symbol]

            if pos.tp1_filled:
                return {"ok": False, "reason": "tp1_already_filled"}

            # sanity: TP 가격이 방향에 맞는지
            if pos.direction == "long" and new_tp_price <= pos.entry_price:
                return {"ok": False, "reason": "long_tp_must_be_above_entry"}
            if pos.direction == "short" and new_tp_price >= pos.entry_price:
                return {"ok": False, "reason": "short_tp_must_be_below_entry"}

            # 옛 TP1 알고 cancel
            old_id = pos.algo_ids.get("tp1")
            if old_id:
                await self.executor.cancel_algo_order(old_id)
                pos.algo_ids["tp1"] = None

            # 새 TP1 등록 (사이즈 계산)
            small = pos.size < self.MIN_ORDER_SIZE_BTC * 2
            tp1_size = pos.remaining_size if small else (pos.remaining_size * self.TP1_CLOSE_PCT)
            tp1_size = round(math.floor(tp1_size / self.MIN_ORDER_SIZE_BTC) * self.MIN_ORDER_SIZE_BTC, 4)
            if tp1_size <= 0:
                return {"ok": False, "reason": "size_too_small"}

            new_id = await self.executor.set_take_profit(
                pos.direction, tp1_size, new_tp_price, level=1
            )
            if not new_id:
                return {"ok": False, "reason": "tp_register_failed"}

            pos.algo_ids["tp1"] = new_id
            pos.tp1_price = new_tp_price
            pos.manual_tp_override = True
            try:
                await self.redis.hset(f"pos:active:{symbol}", pos.to_dict(), ttl=86400)
            except Exception:
                pass
            tp_gain_pct = abs(new_tp_price - pos.entry_price) / pos.entry_price * 100 * pos.leverage
            logger.info(
                f"📝 사용자 TP1 수정: {symbol} {pos.direction.upper()} → ${new_tp_price:.1f} "
                f"({tp_gain_pct:.1f}% 마진 익절)"
            )
            return {"ok": True, "new_tp": new_tp_price, "margin_gain_pct": tp_gain_pct}

    async def _partial_close(self, pos: Position, close_pct: float, reason: str):
        """
        부분 청산. close_size 는 OKX contract 단위 (0.01 BTC) 로 floor
        → fractional contract 거부 방지 (예: 0.015 BTC = 1.5 contract X)
        """
        raw_close = pos.remaining_size * close_pct
        # 0.01 BTC (1 contract) 단위로 floor
        close_size = math.floor(raw_close / self.MIN_ORDER_SIZE_BTC) * self.MIN_ORDER_SIZE_BTC
        close_size = round(close_size, 4)

        if close_size <= 0:
            logger.warning(
                f"부분 청산 스킵 ({reason}): close_size={close_size} (잔여 {pos.remaining_size})"
            )
            return

        # 잔여가 close 후 너무 작으면 (1 contract 미만) 전체 청산으로 변환
        remaining_after = pos.remaining_size - close_size
        if 0 < remaining_after < self.MIN_ORDER_SIZE_BTC:
            logger.info(
                f"부분 청산 → 전체 청산 변환 ({reason}): "
                f"잔여 후 {remaining_after:.6f} < 최소"
            )
            close_size = pos.remaining_size

        order = await self.executor.close_partial(
            pos.direction, close_size, 1.0, reason
        )
        if order:
            pos.remaining_size -= close_size
            if pos.remaining_size < 1e-8:
                pos.remaining_size = 0.0
            fee = (order.get("fee") or {}).get("cost", 0) or 0
            pos.total_fee += float(fee)

            logger.info(
                f"부분 청산 ({reason}): {close_pct*100:.0f}% req → "
                f"실제 {close_size:.4f} | 잔여: {pos.remaining_size:.4f}"
            )

    async def _full_close(self, pos: Position, reason: str):
        """
        전량 청산.
        - remaining_size 가 이미 0 인 경우 (다른 경로로 청산 완료) → finalize 만
        - 청산 실패 시: SL 재등록 + 메모리 유지 (좀비 방지) + 다음 폴링 재시도
        - 청산 성공 시: 잔존 알고 정리 + finalize
        - 예외 발생 시: 알고 정리 + 메모리 유지 (다음 폴링 재시도)
        """
        # entry_price 무결성 체크
        if not pos.entry_price or pos.entry_price <= 0:
            logger.error(f"포지션 청산 실패: entry_price 무효 ({pos.entry_price})")
            if pos.symbol in self.positions:
                del self.positions[pos.symbol]
            return

        # 이미 청산된 케이스 (small position TP1 100% 후 등) → finalize 만
        if pos.remaining_size <= 1e-8:
            await self._finalize_position(pos, reason, exit_price=0)
            return

        # 청산 시도 — 예외 포착으로 _finalize 누락 방지
        try:
            order = await self.executor.close_position(
                pos.direction, pos.remaining_size, reason
            )
        except Exception as e:
            logger.error(f"청산 중 예외 ({reason}): {e}")
            pos.close_attempts += 1
            # 예외 발생해도 OKX 알고는 확실히 정리
            try:
                await self._cancel_all_algos(pos)
            except Exception as e2:
                logger.error(f"예외 후 알고 정리도 실패: {e2}")
            return  # 다음 폴링 재시도

        if not order:
            # 🚨 청산 실패 — 좀비 방지: SL 알고 긴급 재등록 + 메모리 유지
            pos.close_attempts += 1

        # OKX "포지션 이미 없음" (51169) 응답 → 메모리 정리만 (close 재시도 불필요)
        if order and order.get("already_closed"):
            logger.info(
                f"📌 포지션 이미 청산됨 ({reason}) → 메모리 정리만 (close 재시도 없음)"
            )
            # 거래소에서 실제 가격 fetch
            await self._finalize_position(pos, reason, exit_price=0)
            return

        if not order:
            # 04-15: 지수 백오프 — 연속 실패 시 대기 시간 증가 (API ban 방지)
            backoff = min(30, 2 ** min(pos.close_attempts, 5))  # 1,2,4,8,16,30초
            logger.error(
                f"🚨 청산 실패 ({reason}) {pos.close_attempts}/10 → "
                f"SL 긴급 재등록 + {backoff}초 후 재시도"
            )
            await asyncio.sleep(backoff)
            try:
                # 호출자가 _cancel_all_algos 를 미리 불렀으므로 SL 다시 만들어야 함
                new_id = await self.executor.set_stop_loss(
                    pos.direction, pos.remaining_size, pos.current_sl
                )
                pos.algo_ids["sl"] = new_id
                if not new_id:
                    logger.critical(
                        f"💀 SL 재등록도 실패 — {pos.symbol} 포지션 무방비, "
                        f"수동 개입 필요!"
                    )
            except Exception as e:
                logger.critical(f"💀 SL 재등록 예외: {e}")

            # 10회 연속 실패 시 강제 포지션 정리 (04-09 무한루프 fix)
            # 옛 동작: autotrading OFF + return (메모리 유지) → 다음 폴링 또 _full_close → 무한
            # 새 동작: 10회 이상이면 포지션 메모리 강제 삭제 + finalize (0 가격)
            if pos.close_attempts >= 10:
                logger.critical(
                    f"💀💀 청산 {pos.close_attempts}회 연속 실패 ({pos.symbol}) → "
                    f"포지션 메모리 강제 정리 + 자동매매 OFF"
                )
                try:
                    await self.redis.set("sys:autotrading", "off")
                except Exception:
                    pass
                if self.telegram:
                    try:
                        await self.telegram.notify_emergency(
                            f"💀 {pos.symbol} 청산 {pos.close_attempts}회 실패 → "
                            f"포지션 메모리 강제 정리. OKX 수동 확인 필요"
                        )
                    except Exception:
                        pass
                # 포지션 메모리 강제 정리 — 무한루프 탈출
                # 04-15: exit_price=0 대신 현재가로 대체 (PnL 데이터 보존)
                try:
                    price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
                    fallback_price = float(price_str) if price_str else pos.entry_price
                except Exception:
                    fallback_price = pos.entry_price
                await self._finalize_position(pos, f"{reason}_force_cleanup", exit_price=fallback_price)
                return
            return  # 메모리 유지 → 다음 폴링에서 _full_close 재호출 (10회 미만)

        # 청산 성공 — fill 정보 추출
        exit_price = order.get("average") or order.get("price") or 0
        fee = (order.get("fee") or {}).get("cost", 0) or 0
        pos.total_fee += float(fee)

        # finalize (DB / Redis / 메모리 / 알림 / ML)
        await self._finalize_position(pos, reason, exit_price)

    async def _finalize_position(self, pos: Position, reason: str, exit_price: float):
        """
        청산된 포지션의 모든 정리 작업 — DB / Redis / 메모리 / 텔레그램 / ML 콜백.
        _full_close 와 외부 청산 감지 양쪽에서 호출.
        """
        # ── 잔존 알고 주문 정리 (04-10 fix + 04-16 강화: 재시도 + 검증) ──
        try:
            await self._cancel_all_algos(pos)
        except Exception as e:
            logger.error(f"finalize 알고 정리 예외: {e}")

        # 04-16: 정리 검증 — 잔존 알고 여전히 있으면 2회 재시도
        for verify_attempt in range(2):
            try:
                pending = await self.executor.exchange.private_get_trade_orders_algo_pending(
                    {"instType": "SWAP",
                     "instId": self.executor.exchange.market(pos.symbol)["id"],
                     "ordType": "trigger"}
                )
                items = pending.get("data", []) if isinstance(pending, dict) else []
                if not items:
                    break
                logger.warning(
                    f"⚠️  finalize 후 알고 {len(items)}개 잔존 → 재정리 ({verify_attempt+1}/2)"
                )
                await self.executor.cancel_all_algos()
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.debug(f"잔존 알고 검증 실패 ({verify_attempt+1}): {e}")
                break

        # ── exit_price 정확화 (BUG #1~3 fix) ──
        # 외부 청산 (서버 SL/TP 자동 발동) 또는 close 응답에 가격 없는 경우
        # OKX fetch_my_trades 로 실제 청산 가격 받아옴 → PnL 정확
        needs_fetch = exit_price <= 0 or reason in (
            "sl_or_forced", "tp1_full_server",
            "runner_trail_hit", "runner_sl_hit",
            "breakeven_hit",
        )
        if needs_fetch:
            try:
                trades = await self.executor.exchange.fetch_my_trades(
                    pos.symbol, limit=10
                )
                # 진입 시각 이후 + 포지션 방향과 반대 (청산) 거래 가격 평균
                entry_ms = pos.entry_time * 1000
                opp_side = "sell" if pos.direction == "long" else "buy"
                close_trades = [
                    t for t in trades
                    if (t.get("timestamp", 0) >= entry_ms
                        and t.get("side") == opp_side)
                ]
                if close_trades:
                    # 사이즈 가중 평균 가격
                    total_amt = sum(float(t.get("amount", 0) or 0) for t in close_trades)
                    if total_amt > 0:
                        weighted = sum(
                            float(t.get("price", 0) or 0) * float(t.get("amount", 0) or 0)
                            for t in close_trades
                        )
                        fetched_price = weighted / total_amt
                        if fetched_price > 0:
                            old_price = exit_price
                            exit_price = fetched_price
                            # 수수료도 합산
                            fetched_fee = sum(
                                float((t.get("fee") or {}).get("cost", 0) or 0)
                                for t in close_trades
                            )
                            pos.total_fee += abs(fetched_fee)
                            logger.info(
                                f"📊 OKX 실제 청산가 fetch: ${old_price:.1f} → ${exit_price:.1f} "
                                f"(reason={reason}, {len(close_trades)}건 평균)"
                            )
            except Exception as e:
                logger.debug(f"실제 청산가 fetch 실패: {e}")

        # 펀딩비 조회 (포지션 진입 이후 발생한 펀딩비 합산)
        try:
            funding = await self.executor.fetch_funding_bill(pos.entry_time * 1000)
            pos.funding_cost = funding
        except Exception as e:
            logger.debug(f"펀딩비 조회 실패: {e}")

        # P&L 계산 (04-13: remaining_size 기준 — TP1 50% 익절 후 남은 사이즈만)
        pnl_pct = pos.pnl_pct(exit_price) if exit_price > 0 else 0
        pnl_usdt = 0.0
        if pos.entry_price > 0 and pos.leverage > 0:
            margin = pos.remaining_size * pos.entry_price / pos.leverage
            pnl_usdt = margin * pnl_pct / 100

        # DB 업데이트 (음수 trade_id = 임시 ID, DB 에 없으므로 스킵)
        if pos.trade_id and pos.trade_id > 0:
            try:
                await self.db.update_trade_exit(pos.trade_id, {
                    "exit_price": exit_price,
                    "exit_time": int(time.time() * 1000),
                    "exit_reason": reason,
                    "pnl_usdt": round(pnl_usdt, 2),
                    "pnl_pct": round(pnl_pct, 4),
                    "fee_total": round(pos.total_fee, 4),
                    "funding_cost": round(pos.funding_cost, 4),
                })
            except Exception as e:
                logger.error(f"DB 청산 기록 실패: {e}")

        # Redis 정리
        try:
            await self.redis.delete(f"pos:active:{pos.symbol}")
        except Exception:
            pass

        # 메모리 정리
        if pos.symbol in self.positions:
            del self.positions[pos.symbol]

        logger.info(
            f"포지션 종료 ({reason}): {pos.direction.upper()} {pos.symbol} | "
            f"P&L: {pnl_pct:+.2f}% (${pnl_usdt:+.2f}) | "
            f"보유: {pos.hold_minutes}분 | 수수료: ${pos.total_fee:.2f}"
        )

        # 텔레그램 청산 알림
        if self.telegram:
            try:
                await self.telegram.notify_exit(
                    pos.direction, reason, pos.entry_price, exit_price,
                    pnl_pct, pnl_usdt, pos.hold_minutes,
                    fee=pos.total_fee, funding=pos.funding_cost
                )
            except Exception as e:
                logger.error(f"텔레그램 청산 알림 실패: {e}")

        # 거래 로그
        if self.trade_logger:
            try:
                self.trade_logger.log_exit(
                    pos.direction, reason, pos.entry_price, exit_price,
                    pnl_pct, pnl_usdt, pos.hold_minutes, pos.total_fee
                )
            except Exception as e:
                logger.error(f"trade_logger.log_exit 실패: {e}")

        # 04-13: 리스크 매니저에 매매 결과 기록 (일일/주간 P&L, 연패 추적)
        if self.risk_manager:
            try:
                await self.risk_manager.record_trade_result(pnl_pct, pnl_usdt)
            except Exception as e:
                logger.error(f"리스크 매니저 기록 실패: {e}")

        # ML 학습 콜백 (실거래 시그널 데이터 포함 + 수수료율 + 방향/사유)
        if self.on_trade_closed:
            mode = "unified"  # TradeEngine 통합 모델
            margin = pos.remaining_size * pos.entry_price / pos.leverage if pos.leverage > 0 else 0
            fee_pct = (pos.total_fee + pos.funding_cost) / margin * 100 if margin > 0 else 0
            hold_min = (time.time() * 1000 - pos.entry_time) / 60000 if pos.entry_time > 0 else 0
            try:
                await self.on_trade_closed(mode, pos.signals_snapshot, pnl_pct,
                                           fee_pct=fee_pct, direction=pos.direction,
                                           exit_reason=reason, pnl_usdt=pnl_usdt,
                                           hold_min=hold_min)
            except Exception as e:
                logger.error(f"ML 콜백 에러: {e}")

        return {"pnl_pct": pnl_pct, "pnl_usdt": pnl_usdt}

    async def close_all(self, reason: str = "kill_switch"):
        """전 포지션 청산 (킬 스위치)"""
        for symbol in list(self.positions.keys()):
            async with self._get_lock(symbol):
                if symbol not in self.positions:
                    continue
                pos = self.positions[symbol]
                await self._cancel_all_algos(pos)
                await self._full_close(pos, reason)
        await self.executor.cancel_all_orders()
        logger.warning(f"전 포지션 청산 완료: {reason}")

    async def sync_positions(self):
        """
        거래소 포지션과 동기화 (재시작 시).
        - 거래소에 포지션 있는데 봇 메모리에 없으면 → Redis 에서 옛 상태 복원 시도
        - Redis 에도 없으면 → 보호 알고만 재등록 (긴급 보호)
        - fetch 실패 시 3회 재시도 (BUG #H3 — 봇 시작 시 일시 장애 대비)
        """
        exchange_positions = None
        for attempt in range(3):
            try:
                exchange_positions = await self.executor.get_positions()
                break
            except Exception as e:
                logger.error(
                    f"sync_positions 거래소 조회 실패 ({attempt+1}/3): {e}"
                )
                if attempt < 2:
                    await asyncio.sleep(2.0 * (attempt + 1))

        if exchange_positions is None:
            logger.critical(
                "💀 sync_positions 3회 연속 실패 — 거래소 상태 모름. 수동 확인 필요!"
            )
            if self.telegram:
                try:
                    await self.telegram.notify_emergency(
                        "💀 봇 재시작 후 거래소 포지션 조회 3회 실패 — 수동 확인 필요"
                    )
                except Exception:
                    pass
            return

        # 04-15 개선: 포지션 매칭되는 알고는 유지, 고아만 정리
        # 포지션 없으면 모든 알고 = 고아 → 전부 정리
        active_symbols = set()
        for ep in exchange_positions:
            s = ep.get("symbol", "")
            sz = abs(float(ep.get("size") or 0))
            if sz > 0:
                active_symbols.add(s)

        try:
            if not active_symbols:
                # 포지션 0 = 모든 알고 고아 → 전부 정리
                cleaned = await self.executor.cancel_all_algos()
                if cleaned:
                    logger.info(f"🧹 고아 알고 {len(cleaned)}개 정리 (포지션 없음)")
            # 포지션 있으면 알고 유지 (self_heal이 None인 것만 재등록)
        except Exception as e:
            logger.error(f"sync 시 알고 정리 실패: {e}")

        for ep in exchange_positions:
            symbol = ep["symbol"]
            if symbol in self.positions:
                continue

            ex_size = abs(float(ep.get("size") or 0))
            ex_entry = float(ep.get("entry_price") or 0)
            ex_dir = ep.get("direction", "long")
            ex_lev = int(ep.get("leverage") or 10)

            if ex_size <= 0 or ex_entry <= 0:
                continue

            logger.warning(
                f"거래소 포지션 발견 (봇 미추적): {symbol} "
                f"{ex_dir} {ex_size:.6f} @ ${ex_entry:.1f}"
            )

            # Redis 에서 옛 상태 복원 시도
            redis_data = {}
            try:
                redis_data = await self.redis.hgetall(f"pos:active:{symbol}") or {}
            except Exception:
                pass

            if redis_data:
                logger.info(f"Redis 옛 상태 발견 → Position 복원 시도")
                try:
                    # direction 분기 — long: SL=entry-1%, TP=entry+1~3% / short: 반대 (BUG #C1)
                    is_long = (ex_dir == "long")
                    sl_default = ex_entry * (0.99 if is_long else 1.01)
                    tp1_default = ex_entry * (1.01 if is_long else 0.99)
                    tp2_default = ex_entry * (1.02 if is_long else 0.98)
                    tp3_default = ex_entry * (1.03 if is_long else 0.97)
                    pos = Position(
                        trade_id=int(redis_data.get("trade_id", -int(time.time()))),
                        symbol=symbol,
                        direction=ex_dir,
                        entry_price=ex_entry,
                        size=ex_size,
                        leverage=ex_lev,
                        sl_price=float(redis_data.get("sl_price", sl_default)),
                        tp1_price=float(redis_data.get("tp1_price", tp1_default)),
                        tp2_price=float(redis_data.get("tp2_price", tp2_default)),
                        tp3_price=float(redis_data.get("tp3_price", tp3_default)),
                        grade=redis_data.get("grade", "RESTORED"),
                        score=float(redis_data.get("score", 0) or 0),
                        signals_snapshot={},
                    )
                    pos.remaining_size = float(redis_data.get("remaining_size", ex_size))
                    pos.tp1_filled = bool(int(redis_data.get("tp1_filled", 0)))
                    pos.runner_mode = bool(int(redis_data.get("runner_mode", 0)))
                    pos.best_price = float(redis_data.get("best_price", ex_entry))
                    pos.trail_distance = float(redis_data.get("trail_distance", 0))
                    # 기존 OKX 알고 조회 → algo_ids 매핑 (중복 등록 방지)
                    pos.algo_ids = {"sl": None, "tp1": None, "tp2": None, "tp3": None}
                    try:
                        existing = await self.executor.cancel_all_algos()
                        # 정리 후 새로 등록 (깨끗한 상태에서 1벌만)
                        logger.info(f"🧹 기존 알고 {len(existing)}개 정리 → 새로 1벌 등록")
                    except Exception as e:
                        logger.debug(f"기존 알고 정리 실패: {e}")

                    self.positions[symbol] = pos

                    # SL + TP1 즉시 등록 (1벌만)
                    try:
                        new_sl_id = await self.executor.update_stop_loss(
                            pos.direction, pos.remaining_size, pos.current_sl, None
                        )
                        if new_sl_id:
                            pos.algo_ids["sl"] = new_sl_id
                            logger.info(f"🔧 SL 알고 등록: ${pos.current_sl:.0f} id={new_sl_id}")
                    except Exception as e:
                        logger.error(f"SL 등록 실패: {e}")

                    if not pos.tp1_filled and not pos.runner_mode:
                        try:
                            import math
                            tp1_size = pos.remaining_size * 0.5
                            tp1_size = math.floor(tp1_size / 0.01) * 0.01
                            if tp1_size >= 0.01:
                                new_tp_id = await self.executor.set_take_profit(
                                    pos.direction, tp1_size, pos.tp1_price, level=1
                                )
                                if new_tp_id:
                                    pos.algo_ids["tp1"] = new_tp_id
                                    logger.info(f"🔧 TP1 알고 등록: ${pos.tp1_price:.0f} id={new_tp_id}")
                        except Exception as e:
                            logger.error(f"TP1 등록 실패: {e}")

                    logger.info(
                        f"✅ 포지션 복원 완료: {symbol} "
                        f"(tp1_filled={pos.tp1_filled}, runner={pos.runner_mode})"
                    )
                except Exception as e:
                    logger.error(f"Position 복원 실패: {e}")
            else:
                # Redis 도 없음 → 최소한의 보호 알고만 즉시 등록 (긴급)
                logger.warning(f"Redis 상태 없음 → 긴급 보호 알고 등록 (현재가 ±2% SL)")
                emergency_sl = ex_entry * (0.98 if ex_dir == "long" else 1.02)
                try:
                    sl_id = await self.executor.set_stop_loss(
                        ex_dir, ex_size, round(emergency_sl, 1)
                    )
                    if sl_id:
                        logger.info(f"긴급 SL 등록: ${emergency_sl:.0f} id={sl_id}")
                        # 최소 Position 객체 생성 (메모리에 추적)
                        pos = Position(
                            trade_id=-int(time.time()),
                            symbol=symbol,
                            direction=ex_dir,
                            entry_price=ex_entry,
                            size=ex_size,
                            leverage=ex_lev,
                            sl_price=emergency_sl,
                            tp1_price=ex_entry,  # placeholder
                            tp2_price=ex_entry,
                            tp3_price=ex_entry,
                            grade="EMERGENCY_RESTORE",
                            score=0,
                            signals_snapshot={},
                        )
                        pos.algo_ids = {"sl": sl_id, "tp1": None, "tp2": None, "tp3": None}
                        self.positions[symbol] = pos
                        if self.telegram:
                            try:
                                await self.telegram.notify_warning(
                                    f"🔧 미추적 포지션 복원: {symbol} {ex_dir} "
                                    f"{ex_size} @ ${ex_entry:.0f} → 긴급 SL ${emergency_sl:.0f}"
                                )
                            except Exception:
                                pass
                except Exception as e:
                    logger.error(f"긴급 SL 등록 실패: {e}")

        # 거래소에 없는 옛 Redis pos:active:* 키 정리 (stale 데이터 → 대시보드 환상 포지션 방지)
        try:
            exchange_symbols = {ep["symbol"] for ep in exchange_positions}
            stale_keys = await self.redis.keys("pos:active:*")
            for key in stale_keys:
                # bytes 또는 str
                key_str = key.decode() if isinstance(key, bytes) else key
                sym = key_str.replace("pos:active:", "")
                if sym not in exchange_symbols and sym not in self.positions:
                    await self.redis.delete(key_str)
                    logger.warning(
                        f"옛 Redis 포지션 키 정리: {key_str} (거래소+메모리 모두 없음)"
                    )
        except Exception as e:
            logger.debug(f"sync 시 Redis stale 정리 실패: {e}")

    def _is_better_sl(self, pos: Position, new_sl: float) -> bool:
        """새 SL이 기존보다 유리한지 체크 (롱: 더 높으면 유리, 숏: 더 낮으면 유리)"""
        if pos.direction == "long":
            return new_sl > pos.current_sl
        else:
            return new_sl < pos.current_sl

    async def restore_position(self, pos: Position):
        """재시작 시 외부에서 Position 복원 (sync_positions 등에서 호출)"""
        self.positions[pos.symbol] = pos
