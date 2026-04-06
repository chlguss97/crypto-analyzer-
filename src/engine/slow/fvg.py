import pandas as pd
import time
from src.engine.base import BaseIndicator


class FVGIndicator(BaseIndicator):
    """기법 2. Fair Value Gap"""

    @property
    def path(self) -> str:
        return "slow"

    @property
    def weight(self) -> float:
        return 1.5

    def _find_fvgs(self, candles: pd.DataFrame, min_gap_pct: float = 0.12,
                   max_age_hours: float = 12) -> list[dict]:
        """FVG 탐지"""
        fvgs = []
        high = candles["high"].values
        low = candles["low"].values
        close = candles["close"].values
        ts = candles["timestamp"].values
        now_ts = ts[-1] if len(ts) > 0 else int(time.time() * 1000)

        for i in range(2, len(candles) - 1):
            mid_price = (high[i - 1] + low[i - 1]) / 2
            if mid_price == 0:
                continue

            # Bullish FVG: 봉1 고가 < 봉3 저가
            if high[i - 2] < low[i]:
                gap_size = low[i] - high[i - 2]
                gap_pct = gap_size / mid_price * 100
                if gap_pct >= min_gap_pct:
                    age = (now_ts - ts[i]) / 3_600_000
                    if age <= max_age_hours:
                        fvgs.append({
                            "direction": "long",
                            "gap_zone": [float(high[i - 2]), float(low[i])],
                            "gap_size_pct": round(gap_pct, 3),
                            "age_hours": round(age, 1),
                            "timestamp": int(ts[i]),
                            "filled_pct": 0.0,
                        })

            # Bearish FVG: 봉1 저가 > 봉3 고가
            if low[i - 2] > high[i]:
                gap_size = low[i - 2] - high[i]
                gap_pct = gap_size / mid_price * 100
                if gap_pct >= min_gap_pct:
                    age = (now_ts - ts[i]) / 3_600_000
                    if age <= max_age_hours:
                        fvgs.append({
                            "direction": "short",
                            "gap_zone": [float(high[i]), float(low[i - 2])],
                            "gap_size_pct": round(gap_pct, 3),
                            "age_hours": round(age, 1),
                            "timestamp": int(ts[i]),
                            "filled_pct": 0.0,
                        })

        return fvgs

    def _check_fill(self, fvgs: list[dict], candles: pd.DataFrame) -> list[dict]:
        """FVG 채워진 비율 체크"""
        active = []
        for fvg in fvgs:
            fvg_idx = candles["timestamp"].searchsorted(fvg["timestamp"])
            after = candles.iloc[fvg_idx + 1:] if fvg_idx + 1 < len(candles) else pd.DataFrame()

            gap_low, gap_high = fvg["gap_zone"]
            gap_size = gap_high - gap_low
            max_fill = 0.0

            for _, row in after.iterrows():
                if fvg["direction"] == "long":
                    # Bullish FVG: 가격이 위에서 내려와 채움
                    if row["low"] <= gap_high:
                        penetration = gap_high - max(row["low"], gap_low)
                        fill = penetration / gap_size if gap_size > 0 else 1.0
                        max_fill = max(max_fill, min(1.0, fill))
                else:
                    if row["high"] >= gap_low:
                        penetration = min(row["high"], gap_high) - gap_low
                        fill = penetration / gap_size if gap_size > 0 else 1.0
                        max_fill = max(max_fill, min(1.0, fill))

            fvg["filled_pct"] = round(max_fill, 2)

            # 100% 채워지면 무효화
            if max_fill < 1.0:
                active.append(fvg)

        return active

    async def calculate(self, candles: pd.DataFrame, context: dict = None) -> dict:
        fvgs = self._find_fvgs(candles)
        active_fvgs = self._check_fill(fvgs, candles)

        if not active_fvgs:
            return {
                "type": "fvg",
                "direction": "neutral",
                "gap_zone": None,
                "gap_size_pct": 0,
                "filled_pct": 0,
                "overlaps_ob": False,
                "age_hours": 0,
                "strength": 0.0,
                "active_count": 0,
            }

        # 현재가에 가장 가까운 FVG
        last_price = candles["close"].iloc[-1]
        best_fvg = min(active_fvgs,
                       key=lambda f: abs(last_price - sum(f["gap_zone"]) / 2))

        # OB 겹침 체크
        overlaps_ob = False
        if context and "ob_zones" in context:
            for ob_zone in context["ob_zones"]:
                if (best_fvg["gap_zone"][0] <= ob_zone[1] and
                        best_fvg["gap_zone"][1] >= ob_zone[0]):
                    overlaps_ob = True
                    break

        # 강도
        strength = min(1.0, best_fvg["gap_size_pct"] / 0.5) * 0.5
        if overlaps_ob:
            strength = min(1.0, strength + 0.4)  # Golden Zone 보너스
        unfilled = 1.0 - best_fvg["filled_pct"]
        strength *= unfilled

        distance = abs(last_price - sum(best_fvg["gap_zone"]) / 2) / last_price * 100

        return {
            "type": "fvg",
            "direction": best_fvg["direction"],
            "gap_zone": best_fvg["gap_zone"],
            "gap_size_pct": best_fvg["gap_size_pct"],
            "filled_pct": best_fvg["filled_pct"],
            "overlaps_ob": overlaps_ob,
            "age_hours": best_fvg["age_hours"],
            "distance_pct": round(distance, 3),
            "strength": round(strength, 2),
            "active_count": len(active_fvgs),
        }
