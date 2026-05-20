use reqwest::Client;
use serde_json::json;
use tracing::{info, warn};

pub struct TelegramBot {
    client: Client,
    token: String,
    chat_id: String,
    enabled: bool,
}

impl TelegramBot {
    pub fn new(token: &str, chat_id: &str, enabled: bool) -> Self {
        Self {
            client: Client::new(),
            token: token.to_string(),
            chat_id: chat_id.to_string(),
            enabled,
        }
    }

    pub async fn send(&self, text: &str) -> anyhow::Result<()> {
        if !self.enabled || self.token.is_empty() || self.chat_id.is_empty() {
            return Ok(());
        }

        let url = format!("https://api.telegram.org/bot{}/sendMessage", self.token);
        let body = json!({
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
        });

        match self.client.post(&url).json(&body).send().await {
            Ok(_) => Ok(()),
            Err(e) => {
                warn!("텔레그램 전송 실패: {}", e);
                Ok(()) // 알림 실패가 매매를 막지 않게
            }
        }
    }

    pub async fn notify_entry(&self, direction: &str, price: f64, tp: f64, sl: f64, size: f64, leverage: u32) {
        let msg = format!(
            "⚡ <b>SCALP {}</b>\n${:.0} | TP ${:.0} SL ${:.0}\nSize: {} BTC | {}x",
            direction.to_uppercase(), price, tp, sl, size, leverage
        );
        let _ = self.send(&msg).await;
    }

    pub async fn notify_exit(&self, direction: &str, reason: &str, entry: f64, exit: f64,
                              pnl_pct: f64, pnl_usdt: f64, hold_sec: i64) {
        let marker = if pnl_usdt > 0.0 { "✅" } else { "❌" };
        let msg = format!(
            "{} <b>SCALP {} {}</b>\n${:.0}→${:.0} | {:.2}% (${:.2})\n보유: {}초",
            marker, direction.to_uppercase(), reason,
            entry, exit, pnl_pct, pnl_usdt, hold_sec
        );
        let _ = self.send(&msg).await;
    }

    pub async fn notify_status(&self, text: &str) {
        let _ = self.send(text).await;
    }
}
