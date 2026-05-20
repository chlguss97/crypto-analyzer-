use rusqlite::{Connection, params};
use std::path::Path;
use tracing::info;

const SCHEMA: &str = r#"
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
"#;

pub struct Database {
    conn: Connection,
}

impl Database {
    pub fn open(path: &str) -> anyhow::Result<Self> {
        let dir = Path::new(path).parent().unwrap();
        std::fs::create_dir_all(dir)?;

        let conn = Connection::open(path)?;
        conn.execute_batch("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL;")?;
        conn.execute_batch(SCHEMA)?;
        info!("SQLite 연결: {}", path);
        Ok(Self { conn })
    }

    // ── Candles ──

    pub fn insert_candles(&self, symbol: &str, tf: &str, candles: &[crate::models::candle::Candle]) -> anyhow::Result<()> {
        let tx = self.conn.unchecked_transaction()?;
        for c in candles {
            tx.execute(
                "INSERT INTO candles (symbol, timeframe, timestamp, open, high, low, close, volume)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)
                 ON CONFLICT(symbol, timeframe, timestamp)
                 DO UPDATE SET high=MAX(candles.high, excluded.high),
                              low=MIN(candles.low, excluded.low),
                              close=excluded.close, volume=excluded.volume",
                params![symbol, tf, c.timestamp, c.open, c.high, c.low, c.close, c.volume],
            )?;
        }
        tx.commit()?;
        Ok(())
    }

    pub fn get_candles(&self, symbol: &str, tf: &str, limit: usize) -> anyhow::Result<Vec<crate::models::candle::Candle>> {
        let mut stmt = self.conn.prepare(
            "SELECT timestamp, open, high, low, close, volume FROM candles
             WHERE symbol=?1 AND timeframe=?2 ORDER BY timestamp DESC LIMIT ?3"
        )?;
        let rows = stmt.query_map(params![symbol, tf, limit], |row| {
            Ok(crate::models::candle::Candle {
                timestamp: row.get(0)?,
                open: row.get(1)?,
                high: row.get(2)?,
                low: row.get(3)?,
                close: row.get(4)?,
                volume: row.get(5)?,
            })
        })?;
        let mut candles: Vec<_> = rows.filter_map(|r| r.ok()).collect();
        candles.reverse();
        Ok(candles)
    }

    // ── Scalp Signals ──

