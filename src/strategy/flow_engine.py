"""
FlowEngine — 단순 오더플로우 엔진.

규칙 3개:
  1. 큰 추세 방향으로만 (1d/4h EMA)
  2. 주요 레벨에서 플로우 확인되면 진입
  3. SL 타이트, TP는 러너

복잡한 점수/등급 없음. 조건 전부 충족 = 진입, 하나라도 미달 = 패스.
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class FlowEngine:

    def __init__(self, redis=None, flow_ml=None):
        self.redis = redis
        self.flow_ml = flow_ml  # FlowML 인스턴스 (None이면 ML 없이 동작)

    async def analyze(self, df_1m, df_5m, df_15m=None, df_1h=None,
                      df_4h=None, df_1d=None, rt_velocity=None) -> dict:
        """
        Returns:
            {
                "setup": "FLOW" | None,
                "direction": "long" | "short" | "neutral",
                "score": float (확신도 5~10),
                "hold_mode": "standard",
                "sl_distance": float,
                "tp_distance": float,
                "signals": dict,
                "reason": str,
                "atr": float,
                "atr_pct": float,
            }
        """
        result = {
            "setup": None, "direction": "neutral", "score": 0,
            "hold_mode": "standard", "sl_distance": 0, "tp_distance": 0,
            "signals": {}, "reason": "no_signal", "atr": 0, "atr_pct": 0,
        }

        if df_5m is None or len(df_5m) < 30:
            return result

        price = float(df_5m["close"].iloc[-1])
        atr = self._atr(df_5m, 14)
        atr_pct = atr / price * 100 if price > 0 else 0.3
        result["atr"] = round(atr, 2)
        result["atr_pct"] = round(atr_pct, 4)

        # ══════════════════════════════════════
        # 1단계: 큰 추세 — 1d/4h (방향 결정)
        # ══════════════════════════════════════
        trend_1d = self._ema_trend(df_1d)
        trend_4h = self._ema_trend(df_4h)

        # 일봉 우선, 일봉 neutral이면 4시간봉
        if trend_1d != "neutral":
            big_trend = trend_1d
        elif trend_4h != "neutral":
            big_trend = trend_4h
        else:
            result["reason"] = "no_big_trend"
            result["signals"]["trend_1d"] = trend_1d
            result["signals"]["trend_4h"] = trend_4h
            return result

        result["signals"]["trend_1d"] = trend_1d
        result["signals"]["trend_4h"] = trend_4h
        result["signals"]["big_trend"] = big_trend

        # ══════════════════════════════════════
        # 2단계: 주요 레벨 — 4h/1h 지지/저항
        # ══════════════════════════════════════
        levels = self._find_key_levels(df_4h, df_1h, price)
        result["signals"]["levels"] = levels

        # 가격이 레벨 근처인지 (ATR × 1.0 이내)
        near_support = False
        near_resistance = False
        nearest_support = None
        nearest_resistance = None

        for lv in levels.get("supports", []):
            if abs(price - lv["price"]) <= atr * 1.5:
                near_support = True
                nearest_support = lv
                break

        for lv in levels.get("resistances", []):
            if abs(price - lv["price"]) <= atr * 1.5:
                near_resistance = True
                nearest_resistance = lv
                break

        result["signals"]["near_support"] = near_support
        result["signals"]["near_resistance"] = near_resistance

        # 추세 방향 + 레벨 매칭
        # 상승 추세 → 지지선 근처에서 롱 / 하락 추세 → 저항선 근처에서 숏
        if big_trend == "up" and not near_support:
            result["reason"] = f"uptrend_but_not_near_support"
            return result
        if big_trend == "down" and not near_resistance:
            result["reason"] = f"downtrend_but_not_near_resistance"
            return result

        direction = "long" if big_trend == "up" else "short"

        # ══════════════════════════════════════
        # 3단계: 오더플로우 확인 — CVD + 고래 + 청산
        # ══════════════════════════════════════
        flow = await self._check_flow(direction)
        result["signals"]["flow"] = flow

        if not flow["confirmed"]:
            result["reason"] = f"flow_not_confirmed: {flow['reason']}"
            result["signals"]["direction_candidate"] = direction
            return result

        # ══════════════════════════════════════
        # 모든 조건 충족 — 진입
        # ══════════════════════════════════════

        # SL: 레벨 너머 (지지선 아래 or 저항선 위)
        if direction == "long" and nearest_support:
            sl_price = nearest_support["price"] - atr * 0.5
            sl_dist = price - sl_price
            # TP: 가장 가까운 저항선
            tp_dist = sl_dist * 2.0  # 기본 RR 2.0
            if levels.get("resistances"):
                nearest_r = levels["resistances"][0]["price"]
                if nearest_r > price:
                    tp_dist = max(tp_dist, nearest_r - price)
        elif direction == "short" and nearest_resistance:
            sl_price = nearest_resistance["price"] + atr * 0.5
            sl_dist = sl_price - price
            tp_dist = sl_dist * 2.0
            if levels.get("supports"):
                nearest_s = levels["supports"][0]["price"]
                if nearest_s < price:
                    tp_dist = max(tp_dist, price - nearest_s)
        else:
            sl_dist = atr * 2.0
            tp_dist = atr * 4.0

        # 최소 SL
        sl_dist = max(sl_dist, price * 0.003)
        # 최소 RR 1.5
        tp_dist = max(tp_dist, sl_dist * 1.5)

        # 확신도: 플로우 강도 + 추세 일치도
        conviction = 6.0
        if trend_1d == trend_4h:
            conviction += 1.5  # 1d + 4h 동의
        if flow.get("strength", 0) > 0.5:
            conviction += 1.0  # 강한 플로우
        if flow.get("whale_confirm", False):
            conviction += 1.0  # 고래 확인
        if flow.get("liquidation_confirm", False):
            conviction += 0.5  # 청산 캐스케이드 정리
        conviction = min(10.0, conviction)

        result.update({
            "setup": "FLOW",
            "direction": direction,
            "score": round(conviction, 1),
            "sl_distance": round(sl_dist, 1),
            "tp_distance": round(tp_dist, 1),
            "reason": (
                f"{big_trend}_trend | "
                f"{'support' if direction == 'long' else 'resistance'} "
                f"${nearest_support['price'] if nearest_support and direction == 'long' else nearest_resistance['price'] if nearest_resistance else 0:,.0f} | "
                f"flow={flow['direction']} str={flow.get('strength', 0):.2f}"
            ),
        })

        # ═══ ML 보정 (학습 데이터 충분하면) ═══
        if self.flow_ml:
            ml = self.flow_ml.predict(result)
            result["signals"]["ml"] = ml
            if ml["trained"]:
                raw_score = result["score"]
                result["score"] = round(max(0, min(10, raw_score + ml["ml_score"])), 1)
                result["reason"] += f" | ML={ml['ml_score']:+.1f}(wp={ml['win_prob']:.0%})"

        return result

    # ══════════════════════════════════════
    # 헬퍼
    # ══════════════════════════════════════

    def _ema_trend(self, df) -> str:
        """EMA20/50 기반 추세. 단순하게."""
        if df is None or len(df) < 50:
            return "neutral"
        c = df["close"].astype(float)
        ema20 = float(c.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50 = float(c.ewm(span=50, adjust=False).mean().iloc[-1])
        p = float(c.iloc[-1])

        if p > ema20 > ema50:
            return "up"
        elif p < ema20 < ema50:
            return "down"
        return "neutral"

    def _atr(self, df, period=14) -> float:
        h = df["high"].astype(float)
        l = df["low"].astype(float)
        c = df["close"].astype(float)
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        return float(tr.rolling(period).mean().iloc[-1]) if len(tr) >= period else float(tr.mean())

    def _find_key_levels(self, df_4h, df_1h, current_price) -> dict:
        """
        4h/1h 스윙 고/저점에서 주요 지지/저항 탐지.
        멀티TF 겹치면 강도 높음. 상위 3개씩만 반환.
        """
        supports = []
        resistances = []

        for df, tf_weight in [(df_4h, 2.0), (df_1h, 1.0)]:
            if df is None or len(df) < 20:
                continue
            highs = df["high"].astype(float).values
            lows = df["low"].astype(float).values

            # 스윙 고/저점 (좌우 3봉)
            for i in range(3, len(highs) - 3):
                # 스윙 하이 → 저항
                if highs[i] == max(highs[i-3:i+4]):
                    resistances.append({
                        "price": round(float(highs[i]), 1),
                        "tf": "4h" if tf_weight > 1 else "1h",
                        "strength": tf_weight,
                        "idx": i,
                    })
                # 스윙 로우 → 지지
                if lows[i] == min(lows[i-3:i+4]):
                    supports.append({
                        "price": round(float(lows[i]), 1),
                        "tf": "4h" if tf_weight > 1 else "1h",
                        "strength": tf_weight,
                        "idx": i,
                    })

        # 레벨 병합 (0.3% 이내 = 같은 존)
        supports = self._merge_levels(supports, current_price * 0.003)
        resistances = self._merge_levels(resistances, current_price * 0.003)

        # 현재가 아래 지지, 위 저항만
        supports = [s for s in supports if s["price"] < current_price]
        resistances = [r for r in resistances if r["price"] > current_price]

        # 가까운 순 정렬, 상위 3개
        supports.sort(key=lambda x: current_price - x["price"])
        resistances.sort(key=lambda x: x["price"] - current_price)

        return {
            "supports": supports[:3],
            "resistances": resistances[:3],
        }

    def _merge_levels(self, levels, merge_dist):
        """가까운 레벨 병합 — 강도 합산"""
        if not levels:
            return []
        levels.sort(key=lambda x: x["price"])
        merged = [levels[0].copy()]
        for lv in levels[1:]:
            if abs(lv["price"] - merged[-1]["price"]) <= merge_dist:
                # 병합: 가격 = 가중 평균, 강도 합산
                merged[-1]["strength"] += lv["strength"]
                merged[-1]["price"] = round(
                    (merged[-1]["price"] + lv["price"]) / 2, 1
                )
            else:
                merged.append(lv.copy())
        return merged

    async def _check_flow(self, direction: str) -> dict:
        """
        오더플로우 확인 — direction과 같은 방향이면 confirmed.
        CVD 합산(메인) + 고래(보조) + 청산(보조).
        """
        flow = {
            "confirmed": False,
            "direction": "neutral",
            "strength": 0.0,
            "reason": "no_data",
            "whale_confirm": False,
            "liquidation_confirm": False,
        }

        if not self.redis:
            return flow

        try:
            # 1) CVD 합산 (가장 중요)
            cvd_15m = float(await self.redis.get("flow:combined:cvd_15m") or
                           await self.redis.get("cvd:15m:current:BTC-USDT-SWAP") or 0)
            cvd_1h = float(await self.redis.get("flow:combined:cvd_1h") or
                          await self.redis.get("cvd:1h:current:BTC-USDT-SWAP") or 0)

            # CVD 방향
            if cvd_15m > 0 and cvd_1h > 0:
                flow["direction"] = "long"
                flow["strength"] = min(1.0, (abs(cvd_15m) + abs(cvd_1h)) / 100)
            elif cvd_15m < 0 and cvd_1h < 0:
                flow["direction"] = "short"
                flow["strength"] = min(1.0, (abs(cvd_15m) + abs(cvd_1h)) / 100)
            else:
                flow["reason"] = f"cvd_mixed(15m={cvd_15m:.1f},1h={cvd_1h:.1f})"
                return flow

            flow["cvd_15m"] = round(cvd_15m, 2)
            flow["cvd_1h"] = round(cvd_1h, 2)

            # 방향 일치 확인
            if flow["direction"] != direction:
                flow["reason"] = f"cvd_{flow['direction']}_vs_want_{direction}"
                return flow

            # 2) 고래 (보조 확인)
            whale_str = await self.redis.get("flow:combined:whale_bias")
            if whale_str:
                wb = float(whale_str)
                whale_dir = "long" if wb > 0.1 else "short" if wb < -0.1 else "neutral"
                if whale_dir == direction:
                    flow["whale_confirm"] = True

            # 3) 청산 (보조 확인)
            import json
            liq_str = await self.redis.get("flow:liq:surge")
            if liq_str:
                liq = json.loads(liq_str)
                if liq.get("bias") == direction:
                    flow["liquidation_confirm"] = True

            # CVD 방향 일치 = 확인 완료
            flow["confirmed"] = True
            flow["reason"] = "cvd_confirmed"

        except Exception as e:
            flow["reason"] = f"error: {e}"

        return flow
