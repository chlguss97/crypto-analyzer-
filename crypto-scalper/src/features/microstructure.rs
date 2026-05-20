use std::collections::VecDeque;

/// 15종 마이크로스트럭처 피처 계산
/// OKX trades 데이터에서 2초마다 계산

pub struct TradeRecord {
    pub ts: f64,
    pub side: String,   // "buy" | "sell"
    pub qty: f64,
    pub price: f64,
    pub size_usd: f64,
}

pub struct MicrostructureEngine {
    trades: VecDeque<TradeRecord>,
    max_trades: usize,
    price_history: VecDeque<(f64, f64)>,  // (ts, price)
    cvd_snapshots: VecDeque<(f64, f64)>,  // (ts, cvd)
    last_cvd_snap: f64,
    vwap_vol_sum: f64,
    vwap_qty_sum: f64,
    vwap_reset: u64,
    cvd_5m: f64,
}

impl MicrostructureEngine {
    pub fn new() -> Self {
        Self {
            trades: VecDeque::with_capacity(50000),
            max_trades: 50000,
            price_history: VecDeque::with_capacity(60),
            cvd_snapshots: VecDeque::with_capacity(30),
            last_cvd_snap: 0.0,
            vwap_vol_sum: 0.0,
            vwap_qty_sum: 0.0,
            vwap_reset: 0,
            cvd_5m: 0.0,
        }
    }

    pub fn add_trade(&mut self, ts: f64, side: &str, qty: f64, price: f64) {
        let size_usd = price * qty;
        let delta = if side == "buy" { qty } else { -qty };

        self.cvd_5m += delta;

        self.trades.push_back(TradeRecord {
            ts,
            side: side.to_string(),
            qty,
            price,
            size_usd,
        });
        if self.trades.len() > self.max_trades {
            self.trades.pop_front();
        }

        self.price_history.push_back((ts, price));
        if self.price_history.len() > 60 {
            self.price_history.pop_front();
        }

        // VWAP
        let now_sec = ts as u64;
        if now_sec / 300 != self.vwap_reset {
            self.vwap_reset = now_sec / 300;
            self.vwap_vol_sum = 0.0;
            self.vwap_qty_sum = 0.0;
        }
        self.vwap_vol_sum += price * qty;
        self.vwap_qty_sum += qty;

        // CVD 스냅샷 (5초)
        if ts - self.last_cvd_snap >= 5.0 {
            self.cvd_snapshots.push_back((ts, self.cvd_5m));
            if self.cvd_snapshots.len() > 30 {
                self.cvd_snapshots.pop_front();
            }
            self.last_cvd_snap = ts;
        }
    }

    /// 15종 피처 일괄 계산
    pub fn compute(&self, current_price: f64) -> MicroFeatures {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs_f64();

        // 1. Trade Rate (10s)
        let cutoff_10s = now - 10.0;
        let trades_10s: Vec<&TradeRecord> = self.trades.iter().filter(|t| t.ts >= cutoff_10s).collect();
        let trade_rate = trades_10s.len() as f64 / 10.0;

        // 2. Trade Burst
        let cutoff_60s = now - 60.0;
        let trades_60s: Vec<&TradeRecord> = self.trades.iter().filter(|t| t.ts >= cutoff_60s).collect();
        let rate_60s = if !trades_60s.is_empty() { trades_60s.len() as f64 / 60.0 } else { 1.0 };
        let burst = trade_rate / rate_60s.max(0.1);

        // 3. Buy/Sell Ratio (5s/30s/60s)
        let bs_5s = self.calc_bs_ratio(now - 5.0);
        let bs_30s = self.calc_bs_ratio(now - 30.0);
        let bs_60s = self.calc_bs_ratio(now - 60.0);

        // 4. Absorption
        let (absorption_score, absorption_dir) = self.calc_absorption(now, current_price);

        // 5. Delta Acceleration
        let delta_accel = self.calc_delta_accel();

        // 6. Price Impact
        let price_impact = self.calc_price_impact(now, &trades_60s);

        // 7. VWAP
        let vwap = if self.vwap_qty_sum > 0.0 { self.vwap_vol_sum / self.vwap_qty_sum } else { current_price };
        let vwap_dev = if vwap > 0.0 { (current_price - vwap) / vwap * 100.0 } else { 0.0 };

        // 8. Delta Divergence
        let delta_div = self.calc_delta_div(current_price);

        // 9. Momentum Quality
        let mom_quality = self.calc_momentum_quality(burst, bs_30s);

        MicroFeatures {
            trade_rate,
            trade_burst: burst,
            bs_ratio_5s: bs_5s,
            bs_ratio_30s: bs_30s,
            bs_ratio_60s: bs_60s,
            absorption_score,
            absorption_dir,
            delta_accel,
            price_impact,
            vwap,
            vwap_deviation: vwap_dev,
            delta_div,
            momentum_quality: mom_quality,
        }
    }

    fn calc_bs_ratio(&self, cutoff: f64) -> f64 {
        let trades: Vec<&TradeRecord> = self.trades.iter().filter(|t| t.ts >= cutoff).collect();
        let buy_vol: f64 = trades.iter().filter(|t| t.side == "buy").map(|t| t.qty).sum();
        let sell_vol: f64 = trades.iter().filter(|t| t.side == "sell").map(|t| t.qty).sum();
        let total = buy_vol + sell_vol;
        if total > 0.0 { buy_vol / total } else { 0.5 }
    }

