mod config;
mod data;
mod features;
mod models;
mod monitoring;
mod strategy;
mod trading;

use tracing::info;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // 로깅 초기화
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env().add_directive("info".parse()?))
        .with_target(true)
        .with_target(true)
        .init();

    info!("==================================================");
    info!("ScalpEngine v4 — 100% Rust + Burn");
    info!("==================================================");

    // .env 로드
    dotenvy::dotenv().ok();

    // 설정 로드
    let config = config::Config::load("config/settings.yaml")?;
    info!("설정 로드 완료: {}", config.exchange.symbol);

    // Redis
    let mut redis = data::redis_client::RedisClient::new(
        &config.redis.host, config.redis.port, config.redis.db
    )?;
    redis.connect().await?;

    // SQLite
    let db = data::db::Database::open("data/scalp.db")?;
    info!("SQLite 연결 완료");

    // OKX API
    let okx_api_key = std::env::var("OKX_API_KEY").unwrap_or_default();
    let okx_secret = std::env::var("OKX_SECRET").unwrap_or_default();
    let okx_passphrase = std::env::var("OKX_PASSPHRASE").unwrap_or_default();
    let okx = trading::okx_api::OkxApi::new(
        &okx_api_key, &okx_secret, &okx_passphrase, false
    );

    // 잔고 확인
    match okx.get_balance().await {
        Ok(bal) => info!("잔고: ${:.2}", bal),
        Err(e) => tracing::warn!("잔고 조회 실패: {}", e),
    }

    // Telegram
    let tg_token = std::env::var("TELEGRAM_TOKEN").unwrap_or_default();
    let tg_chat = std::env::var("TELEGRAM_CHAT_ID").unwrap_or_default();
    let telegram = monitoring::telegram::TelegramBot::new(
        &tg_token, &tg_chat, config.telegram.enabled
    );

    // Logger
    let logger = monitoring::logger::TradeLogger::new("data/logs");

    // ML Engine
    let ml = strategy::ml_engine::MlEngine::new(
        config.ml.phase_a_min_samples,
        config.ml.retrain_interval,
        config.ml.go_threshold,
        config.ml.min_oos_accuracy,
    );

    // Risk Manager
    let balance = okx.get_balance().await.unwrap_or(0.0);
    let risk = trading::risk::RiskManager::new(balance, config.risk.bot_kill_drawdown);

    let mode = if config.scalp.shadow_mode { "SHADOW" } else { "LIVE" };
    info!("스캘핑 모드: {}", mode);

    telegram.notify_status(&format!(
        "🟢 <b>ScalpEngine v4 — Rust + Burn</b>\nMode: {} | Balance: ${:.2}\nTP/SL: k×vol (dynamic) | Leverage: {}x",
        mode, balance, config.scalp.leverage
    )).await;

    // 채널 생성 (이벤트 드리븐)
    let (okx_tx, mut okx_rx) = tokio::sync::mpsc::channel::<data::okx_ws::OkxEvent>(10000);
    let (bn_tx, mut bn_rx) = tokio::sync::mpsc::channel::<data::binance_ws::BinanceTrade>(10000);

    info!("비동기 태스크 시작...");

    // 모든 태스크 병렬 실행
    tokio::select! {
        // 데이터 수집
        _ = data::okx_ws::run_okx_ws(okx_tx) => {
            tracing::error!("OKX WS 종료");
        }
        _ = data::binance_ws::run_binance_ws(bn_tx) => {
            tracing::error!("Binance WS 종료");
        }

        // Dashboard
        _ = async {
            let app = monitoring::dashboard::create_router();
            let listener = tokio::net::TcpListener::bind("0.0.0.0:8000").await.unwrap();
            info!("Dashboard: http://0.0.0.0:8000");
            axum::serve(listener, app).await.unwrap();
        } => {}

        // TODO: 추가 태스크들
        // - Binance REST 폴링
        // - ScalpDetector 이벤트 루프
        // - ScalpManager 포지션 관리
        // - Shadow 라벨링
        // - ML 재학습
        // - Heartbeat
    }

    info!("ScalpEngine 종료");
    Ok(())
}
