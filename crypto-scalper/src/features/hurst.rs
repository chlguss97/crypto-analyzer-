/// Hurst Exponent via Rescaled Range (R/S) Analysis
/// Reference: Hurst (1951), Mandelbrot & Wallis (1969)
/// Dynamic scales: n/8, n/4, n/2, n (pro reference)

pub fn calc_hurst(returns: &[f64]) -> f64 {
    let n = returns.len();
    if n < 16 {
        return 0.5; // 데이터 부족 → 랜덤워크
    }

    let mut sizes = Vec::new();
    let mut rs_values = Vec::new();

    for divisor in [8, 4, 2, 1] {
        let size = (n / divisor).max(4);
        if size > n || size < 4 {
            continue;
        }

        let mut rs_list = Vec::new();
        let mut start = 0;
        while start + size <= n {
            let chunk = &returns[start..start + size];
            let mean = chunk.iter().sum::<f64>() / chunk.len() as f64;

            // Cumulative deviation
            let mut cumdev = Vec::with_capacity(chunk.len());
            let mut sum = 0.0;
            for &v in chunk {
                sum += v - mean;
                cumdev.push(sum);
            }

            let r = cumdev.iter().cloned().fold(f64::NEG_INFINITY, f64::max)
                - cumdev.iter().cloned().fold(f64::INFINITY, f64::min);

            // Sample std
            let var = chunk.iter().map(|&v| (v - mean).powi(2)).sum::<f64>()
                / (chunk.len() - 1) as f64;
            let s = var.sqrt();

            if s > 1e-10 {
                rs_list.push(r / s);
            }

            start += size;
        }

        if !rs_list.is_empty() {
            let mean_rs = rs_list.iter().sum::<f64>() / rs_list.len() as f64;
            sizes.push(size as f64);
            rs_values.push(mean_rs);
        }
    }

    if sizes.len() < 2 {
        return 0.5;
    }

    // log-log linear regression: log(R/S) = H * log(n) + c
    let log_n: Vec<f64> = sizes.iter().map(|&s| s.ln()).collect();
    let log_rs: Vec<f64> = rs_values.iter().map(|&r| r.ln()).collect();

    let slope = linear_regression_slope(&log_n, &log_rs);
    slope.clamp(0.0, 1.0)
}

fn linear_regression_slope(x: &[f64], y: &[f64]) -> f64 {
    let n = x.len() as f64;
    let sx: f64 = x.iter().sum();
    let sy: f64 = y.iter().sum();
    let sxy: f64 = x.iter().zip(y.iter()).map(|(a, b)| a * b).sum();
    let sxx: f64 = x.iter().map(|a| a * a).sum();

    let denom = n * sxx - sx * sx;
    if denom.abs() < 1e-10 {
        return 0.5;
    }

    (n * sxy - sx * sy) / denom
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_hurst_random() {
        // 랜덤 워크 → H ≈ 0.5
        let returns: Vec<f64> = (0..100).map(|i| ((i * 7 + 3) % 11) as f64 - 5.0).collect();
        let h = calc_hurst(&returns);
        assert!(h > 0.0 && h < 1.0, "Hurst는 0~1 범위: {h}");
    }

    #[test]
    fn test_hurst_too_short() {
        let h = calc_hurst(&[1.0, 2.0, 3.0]);
        assert_eq!(h, 0.5);
    }
}
