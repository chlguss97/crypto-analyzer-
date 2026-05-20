"""
Storage layer — SQLite (candles, scalp_signals, scalp_trades) + Redis async wrapper

v3: 스캘핑 엔진용 클린 스키마 (기존 signals/trades 테이블 폐기)
"""

import aiosqlite
import sqlite3
import redis.asyncio as redis
import json
import logging
import os
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

CREATE TABLE IF NOT EXISTS scalp_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    signal_type TEXT NOT NULL,
    direction TEXT NOT NULL,
    price REAL NOT NULL,
    features TEXT NOT NULL,
    regime TEXT NOT NULL,
    hurst REAL DEFAULT 0,
    vpin REAL DEFAULT 0,
    ml_prob REAL DEFAULT -1,
    ml_go INTEGER DEFAULT -1,
    entry_executed INTEGER DEFAULT 0,
    reject_reason TEXT,
    label INTEGER DEFAULT -1,
    barrier_hit TEXT,
    pnl_pct REAL DEFAULT 0,
    resolve_ts INTEGER DEFAULT 0,
    reach_pct REAL DEFAULT 0,
    mae_pct REAL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_scalp_signals_ts ON scalp_signals(ts);
CREATE INDEX IF NOT EXISTS idx_scalp_signals_label ON scalp_signals(label);

CREATE TABLE IF NOT EXISTS scalp_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    entry_time INTEGER NOT NULL,
    exit_time INTEGER,
    exit_reason TEXT,
    size_btc REAL NOT NULL,
    leverage INTEGER NOT NULL,
    pnl_usdt REAL,
    pnl_pct REAL,
    fee_total REAL,
    hold_sec INTEGER,
    regime TEXT,
    hurst REAL,
    features_snapshot TEXT
);
"""


class Database:
    """SQLite 비동기 래퍼 — v3 스캘핑 스키마"""

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

    # ── 스캘핑 시그널 ──

    async def insert_scalp_signal(self, signal: dict) -> int:
        cursor = await self._db.execute(
            """INSERT INTO scalp_signals
               (ts, signal_type, direction, price, features, regime,
                hurst, vpin, ml_prob, ml_go, entry_executed, reject_reason)
               VALUES (:ts, :signal_type, :direction, :price, :features, :regime,
                       :hurst, :vpin, :ml_prob, :ml_go, :entry_executed, :reject_reason)""",
            signal,
        )
        await self._db.commit()
        return cursor.lastrowid

    async def update_signal_label(self, signal_id: int, label: int,
                                  barrier_hit: str, pnl_pct: float, resolve_ts: int,
                                  reach_pct: float = 0, mae_pct: float = 0):
        await self._db.execute(
            """UPDATE scalp_signals SET label=?, barrier_hit=?, pnl_pct=?,
               resolve_ts=?, reach_pct=?, mae_pct=? WHERE id=?""",
            (label, barrier_hit, pnl_pct, resolve_ts, reach_pct, mae_pct, signal_id),
        )
        await self._db.commit()

    async def update_signal_entry(self, signal_id: int):
        await self._db.execute(
            "UPDATE scalp_signals SET entry_executed=1 WHERE id=?", (signal_id,)
        )
        await self._db.commit()

    async def get_pending_shadows(self) -> list[dict]:
        cursor = await self._db.execute(
            """SELECT id, ts, signal_type, direction, price, features, regime
               FROM scalp_signals WHERE label = -1 ORDER BY ts ASC"""
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_labeled_signals(self, limit: int = 500) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM scalp_signals WHERE label != -1 ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in reversed(rows)]

    async def get_signal_count(self, labeled_only: bool = True) -> int:
        q = "SELECT COUNT(*) FROM scalp_signals WHERE label != -1" if labeled_only \
            else "SELECT COUNT(*) FROM scalp_signals"
        cursor = await self._db.execute(q)
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_recent_shadow_wr(self, hours: int = 4) -> tuple[float, int]:
        import time as _t
        cutoff = int(_t.time()) - hours * 3600
        cursor = await self._db.execute(
            """SELECT label, COUNT(*) as cnt FROM scalp_signals
               WHERE label IN (0, 1) AND resolve_ts > ?
               GROUP BY label""",
            (cutoff,),
        )
        rows = await cursor.fetchall()
        wins = total = 0
        for r in rows:
            total += r["cnt"]
            if r["label"] == 1:
                wins = r["cnt"]
        if total == 0:
            return 50.0, 0
        return round(wins / total * 100, 1), total

    # ── 스캘핑 트레이드 ──

    async def insert_scalp_trade(self, trade: dict) -> int:
        cursor = await self._db.execute(
            """INSERT INTO scalp_trades
               (signal_id, direction, entry_price, entry_time,
                size_btc, leverage, regime, hurst, features_snapshot)
               VALUES (:signal_id, :direction, :entry_price, :entry_time,
                       :size_btc, :leverage, :regime, :hurst, :features_snapshot)""",
            trade,
        )
        await self._db.commit()
        return cursor.lastrowid

    async def update_scalp_trade_exit(self, trade_id: int, exit_data: dict):
        await self._db.execute(
            """UPDATE scalp_trades SET
               exit_price=:exit_price, exit_time=:exit_time, exit_reason=:exit_reason,
               pnl_usdt=:pnl_usdt, pnl_pct=:pnl_pct, fee_total=:fee_total,
               hold_sec=:hold_sec
               WHERE id=:id""",
            {"id": trade_id, **exit_data},
        )
        await self._db.commit()


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
