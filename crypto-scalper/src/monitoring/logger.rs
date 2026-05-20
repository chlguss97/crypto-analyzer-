use chrono::Utc;
use serde_json::Value;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::Path;

/// JSONL Trade Logger — 주간 파일 영구 보존
pub struct TradeLogger {
    log_dir: String,
}

impl TradeLogger {
    pub fn new(log_dir: &str) -> Self {
        fs::create_dir_all(log_dir).ok();
        Self { log_dir: log_dir.to_string() }
    }

    fn week_tag() -> String {
        Utc::now().format("%Y-W%W").to_string()
    }

    fn jsonl_path(&self) -> String {
        format!("{}/trades_{}.jsonl", self.log_dir, Self::week_tag())
    }

    pub fn append(&self, mut record: Value) {
        let now = Utc::now();
        record["ts"] = serde_json::json!(now.timestamp());
        record["ts_iso"] = serde_json::json!(now.format("%Y-%m-%dT%H:%M:%S+00:00").to_string());

        if let Ok(mut file) = OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.jsonl_path())
        {
            let _ = writeln!(file, "{}", record);
            let _ = file.flush();
        }
    }
}
