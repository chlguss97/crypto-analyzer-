use base64::Engine as _;
use chrono::Utc;
use hmac::{Hmac, Mac};
use reqwest::Client;
use serde_json::{json, Value};
use sha2::Sha256;
use tracing::{error, info, warn};

type HmacSha256 = Hmac<Sha256>;

const OKX_REST: &str = "https://www.okx.com";
const INST_ID: &str = "BTC-USDT-SWAP";
const CONTRACT_SIZE: f64 = 0.01; // 1 contract = 0.01 BTC

pub struct OkxApi {
    client: Client,
    api_key: String,
    secret: String,
    passphrase: String,
    simulated: bool,
}

#[derive(Debug, Clone)]
pub struct OrderResult {
    pub order_id: String,
    pub fill_price: f64,
    pub filled_size: f64, // BTC
    pub fee: f64,
    pub success: bool,
}

#[derive(Debug, Clone)]
pub struct AlgoResult {
    pub algo_id: String,
    pub success: bool,
}

impl OkxApi {
    pub fn new(api_key: &str, secret: &str, passphrase: &str, simulated: bool) -> Self {
        Self {
            client: Client::builder()
                .timeout(std::time::Duration::from_secs(10))
                .build()
                .unwrap(),
            api_key: api_key.to_string(),
            secret: secret.to_string(),
            passphrase: passphrase.to_string(),
            simulated,
        }
    }

    fn sign(&self, timestamp: &str, method: &str, path: &str, body: &str) -> String {
        let prehash = format!("{}{}{}{}", timestamp, method, path, body);
        let mut mac = HmacSha256::new_from_slice(self.secret.as_bytes()).unwrap();
        mac.update(prehash.as_bytes());
        let result = mac.finalize();
        base64::engine::general_purpose::STANDARD.encode(result.into_bytes())
    }

    async fn request(&self, method: &str, path: &str, body: Option<&Value>) -> anyhow::Result<Value> {
        let timestamp = Utc::now().format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string();
        let body_str = body.map(|b| b.to_string()).unwrap_or_default();
        let signature = self.sign(&timestamp, method, path, &body_str);

        let url = format!("{}{}", OKX_REST, path);
        let mut req = match method {
            "GET" => self.client.get(&url),
            "POST" => self.client.post(&url).body(body_str.clone()),
            _ => unreachable!(),
        };

        req = req
            .header("OK-ACCESS-KEY", &self.api_key)
            .header("OK-ACCESS-SIGN", &signature)
            .header("OK-ACCESS-TIMESTAMP", &timestamp)
            .header("OK-ACCESS-PASSPHRASE", &self.passphrase)
            .header("Content-Type", "application/json");

        if self.simulated {
            req = req.header("x-simulated-trading", "1");
        }

        let resp = req.send().await?.json::<Value>().await?;

        if resp["code"].as_str() != Some("0") {
            let msg = resp["msg"].as_str().unwrap_or("unknown");
            anyhow::bail!("OKX API 에러: {} (code={})", msg, resp["code"]);
        }

        Ok(resp)
    }

    fn btc_to_contracts(size_btc: f64) -> i64 {
        (size_btc / CONTRACT_SIZE).round() as i64
    }

    // ── 잔고 ──

    pub async fn get_balance(&self) -> anyhow::Result<f64> {
        let resp = self.request("GET", "/api/v5/account/balance?ccy=USDT", None).await?;
        let details = &resp["data"][0]["details"];
        if let Some(arr) = details.as_array() {
            for item in arr {
                if item["ccy"].as_str() == Some("USDT") {
                    let avail: f64 = item["availBal"].as_str().unwrap_or("0").parse()?;
                    return Ok(avail);
                }
            }
        }
        Ok(0.0)
    }

    // ── 레버리지 ──

    pub async fn set_leverage(&self, leverage: u32, direction: &str) -> anyhow::Result<()> {
        let pos_side = if direction == "long" { "long" } else { "short" };
        let body = json!({
            "instId": INST_ID,
            "lever": leverage.to_string(),
            "mgnMode": "isolated",
            "posSide": pos_side,
        });
        self.request("POST", "/api/v5/account/set-leverage", Some(&body)).await?;
        Ok(())
    }

    // ── 시장가 주문 ──

    pub async fn market_order(&self, side: &str, size_btc: f64, pos_side: &str) -> anyhow::Result<OrderResult> {
        let contracts = Self::btc_to_contracts(size_btc);
        let body = json!({
            "instId": INST_ID,
            "tdMode": "isolated",
            "side": side,
            "posSide": pos_side,
            "ordType": "market",
            "sz": contracts.to_string(),
        });

        for attempt in 1..=3 {
            match self.request("POST", "/api/v5/trade/order", Some(&body)).await {
                Ok(resp) => {
                    let order_id = resp["data"][0]["ordId"].as_str().unwrap_or("").to_string();

                    // 체결 조회 (시장가는 즉시 체결)
                    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
                    if let Ok(detail) = self.get_order_detail(&order_id).await {
                        return Ok(detail);
                    }

                    return Ok(OrderResult {
                        order_id,
                        fill_price: 0.0,
                        filled_size: size_btc,
                        fee: 0.0,
                        success: true,
                    });
                }
                Err(e) => {
                    error!("시장가 주문 실패 ({}/3): {}", attempt, e);
                    if attempt < 3 {
                        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                    }
                }
            }
        }

        anyhow::bail!("시장가 주문 3회 실패")
    }

