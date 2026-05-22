"""
OKX WebSocket — Grid Trading 데이터 수집

수집 항목:
  1. trades → CVD 5m/15m/1h + Volume tracking
  2. tickers → 가격/ticker
  3. candle 7종 → DB 직접 저장 + 이벤트 발행
  4. books5 → OBI (인메모리, ws.obi 프로퍼티)

Redis 키:
  rt:price:BTC-USDT-SWAP         — 가격
  rt:ticker:BTC-USDT-SWAP        — ticker
"""

import asyncio
import json
import logging
import time
import websockets
from collections import deque
from src.data.storage import RedisClient

logger = logging.getLogger(__name__)

OKX_WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
OKX_WS_BUSINESS = "wss://ws.okx.com:8443/ws/v5/business"  # 캔들 전용
SYMBOL = "BTC-USDT-SWAP"


class WebSocketStream:
    """OKX WebSocket — Grid Trading 시장 데이터 수집"""

    def __init__(self, redis_client: RedisClient, db=None):
        self.redis = redis_client
        self.db = db
        self.ws = None
        self._running = False
        self._reconnect_count = 0

        # CVD 누적 (5m / 15m / 1h)
        self._cvd_5m = 0.0
        self._cvd_15m = 0.0
        self._cvd_1h = 0.0
        self._cvd_reset_5m = 0
        self._cvd_reset_15m = 0
        self._cvd_reset_1h = 0

        # Trade 버퍼 (volume spike detection용)
        self._trades: deque = deque(maxlen=50000)
        self._cvd_snapshots: deque = deque(maxlen=30)
        self._last_cvd_snap = 0
        self._price_history: deque = deque(maxlen=60)

        # OBI (최신값 캐시)
        self._last_obi = 0.0

        # 통계
        self._trade_count = 0
        self._last_log = 0

        # DB 저장 심볼
        from src.utils.helpers import load_config
        self._db_symbol = load_config().get("exchange", {}).get("symbol", "BTC/USDT:USDT")

    # ══════════════════════════════════════════
    #  Public API for regime_detector
    # ══════════════════════════════════════════

    @property
    def obi(self) -> float:
        """Current Order Book Imbalance [-1, 1]"""
        return self._last_obi

    @property
    def trades(self) -> deque:
        """Recent trades deque: (timestamp, side, size, price, size_usd)"""
        return self._trades

    @property
    def cvd_snapshots(self) -> deque:
        """CVD snapshots deque: (timestamp, cvd_5m_value)"""
        return self._cvd_snapshots

    @property
    def cvd_5m(self) -> float:
        return self._cvd_5m

    @property
    def cvd_15m(self) -> float:
        return self._cvd_15m

    @property
    def cvd_1h(self) -> float:
        return self._cvd_1h

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
        """WS 연결 + 전체 채널 구독"""
        ws = await websockets.connect(OKX_WS_PUBLIC, ping_interval=20, open_timeout=10)
        self.ws = ws
        self._reconnect_count = 0

        # 버퍼 리셋
        self._cvd_5m = 0.0
        self._cvd_15m = 0.0
        self._cvd_1h = 0.0
        self._cvd_reset_5m = 0
        self._cvd_reset_15m = 0
        self._cvd_reset_1h = 0
        self._trades.clear()
        self._cvd_snapshots.clear()
        self._trade_count = 0

        logger.info(f"OKX WS Public 연결 성공: {symbol} (버퍼 리셋)")

        # Business WS (캔들 전용)
        ws_biz = await websockets.connect(OKX_WS_BUSINESS, ping_interval=20, open_timeout=10)
        logger.info(f"OKX WS Business 연결 성공 (캔들)")

        try:
            # Public: trades + tickers + books
            await ws.send(json.dumps({
                "op": "subscribe",
                "args": [
                    {"channel": "tickers", "instId": symbol},
                    {"channel": "trades", "instId": symbol},
                    {"channel": "books5", "instId": symbol},
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
            logger.info("OKX WS 구독: Public 3채널 + Business 7채널")

            # 두 WS에서 동시 수신
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
                # 한쪽이 끊기면 다른 쪽도 종료
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
            logger.warning(f"[DEBUG] 빈 data: channel={channel}")
            return

        if channel == "tickers":
            await self._handle_ticker(items[0])
        elif channel == "trades":
            for trade in items:
                await self._handle_trade(trade)
        elif channel.startswith("candle"):
            tf = channel.replace("candle", "")
            for candle in items:
                await self._handle_candle(candle, tf)
        elif channel == "books5":
            await self._handle_books(items[0])
        else:
            logger.debug(f"알 수 없는 channel: {channel}")

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
    #  Trades → CVD + Volume tracking
    # ══════════════════════════════════════════

    async def _handle_trade(self, trade: dict):
        price = float(trade.get("px", 0))
        size = float(trade.get("sz", 0))
        side = trade.get("side", "")
        ts = int(trade.get("ts", 0))

        if price <= 0 or size <= 0:
            return

        size_usd = price * size
        delta = size if side == "buy" else -size
        now_f = time.time()

        # ── CVD 누적 ──
        MAX_CVD = 1e9
        self._cvd_5m = max(-MAX_CVD, min(MAX_CVD, self._cvd_5m + delta))
        self._cvd_15m = max(-MAX_CVD, min(MAX_CVD, self._cvd_15m + delta))
        self._cvd_1h = max(-MAX_CVD, min(MAX_CVD, self._cvd_1h + delta))

        # ── 버퍼 ──
        self._trades.append((now_f, side, size, price, size_usd))
        self._price_history.append((now_f, price))

        # CVD 스냅샷 (2초 간격)
        if now_f - self._last_cvd_snap >= 2:
            self._cvd_snapshots.append((now_f, self._cvd_5m))
            self._last_cvd_snap = now_f

        self._trade_count += 1

        # ── CVD 윈도우 리셋 ──
        now_sec = int(now_f)
        if now_sec // 300 != self._cvd_reset_5m:
            self._cvd_reset_5m = now_sec // 300
            self._cvd_5m = delta
        if now_sec // 900 != self._cvd_reset_15m:
            self._cvd_reset_15m = now_sec // 900
            self._cvd_15m = delta
        if now_sec // 3600 != self._cvd_reset_1h:
            self._cvd_reset_1h = now_sec // 3600
            self._cvd_1h = delta

        # 주기 로그 (5분마다)
        if now_sec - self._last_log >= 300:
            self._last_log = now_sec
            logger.info(f"OKX trades: {self._trade_count}건")

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

        # TF 매핑 (OKX → 표준)
        tf_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1H": "1h", "4H": "4h", "1D": "1d", "1W": "1w"}
        std_tf = tf_map.get(tf, tf.lower())

        # DB 저장 — 완성봉만
        if self.db and is_closed:
            try:
                await self.db.insert_candles(self._db_symbol, std_tf, [candle_dict])
            except Exception as e:
                logger.debug(f"OKX candle DB 저장 실패 ({std_tf}): {e}")

        # 캔들 확정 → 이벤트 발행
        if is_closed:
            try:
                await self.redis.publish("ch:kline:ready", json.dumps({
                    "tf": std_tf, "close": candle_dict["close"], "ts": candle_dict["timestamp"],
                }))
            except Exception:
                pass

    # ══════════════════════════════════════════
    #  Books (호가창 — OBI + Spread)
    # ══════════════════════════════════════════

    async def _handle_books(self, book: dict):
        """호가 불균형 (OBI) + spread 계산"""
        try:
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if not bids or not asks:
                return

            bid_sizes = [float(b[1]) for b in bids[:5]]
            ask_sizes = [float(a[1]) for a in asks[:5]]
            bid_total = sum(bid_sizes)
            ask_total = sum(ask_sizes)
            total = bid_total + ask_total
            if total <= 0:
                return

            imbalance = (bid_total - ask_total) / total
            spread = float(asks[0][0]) - float(bids[0][0])

            self._last_obi = imbalance

        except Exception:
            pass

    def stop(self):
        self._running = False
