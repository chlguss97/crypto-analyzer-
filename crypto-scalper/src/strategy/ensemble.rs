use crate::models::signal::{Direction, ScalpSignal, SignalType};

/// 앙상블 합의 + CVD Divergence Override
pub struct EnsembleResult {
    pub signal: Option<ScalpSignal>,
    pub blocked: bool,
    pub block_reason: Option<String>,
}

/// 복수 시그널 합의 체크
pub fn check_ensemble(signals: Vec<ScalpSignal>) -> Option<ScalpSignal> {
    match signals.len() {
        0 => None,
        1 => Some(signals.into_iter().next().unwrap()),
        _ => {
            // 방향 일치 확인
            let first_dir = signals[0].direction;
            let all_agree = signals.iter().all(|s| s.direction == first_dir);

            if all_agree {
                // 합의 → 가장 강한 시그널 + 보너스
                let mut best = signals.into_iter()
                    .max_by(|a, b| a.strength.partial_cmp(&b.strength).unwrap())
                    .unwrap();
                best.strength = (best.strength + 0.5).min(3.0);
                best.ensemble_agree = true;
                Some(best)
            } else {
                None // 불일치 → 차단
            }
        }
    }
}

/// CVD Divergence Override (z > 0.3 시 모멘텀 방향 반전)
pub fn apply_cvd_override(
    signal: &mut ScalpSignal,
    delta_div: i8,
    cvd_z: f64,
) -> bool {
    if delta_div == 0 || !matches!(signal.signal_type, SignalType::MicroBurst) {
        return false;
    }

    if cvd_z.abs() <= 0.3 {
        return false;
    }

    let div_dir = if delta_div == 1 { Direction::Long } else { Direction::Short };

    if div_dir != signal.direction {
        signal.direction = div_dir;
        signal.signal_type = SignalType::CvdOverride;
        signal.strength = signal.strength.min(3.0);
        true
    } else {
        false
    }
}
