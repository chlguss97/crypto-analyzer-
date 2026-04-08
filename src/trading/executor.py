import ccxt.async_support as ccxt
import asyncio
import logging
import random
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
        """거래소 연결 (OKX_DEMO=1 환경변수 시 데모 트레이딩 모드)"""
        api_key = get_env("OKX_API_KEY", "")
        secret = get_env("OKX_SECRET_KEY", "")
        passphrase = get_env("OKX_PASSPHRASE", "")
        demo_mode = get_env("OKX_DEMO", "0") in ("1", "true", "True", "yes")

        if not api_key:
            logger.warning("OKX API 키 미설정 → 퍼블릭 모드 (실거래 불가)")

        self.exchange = ccxt.okx(
            {
                "apiKey": api_key,
                "secret": secret,
                "password": passphrase,
                "enableRateLimit": True,
                "aiohttp_trust_env": True,
                "options": {"defaultType": "swap"},
            }
        )
        self.exchange.aiohttp_resolver = "default"

        if demo_mode:
            # OKX 데모 트레이딩: x-simulated-trading: 1 헤더
            self.exchange.set_sandbox_mode(True)
            logger.warning("🧪 OKX 데모 트레이딩 모드 활성화 (x-simulated-trading: 1)")

        try:
            await self.exchange.load_markets()
        except Exception as e:
            # 예외 메시지에 키가 포함될 수 있으므로 마스킹
            err_str = str(e).replace(api_key, "***").replace(secret, "***").replace(passphrase, "***")
            logger.error(f"거래소 연결 실패: {err_str}")
            raise

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

    def _btc_to_contracts(self, size_btc: float) -> float:
        """
        BTC 단위 size → OKX contracts 수 변환.
        ccxt OKX 의 create_order amount 는 contracts 단위로 그대로 전달됨.
        BTC-USDT-SWAP contractSize = 0.01 BTC → amount=0.01 (BTC) 보내면
        OKX 는 0.01 contracts = 0.0001 BTC 진입 (100배 작음).
        → 명시적으로 contracts 수로 변환해서 전달해야 함.
        """
        if not hasattr(self, "_contract_size_cached") or self._contract_size_cached is None:
            try:
                self._contract_size_cached = float(
                    self.exchange.market(self.symbol).get("contractSize", 0.01)
                )
            except Exception:
                self._contract_size_cached = 0.01  # BTC-USDT-SWAP default
        cs = self._contract_size_cached
        if cs <= 0:
            return size_btc
        return size_btc / cs

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

        # SL/TP 설정은 PositionManager.set_protection 에서 일괄 처리
        # (실패 시 진입 즉시 되돌리기 위해 호출자가 결과를 받아야 함)
        return order

    async def _market_order(self, side: str, size: float, pos_side: str) -> dict | None:
        """시장가 주문 (재시도 포함). size 는 BTC 단위, ccxt 에는 contracts 로 변환 전달."""
        contracts = self._btc_to_contracts(size)
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                order = await self.exchange.create_order(
                    symbol=self.symbol,
                    type="market",
                    side=side,
                    amount=contracts,
                    params={"tdMode": "isolated", "posSide": pos_side},
                )
                fill_price = order.get("average", order.get("price", 0))
                logger.info(
                    f"시장가 체결: {side.upper()} {size} BTC ({contracts} ct) @ ${fill_price} "
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
        """지정가 주문 (타임아웃 + 부분체결 처리). size BTC → contracts."""
        contracts = self._btc_to_contracts(size)
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                order = await self.exchange.create_order(
                    symbol=self.symbol,
                    type="limit",
                    side=side,
                    amount=contracts,
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

    # ── 알고 주문 (SL/TP) ──

    @staticmethod
    def _gen_algo_id(prefix: str) -> str:
        """OKX algoClOrdId 생성: 영숫자만, 32자 이하 (언더스코어 불허)"""
        ts = int(time.time() * 1000)
        rnd = random.randint(0, 9999)
        return f"{prefix}{ts}{rnd:04d}"

    async def _create_algo_order(
        self, direction: str, size: float, trigger_price: float, prefix: str
    ) -> str | None:
        """SL/TP 공통 알고 주문 생성. size BTC → contracts."""
        if size <= 0 or trigger_price <= 0:
            logger.error(f"알고 주문 인자 무효 [{prefix}] size={size} trigger={trigger_price}")
            return None
        contracts = self._btc_to_contracts(size)
        side = "sell" if direction == "long" else "buy"
        pos_side = "long" if direction == "long" else "short"
        algo_id = self._gen_algo_id(prefix)
        try:
            # ordType="trigger" 명시: ccxt 가 알고 엔드포인트로 라우팅
            await self.exchange.create_order(
                symbol=self.symbol,
                type="trigger",
                side=side,
                amount=contracts,
                params={
                    "tdMode": "isolated",
                    "posSide": pos_side,
                    "ordType": "trigger",
                    "triggerPx": str(trigger_price),
                    "orderPx": "-1",  # 시장가 청산
                    "triggerPxType": "last",
                    "reduceOnly": True,
                    "algoClOrdId": algo_id,
                },
            )
            logger.info(
                f"알고 주문 등록 [{prefix}]: trigger=${trigger_price:.1f} "
                f"size={size:.6f} id={algo_id}"
            )
            return algo_id
        except Exception as e:
            logger.error(f"알고 주문 등록 실패 [{prefix} ${trigger_price:.1f}]: {e}")
            return None

    async def set_stop_loss(
        self, direction: str, size: float, sl_price: float
    ) -> str | None:
        """서버사이드 SL 등록 → algoClOrdId 반환"""
        return await self._create_algo_order(direction, size, sl_price, "sl")

    async def set_take_profit(
        self, direction: str, size: float, tp_price: float, level: int = 1
    ) -> str | None:
        """서버사이드 TP 등록 → algoClOrdId 반환"""
        return await self._create_algo_order(direction, size, tp_price, f"tp{level}")

    async def cancel_algo_order(self, algo_id: str | None) -> bool:
        """
        알고 주문 취소 (이미 체결된 경우는 무시)
        OKX 는 ccxt 버전에 따라 두 가지 경로 — 둘 다 시도.
        """
        if not algo_id:
            return False

        # 경로 1: OKX 전용 cancel-algos 엔드포인트 (가장 확실)
        try:
            inst_id = self.exchange.market(self.symbol)["id"]
            resp = await self.exchange.private_post_trade_cancel_algos([{
                "algoClOrdId": algo_id,
                "instId": inst_id,
            }])
            # 응답 코드 확인
            if isinstance(resp, dict) and resp.get("code") in ("0", 0):
                logger.debug(f"알고 주문 취소(direct): {algo_id}")
                return True
            # data 안에 sCode 0 인지 확인
            data = resp.get("data", []) if isinstance(resp, dict) else []
            if data and str(data[0].get("sCode", "")) == "0":
                logger.debug(f"알고 주문 취소(direct/data): {algo_id}")
                return True
        except Exception as e:
            logger.debug(f"cancel-algos direct 경로 실패 ({algo_id}): {e}")

        # 경로 2: ccxt 통합 cancel_order (trigger=True)
        try:
            await self.exchange.cancel_order(
                algo_id,
                self.symbol,
                params={"algoClOrdId": algo_id, "trigger": True, "stop": True},
            )
            logger.debug(f"알고 주문 취소(ccxt): {algo_id}")
            return True
        except Exception as e:
            # 이미 체결/취소된 경우는 정상 흐름
            logger.debug(f"알고 주문 취소 무시 ({algo_id}): {e}")
            return False

    async def set_protection(
        self,
        direction: str,
        total_size: float,
        sl_price: float,
        tp_levels: list[tuple[float, float]],
    ) -> dict:
        """
        진입 직후 보호 알고 주문 일괄 등록.

        Args:
            direction: 'long' | 'short'
            total_size: 전체 포지션 크기 (BTC)
            sl_price: 손절가 (진입 시 SL)
            tp_levels: [(tp_price, fraction), ...] 예:
                [(tp1, 0.5), (tp2, 0.3), (tp3, 0.2)]

        Returns:
            {"sl": id|None, "tp1": id|None, "tp2": id|None, "tp3": id|None}
        """
        ids = {"sl": None, "tp1": None, "tp2": None, "tp3": None}

        # SL 먼저 — 가장 중요. 실패 시 호출자가 진입 되돌림
        ids["sl"] = await self.set_stop_loss(direction, total_size, sl_price)

        # TPs (부분 사이즈)
        for i, (tp_price, fraction) in enumerate(tp_levels, start=1):
            tp_size = round(total_size * fraction, 6)
            if tp_size <= 0:
                continue
            ids[f"tp{i}"] = await self.set_take_profit(direction, tp_size, tp_price, level=i)

        return ids

    async def update_stop_loss(
        self,
        direction: str,
        size: float,
        new_sl: float,
        old_algo_id: str | None,
    ) -> str | None:
        """기존 SL 알고 취소 후 새 SL 등록 (트레일링/본절 이동)"""
        if old_algo_id:
            await self.cancel_algo_order(old_algo_id)
        return await self.set_stop_loss(direction, size, new_sl)

    async def get_position_size(self, symbol: str | None = None) -> float:
        """
        현재 포지션 사이즈 — base 통화 (BTC) 단위로 정규화.
        ccxt OKX 의 contracts 필드는 OKX raw "pos" (contracts 수) 라서
        contractSize 를 곱해서 base 단위로 변환해야 봇 추적 사이즈와 비교 가능.
        """
        sym = symbol or self.symbol
        try:
            positions = await self.exchange.fetch_positions([sym])
            for p in positions:
                contracts = abs(float(p.get("contracts", 0) or 0))
                if contracts <= 0:
                    continue
                # contractSize: BTC-USDT-SWAP = 0.01
                cs = p.get("contractSize")
                if not cs:
                    try:
                        cs = self.exchange.market(sym).get("contractSize", 1)
                    except Exception:
                        cs = 1
                return float(contracts) * float(cs)
            return 0.0
        except Exception as e:
            logger.error(f"포지션 사이즈 조회 실패: {e}")
            return -1.0  # 에러 → 호출자가 무시

    async def get_position_entry(self, symbol: str | None = None) -> tuple[float, float]:
        """
        현재 포지션의 (entry_price, size_base) 반환.
        시장가 진입 직후 정확한 fill price 확인용.
        """
        sym = symbol or self.symbol
        try:
            positions = await self.exchange.fetch_positions([sym])
            for p in positions:
                contracts = abs(float(p.get("contracts", 0) or 0))
                if contracts <= 0:
                    continue
                entry = float(p.get("entryPrice") or p.get("info", {}).get("avgPx") or 0)
                cs = p.get("contractSize")
                if not cs:
                    try:
                        cs = self.exchange.market(sym).get("contractSize", 1)
                    except Exception:
                        cs = 1
                size_base = float(contracts) * float(cs)
                return entry, size_base
            return 0.0, 0.0
        except Exception as e:
            logger.error(f"포지션 entry 조회 실패: {e}")
            return 0.0, 0.0

    async def close_position(self, direction: str, size: float,
                             reason: str = "manual") -> dict | None:
        """포지션 청산 (시장가). size BTC → contracts."""
        contracts = self._btc_to_contracts(size)
        side = "sell" if direction == "long" else "buy"
        pos_side = "long" if direction == "long" else "short"

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                order = await self.exchange.create_order(
                    symbol=self.symbol,
                    type="market",
                    side=side,
                    amount=contracts,
                    params={
                        "tdMode": "isolated",
                        "posSide": pos_side,
                        "reduceOnly": True,
                    },
                )
                fill_price = order.get("average", order.get("price", 0))
                logger.info(f"청산 완료 ({reason}): {side.upper()} {size} BTC ({contracts} ct) @ ${fill_price}")
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

    async def cancel_all_algos(self) -> int:
        """
        OKX 의 BTC-USDT-SWAP 활성 알고 주문 (SL/TP/trigger) 모두 cancel.
        봇 재시작 시 옛 알고가 살아있고 self_heal 이 새 알고를 추가 등록하면
        중복 SL/TP 가 됨 → sync_positions 에서 호출.
        """
        canceled = 0
        try:
            # OKX 알고 주문 조회 (private endpoint)
            try:
                resp = await self.exchange.private_get_trade_orders_algo_pending(
                    {"instType": "SWAP", "instId": self.exchange.market(self.symbol)["id"]}
                )
                items = resp.get("data", []) if isinstance(resp, dict) else []
            except Exception as e:
                logger.debug(f"알고 조회 실패 (스킵): {e}")
                return 0

            for item in items:
                algo_id = item.get("algoId") or item.get("algoClOrdId")
                if not algo_id:
                    continue
                try:
                    await self.cancel_algo_order(algo_id)
                    canceled += 1
                except Exception:
                    pass
            if canceled > 0:
                logger.info(f"거래소 활성 알고 {canceled}개 정리 완료 (sync 단계)")
        except Exception as e:
            logger.error(f"cancel_all_algos 에러: {e}")
        return canceled

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
