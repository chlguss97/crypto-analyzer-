use crate::models::candle::Candle;

/// Parkinson Volatility + Realized Vol 50/50 블렌딩
/// Reference: Parkinson (1980), pro reference 50/50 blend
pub fn calc_blended_vol(candles: &[Candle]) -> f64 {
    if candles.len() < 2 {
        return 0.0;
    }

    let recent = if candles.len() > 20 {
        &candles[candles.len() - 20..]
    } else {
        candles
    };

    // Parkinson: σ_P = √[(1/(4n·ln2)) · Σ(ln(H/L))²]
    let mut sum_sq = 0.0;
    let mut valid_count = 0;
    for c in recent {
        if c.high > 0.0 && c.low > 0.0 && c.high >= c.low {
            let log_hl = (c.high / c.low).ln();
            sum_sq += log_hl * log_hl;
            valid_count += 1;
        }
    }

    let p_vol = if valid_count > 0 {
        (sum_sq / (4.0 * valid_count as f64 * 2.0_f64.ln())).sqrt()
    } else {
        0.0
    };

    // Realized Vol: σ_R = std(log returns)
    let closes: Vec<f64> = candles.iter().map(|c| c.close).collect();
    let mut log_rets = Vec::new();
    for i in 1..closes.len() {
        if closes[i - 1] > 0.0 {
            log_rets.push((closes[i] / closes[i - 1]).ln());
        }
    }

    let r_vol = if !log_rets.is_empty() {
        let mean = log_rets.iter().sum::<f64>() / log_rets.len() as f64;
        let var = log_rets.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / log_rets.len() as f64;
        var.sqrt()
    } else {
        0.0
    };

    // 50/50 블렌딩
    p_vol * 0.5 + r_vol * 0.5
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parkinson_basic() {
        let candles = vec![
            Candle { timestamp: 0, open: 100.0, high: 102.0, low: 99.0, close: 101.0, volume: 1.0 },
            Candle { timestamp: 1, open: 101.0, high: 103.0, low: 100.0, close: 102.0, volume: 1.0 },
            Candle { timestamp: 2, open: 102.0, high: 104.0, low: 101.0, close: 103.0, volume: 1.0 },
        ];
        let vol = calc_blended_vol(&candles);
        assert!(vol > 0.0, "변동성은 양수: {vol}");
    }
}
