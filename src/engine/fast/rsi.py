import pandas as pd
import numpy as np
from src.engine.base import BaseIndicator


class RSIIndicator(BaseIndicator):
    """기법 6. RSI + 다이버전스"""

    @property
    def path(self) -> str:
        return "fast"

    @property
    def weight(self) -> float:
        return 1.5

    def _calc_rsi(self, close: pd.Series, period: int) -> pd.Series:
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        # 0으로 나누기 방지 + inf 처리
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        # avg_loss가 0이면 RSI는 100 (강한 상승)
        rsi = rsi.fillna(100).clip(0, 100)
        return rsi

    def _find_swing_points(self, series: pd.Series, strength: int = 5) -> list[tuple]:
        """스윙 고점/저점 찾기 → (index, value, 'high'|'low')"""
        points = []
        for i in range(strength, len(series) - strength):
            # 스윙 하이
            if all(series.iloc[i] >= series.iloc[i - j] for j in range(1, strength + 1)) and \
               all(series.iloc[i] >= series.iloc[i + j] for j in range(1, strength + 1)):
                points.append((i, series.iloc[i], "high"))
            # 스윙 로우
            if all(series.iloc[i] <= series.iloc[i - j] for j in range(1, strength + 1)) and \
               all(series.iloc[i] <= series.iloc[i + j] for j in range(1, strength + 1)):
                points.append((i, series.iloc[i], "low"))
        return points

    def _detect_divergence(self, close: pd.Series, rsi: pd.Series) -> str | None:
        """다이버전스 감지"""
        price_points = self._find_swing_points(close, strength=3)
        rsi_points = self._find_swing_points(rsi, strength=3)

        if len(price_points) < 2 or len(rsi_points) < 2:
            return None

        # 최근 로우 2개 비교 (Bullish Divergence)
        price_lows = [(i, v) for i, v, t in price_points if t == "low"]
        rsi_lows = [(i, v) for i, v, t in rsi_points if t == "low"]

        if len(price_lows) >= 2 and len(rsi_lows) >= 2:
            p1, p2 = price_lows[-2], price_lows[-1]
            r1, r2 = rsi_lows[-2], rsi_lows[-1]
            # 가격 LL + RSI HL = Bullish Divergence
            if p2[1] < p1[1] and r2[1] > r1[1] and abs(p2[0] - p1[0]) >= 5:
                return "bullish"
            # 가격 HL + RSI LL = Hidden Bullish
            if p2[1] > p1[1] and r2[1] < r1[1] and abs(p2[0] - p1[0]) >= 5:
                return "hidden_bullish"

        # 최근 하이 2개 비교 (Bearish Divergence)
        price_highs = [(i, v) for i, v, t in price_points if t == "high"]
        rsi_highs = [(i, v) for i, v, t in rsi_points if t == "high"]

        if len(price_highs) >= 2 and len(rsi_highs) >= 2:
            p1, p2 = price_highs[-2], price_highs[-1]
            r1, r2 = rsi_highs[-2], rsi_highs[-1]
            # 가격 HH + RSI LH = Bearish Divergence
            if p2[1] > p1[1] and r2[1] < r1[1] and abs(p2[0] - p1[0]) >= 5:
                return "bearish"
            # 가격 LH + RSI HH = Hidden Bearish
            if p2[1] < p1[1] and r2[1] > r1[1] and abs(p2[0] - p1[0]) >= 5:
                return "hidden_bearish"

        return None

    async def calculate(self, candles: pd.DataFrame, context: dict = None) -> dict:
        close = candles["close"]

        rsi_14 = self._calc_rsi(close, 14)
        rsi_7 = self._calc_rsi(close, 7)

        r14 = rsi_14.iloc[-1]
        r7 = rsi_7.iloc[-1]

        # 과매수/과매도 영역
        if r14 < 20:
            zone = "extreme_oversold"
        elif r14 < 30:
            zone = "oversold"
        elif r14 > 80:
            zone = "extreme_overbought"
        elif r14 > 70:
            zone = "overbought"
        else:
            zone = "neutral"

        # 다이버전스 감지
        divergence = self._detect_divergence(close, rsi_14)

        # BB+RSI 콤보 체크
        bb_rsi_combo = False
        if context and "bb_position" in context:
            bb_pos = context["bb_position"]
            if bb_pos < 0.15 and r14 < 30:
                bb_rsi_combo = True
            elif bb_pos > 0.85 and r14 > 70:
                bb_rsi_combo = True

        # 방향 + 강도
        if zone in ("oversold", "extreme_oversold") or divergence in ("bullish", "hidden_bullish"):
            direction = "long"
            strength = min(1.0, (70 - r14) / 40) if r14 < 50 else 0.5
        elif zone in ("overbought", "extreme_overbought") or divergence in ("bearish", "hidden_bearish"):
            direction = "short"
            strength = min(1.0, (r14 - 30) / 40) if r14 > 50 else 0.5
        else:
            direction = "neutral"
            strength = 0.0

        # 다이버전스 있으면 강도 보너스
        if divergence:
            strength = min(1.0, strength + 0.3)

        return {
            "type": "rsi",
            "rsi_14": round(r14, 1),
            "rsi_7": round(r7, 1),
            "zone": zone,
            "divergence": divergence,
            "divergence_strength": round(strength, 2) if divergence else 0,
            "bb_rsi_combo": bb_rsi_combo,
            "direction": direction,
            "strength": round(strength, 2),
        }
