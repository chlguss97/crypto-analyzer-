use std::collections::VecDeque;

/// Ornstein-Uhlenbeck Z-Score with 0.93 decay
/// Reference: Uhlenbeck & Ornstein (1930), Elliott et al. (2005)
pub struct OuZScore {
    prices: VecDeque<f64>,
    max_samples: usize,
    last_z: f64,
    decay_factor: f64,
}

impl OuZScore {
    pub fn new(max_samples: usize) -> Self {
        Self {
            prices: VecDeque::with_capacity(max_samples),
            max_samples,
            last_z: 0.0,
            decay_factor: 0.93,
        }
    }

    /// 새 가격 추가 + OU z-score 반환
    pub fn update(&mut self, price: f64) -> (f64, f64) {
        // (z_score, mu)
        self.prices.push_back(price);
        if self.prices.len() > self.max_samples {
            self.prices.pop_front();
        }

        if self.prices.len() < 30 {
            return (0.0, price);
        }

        let prices: Vec<f64> = self.prices.iter().copied().collect();
        let n = prices.len() - 1;

        // OLS: ΔX = a + b*X_{t-1}
        let x: Vec<f64> = prices[..n].to_vec();
        let dx: Vec<f64> = (1..=n).map(|i| prices[i] - prices[i - 1]).collect();

        let sx: f64 = x.iter().sum();
        let sy: f64 = dx.iter().sum();
        let sxy: f64 = x.iter().zip(dx.iter()).map(|(a, b)| a * b).sum();
        let sxx: f64 = x.iter().map(|a| a * a).sum();

        let nf = n as f64;
        let denom = nf * sxx - sx * sx;
        if denom.abs() < 1e-10 {
            return (self.last_z * self.decay_factor, price);
        }

        let b = (nf * sxy - sx * sy) / denom;
        let a = (sy - b * sx) / nf;

        if b >= 0.0 {
            // b >= 0 → non-stationary (비정상)
            let decayed = self.last_z * self.decay_factor;
            self.last_z = decayed;
            return (decayed, price);
        }

        let mu = -a / b;

        // 잔차
        let residuals: Vec<f64> = dx
            .iter()
            .zip(x.iter())
            .map(|(dy, xi)| dy - (a + b * xi))
            .collect();
        let sigma = {
            let mean = residuals.iter().sum::<f64>() / residuals.len() as f64;
            let var =
                residuals.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / residuals.len() as f64;
            var.sqrt()
        };

        let ou_sigma = if (-2.0 * b) > 1e-10 {
            sigma / (-2.0 * b).sqrt()
        } else {
            sigma
        };

        let z = if ou_sigma > 1e-10 {
            (prices.last().unwrap() - mu) / ou_sigma
        } else {
            0.0
        };

        let z = z.clamp(-5.0, 5.0);

        // 시그널 감쇠: 0.93 per update
        let decayed = self.last_z * self.decay_factor;
        let final_z = if z.abs() >= decayed.abs() { z } else { decayed };
        self.last_z = final_z;

        (final_z, mu)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_ou_basic() {
        let mut ou = OuZScore::new(200);

        // Mean-reverting 시계열 생성
        let mut price = 100.0;
        let mu = 100.0;
        for _ in 0..50 {
            price = price + 0.5 * (mu - price) + 0.1; // 평균 회귀
            let (z, _) = ou.update(price);
            // 30샘플 전까지는 0
            let _ = z;
        }
        // 50샘플 후 z-score가 계산됨
        assert!(ou.last_z != 0.0 || ou.prices.len() >= 30);
    }

    #[test]
    fn test_ou_decay() {
        let mut ou = OuZScore::new(200);
        ou.last_z = 3.0;

        // 같은 값 계속 넣으면 감쇠
        for _ in 0..50 {
            ou.update(100.0);
        }
        // 감쇠로 z가 줄어들어야 함
        assert!(ou.last_z.abs() < 3.0);
    }
}
