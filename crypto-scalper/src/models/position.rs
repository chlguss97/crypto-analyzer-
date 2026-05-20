use super::signal::Direction;

/// 스캘핑 포지션
#[derive(Debug, Clone)]
pub struct ScalpPosition {
    pub trade_id: i64,
    pub signal_id: i64,
    pub direction: Direction,
    pub entry_price: f64,
    pub size: f64,              // BTC
    pub leverage: u32,
    pub sl_price: f64,
    pub tp_price: f64,
    pub entry_time: f64,        // unix timestamp (seconds)

    // OKX algo order IDs
    pub sl_algo_id: Option<String>,
    pub tp_algo_id: Option<String>,

    // Tracking
    pub best_price: f64,
    pub worst_price: f64,
    pub total_fee: f64,
    pub close_attempts: u32,

    // SL self-heal
    pub sl_lost_count: u32,
    pub last_sl_verify: f64,
}

impl ScalpPosition {
    pub fn new(
        trade_id: i64,
        signal_id: i64,
        direction: Direction,
        entry_price: f64,
        size: f64,
        leverage: u32,
        sl_price: f64,
        tp_price: f64,
    ) -> Self {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs_f64();

        Self {
            trade_id,
            signal_id,
            direction,
            entry_price,
            size,
            leverage,
            sl_price,
            tp_price,
            entry_time: now,
            sl_algo_id: None,
            tp_algo_id: None,
            best_price: entry_price,
            worst_price: entry_price,
            total_fee: 0.0,
            close_attempts: 0,
            sl_lost_count: 0,
            last_sl_verify: 0.0,
        }
    }

    pub fn hold_seconds(&self) -> f64 {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs_f64();
        now - self.entry_time
    }

    pub fn margin_pct(&self, current_price: f64) -> f64 {
        match self.direction {
            Direction::Long => {
                (current_price - self.entry_price) / self.entry_price
                    * self.leverage as f64
                    * 100.0
            }
            Direction::Short => {
                (self.entry_price - current_price) / self.entry_price
                    * self.leverage as f64
                    * 100.0
            }
        }
    }

    pub fn update_best_worst(&mut self, price: f64) {
        match self.direction {
            Direction::Long => {
                self.best_price = self.best_price.max(price);
                self.worst_price = self.worst_price.min(price);
            }
            Direction::Short => {
                if self.best_price == 0.0 || price < self.best_price {
                    self.best_price = price;
                }
                self.worst_price = self.worst_price.max(price);
            }
        }
    }

    pub fn is_sl_breached(&self, price: f64) -> bool {
        match self.direction {
            Direction::Long => price <= self.sl_price,
            Direction::Short => price >= self.sl_price,
        }
    }
}
