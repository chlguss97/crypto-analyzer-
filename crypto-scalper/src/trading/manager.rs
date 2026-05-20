use crate::config::ScalpConfig;
use crate::models::position::ScalpPosition;
use crate::models::signal::{Direction, ScalpSignal};
use crate::trading::okx_api::OkxApi;
use tracing::{error, info, warn};

const MIN_ORDER_SIZE_BTC: f64 = 0.01;

/// ScalpManager — 진입/청산/SL self-heal
pub struct ScalpManager {
    config: ScalpConfig,
    pub position: Option<ScalpPosition>,
}

impl ScalpManager {
    pub fn new(config: &ScalpConfig) -> Self {
        Self {
            config: config.clone(),
            position: None,
        }
    }

    pub fn has_position(&self) -> bool {
        self.position.is_some()
    }

    /// 시장가 진입
    pub async fn open_scalp(
        &mut self,
        signal: &ScalpSignal,
        balance: f64,
        okx: &OkxApi,
        parkinson_vol: f64,
    ) -> anyhow::Result<bool> {
        if self.position.is_some() {
            return Ok(false);
        }

        let price = signal.price;
        let direction = signal.direction;
        let combined_mult = signal.combined_size_mult;

        // 사이즈
        let margin = balance * self.config.margin_pct * combined_mult;
        if margin <= 0.0 { return Ok(false); }
        let raw_size = margin * self.config.leverage as f64 / price;
        let size = ((raw_size / MIN_ORDER_SIZE_BTC).floor() * MIN_ORDER_SIZE_BTC).max(MIN_ORDER_SIZE_BTC);

        // 동적 SL/TP (k × Parkinson Vol)
        let (sl_dist, tp_dist) = if parkinson_vol > 0.0 {
            let sl = (self.config.sl_k_vol * parkinson_vol).clamp(0.001, 0.005);
            let tp = (self.config.tp_k_vol * parkinson_vol).clamp(0.001, 0.005);
            (sl, tp.max(sl)) // RR >= 1.0
        } else {
            (self.config.sl_price_pct / 100.0, self.config.tp_price_pct / 100.0)
        };

        // 레버리지
        let dir_str = direction.as_str();
        if let Err(e) = okx.set_leverage(self.config.leverage, dir_str).await {
            warn!("레버리지 설정 실패: {}", e);
        }

        // 시장가 진입
        let side = if direction == Direction::Long { "buy" } else { "sell" };
        let pos_side = dir_str;
        info!("[SCALP] {} 진입 시도 @ ${:.0} | size={} BTC", dir_str.to_uppercase(), price, size);

        let order = okx.market_order(side, size, pos_side).await?;
        if !order.success {
            info!("[SCALP] 진입 실패");
            return Ok(false);
        }

        let fill_price = if order.fill_price > 0.0 { order.fill_price } else { price };

        // SL/TP 계산
        let (sl_price, tp_price) = match direction {
            Direction::Long => (
                (fill_price * (1.0 - sl_dist) * 10.0).round() / 10.0,
                (fill_price * (1.0 + tp_dist) * 10.0).round() / 10.0,
            ),
            Direction::Short => (
                (fill_price * (1.0 + sl_dist) * 10.0).round() / 10.0,
                (fill_price * (1.0 - tp_dist) * 10.0).round() / 10.0,
            ),
        };

        // SL 등록
        let sl_result = okx.set_stop_loss(dir_str, size, sl_price).await?;
        if !sl_result.success {
            error!("[SCALP] SL 등록 실패 → 즉시 청산");
            let _ = okx.close_position(dir_str, size).await;
            return Ok(false);
        }

        // TP 등록
        let tp_result = okx.set_take_profit(dir_str, size, tp_price).await?;

        let mut pos = ScalpPosition::new(
            0, // trade_id — DB insert 후 설정
            signal.signal_id.unwrap_or(0),
            direction, fill_price, size, self.config.leverage, sl_price, tp_price,
        );
        pos.sl_algo_id = Some(sl_result.algo_id);
        pos.tp_algo_id = if tp_result.success { Some(tp_result.algo_id) } else { None };
        pos.total_fee = order.fee;

        info!(
            "[SCALP] 진입 완료: {} ${:.0} | TP ${:.0} SL ${:.0} | {} BTC",
            dir_str.to_uppercase(), fill_price, tp_price, sl_price, size
        );

        self.position = Some(pos);
        Ok(true)
    }

