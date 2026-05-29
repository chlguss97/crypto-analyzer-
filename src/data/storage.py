"""
Storage layer — SQLite (candles, scalp_trades) + Redis async wrapper

Scalp Trading: 캔들 + 단타 매매 기록
"""

import aiosqlite
import sqlite3
import redis.asyncio as redis
import json
import logging
import os
import time
from pathlib import Path
from src.utils.helpers import load_config, DATA_DIR

logger = logging.getLogger(__name__)

# ── SQLite Schema ──────────────────────────────────────────

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

CREATE TABLE IF NOT EXISTS scalp_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    size_btc REAL NOT NULL,
    pnl_usdt REAL NOT NULL,
    fee_total REAL NOT NULL,
    entry_time INTEGER NOT NULL,
    exit_time INTEGER NOT NULL,
    exit_reason TEXT NOT NULL,
    timeframe TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scalp_trades_ts ON scalp_trades(exit_time DESC);
"""


class Database:
    """SQLite 비동기 래퍼 — Scalp Trading 스키마"""

    def __init__(self):
        self.db_path = DATA_DIR / "scalp.db"
        self._db: aiosqlite.Connection | None = None

    async def connect(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        if self.db_path.exists():
            try:
                _check = await aiosqlite.connect(str(self.db_path))
                cur = await _check.execute("PRAGMA integrity_check")
                row = await cur.fetchone()
                await _check.close()
                if not row or row[0] != "ok":
                    raise sqlite3.DatabaseError(f"integrity_check 실패: {row}")
            except Exception as e:
                import time as _t
                backup = self.db_path.with_suffix(f".db.broken.{int(_t.time())}")
                logger.critical(f"SQLite 손상 감지: {e} → 백업 후 재생성 ({backup.name})")
                try:
                    self.db_path.rename(backup)
                except Exception:
                    self.db_path.unlink(missing_ok=True)

        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=DELETE")
        await self._db.execute("PRAGMA synchronous=FULL")
        await self._db.executescript(SCHEMA_SQL)
        await self._db.commit()
        logger.info(f"SQLite 연결: {self.db_path}")

    async def close(self):
        if self._db:
            await self._db.close()

    # ── 캔들 ──

    async def insert_candles(self, symbol: str, timeframe: str, candles: list[dict]):
        if not candles:
            return
        await self._db.executemany(
            """INSERT INTO candles
               (symbol, timeframe, timestamp, open, high, low, close, volume)
               VALUES (:symbol, :timeframe, :timestamp, :open, :high, :low, :close, :volume)
               ON CONFLICT(symbol, timeframe, timestamp)
               DO UPDATE SET high=MAX(candles.high, excluded.high),
                            low=MIN(candles.low, excluded.low),
                            close=excluded.close,
                            volume=excluded.volume""",
            [{"symbol": symbol, "timeframe": timeframe, **c} for c in candles],
        )
        await self._db.commit()

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 500) -> list[dict]:
        cursor = await self._db.execute(
            """SELECT timestamp, open, high, low, close, volume
               FROM candles WHERE symbol=? AND timeframe=?
               ORDER BY timestamp DESC LIMIT ?""",
            (symbol, timeframe, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in reversed(rows)]

    async def get_latest_candle_time(self, symbol: str, timeframe: str) -> int | None:
        cursor = await self._db.execute(
            "SELECT MAX(timestamp) FROM candles WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    # ── Scalp Trades ──

    async def insert_scalp_trade(self, trade: dict) -> int:
        cursor = await self._db.execute(
            """INSERT INTO scalp_trades
               (direction, entry_price, exit_price, size_btc, pnl_usdt,
                fee_total, entry_time, exit_time, exit_reason, timeframe)
               VALUES (:direction, :entry_price, :exit_price, :size_btc, :pnl_usdt,
                       :fee_total, :entry_time, :exit_time, :exit_reason, :timeframe)""",
            trade,
        )
        await self._db.commit()
        return cursor.lastrowid

    async def get_scalp_pnl_summary(self, hours: int = 24) -> dict:
        since = int(time.time() * 1000) - hours * 3600 * 1000
        row = await self._db.execute_fetchall(
            "SELECT COUNT(*), COALESCE(SUM(pnl_usdt),0), COALESCE(SUM(fee_total),0) "
            "FROM scalp_trades WHERE exit_time > ?", (since,)
        )
        if row and row[0]:
            return {"trades": row[0][0], "pnl": row[0][1], "fees": row[0][2]}
        return {"trades": 0, "pnl": 0, "fees": 0}

    async def cleanup_old_candles(self):
        """오래된 소형 캔들 정리 — 1m(3일), 5m(7일), 15m(30일) 보존"""
        now_ms = int(time.time() * 1000)
        retention = {
            "1m": 3 * 86400 * 1000,
            "5m": 7 * 86400 * 1000,
            "15m": 30 * 86400 * 1000,
        }
        total_deleted = 0
        for tf, max_age_ms in retention.items():
            cutoff = now_ms - max_age_ms
            cursor = await self._db.execute(
                "DELETE FROM candles WHERE timeframe=? AND timestamp < ?",
                (tf, cutoff),
            )
            total_deleted += cursor.rowcount
        if total_deleted > 0:
            await self._db.commit()
            logger.info(f"캔들 정리: {total_deleted}개 삭제")
        return total_deleted


def _json_default(obj):
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
            logger.warning(f"Redis 연결 실패: {e}")
            self._client = None

    async def close(self):
        if self._client:
            await self._client.close()

    @property
    def connected(self) -> bool:
        return self._client is not None

    async def _ensure_connected(self):
        if self._client is not None:
            return
        try:
            self._client = redis.Redis(
                host=self.host, port=self.port, db=self.db_num, decode_responses=True
            )
            await self._client.ping()
            logger.info(f"Redis 재연결 성공")
        except Exception:
            self._client = None

    async def set(self, key: str, value, ttl: int = None):
        if not self._client:
            await self._ensure_connected()
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
            logger.warning(f"Redis set error ({key}): {e}")
            self._client = None

    async def get(self, key: str) -> str | None:
        if not self._client:
            await self._ensure_connected()
        if not self._client:
            return None
        try:
            return await self._client.get(key)
        except Exception as e:
            logger.warning(f"Redis get error ({key}): {e}")
            self._client = None
            return None

    async def get_json(self, key: str) -> dict | list | None:
        val = await self.get(key)
        if val:
            return json.loads(val)
        return None

    async def hset(self, key: str, mapping: dict, ttl: int = None):
        if not self._client:
            await self._ensure_connected()
        if not self._client:
            return
        try:
            for field, value in mapping.items():
                await self._client.hset(key, field, value)
            if ttl:
                await self._client.expire(key, ttl)
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

    async def keys(self, pattern: str) -> list[str]:
        if not self._client:
            return []
        try:
            return await self._client.keys(pattern)
        except Exception:
            return []

    async def delete(self, key: str):
        if not self._client:
            await self._ensure_connected()
        if not self._client:
            return
        try:
            await self._client.delete(key)
        except Exception:
            pass

    async def rpush(self, key: str, value):
        if not self._client:
            await self._ensure_connected()
        if not self._client:
            return
        try:
            if isinstance(value, (dict, list)):
                value = json.dumps(value, default=_json_default)
            await self._client.rpush(key, value)
        except Exception as e:
            logger.warning(f"Redis rpush error ({key}): {e}")
            self._client = None

    async def lpop(self, key: str) -> str | None:
        if not self._client:
            await self._ensure_connected()
        if not self._client:
            return None
        try:
            return await self._client.lpop(key)
        except Exception:
            return None

    async def publish(self, channel: str, message: str):
        if not self._client:
            return
        try:
            await self._client.publish(channel, message)
        except Exception:
            pass
