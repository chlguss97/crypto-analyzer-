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
                 grade: str, score: float, signals_snapshot: dict = None):
        self.trade_id = trade_id
        self.symbol = symbol
        self.direction = direction
        self.entry_price = entry_price
        self.size = size                  # 원래 사이즈
        self.remaining_size = size        # 잔여 사이즈
        self.leverage = leverage
        self.sl_price = sl_price
        self.current_sl = sl_price        # 트레일링으로 갱신
        self.tp1_price = tp1_price
        self.tp2_price = tp2_price
        self.grade = grade
        self.score = score
        self.entry_time = int(time.time())
        self.tier = 0                     # 트레일링 단계
        self.total_fee = 0.0
        self.funding_cost = 0.0
        self.signals_snapshot = signals_snapshot or {}

    @property
    def hold_minutes(self) -> int:
        return (int(time.time()) - self.entry_time) // 60

    @property
    def hold_hours(self) -> float:
        return (int(time.time()) - self.entry_time) / 3600

    def pnl_pct(self, current_price: float) -> float:
        if self.direction == "long":
            return (current_price - self.entry_price) / self.entry_price * 100
        else:
            return (self.entry_price - current_price) / self.entry_price * 100

    def to_dict(self) -> dict:
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
            "grade": self.grade,
            "entry_time": self.entry_time,
            "tier": self.tier,
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

    async def open_position(self, trade_request: dict) -> Position | None:
        """새 포지션 진입"""
        symbol = trade_request["symbol"]

        if symbol in self.positions:
            logger.warning(f"이미 {symbol} 포지션 존재 → 진입 거부")
            return None

        # 주문 실행
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

        fill_price = order.get("average") or order.get("price") or trade_request.get("entry_price", 0)
        filled_size = order.get("filled", trade_request["size"])

        # 수수료 기록
        fee = order.get("fee", {}).get("cost", 0) or 0

        # DB 기록
        trade_id = await self.db.insert_trade({
            "symbol": symbol,
            "direction": trade_request["direction"],
            "grade": trade_request["grade"],
            "score": trade_request["score"],
            "entry_price": fill_price,
            "entry_time": int(time.time() * 1000),
            "leverage": trade_request["leverage"],
            "position_size": filled_size * fill_price,
            "signals_snapshot": json.dumps(trade_request.get("signals_snapshot", {})),
        })

        # Position 객체 생성
        pos = Position(
            trade_id=trade_id,
            symbol=symbol,
            direction=trade_request["direction"],
            entry_price=float(fill_price),
            size=float(filled_size),
            leverage=trade_request["leverage"],
            sl_price=trade_request["sl_price"],
            tp1_price=trade_request["tp1_price"],
            tp2_price=trade_request["tp2_price"],
            grade=trade_request["grade"],
            score=trade_request["score"],
            signals_snapshot=trade_request.get("signals_snapshot", {}),
        )
        pos.total_fee = float(fee)
        self.positions[symbol] = pos

        # Redis에 포지션 저장
        await self.redis.hset(f"pos:active:{symbol}", pos.to_dict())

        logger.info(
            f"포지션 오픈: {pos.direction.upper()} {symbol} | "
            f"진입 ${fill_price} | SL ${pos.sl_price:.0f} | "
            f"TP1 ${pos.tp1_price:.0f} TP2 ${pos.tp2_price:.0f} | "
            f"{pos.leverage}x | 등급 {pos.grade}"
        )

        return pos

    async def check_positions(self, current_price: float):
        """활성 포지션 체크 (15초마다 호출)"""
        if not current_price or current_price <= 0:
            return
        for symbol, pos in list(self.positions.items()):
            if not pos.entry_price or pos.entry_price <= 0:
                continue
            pnl = pos.pnl_pct(current_price)

            # 1. 시간 청산 체크
            time_close = await self._check_time_exit(pos, pnl)
            if time_close:
                continue

            # 2. 트레일링 업데이트
            await self._update_trailing(pos, current_price, pnl)

            # 3. TP 체크
            await self._check_tp(pos, current_price, pnl)

    async def _update_trailing(self, pos: Position, current_price: float, pnl: float):
        """트레일링 스톱 4단계"""
        cfg = self.trailing_cfg

        # Tier 4: +3.5% 이상 → ATR 트레일링
        if pnl >= 3.5 and pos.tier < 4:
            pos.tier = 4
            # ATR 기반 동적 트레일링 (여기서는 현재가 - 진입가 × 비율로 근사)
            trail_distance = abs(current_price - pos.entry_price) * 0.2
            if pos.direction == "long":
                new_sl = current_price - trail_distance
            else:
                new_sl = current_price + trail_distance
            if self._is_better_sl(pos, new_sl):
                pos.current_sl = new_sl
                logger.info(f"트레일링 Tier 4: SL → ${new_sl:.0f} (ATR 동적)")

        # Tier 3: TP2 도달 (+2.5%) → 30% 청산, SL +1.5%
        elif pnl >= cfg["tp2_trigger"] * 100 and pos.tier < 3:
            pos.tier = 3
            await self._partial_close(pos, cfg["tp2_close_pct"], "tp2")
            if pos.direction == "long":
                new_sl = pos.entry_price * 1.015
            else:
                new_sl = pos.entry_price * 0.985
            pos.current_sl = new_sl
            logger.info(f"트레일링 Tier 3: TP2 청산 30% | SL → ${new_sl:.0f}")

        # Tier 2: TP1 도달 (+1.5%) → 50% 청산, SL +0.5%
        elif pnl >= cfg["tp1_trigger"] * 100 and pos.tier < 2:
            pos.tier = 2
            await self._partial_close(pos, cfg["tp1_close_pct"], "tp1")
            if pos.direction == "long":
                new_sl = pos.entry_price * 1.005
            else:
                new_sl = pos.entry_price * 0.995
            pos.current_sl = new_sl
            logger.info(f"트레일링 Tier 2: TP1 청산 50% | SL → ${new_sl:.0f}")

        # Tier 1: +0.8% → SL을 본전으로
        elif pnl >= cfg["breakeven_trigger"] * 100 and pos.tier < 1:
            pos.tier = 1
            fee_offset = pos.entry_price * 0.001  # 수수료 보상
            if pos.direction == "long":
                new_sl = pos.entry_price + fee_offset
            else:
                new_sl = pos.entry_price - fee_offset
            pos.current_sl = new_sl
            logger.info(f"트레일링 Tier 1: 본전 확보 | SL → ${new_sl:.0f}")

        # Tier 4에서는 지속적으로 SL 따라올리기
        if pos.tier == 4:
            trail_distance = abs(current_price - pos.entry_price) * 0.2
            if pos.direction == "long":
                new_sl = current_price - trail_distance
            else:
                new_sl = current_price + trail_distance
            if self._is_better_sl(pos, new_sl):
                pos.current_sl = new_sl

    async def _check_tp(self, pos: Position, current_price: float, pnl: float):
        """TP 도달 체크 (Tier로 이미 처리, 여기서는 보조)"""
        pass  # 트레일링에서 처리

    async def _check_time_exit(self, pos: Position, pnl: float) -> bool:
        """시간 기반 청산"""
        hours = pos.hold_hours

        # 6시간 → 무조건 전량 청산
        if hours >= 6:
            await self._full_close(pos, "time_6h")
            return True

        # 4시간 → 미청산 전량 청산
        if hours >= 4 and pos.tier < 3:
            await self._full_close(pos, "time_4h")
            return True

        # 2시간 → TP1 미달 시 75% 청산
        if hours >= 2 and pos.tier < 2:
            await self._partial_close(pos, 0.75, "time_2h")
            return False

        # 1시간 → 수익 < 0.3% 시 50% 청산
        if hours >= 1 and pos.tier < 1 and pnl < 0.3:
            await self._partial_close(pos, 0.5, "time_1h")
            return False

        return False

    async def signal_exit(self, symbol: str, reason: str):
        """시그널 기반 청산 (1H CHoCH, 반대 Grade A 등)"""
        if symbol in self.positions:
            await self._full_close(self.positions[symbol], reason)

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
            fee = order.get("fee", {}).get("cost", 0) or 0
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
            fee = order.get("fee", {}).get("cost", 0) or 0
            pos.total_fee += float(fee)

        # P&L 계산 (entry_price 0 방어)
        pnl_pct = pos.pnl_pct(exit_price) if exit_price > 0 else 0
        pnl_usdt = (pos.size * pos.entry_price * pnl_pct / 100) if pos.entry_price > 0 else 0

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
            await self._full_close(self.positions[symbol], reason)
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
