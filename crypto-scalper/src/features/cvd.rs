/// Cumulative Volume Delta (CVD) — 5m/15m/1h 윈도우
/// Binance aggTrade 기반 (OKX 대비 3~5배 거래량)
pub struct CvdTracker {
    pub cvd_5m: f64,
    pub cvd_15m: f64,
    pub cvd_1h: f64,
    reset_5m: u64,
    reset_15m: u64,
    reset_1h: u64,
}

const MAX_CVD: f64 = 1e9;

impl CvdTracker {
    pub fn new() -> Self {
        Self {
            cvd_5m: 0.0,
            cvd_15m: 0.0,
            cvd_1h: 0.0,
            reset_5m: 0,
            reset_15m: 0,
            reset_1h: 0,
        }
    }

    /// 체결 데이터 → CVD 업데이트
    /// delta: +qty (taker buy), -qty (taker sell)
    pub fn update(&mut self, delta: f64, now_sec: u64) {
        // 윈도우 리셋
        let period_5m = now_sec / 300;
        let period_15m = now_sec / 900;
        let period_1h = now_sec / 3600;

        if period_5m != self.reset_5m {
            self.reset_5m = period_5m;
            self.cvd_5m = delta;
        } else {
            self.cvd_5m = (self.cvd_5m + delta).clamp(-MAX_CVD, MAX_CVD);
        }

        if period_15m != self.reset_15m {
            self.reset_15m = period_15m;
            self.cvd_15m = delta;
        } else {
            self.cvd_15m = (self.cvd_15m + delta).clamp(-MAX_CVD, MAX_CVD);
        }

        if period_1h != self.reset_1h {
            self.reset_1h = period_1h;
            self.cvd_1h = delta;
        } else {
            self.cvd_1h = (self.cvd_1h + delta).clamp(-MAX_CVD, MAX_CVD);
        }
    }
}
