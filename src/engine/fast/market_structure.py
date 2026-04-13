import pandas as pd
import numpy as np
from src.engine.base import BaseIndicator


class MarketStructureIndicator(BaseIndicator):
    """기법 3. Market Structure (BOS / CHoCH)"""

    @property
    def path(self) -> str:
        return "fast"

    @property
    def weight(self) -> float:
        return 2.5

    def _find_swing_points(self, candles: pd.DataFrame, strength: int = 5) -> list[dict]:
        """스윙 고점/저점 탐지"""
        highs = candles["high"].values
        lows = candles["low"].values
        points = []

        for i in range(strength, len(candles) - strength):
            # 스윙 하이
            is_high = True
            for j in range(1, strength + 1):
                if highs[i] < highs[i - j] or highs[i] < highs[i + j]:
                    is_high = False
                    break
            if is_high:
                points.append({
                    "index": i,
                    "type": "high",
                    "price": highs[i],
                    "timestamp": candles["timestamp"].iloc[i],
                })

            # 스윙 로우
            is_low = True
            for j in range(1, strength + 1):
                if lows[i] > lows[i - j] or lows[i] > lows[i + j]:
                    is_low = False
                    break
            if is_low:
                points.append({
                    "index": i,
                    "type": "low",
                    "price": lows[i],
                    "timestamp": candles["timestamp"].iloc[i],
                })

        points.sort(key=lambda x: x["index"])
        return points

    def _analyze_structure(self, swing_points: list[dict]) -> dict:
        """스윙 포인트로 추세 구조 분석"""
        if len(swing_points) < 4:
            return {
                "trend": "ranging",
                "last_event": None,
                "last_event_index": None,
                "swing_high": None,
                "swing_low": None,
            }

        swing_highs = [p for p in swing_points if p["type"] == "high"]
        swing_lows = [p for p in swing_points if p["type"] == "low"]

        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return {
                "trend": "ranging",
                "last_event": None,
                "last_event_index": None,
                "swing_high": swing_highs[-1]["price"] if swing_highs else None,
                "swing_low": swing_lows[-1]["price"] if swing_lows else None,
            }

        # 최근 스윙 포인트 비교
        sh1, sh2 = swing_highs[-2], swing_highs[-1]
        sl1, sl2 = swing_lows[-2], swing_lows[-1]

        hh = sh2["price"] > sh1["price"]  # Higher High
        hl = sl2["price"] > sl1["price"]  # Higher Low
        lh = sh2["price"] < sh1["price"]  # Lower High
        ll = sl2["price"] < sl1["price"]  # Lower Low

        # 추세 판단
        trend = "ranging"
        last_event = None

        if hh and hl:
            trend = "bullish"
            last_event = "BOS_bullish"  # 상승 추세 유지
        elif ll and lh:
            trend = "bearish"
            last_event = "BOS_bearish"  # 하락 추세 유지
        elif hh and ll:
            # 04-13: HH+LL = 확장 변동성 (폭락/급등), ranging이 아님
            trend = "volatile"
        elif lh and hl:
            # 04-13: LH+HL = 수축 변동성, ranging보다는 전환 구간
            trend = "volatile"
        elif ll and hl:
            # 이전 상승 중 LL → CHoCH
            last_event = "CHoCH_bearish"
            trend = "bearish"
        elif hh and lh:
            # 이전 하락 중 HH → CHoCH
            last_event = "CHoCH_bullish"
            trend = "bullish"

        return {
            "trend": trend,
            "last_event": last_event,
            "last_event_index": swing_points[-1]["index"],
            "swing_high": sh2["price"],
            "swing_low": sl2["price"],
        }

    async def calculate(self, candles: pd.DataFrame, context: dict = None) -> dict:
        # 15m 구조 분석
        swing_points = self._find_swing_points(candles, strength=5)
        structure = self._analyze_structure(swing_points)

        # 마지막 이벤트 이후 봉 수
        last_event_bars_ago = None
        if structure["last_event_index"] is not None:
            last_event_bars_ago = len(candles) - 1 - structure["last_event_index"]

        # 상위 TF 추세 (context에서 가져옴)
        htf_trend = "unknown"
        aligned = False
        if context and "htf_trend" in context:
            htf_trend = context["htf_trend"]
            aligned = structure["trend"] == htf_trend

        # 방향
        trend = structure["trend"]
        if trend == "bullish":
            direction = "long"
        elif trend == "bearish":
            direction = "short"
        elif trend == "volatile":
            # 04-13: volatile에서도 direction/strength 반영 (H11)
            direction = "neutral"
        else:
            direction = "neutral"

        # 강도
        strength = 0.0
        if trend == "volatile":
            # HH+LL 또는 LH+HL: 변동성 확대 상태 — 방향은 중립이지만 감지됨
            strength = 0.4
        elif structure["last_event"]:
            if "BOS" in structure["last_event"]:
                strength = 0.7
                if aligned:
                    strength = 0.9
            elif "CHoCH" in structure["last_event"]:
                strength = 0.8  # CHoCH는 전환 시그널이라 강함

        return {
            "type": "market_structure",
            "trend": trend,
            "last_event": structure["last_event"],
            "last_event_bars_ago": last_event_bars_ago,
            "swing_high": structure["swing_high"],
            "swing_low": structure["swing_low"],
            "htf_trend": htf_trend,
            "aligned": aligned,
            "direction": direction,
            "strength": round(strength, 2),
        }
