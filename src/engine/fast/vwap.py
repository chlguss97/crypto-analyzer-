import pandas as pd
import numpy as np
from src.engine.base import BaseIndicator


class VWAPIndicator(BaseIndicator):
    """기법 14. VWAP (거래량가중평균가)"""

    @property
    def path(self) -> str:
        return "fast"

    @property
    def weight(self) -> float:
        return 1.0

    async def calculate(self, candles: pd.DataFrame, context: dict = None) -> dict:
        # 세션 VWAP 계산 (24시간 단위)
        # typical price = (H + L + C) / 3
        tp = (candles["high"] + candles["low"] + candles["close"]) / 3
        vol = candles["volume"]

        # 세션 구분: UTC 00:00 기준 (timestamp → 일자)
        candles_ts = candles["timestamp"]
        day_ms = 86_400_000
        current_day = candles_ts.iloc[-1] // day_ms

        # 당일 데이터만 추출
        session_mask = candles_ts // day_ms == current_day
        session_tp = tp[session_mask]
        session_vol = vol[session_mask]

        if session_vol.sum() > 0:
            session_vwap = (session_tp * session_vol).sum() / session_vol.sum()
        else:
            session_vwap = tp.iloc[-1]

        # 주간 VWAP (최근 7일)
        week_start = current_day - 7
        week_mask = candles_ts // day_ms >= week_start
        week_tp = tp[week_mask]
        week_vol = vol[week_mask]

        if week_vol.sum() > 0:
            weekly_vwap = (week_tp * week_vol).sum() / week_vol.sum()
        else:
            weekly_vwap = session_vwap

        last = candles["close"].iloc[-1]

        # VWAP 대비 거리
        dist_pct = (last - session_vwap) / session_vwap * 100 if session_vwap > 0 else 0

        # 최근 3봉 내 VWAP 터치 여부
        touch_recent = False
        for i in range(-3, 0):
            if len(candles) > abs(i):
                low = candles["low"].iloc[i]
                high = candles["high"].iloc[i]
                if low <= session_vwap <= high:
                    touch_recent = True
                    break

        # 방향
        if last > session_vwap:
            direction = "long"
            price_vs = "above"
        elif last < session_vwap:
            direction = "short"
            price_vs = "below"
        else:
            direction = "neutral"
            price_vs = "at"

        # 강도: VWAP 터치 + 방향 일치 시 높음
        strength = 0.3
        if touch_recent:
            strength = 0.6
        if abs(dist_pct) > 1.0:
            strength = max(strength, 0.5)

        return {
            "type": "vwap",
            "session_vwap": round(session_vwap, 2),
            "weekly_vwap": round(weekly_vwap, 2),
            "price_vs_vwap": price_vs,
            "dist_pct": round(dist_pct, 3),
            "touch_recent": touch_recent,
            "direction": direction,
            "strength": round(strength, 2),
        }