    fn calc_absorption(&self, now: f64, current_price: f64) -> (f64, String) {
        let cutoff = now - 30.0;
        let trades: Vec<&TradeRecord> = self.trades.iter().filter(|t| t.ts >= cutoff).collect();
        let buy_vol: f64 = trades.iter().filter(|t| t.side == "buy").map(|t| t.qty).sum();
        let sell_vol: f64 = trades.iter().filter(|t| t.side == "sell").map(|t| t.qty).sum();

        let price_30s_ago = self.price_history.iter()
            .find(|(t, _)| *t >= cutoff)
            .map(|(_, p)| *p)
            .unwrap_or(current_price);

        let price_change = (current_price - price_30s_ago).abs();
        let atr_proxy = current_price * 0.002;

        if sell_vol > buy_vol * 1.5 && price_change < atr_proxy * 0.3 {
            let score = (sell_vol / (price_change + 0.01).max(1.0) * 0.001).min(5.0);
            (score, "long".to_string())
        } else if buy_vol > sell_vol * 1.5 && price_change < atr_proxy * 0.3 {
            let score = (buy_vol / (price_change + 0.01).max(1.0) * 0.001).min(5.0);
            (score, "short".to_string())
        } else {
            (0.0, "neutral".to_string())
        }
    }

    fn calc_delta_accel(&self) -> f64 {
        if self.cvd_snapshots.len() < 4 {
            return 0.0;
        }
        let snaps: Vec<&(f64, f64)> = self.cvd_snapshots.iter().rev().take(6).collect();
        if snaps.len() < 3 {
            return 0.0;
        }
        let deltas: Vec<f64> = (0..snaps.len() - 1)
            .map(|i| snaps[i].1 - snaps[i + 1].1)
            .collect();
        if deltas.len() < 2 {
            return 0.0;
        }
        let accel = deltas[0] - deltas[deltas.len() - 1];
        (accel / (deltas.last().unwrap().abs() + 1.0).max(1.0)).clamp(-2.0, 2.0)
    }

    fn calc_price_impact(&self, now: f64, trades_60s: &[&TradeRecord]) -> f64 {
        let total_vol: f64 = trades_60s.iter().map(|t| t.size_usd).sum();
        let prices: Vec<f64> = trades_60s.iter().map(|t| t.price).collect();
        if prices.len() >= 2 && total_vol > 0.0 {
            let range = prices.iter().cloned().fold(f64::NEG_INFINITY, f64::max)
                - prices.iter().cloned().fold(f64::INFINITY, f64::min);
            (range / (total_vol / 1_000_000.0)).min(500.0)
        } else {
            0.0
        }
    }

    fn calc_delta_div(&self, current_price: f64) -> i8 {
        let cutoff_60s = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs_f64() - 60.0;
        let prices: Vec<f64> = self.trades.iter()
            .filter(|t| t.ts >= cutoff_60s)
            .map(|t| t.price)
            .collect();

        if prices.len() < 10 || self.cvd_snapshots.len() < 3 {
            return 0;
        }

        let price_high = prices.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
        let price_low = prices.iter().cloned().fold(f64::INFINITY, f64::min);
        let is_price_high = current_price >= price_high * 0.999;
        let is_price_low = current_price <= price_low * 1.001;

        let cvd_vals: Vec<f64> = self.cvd_snapshots.iter().rev().take(12).map(|(_, v)| *v).collect();
        if cvd_vals.is_empty() {
            return 0;
        }
        let cvd_max = cvd_vals.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
        let cvd_min = cvd_vals.iter().cloned().fold(f64::INFINITY, f64::min);

        let is_cvd_high = if cvd_max > 0.0 { self.cvd_5m >= cvd_max * 0.95 } else { self.cvd_5m >= cvd_max };
        let is_cvd_low = if cvd_min < 0.0 { self.cvd_5m <= cvd_min * 0.95 } else { self.cvd_5m <= cvd_min };

        if is_price_high && !is_cvd_high {
            -1 // 약세 다이버전스
        } else if is_price_low && !is_cvd_low {
            1 // 강세 다이버전스
        } else {
            0
        }
    }

    fn calc_momentum_quality(&self, burst: f64, bs_30s: f64) -> f64 {
        let delta_alignment = if bs_30s > 0.6 {
            if self.cvd_5m > 0.0 { 1.5 } else { 0.5 }
        } else if bs_30s < 0.4 {
            if self.cvd_5m < 0.0 { 1.5 } else { 0.5 }
        } else {
            1.0
        };

        let quality = burst * ((bs_30s - 0.5).abs() * 4.0) * delta_alignment;
        quality.min(5.0)
    }
}

#[derive(Debug, Clone)]
pub struct MicroFeatures {
    pub trade_rate: f64,
    pub trade_burst: f64,
    pub bs_ratio_5s: f64,
    pub bs_ratio_30s: f64,
    pub bs_ratio_60s: f64,
    pub absorption_score: f64,
    pub absorption_dir: String,
    pub delta_accel: f64,
    pub price_impact: f64,
    pub vwap: f64,
    pub vwap_deviation: f64,
    pub delta_div: i8,
    pub momentum_quality: f64,
}
