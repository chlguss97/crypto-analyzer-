"""
FlowEngine v2 — 다중 셋업 오더플로우 엔진 (데이터 축적용)

6종 셋업:
  LVL  - Level Bounce: S/R 반등 + CVD 확인
  MOM  - Momentum: EMA 크로스 + CVD 방향 일치
  PB   - Pullback: 추세 중 EMA20 되돌림 + CVD 반등
  BRK  - Breakout: 레벨 돌파 + 거래량/CVD 급증
  DIV  - RSI Divergence: RSI 다이버전스 + 과매수/과매도
  SES  - Session Open: 런던/뉴욕 세션 오픈 모멘텀

각 셋업 독립 평가 → 가장 높은 점수 1개 선택 → 진입.
모든 셋업 결과가 signals에 기록되어 ML 학습 데이터로 활용.
"""

import json
import logging
import numpy as np
import pandas as pd
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# 세션 시간대 (UTC)
SESSIONS = {
    "asia":   (0, 8),    # 00:00~08:00 UTC (KST 09:00~17:00)
    "london": (8, 14),   # 08:00~14:00 UTC
    "newyork": (14, 21), # 14:00~21:00 UTC
    "late":   (21, 24),  # 21:00~00:00 UTC
}


class FlowEngine:

    def __init__(self, redis=None, flow_ml=None):
        self.redis = redis
        self.flow_ml = flow_ml

    async def analyze(self, df_1m, df_5m, df_15m=None, df_1h=None,
                      df_4h=None, df_1d=None, rt_velocity=None) -> dict:
        result = {
            "setup": None, "direction": "neutral", "score": 0,
            "hold_mode": "standard", "sl_distance": 0, "tp_distance": 0,
            "signals": {}, "reason": "no_signal", "atr": 0, "atr_pct": 0,
        }

        if df_5m is None or len(df_5m) < 30:
            return result

        price = float(df_5m["close"].iloc[-1])
        atr_5m = self._atr(df_5m, 14)
        atr_pct = atr_5m / price * 100 if price > 0 else 0.3
        result["atr"] = round(atr_5m, 2)
        result["atr_pct"] = round(atr_pct, 4)

        # ── 공통 컨텍스트 ──
        trend_1d = self._ema_trend(df_1d)
        trend_4h = self._ema_trend(df_4h)
        trend_1h = self._ema_trend(df_1h)
        trend_15m = self._ema_trend(df_15m)
        trend_5m = self._ema_trend(df_5m)

        # 큰 추세: 1d > 4h > 1h 순서
        if trend_1d != "neutral":
            big_trend = trend_1d
        elif trend_4h != "neutral":
            big_trend = trend_4h
        elif trend_1h != "neutral":
            big_trend = trend_1h
        else:
            big_trend = "neutral"

        # 변동성 단계
        if atr_pct >= 0.5:
            vol_band = "high"
        elif atr_pct >= 0.2:
            vol_band = "mid"
        else:
            vol_band = "low"

        # 세션
        hour = datetime.now(timezone.utc).hour
        session = "late"
        for s_name, (s_start, s_end) in SESSIONS.items():
            if s_start <= hour < s_end:
                session = s_name
                break

        # RSI (5m)
        rsi_5m = self._rsi(df_5m, 14)
        rsi_15m = self._rsi(df_15m, 14) if df_15m is not None and len(df_15m) >= 20 else 50.0

        # EMA (5m)
        ema8_5m = self._ema(df_5m, 8)
        ema21_5m = self._ema(df_5m, 21)
        ema20_15m = self._ema(df_15m, 20) if df_15m is not None and len(df_15m) >= 20 else price

        # 레벨
        levels = self._find_key_levels(df_4h, df_1h, price, df_1d=df_1d)

        # CVD
        flow = await self._get_flow_data()

        # 컨텍스트 저장
        ctx = {
            "trend_1d": trend_1d, "trend_4h": trend_4h, "trend_1h": trend_1h,
            "trend_15m": trend_15m, "trend_5m": trend_5m,
            "big_trend": big_trend, "vol_band": vol_band, "session": session,
            "rsi_5m": round(rsi_5m, 1), "rsi_15m": round(rsi_15m, 1),
            "ema8_5m": round(ema8_5m, 1), "ema21_5m": round(ema21_5m, 1),
            "ema20_15m": round(ema20_15m, 1),
            "price": price, "hour": hour,
        }
        # 레벨 근접 판단 (FlowML 피처용)
        near_support = any(abs(price - lv["price"]) <= atr_5m * 3.0 for lv in levels.get("supports", []))
        near_resistance = any(abs(price - lv["price"]) <= atr_5m * 3.0 for lv in levels.get("resistances", []))

        # ── SignalTracker 호환 normalized 시그널 추가 ──
        # 기존 키(trend_1d 등)는 문자열/bool 그대로 유지 (FlowML, main.py, dashboard 하위호환)
        # SignalTracker용 {"direction":..,"strength":..} 포맷은 별도 키로 추가
        def _trend_to_signal(trend_str, strength=0.5):
            if trend_str == "up":
                return {"direction": "long", "strength": strength}
            elif trend_str == "down":
                return {"direction": "short", "strength": strength}
            return {"direction": "neutral", "strength": 0.0}

        def _level_signal(near_sup, near_res):
            if near_sup and not near_res:
                return {"direction": "long", "strength": 0.5}
            elif near_res and not near_sup:
                return {"direction": "short", "strength": 0.5}
            return {"direction": "neutral", "strength": 0.0}

        result["signals"] = {
            # 원본 값 유지 (FlowML, main.py, dashboard 등 하위호환)
            "trend_1d": trend_1d, "trend_4h": trend_4h, "trend_1h": trend_1h,
            "big_trend": big_trend, "levels": levels, "flow": flow,
            "near_support": near_support, "near_resistance": near_resistance,
            "context": ctx,
            # SignalTracker 호환 normalized 시그널 ({"direction":..,"strength":..} 포맷)
            "sig_trend_1d": _trend_to_signal(trend_1d, 0.6),
            "sig_trend_4h": _trend_to_signal(trend_4h, 0.5),
            "sig_trend_1h": _trend_to_signal(trend_1h, 0.4),
            "sig_big_trend": _trend_to_signal(big_trend, 0.5),
            "sig_level": _level_signal(near_support, near_resistance),
            # flow는 이미 {"direction":..,"strength":..} 포맷 → SignalTracker가 직접 사용
        }

        # ── 6종 셋업 독립 평가 ──
        candidates = []

        s1 = self._check_level_bounce(price, atr_5m, levels, flow, big_trend)
        if s1:
            candidates.append(s1)

        s2 = self._check_momentum(price, atr_5m, ema8_5m, ema21_5m, flow, trend_5m, trend_15m, df_5m)
        if s2:
            candidates.append(s2)

        s3 = self._check_pullback(price, atr_5m, ema20_15m, flow, big_trend, trend_15m, rsi_5m)
        if s3:
            candidates.append(s3)

        s4 = self._check_breakout(price, atr_5m, levels, flow, df_5m)
        if s4:
            candidates.append(s4)

        s5 = self._check_rsi_divergence(price, atr_5m, rsi_5m, df_5m, flow)
        if s5:
            candidates.append(s5)

        s6 = self._check_session_open(price, atr_5m, hour, flow, big_trend, df_1m)
        if s6:
            candidates.append(s6)

        # 셋업별 결과를 signals에 기록 (ML 학습용)
        result["signals"]["setups_evaluated"] = [
            {"setup": c["setup"], "direction": c["direction"], "score": c["score"]}
            for c in candidates
        ]

        if not candidates:
            result["reason"] = f"no_setup_triggered | big={big_trend} vol={vol_band} ses={session}"
            return result

        # 가장 높은 점수 선택
        best = max(candidates, key=lambda x: x["score"])

        result.update({
            "setup": best["setup"],
            "direction": best["direction"],
            "score": best["score"],
            "hold_mode": best.get("hold_mode", "standard"),
            "sl_distance": best["sl_distance"],
            "tp_distance": best["tp_distance"],
            "reason": best["reason"],
        })

        # ML 보정
        if self.flow_ml:
            ml = self.flow_ml.predict(result)
            result["signals"]["ml"] = ml
            if ml["trained"]:
                raw = result["score"]
                result["score"] = round(max(0, min(10, raw + ml["ml_score"])), 1)
                result["signals"]["raw_score"] = raw
                result["signals"]["htf_bias_applied"] = ml["ml_score"]

        return result

    # ══════════════════════════════════════
    # 셋업 1: Level Bounce
    # ══════════════════════════════════════

    def _check_level_bounce(self, price, atr, levels, flow, big_trend) -> dict | None:
        near_support = None
        near_resistance = None

        for lv in levels.get("supports", []):
            if abs(price - lv["price"]) <= atr * 3.0:
                near_support = lv
                break
        for lv in levels.get("resistances", []):
            if abs(price - lv["price"]) <= atr * 3.0:
                near_resistance = lv
                break

        if not near_support and not near_resistance:
            return None

        # 방향 결정
        if near_support and (big_trend in ("up", "neutral")):
            direction = "long"
            level = near_support
        elif near_resistance and (big_trend in ("down", "neutral")):
            direction = "short"
            level = near_resistance
        else:
            return None

        # CVD 확인 (같은 방향이면 보너스, 반대면 차단하지 않음)
        score = 6.0
        if flow.get("direction") == direction:
            score += 1.5
        elif flow.get("direction") != "neutral" and flow.get("direction") != direction:
            score -= 1.0

        if level.get("strength", 1) >= 3.0:
            score += 1.0  # 멀티TF 겹침 레벨

        # SL/TP
        if direction == "long":
            sl_dist = price - level["price"] + atr * 0.5
            tp_dist = sl_dist * 2.0
            if levels.get("resistances"):
                tp_dist = max(tp_dist, levels["resistances"][0]["price"] - price)
        else:
            sl_dist = level["price"] - price + atr * 0.5
            tp_dist = sl_dist * 2.0
            if levels.get("supports"):
                tp_dist = max(tp_dist, price - levels["supports"][0]["price"])

        sl_dist = max(sl_dist, price * 0.003)
        tp_dist = max(tp_dist, sl_dist * 1.5)

        return {
            "setup": "LVL", "direction": direction,
            "score": round(min(10, score), 1),
            "hold_mode": "standard",
            "sl_distance": round(sl_dist, 1),
            "tp_distance": round(tp_dist, 1),
            "reason": f"level_bounce {direction} @ ${level['price']:,.0f} str={level.get('strength',1):.0f}",
        }

    # ══════════════════════════════════════
    # 셋업 2: Momentum (EMA Cross)
    # ══════════════════════════════════════

    def _check_momentum(self, price, atr, ema8, ema21, flow, trend_5m, trend_15m, df_5m) -> dict | None:
        if len(df_5m) < 25:
            return None

        c = df_5m["close"].astype(float)
        ema8_prev = float(c.iloc[:-1].ewm(span=8, adjust=False).mean().iloc[-1])
        ema21_prev = float(c.iloc[:-1].ewm(span=21, adjust=False).mean().iloc[-1])

        # 크로스 감지: 이전에는 EMA8 < EMA21, 지금은 EMA8 > EMA21 (골든)
        golden_cross = ema8_prev <= ema21_prev and ema8 > ema21
        death_cross = ema8_prev >= ema21_prev and ema8 < ema21

        if not golden_cross and not death_cross:
            return None

        direction = "long" if golden_cross else "short"

        score = 6.0
        # 15m 추세 일치
        if trend_15m == ("up" if direction == "long" else "down"):
            score += 1.0
        # CVD 일치
        if flow.get("direction") == direction:
            score += 1.5
        # 5m 추세 강도
        if trend_5m == ("up" if direction == "long" else "down"):
            score += 0.5

        sl_dist = atr * 2.0
        tp_dist = atr * 3.0

        return {
            "setup": "MOM", "direction": direction,
            "score": round(min(10, score), 1),
            "hold_mode": "quick",
            "sl_distance": round(sl_dist, 1),
            "tp_distance": round(tp_dist, 1),
            "reason": f"momentum {'golden' if golden_cross else 'death'}_cross 5m",
        }

    # ══════════════════════════════════════
    # 셋업 3: Pullback
    # ══════════════════════════════════════

    def _check_pullback(self, price, atr, ema20_15m, flow, big_trend, trend_15m, rsi) -> dict | None:
        if big_trend == "neutral":
            return None

        direction = "long" if big_trend == "up" else "short"

        # 풀백 조건: 추세는 있지만 가격이 EMA20 근처로 되돌림
        dist_to_ema = (price - ema20_15m) / price * 100  # % 거리

        if direction == "long":
            # 상승 추세에서 가격이 EMA20 근처(0~-0.3%)로 내려옴
            if dist_to_ema > 0.1 or dist_to_ema < -0.5:
                return None
            # RSI 과매도 근처면 더 좋음
            if rsi > 60:
                return None
        else:
            # 하락 추세에서 가격이 EMA20 근처(0~+0.3%)로 올라옴
            if dist_to_ema < -0.1 or dist_to_ema > 0.5:
                return None
            if rsi < 40:
                return None

        score = 6.5
        if flow.get("direction") == direction:
            score += 1.5
        if trend_15m == big_trend.replace("neutral", ""):
            score += 0.5
        # RSI 적정 범위
        if direction == "long" and 30 <= rsi <= 45:
            score += 0.5
        elif direction == "short" and 55 <= rsi <= 70:
            score += 0.5

        sl_dist = atr * 2.5
        tp_dist = atr * 4.0

        return {
            "setup": "PB", "direction": direction,
            "score": round(min(10, score), 1),
            "hold_mode": "standard",
            "sl_distance": round(sl_dist, 1),
            "tp_distance": round(tp_dist, 1),
            "reason": f"pullback {direction} ema20_dist={dist_to_ema:+.2f}% rsi={rsi:.0f}",
        }

    # ══════════════════════════════════════
    # 셋업 4: Breakout
    # ══════════════════════════════════════

    def _check_breakout(self, price, atr, levels, flow, df_5m) -> dict | None:
        if len(df_5m) < 10:
            return None

        # 최근 5봉 중 돌파 확인
        recent_highs = df_5m["high"].astype(float).iloc[-5:]
        recent_lows = df_5m["low"].astype(float).iloc[-5:]
        prev_close = float(df_5m["close"].iloc[-2])

        # 저항 돌파 (long)
        for lv in levels.get("resistances", []):
            # 이전 봉은 레벨 아래, 현재 봉은 레벨 위
            if prev_close < lv["price"] and price > lv["price"]:
                # 거래량 증가 확인 (최근 5봉 평균 대비)
                vol_now = float(df_5m["volume"].iloc[-1])
                vol_avg = float(df_5m["volume"].iloc[-10:-1].mean()) if len(df_5m) >= 11 else vol_now
                vol_ratio = vol_now / vol_avg if vol_avg > 0 else 1.0

                if vol_ratio < 1.2:
                    continue  # 거래량 미동반 돌파 무시

                score = 6.0
                if flow.get("direction") == "long":
                    score += 1.5
                if vol_ratio > 2.0:
                    score += 1.0
                if lv.get("strength", 1) >= 2.0:
                    score += 0.5

                sl_dist = price - lv["price"] + atr * 0.3
                sl_dist = max(sl_dist, price * 0.003)
                tp_dist = atr * 4.0

                return {
                    "setup": "BRK", "direction": "long",
                    "score": round(min(10, score), 1),
                    "hold_mode": "runner",
                    "sl_distance": round(sl_dist, 1),
                    "tp_distance": round(tp_dist, 1),
                    "reason": f"breakout_up ${lv['price']:,.0f} vol={vol_ratio:.1f}x",
                }

        # 지지 하향 돌파 (short)
        for lv in levels.get("supports", []):
            if prev_close > lv["price"] and price < lv["price"]:
                vol_now = float(df_5m["volume"].iloc[-1])
                vol_avg = float(df_5m["volume"].iloc[-10:-1].mean()) if len(df_5m) >= 11 else vol_now
                vol_ratio = vol_now / vol_avg if vol_avg > 0 else 1.0

                if vol_ratio < 1.2:
                    continue

                score = 6.0
                if flow.get("direction") == "short":
                    score += 1.5
                if vol_ratio > 2.0:
                    score += 1.0
                if lv.get("strength", 1) >= 2.0:
                    score += 0.5

                sl_dist = lv["price"] - price + atr * 0.3
                sl_dist = max(sl_dist, price * 0.003)
                tp_dist = atr * 4.0

                return {
                    "setup": "BRK", "direction": "short",
                    "score": round(min(10, score), 1),
                    "hold_mode": "runner",
                    "sl_distance": round(sl_dist, 1),
                    "tp_distance": round(tp_dist, 1),
                    "reason": f"breakout_down ${lv['price']:,.0f} vol={vol_ratio:.1f}x",
                }

        return None

    # ══════════════════════════════════════
    # 셋업 5: RSI Divergence
    # ══════════════════════════════════════

    def _check_rsi_divergence(self, price, atr, rsi, df_5m, flow) -> dict | None:
        if len(df_5m) < 30:
            return None

        closes = df_5m["close"].astype(float).values
        rsi_series = self._rsi_series(df_5m, 14)
        if len(rsi_series) < 20:
            return None

        # Bullish divergence: 가격 lower low + RSI higher low + RSI < 35
        if rsi < 35:
            price_ll = closes[-1] < min(closes[-10:-5])
            rsi_hl = rsi_series[-1] > min(rsi_series[-10:-5])
            if price_ll and rsi_hl:
                score = 6.5
                if flow.get("direction") == "long":
                    score += 1.0
                sl_dist = atr * 2.0
                tp_dist = atr * 3.5
                return {
                    "setup": "DIV", "direction": "long",
                    "score": round(min(10, score), 1),
                    "hold_mode": "standard",
                    "sl_distance": round(sl_dist, 1),
                    "tp_distance": round(tp_dist, 1),
                    "reason": f"bullish_divergence rsi={rsi:.0f}",
                }

        # Bearish divergence: 가격 higher high + RSI lower high + RSI > 65
        if rsi > 65:
            price_hh = closes[-1] > max(closes[-10:-5])
            rsi_lh = rsi_series[-1] < max(rsi_series[-10:-5])
            if price_hh and rsi_lh:
                score = 6.5
                if flow.get("direction") == "short":
                    score += 1.0
                sl_dist = atr * 2.0
                tp_dist = atr * 3.5
                return {
                    "setup": "DIV", "direction": "short",
                    "score": round(min(10, score), 1),
                    "hold_mode": "standard",
                    "sl_distance": round(sl_dist, 1),
                    "tp_distance": round(tp_dist, 1),
                    "reason": f"bearish_divergence rsi={rsi:.0f}",
                }

        return None

    # ══════════════════════════════════════
    # 셋업 6: Session Open
    # ══════════════════════════════════════

    def _check_session_open(self, price, atr, hour, flow, big_trend, df_1m) -> dict | None:
        # 런던(08 UTC) / 뉴욕(14 UTC) 오픈 후 30분 이내만
        if hour not in (8, 14):
            return None
        minute = datetime.now(timezone.utc).minute
        if minute > 30:
            return None

        if df_1m is None or len(df_1m) < 15:
            return None

        # 오픈 후 모멘텀: 최근 10분 방향
        recent = df_1m["close"].astype(float).iloc[-10:]
        move_pct = (float(recent.iloc[-1]) - float(recent.iloc[0])) / float(recent.iloc[0]) * 100

        if abs(move_pct) < 0.05:
            return None  # 모멘텀 부족

        direction = "long" if move_pct > 0 else "short"

        session_name = "london" if hour == 8 else "newyork"
        score = 6.0

        # 큰 추세와 일치
        if big_trend == ("up" if direction == "long" else "down"):
            score += 1.0
        # CVD 일치
        if flow.get("direction") == direction:
            score += 1.5
        # 강한 모멘텀
        if abs(move_pct) > 0.15:
            score += 0.5

        sl_dist = atr * 2.0
        tp_dist = atr * 3.0

        return {
            "setup": "SES", "direction": direction,
            "score": round(min(10, score), 1),
            "hold_mode": "quick",
            "sl_distance": round(sl_dist, 1),
            "tp_distance": round(tp_dist, 1),
            "reason": f"session_{session_name}_open {direction} move={move_pct:+.2f}%",
        }

    # ══════════════════════════════════════
    # 헬퍼
    # ══════════════════════════════════════

    def _ema_trend(self, df) -> str:
        if df is None or len(df) < 10:
            return "neutral"
        c = df["close"].astype(float)
        p = float(c.iloc[-1])
        ema20 = float(c.ewm(span=max(1, min(20, len(c)-1)), adjust=False).mean().iloc[-1])

        if len(df) >= 50:
            ema50 = float(c.ewm(span=50, adjust=False).mean().iloc[-1])
            if p > ema20 > ema50:
                return "up"
            elif p < ema20 < ema50:
                return "down"
        else:
            if p > ema20 * 1.001:
                return "up"
            elif p < ema20 * 0.999:
                return "down"
        return "neutral"

    def _atr(self, df, period=14) -> float:
        h = df["high"].astype(float)
        l = df["low"].astype(float)
        c = df["close"].astype(float)
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        return float(tr.rolling(period).mean().iloc[-1]) if len(tr) >= period else float(tr.mean())

    def _ema(self, df, span) -> float:
        if df is None or len(df) < span:
            return 0.0
        return float(df["close"].astype(float).ewm(span=span, adjust=False).mean().iloc[-1])

    def _rsi(self, df, period=14) -> float:
        if df is None or len(df) < period + 1:
            return 50.0
        delta = df["close"].astype(float).diff()
        gain = delta.where(delta > 0, 0.0).ewm(alpha=1/period, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/period, adjust=False).mean()
        rs = gain / loss.replace(0, 1e-10)
        rsi = 100 - 100 / (1 + rs)
        return float(rsi.iloc[-1])

    def _rsi_series(self, df, period=14) -> list:
        if df is None or len(df) < period + 1:
            return []
        delta = df["close"].astype(float).diff()
        gain = delta.where(delta > 0, 0.0).ewm(alpha=1/period, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/period, adjust=False).mean()
        rs = gain / loss.replace(0, 1e-10)
        rsi = 100 - 100 / (1 + rs)
        return rsi.dropna().tolist()[-20:]

    def _find_key_levels(self, df_4h, df_1h, current_price, df_1d=None) -> dict:
        supports = []
        resistances = []

        for df, tf_weight in [(df_1d, 3.0), (df_4h, 2.0), (df_1h, 1.0)]:
            if df is None or len(df) < 20:
                continue
            highs = df["high"].astype(float).values
            lows = df["low"].astype(float).values

            for i in range(3, len(highs) - 3):
                if highs[i] == max(highs[i-3:i+4]):
                    resistances.append({
                        "price": round(float(highs[i]), 1),
                        "strength": tf_weight, "idx": i,
                    })
                if lows[i] == min(lows[i-3:i+4]):
                    supports.append({
                        "price": round(float(lows[i]), 1),
                        "strength": tf_weight, "idx": i,
                    })

        supports = self._merge_levels(supports, current_price * 0.003)
        resistances = self._merge_levels(resistances, current_price * 0.003)

        supports = [s for s in supports if s["price"] < current_price]
        resistances = [r for r in resistances if r["price"] > current_price]

        supports.sort(key=lambda x: current_price - x["price"])
        resistances.sort(key=lambda x: x["price"] - current_price)

        return {"supports": supports[:5], "resistances": resistances[:5]}

    def _merge_levels(self, levels, merge_dist):
        if not levels:
            return []
        levels.sort(key=lambda x: x["price"])
        merged = [levels[0].copy()]
        for lv in levels[1:]:
            if abs(lv["price"] - merged[-1]["price"]) <= merge_dist:
                merged[-1]["strength"] += lv["strength"]
                merged[-1]["price"] = round((merged[-1]["price"] + lv["price"]) / 2, 1)
            else:
                merged.append(lv.copy())
        return merged

    async def _get_flow_data(self) -> dict:
        """CVD + 고래 + 청산 데이터 조회 (확인/비확인 판단 없이 원시 데이터 반환)"""
        flow = {
            "direction": "neutral", "strength": 0.0,
            "cvd_15m": 0, "cvd_1h": 0,
            "whale_confirm": False, "liquidation_confirm": False,
        }

        if not self.redis:
            return flow

        try:
            cvd_15m = float(await self.redis.get("flow:combined:cvd_15m") or
                           await self.redis.get("cvd:15m:current:BTC-USDT-SWAP") or 0)
            cvd_1h = float(await self.redis.get("flow:combined:cvd_1h") or
                          await self.redis.get("cvd:1h:current:BTC-USDT-SWAP") or 0)

            flow["cvd_15m"] = round(cvd_15m, 2)
            flow["cvd_1h"] = round(cvd_1h, 2)

            # CVD 방향 (15m 단독으로도 판단 — 1h는 보너스)
            CVD_MIN = 0.3
            if cvd_15m > CVD_MIN:
                flow["direction"] = "long"
                flow["strength"] = min(1.0, abs(cvd_15m) / 50)
            elif cvd_15m < -CVD_MIN:
                flow["direction"] = "short"
                flow["strength"] = min(1.0, abs(cvd_15m) / 50)

            # 1h 일치 시 강도 부스트
            if (cvd_15m > 0 and cvd_1h > 0) or (cvd_15m < 0 and cvd_1h < 0):
                flow["strength"] = min(1.0, flow["strength"] * 1.5)

            # 고래
            whale_str = await self.redis.get("flow:combined:whale_bias")
            if whale_str:
                wb = float(whale_str)
                if (flow["direction"] == "long" and wb > 0.1) or \
                   (flow["direction"] == "short" and wb < -0.1):
                    flow["whale_confirm"] = True

            # 청산
            liq_str = await self.redis.get("flow:liq:surge")
            if liq_str:
                liq = json.loads(liq_str)
                if liq.get("bias") == flow["direction"]:
                    flow["liquidation_confirm"] = True

        except Exception as e:
            logger.debug(f"flow data error: {e}")

        return flow
