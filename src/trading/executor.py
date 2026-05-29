import ccxt.async_support as ccxt
import asyncio
import logging
from src.utils.helpers import load_config, get_env

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 2


class OrderExecutor:
    """OKX 주문 실행 — limit post-only + market 주문"""

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
            self.exchange.set_sandbox_mode(True)
            logger.warning("OKX 데모 트레이딩 모드 활성화")

        try:
            await self.exchange.load_markets()
        except Exception as e:
            err_str = str(e).replace(api_key, "***").replace(secret, "***").replace(passphrase, "***")
            logger.error(f"거래소 연결 실패: {err_str}")
            raise

        try:
            await self.exchange.set_margin_mode("isolated", self.symbol)
        except Exception:
            pass  # 이미 설정된 경우

        logger.info("OrderExecutor 초기화 완료")

    async def close(self):
        if self.exchange:
            await self.exchange.close()

    def _btc_to_contracts(self, size_btc: float) -> float:
        """BTC 단위 size → OKX contracts 수 변환."""
        if not hasattr(self, "_contract_size_cached") or self._contract_size_cached is None:
            try:
                self._contract_size_cached = float(
                    self.exchange.market(self.symbol).get("contractSize", 0.01)
                )
            except Exception:
                self._contract_size_cached = 0.01
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
                    logger.warning(f"레버리지 설정 충돌 → 알고 주문 정리 후 재시도 ({attempt+1}/3)")
                    await self.cancel_all_algos()
                    await asyncio.sleep(1)
                else:
                    logger.error(f"레버리지 설정 실패: {e}")
                    raise

    async def _market_order(self, side: str, size: float, pos_side: str,
                            reduce_only: bool = False) -> dict | None:
        """시장가 주문 (taker 0.05%). 복구/긴급 청산 시에만 사용."""
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
                fill_price = order.get("average") or order.get("price")
                if not fill_price:
                    try:
                        await asyncio.sleep(0.3)
                        fetched = await self.exchange.fetch_order(order["id"], self.symbol)
                        fill_price = fetched.get("average") or fetched.get("price")
                        if fill_price:
                            order["average"] = fill_price
                    except Exception:
                        pass
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

    # ── limit 주문 ──

    async def place_limit_order(
        self, side: str, size_btc: float, price: float,
        pos_side: str, reduce_only: bool = False,
    ) -> dict | None:
        """limit 주문: post-only, 즉시 반환 (체결 대기 없음)"""
        contracts = self._btc_to_contracts(size_btc)
        params = {"tdMode": "isolated", "posSide": pos_side, "postOnly": True}
        if reduce_only:
            params["reduceOnly"] = True
        try:
            order = await self.exchange.create_order(
                symbol=self.symbol, type="limit", side=side,
                amount=contracts, price=round(price, 1), params=params,
            )
            logger.debug(
                f"[EXEC] limit {side} {size_btc}BTC @ ${price:.1f} "
                f"id={order.get('id')} reduce={reduce_only}"
            )
            return order
        except Exception as e:
            err = str(e)
            if "51121" in err or "post only" in err.lower():
                logger.debug(f"[EXEC] post-only 거부 @ ${price:.1f}")
            else:
                logger.error(f"[EXEC] limit order 실패: {e}")
            return None

    async def cancel_order_by_id(self, order_id: str) -> bool:
        """단일 주문 취소"""
        try:
            await self.exchange.cancel_order(order_id, self.symbol)
            return True
        except Exception:
            return False

    async def cancel_all_orders(self):
        """전체 미체결 주문 취소"""
        try:
            orders = await self.exchange.fetch_open_orders(self.symbol)
            for order in orders:
                await self.exchange.cancel_order(order["id"], self.symbol)
            logger.info(f"미체결 주문 {len(orders)}개 취소")
        except Exception as e:
            logger.error(f"주문 전체 취소 실패: {e}")

    async def cancel_algo_order(self, algo_id: str | None) -> bool:
        """알고 주문 취소 (이미 체결된 경우는 무시)"""
        if not algo_id:
            return False

        # 경로 1: OKX 전용 cancel-algos 엔드포인트
        try:
            inst_id = self.exchange.market(self.symbol)["id"]
            resp = await self.exchange.private_post_trade_cancel_algos([{
                "algoClOrdId": algo_id,
                "instId": inst_id,
            }])
            if isinstance(resp, dict) and resp.get("code") in ("0", 0):
                logger.debug(f"알고 주문 취소(direct): {algo_id}")
                return True
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
            logger.debug(f"알고 주문 취소 무시 ({algo_id}): {e}")
            return False

    async def cancel_all_algos(self) -> list[dict]:
        """OKX BTC-USDT-SWAP 활성 알고 주문 (SL/TP/trigger) 모두 cancel."""
        canceled_info = []
        try:
            items = []
            for attempt in range(3):
                try:
                    resp = await self.exchange.private_get_trade_orders_algo_pending(
                        {"instType": "SWAP", "instId": self.exchange.market(self.symbol)["id"],
                         "ordType": "trigger"}
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
                            f"알고 정리: {info['ord_type']} {info['side']} "
                            f"sz={info['sz']} trigger=${info['trigger_px']} id={algo_id}"
                        )
                    else:
                        failed_ids.append(algo_id)
                except Exception as e:
                    logger.debug(f"알고 cancel 실패 ({algo_id}): {e}")
                    failed_ids.append(algo_id)

            if failed_ids:
                logger.warning(f"알고 {len(failed_ids)}개 취소 실패 (확인 필요): {failed_ids[:5]}")

            if canceled_info:
                logger.info(f"거래소 활성 알고 {len(canceled_info)}개 정리 완료")
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

    async def get_balance(self) -> float:
        """USDT 잔고 조회"""
        try:
            balance = await self.exchange.fetch_balance()
            return float(balance.get("USDT", {}).get("total", 0))
        except Exception as e:
            logger.error(f"잔고 조회 실패: {e}")
            return 0
