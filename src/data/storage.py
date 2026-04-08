import aiosqlite
import redis.asyncio as redis
import json
import logging
import os
from pathlib import Path
from src.utils.helpers import load_config, DATA_DIR

logger = logging.getLogger(__name__)

# ── SQLite ──────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS candles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    UNIQUE(symbol, timeframe, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_candles_lookup
    ON candles(symbol, timeframe, timestamp DESC);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    grade TEXT NOT NULL,
    score REAL NOT NULL,
    entry_price REAL NOT NULL,
    entry_time INTEGER NOT NULL,
    exit_price REAL,
    exit_time INTEGER,
    exit_reason TEXT,
    leverage INTEGER NOT NULL,
    position_size REAL NOT NULL,
    pnl_usdt REAL,
    pnl_pct REAL,
    fee_total REAL,
    funding_cost REAL DEFAULT 0,
    signals_snapshot TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS oi_funding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    open_interest REAL,
    funding_rate REAL,
    long_short_ratio_account REAL,
    long_short_ratio_position REAL,
    UNIQUE(symbol, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_oi_funding_lookup
    ON oi_funding(symbol, timestamp DESC);

CREATE TABLE IF NOT EXISTS daily_summary (
    date TEXT PRIMARY KEY,
    total_trades INTEGER DEFAULT 0,
    win_count INTEGER DEFAULT 0,
    loss_count INTEGER DEFAULT 0,
    total_pnl_usdt REAL DEFAULT 0,
    total_pnl_pct REAL DEFAULT 0,
    max_drawdown_pct REAL DEFAULT 0,
    total_fees REAL DEFAULT 0,
    total_funding REAL DEFAULT 0
);
"""


class Database:
    """SQLite 비동기 래퍼"""

    def __init__(self):
        self.db_path = DATA_DIR / "candles.db"
        self._db: aiosqlite.Connection | None = None

    async def connect(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        # WAL 모드 — asyncio + 다중 코루틴 동시 read/write 안정성
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.executescript(SCHEMA_SQL)
        await self._db.commit()
        logger.info(f"SQLite 연결: {self.db_path} (WAL)")

    async def close(self):
        if self._db:
            await self._db.close()
            logger.info("SQLite 연결 종료")

    # ── 캔들 ──

    async def insert_candles(self, symbol: str, timeframe: str, candles: list[dict]):
        """캔들 데이터 벌크 삽입 (중복 무시)"""
        if not candles:
            return
        await self._db.executemany(
            """INSERT OR IGNORE INTO candles
               (symbol, timeframe, timestamp, open, high, low, close, volume)
               VALUES (:symbol, :timeframe, :timestamp, :open, :high, :low, :close, :volume)""",
            [{"symbol": symbol, "timeframe": timeframe, **c} for c in candles],
        )
        await self._db.commit()

    async def get_candles(
        self, symbol: str, timeframe: str, limit: int = 500, since: int = None
    ) -> list[dict]:
        """캔들 조회 (항상 시간순 ASC 정렬 반환)"""
        if since:
            cursor = await self._db.execute(
                """SELECT timestamp, open, high, low, close, volume
                   FROM candles
                   WHERE symbol=? AND timeframe=? AND timestamp>=?
                   ORDER BY timestamp ASC LIMIT ?""",
                (symbol, timeframe, since, limit),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        else:
            # 최신 N개를 가져온 후 시간순으로 뒤집어 반환
            cursor = await self._db.execute(
                """SELECT timestamp, open, high, low, close, volume
                   FROM candles
                   WHERE symbol=? AND timeframe=?
                   ORDER BY timestamp DESC LIMIT ?""",
                (symbol, timeframe, limit),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in reversed(rows)]

    async def get_latest_candle_time(self, symbol: str, timeframe: str) -> int | None:
        """가장 최근 캔들 timestamp 조회"""
        cursor = await self._db.execute(
            """SELECT MAX(timestamp) FROM candles
               WHERE symbol=? AND timeframe=?""",
            (symbol, timeframe),
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    # ── OI / 펀딩비 ──

    async def insert_oi_funding(self, data: dict):
        """OI/펀딩비 데이터 삽입"""
        await self._db.execute(
            """INSERT OR REPLACE INTO oi_funding
               (symbol, timestamp, open_interest, funding_rate,
                long_short_ratio_account, long_short_ratio_position)
               VALUES (:symbol, :timestamp, :open_interest, :funding_rate,
                       :long_short_ratio_account, :long_short_ratio_position)""",
            data,
        )
        await self._db.commit()

    async def get_oi_funding(self, symbol: str, limit: int = 100) -> list[dict]:
        cursor = await self._db.execute(
            """SELECT * FROM oi_funding
               WHERE symbol=? ORDER BY timestamp DESC LIMIT ?""",
            (symbol, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in reversed(rows)]

    # ── 트레이드 ──

    async def insert_trade(self, trade: dict) -> int:
        cursor = await self._db.execute(
            """INSERT INTO trades
               (symbol, direction, grade, score, entry_price, entry_time,
                leverage, position_size, signals_snapshot)
               VALUES (:symbol, :direction, :grade, :score, :entry_price,
                       :entry_time, :leverage, :position_size, :signals_snapshot)""",
            trade,
        )
        await self._db.commit()
        return cursor.lastrowid

    async def update_trade_exit(self, trade_id: int, exit_data: dict):
        await self._db.execute(
            """UPDATE trades SET
               exit_price=:exit_price, exit_time=:exit_time, exit_reason=:exit_reason,
               pnl_usdt=:pnl_usdt, pnl_pct=:pnl_pct, fee_total=:fee_total,
               funding_cost=:funding_cost
               WHERE id=:id""",
            {"id": trade_id, **exit_data},
        )
        await self._db.commit()


def _json_default(obj):
    """numpy/bool 등 JSON 직렬화 헬퍼"""
    import numpy as np
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return str(obj)


# ── Redis ───────────────────────────────────────────────

class RedisClient:
    """Redis 비동기 래퍼"""

    def __init__(self):
        config = load_config()
        redis_cfg = config.get("redis", {})
        # 환경변수 우선 (Docker용), 없으면 config 사용
        self.host = os.getenv("REDIS_HOST") or redis_cfg.get("host", "localhost")
        self.port = int(os.getenv("REDIS_PORT", redis_cfg.get("port", 6379)))
        self.db_num = redis_cfg.get("db", 0)
        self._client: redis.Redis | None = None

    async def connect(self):
        self._client = redis.Redis(
            host=self.host, port=self.port, db=self.db_num, decode_responses=True
        )
        try:
            await self._client.ping()
            logger.info(f"Redis 연결: {self.host}:{self.port}")
        except Exception as e:
            logger.warning(f"Redis 연결 실패: {e} → 메모리 폴백 모드")
            self._client = None

    async def close(self):
        if self._client:
            await self._client.close()
            logger.info("Redis 연결 종료")

    @property
    def connected(self) -> bool:
        return self._client is not None

    async def set(self, key: str, value, ttl: int = None):
        if not self._client:
            return
        try:
            if isinstance(value, (dict, list)):
                value = json.dumps(value, default=_json_default)
            if ttl:
                await self._client.setex(key, ttl, value)
            else:
                await self._client.set(key, value)
        except Exception as e:
            logger.debug(f"Redis set error ({key}): {e}")

    async def get(self, key: str) -> str | None:
        if not self._client:
            return None
        try:
            return await self._client.get(key)
        except Exception as e:
            logger.debug(f"Redis get error ({key}): {e}")
            return None

    async def get_json(self, key: str) -> dict | list | None:
        val = await self.get(key)
        if val:
            return json.loads(val)
        return None

    async def hset(self, key: str, mapping: dict):
        if not self._client:
            return
        try:
            for field, value in mapping.items():
                await self._client.hset(key, field, value)
        except Exception as e:
            logger.debug(f"Redis hset error ({key}): {e}")

    async def hgetall(self, key: str) -> dict:
        if not self._client:
            return {}
        try:
            return await self._client.hgetall(key)
        except Exception as e:
            logger.debug(f"Redis hgetall error ({key}): {e}")
            return {}

    async def delete(self, key: str):
        if not self._client:
            return
        try:
            await self._client.delete(key)
        except Exception as e:
            logger.debug(f"Redis delete error ({key}): {e}")

    async def publish(self, channel: str, message: str):
        if not self._client:
            return
        try:
            await self._client.publish(channel, message)
        except Exception as e:
            logger.debug(f"Redis publish error ({channel}): {e}")
