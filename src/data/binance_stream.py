"""
Binance Futures REST 폴링 — 청산 데이터만 수집.
(Vultr에서 fstream WS 차단 → 나머지 데이터는 OKX WS로 전환)

Redis 키:
  flow:liq:1m_total/1m_long/1m_short — 1분 청산 합산
  flow:liq:surge                      — $500K+ 청산 이벤트
  rt:funding:BTC-USDT-SWAP            — 펀딩비
  rt:oi:BTC-USDT-SWAP                 — OI
"""

import asyncio
import json
import logging
import time
import aiohttp
from src.data.storage import RedisClient

logger = logging.getLogger(__name__)

FAPI = "https://fapi.binance.com"


class BinanceStream:
    """Binance Futures REST 폴링 — 청산/펀딩비/OI만"""

    def __init__(self, redis_client: RedisClient, db=None):
        self.redis = redis_client
        self.db = db  # 미사용 (호환용)
        self._running = False

    async def start(self):
        """REST 폴링 시작"""
        self._running = True
        logger.info("Binance REST 폴링 시작 (청산/펀딩비/OI)")

        async with aiohttp.ClientSession() as session:
            while self._running:
                now = time.time()
                try:
                    # 1. 청산 (5초마다)
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
                                        "bias": bias,
                                        "ts": now,
                                    }), ttl=120)
                                    logger.warning(
                                        f"청산 감지: ${total_liq:,.0f} "
                                        f"(롱${long_liq:,.0f}/숏${short_liq:,.0f}) → {bias.upper()}"
                                    )
                    except Exception as e:
                        logger.debug(f"청산 REST 실패: {e}")

                    # 2. 펀딩비 + OI (30초마다)
                    if now - getattr(self, '_last_funding_poll', 0) >= 30:
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
                                    await self.redis.set("rt:funding:BTC-USDT-SWAP", str(round(funding, 6)), ttl=120)
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
                                    await self.redis.set("rt:oi:BTC-USDT-SWAP", str(round(oi, 2)), ttl=120)
                        except Exception as e:
                            logger.debug(f"OI REST 실패: {e}")

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Binance REST 폴링 에러: {e}")

                await asyncio.sleep(5)

    def stop(self):
        self._running = False
