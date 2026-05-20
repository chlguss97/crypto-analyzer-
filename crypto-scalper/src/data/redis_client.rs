use redis::AsyncCommands;
use std::time::Duration;
use tracing::{debug, warn};

pub struct RedisClient {
    client: redis::Client,
    conn: Option<redis::aio::MultiplexedConnection>,
}

impl RedisClient {
    pub fn new(host: &str, port: u16, db: u8) -> anyhow::Result<Self> {
        let url = format!("redis://{}:{}/{}", host, port, db);
        let client = redis::Client::open(url)?;
        Ok(Self { client, conn: None })
    }

    pub async fn connect(&mut self) -> anyhow::Result<()> {
        let conn = self.client.get_multiplexed_async_connection().await?;
        self.conn = Some(conn);
        tracing::info!("Redis 연결 완료");
        Ok(())
    }

    async fn get_conn(&mut self) -> anyhow::Result<&mut redis::aio::MultiplexedConnection> {
        if self.conn.is_none() {
            self.connect().await?;
        }
        self.conn.as_mut().ok_or_else(|| anyhow::anyhow!("Redis 연결 없음"))
    }

    pub async fn set(&mut self, key: &str, value: &str, ttl_sec: Option<u64>) -> anyhow::Result<()> {
        let conn = self.get_conn().await?;
        if let Some(ttl) = ttl_sec {
            conn.set_ex(key, value, ttl).await?;
        } else {
            conn.set(key, value).await?;
        }
        Ok(())
    }

    pub async fn get(&mut self, key: &str) -> anyhow::Result<Option<String>> {
        let conn = self.get_conn().await?;
        let val: Option<String> = conn.get(key).await?;
        Ok(val)
    }

    pub async fn hset(&mut self, key: &str, fields: &[(&str, &str)], ttl_sec: Option<u64>) -> anyhow::Result<()> {
        let conn = self.get_conn().await?;
        for (field, value) in fields {
            conn.hset(key, *field, *value).await?;
        }
        if let Some(ttl) = ttl_sec {
            conn.expire(key, ttl as i64).await?;
        }
        Ok(())
    }

    pub async fn hgetall(&mut self, key: &str) -> anyhow::Result<std::collections::HashMap<String, String>> {
        let conn = self.get_conn().await?;
        let map: std::collections::HashMap<String, String> = conn.hgetall(key).await?;
        Ok(map)
    }

    pub async fn del(&mut self, key: &str) -> anyhow::Result<()> {
        let conn = self.get_conn().await?;
        conn.del(key).await?;
        Ok(())
    }

    pub async fn keys(&mut self, pattern: &str) -> anyhow::Result<Vec<String>> {
        let conn = self.get_conn().await?;
        let keys: Vec<String> = conn.keys(pattern).await?;
        Ok(keys)
    }

    pub async fn publish(&mut self, channel: &str, message: &str) -> anyhow::Result<()> {
        let conn = self.get_conn().await?;
        conn.publish(channel, message).await?;
        Ok(())
    }
}
