import pandas as pd
import numpy as np
from src.engine.base import BaseIndicator


class CVDIndicator(BaseIndicator):
    """기법 12. CVD (Cumulative Volume Delta)"""

    @property
    def path(self) -> str:
        return "slow"

    @property
    def weight(self) -> float:
        return 1.5

    async def calculate(self, candles: pd.DataFrame, context: dict = None) -> dict:
        # CVD 데이터는 WebSocket에서 실시간 계산 → context로 전달
        cvd_15m = 0.0
        cvd_1h = 0.0

        if context:
            cvd_15m = context.get("cvd_15m", 0.0)
            cvd_1h = context.get("cvd_1h", 0.0)

        # CVD 추세 (기울기)
        if cvd_15m > 0:
            cvd_trend = "rising"
        elif cvd_15m < 0:
            cvd_trend = "falling"
        else:
            cvd_trend = "flat"

        # CVD 기울기 정규화
        close = candles["close"]
        vol = candles["volume"]
        avg_vol = vol.tail(20).mean()
        cvd_slope = cvd_15m / avg_vol if avg_vol > 0 else 0

        # 가격-CVD 다이버전스
        price_change = close.iloc[-1] - close.iloc[-5] if len(close) >= 5 else 0
        price_cvd_divergence = False
        divergence_type = None

        if price_change > 0 and cvd_15m < 0:
            price_cvd_divergence = True
            divergence_type = "bearish"  # 가격↑ + CVD↓ = 약한 상승
        elif price_change < 0 and cvd_15m > 0:
            price_cvd_divergence = True
            divergence_type = "bullish"  # 가격↓ + CVD↑ = 약한 하락

        # 방향
        if divergence_type == "bullish":
            direction = "long"
        elif divergence_type == "bearish":
            direction = "short"
        elif cvd_trend == "rising":
            direction = "long"
        elif cvd_trend == "falling":
            direction = "short"
        else:
            direction = "neutral"

        # 강도
        strength = 0.0
        if price_cvd_divergence:
            strength = 0.7
        elif abs(cvd_slope) > 0.5:
            strength = 0.5
        elif abs(cvd_slope) > 0.2:
            strength = 0.3

        return {
            "type": "cvd",
            "cvd_15m": round(cvd_15m, 4),
            "cvd_1h": round(cvd_1h, 4),
            "cvd_trend": cvd_trend,
            "cvd_slope": round(cvd_slope, 4),
            "price_cvd_divergence": price_cvd_divergence,
            "divergence_type": divergence_type,
            "direction": direction,
            "strength": round(strength, 2),
        }
