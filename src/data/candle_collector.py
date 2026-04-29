"""
캔들 데이터 수집 — OKX REST (백필 + 30초 백업)
WS 캔들은 ws_stream.py에서 실시간 저장.
"""

import ccxt.async_support as ccxt
import asyncio
import logging
import time
from src.data.storage import Database
from src.utils.helpers import load_config, get_env

logger = logging.getLogger(__name__)

TF_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
    "1w": 604_800_000,
}


class CandleCollector:
    """캔들 데이터 수집 — OKX REST Only"""

    def __init__(self, db: Database):
        self.db = db
        self.config = load_config()
        self.symbol = self.config["exchange"]["symbol"]
        self.exchange: ccxt.okx | None = None

    async def init_exchange(self):
        """OKX 연결 초기화"""
        try:
            api_key = get_env("OKX_API_KEY", "")
            secret = get_env("OKX_SECRET_KEY", "")
            passphrase = get_env("OKX_PASSPHRASE", "")

            self.exchange = ccxt.okx({
                "apiKey": api_key,
                "secret": secret,
                "password": passphrase,
                "enableRateLimit": True,
                "aiohttp_trust_env": True,
                "options": {"defaultType": "swap"},
            })
            self.exchange.aiohttp_resolver = "default"

            if not api_key:
                logger.warning("API 키 없음 → 퍼블릭 데이터만 수집 가능")

            await self.exchange.load_markets()
            logger.info(f"OKX 연결 완료: {self.symbol}")

        except Exception as e:
            logger.error(f"OKX 연결 실패: {e}")
            raise

    async def close(self):
        if self.exchange:
            await self.exchange.close()

    async def fetch_candles(self, timeframe: str, since: int = None, limit: int = 300) -> list[dict]:
        """OKX REST 캔들 조회"""
        last_err = None
        for attempt in range(2):
            try:
                ohlcv = await self.exchange.fetch_ohlcv(
                    self.symbol, timeframe, since=since, limit=limit
                )
                return [
                    {
                        "timestamp": c[0],
                        "open": c[1],
                        "high": c[2],
                        "low": c[3],
                        "close": c[4],
                        "volume": c[5],
                    }
                    for c in ohlcv
                ]
            except Exception as e:
                last_err = e
                if attempt < 1:
                    await asyncio.sleep(1)

        logger.error(f"캔들 조회 실패 [{timeframe}]: {last_err}")
        return []

    async def backfill(self, timeframe: str, days: int = None):
        """과거 캔들 백필"""
        if days is None:
            days = self.config.get("data", {}).get("candle_backfill_days", 30)

        tf_ms = TF_MS.get(timeframe, 900_000)
        now = int(time.time() * 1000)
        since = now - (days * 86_400_000)

        latest = await self.db.get_latest_candle_time(self.symbol, timeframe)
        if latest and latest > since:
            since = latest + tf_ms

        total_inserted = 0
        current = since
        max_iterations = days * 24 * 4
        iteration = 0

        while current < now and iteration < max_iterations:
            candles = await self.fetch_candles(timeframe, since=current, limit=300)
            if not candles:
                break
            await self.db.insert_candles(self.symbol, timeframe, candles)
            total_inserted += len(candles)
            new_current = candles[-1]["timestamp"] + tf_ms
            if new_current <= current:
                break
            current = new_current
            iteration += 1
            await asyncio.sleep(0.2)

        logger.info(f"백필 완료 [{timeframe}]: {total_inserted}개 캔들 ({days}일)")
        return total_inserted

    async def backfill_all(self):
        """전체 타임프레임 백필"""
        for tf, days in [("1m", 3), ("5m", 7), ("15m", 30), ("1h", 60),
                         ("4h", 90), ("1d", 365), ("1w", 365)]:
            await self.backfill(tf, days=days)
