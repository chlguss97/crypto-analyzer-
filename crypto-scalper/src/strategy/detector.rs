use crate::config::ScalpConfig;
use crate::features::welford::FeatureNormalizer;
use crate::models::features::FeatureSet;
use crate::models::signal::*;
use crate::strategy::regime::{RegimeGate, RegimeResult};
use crate::strategy::ensemble;
use tracing::info;

/// ScalpDetector — 4계층 파이프라인 (이벤트 드리븐, ~1ms)
pub struct ScalpDetector {
    config: ScalpConfig,
    normalizer: FeatureNormalizer,
    regime_gate: RegimeGate,
    warmup_count: usize,
    warmup_threshold: usize,
}

impl ScalpDetector {
    pub fn new(config: &ScalpConfig) -> Self {
        Self {
            config: config.clone(),
            normalizer: FeatureNormalizer::new(100),
            regime_gate: RegimeGate::new(
                config.hurst_momentum,
                config.hurst_mean_revert,
                config.vpin_extreme,
            ),
            warmup_count: 0,
            warmup_threshold: 100,
        }
    }

    /// 피처 → 시그널 (None = 진입 안 함)
    pub fn evaluate(&mut self, features: &FeatureSet) -> Option<ScalpSignal> {
        self.warmup_count += 1;

        // 1. 워밍업
        if self.warmup_count < self.warmup_threshold {
            return None;
        }

        // 2. Staleness
        let now_ms = chrono::Utc::now().timestamp_millis();
        if features.velocity_ts > 0 && (now_ms - features.velocity_ts) > self.config.stale_threshold_ms as i64 {
            return None;
        }

        // 3. Spread (bps)
        let price = features.price;
        if price <= 0.0 { return None; }
        let spread_bps = features.spread / price * 10000.0;
        if spread_bps > 3.0 { return None; }

        // 4. Book Shock
        if features.depth_shock { return None; }

        // 5. Regime Gate
        let regime_result = self.regime_gate.evaluate(
            features.hurst, features.hurst_available, features.vpin
        );
        if regime_result.blocked { return None; }

        // 6. Micro Confidence
        let micro_conf = calc_micro_confidence(features);
        if micro_conf < self.config.min_micro_confidence { return None; }

        // 7. Z-Score 정규화
        let z_ofi = self.normalizer.update("ofi", features.ofi);
        let z_imbal = self.normalizer.update("book_imbalance", features.book_imbalance);
        let z_burst = self.normalizer.update("trade_burst", features.trade_burst);
        let z_bs5 = self.normalizer.update("bs_ratio_5s", features.bs_ratio_5s);
        let z_bs30 = self.normalizer.update("bs_ratio_30s", features.bs_ratio_30s);
        let z_mom = self.normalizer.update("momentum_quality", features.momentum_quality);
        let z_accel = self.normalizer.update("delta_accel", features.delta_accel);
        let z_cvd = self.normalizer.update("cvd_5m", features.cvd_5m);
        let z_move10 = self.normalizer.update("move_10s", features.move_10s);
        let z_move30 = self.normalizer.update("move_30s", features.move_30s);
        let z_spread = self.normalizer.update("spread_bps", spread_bps);
        let z_abs = self.normalizer.update("absorption", features.absorption_score);

        // 8. Signal Check (앙상블)
        let regime = regime_result.regime;
        let mut signals = Vec::new();

        // Signal A: Burst
        if matches!(regime, Regime::Momentum | Regime::Both | Regime::Neutral | Regime::DeadZone) {
            if let Some(sig) = self.check_burst(features, z_move10, z_move30, z_ofi, z_bs5) {
                signals.push(sig);
            }
        }

        // Signal B: OU Reversion
        if matches!(regime, Regime::MeanRevert | Regime::Both | Regime::Neutral | Regime::DeadZone) {
            if let Some(sig) = self.check_ou(features, z_imbal, z_spread, z_abs) {
                signals.push(sig);
            }
        }

        // 앙상블 합의
        let mut signal = ensemble::check_ensemble(signals)?;

        // CVD Override
        ensemble::apply_cvd_override(&mut signal, features.delta_div, z_cvd);

        // 사이징 배수
        let combined = regime_result.vpin_size_mult * regime_result.hurst_size_mult * micro_conf;
        signal.price = price;
        signal.regime = regime;
        signal.hurst = features.hurst;
        signal.vpin = features.vpin;
        signal.micro_confidence = micro_conf;
        signal.vpin_size_mult = regime_result.vpin_size_mult;
        signal.hurst_size_mult = regime_result.hurst_size_mult;
        signal.combined_size_mult = combined;

        // ML features JSON
        signal.features_json = serde_json::json!({
            "z_ofi": z_ofi, "z_book_imbalance": z_imbal,
            "z_trade_burst": z_burst, "z_bs_ratio_5s": z_bs5,
            "z_bs_ratio_30s": z_bs30, "z_momentum_quality": z_mom,
            "z_delta_accel": z_accel, "z_cvd_5m": z_cvd,
            "spread": features.spread, "vwap_deviation": features.vwap_deviation,
            "delta_div": features.delta_div, "absorption_score": features.absorption_score,
            "whale_bias": features.whale_bias, "price_impact": features.price_impact,
            "hurst": features.hurst, "vpin": features.vpin,
            "parkinson_vol": features.parkinson_vol, "micro_confidence": micro_conf,
            "cvd_5m_raw": features.cvd_5m, "funding_rate": features.funding_rate,
        }).to_string();

        Some(signal)
    }

