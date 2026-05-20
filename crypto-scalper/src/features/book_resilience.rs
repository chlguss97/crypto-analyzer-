/// Book Resilience — EWMA depth tracking + shock detection
/// Reference: Kyle (1985), Foucault et al. (2013)

pub struct BookResilience {
    ewma: f64,
    alpha: f64,
    initialized: bool,
}

impl BookResilience {
    pub fn new(alpha: f64) -> Self {
        Self {
            ewma: 0.0,
            alpha,
            initialized: false,
        }
    }

    /// 호가 깊이 업데이트 → (resilience, depth_shock)
    pub fn update(&mut self, total_depth: f64) -> (f64, bool) {
        if !self.initialized {
            self.ewma = total_depth;
            self.initialized = true;
            return (1.0, false);
        }

        self.ewma = self.alpha * total_depth + (1.0 - self.alpha) * self.ewma;

        let resilience = if self.ewma > 1e-10 {
            (total_depth / self.ewma).min(2.0) / 2.0
        } else {
            1.0
        };

        let depth_shock = total_depth < self.ewma * 0.7; // 30% 급감

        (resilience, depth_shock)
    }
}
