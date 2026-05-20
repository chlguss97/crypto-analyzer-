use serde::Deserialize;
use std::fs;
use std::path::Path;

#[derive(Debug, Clone, Deserialize)]
pub struct Config {
    pub exchange: ExchangeConfig,
    pub scalp: ScalpConfig,
    pub risk: RiskConfig,
    pub fees: FeeConfig,
    pub ml: MlConfig,
    pub telegram: TelegramConfig,
    pub redis: RedisConfig,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ExchangeConfig {
    pub name: String,
    pub symbol: String,
    pub margin_mode: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ScalpConfig {
    pub shadow_mode: bool,
    pub leverage: u32,
    pub margin_pct: f64,
    pub tp_price_pct: f64,
    pub sl_price_pct: f64,
    pub sl_k_vol: f64,
    pub tp_k_vol: f64,
    pub ou_entry_z: f64,
    // Regime gates
    pub hurst_momentum: f64,
    pub hurst_mean_revert: f64,
    pub vpin_extreme: f64,
    // Burst signal
    pub burst_freshness: f64,
    pub burst_min_ofi_z: f64,
    pub burst_min_trade_burst: f64,
    // Common filters
    pub min_micro_confidence: f64,
    pub stale_threshold_ms: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct RiskConfig {
    pub margin_pct: f64,
    pub max_positions: u32,
    pub bot_kill_drawdown: f64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct FeeConfig {
    pub taker: f64,
    pub maker: f64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct MlConfig {
    pub phase_a_min_samples: usize,
    pub retrain_interval: usize,
    pub window_size: usize,
    pub min_oos_accuracy: f64,
    pub go_threshold: f64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct TelegramConfig {
    pub enabled: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct RedisConfig {
    pub host: String,
    pub port: u16,
    pub db: u8,
}

impl Config {
    pub fn load(path: &str) -> anyhow::Result<Self> {
        let content = fs::read_to_string(Path::new(path))?;
        let config: Config = serde_yaml::from_str(&content)?;
        Ok(config)
    }
}
