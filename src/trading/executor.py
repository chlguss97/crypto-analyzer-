import ccxt.async_support as ccxt
import asyncio
import logging
import time
from src.utils.helpers import load_config, get_env

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 2
LIMIT_TIMEOUT_SEC = 120
MAX_SLIPPAGE_PCT = 0.1


class OrderExecutor:
    """OKX 주문 실행 (시장가/지정가 + SL 서버사이드)"""

    def __init__(self):
        self.config = load_config()
        self.exchange: ccxt.okx | None = None
        self.symbol = self.config["exchange"]["symbol"]

    async def initialize(self):
        """거래소 연결"""
        self.exchange = ccxt.okx(
            {
                "apiKey": get_env("OKX_API_KEY", ""),
                "secret": get_env("OKX_SECRET_KEY", ""),
                "password": get_env("OKX_PASSPHRASE", ""),
                "enableRateLimit": True,
                "aiohttp_trust_env": True,
                "options": {"defaultType": "swap"},
            }
        )
        self.exchange.aiohttp_resolver = "default"
        await self.exchange.load_markets()

        # 마진 모드 설정 (isolated)
        try:
            await self.exchange.set_margin_mode(
                "isolated", self.symbol
            )
        except Exception:
            pass  # 이미 설정된 경우

        logger.info("OrderExecutor 초기화 완료")

    async def close(self):
        if self.exchange:
            await self.exchange.close()

    async def set_leverage(self, leverage: int, direction: str):
        """레버리지 설정"""
        try:
            side = "long" if direction == "long" else "short"
            await self.exchange.set_leverage(
                leverage, self.symbol, params={"mgnMode": "isolated", "posSide": side}
            )
            logger.info(f"레버리지 설정: {leverage}x ({side})")
        except Exception as e:
            logger.error(f"레버리지 설정 실패: {e}")
            raise

    async def open_position(self, direction: str, size: float, grade: str,
                            entry_price: float = None, sl_price: float = None,
                            leverage: int = 10) -> dict | None:
        """
        포지션 진입.

        Args:
            direction: 'long' | 'short'
            size: 계약 수량 (BTC 단위)
            grade: 등급 → 실행 방식 결정
            entry_price: 지정가일 때 가격
            sl_price: 손절가
            leverage: 레버리지

        Returns:
            주문 결과 dict or None
        """
        # 레버리지 설정
        await self.set_leverage(leverage, direction)

        side = "buy" if direction == "long" else "sell"
        pos_side = "long" if direction == "long" else "short"

        # 실행 방식 결정
        if grade in ("A+", "A"):
            order = await self._market_order(side, size, pos_side)
        else:
            if entry_price:
                order = await self._limit_order(side, size, entry_price, pos_side)
            else:
                order = await self._market_order(side, size, pos_side)

        if not order:
            return None

        # SL 설정 (서버사이드)
        if sl_price and order:
            await self._set_stop_loss(direction, size, sl_price)

        return order

    async def _market_order(self, side: str, size: float, pos_side: str) -> dict | None:
        """시장가 주문 (재시도 포함)"""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                order = await self.exchange.create_order(
                    symbol=self.symbol,
                    type="market",
                    side=side,
                    amount=size,
                    params={"tdMode": "isolated", "posSide": pos_side},
                )
                fill_price = order.get("average", order.get("price", 0))
                logger.info(
                    f"시장가 체결: {side.upper()} {size} @ ${fill_price} "
                    f"(주문ID: {order.get('id')})"
                )
                return order

            except Exception as e:
                logger.error(f"시장가 주문 실패 ({attempt}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY)

        logger.error("시장가 주문 최종 실패 → 시그널 포기")
        return None

    async def _limit_order(self, side: str, size: float, price: float,
                           pos_side: str) -> dict | None:
        """지정가 주문 (타임아웃 + 부분체결 처리)"""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                order = await self.exchange.create_order(
                    symbol=self.symbol,
                    type="limit",
                    side=side,
                    amount=size,
                    price=price,
                    params={"tdMode": "isolated", "posSide": pos_side},
                )
                order_id = order.get("id")
                logger.info(f"지정가 주문 제출: {side.upper()} {size} @ ${price} (ID: {order_id})")

                # 타임아웃 대기
                filled_order = await self._wait_for_fill(order_id, LIMIT_TIMEOUT_SEC)
                if filled_order:
                    return filled_order

                # 미체결 → 부분체결 체크
                return await self._handle_unfilled(order_id, size)

            except Exception as e:
                logger.error(f"지정가 주문 실패 ({attempt}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES:
                    # 가격 0.05% 조정 후 재시도
                    if side == "buy":
                        price *= 1.0005
                    else:
                        price *= 0.9995
                    await asyncio.sleep(RETRY_DELAY)

        logger.error("지정가 주문 최종 실패 → 시그널 포기")
        return None

    async def _wait_for_fill(self, order_id: str, timeout: int) -> dict | None:
        """주문 체결 대기"""
        start = time.time()
        while time.time() - start < timeout:
            try:
                order = await self.exchange.fetch_order(order_id, self.symbol)
                status = order.get("status", "")
                if status == "closed":
                    logger.info(f"지정가 체결 완료: {order.get('average')}")
                    return order
                elif status == "canceled":
                    return None
            except Exception:
                pass
            await asyncio.sleep(5)
        return None

    async def _handle_unfilled(self, order_id: str, original_size: float) -> dict | None:
        """미체결/부분체결 처리"""
        try:
            order = await self.exchange.fetch_order(order_id, self.symbol)
            filled = order.get("filled", 0)
            fill_ratio = filled / original_size if original_size > 0 else 0

            if fill_ratio >= 0.7:
                # 70% 이상 체결 → 나머지 취소, 체결분으로 진행
                await self.exchange.cancel_order(order_id, self.symbol)
                logger.info(f"부분 체결 {fill_ratio*100:.0f}% → 나머지 취소, 체결분 진행")
                return order
            else:
                # 70% 미만 → 전량 취소
                await self.exchange.cancel_order(order_id, self.symbol)
                logger.info(f"부분 체결 {fill_ratio*100:.0f}% → 전량 취소 (기회 포기)")
                return None

        except Exception as e:
            logger.error(f"미체결 처리 에러: {e}")
            try:
                await self.exchange.cancel_order(order_id, self.symbol)
            except Exception:
                pass
            return None

    async def _set_stop_loss(self, direction: str, size: float, sl_price: float):
        """서버사이드 SL 설정 (algo order)"""
        try:
            side = "sell" if direction == "long" else "buy"
            pos_side = "long" if direction == "long" else "short"

            await self.exchange.create_order(
                symbol=self.symbol,
                type="market",
                side=side,
                amount=size,
                params={
                    "tdMode": "isolated",
                    "posSide": pos_side,
                    "triggerPx": str(sl_price),
                    "orderPx": "-1",  # 시장가
                    "triggerPxType": "last",
                    "algoClOrdId": f"sl_{int(time.time())}",
                },
            )
            logger.info(f"SL 설정 (서버사이드): ${sl_price}")

        except Exception as e:
            logger.error(f"SL 설정 실패: {e}")
            raise

    async def close_position(self, direction: str, size: float,
                             reason: str = "manual") -> dict | None:
        """포지션 청산 (시장가)"""
        side = "sell" if direction == "long" else "buy"
        pos_side = "long" if direction == "long" else "short"

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                order = await self.exchange.create_order(
                    symbol=self.symbol,
                    type="market",
                    side=side,
                    amount=size,
                    params={
                        "tdMode": "isolated",
                        "posSide": pos_side,
                        "reduceOnly": True,
                    },
                )
                fill_price = order.get("average", order.get("price", 0))
                logger.info(f"청산 완료 ({reason}): {side.upper()} {size} @ ${fill_price}")
                return order

            except Exception as e:
                logger.error(f"청산 실패 ({attempt}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY)

        logger.error(f"청산 최종 실패 → SL에 위임")
        return None

    async def close_partial(self, direction: str, size: float,
                            close_pct: float, reason: str) -> dict | None:
        """부분 청산"""
        partial_size = size * close_pct
        return await self.close_position(direction, partial_size, reason)

    async def cancel_all_orders(self):
        """전체 미체결 주문 취소"""
        try:
            orders = await self.exchange.fetch_open_orders(self.symbol)
            for order in orders:
                await self.exchange.cancel_order(order["id"], self.symbol)
            logger.info(f"미체결 주문 {len(orders)}개 취소")
        except Exception as e:
            logger.error(f"주문 전체 취소 실패: {e}")

    async def get_positions(self) -> list[dict]:
        """현재 포지션 조회"""
        try:
            positions = await self.exchange.fetch_positions([self.symbol])
            active = [
                {
                    "symbol": p["symbol"],
                    "direction": "long" if p["side"] == "long" else "short",
                    "size": abs(p["contracts"]),
                    "entry_price": p["entryPrice"],
                    "unrealized_pnl": p["unrealizedPnl"],
                    "margin": p["initialMargin"],
                    "leverage": p["leverage"],
                    "liquidation_price": p["liquidationPrice"],
                    "margin_ratio": p.get("marginRatio", 0),
                }
                for p in positions
                if abs(p.get("contracts", 0)) > 0
            ]
            return active
        except Exception as e:
            logger.error(f"포지션 조회 실패: {e}")
            return []

    async def get_balance(self) -> float:
        """USDT 잔고 조회"""
        try:
            balance = await self.exchange.fetch_balance()
            return float(balance.get("USDT", {}).get("total", 0))
        except Exception as e:
            logger.error(f"잔고 조회 실패: {e}")
            return 0
