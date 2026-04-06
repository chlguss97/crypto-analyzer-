import pandas as pd
import numpy as np
import time
from src.engine.base import BaseIndicator


class OrderBlockIndicator(BaseIndicator):
    """기법 1. Order Block (오더블록) ★ 핵심"""

    @property
    def path(self) -> str:
        return "slow"

    @property
    def weight(self) -> float:
        return 3.0

    def _calc_atr(self, candles: pd.DataFrame, period: int = 14) -> pd.Series:
        high = candles["high"]
        low = candles["low"]
        close = candles["close"]
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ], axis=1).max(axis=1)
        return tr.rolling(window=period).mean()

    def _find_order_blocks(self, candles: pd.DataFrame, atr: pd.Series,
                           impulse_mult: float = 1.5, min_vol_ratio: float = 1.3,
                           max_age_hours: float = 24) -> list[dict]:
        """오더블록 탐지"""
        obs = []
        close = candles["close"].values
        open_ = candles["open"].values
        high = candles["high"].values
        low = candles["low"].values
        vol = candles["volume"].values
        ts = candles["timestamp"].values

        avg_vol_20 = pd.Series(vol).rolling(20).mean().values
        now_ts = ts[-1] if len(ts) > 0 else int(time.time() * 1000)
        lookback = min(100, len(candles) - 5)

        for i in range(3, lookback):
            atr_val = atr.iloc[i] if i < len(atr) and not np.isnan(atr.iloc[i]) else 0
            if atr_val == 0:
                continue

            # 임펄스 감지: i+1 ~ i+3 봉에서 ATR × 배수 이상 이동
            if i + 3 >= len(candles):
                continue

            move = close[i + 3] - close[i]
            move_abs = abs(move)

            if move_abs < atr_val * impulse_mult:
                continue

            # 임펄스 시작 직전 반대 캔들 = OB
            is_bullish_ob = move > 0 and close[i] < open_[i]  # 음봉 후 상승
            is_bearish_ob = move < 0 and close[i] > open_[i]  # 양봉 후 하락

            if not is_bullish_ob and not is_bearish_ob:
                continue

            # 거래량 비율 체크
            if avg_vol_20[i] > 0:
                vol_ratio = vol[i] / avg_vol_20[i]
            else:
                vol_ratio = 1.0

            if vol_ratio < min_vol_ratio:
                continue

            # 유효기간 체크
            age_hours = (now_ts - ts[i]) / 3_600_000
            if age_hours > max_age_hours:
                continue

            # 강도 계산
            impulse_strength = min(1.0, move_abs / (atr_val * 3))
            vol_strength = min(1.0, vol_ratio / 3)
            ob_strength = (impulse_strength * 0.6 + vol_strength * 0.4)

            ob = {
                "direction": "long" if is_bullish_ob else "short",
                "zone_low": float(low[i]),
                "zone_high": float(high[i]),
                "strength": round(ob_strength, 3),
                "vol_ratio": round(vol_ratio, 2),
                "age_hours": round(age_hours, 1),
                "timestamp": int(ts[i]),
                "retest_count": 0,
                "mitigated": False,
            }
            obs.append(ob)

        return obs

    def _check_mitigation(self, obs: list[dict], candles: pd.DataFrame) -> list[dict]:
        """OB 소진(mitigation) 체크"""
        active = []
        for ob in obs:
            ob_idx = candles["timestamp"].searchsorted(ob["timestamp"])
            after = candles.iloc[ob_idx + 1:] if ob_idx + 1 < len(candles) else pd.DataFrame()

            mitigated = False
            retest_count = 0

            for _, row in after.iterrows():
                if ob["direction"] == "long":
                    # Bullish OB: 가격이 OB 영역에 진입하면 리테스트
                    if row["low"] <= ob["zone_high"]:
                        retest_count += 1
                    # 완전히 관통하면 무효화
                    if row["close"] < ob["zone_low"]:
                        mitigated = True
                        break
                else:
                    if row["high"] >= ob["zone_low"]:
                        retest_count += 1
                    if row["close"] > ob["zone_high"]:
                        mitigated = True
                        break

            ob["retest_count"] = retest_count
            ob["mitigated"] = mitigated

            # 리테스트 2회 초과 → 제외
            if not mitigated and retest_count <= 2:
                active.append(ob)

        return active

    async def calculate(self, candles: pd.DataFrame, context: dict = None) -> dict:
        atr = self._calc_atr(candles, 14)

        # OB 탐지
        obs = self._find_order_blocks(candles, atr)
        active_obs = self._check_mitigation(obs, candles)

        if not active_obs:
            return {
                "type": "order_block",
                "direction": "neutral",
                "strength": 0.0,
                "ob_zone": None,
                "distance_pct": None,
                "htf_aligned": False,
                "retest_count": 0,
                "age_hours": 0,
                "active_count": 0,
            }

        # 현재가에 가장 가까운 OB 선택
        last_price = candles["close"].iloc[-1]
        best_ob = None
        min_dist = float("inf")

        for ob in active_obs:
            ob_mid = (ob["zone_low"] + ob["zone_high"]) / 2
            dist = abs(last_price - ob_mid) / last_price * 100
            if dist < min_dist:
                min_dist = dist
                best_ob = ob

        # 상위 TF OB 겹침 체크
        htf_aligned = False
        if context and "htf_obs" in context:
            for htf_ob in context["htf_obs"]:
                if (best_ob["zone_low"] <= htf_ob["zone_high"] and
                        best_ob["zone_high"] >= htf_ob["zone_low"]):
                    htf_aligned = True
                    best_ob["strength"] = min(1.0, best_ob["strength"] * 1.5)
                    break

        return {
            "type": "order_block",
            "direction": best_ob["direction"],
            "strength": best_ob["strength"],
            "ob_zone": [best_ob["zone_low"], best_ob["zone_high"]],
            "distance_pct": round(min_dist, 3),
            "htf_aligned": htf_aligned,
            "retest_count": best_ob["retest_count"],
            "age_hours": best_ob["age_hours"],
            "active_count": len(active_obs),
        }
