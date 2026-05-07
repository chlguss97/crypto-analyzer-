"""
Binance Futures — REST 폴링 (청산/펀딩/OI) + WS aggTrade (CVD/Whale)

CVD/Whale은 Binance Futures WS에서 계산 (OKX 대비 거래량 3~5배 → 신뢰도↑)
fstream.binance.com 차단 → fstream.binancefuture.com (대체 도메인) 사용

Redis 키:
  flow:combined:cvd_5m/15m/1h  — CVD (Binance Futures aggTrade 기반)
  flow:combined:whale_bias     — 고래 편향 (Binance $50K+ 거래)
  flow:liq:1m_total/long/short — 1분 청산 합산
  flow:liq:surge               — $500K+ 청산 이벤트
  rt:funding:BTC-USDT-SWAP     — 펀딩비
  rt:oi:BTC-USDT-SWAP          — OI
  bn:vol_ratio_1m              — 1분 거래량비
"""

import asyncio
import json
import logging
import time
from collections import deque

import aiohttp
import websockets

from src.data.storage import RedisClient

logger = logging.getLogger(__name__)

FAPI = "https://fapi.binance.com"
BINANCE_FUTURES_WS = "wss://fstream.binancefuture.com/ws/btcusdt@aggTrade"

WHALE_THRESHOLD_USD = 50_000
WHALE_WINDOW_SEC = 300


