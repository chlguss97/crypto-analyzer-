"""
Technical Indicators — StochRSI + MACD + Bollinger Bands

Jay 단타법 핵심 지표. 전부 numpy 순수 함수.
외부 TA 라이브러리 없음.
"""

import numpy as np


def _sma(data: np.ndarray, period: int) -> np.ndarray:
    """Simple Moving Average"""
    result = np.full_like(data, np.nan)
    for i in range(period - 1, len(data)):
        window = data[i - period + 1: i + 1]
        if np.any(np.isnan(window)):
            continue
        result[i] = np.mean(window)
    return result


def _wilder_smooth(data: np.ndarray, period: int) -> np.ndarray:
    """Wilder smoothing (RSI용): alpha = 1/period"""
    result = np.empty_like(data)
    result[:] = np.nan
    result[period - 1] = np.mean(data[:period])
    alpha = 1.0 / period
    for i in range(period, len(data)):
        result[i] = data[i] * alpha + result[i - 1] * (1 - alpha)
    return result


def ema(data: np.ndarray, period: int) -> np.ndarray:
    """Exponential Moving Average"""
    result = np.full_like(data, np.nan, dtype=float)
    # Seed with SMA
    valid = data[:period]
    if np.any(np.isnan(valid)):
        # 유효 데이터가 부족하면 첫 유효 구간 탐색
        start = 0
        for i in range(len(data)):
            if not np.isnan(data[i]):
                start = i
                break
        if start + period > len(data):
            return result
        result[start + period - 1] = np.mean(data[start: start + period])
        k = 2.0 / (period + 1)
        for i in range(start + period, len(data)):
            result[i] = data[i] * k + result[i - 1] * (1 - k)
    else:
        result[period - 1] = np.mean(data[:period])
        k = 2.0 / (period + 1)
        for i in range(period, len(data)):
            result[i] = data[i] * k + result[i - 1] * (1 - k)
    return result


def rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """RSI (Wilder smoothing)"""
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = _wilder_smooth(gains, period)
    avg_loss = _wilder_smooth(losses, period)
    rs = avg_gain / np.where(avg_loss == 0, 1e-10, avg_loss)
    rsi_vals = 100.0 - 100.0 / (1.0 + rs)
    return np.concatenate([[np.nan], rsi_vals])


def stoch_rsi(closes: np.ndarray, rsi_period: int = 14,
              stoch_period: int = 14, k_smooth: int = 3,
              d_smooth: int = 3) -> tuple[np.ndarray, np.ndarray]:
    """
    Stochastic RSI — TradingView 기본 설정과 동일.
    Returns (K, D) 0~100 스케일.
    """
    rsi_vals = rsi(closes, rsi_period)

    # Stochastic of RSI
    stoch_raw = np.full_like(rsi_vals, np.nan)
    for i in range(stoch_period - 1, len(rsi_vals)):
        window = rsi_vals[i - stoch_period + 1: i + 1]
        if np.any(np.isnan(window)):
            continue
        lo, hi = np.min(window), np.max(window)
        if hi == lo:
            stoch_raw[i] = 50.0
        else:
            stoch_raw[i] = (rsi_vals[i] - lo) / (hi - lo) * 100.0

    k_line = _sma(stoch_raw, k_smooth)
    d_line = _sma(k_line, d_smooth)
    return k_line, d_line


def macd(closes: np.ndarray, fast: int = 8, slow: int = 26,
         signal_period: int = 9) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    MACD — Jay 설정: fast=8 (비표준), slow=26, signal=9.
    Returns (macd_line, signal_line, histogram).
    """
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line[~np.isnan(macd_line)], signal_period)

    # signal_line을 원래 길이에 맞춤
    full_signal = np.full_like(macd_line, np.nan)
    offset = len(macd_line) - len(signal_line)
    full_signal[offset:] = signal_line

    histogram = macd_line - full_signal
    return macd_line, full_signal, histogram


def bollinger_bands(closes: np.ndarray, period: int = 20,
                    std_dev: float = 2.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Bollinger Bands. Returns (upper, middle, lower)."""
    middle = _sma(closes, period)
    std = np.full_like(closes, np.nan)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1: i + 1]
        std[i] = np.std(window, ddof=0)
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return upper, middle, lower


def atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
        period: int = 14) -> np.ndarray:
    """Average True Range"""
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:] - closes[:-1])
        )
    )
    atr_vals = _wilder_smooth(tr, period)
    return np.concatenate([[np.nan], atr_vals])
