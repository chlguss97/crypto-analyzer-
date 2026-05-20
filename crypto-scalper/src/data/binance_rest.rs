use reqwest::Client;
use serde_json::Value;
use tracing::{debug, warn};

const FAPI: &str = "https://fapi.binance.com";

pub struct BinanceRest {
    client: Client,
}

#[derive(Debug, Clone)]
pub struct LiquidationData {
    pub total: f64,
    pub long_liq: f64,
    pub short_liq: f64,
}

impl BinanceRest {
    pub fn new() -> Self {
        Self {
            client: Client::builder()
                .timeout(std::time::Duration::from_secs(5))
                .build()
                .unwrap(),
        }
    }

    /// 최근 1분 청산 데이터
    pub async fn fetch_liquidations(&self) -> anyhow::Result<LiquidationData> {
        let resp = self.client
            .get(format!("{}/fapi/v1/allForceOrders", FAPI))
            .query(&[("symbol", "BTCUSDT"), ("limit", "20")])
            .send()
            .await?
            .json::<Vec<Value>>()
            .await?;

        let cutoff = chrono::Utc::now().timestamp_millis() - 60_000;
        let recent: Vec<&Value> = resp.iter()
            .filter(|o| o["time"].as_i64().unwrap_or(0) > cutoff)
            .collect();

        let long_liq: f64 = recent.iter()
            .filter(|o| o["side"].as_str() == Some("SELL"))
            .map(|o| {
                let p: f64 = o["price"].as_str().unwrap_or("0").parse().unwrap_or(0.0);
                let q: f64 = o["origQty"].as_str().unwrap_or("0").parse().unwrap_or(0.0);
                p * q
            })
            .sum();

        let short_liq: f64 = recent.iter()
            .filter(|o| o["side"].as_str() == Some("BUY"))
            .map(|o| {
                let p: f64 = o["price"].as_str().unwrap_or("0").parse().unwrap_or(0.0);
                let q: f64 = o["origQty"].as_str().unwrap_or("0").parse().unwrap_or(0.0);
                p * q
            })
            .sum();

        Ok(LiquidationData {
            total: long_liq + short_liq,
            long_liq,
            short_liq,
        })
    }

    /// 펀딩레이트
    pub async fn fetch_funding_rate(&self) -> anyhow::Result<f64> {
        let resp = self.client
            .get(format!("{}/fapi/v1/premiumIndex", FAPI))
            .query(&[("symbol", "BTCUSDT")])
            .send()
            .await?
            .json::<Value>()
            .await?;

        let rate: f64 = resp["lastFundingRate"]
            .as_str()
            .unwrap_or("0")
            .parse()
            .unwrap_or(0.0);
        Ok(rate)
    }

    /// 미결제약정
    pub async fn fetch_open_interest(&self) -> anyhow::Result<f64> {
        let resp = self.client
            .get(format!("{}/fapi/v1/openInterest", FAPI))
            .query(&[("symbol", "BTCUSDT")])
            .send()
            .await?
            .json::<Value>()
            .await?;

        let oi: f64 = resp["openInterest"]
            .as_str()
            .unwrap_or("0")
            .parse()
            .unwrap_or(0.0);
        Ok(oi)
    }
}
