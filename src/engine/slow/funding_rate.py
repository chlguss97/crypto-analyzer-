import pandas as pd
from src.engine.base import BaseIndicator


class FundingRateIndicator(BaseIndicator):
    """기법 8. Funding Rate (펀딩비) ★ 선물 전용"""

    @property
    def path(self) -> str:
        return "slow"

    @property
    def weight(self) -> float:
        return 2.0

    async def calculate(self, candles: pd.DataFrame, context: dict = None) -> dict:
        # 펀딩비 데이터는 context에서 가져옴
        current_rate = 0.0
        avg_rate_24h = 0.0
        next_settlement_min = 999
        funding_history = []

        if context:
            current_rate = context.get("funding_rate", 0.0)
            next_settlement_min = context.get("funding_next_min", 999)
            funding_history = context.get("funding_history", [])

        # 24시간 평균 펀딩비
        if funding_history:
            recent = funding_history[-3:]  # 8시간 × 3 = 24시간
            avg_rate_24h = sum(r.get("funding_rate", 0) for r in recent) / len(recent) \
                if recent else 0

        # 추세
        if len(funding_history) >= 2:
            prev = funding_history[-2].get("funding_rate", 0) if len(funding_history) >= 2 else 0
            if current_rate > prev:
                trend = "increasing"
            elif current_rate < prev:
                trend = "decreasing"
            else:
                trend = "stable"
        else:
            trend = "unknown"

        # 극단 펀딩비 판단
        extreme = abs(current_rate) > 0.0005  # 0.05%
        very_extreme = abs(current_rate) > 0.001  # 0.1%

        # 역발상 방향
        if current_rate > 0.0005:
            contrarian_direction = "short"  # 롱 과열 → 숏 기회
        elif current_rate < -0.0005:
            contrarian_direction = "long"  # 숏 과열 → 롱 기회
        else:
            contrarian_direction = "neutral"

        # 방향 + 강도
        direction = contrarian_direction
        strength = 0.0

        if very_extreme:
            strength = 0.9
        elif extreme:
            strength = 0.6
        elif abs(current_rate) > 0.0003:
            strength = 0.3
            direction = contrarian_direction

        # 정산 임박 경고 (15분 이내)
        settlement_blackout = next_settlement_min <= 15

        return {
            "type": "funding_rate",
            "current_rate": round(current_rate, 6),
            "avg_rate_24h": round(avg_rate_24h, 6),
            "trend": trend,
            "extreme": extreme,
            "very_extreme": very_extreme,
            "contrarian_direction": contrarian_direction,
            "next_settlement_min": next_settlement_min,
            "settlement_blackout": settlement_blackout,
            "direction": direction,
            "strength": round(strength, 2),
        }