    /// 포지션 체크 (SL failsafe + self-heal)
    pub async fn check_position(&mut self, price: f64, okx: &OkxApi) -> anyhow::Result<Option<CloseResult>> {
        let pos = match &mut self.position {
            Some(p) => p,
            None => return Ok(None),
        };

        pos.update_best_worst(price);
        let hold_sec = pos.hold_seconds();

        // 1. 외부 청산 감지
        if let Ok(ex_size) = okx.get_position_size().await {
            if ex_size < 1e-8 && ex_size >= 0.0 {
                let reason = infer_exit_reason(pos, price);
                let result = self.finalize(price, &reason, hold_sec);
                return Ok(Some(result));
            }
        }

        // 2. SL Failsafe
        if pos.is_sl_breached(price) {
            error!("[SCALP] SL failsafe: ${:.0} vs SL ${:.0}", price, pos.sl_price);
            self.close_and_finalize(okx, "sl_failsafe", hold_sec).await?;
            return Ok(self.position.is_none().then(|| CloseResult {
                pnl_pct: 0.0, pnl_usdt: 0.0, exit_reason: "sl_failsafe".to_string(),
                hold_sec: hold_sec as i64,
            }));
        }

        // 3. SL Self-Heal (5초 간격)
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH).unwrap().as_secs_f64();

        if let Some(ref sl_id) = pos.sl_algo_id {
            if now - pos.last_sl_verify >= 5.0 {
                pos.last_sl_verify = now;
                if let Ok(pending) = okx.get_pending_algos().await {
                    let found = pending.iter().any(|p| {
                        p["algoClOrdId"].as_str() == Some(sl_id) ||
                        p["algoId"].as_str() == Some(sl_id)
                    });
                    if !found {
                        pos.sl_lost_count += 1;
                        warn!("SL 소실 ({}회) → 재등록", pos.sl_lost_count);
                        if let Ok(new_sl) = okx.set_stop_loss(
                            pos.direction.as_str(), pos.size, pos.sl_price
                        ).await {
                            pos.sl_algo_id = Some(new_sl.algo_id);
                        }

                        if pos.sl_lost_count >= 3 {
                            error!("SL 3회 소실 → 강제 청산");
                            self.close_and_finalize(okx, "sl_repeated_loss", hold_sec).await?;
                        }
                    }
                }
            }
        }

        Ok(None)
    }

    /// 시그널 반전 청산
    pub async fn close_on_reversal(&mut self, okx: &OkxApi) -> anyhow::Result<Option<CloseResult>> {
        if let Some(pos) = &self.position {
            let hold_sec = pos.hold_seconds();
            self.close_and_finalize(okx, "signal_reversal", hold_sec).await?;
            // finalize 후 position은 None
            Ok(Some(CloseResult {
                pnl_pct: 0.0, pnl_usdt: 0.0,
                exit_reason: "signal_reversal".to_string(),
                hold_sec: hold_sec as i64,
            }))
        } else {
            Ok(None)
        }
    }

    async fn close_and_finalize(&mut self, okx: &OkxApi, reason: &str, hold_sec: f64) -> anyhow::Result<()> {
        if let Some(pos) = &self.position {
            // 알고 취소
            if let Some(ref id) = pos.sl_algo_id { let _ = okx.cancel_algo(id).await; }
            if let Some(ref id) = pos.tp_algo_id { let _ = okx.cancel_algo(id).await; }
            let _ = okx.cancel_all_algos().await;

            // 청산
            let _ = okx.close_position(pos.direction.as_str(), pos.size).await;
        }

        // position 해제
        self.position = None;
        Ok(())
    }

    fn finalize(&mut self, exit_price: f64, reason: &str, hold_sec: f64) -> CloseResult {
        let pos = self.position.take().unwrap();
        let pnl_pct = pos.margin_pct(exit_price);
        let pnl_usdt = match pos.direction {
            Direction::Long => pos.size * (exit_price - pos.entry_price) - pos.total_fee,
            Direction::Short => pos.size * (pos.entry_price - exit_price) - pos.total_fee,
        };

        info!(
            "[SCALP] 청산: {} {} | ${:.0}→${:.0} | {:.2}% (${:.2}) | {}초",
            pos.direction.as_str().to_uppercase(), reason,
            pos.entry_price, exit_price, pnl_pct, pnl_usdt, hold_sec as i64
        );

        CloseResult {
            pnl_pct,
            pnl_usdt,
            exit_reason: reason.to_string(),
            hold_sec: hold_sec as i64,
        }
    }
}

#[derive(Debug)]
pub struct CloseResult {
    pub pnl_pct: f64,
    pub pnl_usdt: f64,
    pub exit_reason: String,
    pub hold_sec: i64,
}

fn infer_exit_reason(pos: &ScalpPosition, price: f64) -> String {
    match pos.direction {
        Direction::Long => {
            if price >= pos.tp_price * 0.999 { "tp".to_string() }
            else if price <= pos.sl_price * 1.001 { "sl".to_string() }
            else { "external".to_string() }
        }
        Direction::Short => {
            if price <= pos.tp_price * 1.001 { "tp".to_string() }
            else if price >= pos.sl_price * 0.999 { "sl".to_string() }
            else { "external".to_string() }
        }
    }
}
