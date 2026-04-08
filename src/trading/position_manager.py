import asyncio
import logging
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

        # OKX 알고 주문 ID 추적 (cancel/replace 용)
        # 러너 모드에서는 tp2/tp3 사용 안 함 — 호환용으로만 유지
        self.algo_ids: dict[str, str | None] = {
            "sl": None, "tp1": None, "tp2": None, "tp3": None
        }

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
            "entry_time": self.entry_time,
            # bool → int (Redis hset 호환)
            "tp1_filled": 1 if self.tp1_filled else 0,
            "tp2_filled": 1 if self.tp2_filled else 0,
            "tp3_filled": 1 if self.tp3_filled else 0,
            "runner_mode": 1 if self.runner_mode else 0,
            "best_price": self.best_price,
            "trail_distance": self.trail_distance,
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
        self.on_trade_closed = None  # 콜백: async def(mode, signals, pnl_pct)

    # 러너 트레일링: TP1 50% 익절 + 잔여 50% 는 트레일링 SL 로 추세 끝까지
    TP1_CLOSE_PCT = 0.5
    # 러너 트레일 거리 = TP1 거리 × 이 비율 (가격이 새 고/저 갱신 시 SL을 이만큼 뒤에 둠)
    RUNNER_TRAIL_RATIO = 0.5

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
        # → fetch_positions 로 정확한 entry 와 size 확보
        if fill_price <= 0 or filled_size <= 0:
            await asyncio.sleep(0.5)  # 거래소 반영 대기
            ex_entry, ex_size = await self.executor.get_position_entry(symbol)
            if ex_entry > 0 and ex_size > 0:
                fill_price = ex_entry
                filled_size = ex_size
                logger.info(f"진입 정보 fetch 보정: entry=${fill_price:.1f} size={filled_size:.6f}")
            else:
                # 그래도 못 찾으면 진입 취소 (보호 없는 포지션 금지)
                logger.error(
                    f"🚨 진입 가격/사이즈 확인 실패 → 즉시 청산 시도 "
                    f"(ccxt order={order})"
                )
                try:
                    await self.executor.close_position(
                        direction, float(trade_request["size"]), "fill_price_unknown"
                    )
                except Exception as e:
                    logger.error(f"실패한 진입 청산 시도 에러: {e}")
                return None

        sl_price = float(trade_request["sl_price"])
        tp1_price = float(trade_request["tp1_price"])
        tp2_price = float(trade_request["tp2_price"])
        tp3_price = float(trade_request.get("tp3_price", tp2_price))

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

        # 🚨 SL 등록 실패 시 진입을 즉시 되돌림 (보호장치 없는 포지션 금지)
        if not algo_ids.get("sl"):
            logger.error(
                f"🚨 SL 알고 등록 실패 → 포지션 즉시 청산 "
                f"({direction.upper()} {filled_size} @ ${fill_price})"
            )
            await self.executor.close_position(direction, float(filled_size), "sl_protect_failed")
            # 등록된 TP1 도 정리
            if algo_ids.get("tp1"):
                await self.executor.cancel_algo_order(algo_ids["tp1"])
            return None

        # DB 기록
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

        await self.redis.hset(f"pos:active:{symbol}", pos.to_dict())

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
                continue

            # 2. 거래소 사이즈 동기화 — 서버사이드 TP/SL 체결 감지
            try:
                ex_size = await self.executor.get_position_size(symbol)
                # epsilon 비교 (float 정확비교 위험)
                if 0 <= ex_size < 1e-8:
                    # 외부에서 전량 청산됨 (서버 SL/TP, 강제청산, 수동 등)
                    logger.warning(
                        f"포지션 외부 종료 감지 (사이즈≈0) → 정리: {symbol}"
                    )
                    await self._cancel_all_algos(pos)
                    await self._reconcile_external_close(pos, current_price)
                    continue
                elif ex_size > 0 and ex_size < pos.remaining_size * 0.95:
                    # 부분 체결 감지 (서버사이드 TP1 이 발동한 경우)
                    closed_amount = pos.remaining_size - ex_size
                    pct_closed = closed_amount / pos.remaining_size
                    logger.info(
                        f"부분 체결 감지: 봇={pos.remaining_size:.6f} → "
                        f"거래소={ex_size:.6f} ({pct_closed*100:.0f}%)"
                    )
                    pos.remaining_size = ex_size

                    # 🔒 서버 TP1 이 발동한 것으로 간주 → 봇이 또 처리하지 않도록 마킹
                    # 그리고 SL 본절 이동 + 러너 모드 활성화 (가격 기반 _handle_tp_progression 스킵)
                    if not pos.tp1_filled:
                        pos.tp1_filled = True
                        # TP1 알고 ID 정리 (이미 체결됨)
                        pos.algo_ids["tp1"] = None

                        # SL 본절 이동
                        fee_offset = pos.entry_price * 0.001
                        new_sl = (pos.entry_price + fee_offset) if pos.direction == "long" \
                            else (pos.entry_price - fee_offset)
                        await self._move_sl(pos, new_sl, label="본절(서버TP)")

                        # 러너 모드 활성화
                        pos.runner_mode = True
                        pos.best_price = current_price
                        tp1_dist = abs(pos.tp1_price - pos.entry_price)
                        pos.trail_distance = max(
                            tp1_dist * self.RUNNER_TRAIL_RATIO,
                            pos.entry_price * 0.003,  # 최소 0.3% 가격
                        )
                        logger.info(
                            f"✅ 서버 TP1 자동 체결 감지 → SL 본전 ${new_sl:.0f} | "
                            f"🏃 러너 모드 ON (트레일 ${pos.trail_distance:.1f})"
                        )
            except Exception as e:
                logger.error(f"포지션 사이즈 동기화 실패: {e}")

            # 3. 가격 기반 TP 도달 처리 + 반익본절 SL 끌어올리기 (이중 처리 방지: tp1_filled 체크)
            await self._handle_tp_progression(pos, current_price)

            # 종료된 포지션이면 다음 루프
            if symbol not in self.positions:
                continue

            # 4. 시간 청산
            await self._check_time_exit(pos, current_price)

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
            # 사이즈가 최소단위 × 2 미만이면 100% 청산 (분할 불가)
            small_position = pos.size < self.MIN_ORDER_SIZE_BTC * 2
            close_pct = 1.0 if small_position else self.TP1_CLOSE_PCT

            await self._on_tp_hit(pos, level=1, close_pct=close_pct)
            if pos.symbol not in self.positions:
                return
            if not pos.tp1_filled:
                return  # 부분청산 실패 → 다음 폴링 재시도

            # 100% 청산했으면 포지션 종료 처리
            if small_position or pos.remaining_size < 1e-8:
                await self._cancel_all_algos(pos)
                await self._full_close(pos, "tp1_full")
                logger.info(f"✅ TP1 100% 청산 @ ${pos.tp1_price:.0f} (소형 포지션 — 러너 비활성)")
                return

            # SL → 본전 + 수수료 보상
            fee_offset = pos.entry_price * 0.001
            new_sl = pos.entry_price + fee_offset if pos.direction == "long" \
                else pos.entry_price - fee_offset
            await self._move_sl(pos, new_sl, label="본절")

            # 러너 모드 활성화
            pos.runner_mode = True
            pos.best_price = current_price
            tp1_distance = abs(pos.tp1_price - pos.entry_price)
            pos.trail_distance = max(
                tp1_distance * self.RUNNER_TRAIL_RATIO,
                pos.entry_price * 0.003,  # 최소 0.3% 가격 — 노이즈 방어
            )

            logger.info(
                f"✅ TP1 익절 50% @ ${pos.tp1_price:.0f} → SL 본전 ${new_sl:.0f} | "
                f"🏃 러너 모드 ON (트레일 ${pos.trail_distance:.1f})"
            )
            return

        # 러너 모드 트레일링 — 가격이 새 고/저 갱신 시 SL 추격
        if pos.runner_mode:
            await self._update_runner_trail(pos, current_price)

    async def _update_runner_trail(self, pos: Position, current_price: float):
        """러너 모드: 가격이 새 고/저 갱신 시 트레일링 SL 끌어올림"""
        moved = False
        if pos.direction == "long":
            if current_price > pos.best_price:
                pos.best_price = current_price
                new_sl = current_price - pos.trail_distance
                if new_sl > pos.current_sl:
                    await self._move_sl(pos, new_sl, label="러너트레일")
                    moved = True
        else:
            if current_price < pos.best_price:
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
        """포지션의 모든 SL/TP 알고 주문 취소"""
        for key in ("sl", "tp1", "tp2", "tp3"):
            algo_id = pos.algo_ids.get(key)
            if algo_id:
                await self.executor.cancel_algo_order(algo_id)
                pos.algo_ids[key] = None

    async def _reconcile_external_close(self, pos: Position, last_price: float):
        """외부에서 포지션이 전량 청산된 경우 DB/콜백 정리"""
        # 어떤 사유로 청산됐는지 추론
        pnl_now = pos.pnl_pct(last_price) if last_price > 0 else 0
        small_position = pos.size < self.MIN_ORDER_SIZE_BTC * 2

        if pos.runner_mode:
            reason = "runner_trail_hit" if pnl_now >= 0 else "runner_sl_hit"
        elif pos.tp1_filled:
            reason = "breakeven_hit"
        elif small_position and pnl_now > 0:
            # 소형 포지션 — TP1 100% 청산이 서버에서 발동
            reason = "tp1_full_server"
        else:
            # TP1 도 안 갔는데 청산: 원본 SL 또는 강제청산
            reason = "sl_or_forced"
        # 가짜 close 처리 (이미 체결됐으므로 close_position 호출 안 함)
        pnl_pct = pos.pnl_pct(last_price) if last_price > 0 else 0
        pnl_usdt = (pos.size * pos.entry_price * pnl_pct / 100 / max(pos.leverage, 1)) \
            if pos.entry_price > 0 else 0
        try:
            await self.db.update_trade_exit(pos.trade_id, {
                "exit_price": last_price,
                "exit_time": int(time.time() * 1000),
                "exit_reason": reason,
                "pnl_usdt": round(pnl_usdt, 2),
                "pnl_pct": round(pnl_pct, 4),
                "fee_total": round(pos.total_fee, 4),
                "funding_cost": round(pos.funding_cost, 4),
            })
        except Exception as e:
            logger.error(f"외부 청산 DB 기록 실패: {e}")

        await self.redis.delete(f"pos:active:{pos.symbol}")
        if pos.symbol in self.positions:
            del self.positions[pos.symbol]

        if self.on_trade_closed:
            mode = "scalp" if pos.grade == "SCALP" else "swing"
            try:
                await self.on_trade_closed(mode, pos.signals_snapshot, pnl_pct)
            except Exception as e:
                logger.error(f"ML 콜백 에러: {e}")

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

        # 2시간 → TP1 미체결 시 75% 청산
        if hours >= 2 and not pos.tp1_filled:
            await self._partial_close(pos, 0.75, "time_2h")
            # 잔여 사이즈에 맞춰 OKX SL 알고도 갱신 (size mismatch 방지)
            if pos.remaining_size > 0:
                await self._move_sl(pos, pos.current_sl, label="time_2h_resize")
            return False

        # 1시간 → 수익 < 3% 시 50% 청산
        if hours >= 1 and not pos.tp1_filled and pnl < 3.0:
            await self._partial_close(pos, 0.5, "time_1h")
            if pos.remaining_size > 0:
                await self._move_sl(pos, pos.current_sl, label="time_1h_resize")
            return False

        return False

    async def signal_exit(self, symbol: str, reason: str):
        """시그널 기반 청산 (1H CHoCH, 반대 Grade A 등)"""
        if symbol in self.positions:
            pos = self.positions[symbol]
            await self._cancel_all_algos(pos)
            await self._full_close(pos, reason)

    async def _partial_close(self, pos: Position, close_pct: float, reason: str):
        """부분 청산"""
        close_size = pos.remaining_size * close_pct
        if close_size <= 0:
            return

        order = await self.executor.close_partial(
            pos.direction, close_size, 1.0, reason
        )
        if order:
            pos.remaining_size -= close_size
            fee = (order.get("fee") or {}).get("cost", 0) or 0
            pos.total_fee += float(fee)

            logger.info(
                f"부분 청산 ({reason}): {close_pct*100:.0f}% | "
                f"잔여: {pos.remaining_size:.4f}"
            )

    async def _full_close(self, pos: Position, reason: str):
        """전량 청산 + DB 기록"""
        if pos.remaining_size <= 0:
            return

        # entry_price 무결성 체크
        if not pos.entry_price or pos.entry_price <= 0:
            logger.error(f"포지션 청산 실패: entry_price 무효 ({pos.entry_price})")
            if pos.symbol in self.positions:
                del self.positions[pos.symbol]
            return

        order = await self.executor.close_position(
            pos.direction, pos.remaining_size, reason
        )

        exit_price = 0
        if order:
            exit_price = order.get("average") or order.get("price") or 0
            fee = (order.get("fee") or {}).get("cost", 0) or 0
            pos.total_fee += float(fee)

        # P&L 계산 (계좌 기준 PnL %, 마진 기준 USDT)
        pnl_pct = pos.pnl_pct(exit_price) if exit_price > 0 else 0  # 레버리지 적용
        # 마진 = (size × entry_price) / leverage,  pnl_usdt = 마진 × pnl_pct / 100
        pnl_usdt = 0
        if pos.entry_price > 0 and pos.leverage > 0:
            margin = pos.size * pos.entry_price / pos.leverage
            pnl_usdt = margin * pnl_pct / 100

        # DB 업데이트
        await self.db.update_trade_exit(pos.trade_id, {
            "exit_price": exit_price,
            "exit_time": int(time.time() * 1000),
            "exit_reason": reason,
            "pnl_usdt": round(pnl_usdt, 2),
            "pnl_pct": round(pnl_pct, 4),
            "fee_total": round(pos.total_fee, 4),
            "funding_cost": round(pos.funding_cost, 4),
        })

        # Redis 정리
        await self.redis.delete(f"pos:active:{pos.symbol}")
        if pos.symbol in self.positions:
            del self.positions[pos.symbol]

        logger.info(
            f"포지션 종료 ({reason}): {pos.direction.upper()} {pos.symbol} | "
            f"P&L: {pnl_pct:+.2f}% (${pnl_usdt:+.2f}) | "
            f"보유: {pos.hold_minutes}분 | 수수료: ${pos.total_fee:.2f}"
        )

        # ML 학습 콜백 (실거래 시그널 데이터 포함)
        if self.on_trade_closed:
            mode = "scalp" if pos.grade == "SCALP" else "swing"
            try:
                await self.on_trade_closed(mode, pos.signals_snapshot, pnl_pct)
            except Exception as e:
                logger.error(f"ML 콜백 에러: {e}")

        return {"pnl_pct": pnl_pct, "pnl_usdt": pnl_usdt}

    async def close_all(self, reason: str = "kill_switch"):
        """전 포지션 청산 (킬 스위치)"""
        for symbol in list(self.positions.keys()):
            pos = self.positions[symbol]
            await self._cancel_all_algos(pos)
            await self._full_close(pos, reason)
        await self.executor.cancel_all_orders()
        logger.warning(f"전 포지션 청산 완료: {reason}")

    async def sync_positions(self):
        """거래소 포지션과 동기화 (재시작 시)"""
        exchange_positions = await self.executor.get_positions()
        for ep in exchange_positions:
            symbol = ep["symbol"]
            if symbol not in self.positions:
                logger.warning(
                    f"거래소에 포지션 발견 (봇 미추적): {symbol} "
                    f"{ep['direction']} {ep['size']} @ ${ep['entry_price']}"
                )
                # TODO: 복원 로직 (Redis에서 상태 조회 후 Position 객체 재생성)

    def _is_better_sl(self, pos: Position, new_sl: float) -> bool:
        """새 SL이 기존보다 유리한지 체크 (롱: 더 높으면 유리, 숏: 더 낮으면 유리)"""
        if pos.direction == "long":
            return new_sl > pos.current_sl
        else:
            return new_sl < pos.current_sl

    async def restore_position(self, pos: Position):
        """재시작 시 외부에서 Position 복원 (sync_positions 등에서 호출)"""
        self.positions[pos.symbol] = pos
