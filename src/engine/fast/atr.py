import pandas as pd
import numpy as np
from src.engine.base import BaseIndicator


class ATRIndicator(BaseIndicator):
    """기법 13. ATR 기반 동적 SL/TP + 레버리지 산정"""

    @property
    def path(self) -> str:
        return "fast"

    @property
    def weight(self) -> float:
        return 0.0  # 직접 시그널 가중치 없음 (SL/TP/레버리지 산정용)

    async def calculate(self, candles: pd.DataFrame, context: dict = None) -> dict:
        high = candles["high"]
        low = candles["low"]
        close = candles["close"]

        # True Range
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ], axis=1).max(axis=1)

        atr_14 = tr.rolling(window=14).mean()
        last_atr = atr_14.iloc[-1]
        last_price = close.iloc[-1]

        # ATR %
        atr_pct = last_atr / last_price * 100 if last_price > 0 else 0

        # SL 산정: ATR × 1.2
        sl_multiplier = 1.2
        sl_distance = last_atr * sl_multiplier
        sl_pct = sl_distance / last_price * 100

        # SL 범위 제한
        sl_pct = max(0.3, min(1.5, sl_pct))

        # TP 산정
        risk_distance = sl_distance
        tp1_distance = risk_distance * 1.5  # 1.5R
        tp2_distance = risk_distance * 2.5  # 2.5R

        # 변동성 구간 판단
        if atr_pct < 0.2:
            volatility = "low"
        elif atr_pct < 0.4:
            volatility = "medium"
        else:
            volatility = "high"

        return {
            "type": "atr",
            "atr_14": round(last_atr, 2),
            "atr_pct": round(atr_pct, 4),
            "sl_distance": round(sl_distance, 2),
            "sl_pct": round(sl_pct, 4),
            "tp1_distance": round(tp1_distance, 2),
            "tp2_distance": round(tp2_distance, 2),
            "volatility": volatility,
            "direction": "neutral",
            "strength": 0.0,
        }
