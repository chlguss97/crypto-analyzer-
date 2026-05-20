use futures_util::StreamExt;
use serde_json::Value;
use tokio::sync::mpsc;
use tokio_tungstenite::{connect_async, tungstenite::Message};
use tracing::{info, warn};

const BINANCE_WS: &str = "wss://fstream.binancefuture.com/ws/btcusdt@aggTrade";

/// Binance aggTrade 이벤트
#[derive(Debug, Clone)]
pub struct BinanceTrade {
    pub price: f64,
    pub qty: f64,
    pub is_buyer_maker: bool,  // true = taker sell
    pub ts: i64,
}

pub async fn run_binance_ws(tx: mpsc::Sender<BinanceTrade>) {
    loop {
        if let Err(e) = stream_aggtrade(&tx).await {
            warn!("Binance WS 끊김: {} → 5초 후 재연결", e);
        }
        tokio::time::sleep(std::time::Duration::from_secs(5)).await;
    }
}

async fn stream_aggtrade(tx: &mpsc::Sender<BinanceTrade>) -> anyhow::Result<()> {
    let (ws, _) = connect_async(BINANCE_WS).await?;
    let (_, mut read) = ws.split();
    info!("Binance WS 연결 성공 (aggTrade)");

    while let Some(msg) = read.next().await {
        match msg {
            Ok(Message::Text(text)) => {
                if let Ok(data) = serde_json::from_str::<Value>(&text) {
                    let trade = BinanceTrade {
                        price: data["p"].as_str().unwrap_or("0").parse().unwrap_or(0.0),
                        qty: data["q"].as_str().unwrap_or("0").parse().unwrap_or(0.0),
                        is_buyer_maker: data["m"].as_bool().unwrap_or(false),
                        ts: data["T"].as_i64().unwrap_or(0),
                    };
                    if trade.price > 0.0 && trade.qty > 0.0 {
                        let _ = tx.send(trade).await;
                    }
                }
            }
            Ok(Message::Ping(data)) => {} // tungstenite auto-pongs
            Err(e) => {
                warn!("Binance WS 수신 에러: {}", e);
                break;
            }
            _ => {}
        }
    }

    Ok(())
}
