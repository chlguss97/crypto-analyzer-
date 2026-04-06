import pandas as pd
import numpy as np
from src.engine.base import BaseIndicator


class BollingerIndicator(BaseIndicator):
    """기법 4. Bollinger Bands (스퀴즈/반전/밴드워크)"""

    @property
    def path(self) -> str:
        return "fast"

    @property
    def weight(self) -> float:
        return 2.0

    async def calculate(self, candles: pd.DataFrame, context: dict = None) -> dict:
        close = candles["close"]
        period = 20
        std_dev = 2.0

        sma = close.rolling(window=period).mean()
        std = close.rolling(window=period).std()
        upper = sma + std_dev * std
        lower = sma - std_dev * std

        last = close.iloc[-1]
        bb_upper = upper.iloc[-1]
        bb_lower = lower.iloc[-1]
        bb_mid = sma.iloc[-1]

        # BB Position (0~1)
        bb_range = bb_upper - bb_lower
        bb_position = (last - bb_lower) / bb_range if bb_range > 0 else 0.5

        # BB Width
        bb_width = bb_range / bb_mid if bb_mid > 0 else 0

        # 스퀴즈 감지: 현재 BB폭이 최근 50봉 최소의 1.1배 이내
        width_series = (upper - lower) / sma
        width_series = width_series.dropna()
        min_width_50 = width_series.tail(50).min() if len(width_series) >= 50 else width_series.min()
        is_squeeze = bb_width <= min_width_50 * 1.1 if min_width_50 > 0 else False

        # 스퀴즈 지속 봉수
        squeeze_bars = 0
        if is_squeeze:
            threshold = min_width_50 * 1.1
            for i in range(1, min(50, len(width_series))):
                if width_series.iloc[-i] <= threshold:
                    squeeze_bars += 1
                else:
                    break

        # 패턴 판별
        pattern = "none"
        direction = "neutral"
        strength = 0.0

        if is_squeeze:
            pattern = "squeeze"
            # 돌파 확인: 마지막 봉이 밴드 밖으로 나갔는지
            if last > bb_upper:
                direction = "long"
                strength = min(1.0, 0.5 + squeeze_bars * 0.05)
            elif last < bb_lower:
                direction = "short"
                strength = min(1.0, 0.5 + squeeze_bars * 0.05)
            else:
                strength = 0.3  # 아직 돌파 안 됨, 대기

        elif bb_position < 0.15:
            pattern = "mean_reversion"
            direction = "long"
            strength = min(1.0, (0.15 - bb_position) / 0.15 + 0.3)

        elif bb_position > 0.85:
            pattern = "mean_reversion"
            direction = "short"
            strength = min(1.0, (bb_position - 0.85) / 0.15 + 0.3)

        else:
            # 밴드워크 체크: 최근 3봉 연속 상단/하단 터치
            recent_upper = sum(1 for i in range(-3, 0) if close.iloc[i] >= upper.iloc[i] * 0.99)
            recent_lower = sum(1 for i in range(-3, 0) if close.iloc[i] <= lower.iloc[i] * 1.01)
            if recent_upper >= 2:
                pattern = "band_walk"
                direction = "long"
                strength = 0.6
            elif recent_lower >= 2:
                pattern = "band_walk"
                direction = "short"
                strength = 0.6

        return {
            "type": "bollinger",
            "bb_position": round(bb_position, 3),
            "bb_width": round(bb_width, 5),
            "bb_upper": round(bb_upper, 2),
            "bb_lower": round(bb_lower, 2),
            "bb_mid": round(bb_mid, 2),
            "pattern": pattern,
            "squeeze_bars": squeeze_bars,
            "is_squeeze": is_squeeze,
            "direction": direction,
            "strength": round(strength, 2),
        }
