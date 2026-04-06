import ccxt.async_support as ccxt
import asyncio
import logging
import time
from src.data.storage import Database, RedisClient
from src.utils.helpers import load_config, get_env

logger = logging.getLogger(__name__)


class OIFundingCollector:
    """OI, 펀딩비, 롱숏비율 수집기"""

    def __init__(self, db: Database, redis_client: RedisClient):
        self.db = db
        self.redis = redis_client
        self.config = load_config()
        self.symbol = self.config["exchange"]["symbol"]
        self.inst_id = "BTC-USDT-SWAP"
        self.exchange: ccxt.okx | None = None

    async def init_exchange(self):
        try:
            self.exchange = ccxt.okx(
                {
                    "apiKey": get_env("OKX_API_KEY", ""),
                    "secret": get_env("OKX_SECRET_KEY", ""),
                    "password": get_env("OKX_PASSPHRASE", ""),
                    "enableRateLimit": True,
                    "options": {"defaultType": "swap"},
                }
            )
            await self.exchange.load_markets()
            logger.info("OI/Funding 수집기 초기화 완료")
        except Exception as e:
            logger.error(f"OI/Funding 수집기 초기화 실패: {e}")
            raise

    async def close(self):
        if self.exchange:
            await self.exchange.close()

    async def fetch_open_interest(self) -> dict | None:
        """미결제약정 조회"""
        try:
            response = await self.exchange.public_get_public_open_interest(
                {"instType": "SWAP", "instId": self.inst_id}
            )
            data = response.get("data", [])
            if data:
                oi = float(data[0].get("oi", 0))
                oi_ccy = float(data[0].get("oiCcy", 0))
                return {"oi": oi, "oi_ccy": oi_ccy, "timestamp": int(data[0].get("ts", 0))}
        except Exception as e:
            logger.error(f"OI 조회 실패: {e}")
        return None

    async def fetch_funding_rate(self) -> dict | None:
        """현재 펀딩비 조회"""
        try:
            response = await self.exchange.public_get_public_funding_rate(
                {"instId": self.inst_id}
            )
            data = response.get("data", [])
            if data:
                return {
                    "current_rate": float(data[0].get("fundingRate", 0)),
                    "next_rate": float(data[0].get("nextFundingRate", 0)),
                    "funding_time": int(data[0].get("fundingTime", 0)),
                }
        except Exception as e:
            logger.error(f"펀딩비 조회 실패: {e}")
        return None

    async def fetch_long_short_ratio(self) -> dict | None:
        """롱숏비율 조회"""
        try:
            response = await self.exchange.public_get_rubik_stat_contracts_long_short_account_ratio(
                {"ccy": "BTC", "period": "5m"}
            )
            data = response.get("data", [])
            if data:
                latest = data[0]
                return {
                    "ratio": float(latest[1]) if len(latest) > 1 else None,
                    "timestamp": int(latest[0]) if latest else 0,
                }
        except Exception as e:
            logger.error(f"롱숏비율 조회 실패: {e}")
        return None

    async def collect_all(self):
        """전체 데이터 수집 + 저장"""
        now_ts = int(time.time() * 1000)

        oi_data = await self.fetch_open_interest()
        funding_data = await self.fetch_funding_rate()
        ls_data = await self.fetch_long_short_ratio()

        # SQLite 저장
        record = {
            "symbol": self.symbol,
            "timestamp": now_ts,
            "open_interest": oi_data["oi_ccy"] if oi_data else None,
            "funding_rate": funding_data["current_rate"] if funding_data else None,
            "long_short_ratio_account": ls_data["ratio"] if ls_data else None,
            "long_short_ratio_position": None,  # 별도 API 필요 시 추가
        }
        await self.db.insert_oi_funding(record)

        # Redis 캐시 (실시간 조회용)
        if oi_data:
            await self.redis.set(
                f"rt:oi:{self.inst_id}",
                str(oi_data["oi_ccy"]),
                ttl=600,
            )

        if funding_data:
            await self.redis.set(
                f"rt:funding:{self.inst_id}",
                str(funding_data["current_rate"]),
                ttl=600,
            )
            # 다음 정산까지 남은 시간 (분)
            next_settlement = funding_data["funding_time"]
            remaining_min = max(0, (next_settlement - now_ts) // 60_000)
            await self.redis.set(
                f"rt:funding_next_min:{self.inst_id}",
                str(remaining_min),
                ttl=600,
            )

        if ls_data and ls_data["ratio"] is not None:
            await self.redis.set(
                f"rt:ls_ratio:{self.inst_id}",
                str(ls_data["ratio"]),
                ttl=600,
            )

        logger.debug(
            f"OI/Funding 수집 완료 - OI: {oi_data}, FR: {funding_data}, LS: {ls_data}"
        )
