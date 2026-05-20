use std::collections::VecDeque;

/// Welford Online Z-Score — O(1) memory, sliding window
/// Reference: Welford (1962)
pub struct WelfordZScore {
    window: usize,
    values: VecDeque<f64>,
    mean: f64,
    m2: f64,
    count: usize,
}

impl WelfordZScore {
    pub fn new(window: usize) -> Self {
        Self {
            window,
            values: VecDeque::with_capacity(window),
            mean: 0.0,
            m2: 0.0,
            count: 0,
        }
    }

    /// 새 값 추가 + z-score 반환. 워밍업(10샘플) 전 0.0 반환.
    pub fn update(&mut self, x: f64) -> f64 {
        // 윈도우 초과 시 가장 오래된 값 제거
        if self.count >= self.window {
            let old = self.values[0];
            self.count -= 1;
            let old_mean = self.mean;
            self.mean = if self.count > 0 {
                (old_mean * (self.count + 1) as f64 - old) / self.count as f64
            } else {
                0.0
            };
            self.m2 -= (old - old_mean) * (old - self.mean);
            self.m2 = self.m2.max(0.0); // 부동소수점 보정
            self.values.pop_front();
        }

        // 새 값 추가
        self.values.push_back(x);
        self.count += 1;
        let delta = x - self.mean;
        self.mean += delta / self.count as f64;
        let delta2 = x - self.mean;
        self.m2 += delta * delta2;

        // 워밍업
        if self.count < 10 {
            return 0.0;
        }

        let std = (self.m2 / self.count as f64).sqrt();
        if std < 1e-10 {
            return 0.0;
        }

        let z = (x - self.mean) / std;
        z.clamp(-5.0, 5.0)
    }

    pub fn mean(&self) -> f64 {
        self.mean
    }

    pub fn std(&self) -> f64 {
        if self.count < 2 {
            return 0.0;
        }
        (self.m2 / self.count as f64).sqrt()
    }

    pub fn count(&self) -> usize {
        self.count
    }
}

/// 다수 피처의 z-score 관리자
pub struct FeatureNormalizer {
    normalizers: std::collections::HashMap<String, WelfordZScore>,
    window: usize,
}

impl FeatureNormalizer {
    pub fn new(window: usize) -> Self {
        Self {
            normalizers: std::collections::HashMap::new(),
            window,
        }
    }

    pub fn update(&mut self, name: &str, value: f64) -> f64 {
        let normalizer = self
            .normalizers
            .entry(name.to_string())
            .or_insert_with(|| WelfordZScore::new(self.window));
        normalizer.update(value)
    }

    pub fn warmup_complete(&self) -> bool {
        // 모든 등록된 피처가 10+ 샘플
        !self.normalizers.is_empty()
            && self.normalizers.values().all(|n| n.count() >= 10)
    }

    pub fn total_updates(&self) -> usize {
        self.normalizers.values().map(|n| n.count()).max().unwrap_or(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_welford_basic() {
        let mut w = WelfordZScore::new(100);
        // 워밍업 기간
        for i in 0..10 {
            let z = w.update(i as f64);
            assert_eq!(z, 0.0, "워밍업 중 z=0이어야 함");
        }
        // 워밍업 후 z-score 계산
        let z = w.update(100.0); // 극단값
        assert!(z > 2.0, "극단값은 z > 2여야 함: got {z}");
    }

    #[test]
    fn test_welford_window() {
        let mut w = WelfordZScore::new(20);
        for i in 0..30 {
            w.update(i as f64);
        }
        assert_eq!(w.count(), 20, "윈도우 크기 유지");
    }

    #[test]
    fn test_m2_never_negative() {
        let mut w = WelfordZScore::new(5);
        for _ in 0..100 {
            w.update(1.0); // 같은 값 반복
        }
        assert!(w.m2 >= 0.0, "M2는 음수가 될 수 없음");
    }
}