    fn check_burst(&self, f: &FeatureSet, z10: f64, z30: f64, z_ofi: f64, z_bs5: f64) -> Option<ScalpSignal> {
        // 방향
        let direction = if f.move_10s > 0.0 && f.move_30s > 0.0 {
            Direction::Long
        } else if f.move_10s < 0.0 && f.move_30s < 0.0 {
            Direction::Short
        } else {
            return None;
        };

        // z-score 속도
        if z10.abs() < 1.5 || z30.abs() < 1.0 { return None; }

        // 신선도
        let abs10 = f.move_10s.abs();
        let abs30 = f.move_30s.abs();
        if abs30 > 0.0 && abs10 / abs30 < self.config.burst_freshness { return None; }

        // OFI
        match direction {
            Direction::Long if z_ofi < self.config.burst_min_ofi_z => return None,
            Direction::Short if z_ofi > -self.config.burst_min_ofi_z => return None,
            _ => {}
        }

        // Burst
        if f.trade_burst < self.config.burst_min_trade_burst { return None; }

        // BS ratio z-score
        match direction {
            Direction::Long if z_bs5 < 0.5 => return None,
            Direction::Short if z_bs5 > -0.5 => return None,
            _ => {}
        }

        // 소진 필터
        if abs30 > 0.0 && f.move_60s.abs() > abs30 * 3.0 { return None; }

        // Strength
        let mut strength = 1.0;
        if (direction == Direction::Long && f.cvd_5m > 0.0) ||
           (direction == Direction::Short && f.cvd_5m < 0.0) { strength += 0.5; }
        if (direction == Direction::Long && f.whale_bias > 0.2) ||
           (direction == Direction::Short && f.whale_bias < -0.2) { strength += 0.3; }

        Some(ScalpSignal {
            signal_type: SignalType::MicroBurst,
            direction,
            strength,
            price: 0.0,
            regime: Regime::Both,
            hurst: 0.0,
            vpin: 0.0,
            micro_confidence: 0.0,
            vpin_size_mult: 1.0,
            hurst_size_mult: 1.0,
            combined_size_mult: 1.0,
            features_json: String::new(),
            signal_id: None,
            ensemble_agree: false,
        })
    }

    fn check_ou(&self, f: &FeatureSet, z_imbal: f64, z_spread: f64, z_abs: f64) -> Option<ScalpSignal> {
        if f.ou_zscore.abs() < self.config.ou_entry_z { return None; }

        let direction = if f.ou_zscore < 0.0 { Direction::Long } else { Direction::Short };

        // Book imbalance z-score
        match direction {
            Direction::Long if z_imbal < 0.3 => return None,
            Direction::Short if z_imbal > -0.3 => return None,
            _ => {}
        }

        // Spread z-score
        if z_spread > 2.0 { return None; }

        let mut strength = 1.0;
        if f.ou_zscore.abs() > 3.0 { strength += 0.5; }
        if (direction == Direction::Long && f.delta_div == 1) ||
           (direction == Direction::Short && f.delta_div == -1) { strength += 0.3; }
        if z_abs.abs() > 1.0 && f.absorption_dir == direction.as_str() { strength += 0.3; }

        Some(ScalpSignal {
            signal_type: SignalType::OuReversion,
            direction,
            strength,
            price: 0.0,
            regime: Regime::Both,
            hurst: 0.0,
            vpin: 0.0,
            micro_confidence: 0.0,
            vpin_size_mult: 1.0,
            hurst_size_mult: 1.0,
            combined_size_mult: 1.0,
            features_json: String::new(),
            signal_id: None,
            ensemble_agree: false,
        })
    }
}

fn calc_micro_confidence(f: &FeatureSet) -> f64 {
    let s = if f.spread < 0.5 { 1.0 } else if f.spread < 2.0 { 0.7 } else { 0.2 };
    let d = if f.book_imbalance.abs() < 0.3 { 1.0 } else if f.book_imbalance.abs() < 0.7 { 0.6 } else { 0.3 };
    let a = if f.trade_burst > 1.5 { 1.0 } else if f.trade_burst > 0.7 { 0.6 } else { 0.2 };
    s * 0.4 + d * 0.3 + a * 0.3
}
