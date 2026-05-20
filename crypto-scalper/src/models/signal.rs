use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum SignalType {
    MicroBurst,
    OuReversion,
    CvdOverride,
}

impl SignalType {
    pub fn as_str(&self) -> &str {
        match self {
            SignalType::MicroBurst => "micro_burst",
            SignalType::OuReversion => "ou_reversion",
            SignalType::CvdOverride => "cvd_override",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub enum Direction {
    Long,
    Short,
}

impl Direction {
    pub fn as_str(&self) -> &str {
        match self {
            Direction::Long => "long",
            Direction::Short => "short",
        }
    }

    pub fn opposite(&self) -> Self {
        match self {
            Direction::Long => Direction::Short,
            Direction::Short => Direction::Long,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub enum Regime {
    Momentum,
    MeanRevert,
    Neutral,
    DeadZone,
    Both,
}

impl Regime {
    pub fn as_str(&self) -> &str {
        match self {
            Regime::Momentum => "momentum",
            Regime::MeanRevert => "mean_revert",
            Regime::Neutral => "neutral",
            Regime::DeadZone => "dead_zone",
            Regime::Both => "both",
        }
    }
}

/// 시그널 감지 결과
#[derive(Debug, Clone, Serialize)]
pub struct ScalpSignal {
    pub signal_type: SignalType,
    pub direction: Direction,
    pub strength: f64,
    pub price: f64,
    pub regime: Regime,
    pub hurst: f64,
    pub vpin: f64,
    pub micro_confidence: f64,
    pub vpin_size_mult: f64,
    pub hurst_size_mult: f64,
    pub combined_size_mult: f64,
    pub features_json: String,
    pub signal_id: Option<i64>,
    pub ensemble_agree: bool,
}
