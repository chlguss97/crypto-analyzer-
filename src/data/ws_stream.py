import asyncio
import json
import logging
import time
import websockets
from src.data.storage import RedisClient

logger = logging.getLogger(__name__)

OKX_WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"


class WebSocketStream:
    """OKX WebSocket 실시간 데이터 스트림"""

    def __init__(self, redis_client: RedisClient):
        self.redis = redis_client
        self.ws = None
        self._running = False
        self._reconnect_count = 0
        self._cvd_15m = 0.0
        self._cvd_1h = 0.0
        self._cvd_reset_15m = 0
        self._cvd_reset_1h = 0
        # 가격 변속도 추적 (급등락 감지용)
        self._price_window = []  # [(timestamp_ms, price), ...]
        self._price_window_max = 120  # 최근 120 체결 보관 (~60초분)

    async def start(self, symbol: str = "BTC-USDT-SWAP"):
        """WebSocket 연결 시작 (무한 재시도)"""
        self._running = True
        self._reconnect_count = 0

        while self._running:
            try:
                await self._connect(symbol)
                # 정상 종료 시에도 재연결
                self._reconnect_count = 0
            except Exception as e:
                self._reconnect_count += 1
                # 무한 재시도 (최대 60초 대기)
                wait = min(5 * min(self._reconnect_count, 12), 60)
                logger.warning(
                    f"WebSocket 끊김: {e} → {wait}초 후 재연결 (시도 {self._reconnect_count})"
                )
                await asyncio.sleep(wait)

    async def _connect(self, symbol: str):
        """WebSocket 연결 + 구독"""
        async with websockets.connect(OKX_WS_PUBLIC, ping_interval=20) as ws:
            self.ws = ws
            self._reconnect_count = 0
            logger.info("WebSocket 연결 성공")

            # 구독: 틱, 체결, 캔들(15m)
            subscribe_msg = {
                "op": "subscribe",
                "args": [
                    {"channel": "tickers", "instId": symbol},
                    {"channel": "trades", "instId": symbol},
                    {"channel": "candle15m", "instId": symbol},
                ],
            }
            await ws.send(json.dumps(subscribe_msg))

            async for message in ws:
                if not self._running:
                    break
                # JSON parse 실패가 연결을 끊지 않게 (잘못된 메시지 1건은 skip)
                try:
                    data = json.loads(message)
                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning(f"WS JSON parse 실패 (skip): {e} | msg[:120]={str(message)[:120]}")
                    continue
                # 메시지 처리 실패도 연결 유지
                try:
                    await self._handle_message(data)
                except Exception as e:
                    logger.error(f"WS 메시지 처리 에러 (skip): {e}", exc_info=True)
                    continue

    async def _handle_message(self, data: dict):
        """수신 메시지 처리"""
        if "event" in data:
            if data["event"] == "subscribe":
                logger.debug(f"구독 확인: {data.get('arg', {}).get('channel')}")
            return

        arg = data.get("arg", {})
        channel = arg.get("channel", "")
        items = data.get("data", [])

        if not items:
            return

        if channel == "tickers":
            await self._handle_ticker(items[0])
        elif channel == "trades":
            for trade in items:
                await self._handle_trade(trade)
        elif channel.startswith("candle"):
            await self._handle_candle(items[0])
        else:
            # OKX 가 새 채널을 보내는 경우 감지 (운영 모니터링 강화)
            logger.debug(f"WS unknown channel: {channel}")

    async def _handle_ticker(self, ticker: dict):
        """틱 데이터 → Redis"""
        symbol = ticker.get("instId", "")
        await self.redis.set(
            f"rt:price:{symbol}",
            ticker.get("last", "0"),
            ttl=30,
        )
        await self.redis.hset(
            f"rt:ticker:{symbol}",
            {
                "last": ticker.get("last", "0"),
                "bid": ticker.get("bidPx", "0"),
                "ask": ticker.get("askPx", "0"),
                "high24h": ticker.get("high24h", "0"),
                "low24h": ticker.get("low24h", "0"),
                "vol24h": ticker.get("volCcy24h", "0"),
                "timestamp": ticker.get("ts", "0"),
            },
        )

    async def _handle_trade(self, trade: dict):
        """체결 데이터 → CVD 계산 + 가격 변속도 추적"""
        price = float(trade.get("px", 0))
        size = float(trade.get("sz", 0))
        side = trade.get("side", "")  # buy or sell
        ts = int(trade.get("ts", 0))

        # ── 가격 변속도 추적 (급등락 $500-1000 감지) ──
        if price > 0 and ts > 0:
            self._price_window.append((ts, price))
            # 윈도우 관리: 60초 이상 된 데이터 제거
            cutoff = ts - 60_000  # 60초
            while self._price_window and self._price_window[0][0] < cutoff:
                self._price_window.pop(0)

            # 10초/30초/60초 내 변동폭 계산 → Redis 저장
            if len(self._price_window) >= 5:
                prices_in_window = [p for _, p in self._price_window]
                win_high = max(prices_in_window)
                win_low = min(prices_in_window)
                win_range = win_high - win_low
                oldest_price = self._price_window[0][1]
                direction_move = price - oldest_price  # 양수=상승, 음수=하락

                # 10초 윈도우
                ts_10s = ts - 10_000
                prices_10s = [p for t, p in self._price_window if t >= ts_10s]
                range_10s = max(prices_10s) - min(prices_10s) if len(prices_10s) >= 2 else 0
                move_10s = price - prices_10s[0] if prices_10s else 0

                # 30초 윈도우
                ts_30s = ts - 30_000
                prices_30s = [p for t, p in self._price_window if t >= ts_30s]
                range_30s = max(prices_30s) - min(prices_30s) if len(prices_30s) >= 2 else 0
                move_30s = price - prices_30s[0] if prices_30s else 0

                await self.redis.hset("rt:velocity:BTC-USDT-SWAP", {
                    "range_60s": str(round(win_range, 1)),
                    "move_60s": str(round(direction_move, 1)),
                    "range_30s": str(round(range_30s, 1)),
                    "move_30s": str(round(move_30s, 1)),
                    "range_10s": str(round(range_10s, 1)),
                    "move_10s": str(round(move_10s, 1)),
                    "high_60s": str(round(win_high, 1)),
                    "low_60s": str(round(win_low, 1)),
                    "ts": str(ts),
                })

        # CVD 누적: buy → +, sell → - (오버플로우 방어 ±1e9)
        delta = size if side == "buy" else -size
        MAX_CVD = 1e9
        self._cvd_15m = max(-MAX_CVD, min(MAX_CVD, self._cvd_15m + delta))
        self._cvd_1h = max(-MAX_CVD, min(MAX_CVD, self._cvd_1h + delta))

        # 진행 중인 윈도우의 CVD 도 즉시 캐시 — 시그널 엔진이 1봉 lag 없이 읽을 수 있게
        # cvd:15m: (옛 키) 는 직전 윈도우 합계, cvd:15m:current 는 진행 중 누적값
        await self.redis.set("cvd:15m:current:BTC-USDT-SWAP", str(self._cvd_15m), ttl=1000)
        await self.redis.set("cvd:1h:current:BTC-USDT-SWAP", str(self._cvd_1h), ttl=4000)

        # 15m 리셋 체크 (900초)
        now = int(time.time())
        if now // 900 != self._cvd_reset_15m:
            self._cvd_reset_15m = now // 900
            await self.redis.set("cvd:15m:BTC-USDT-SWAP", str(self._cvd_15m), ttl=1800)
            self._cvd_15m = 0.0

        # 1h 리셋 체크 (3600초)
        if now // 3600 != self._cvd_reset_1h:
            self._cvd_reset_1h = now // 3600
            await self.redis.set("cvd:1h:BTC-USDT-SWAP", str(self._cvd_1h), ttl=7200)
            self._cvd_1h = 0.0

    async def _handle_candle(self, candle: list):
        """15m 캔들 완성 알림"""
        # candle: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        if len(candle) >= 9 and candle[8] == "1":
            # 캔들 확정 → 퍼블리시
            await self.redis.publish(
                "ch:candle:BTC-USDT-SWAP",
                json.dumps({
                    "timestamp": int(candle[0]),
                    "open": float(candle[1]),
                    "high": float(candle[2]),
                    "low": float(candle[3]),
                    "close": float(candle[4]),
                    "volume": float(candle[5]),
                    "confirmed": True,
                }),
            )
            logger.info(f"15m 캔들 확정: {candle[4]}")

    def stop(self):
        """스트림 중지"""
        self._running = False
