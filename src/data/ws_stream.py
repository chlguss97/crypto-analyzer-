"""
OKX WebSocket — Grid Trading 데이터 수집 (Minimal)

수집 항목:
  1. tickers → 가격/ticker
  2. candle 7종 → DB 직접 저장 + 이벤트 발행

Redis 키:
  rt:price:BTC-USDT-SWAP         — 가격
  rt:ticker:BTC-USDT-SWAP        — ticker
"""

import asyncio
import json
import logging
import websockets
from src.data.storage import RedisClient

logger = logging.getLogger(__name__)

OKX_WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
OKX_WS_BUSINESS = "wss://ws.okx.com:8443/ws/v5/business"  # 캔들 전용
SYMBOL = "BTC-USDT-SWAP"


class WebSocketStream:
    """OKX WebSocket — ticker + candle only"""

    def __init__(self, redis_client: RedisClient, db=None):
        self.redis = redis_client
        self.db = db
        self.ws = None
        self._running = False
        self._reconnect_count = 0

        # DB 저장 심볼
        from src.utils.helpers import load_config
        self._db_symbol = load_config().get("exchange", {}).get("symbol", "BTC/USDT:USDT")

    # ══════════════════════════════════════════
    #  Connection
    # ══════════════════════════════════════════

    async def start(self, symbol: str = SYMBOL):
        """OKX WS 연결 시작 (무한 재시도)"""
        self._running = True
        self._reconnect_count = 0

        while self._running:
            try:
                await self._connect(symbol)
                self._reconnect_count = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._reconnect_count += 1
                wait = min(5 * min(self._reconnect_count, 12), 60)
                logger.warning(f"OKX WS 끊김: {e} → {wait}초 후 재연결 (시도 {self._reconnect_count})")
                await asyncio.sleep(wait)

    async def _connect(self, symbol: str):
        """WS 연결 + 채널 구독"""
        ws = await websockets.connect(OKX_WS_PUBLIC, ping_interval=20, open_timeout=10)
        self.ws = ws
        self._reconnect_count = 0
        logger.info(f"OKX WS Public 연결 성공: {symbol}")

        # Business WS (캔들 전용)
        ws_biz = await websockets.connect(OKX_WS_BUSINESS, ping_interval=20, open_timeout=10)
        logger.info("OKX WS Business 연결 성공 (캔들)")

        try:
            # Public: tickers only
            await ws.send(json.dumps({
                "op": "subscribe",
                "args": [
                    {"channel": "tickers", "instId": symbol},
                ],
            }))

            # Business: 캔들 7종
            await ws_biz.send(json.dumps({
                "op": "subscribe",
                "args": [
                    {"channel": "candle1m", "instId": symbol},
                    {"channel": "candle5m", "instId": symbol},
                    {"channel": "candle15m", "instId": symbol},
                    {"channel": "candle1H", "instId": symbol},
                    {"channel": "candle4H", "instId": symbol},
                    {"channel": "candle1D", "instId": symbol},
                    {"channel": "candle1W", "instId": symbol},
                ],
            }))
            logger.info("OKX WS 구독: tickers + candle 7종")

            self._ws_tasks_done = asyncio.Event()

            async def _recv_loop(ws_conn, name):
                while self._running and not self._ws_tasks_done.is_set():
                    try:
                        message = await asyncio.wait_for(ws_conn.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        try:
                            await ws_conn.ping()
                            continue
                        except Exception:
                            logger.warning(f"OKX WS {name} ping 실패 → 재연결")
                            break
                    except Exception as e:
                        logger.warning(f"OKX WS {name} 수신 끊김: {e} → 재연결")
                        break
                    try:
                        data = json.loads(message)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    try:
                        await self._handle_message(data)
                    except Exception as e:
                        logger.error(f"OKX WS {name} 처리 에러: {e}", exc_info=True)
                self._ws_tasks_done.set()

            await asyncio.gather(
                _recv_loop(ws, "public"),
                _recv_loop(ws_biz, "business"),
            )
        finally:
            await ws.close()
            await ws_biz.close()

    async def _handle_message(self, data: dict):
        """수신 메시지 라우팅"""
        if "event" in data:
            if data["event"] == "subscribe":
                logger.info(f"OKX WS 구독 확인: {data.get('arg', {}).get('channel')}")
            elif data["event"] == "error":
                logger.error(f"OKX WS 에러: {data}")
            return

        arg = data.get("arg", {})
        channel = arg.get("channel", "")
        items = data.get("data", [])
        if not items:
            return

        if channel == "tickers":
            await self._handle_ticker(items[0])
        elif channel.startswith("candle"):
            tf = channel.replace("candle", "")
            for candle in items:
                await self._handle_candle(candle, tf)

    # ══════════════════════════════════════════
    #  Ticker
    # ══════════════════════════════════════════

    async def _handle_ticker(self, ticker: dict):
        symbol = ticker.get("instId", SYMBOL)
        await self.redis.set(f"rt:price:{symbol}", ticker.get("last", "0"), ttl=30)
        await self.redis.hset(f"rt:ticker:{symbol}", {
            "last": ticker.get("last", "0"),
            "bid": ticker.get("bidPx", "0"),
            "ask": ticker.get("askPx", "0"),
            "high24h": ticker.get("high24h", "0"),
            "low24h": ticker.get("low24h", "0"),
            "vol24h": ticker.get("volCcy24h", "0"),
            "timestamp": ticker.get("ts", "0"),
        }, ttl=30)

    # ══════════════════════════════════════════
    #  Candle → DB 저장 + 이벤트 발행
    # ══════════════════════════════════════════

    async def _handle_candle(self, candle: list, tf: str):
        """OKX candle: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]"""
        if len(candle) < 9:
            return

        is_closed = candle[8] == "1"
        candle_dict = {
            "timestamp": int(candle[0]),
            "open": float(candle[1]),
            "high": float(candle[2]),
            "low": float(candle[3]),
            "close": float(candle[4]),
            "volume": float(candle[5]),
        }

        if candle_dict["close"] <= 0:
            return

        tf_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1H": "1h", "4H": "4h", "1D": "1d", "1W": "1w"}
        std_tf = tf_map.get(tf, tf.lower())

        if self.db and is_closed:
            try:
                await self.db.insert_candles(self._db_symbol, std_tf, [candle_dict])
            except Exception as e:
                logger.debug(f"OKX candle DB 저장 실패 ({std_tf}): {e}")

        if is_closed:
            try:
                await self.redis.publish("ch:kline:ready", json.dumps({
                    "tf": std_tf, "close": candle_dict["close"], "ts": candle_dict["timestamp"],
                }))
            except Exception:
                pass

    def stop(self):
        self._running = False
