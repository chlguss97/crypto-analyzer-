use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::sync::mpsc;
use tokio_tungstenite::{connect_async, tungstenite::Message};
use tracing::{error, info, warn};

const OKX_WS_PUBLIC: &str = "wss://ws.okx.com:8443/ws/v5/public";
const OKX_WS_BUSINESS: &str = "wss://ws.okx.com:8443/ws/v5/business";
const SYMBOL: &str = "BTC-USDT-SWAP";

/// OKX WebSocket 이벤트
#[derive(Debug, Clone)]
pub enum OkxEvent {
    Ticker { price: f64, bid: f64, ask: f64, ts: i64 },
    Trade { price: f64, size: f64, side: String, ts: i64 },
    Book { bids: Vec<(f64, f64)>, asks: Vec<(f64, f64)> },
    Candle { tf: String, candle: crate::models::candle::Candle, is_closed: bool },
}

/// OKX Public + Business WebSocket
pub async fn run_okx_ws(tx: mpsc::Sender<OkxEvent>) {
    loop {
        if let Err(e) = connect_and_stream(&tx).await {
            warn!("OKX WS 끊김: {} → 5초 후 재연결", e);
        }
        tokio::time::sleep(std::time::Duration::from_secs(5)).await;
    }
}

async fn connect_and_stream(tx: &mpsc::Sender<OkxEvent>) -> anyhow::Result<()> {
    // Public WS
    let (pub_ws, _) = connect_async(OKX_WS_PUBLIC).await?;
    let (mut pub_write, mut pub_read) = pub_ws.split();
    info!("OKX WS Public 연결 성공");

    // Subscribe: tickers + trades + books5
    let sub = json!({
        "op": "subscribe",
        "args": [
            {"channel": "tickers", "instId": SYMBOL},
            {"channel": "trades", "instId": SYMBOL},
            {"channel": "books5", "instId": SYMBOL},
        ]
    });
    pub_write.send(Message::Text(sub.to_string())).await?;

    // Business WS (candles)
    let (biz_ws, _) = connect_async(OKX_WS_BUSINESS).await?;
    let (mut biz_write, mut biz_read) = biz_ws.split();
    info!("OKX WS Business 연결 성공");

    let candle_sub = json!({
        "op": "subscribe",
        "args": [
            {"channel": "candle1m", "instId": SYMBOL},
            {"channel": "candle5m", "instId": SYMBOL},
            {"channel": "candle15m", "instId": SYMBOL},
            {"channel": "candle1H", "instId": SYMBOL},
            {"channel": "candle4H", "instId": SYMBOL},
            {"channel": "candle1D", "instId": SYMBOL},
            {"channel": "candle1W", "instId": SYMBOL},
        ]
    });
    biz_write.send(Message::Text(candle_sub.to_string())).await?;

    // 양쪽 WS 동시 수신
    loop {
        tokio::select! {
            msg = pub_read.next() => {
                match msg {
                    Some(Ok(Message::Text(text))) => {
                        if let Err(e) = handle_message(&text, tx).await {
                            error!("OKX pub 처리 에러: {}", e);
                        }
                    }
                    Some(Ok(Message::Ping(data))) => {
                        let _ = pub_write.send(Message::Pong(data)).await;
                    }
                    None | Some(Err(_)) => break,
                    _ => {}
                }
            }
            msg = biz_read.next() => {
                match msg {
                    Some(Ok(Message::Text(text))) => {
                        if let Err(e) = handle_message(&text, tx).await {
                            error!("OKX biz 처리 에러: {}", e);
                        }
                    }
                    Some(Ok(Message::Ping(data))) => {
                        let _ = biz_write.send(Message::Pong(data)).await;
                    }
                    None | Some(Err(_)) => break,
                    _ => {}
                }
            }
        }
    }

    Ok(())
}

async fn handle_message(text: &str, tx: &mpsc::Sender<OkxEvent>) -> anyhow::Result<()> {
    let data: Value = serde_json::from_str(text)?;

    // 구독 확인/에러 무시
    if data.get("event").is_some() {
        return Ok(());
    }

    let channel = data["arg"]["channel"].as_str().unwrap_or("");
    let items = data["data"].as_array();
    if items.is_none() || items.unwrap().is_empty() {
        return Ok(());
    }

    match channel {
        "tickers" => {
            let item = &items.unwrap()[0];
            let event = OkxEvent::Ticker {
                price: parse_f64(item, "last"),
                bid: parse_f64(item, "bidPx"),
                ask: parse_f64(item, "askPx"),
                ts: parse_i64(item, "ts"),
            };
            let _ = tx.send(event).await;
        }
        "trades" => {
            for item in items.unwrap() {
                let event = OkxEvent::Trade {
                    price: parse_f64(item, "px"),
                    size: parse_f64(item, "sz"),
                    side: item["side"].as_str().unwrap_or("").to_string(),
                    ts: parse_i64(item, "ts"),
                };
                let _ = tx.send(event).await;
            }
        }
        "books5" => {
            let item = &items.unwrap()[0];
            let bids = parse_book_levels(&item["bids"]);
            let asks = parse_book_levels(&item["asks"]);
            let _ = tx.send(OkxEvent::Book { bids, asks }).await;
        }
        ch if ch.starts_with("candle") => {
            let tf = ch.replace("candle", "");
            let tf_std = match tf.as_str() {
                "1m" => "1m", "5m" => "5m", "15m" => "15m",
                "1H" => "1h", "4H" => "4h", "1D" => "1d", "1W" => "1w",
                _ => &tf,
            };
            for item in items.unwrap() {
                if let Some(arr) = item.as_array() {
                    if arr.len() >= 9 {
                        let is_closed = arr[8].as_str() == Some("1");
                        let candle = crate::models::candle::Candle {
                            timestamp: arr[0].as_str().unwrap_or("0").parse().unwrap_or(0),
                            open: arr[1].as_str().unwrap_or("0").parse().unwrap_or(0.0),
                            high: arr[2].as_str().unwrap_or("0").parse().unwrap_or(0.0),
                            low: arr[3].as_str().unwrap_or("0").parse().unwrap_or(0.0),
                            close: arr[4].as_str().unwrap_or("0").parse().unwrap_or(0.0),
                            volume: arr[5].as_str().unwrap_or("0").parse().unwrap_or(0.0),
                        };
                        let _ = tx.send(OkxEvent::Candle {
                            tf: tf_std.to_string(),
                            candle,
                            is_closed,
                        }).await;
                    }
                }
            }
        }
        _ => {}
    }

    Ok(())
}

fn parse_f64(v: &Value, key: &str) -> f64 {
    v[key].as_str().unwrap_or("0").parse().unwrap_or(0.0)
}

fn parse_i64(v: &Value, key: &str) -> i64 {
    v[key].as_str().unwrap_or("0").parse().unwrap_or(0)
}

fn parse_book_levels(v: &Value) -> Vec<(f64, f64)> {
    v.as_array()
        .map(|arr| {
            arr.iter()
                .filter_map(|level| {
                    let price: f64 = level[0].as_str()?.parse().ok()?;
                    let size: f64 = level[1].as_str()?.parse().ok()?;
                    Some((price, size))
                })
                .collect()
        })
        .unwrap_or_default()
}
