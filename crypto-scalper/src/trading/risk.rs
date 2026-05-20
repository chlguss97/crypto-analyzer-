use tracing::{info, warn};

/// Risk Manager — BOT_KILL only (프로 동일: 인위적 제한 없음)
pub struct RiskManager {
    peak_balance: f64,
    current_balance: f64,
    bot_kill_pct: f64,
    streak: i32,
    daily_pnl: f64,
    trade_count_today: u32,
}

impl RiskManager {
    pub fn new(initial_balance: f64, bot_kill_pct: f64) -> Self {
        Self {
            peak_balance: initial_balance,
            current_balance: initial_balance,
            bot_kill_pct,
            streak: 0,
            daily_pnl: 0.0,
            trade_count_today: 0,
        }
    }

    pub fn is_trading_allowed(&self) -> (bool, &str) {
        let drawdown = if self.peak_balance > 0.0 {
            (self.peak_balance - self.current_balance) / self.peak_balance
        } else {
            0.0
        };

        if drawdown >= self.bot_kill_pct {
            return (false, "BOT_KILL drawdown");
        }

        (true, "OK")
    }

    pub fn record_trade(&mut self, pnl_pct: f64, pnl_usdt: f64) {
        self.daily_pnl += pnl_pct;
        self.trade_count_today += 1;
        self.current_balance += pnl_usdt;

        if self.current_balance > self.peak_balance {
            self.peak_balance = self.current_balance;
        }

        if pnl_pct < 0.0 {
            self.streak += 1;
            warn!("손실: {:.2}% | 연패: {}", pnl_pct, self.streak);
        } else {
            self.streak = 0;
            info!("수익: {:.2}% | 연패 리셋", pnl_pct);
        }
    }

    pub fn update_balance(&mut self, balance: f64) {
        self.current_balance = balance;
        if balance > self.peak_balance {
            self.peak_balance = balance;
        }
    }

    pub fn reset_daily(&mut self) {
        self.daily_pnl = 0.0;
        self.trade_count_today = 0;
        self.streak = 0;
    }

    pub fn streak(&self) -> i32 { self.streak }
    pub fn daily_pnl(&self) -> f64 { self.daily_pnl }
    pub fn balance(&self) -> f64 { self.current_balance }
}
