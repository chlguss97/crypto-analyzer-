import pandas as pd
import numpy as np
from src.engine.base import BaseIndicator


class VolumePatternIndicator(BaseIndicator):
    """기법 7. 거래량 패턴 분석"""

    @property
    def path(self) -> str:
        return "slow"

    @property
    def weight(self) -> float:
        return 1.5

    async def calculate(self, candles: pd.DataFrame, context: dict = None) -> dict:
        vol = candles["volume"]
        close = candles["close"]
        high = candles["high"]
        low = candles["low"]

        avg_vol_20 = vol.rolling(20).mean()
        last_vol = vol.iloc[-1]
        last_avg = avg_vol_20.iloc[-1]

        # 스파이크 비율
        spike_ratio = last_vol / last_avg if last_avg > 0 else 1.0

        # 패턴 판별
        pattern = "normal"
        direction = "neutral"
        strength = 0.0

        # [A] 볼륨 스파이크
        if spike_ratio > 2.0:
            pattern = "spike"
            last_candle_dir = close.iloc[-1] - close.iloc[-2]
            if last_candle_dir > 0:
                direction = "long"
            else:
                direction = "short"
            strength = min(1.0, spike_ratio / 5.0)

        # [B] 볼륨 드라이업 (3봉 연속 감소 + 횡보)
        elif len(vol) >= 4:
            decreasing = all(vol.iloc[-i] < vol.iloc[-i - 1] for i in range(1, 4))
            price_range = (high.tail(3).max() - low.tail(3).min()) / close.iloc[-1] * 100
            if decreasing and price_range < 0.3:
                pattern = "dryup"
                direction = "neutral"  # 방향은 돌파 후 결정
                strength = 0.5

        # [C] 볼륨 다이버전스
        if pattern == "normal" and len(vol) >= 10:
            # 가격 신고가 + 거래량 감소
            price_high_5 = high.tail(5).max()
            price_high_prev = high.iloc[-10:-5].max()
            vol_avg_5 = vol.tail(5).mean()
            vol_avg_prev = vol.iloc[-10:-5].mean()

            if price_high_5 > price_high_prev and vol_avg_5 < vol_avg_prev * 0.8:
                pattern = "divergence"
                direction = "short"  # 약한 돌파 경고
                strength = 0.6

            price_low_5 = low.tail(5).min()
            price_low_prev = low.iloc[-10:-5].min()
            if price_low_5 < price_low_prev and vol_avg_5 < vol_avg_prev * 0.8:
                pattern = "divergence"
                direction = "long"  # 매도 소진
                strength = 0.6

        # [D] 클라이맥스 볼륨 (극단 거래량 + 긴 꼬리)
        if spike_ratio > 3.0 and len(candles) >= 1:
            body = abs(close.iloc[-1] - candles["open"].iloc[-1])
            full_range = high.iloc[-1] - low.iloc[-1]
            if full_range > 0 and body / full_range < 0.3:  # 긴 꼬리
                pattern = "climax"
                # 클라이맥스 = 추세 종료 → 반대 방향
                direction = "short" if close.iloc[-1] > close.iloc[-2] else "long"
                strength = 0.8

        # Taker Buy Ratio (context에서 가져옴)
        taker_buy_ratio = 0.5
        if context and "taker_buy_ratio" in context:
            taker_buy_ratio = context["taker_buy_ratio"]

        # 추세 확인: 가격 방향 + 거래량 증가 = 확인
        trend_confirm = False
        if spike_ratio > 1.5:
            price_dir = close.iloc[-1] - close.iloc[-3]
            if (price_dir > 0 and taker_buy_ratio > 0.55) or \
               (price_dir < 0 and taker_buy_ratio < 0.45):
                trend_confirm = True

        return {
            "type": "volume",
            "spike_ratio": round(spike_ratio, 2),
            "pattern": pattern,
            "taker_buy_ratio": round(taker_buy_ratio, 3),
            "trend_confirm": trend_confirm,
            "direction": direction,
            "strength": round(strength, 2),
        }
