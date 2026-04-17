"""
Binance BTCUSDT 실시간 데이터 스트림 — OKX 보완용 (인증 불필요).

수집 항목:
  1. aggTrades → CVD (OKX + Binance 합산 = 시장 전체 플로우)
  2. aggTrades → 대형 체결 감지 ($50k+)
  3. ticker → Binance 가격 (OKX 대비 프리미엄 추적)

Redis 키:
  bn:cvd:5m:BTCUSDT          — Binance 5분 CVD (진행 중)
  bn:cvd:15m:BTCUSDT         — Binance 15분 CVD (진행 중)
  bn:cvd:1h:BTCUSDT          — Binance 1시간 CVD (진행 중)
  bn:whale:BTCUSDT           — 최근 대형 체결 리스트 (JSON)
  bn:price:BTCUSDT           — Binance 현재가
  flow:combined:cvd_5m       — OKX + Binance 합산 CVD 5분
  flow:combined:cvd_15m      — OKX + Binance 합산 CVD 15분
  flow:combined:cvd_1h       — OKX + Binance 합산 CVD 1시간
  flow:combined:whale_bias   — 대형 체결 방향 편향 (-1~+1)
"""

import asyncio
import json
import logging
import time
from collections import deque
from src.data.storage import RedisClient

logger = logging.getLogger(__name__)

BINANCE_WS = "wss://fstream.binance.com/ws"
SYMBOL = "btcusdt"
WHALE_THRESHOLD_USD = 50_000  # $50k 이상 = 대형 체결
WHALE_WINDOW_SEC = 300        # 최근 5분간 대형 체결 추적


