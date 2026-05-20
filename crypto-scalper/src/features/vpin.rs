use std::collections::VecDeque;

/// VPIN — Volume-Synchronized Probability of Informed Trading
/// Reference: Easley, López de Prado, & O'Hara (2012)
/// Trade-count based buckets (200 trades/bucket)
pub struct VpinCalculator {
    bucket_trades: usize,
    buckets: VecDeque<f64>,
    max_buckets: usize,
    current_buy: f64,
    current_sell: f64,
    current_count: usize,
}

impl VpinCalculator {
    pub fn new(bucket_trades: usize, max_buckets: usize) -> Self {
        Self {
            bucket_trades,
            buckets: VecDeque::with_capacity(max_buckets),
            max_buckets,
            current_buy: 0.0,
            current_sell: 0.0,
            current_count: 0,
        }
    }

    /// 거래 추가 → VPIN 반환 (None = 버킷 부족)
    pub fn update(&mut self, qty: f64, is_buyer_maker: bool) -> Option<f64> {
        // Lee-Ready: m=true → taker sell, m=false → taker buy
        if is_buyer_maker {
            self.current_sell += qty;
        } else {
            self.current_buy += qty;
        }
        self.current_count += 1;

        // 버킷 완성
        if self.current_count >= self.bucket_trades {
            let total = self.current_buy + self.current_sell;
            if total > 0.0 {
                let imbal = (self.current_buy - self.current_sell).abs() / total;
                self.buckets.push_back(imbal);
                if self.buckets.len() > self.max_buckets {
                    self.buckets.pop_front();
                }
            }
            self.current_buy = 0.0;
            self.current_sell = 0.0;
            self.current_count = 0;
        }

        // VPIN = mean(buckets)
        if self.buckets.len() >= 10 {
            let sum: f64 = self.buckets.iter().sum();
            Some(sum / self.buckets.len() as f64)
        } else {
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_vpin_balanced() {
        let mut vpin = VpinCalculator::new(10, 50);
        // 매수/매도 균형 → VPIN 낮음
        for i in 0..500 {
            let is_sell = i % 2 == 0;
            vpin.update(0.01, is_sell);
        }
        if let Some(v) = vpin.update(0.01, false) {
            assert!(v < 0.3, "균형 시장 VPIN < 0.3: {v}");
        }
    }

    #[test]
    fn test_vpin_imbalanced() {
        let mut vpin = VpinCalculator::new(10, 50);
        // 매수만 → VPIN 높음
        for _ in 0..500 {
            vpin.update(0.01, false); // all buys
        }
        if let Some(v) = vpin.update(0.01, false) {
            assert!(v > 0.8, "편향 시장 VPIN > 0.8: {v}");
        }
    }
}