class BinanceStream:
    """Binance Futures REST + WS"""

    def __init__(self, redis_client: RedisClient, db=None):
        self.redis = redis_client
        self.db = db
        self._running = False

        # CVD 누적
        self._cvd_5m = 0.0
        self._cvd_15m = 0.0
        self._cvd_1h = 0.0
        self._cvd_5m_reset = 0
        self._cvd_15m_reset = 0
        self._cvd_1h_reset = 0

        # Whale
        self._whales: deque = deque(maxlen=500)

        # Vol ratio
        self._trade_count_1m = 0
        self._vol_1m_reset = 0
        self._vol_avg_20 = deque(maxlen=20)

        # WS 상태
        self._ws_connected = False
        self._ws_trade_count = 0
        self._last_flush = 0

    async def start(self):
        self._running = True
        # REST + WS 병렬 실행
        await asyncio.gather(
            self._rest_polling(),
            self._ws_aggtrade(),
        )

    def stop(self):
        self._running = False

    # ══════════════════════════════════════════
    #  WS aggTrade — CVD + Whale + Vol
    # ══════════════════════════════════════════

    async def _ws_aggtrade(self):
        """Binance Futures aggTrade WebSocket → CVD/Whale"""
        reconnect_count = 0

        while self._running:
            try:
                logger.info(f"Binance Futures WS 연결: {BINANCE_FUTURES_WS}")
                async with websockets.connect(
                    BINANCE_FUTURES_WS,
                    ping_interval=20,
                    ping_timeout=30,
                    open_timeout=10,
                ) as ws:
                    self._ws_connected = True
                    reconnect_count = 0
                    logger.info("Binance Futures WS 연결 성공 (aggTrade)")

                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=30)
                            data = json.loads(msg)
                            await self._process_trade(data)
                        except asyncio.TimeoutError:
                            # ping으로 연결 확인
                            try:
                                await ws.ping()
                            except Exception:
                                break
                        except websockets.ConnectionClosed:
                            break

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Binance WS 에러: {e}")

            self._ws_connected = False
            reconnect_count += 1
            wait = min(5 * reconnect_count, 60)
            logger.info(f"Binance WS {wait}초 후 재연결 (#{reconnect_count})")
            await asyncio.sleep(wait)

    async def _process_trade(self, data: dict):
        """aggTrade 메시지 처리 → CVD + Whale + Vol"""
        try:
            price = float(data["p"])
            qty = float(data["q"])
            is_buyer_maker = data.get("m", False)
            # m=true → taker sell, m=false → taker buy
            delta = -qty if is_buyer_maker else qty
            size_usd = price * qty

            now = time.time()
            now_sec = int(now)

            # CVD 윈도우 리셋
            period_5m = now_sec // 300
            period_15m = now_sec // 900
            period_1h = now_sec // 3600

            if period_5m != self._cvd_5m_reset:
                self._cvd_5m = 0.0
                self._cvd_5m_reset = period_5m
            if period_15m != self._cvd_15m_reset:
                self._cvd_15m = 0.0
                self._cvd_15m_reset = period_15m
            if period_1h != self._cvd_1h_reset:
                self._cvd_1h = 0.0
                self._cvd_1h_reset = period_1h

            # CVD 누적
            self._cvd_5m += delta
            self._cvd_15m += delta
            self._cvd_1h += delta

            # Vol ratio (1분 카운트)
            period_1m = now_sec // 60
            if period_1m != self._vol_1m_reset:
                if self._trade_count_1m > 0:
                    self._vol_avg_20.append(self._trade_count_1m)
                self._trade_count_1m = 0
                self._vol_1m_reset = period_1m
            self._trade_count_1m += 1

            # Whale 감지
            if size_usd >= WHALE_THRESHOLD_USD:
                side = "sell" if is_buyer_maker else "buy"
                self._whales.append({
                    "ts": now, "side": side,
                    "size_usd": size_usd, "price": price,
                })

            # Redis flush (100 trades마다 또는 whale 시)
            self._ws_trade_count += 1
            if self._ws_trade_count >= 100 or size_usd >= WHALE_THRESHOLD_USD or (now - self._last_flush >= 2):
                await self._flush_to_redis(now)
                self._ws_trade_count = 0
                self._last_flush = now

        except Exception as e:
            logger.debug(f"aggTrade 처리 에러: {e}")

    async def _flush_to_redis(self, now: float):
        """CVD/Whale/Vol을 Redis에 저장"""
        try:
            # CVD
            await self.redis.set("flow:combined:cvd_5m", str(round(self._cvd_5m, 2)), ttl=400)
            await self.redis.set("flow:combined:cvd_15m", str(round(self._cvd_15m, 2)), ttl=1200)
            await self.redis.set("flow:combined:cvd_1h", str(round(self._cvd_1h, 2)), ttl=4800)

            # Whale bias (최근 5분)
            cutoff = now - WHALE_WINDOW_SEC
            recent_whales = [w for w in self._whales if w["ts"] > cutoff]
            if recent_whales:
                buy_vol = sum(w["size_usd"] for w in recent_whales if w["side"] == "buy")
                sell_vol = sum(w["size_usd"] for w in recent_whales if w["side"] == "sell")
                total = buy_vol + sell_vol
                bias = (buy_vol - sell_vol) / total if total > 0 else 0
                await self.redis.set("flow:combined:whale_bias", str(round(bias, 4)), ttl=600)

            # Vol ratio (1분 거래 수 대비 20분 평균)
            if self._vol_avg_20:
                avg = sum(self._vol_avg_20) / len(self._vol_avg_20)
                ratio = self._trade_count_1m / avg if avg > 0 else 1.0
                await self.redis.set("bn:vol_ratio_1m", str(round(ratio, 2)), ttl=120)

        except Exception as e:
            logger.debug(f"Redis flush 에러: {e}")

    # ══════════════════════════════════════════
    #  REST — 청산/펀딩/OI (기존 유지)
    # ══════════════════════════════════════════

    async def _rest_polling(self):
        """REST 폴링: 청산(5초)/펀딩+OI(30초)"""
        logger.info("Binance REST 폴링 시작 (청산/펀딩비/OI)")
        self._last_funding_poll = 0

        async with aiohttp.ClientSession() as session:
            while self._running:
                now = time.time()
                try:
                    # 1. 청산
                    try:
                        async with session.get(
                            f"{FAPI}/fapi/v1/allForceOrders",
                            params={"symbol": "BTCUSDT", "limit": 20},
                            timeout=aiohttp.ClientTimeout(total=5),
                        ) as resp:
                            if resp.status == 200:
                                orders = await resp.json()
                                cutoff_ms = int((now - 60) * 1000)
                                recent = [o for o in orders if int(o.get("time", 0)) > cutoff_ms]

                                long_liq = sum(
                                    float(o.get("price", 0)) * float(o.get("origQty", 0))
                                    for o in recent if o.get("side") == "SELL"
                                )
                                short_liq = sum(
                                    float(o.get("price", 0)) * float(o.get("origQty", 0))
                                    for o in recent if o.get("side") == "BUY"
                                )
                                total_liq = long_liq + short_liq

                                await self.redis.set("flow:liq:1m_total", str(round(total_liq)), ttl=120)
                                await self.redis.set("flow:liq:1m_long", str(round(long_liq)), ttl=120)
                                await self.redis.set("flow:liq:1m_short", str(round(short_liq)), ttl=120)

                                if total_liq >= 500_000:
                                    bias = "long" if short_liq > long_liq else "short"
                                    await self.redis.set("flow:liq:surge", json.dumps({
                                        "total": round(total_liq),
                                        "long_liq": round(long_liq),
                                        "short_liq": round(short_liq),
                                        "bias": bias, "ts": now,
                                    }), ttl=120)
                    except Exception as e:
                        logger.debug(f"청산 REST 실패: {e}")

                    # 2. 펀딩비 + OI (30초)
                    if now - self._last_funding_poll >= 30:
                        self._last_funding_poll = now
                        try:
                            async with session.get(
                                f"{FAPI}/fapi/v1/premiumIndex",
                                params={"symbol": "BTCUSDT"},
                                timeout=aiohttp.ClientTimeout(total=5),
                            ) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    funding = float(data.get("lastFundingRate", 0))
                                    await self.redis.set("rt:funding:BTC-USDT-SWAP",
                                                         str(round(funding, 6)), ttl=120)
                        except Exception as e:
                            logger.debug(f"펀딩비 REST 실패: {e}")

                        try:
                            async with session.get(
                                f"{FAPI}/fapi/v1/openInterest",
                                params={"symbol": "BTCUSDT"},
                                timeout=aiohttp.ClientTimeout(total=5),
                            ) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    oi = float(data.get("openInterest", 0))
                                    await self.redis.set("rt:oi:BTC-USDT-SWAP",
                                                         str(round(oi, 2)), ttl=120)
                        except Exception as e:
                            logger.debug(f"OI REST 실패: {e}")

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Binance REST 에러: {e}")

                await asyncio.sleep(5)