class BinanceStream:
    """Binance Futures BTCUSDT WebSocket — CVD + 대형 체결 + 가격"""

    def __init__(self, redis_client: RedisClient):
        self.redis = redis_client
        self._running = False
        self._reconnect_count = 0

        # CVD 누적 (5m / 15m / 1h)
        self._cvd_5m = 0.0
        self._cvd_15m = 0.0
        self._cvd_1h = 0.0
        self._cvd_reset_5m = 0
        self._cvd_reset_15m = 0
        self._cvd_reset_1h = 0

        # 대형 체결 추적
        self._whales: deque = deque(maxlen=200)  # (ts, side, size_usd, price)

        # 통계
        self._trade_count = 0
        self._last_log = 0

    async def start(self):
        """WebSocket 연결 시작 (무한 재시도)"""
        import websockets
        self._running = True
        self._reconnect_count = 0

        # aggTrades + miniTicker 멀티스트림
        url = f"{BINANCE_WS}/{SYMBOL}@aggTrade/{SYMBOL}@miniTicker"

        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    self._reconnect_count = 0
                    logger.info(f"Binance WS 연결 성공: {SYMBOL}")
                    async for message in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(message)
                            await self._handle(data)
                        except json.JSONDecodeError:
                            continue
                        except Exception as e:
                            logger.debug(f"Binance WS 처리 에러: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._reconnect_count += 1
                wait = min(5 * min(self._reconnect_count, 12), 60)
                logger.warning(f"Binance WS 끊김: {e} → {wait}초 후 재연결")
                await asyncio.sleep(wait)

    def stop(self):
        self._running = False

    async def _handle(self, data: dict):
        event = data.get("e", "")
        if event == "aggTrade":
            await self._on_agg_trade(data)
        elif event == "24hrMiniTicker":
            await self._on_ticker(data)

    async def _on_agg_trade(self, t: dict):
        """체결 → CVD 누적 + 대형 체결 감지"""
        price = float(t.get("p", 0))
        qty = float(t.get("q", 0))
        is_buyer_maker = t.get("m", False)  # True = 매도 체결 (seller aggressor)
        ts = int(t.get("T", 0))

        if price <= 0 or qty <= 0:
            return

        size_usd = price * qty
        # Binance: m=True → seller is maker → buyer aggressed → "buy" volume
        # m=False → buyer is maker → seller aggressed → "sell" volume
        # NOTE: Binance aggTrade m 필드는 OKX와 반대! m=True = taker sell
        side = "sell" if is_buyer_maker else "buy"
        delta = qty if side == "buy" else -qty

        # CVD 누적
        MAX_CVD = 1e9
        self._cvd_5m = max(-MAX_CVD, min(MAX_CVD, self._cvd_5m + delta))
        self._cvd_15m = max(-MAX_CVD, min(MAX_CVD, self._cvd_15m + delta))
        self._cvd_1h = max(-MAX_CVD, min(MAX_CVD, self._cvd_1h + delta))

        # Redis 저장 (매 체결마다는 과부하 → 100체결마다 or 대형 체결 시)
        self._trade_count += 1
        flush = self._trade_count % 100 == 0 or size_usd >= WHALE_THRESHOLD_USD

        if flush:
            await self.redis.set("bn:cvd:5m:BTCUSDT", str(round(self._cvd_5m, 4)), ttl=400)
            await self.redis.set("bn:cvd:15m:BTCUSDT", str(round(self._cvd_15m, 4)), ttl=1200)
            await self.redis.set("bn:cvd:1h:BTCUSDT", str(round(self._cvd_1h, 4)), ttl=4800)

            # 합산 CVD 계산 (OKX + Binance)
            await self._update_combined_cvd()

        # 대형 체결 감지
        if size_usd >= WHALE_THRESHOLD_USD:
            now = time.time()
            self._whales.append((now, side, round(size_usd), round(price, 1)))
            logger.info(f"🐋 Binance 대형 체결: {side.upper()} ${size_usd:,.0f} @ ${price:,.1f}")

            # 오래된 거 정리 (5분 초과)
            while self._whales and self._whales[0][0] < now - WHALE_WINDOW_SEC:
                self._whales.popleft()

            # 대형 체결 방향 편향 계산
            buy_vol = sum(s for _, sd, s, _ in self._whales if sd == "buy")
            sell_vol = sum(s for _, sd, s, _ in self._whales if sd == "sell")
            total = buy_vol + sell_vol
            whale_bias = (buy_vol - sell_vol) / total if total > 0 else 0
            # -1 (숏 압도) ~ +1 (롱 압도)

            await self.redis.set("flow:combined:whale_bias", str(round(whale_bias, 3)), ttl=600)
            await self.redis.set("bn:whale:BTCUSDT", json.dumps({
                "count": len(self._whales),
                "buy_vol": buy_vol,
                "sell_vol": sell_vol,
                "bias": round(whale_bias, 3),
                "recent": list(self._whales)[-10:],  # 최근 10건
            }), ttl=600)

        # CVD 윈도우 리셋
        now_sec = int(time.time())
        if now_sec // 300 != self._cvd_reset_5m:
            self._cvd_reset_5m = now_sec // 300
            self._cvd_5m = 0.0
        if now_sec // 900 != self._cvd_reset_15m:
            self._cvd_reset_15m = now_sec // 900
            self._cvd_15m = 0.0
        if now_sec // 3600 != self._cvd_reset_1h:
            self._cvd_reset_1h = now_sec // 3600
            self._cvd_1h = 0.0

        # 5분마다 로그
        if now_sec - self._last_log >= 300:
            self._last_log = now_sec
            logger.info(
                f"Binance CVD: 5m={self._cvd_5m:+.2f} 15m={self._cvd_15m:+.2f} "
                f"1h={self._cvd_1h:+.2f} | whales={len(self._whales)}"
            )

    async def _on_ticker(self, t: dict):
        """미니티커 → Binance 가격 + OKX 대비 프리미엄"""
        price = float(t.get("c", 0))
        if price > 0:
            await self.redis.set("bn:price:BTCUSDT", str(price), ttl=30)

            # OKX 가격과 비교 → 프리미엄 계산
            okx_price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
            if okx_price_str:
                okx_price = float(okx_price_str)
                if okx_price > 0:
                    premium_pct = (price - okx_price) / okx_price * 100
                    await self.redis.set("flow:okx_bn_premium", str(round(premium_pct, 4)), ttl=60)

    async def _update_combined_cvd(self):
        """OKX + Binance CVD 합산 → Redis"""
        try:
            # OKX CVD 읽기
            okx_15m = float(await self.redis.get("cvd:15m:current:BTC-USDT-SWAP") or 0)
            okx_1h = float(await self.redis.get("cvd:1h:current:BTC-USDT-SWAP") or 0)

            # 합산 (Binance 가중치 높게 — 거래량 2~3배)
            combined_5m = self._cvd_5m * 1.0  # Binance만 (OKX에 5m CVD 없음)
            combined_15m = okx_15m + self._cvd_15m
            combined_1h = okx_1h + self._cvd_1h

            await self.redis.set("flow:combined:cvd_5m", str(round(combined_5m, 4)), ttl=400)
            await self.redis.set("flow:combined:cvd_15m", str(round(combined_15m, 4)), ttl=1200)
            await self.redis.set("flow:combined:cvd_1h", str(round(combined_1h, 4)), ttl=4800)
        except Exception as e:
            logger.debug(f"합산 CVD 계산 실패: {e}")
