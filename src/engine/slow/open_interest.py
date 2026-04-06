import pandas as pd
from src.engine.base import BaseIndicator


class OpenInterestIndicator(BaseIndicator):
    """기법 9. Open Interest (미결제약정) ★ 선물 전용"""

    @property
    def path(self) -> str:
        return "slow"

    @property
    def weight(self) -> float:
        return 2.0

    async def calculate(self, candles: pd.DataFrame, context: dict = None) -> dict:
        # OI 데이터는 context에서 가져옴
        oi_current = 0
        oi_history = []

        if context:
            oi_current = context.get("oi_current", 0)
            oi_history = context.get("oi_history", [])

        # OI 변화율 계산
        oi_change_1h_pct = 0.0
        oi_change_24h_pct = 0.0

        if oi_history and oi_current > 0:
            # 1시간 전 대비
            if len(oi_history) >= 1:
                oi_1h_ago = oi_history[-1].get("open_interest", oi_current)
                if oi_1h_ago > 0:
                    oi_change_1h_pct = (oi_current - oi_1h_ago) / oi_1h_ago * 100

            # 24시간 전 대비
            if len(oi_history) >= 24:
                oi_24h_ago = oi_history[-24].get("open_interest", oi_current)
                if oi_24h_ago > 0:
                    oi_change_24h_pct = (oi_current - oi_24h_ago) / oi_24h_ago * 100

        # 가격 + OI 조합 해석
        price_change = 0
        if len(candles) >= 2:
            price_change = candles["close"].iloc[-1] - candles["close"].iloc[-2]

        oi_change = oi_change_1h_pct

        if price_change > 0 and oi_change > 0:
            oi_price_combo = "new_longs"      # 새 롱 유입 → 상승 강화
            direction = "long"
        elif price_change > 0 and oi_change < 0:
            oi_price_combo = "short_covering"  # 숏 청산 → 약한 상승
            direction = "long"
        elif price_change < 0 and oi_change > 0:
            oi_price_combo = "new_shorts"      # 새 숏 유입 → 하락 강화
            direction = "short"
        elif price_change < 0 and oi_change < 0:
            oi_price_combo = "long_liquidation" # 롱 청산 → 약한 하락
            direction = "short"
        else:
            oi_price_combo = "neutral"
            direction = "neutral"

        # OI 다이버전스
        divergence = False
        if len(candles) >= 5 and len(oi_history) >= 5:
            price_new_high = candles["high"].iloc[-1] >= candles["high"].tail(20).max()
            oi_decreasing = oi_change_1h_pct < -1
            if price_new_high and oi_decreasing:
                divergence = True
                direction = "short"  # 가짜 돌파 경고

        # OI 급증
        oi_spike = abs(oi_change_1h_pct) > 5

        # 강도
        strength = 0.0
        if oi_spike:
            strength = 0.8
        elif abs(oi_change_1h_pct) > 3:
            strength = 0.6
        elif oi_price_combo in ("new_longs", "new_shorts"):
            strength = 0.5
        elif oi_price_combo in ("short_covering", "long_liquidation"):
            strength = 0.3

        if divergence:
            strength = max(strength, 0.7)

        return {
            "type": "open_interest",
            "oi_current": oi_current,
            "oi_change_1h_pct": round(oi_change_1h_pct, 2),
            "oi_change_24h_pct": round(oi_change_24h_pct, 2),
            "oi_price_combo": oi_price_combo,
            "divergence": divergence,
            "oi_spike": oi_spike,
            "direction": direction,
            "strength": round(strength, 2),
        }
