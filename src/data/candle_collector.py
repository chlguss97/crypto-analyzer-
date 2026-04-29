import ccxt.async_support as ccxt
import asyncio
import logging
import time
from src.data.storage import Database
from src.utils.helpers import load_config, get_env

logger = logging.getLogger(__name__)

# 타임프레임 → 밀리초 변환
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
    """캔들 데이터 수집 — 04-17: Binance 선물 기준 분석 + OKX 실행"""

    # Binance 심볼 매핑
    BINANCE_SYMBOL = "BTC/USDT"  # spot (futures 403 차단)

    def __init__(self, db: Database):
        self.db = db
        self.config = load_config()
        self.symbol = self.config["exchange"]["symbol"]  # OKX 심볼 (실행용)
        self.exchange: ccxt.okx | None = None
        self.binance: ccxt.binance | None = None  # 분석용

    async def init_exchange(self):
        """OKX + Binance 연결 초기화"""
        try:
            api_key = get_env("OKX_API_KEY", "")
            secret = get_env("OKX_SECRET_KEY", "")
            passphrase = get_env("OKX_PASSPHRASE", "")

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

            if not api_key:
                logger.warning("API 키 없음 → 퍼블릭 데이터만 수집 가능")

            await self.exchange.load_markets()
            logger.info(f"OKX 연결 완료: {self.symbol}")

            # Binance Futures — 인증 불필요 (퍼블릭 캔들)
            self.binance = ccxt.binance({
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},  # futures fstream 403 → spot 사용
            })
            self.binance.aiohttp_resolver = "default"
            await self.binance.load_markets()
            logger.info(f"Binance 연결 완료: {self.BINANCE_SYMBOL} (분석용)")

        except Exception as e:
            logger.error(f"OKX 연결 실패: {e}")
            raise

    async def close(self):
        if self.exchange:
            await self.exchange.close()
        if self.binance:
            await self.binance.close()

    async def fetch_candles(
        self, timeframe: str, since: int = None, limit: int = 300,
        source: str = "binance",
    ) -> list[dict]:
        """
        캔들 조회 — 기본 Binance 선물 (분석용), OKX 폴백.
        source: "binance" (기본) | "okx" (실행가 참조용)
        """
        exchange = self.binance if source == "binance" and self.binance else self.exchange
        symbol = self.BINANCE_SYMBOL if source == "binance" and self.binance else self.symbol

        last_err = None
        for attempt in range(2):
            try:
                ohlcv = await exchange.fetch_ohlcv(
                    symbol, timeframe, since=since, limit=limit
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

        # Binance 실패 시 OKX 폴백
        if source == "binance" and self.exchange:
            logger.warning(f"Binance 캔들 실패 → OKX 폴백 [{timeframe}]")
            return await self.fetch_candles(timeframe, since, limit, source="okx")

        err_type = type(last_err).__name__
        err_msg = str(last_err) or repr(last_err)
        logger.error(f"캔들 조회 실패 [{timeframe}] {err_type}: {err_msg[:300]}")
        return []

    async def backfill(self, timeframe: str, days: int = None):
        """과거 캔들 데이터 백필"""
        if days is None:
            days = self.config.get("data", {}).get("candle_backfill_days", 30)

        tf_ms = TF_MS.get(timeframe, 900_000)
        now = int(time.time() * 1000)
        since = now - (days * 86_400_000)

        # 이미 저장된 데이터가 있으면 그 이후부터
        latest = await self.db.get_latest_candle_time(self.symbol, timeframe)
        if latest and latest > since:
            since = latest + tf_ms

        total_inserted = 0
        current = since
        max_iterations = days * 24 * 4  # 안전 한도
        iteration = 0

        while current < now and iteration < max_iterations:
            candles = await self.fetch_candles(timeframe, since=current, limit=300)
            if not candles:
                break

            await self.db.insert_candles(self.symbol, timeframe, candles)
            total_inserted += len(candles)

            # 진행 안 되면 무한 루프 방지
            new_current = candles[-1]["timestamp"] + tf_ms
            if new_current <= current:
                break
            current = new_current
            iteration += 1

            # Rate limit 존중
            await asyncio.sleep(0.2)

        logger.info(
            f"백필 완료 [{timeframe}]: {total_inserted}개 캔들 ({days}일)"
        )
        return total_inserted

    async def backfill_all(self):
        """모든 타임프레임 백필 — 단기 + HTF 포함"""
        for tf, days in [("1m", 3), ("5m", 7), ("15m", 30), ("1h", 60),
                         ("4h", 90), ("1d", 365), ("1w", 365)]:
            await self.backfill(tf, days=days)

    async def fetch_latest(self, timeframe: str) -> list[dict]:
        """최신 캔들 가져와서 DB 저장 — Binance 기준"""
        candles = await self.fetch_candles(timeframe, limit=5, source="binance")
        if candles:
            # DB에는 OKX 심볼로 저장 (기존 호환 — 쿼리가 OKX 심볼 기준)
            await self.db.insert_candles(self.symbol, timeframe, candles)
        return candles

    async def fetch_all_latest(self):
        """모든 타임프레임 최신 캔들 갱신"""
        timeframes = [
            self.config["timeframes"]["execution"],
            self.config["timeframes"]["confirmation"],
            self.config["timeframes"]["filter"],
        ]
        for tf in timeframes:
            await self.fetch_latest(tf)
