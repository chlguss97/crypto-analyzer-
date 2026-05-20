use crate::models::signal::Regime;

/// Regime Gate — Hurst + VPIN 기반 시장 상태 판단
pub struct RegimeGate {
    pub hurst_momentum: f64,
    pub hurst_mean_revert: f64,
    pub vpin_extreme: f64,
}

pub struct RegimeResult {
    pub regime: Regime,
    pub vpin_size_mult: f64,
    pub hurst_size_mult: f64,
    pub blocked: bool,
    pub block_reason: Option<String>,
}

impl RegimeGate {
    pub fn new(hurst_momentum: f64, hurst_mean_revert: f64, vpin_extreme: f64) -> Self {
        Self { hurst_momentum, hurst_mean_revert, vpin_extreme }
    }

    pub fn evaluate(&self, hurst: f64, hurst_available: bool, vpin: f64) -> RegimeResult {
        // VPIN 4단계 (BTC 보정)
        let (vpin_size_mult, vpin_blocked) = if vpin >= self.vpin_extreme {
            (0.0, true)
        } else if vpin >= 0.6 {
            (0.25, false)
        } else if vpin >= 0.4 {
            (0.5, false)
        } else {
            (1.0, false)
        };

        if vpin_blocked {
            return RegimeResult {
                regime: Regime::Both,
                vpin_size_mult: 0.0,
                hurst_size_mult: 1.0,
                blocked: true,
                block_reason: Some(format!("VPIN extreme: {:.4}", vpin)),
            };
        }

        // Hurst Regime
        let (regime, hurst_size_mult) = if !hurst_available {
            (Regime::Both, 1.0)
        } else if hurst > self.hurst_momentum {
            (Regime::Momentum, 1.0)
        } else if hurst < self.hurst_mean_revert {
            (Regime::MeanRevert, 1.0)
        } else if hurst >= 0.45 && hurst <= 0.55 {
            (Regime::DeadZone, 0.25)
        } else {
            (Regime::Neutral, 0.5)
        };

        RegimeResult {
            regime,
            vpin_size_mult,
            hurst_size_mult,
            blocked: false,
            block_reason: None,
        }
    }
}
