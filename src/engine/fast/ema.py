import pandas as pd
from src.engine.base import BaseIndicator


class EMAIndicator(BaseIndicator):
    """기법 5. EMA 다중 정배열/역배열"""

    @property
    def path(self) -> str:
        return "fast"

    @property
    def weight(self) -> float:
        return 1.0

    async def calculate(self, candles: pd.DataFrame, context: dict = None) -> dict:
        close = candles["close"]

        ema9 = close.ewm(span=9, adjust=False).mean()
        ema21 = close.ewm(span=21, adjust=False).mean()
        ema50 = close.ewm(span=50, adjust=False).mean()
        ema200 = close.ewm(span=200, adjust=False).mean()

        last = close.iloc[-1]
        e9, e21, e50, e200 = ema9.iloc[-1], ema21.iloc[-1], ema50.iloc[-1], ema200.iloc[-1]

        # 정배열 점수
        bullish_pairs = sum([
            e9 > e21,
            e21 > e50,
            e50 > e200,
            last > e9,
        ])
        bearish_pairs = sum([
            e9 < e21,
            e21 < e50,
            e50 < e200,
            last < e9,
        ])

        if bullish_pairs >= 3:
            alignment = "bullish"
            alignment_score = bullish_pairs / 4
        elif bearish_pairs >= 3:
            alignment = "bearish"
            alignment_score = bearish_pairs / 4
        else:
            alignment = "mixed"
            alignment_score = 0.0

        # EMA50 기울기 (최근 5봉 대비)
        ema50_slope = 0.0
        if len(ema50) >= 6:
            ema50_slope = (ema50.iloc[-1] - ema50.iloc[-6]) / ema50.iloc[-6] * 100

        # EMA 크로스 감지
        recent_cross = None
        cross_bars_ago = None
        for i in range(min(10, len(ema9) - 1), 0, -1):
            idx = -i
            prev = -(i + 1)
            if len(ema9) > abs(prev):
                if ema9.iloc[prev] <= ema21.iloc[prev] and ema9.iloc[idx] > ema21.iloc[idx]:
                    recent_cross = "golden_9_21"
                    cross_bars_ago = i
                    break
                elif ema9.iloc[prev] >= ema21.iloc[prev] and ema9.iloc[idx] < ema21.iloc[idx]:
                    recent_cross = "death_9_21"
                    cross_bars_ago = i
                    break

        # 방향 결정
        if alignment == "bullish":
            direction = "long"
        elif alignment == "bearish":
            direction = "short"
        else:
            direction = "neutral"

        # EMA200 대비 거리
        distance_ema200 = abs(last - e200) / e200 * 100

        return {
            "type": "ema",
            "alignment": alignment,
            "alignment_score": alignment_score,
            "ema9": round(e9, 2),
            "ema21": round(e21, 2),
            "ema50": round(e50, 2),
            "ema200": round(e200, 2),
            "ema50_slope": round(ema50_slope, 4),
            "recent_cross": recent_cross,
            "cross_bars_ago": cross_bars_ago,
            "price_vs_ema50": "above" if last > e50 else "below",
            "distance_ema200_pct": round(distance_ema200, 2),
            "direction": direction,
            "strength": alignment_score,
        }
