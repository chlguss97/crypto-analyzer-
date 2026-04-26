"""
Williams Fractal 지표
- 프랙탈 고점/저점으로 지지/저항 레벨 식별
- 프랙탈 돌파(Fractal Breakout) 시 추세 전환 시그널
- 멀티 TF 프랙탈 컨플루언스
"""
import pandas as pd
import numpy as np
from src.engine.base import BaseIndicator


class FractalIndicator(BaseIndicator):
    """Williams Fractal — 프랙탈 기반 지지/저항 + 돌파 시그널"""

    @property
    def path(self) -> str:
        return "fast"

    @property
    def weight(self) -> float:
        return 2.0

    def _find_fractals(self, highs: np.ndarray, lows: np.ndarray, period: int = 2) -> tuple[list, list]:
        """
        Williams Fractal 탐지
        - Fractal High: 중심봉의 고가가 좌우 period봉보다 높은 패턴
        - Fractal Low: 중심봉의 저가가 좌우 period봉보다 낮은 패턴
        """
        fractal_highs = []  # [(index, price)]
        fractal_lows = []

        for i in range(period, len(highs) - period):
            # Fractal High
            is_fh = True
            for j in range(1, period + 1):
                if highs[i] <= highs[i - j] or highs[i] <= highs[i + j]:
                    is_fh = False
                    break
            if is_fh:
                fractal_highs.append((i, highs[i]))

            # Fractal Low
            is_fl = True
            for j in range(1, period + 1):
                if lows[i] >= lows[i - j] or lows[i] >= lows[i + j]:
                    is_fl = False
                    break
            if is_fl:
                fractal_lows.append((i, lows[i]))

        return fractal_highs, fractal_lows

    async def calculate(self, candles: pd.DataFrame, context: dict = None) -> dict:
        highs = candles["high"].values
        lows = candles["low"].values
        closes = candles["close"].values
        current_price = closes[-1]

        # 멀티 스케일 프랙탈 (period 2, 3, 5)
        fh2, fl2 = self._find_fractals(highs, lows, period=2)
        fh3, fl3 = self._find_fractals(highs, lows, period=3)
        fh5, fl5 = self._find_fractals(highs, lows, period=5)

        # 최근 프랙탈 레벨 수집
        recent_resistance = []  # 저항 (프랙탈 고점)
        recent_support = []     # 지지 (프랙탈 저점)

        for fh_list, weight in [(fh2, 1.0), (fh3, 1.5), (fh5, 2.0)]:
            for idx, price in fh_list[-5:]:  # 최근 5개
                recent_resistance.append({"price": price, "index": idx, "weight": weight})

        for fl_list, weight in [(fl2, 1.0), (fl3, 1.5), (fl5, 2.0)]:
            for idx, price in fl_list[-5:]:
                recent_support.append({"price": price, "index": idx, "weight": weight})

        # 가장 가까운 지지/저항 찾기
        nearest_resistance = None
        nearest_support = None
        resistance_strength = 0
        support_strength = 0

        for r in recent_resistance:
            if r["price"] > current_price:
                dist = (r["price"] - current_price) / current_price
                if dist < 0.02 and (nearest_resistance is None or r["price"] < nearest_resistance):
                    nearest_resistance = r["price"]
                    # 여러 프랙탈이 같은 레벨에 모이면 강도 증가
                    resistance_strength += r["weight"]

        for s in recent_support:
            if s["price"] < current_price:
                dist = (current_price - s["price"]) / current_price
                if dist < 0.02 and (nearest_support is None or s["price"] > nearest_support):
                    nearest_support = s["price"]
                    support_strength += s["weight"]

        # 프랙탈 돌파 감지 (최근 3봉 내)
        breakout = "none"
        breakout_strength = 0.0

        if fh2:
            last_fh = fh2[-1][1]
            # 최근 봉이 프랙탈 고점 돌파 (i-1 접근 위해 -len(closes)+1 까지)
            for i in range(-1, max(-4, -len(closes) + 1), -1):
                if closes[i] > last_fh and closes[i - 1] <= last_fh:
                    breakout = "bullish"
                    breakout_strength = 0.7
                    # 큰 프랙탈(period 5)도 돌파하면 더 강함
                    if fh5 and closes[i] > fh5[-1][1]:
                        breakout_strength = 0.9
                    break

        if fh2 and breakout == "none":
            if fl2:
                last_fl = fl2[-1][1]
                for i in range(-1, max(-4, -len(closes) + 1), -1):
                    if closes[i] < last_fl and closes[i - 1] >= last_fl:
                        breakout = "bearish"
                        breakout_strength = 0.7
                        if fl5 and closes[i] < fl5[-1][1]:
                            breakout_strength = 0.9
                        break

        # 프랙탈 클러스터 (여러 프랙탈이 비슷한 가격대에 모이는 곳)
        cluster_zone = self._find_cluster(recent_support + recent_resistance, current_price)

        # 방향 판단
        direction = "neutral"
        strength = 0.0

        if breakout == "bullish":
            direction = "long"
            strength = breakout_strength
        elif breakout == "bearish":
            direction = "short"
            strength = breakout_strength
        elif support_strength > resistance_strength * 1.5:
            direction = "long"
            strength = min(0.6, support_strength / 10)
        elif resistance_strength > support_strength * 1.5:
            direction = "short"
            strength = min(0.6, resistance_strength / 10)

        return {
            "type": "fractal",
            "direction": direction,
            "strength": round(strength, 2),
            "breakout": breakout,
            "nearest_resistance": round(nearest_resistance, 1) if nearest_resistance else None,
            "nearest_support": round(nearest_support, 1) if nearest_support else None,
            "resistance_strength": round(resistance_strength, 2),
            "support_strength": round(support_strength, 2),
            "fractal_count": {
                "highs": len(fh2),
                "lows": len(fl2),
            },
            "cluster_zone": cluster_zone,
        }

    def _find_cluster(self, fractals: list, current_price: float, tolerance: float = 0.003) -> dict | None:
        """프랙탈 가격 클러스터 탐지 (tolerance 범위 내 3개 이상 모이면 클러스터)"""
        if not fractals:
            return None

        prices = [f["price"] for f in fractals]
        prices.sort()

        best_cluster = None
        best_count = 0

        for i, base in enumerate(prices):
            cluster_prices = [p for p in prices if abs(p - base) / base < tolerance]
            if len(cluster_prices) >= 3 and len(cluster_prices) > best_count:
                best_count = len(cluster_prices)
                avg_price = sum(cluster_prices) / len(cluster_prices)
                best_cluster = {
                    "price": round(avg_price, 1),
                    "count": len(cluster_prices),
                    "type": "support" if avg_price < current_price else "resistance",
                    "distance_pct": round(abs(avg_price - current_price) / current_price * 100, 3),
                }

        return best_cluster
