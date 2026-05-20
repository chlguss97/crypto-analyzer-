/// Order Flow Imbalance (5-Level)
/// Reference: Cont, Kukanov, & Stoikov (2014)
/// OFI = Σ(ΔBid_k - ΔAsk_k) for k=0..4

pub struct OfiCalculator {
    prev_bids: Vec<f64>,
    prev_asks: Vec<f64>,
    accumulator: f64,
    reset_ts: u64,  // 5초 윈도우 리셋
}

impl OfiCalculator {
    pub fn new() -> Self {
        Self {
            prev_bids: Vec::new(),
            prev_asks: Vec::new(),
            accumulator: 0.0,
            reset_ts: 0,
        }
    }

    /// books5 업데이트 → OFI 반환
    pub fn update(&mut self, bid_sizes: &[f64], ask_sizes: &[f64], now_sec: u64) -> f64 {
        if !self.prev_bids.is_empty() && !self.prev_asks.is_empty() {
            let mut ofi_tick = 0.0;
            let levels = bid_sizes.len().min(ask_sizes.len()).min(self.prev_bids.len()).min(self.prev_asks.len()).min(5);

            for k in 0..levels {
                let delta_bid = bid_sizes[k] - self.prev_bids[k];
                let delta_ask = ask_sizes[k] - self.prev_asks[k];
                ofi_tick += delta_bid - delta_ask;
            }

            // 5초 윈도우 누적
            let period = now_sec / 5;
            if period != self.reset_ts {
                self.reset_ts = period;
                self.accumulator = ofi_tick;
            } else {
                self.accumulator += ofi_tick;
            }
        }

        self.prev_bids = bid_sizes.to_vec();
        self.prev_asks = ask_sizes.to_vec();
        self.accumulator
    }
}