    async fn get_order_detail(&self, order_id: &str) -> anyhow::Result<OrderResult> {
        let path = format!("/api/v5/trade/order?instId={}&ordId={}", INST_ID, order_id);
        let resp = self.request("GET", &path, None).await?;
        let data = &resp["data"][0];

        Ok(OrderResult {
            order_id: order_id.to_string(),
            fill_price: data["avgPx"].as_str().unwrap_or("0").parse().unwrap_or(0.0),
            filled_size: {
                let contracts: f64 = data["fillSz"].as_str().unwrap_or("0").parse().unwrap_or(0.0);
                contracts * CONTRACT_SIZE
            },
            fee: {
                let f: f64 = data["fee"].as_str().unwrap_or("0").parse().unwrap_or(0.0);
                f.abs()
            },
            success: data["state"].as_str() == Some("filled"),
        })
    }

    // ── 포지션 청산 ──

    pub async fn close_position(&self, direction: &str, size_btc: f64) -> anyhow::Result<OrderResult> {
        let side = if direction == "long" { "sell" } else { "buy" };
        let pos_side = direction;
        self.market_order(side, size_btc, pos_side).await
    }

    // ── 포지션 조회 ──

    pub async fn get_position_size(&self) -> anyhow::Result<f64> {
        let path = format!("/api/v5/account/positions?instId={}", INST_ID);
        let resp = self.request("GET", &path, None).await?;
        if let Some(arr) = resp["data"].as_array() {
            for pos in arr {
                let contracts: f64 = pos["pos"].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0).abs();
                if contracts > 0.0 {
                    return Ok(contracts * CONTRACT_SIZE);
                }
            }
        }
        Ok(0.0)
    }

    // ── 알고 주문 (SL/TP) ──

    pub async fn set_algo_order(
        &self,
        trigger_px: f64,
        order_px: &str, // "-1" for market, price for limit
        side: &str,
        pos_side: &str,
        size_btc: f64,
        algo_prefix: &str,
    ) -> anyhow::Result<AlgoResult> {
        let contracts = Self::btc_to_contracts(size_btc);
        let ts = chrono::Utc::now().timestamp_millis();
        let algo_id = format!("{}{}{}", algo_prefix, ts, rand::random::<u16>());

        let body = json!({
            "instId": INST_ID,
            "tdMode": "isolated",
            "side": side,
            "posSide": pos_side,
            "ordType": "trigger",
            "triggerPx": format!("{:.1}", trigger_px),
            "orderPx": order_px,
            "triggerPxType": "last",
            "reduceOnly": true,
            "sz": contracts.to_string(),
            "algoClOrdId": &algo_id,
        });

        match self.request("POST", "/api/v5/trade/order-algo", Some(&body)).await {
            Ok(resp) => {
                info!("알고 주문 등록 [{}]: trigger=${:.1} size={}", algo_prefix, trigger_px, size_btc);
                Ok(AlgoResult {
                    algo_id,
                    success: true,
                })
            }
            Err(e) => {
                error!("알고 주문 실패 [{}]: {}", algo_prefix, e);
                Ok(AlgoResult {
                    algo_id: String::new(),
                    success: false,
                })
            }
        }
    }

    /// SL 등록 (market-on-trigger)
    pub async fn set_stop_loss(&self, direction: &str, size_btc: f64, sl_price: f64) -> anyhow::Result<AlgoResult> {
        let side = if direction == "long" { "sell" } else { "buy" };
        let pos_side = direction;
        self.set_algo_order(sl_price, "-1", side, pos_side, size_btc, "sl").await
    }

    /// TP 등록 (limit-on-trigger)
    pub async fn set_take_profit(&self, direction: &str, size_btc: f64, tp_price: f64) -> anyhow::Result<AlgoResult> {
        let side = if direction == "long" { "sell" } else { "buy" };
        let pos_side = direction;
        self.set_algo_order(tp_price, &format!("{:.1}", tp_price), side, pos_side, size_btc, "tp").await
    }

    /// 알고 주문 취소
    pub async fn cancel_algo(&self, algo_id: &str) -> anyhow::Result<()> {
        let body = json!([{
            "algoClOrdId": algo_id,
            "instId": INST_ID,
        }]);
        let _ = self.request("POST", "/api/v5/trade/cancel-algos", Some(&body)).await;
        Ok(())
    }

    /// 알고 pending 조회 (SL 검증용)
    pub async fn get_pending_algos(&self) -> anyhow::Result<Vec<Value>> {
        let path = format!("/api/v5/trade/orders-algo-pending?instType=SWAP&instId={}&ordType=trigger", INST_ID);
        let resp = self.request("GET", &path, None).await?;
        Ok(resp["data"].as_array().cloned().unwrap_or_default())
    }

    /// 모든 알고 취소
    pub async fn cancel_all_algos(&self) -> anyhow::Result<()> {
        let pending = self.get_pending_algos().await?;
        for algo in &pending {
            if let Some(id) = algo["algoClOrdId"].as_str() {
                let _ = self.cancel_algo(id).await;
            } else if let Some(id) = algo["algoId"].as_str() {
                let body = json!([{"algoId": id, "instId": INST_ID}]);
                let _ = self.request("POST", "/api/v5/trade/cancel-algos", Some(&body)).await;
            }
        }
        if !pending.is_empty() {
            info!("알고 {}건 취소", pending.len());
        }
        Ok(())
    }
}
