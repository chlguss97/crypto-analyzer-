use serde::{Deserialize, Serialize};

/// 20종 마이크로스트럭처 피처 — 4계층 파이프라인 전체에서 사용
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct FeatureSet {
    // 가격/속도 (OKX trades)
    pub price: f64,
    pub velocity_ts: i64,  // ms timestamp
    pub move_10s: f64,
    pub move_30s: f64,
    pub move_60s: f64,
    pub range_60s: f64,

    // 호가 (OKX books5)
    pub book_imbalance: f64,    // -1 ~ +1
    pub spread: f64,            // USD
    pub ofi: f64,               // Order Flow Imbalance
    pub book_resilience: f64,   // EWMA depth ratio
    pub depth_shock: bool,

    // 체결 (OKX trades → microstructure)
    pub trade_rate: f64,
    pub trade_burst: f64,
    pub bs_ratio_5s: f64,
    pub bs_ratio_30s: f64,
    pub bs_ratio_60s: f64,
    pub momentum_quality: f64,
    pub delta_accel: f64,
    pub price_impact: f64,

    // VWAP (OKX trades)
    pub vwap: f64,
    pub vwap_deviation: f64,

    // CVD (Binance aggTrade)
    pub cvd_5m: f64,
    pub cvd_15m: f64,
    pub cvd_1h: f64,
    pub delta_div: i8,          // -1, 0, +1

    // 흡수 (OKX trades)
    pub absorption_score: f64,
    pub absorption_dir: String,

    // 고래 (Binance aggTrade)
    pub whale_bias: f64,
    pub whale_cluster_score: f64,
    pub whale_cluster_dir: String,

    // 레짐 (5분봉 기반)
    pub hurst: f64,
    pub hurst_available: bool,
    pub parkinson_vol: f64,     // Parkinson + Realized 50/50

    // VPIN (Binance aggTrade)
    pub vpin: f64,

    // 외부 (Binance REST)
    pub funding_rate: f64,
    pub open_interest: f64,

    // OU Z-Score
    pub ou_zscore: f64,
    pub ou_mu: f64,
}

/// ML 입력용 20종 정규화 피처
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct NormalizedFeatures {
    // z-score 정규화
    pub z_ofi: f64,
    pub z_book_imbalance: f64,
    pub z_trade_burst: f64,
    pub z_bs_ratio_5s: f64,
    pub z_bs_ratio_30s: f64,
    pub z_momentum_quality: f64,
    pub z_delta_accel: f64,
    pub z_cvd_5m: f64,
    pub z_move_10s: f64,
    pub z_move_30s: f64,
    pub z_spread_bps: f64,
    pub z_absorption: f64,

    // 원시값 (정규화 불필요)
    pub spread: f64,
    pub vwap_deviation: f64,
    pub delta_div: f64,
    pub absorption_score: f64,
    pub whale_bias: f64,
    pub price_impact: f64,
    pub hurst: f64,
    pub vpin: f64,
    pub parkinson_vol: f64,
    pub micro_confidence: f64,
    pub cvd_5m_raw: f64,
    pub funding_rate: f64,
}

impl NormalizedFeatures {
    /// ML 입력 벡터 (20종, 순서 고정)
    pub fn to_vec(&self) -> Vec<f64> {
        vec![
            self.z_ofi,
            self.z_book_imbalance,
            self.z_trade_burst,
            self.z_bs_ratio_5s,
            self.z_bs_ratio_30s,
            self.z_momentum_quality,
            self.z_delta_accel,
            self.z_cvd_5m,
            self.spread,
            self.vwap_deviation,
            self.delta_div,
            self.absorption_score,
            self.whale_bias,
            self.price_impact,
            self.hurst,
            self.vpin,
            self.parkinson_vol,
            self.micro_confidence,
            self.cvd_5m_raw,
            self.funding_rate,
        ]
    }
}
