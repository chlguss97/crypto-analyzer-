import pandas as pd
from src.engine.base import BaseIndicator


class LiquidationIndicator(BaseIndicator):
    """기법 10. Liquidation Level (청산 구간) ★ 선물 전용"""

    @property
    def path(self) -> str:
        return "slow"

    @property
    def weight(self) -> float:
        return 1.5

    def _estimate_liquidation_levels(self, current_price: float) -> dict:
        """주요 레버리지별 청산가 추정"""
        levels = {}
        for lev in [10, 25, 50, 100]:
            levels[f"long_{lev}x"] = current_price * (1 - 1 / lev)
            levels[f"short_{lev}x"] = current_price * (1 + 1 / lev)
        return levels

    async def calculate(self, candles: pd.DataFrame, context: dict = None) -> dict:
        last_price = candles["close"].iloc[-1]

        # 청산가 추정
        liq_levels = self._estimate_liquidation_levels(last_price)

        # 현재가 ±5% 이내 청산 밀집대 추정
        # 실제로는 OI 분포 데이터가 필요하지만, 가격 구간으로 추정
        range_pct = 0.05
        upper_bound = last_price * (1 + range_pct)
        lower_bound = last_price * (1 - range_pct)

        # 최근 가격 분포로 밀집대 추정
        # 많이 거래된 가격대 = 포지션 밀집 = 청산 밀집 가능성
        highs = candles["high"].tail(50)
        lows = candles["low"].tail(50)

        # 위쪽 숏 청산 밀집도 (최근 고점 빈도)
        upper_resistance = highs[highs >= last_price].count() / max(len(highs), 1)
        # 아래쪽 롱 청산 밀집도 (최근 저점 빈도)
        lower_support = lows[lows <= last_price].count() / max(len(lows), 1)

        # 가장 가까운 청산 구간
        nearest_long_liq = last_price * (1 - 1 / 25)   # 25x 롱 청산가
        nearest_short_liq = last_price * (1 + 1 / 25)  # 25x 숏 청산가

        # 자석 방향: 밀집도 높은 쪽
        if upper_resistance > lower_support:
            magnet_direction = "up"
        elif lower_support > upper_resistance:
            magnet_direction = "down"
        else:
            magnet_direction = "neutral"

        # 캐스케이드 리스크
        oi_spike = False
        if context and "oi_spike" in context:
            oi_spike = context["oi_spike"]

        if oi_spike:
            cascade_risk = "high"
        elif abs(upper_resistance - lower_support) > 0.3:
            cascade_risk = "moderate"
        else:
            cascade_risk = "low"

        # 방향: 자석 방향 = 가격이 끌려갈 방향
        direction = "neutral"
        strength = 0.0
        if magnet_direction == "up":
            direction = "long"
            strength = min(1.0, upper_resistance + 0.2)
        elif magnet_direction == "down":
            direction = "short"
            strength = min(1.0, lower_support + 0.2)

        if cascade_risk == "high":
            strength = min(1.0, strength + 0.3)

        # 가장 가까운 청산대까지 거리
        dist_long = abs(last_price - nearest_long_liq) / last_price * 100
        dist_short = abs(nearest_short_liq - last_price) / last_price * 100
        distance_nearest = min(dist_long, dist_short)

        return {
            "type": "liquidation",
            "nearest_long_liq_zone": round(nearest_long_liq, 2),
            "nearest_short_liq_zone": round(nearest_short_liq, 2),
            "long_liq_density": round(lower_support, 2),
            "short_liq_density": round(upper_resistance, 2),
            "cascade_risk": cascade_risk,
            "magnet_direction": magnet_direction,
            "distance_to_nearest_pct": round(distance_nearest, 2),
            "direction": direction,
            "strength": round(strength, 2),
        }
