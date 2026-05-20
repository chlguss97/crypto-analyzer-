use axum::{extract::State, routing::get, Json, Router};
use serde_json::{json, Value};
use std::sync::Arc;
use tokio::sync::RwLock;

/// Axum Dashboard — 최소 API
pub fn create_router() -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/api/status", get(status))
}

async fn health() -> Json<Value> {
    Json(json!({"status": "ok", "engine": "ScalpEngine v4 Rust"}))
}

async fn status() -> Json<Value> {
    // TODO: 공유 상태에서 실시간 데이터 읽기
    Json(json!({
        "engine": "ScalpEngine v4",
        "language": "Rust + Burn",
        "status": "running",
    }))
}
