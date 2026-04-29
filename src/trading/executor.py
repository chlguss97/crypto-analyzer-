import ccxt.async_support as ccxt
import asyncio
import logging
import random
import time
from src.utils.helpers import load_config, get_env

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 2
LIMIT_TIMEOUT_SEC = 30  # 120→30초: 미체결 시 빠르게 시장가 전환 (04-15)
MAX_SLIPPAGE_PCT = 0.1

# 04-16: 수수료 절감 — maker 0.02% vs taker 0.05% → 전 주문 limit post-only 우선
POST_ONLY_TIMEOUT_SEC = 2        # post-only 체결 대기 (2초 — BTC 스프레드 내 체결 충분)
POST_ONLY_MAX_RETRIES = 3        # 가격 추격 3회 (최대 6초 블록)


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
        """레버리지 설정 — 알고 주문 충돌 시 자동 정리 후 재시도"""
        side = "long" if direction == "long" else "short"
        for attempt in range(3):
            try:
                await self.exchange.set_leverage(
                    leverage, self.symbol, params={"mgnMode": "isolated", "posSide": side}
                )
                logger.info(f"레버리지 설정: {leverage}x ({side})")
                return
            except Exception as e:
                err_str = str(e)
                if "59668" in err_str and attempt < 2:
                    # OKX: 기존 알고 주문 때문에 레버리지 변경 불가 → 알고 정리 후 재시도
                    logger.warning(f"레버리지 설정 충돌 → 알고 주문 정리 후 재시도 ({attempt+1}/3)")
                    await self.cancel_all_algos()
                    await asyncio.sleep(1)
                else:
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

        # 04-16: 모든 진입을 post-only limit 우선 시도 → maker 수수료(0.02%) 적용
        # 실패/타임아웃 시에만 시장가 폴백 (taker 0.05%)
        if entry_price:
            # 호출자가 명시 가격 → 해당 가격으로 limit
            order = await self._limit_order(side, size, entry_price, pos_side)
        else:
            # 가격 미지정 → post-only 로 best bid/ask 공략 + 실패 시 market 폴백
            order = await self._post_only_entry(side, size, pos_side)

        if not order:
            return None

        # SL/TP 설정은 PositionManager.set_protection 에서 일괄 처리
        # (실패 시 진입 즉시 되돌리기 위해 호출자가 결과를 받아야 함)
        return order

    async def _market_order(self, side: str, size: float, pos_side: str,
                            reduce_only: bool = False) -> dict | None:
        """시장가 주문 폴백 (taker 0.05%). 리밋이 실패하거나 긴급 청산 시에만 사용."""
        contracts = self._btc_to_contracts(size)
        params = {"tdMode": "isolated", "posSide": pos_side}
        if reduce_only:
            params["reduceOnly"] = True
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                order = await self.exchange.create_order(
                    symbol=self.symbol,
                    type="market",
                    side=side,
                    amount=contracts,
                    params=params,
                )
                fill_price = order.get("average", order.get("price", 0))
                logger.info(
                    f"시장가 체결: {side.upper()} {size} BTC ({contracts} ct) @ ${fill_price} "
                    f"(주문ID: {order.get('id')})"
                )
                return order

            except Exception as e:
                err_str = str(e)
                logger.error(f"시장가 주문 실패 ({attempt}/{MAX_RETRIES}): {e}")
                if reduce_only and ("51169" in err_str or "no position" in err_str.lower()):
                    return {"already_closed": True, "average": 0, "price": 0}
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY)

        logger.error("시장가 주문 최종 실패")
        return None

    async def _post_only_entry(self, side: str, size: float, pos_side: str) -> dict | None:
        """
        Post-only limit 진입 (maker 수수료 강제).
        호가 추격 5회 → 실패 시 진입 포기 (market 폴백 없음).
        수수료 > 수익 구조 근절: maker 0.02% 아니면 안 치는 게 이득.
        """
        contracts = self._btc_to_contracts(size)
        for attempt in range(1, POST_ONLY_MAX_RETRIES + 1):
            # best bid/ask 조회
            try:
                ticker = await self.exchange.fetch_ticker(self.symbol)
                best_bid = float(ticker.get("bid") or 0)
                best_ask = float(ticker.get("ask") or 0)
            except Exception as e:
                logger.warning(f"ticker 조회 실패 → 진입 포기 (maker 강제): {e}")
                return None

            if best_bid <= 0 or best_ask <= 0:
                logger.warning("best bid/ask 0 → 진입 포기")
                return None

            # maker: 매 시도마다 호가 새로 조회 + 점진적 양보
            # BTC $78k 기준 1$ = 0.0013% — 스프레드 수준
            offset = 1.0 * (attempt - 1)  # 시도1: 0$, 시도2: +1$, ... 시도5: +4$
            if side == "buy":
                price = round(best_bid - offset, 1)
            else:
                price = round(best_ask + offset, 1)

            try:
                order = await self.exchange.create_order(
                    symbol=self.symbol,
                    type="limit",
                    side=side,
                    amount=contracts,
                    price=price,
                    params={
                        "tdMode": "isolated",
                        "posSide": pos_side,
                        "postOnly": True,  # OKX: ordType=post_only → maker 만 허용
                    },
                )
                order_id = order.get("id")
                logger.info(
                    f"post-only ��입 시도 {attempt}/{POST_ONLY_MAX_RETRIES}: "
                    f"{side.upper()} {size} @ ${price} id={order_id}"
                )

                # 체결 대기
                filled = await self._wait_for_fill_fast(order_id, POST_ONLY_TIMEOUT_SEC)
                if filled:
                    logger.info(
                        f"post-only 진입 체결: {side.upper()} @ "
                        f"${filled.get('average') or filled.get('price')} (maker 0.02%)"
                    )
                    return filled

                # 미체결 → 취��� 후 가격 추���
                try:
                    await self.exchange.cancel_order(order_id, self.symbol)
                except Exception:
                    pass
                logger.debug(f"post-only 미체결 ({attempt}) — 가격 추격 재시도")

            except Exception as e:
                err_str = str(e)
                # OKX 51121: post-only reject (크로싱) — 가격 다시 조정해서 재��도
                if "51121" in err_str or "post only" in err_str.lower():
                    logger.debug(f"post-only 거부 (크로싱) → 재시도")
                    continue
                logger.error(f"post-only 주문 에러 ({attempt}): {e}")
                await asyncio.sleep(0.5)

        # 모든 추격 실패 → 진입 포기 (taker 수수료 > 기대수익이면 안 치는 게 이득)
        logger.warning("post-only 3회 추격 실패 → 진입 포기 (maker 강제 정책)")
        return None

    async def _wait_for_fill_fast(self, order_id: str, timeout: int) -> dict | None:
        """체결 대기 (500ms 폴링 — post-only 용)"""
        start = time.time()
        while time.time() - start < timeout:
            try:
                order = await self.exchange.fetch_order(order_id, self.symbol)
                status = order.get("status", "")
                if status == "closed":
                    return order
                if status in ("canceled", "rejected", "expired"):
                    return None
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return None

    async def _limit_order(self, side: str, size: float, price: float,
                           pos_side: str) -> dict | None:
        """지정가 주문 (post-only 강제 + 타임아웃 + 부분체결). market 폴백 없음."""
        contracts = self._btc_to_contracts(size)
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                order = await self.exchange.create_order(
                    symbol=self.symbol,
                    type="limit",
                    side=side,
                    amount=contracts,
                    price=price,
                    params={
                        "tdMode": "isolated",
                        "posSide": pos_side,
                        "postOnly": True,  # maker 수수료 강제
                    },
                )
                order_id = order.get("id")
                logger.info(f"지정가(post-only) 제출: {side.upper()} {size} @ ${price} (ID: {order_id})")

                # 타임아웃 대기
                filled_order = await self._wait_for_fill(order_id, LIMIT_TIMEOUT_SEC)
                if filled_order:
                    return filled_order

                # 미체결 → 부분체결 체크
                return await self._handle_unfilled(order_id, size)

            except Exception as e:
                err_str = str(e)
                if "51121" in err_str or "post only" in err_str.lower():
                    # post-only 거부 → 가격 조정 후 재시도
                    if side == "buy":
                        price *= 0.9995  # 더 낮게
                    else:
                        price *= 1.0005  # 더 높게
                    logger.debug(f"post-only 거부 → 가격 조정 재시도 ({attempt})")
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                logger.error(f"지정가 주문 실패 ({attempt}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY)

        # market 폴백 없음 — maker 강제 정책
        logger.warning("지정가(post-only) 미체결 → 진입 포기 (maker 강제 정책)")
        return None

    # ── 이전 _market_order → 04-16: reduce_only 인자 추가됨 (위 정의 참조) ──

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
        """
        SL/TP 알고 주문 생성.
        - SL: 항상 market-on-trigger (orderPx=-1) — 반드시 체결돼야 함 (손실 확대 방지)
        - TP: limit-on-trigger (orderPx=triggerPx) — 04-16: maker 수수료 목표
             체결 실패 시 러너 트레일링 SL 이 백업
        """
        if size <= 0 or trigger_price <= 0:
            logger.error(f"알고 주문 인자 무효 [{prefix}] size={size} trigger={trigger_price}")
            return None
        contracts = self._btc_to_contracts(size)
        side = "sell" if direction == "long" else "buy"
        pos_side = "long" if direction == "long" else "short"
        algo_id = self._gen_algo_id(prefix)

        # prefix 로 TP/SL/러너SL 구분 → orderPx 결정
        # 04-28: 전 주문 maker 강제 — SL도 limit-on-trigger
        # SL 미체결 위험은 sl_failsafe(폴링)가 백업
        is_limit_trigger = True  # 모든 알고를 limit-on-trigger (maker)
        order_px = str(round(trigger_price, 1))

        try:
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
                    "orderPx": order_px,
                    "triggerPxType": "last",
                    "reduceOnly": True,
                    "algoClOrdId": algo_id,
                },
            )
            fill_type = "limit" if is_limit_trigger else "market"
            logger.info(
                f"알고 주문 등록 [{prefix}/{fill_type}]: trigger=${trigger_price:.1f} "
                f"size={size:.6f} id={algo_id}"
            )
            return algo_id
        except Exception as e:
            logger.error(f"알고 주문 등록 실패 [{prefix} ${trigger_price:.1f}]: {e}")
            return None

    async def set_stop_loss(
        self, direction: str, size: float, sl_price: float,
        use_limit: bool = False,
    ) -> str | None:
        """서버사이드 SL 등록 → algoClOrdId 반환.
        use_limit=True: 러너 SL용 limit-on-trigger (maker 수수료)
        """
        if use_limit:
            return await self._create_algo_order(direction, size, sl_price, "rsl")
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
        use_limit: bool = False,
    ) -> str | None:
        """SL 갱신: 새 SL 먼저 등록 → 성공 시에만 old 취소 (나체 포지션 방지)"""
        new_id = await self.set_stop_loss(direction, size, new_sl, use_limit=use_limit)
        if new_id and old_algo_id:
            # 새 SL 성공 → 이제 old 취소 (실패해도 두 개 공존은 안전)
            await self.cancel_algo_order(old_algo_id)
        elif not new_id and old_algo_id:
            # 새 SL 실패 → old 유지 (나체 방지)
            logger.warning(f"새 SL 등록 실패 → 기존 SL {old_algo_id} 유지")
            return old_algo_id
        return new_id

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
        """
        포지션 청산 — maker 강제 정책.
        SL/긴급만 market 허용 (손실 확대 방지), 나머지는 post-only only.
        """
        side = "sell" if direction == "long" else "buy"
        pos_side = "long" if direction == "long" else "short"

        # 긴급 사유 → market (SL은 체결이 최우선, 수수료 < 미체결 손실)
        URGENT_REASONS = (
            "sl_failsafe", "sl_hit", "kill_switch", "kill_switch_dashboard",
            "manual_sl_failed", "emergency",
        )
        is_urgent = any(u in reason for u in URGENT_REASONS)

        if is_urgent:
            return await self._market_order(side, size, pos_side, reduce_only=True)

        # 일반 청산 (TP, time_exit, trail 등): post-only 강제
        order = await self._post_only_close(side, size, pos_side, reason)
        if order:
            return order

        # post-only 실패해도 market 폴백 없음 — 다음 사이클에서 재시도
        logger.warning(f"post-only 청산 실패 → 다음 사이클 재시도 ({reason})")
        return None

    async def _post_only_close(self, side: str, size: float, pos_side: str,
                               reason: str) -> dict | None:
        """Post-only limit 청산 — maker 수수료 목표. 호가 추격 3회."""
        contracts = self._btc_to_contracts(size)
        for attempt in range(1, POST_ONLY_MAX_RETRIES + 1):
            try:
                ticker = await self.exchange.fetch_ticker(self.symbol)
                best_bid = float(ticker.get("bid") or 0)
                best_ask = float(ticker.get("ask") or 0)
            except Exception as e:
                logger.debug(f"ticker 조회 실패 ({reason}): {e}")
                return None

            if best_bid <= 0 or best_ask <= 0:
                return None

            # maker: 첫 시도 공격적, 이후 보수적
            offset = 1.0 * (attempt - 1)
            if side == "sell":
                price = round(best_ask + offset, 1)
            else:
                price = round(best_bid - offset, 1)

            try:
                order = await self.exchange.create_order(
                    symbol=self.symbol,
                    type="limit",
                    side=side,
                    amount=contracts,
                    price=price,
                    params={
                        "tdMode": "isolated",
                        "posSide": pos_side,
                        "reduceOnly": True,
                        "postOnly": True,
                    },
                )
                order_id = order.get("id")
                logger.info(
                    f"post-only 청산 시도 {attempt}/{POST_ONLY_MAX_RETRIES} ({reason}): "
                    f"{side.upper()} {size} @ ${price}"
                )

                filled = await self._wait_for_fill_fast(order_id, POST_ONLY_TIMEOUT_SEC)
                if filled:
                    fill_price = filled.get("average") or filled.get("price")
                    logger.info(
                        f"✅ post-only 청산 체결 ({reason}): @ ${fill_price} (maker 0.02%)"
                    )
                    return filled

                # 미체결 → 취소 후 재시도
                try:
                    await self.exchange.cancel_order(order_id, self.symbol)
                except Exception:
                    pass

            except Exception as e:
                err_str = str(e)
                if "51169" in err_str or "no position" in err_str.lower():
                    logger.info(f"📌 포지션 이미 청산됨 ({reason})")
                    return {"already_closed": True, "average": 0, "price": 0}
                if "51121" in err_str or "post only" in err_str.lower():
                    logger.debug(f"post-only 거부 → 재시도")
                    continue
                logger.debug(f"post-only 청산 에러 ({attempt}): {e}")
                await asyncio.sleep(0.3)

        return None  # 모든 시도 실패

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

    async def cancel_all_algos(self) -> list[dict]:
        """
        OKX 의 BTC-USDT-SWAP 활성 알고 주문 (SL/TP/trigger) 모두 cancel.
        쿼리 실패 시 3회 재시도 (네트워크 일시 장애로 고아 알고 방치 방지).

        Returns: 정리한 알고 정보 list [{algo_id, ord_type, trigger_px, side, sz}, ...]
        """
        canceled_info = []
        try:
            # OKX 알고 주문 조회 — 실패 시 재시도 (silent skip 금지)
            items = []
            for attempt in range(3):
                try:
                    resp = await self.exchange.private_get_trade_orders_algo_pending(
                        {"instType": "SWAP", "instId": self.exchange.market(self.symbol)["id"],
                         "ordType": "trigger"}  # OKX 필수 파라미터
                    )
                    items = resp.get("data", []) if isinstance(resp, dict) else []
                    break
                except Exception as e:
                    if attempt < 2:
                        logger.warning(f"알고 조회 실패 ({attempt+1}/3) → 재시도: {e}")
                        await asyncio.sleep(0.5 * (attempt + 1))
                    else:
                        logger.error(f"알고 조회 3회 연속 실패 — 고아 알고 방치 위험: {e}")
                        return []

            if not items:
                return []

            failed_ids = []
            for item in items:
                algo_id = item.get("algoId") or item.get("algoClOrdId")
                if not algo_id:
                    continue
                info = {
                    "algo_id": algo_id,
                    "ord_type": item.get("ordType", "?"),
                    "trigger_px": item.get("triggerPx", "?"),
                    "side": item.get("side", "?"),
                    "sz": item.get("sz", "?"),
                    "state": item.get("state", "?"),
                }
                try:
                    ok = await self.cancel_algo_order(algo_id)
                    if ok:
                        canceled_info.append(info)
                        logger.info(
                            f"옛 알고 정리: {info['ord_type']} {info['side']} "
                            f"sz={info['sz']} trigger=${info['trigger_px']} id={algo_id}"
                        )
                    else:
                        failed_ids.append(algo_id)
                except Exception as e:
                    logger.debug(f"알고 cancel 실패 ({algo_id}): {e}")
                    failed_ids.append(algo_id)

            if failed_ids:
                logger.warning(f"⚠️  알고 {len(failed_ids)}개 취소 실패 (확인 필요): {failed_ids[:5]}")

            if canceled_info:
                logger.info(f"🧹 거래소 활성 알고 {len(canceled_info)}개 정리 완료")
        except Exception as e:
            logger.error(f"cancel_all_algos 에러: {e}")
        return canceled_info

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

    async def fetch_funding_bill(self, since_ms: int) -> float:
        """포지션 보유 중 발생한 펀딩비 비용 합계 (양수=지출, 음수=수입 → 비용만 합산)"""
        total = 0.0
        try:
            # 04-13: instId 포맷 수정 (M6) + abs() 제거 (H3)
            market = self.exchange.market(self.symbol)
            inst_id = market["id"]  # "BTC-USDT-SWAP"
            response = await self.exchange.private_get_account_bills(
                {"instId": inst_id,
                 "type": "8", "begin": str(since_ms), "limit": "100"}
            )
            for bill in response.get("data", []):
                # balChg: 음수=지출(비용), 양수=수입(보너스) → 부호 그대로 합산
                total += float(bill.get("balChg", 0) or 0)
        except Exception as e:
            logger.debug(f"펀딩비 조회 실패: {e}")
        # 반환: 음수=순비용, 양수=순수입 → 호출자가 비용으로 사용 시 abs() 적용
        return abs(total)  # 호환: 기존 코드가 funding_cost를 양수로 사용

    async def get_balance(self) -> float:
        """USDT 잔고 조회"""
        try:
            balance = await self.exchange.fetch_balance()
            return float(balance.get("USDT", {}).get("total", 0))
        except Exception as e:
            logger.error(f"잔고 조회 실패: {e}")
            return 0