    pub fn insert_scalp_signal(&self, ts: i64, signal_type: &str, direction: &str,
                                price: f64, features: &str, regime: &str,
                                hurst: f64, vpin: f64, ml_prob: f64, ml_go: i32,
                                reject_reason: Option<&str>) -> anyhow::Result<i64> {
        self.conn.execute(
            "INSERT INTO scalp_signals (ts, signal_type, direction, price, features, regime,
             hurst, vpin, ml_prob, ml_go, entry_executed, reject_reason)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, 0, ?11)",
            params![ts, signal_type, direction, price, features, regime,
                    hurst, vpin, ml_prob, ml_go, reject_reason],
        )?;
        Ok(self.conn.last_insert_rowid())
    }

    pub fn update_signal_label(&self, id: i64, label: i32, barrier_hit: &str,
                                pnl_pct: f64, resolve_ts: i64,
                                reach_pct: f64, mae_pct: f64) -> anyhow::Result<()> {
        self.conn.execute(
            "UPDATE scalp_signals SET label=?1, barrier_hit=?2, pnl_pct=?3,
             resolve_ts=?4, reach_pct=?5, mae_pct=?6 WHERE id=?7",
            params![label, barrier_hit, pnl_pct, resolve_ts, reach_pct, mae_pct, id],
        )?;
        Ok(())
    }

    pub fn update_signal_entry(&self, id: i64) -> anyhow::Result<()> {
        self.conn.execute("UPDATE scalp_signals SET entry_executed=1 WHERE id=?1", params![id])?;
        Ok(())
    }

    pub fn get_pending_shadows(&self) -> anyhow::Result<Vec<ShadowSignal>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, ts, signal_type, direction, price, features, regime
             FROM scalp_signals WHERE label = -1 ORDER BY ts ASC"
        )?;
        let rows = stmt.query_map([], |row| {
            Ok(ShadowSignal {
                id: row.get(0)?,
                ts: row.get(1)?,
                signal_type: row.get(2)?,
                direction: row.get(3)?,
                price: row.get(4)?,
                features: row.get(5)?,
                regime: row.get(6)?,
            })
        })?;
        Ok(rows.filter_map(|r| r.ok()).collect())
    }

    pub fn get_labeled_signals(&self, limit: usize) -> anyhow::Result<Vec<LabeledSignal>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, features, label, entry_executed FROM scalp_signals
             WHERE label != -1 ORDER BY ts DESC LIMIT ?1"
        )?;
        let rows = stmt.query_map(params![limit], |row| {
            Ok(LabeledSignal {
                id: row.get(0)?,
                features: row.get(1)?,
                label: row.get(2)?,
                entry_executed: row.get(3)?,
            })
        })?;
        let mut signals: Vec<_> = rows.filter_map(|r| r.ok()).collect();
        signals.reverse();
        Ok(signals)
    }

    pub fn get_signal_count(&self) -> anyhow::Result<usize> {
        let count: usize = self.conn.query_row(
            "SELECT COUNT(*) FROM scalp_signals WHERE label != -1", [], |r| r.get(0)
        )?;
        Ok(count)
    }

    // ── Scalp Trades ──

    pub fn insert_scalp_trade(&self, signal_id: i64, direction: &str, entry_price: f64,
                               entry_time: i64, size_btc: f64, leverage: u32,
                               regime: &str, hurst: f64, features: &str) -> anyhow::Result<i64> {
        self.conn.execute(
            "INSERT INTO scalp_trades (signal_id, direction, entry_price, entry_time,
             size_btc, leverage, regime, hurst, features_snapshot)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            params![signal_id, direction, entry_price, entry_time,
                    size_btc, leverage, regime, hurst, features],
        )?;
        Ok(self.conn.last_insert_rowid())
    }

    pub fn update_scalp_trade_exit(&self, id: i64, exit_price: f64, exit_time: i64,
                                    exit_reason: &str, pnl_usdt: f64, pnl_pct: f64,
                                    fee_total: f64, hold_sec: i64) -> anyhow::Result<()> {
        self.conn.execute(
            "UPDATE scalp_trades SET exit_price=?1, exit_time=?2, exit_reason=?3,
             pnl_usdt=?4, pnl_pct=?5, fee_total=?6, hold_sec=?7 WHERE id=?8",
            params![exit_price, exit_time, exit_reason, pnl_usdt, pnl_pct, fee_total, hold_sec, id],
        )?;
        Ok(())
    }

    pub fn get_last_trade(&self) -> anyhow::Result<Option<TradeResult>> {
        let mut stmt = self.conn.prepare(
            "SELECT pnl_pct, pnl_usdt, exit_reason FROM scalp_trades ORDER BY id DESC LIMIT 1"
        )?;
        let result = stmt.query_row([], |row| {
            Ok(TradeResult {
                pnl_pct: row.get(0)?,
                pnl_usdt: row.get(1)?,
                exit_reason: row.get(2)?,
            })
        });
        match result {
            Ok(r) => Ok(Some(r)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(e.into()),
        }
    }
}

#[derive(Debug)]
pub struct ShadowSignal {
    pub id: i64,
    pub ts: i64,
    pub signal_type: String,
    pub direction: String,
    pub price: f64,
    pub features: String,
    pub regime: String,
}

#[derive(Debug)]
pub struct LabeledSignal {
    pub id: i64,
    pub features: String,
    pub label: i32,
    pub entry_executed: i32,
}

#[derive(Debug)]
pub struct TradeResult {
    pub pnl_pct: Option<f64>,
    pub pnl_usdt: Option<f64>,
    pub exit_reason: Option<String>,
}
