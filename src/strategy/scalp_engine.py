"""
Scalp Engine — 단타 모델
TF: 1m/5m 실행, 15m 확인
보유: 5~30분
목표: 하루 5% (0.3~0.8% × 고레버 20~30x × 다회전)
"""
import pandas as pd
import numpy as np
from src.engine.base import BaseIndicator


class ScalpEngine:
    """단타 전용 시그널 엔진"""

    def __init__(self):
        self.name = "scalp"
        self.target_daily_pct = 5.0
        self.max_hold_bars_1m = 30   # 30분 (1m봉 기준)
        self.max_hold_bars_5m = 6    # 30분 (5m봉 기준)
        self.default_leverage = 25
        self.sl_atr_mult = 0.8       # 타이트한 SL
        self.tp_rr = 2.0             # 리스크 대비 2배 TP

    async def analyze(self, candles_1m: pd.DataFrame, candles_5m: pd.DataFrame,
                      candles_15m: pd.DataFrame = None) -> dict:
        """단타 시그널 분석"""
        signals = {}
        score_long = 0.0
        score_short = 0.0

        # 1. EMA 크로스 (5m)
        ema_sig = self._ema_cross(candles_5m)
        signals["ema_cross"] = ema_sig
        if ema_sig["direction"] == "long":
            score_long += ema_sig["strength"] * 2.5
        elif ema_sig["direction"] == "short":
            score_short += ema_sig["strength"] * 2.5

        # 2. RSI 극단 반전 (1m)
        rsi_sig = self._rsi_reversal(candles_1m)
        signals["rsi_reversal"] = rsi_sig
        if rsi_sig["direction"] == "long":
            score_long += rsi_sig["strength"] * 2.0
        elif rsi_sig["direction"] == "short":
            score_short += rsi_sig["strength"] * 2.0

        # 3. BB 스퀴즈 돌파 (5m)
        bb_sig = self._bb_breakout(candles_5m)
        signals["bb_breakout"] = bb_sig
        if bb_sig["direction"] == "long":
            score_long += bb_sig["strength"] * 3.0
        elif bb_sig["direction"] == "short":
            score_short += bb_sig["strength"] * 3.0

        # 4. 거래량 스파이크 (1m)
        vol_sig = self._volume_spike(candles_1m)
        signals["volume_spike"] = vol_sig
        if vol_sig["direction"] == "long":
            score_long += vol_sig["strength"] * 2.0
        elif vol_sig["direction"] == "short":
            score_short += vol_sig["strength"] * 2.0

        # 5. 모멘텀 (1m - 3봉 연속 방향)
        mom_sig = self._momentum(candles_1m)
        signals["momentum"] = mom_sig
        if mom_sig["direction"] == "long":
            score_long += mom_sig["strength"] * 1.5
        elif mom_sig["direction"] == "short":
            score_short += mom_sig["strength"] * 1.5

        # 6. 15m 추세 필터
        trend_filter = "neutral"
        if candles_15m is not None and len(candles_15m) >= 20:
            ema50_15m = candles_15m["close"].ewm(span=50, adjust=False).mean().iloc[-1]
            if candles_15m["close"].iloc[-1] > ema50_15m:
                trend_filter = "long"
            else:
                trend_filter = "short"

        # 정규화 (0~10)
        max_possible = 11.0  # 2.5+2+3+2+1.5
        if score_long > score_short:
            direction = "long"
            raw = score_long
        elif score_short > score_long:
            direction = "short"
            raw = score_short
        else:
            direction = "neutral"
            raw = 0

        # 15m 추세 역방향이면 감점
        if trend_filter != "neutral" and trend_filter != direction:
            raw *= 0.5

        score = min(10.0, raw / max_possible * 10)

        # ATR 계산 (5m)
        atr = self._calc_atr(candles_5m, 14)
        atr_pct = atr / candles_5m["close"].iloc[-1] * 100 if atr > 0 else 0.2

        return {
            "mode": "scalp",
            "score": round(score, 2),
            "direction": direction,
            "long_score": round(score_long, 2),
            "short_score": round(score_short, 2),
            "trend_filter": trend_filter,
            "signals": signals,
            "atr": round(atr, 2),
            "atr_pct": round(atr_pct, 4),
            "sl_distance": round(atr * self.sl_atr_mult, 2),
            "tp_distance": round(atr * self.sl_atr_mult * self.tp_rr, 2),
            "leverage": self.default_leverage,
        }

    def _ema_cross(self, df: pd.DataFrame) -> dict:
        close = df["close"]
        ema5 = close.ewm(span=5, adjust=False).mean()
        ema13 = close.ewm(span=13, adjust=False).mean()
        ema21 = close.ewm(span=21, adjust=False).mean()

        # 크로스 감지 (최근 3봉)
        direction = "neutral"
        strength = 0.0

        for i in range(-3, 0):
            if len(ema5) > abs(i) + 1:
                prev = i - 1
                if ema5.iloc[prev] <= ema13.iloc[prev] and ema5.iloc[i] > ema13.iloc[i]:
                    direction = "long"
                    # EMA21 위에서 골든크로스면 더 강함
                    strength = 0.8 if close.iloc[i] > ema21.iloc[i] else 0.5
                elif ema5.iloc[prev] >= ema13.iloc[prev] and ema5.iloc[i] < ema13.iloc[i]:
                    direction = "short"
                    strength = 0.8 if close.iloc[i] < ema21.iloc[i] else 0.5

        # 정배열/역배열 보너스
        if ema5.iloc[-1] > ema13.iloc[-1] > ema21.iloc[-1]:
            if direction == "long":
                strength = min(1.0, strength + 0.2)
        elif ema5.iloc[-1] < ema13.iloc[-1] < ema21.iloc[-1]:
            if direction == "short":
                strength = min(1.0, strength + 0.2)

        return {"type": "ema_cross", "direction": direction, "strength": round(strength, 2)}

    def _rsi_reversal(self, df: pd.DataFrame) -> dict:
        close = df["close"]
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1/7, min_periods=7, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/7, min_periods=7, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.inf)
        rsi = 100 - (100 / (1 + rs))

        r = rsi.iloc[-1]
        r_prev = rsi.iloc[-2] if len(rsi) >= 2 else 50

        direction = "neutral"
        strength = 0.0

        # RSI 반전 감지
        if r < 25 and r > r_prev:  # 과매도에서 반등
            direction = "long"
            strength = min(1.0, (30 - r) / 15)
        elif r > 75 and r < r_prev:  # 과매수에서 하락
            direction = "short"
            strength = min(1.0, (r - 70) / 15)

        return {"type": "rsi_reversal", "direction": direction, "strength": round(strength, 2), "rsi": round(r, 1)}

    def _bb_breakout(self, df: pd.DataFrame) -> dict:
        close = df["close"]
        sma = close.rolling(20).mean()
        std = close.rolling(20).std()
        upper = sma + 2 * std
        lower = sma - 2 * std

        width = ((upper - lower) / sma).dropna()
        min_width = width.tail(50).min() if len(width) >= 50 else width.min()
        current_width = width.iloc[-1] if len(width) > 0 else 0

        is_squeeze = current_width <= min_width * 1.2 if min_width > 0 else False
        last = close.iloc[-1]

        direction = "neutral"
        strength = 0.0

        if is_squeeze:
            # 돌파 확인
            if last > upper.iloc[-1]:
                direction = "long"
                strength = 0.9
            elif last < lower.iloc[-1]:
                direction = "short"
                strength = 0.9
            else:
                strength = 0.3  # 스퀴즈 중 대기

        return {"type": "bb_breakout", "direction": direction, "strength": round(strength, 2), "squeeze": is_squeeze}

    def _volume_spike(self, df: pd.DataFrame) -> dict:
        vol = df["volume"]
        close = df["close"]
        avg = vol.rolling(20).mean()
        ratio = vol.iloc[-1] / avg.iloc[-1] if avg.iloc[-1] > 0 else 1

        direction = "neutral"
        strength = 0.0

        if ratio > 2.0:
            # 가격 방향으로 진입
            if close.iloc[-1] > close.iloc[-2]:
                direction = "long"
            else:
                direction = "short"
            strength = min(1.0, ratio / 5.0)

        return {"type": "volume_spike", "direction": direction, "strength": round(strength, 2), "ratio": round(ratio, 2)}

    def _momentum(self, df: pd.DataFrame) -> dict:
        close = df["close"]
        if len(close) < 4:
            return {"type": "momentum", "direction": "neutral", "strength": 0.0}

        # 최근 3봉 연속 방향
        changes = [close.iloc[-i] - close.iloc[-i-1] for i in range(1, 4)]
        up = all(c > 0 for c in changes)
        down = all(c < 0 for c in changes)

        if up:
            avg_move = np.mean(changes) / close.iloc[-1] * 100
            return {"type": "momentum", "direction": "long", "strength": min(1.0, avg_move / 0.1)}
        elif down:
            avg_move = abs(np.mean(changes)) / close.iloc[-1] * 100
            return {"type": "momentum", "direction": "short", "strength": min(1.0, avg_move / 0.1)}

        return {"type": "momentum", "direction": "neutral", "strength": 0.0}

    def _calc_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift(1)).abs(),
            (df["low"] - df["close"].shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(period).mean()
        return float(atr.iloc[-1]) if not np.isnan(atr.iloc[-1]) else 0
