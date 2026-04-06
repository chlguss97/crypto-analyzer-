import pandas as pd
from src.engine.base import BaseIndicator


class LongShortRatioIndicator(BaseIndicator):
    """기법 11. Long/Short Ratio (롱숏 비율)"""

    @property
    def path(self) -> str:
        return "slow"

    @property
    def weight(self) -> float:
        return 1.0

    async def calculate(self, candles: pd.DataFrame, context: dict = None) -> dict:
        ratio_account = 1.0
        ratio_position = None
        ls_history = []

        if context:
            ratio_account = context.get("ls_ratio_account", 1.0)
            ratio_position = context.get("ls_ratio_position", None)
            ls_history = context.get("ls_history", [])

        # 1시간 내 변화율
        change_1h_pct = 0.0
        if ls_history and len(ls_history) >= 2:
            prev_ratio = ls_history[-2].get("long_short_ratio_account", ratio_account)
            if prev_ratio > 0:
                change_1h_pct = (ratio_account - prev_ratio) / prev_ratio * 100

        # 고래 vs 개인 불일치
        divergence = False
        whale_direction = "neutral"
        if ratio_position is not None and ratio_account > 0:
            if ratio_position < 0.8 and ratio_account > 1.2:
                divergence = True
                whale_direction = "short"  # 고래는 숏, 개인은 롱
            elif ratio_position > 1.2 and ratio_account < 0.8:
                divergence = True
                whale_direction = "long"  # 고래는 롱, 개인은 숏

        # 역발상 시그널
        contrarian_signal = "neutral"
        if ratio_account > 2.0:
            contrarian_signal = "short"  # 롱 과밀
        elif ratio_account < 0.5:
            contrarian_signal = "long"  # 숏 과밀

        # 방향 결정: 고래 방향 우선, 없으면 역발상
        if divergence:
            direction = whale_direction
        elif contrarian_signal != "neutral":
            direction = contrarian_signal
        else:
            direction = "neutral"

        # 강도
        strength = 0.0
        if ratio_account > 2.5 or ratio_account < 0.4:
            strength = 0.8
        elif ratio_account > 2.0 or ratio_account < 0.5:
            strength = 0.5
        elif abs(change_1h_pct) > 20:
            strength = 0.6

        if divergence:
            strength = max(strength, 0.7)

        return {
            "type": "long_short_ratio",
            "ratio_account": round(ratio_account, 3),
            "ratio_position": round(ratio_position, 3) if ratio_position else None,
            "divergence": divergence,
            "whale_direction": whale_direction,
            "change_1h_pct": round(change_1h_pct, 1),
            "contrarian_signal": contrarian_signal,
            "direction": direction,
            "strength": round(strength, 2),
        }
