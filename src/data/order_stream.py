"""
OKX Private WebSocket — 주문 상태 실시간 수신

orders 채널 구독 → 체결/취소 즉시 콜백
REST 폴링(1초)보다 20배 빠름 (10~50ms)

사용: grid_engine이 on_order_update 콜백 등록
"""

import asyncio
import hmac
import hashlib
import base64
import json
import logging
import time
import websockets

from src.utils.helpers import get_env

logger = logging.getLogger(__name__)

OKX_WS_PRIVATE = "wss://ws.okx.com:8443/ws/v5/private"


class OrderStream:
    """OKX Private WebSocket — 주문 체결 실시간 감지"""

    def __init__(self):
        self._running = False
        self._ws = None
        self._reconnect_count = 0

        # 콜백: grid_engine이 등록
        self.on_order_update = None  # async callback(order_data)

    async def start(self):
        """Private WS 연결 (무한 재시도)"""
        self._running = True

        while self._running:
            try:
                await self._connect()
                self._reconnect_count = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._reconnect_count += 1
                wait = min(5 * min(self._reconnect_count, 12), 60)
                logger.warning(f"OKX Private WS 끊김: {e} → {wait}초 후 재연결")
                await asyncio.sleep(wait)

    async def _connect(self):
        """Private WS 연결 + 인증 + orders 구독"""
        api_key = get_env("OKX_API_KEY", "")
        secret = get_env("OKX_SECRET_KEY", "")
        passphrase = get_env("OKX_PASSPHRASE", "")

        if not api_key or not secret:
            logger.warning("[ORDER_WS] API 키 미설정 → 비활성")
            await asyncio.sleep(60)
            return

        ws = await websockets.connect(OKX_WS_PRIVATE, ping_interval=20, open_timeout=10)
        self._ws = ws

        # 인증
        timestamp = str(int(time.time()))
        sign_str = timestamp + "GET" + "/users/self/verify"
        mac = hmac.new(secret.encode(), sign_str.encode(), hashlib.sha256)
        signature = base64.b64encode(mac.digest()).decode()

        await ws.send(json.dumps({
            "op": "login",
            "args": [{
                "apiKey": api_key,
                "passphrase": passphrase,
                "timestamp": timestamp,
                "sign": signature,
            }],
        }))

        # 로그인 응답 대기
        resp = await asyncio.wait_for(ws.recv(), timeout=10)
        data = json.loads(resp)
        if data.get("event") != "login" or data.get("code") != "0":
            raise ConnectionError(f"OKX Private WS 로그인 실패: {data}")

        logger.info("[ORDER_WS] OKX Private WS 인증 성공")

        # orders 채널 구독
        await ws.send(json.dumps({
            "op": "subscribe",
            "args": [{"channel": "orders", "instType": "SWAP"}],
        }))

        # 구독 확인
        resp = await asyncio.wait_for(ws.recv(), timeout=10)
        data = json.loads(resp)
        if data.get("event") == "subscribe":
            logger.info("[ORDER_WS] orders 채널 구독 완료")

        # 수신 루프
        while self._running:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=30)
            except asyncio.TimeoutError:
                # keepalive ping
                try:
                    await ws.send("ping")
                    continue
                except Exception:
                    break
            except Exception as e:
                logger.warning(f"[ORDER_WS] 수신 에러: {e}")
                break

            try:
                data = json.loads(message)
            except (json.JSONDecodeError, ValueError):
                if message == "pong":
                    continue
                continue

            await self._handle_message(data)

        await ws.close()

    async def _handle_message(self, data: dict):
        """주문 상태 변경 처리"""
        if "data" not in data:
            return

        arg = data.get("arg", {})
        if arg.get("channel") != "orders":
            return

        for order in data["data"]:
            state = order.get("state", "")
            # filled(완전체결) 또는 canceled만 관심
            if state in ("filled", "canceled", "partially_filled"):
                order_info = {
                    "id": order.get("ordId", ""),
                    "status": "closed" if state == "filled" else state,
                    "side": order.get("side", ""),
                    "price": order.get("avgPx") or order.get("px", "0"),
                    "size": order.get("sz", "0"),
                    "instId": order.get("instId", ""),
                    "posSide": order.get("posSide", ""),
                    "ts": order.get("uTime", ""),
                }

                if self.on_order_update:
                    try:
                        await self.on_order_update(order_info)
                    except Exception as e:
                        logger.error(f"[ORDER_WS] 콜백 에러: {e}")

    def stop(self):
        self._running = False
