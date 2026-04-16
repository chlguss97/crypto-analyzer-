"""
TradeEngine v1 — 멀티TF 셋업 매칭 방식

점수 합산이 아닌, 3개 명확한 셋업의 조건 전부 충족 시에만 진입.

셋업 A (추세 모멘텀):  15m 추세 + 5m BOS + 1m 모멘텀 + 거래량
셋업 B (OB 리테스트):  15m 추세 + 5m OB + 가격 OB존 도달 + 1m ChoCH
셋업 C (브레이크아웃): 5m 레인지 + 돌파 + 거래량 2x + 리테스트 홀드

횡보장 = 매매 안 함 (핵심 룰)
"""
import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)


class TradeEngine:
    """멀티TF 셋업 매칭 엔진"""

    def __init__(self):
        self.name = "unified"

    async def analyze(self, df_1m: pd.DataFrame, df_5m: pd.DataFrame,
                      df_15m: pd.DataFrame = None, df_1h: pd.DataFrame = None,
                      rt_velocity: dict = None) -> dict:
        """
        멀티TF 분석 → 셋업 매칭 → 진입 판정.

        Returns:
            {
                "setup": "A" | "B" | "C" | None,
                "direction": "long" | "short" | "neutral",
                "score": float (0~10, 셋업 신뢰도),
                "hold_mode": "quick" | "standard" | "swing",
                "sl_distance": float,
                "tp_distance": float,
                "signals": dict,
                "reason": str,
            }
        """
        result = {
            "setup": None, "direction": "neutral", "score": 0.0,
            "hold_mode": "standard", "sl_distance": 0, "tp_distance": 0,
            "signals": {}, "reason": "no_setup",
            "atr": 0, "atr_pct": 0,
        }

        if df_1m is None or len(df_1m) < 30:
            return result
        if df_5m is None or len(df_5m) < 30:
            return result

        # ═══ 1단계: 시장 컨텍스트 (HTF 분석) ═══
        ctx = self._market_context(df_5m, df_15m, df_1h)
        result["signals"]["context"] = ctx

        # ATR 계산 (SL/TP용)
        atr = self._calc_atr(df_5m, 14)
        atr_pct = atr / float(df_5m["close"].iloc[-1]) * 100 if atr > 0 else 0.3
        result["atr"] = round(atr, 2)
        result["atr_pct"] = round(atr_pct, 4)

        # ═══ 2단계: 셋업 매칭 (우선순위: B > A > C) ═══

        # 셋업 B: OB 리테스트 (최고 RR, 우선 체크)
        setup_b = self._check_setup_b(df_1m, df_5m, df_15m, ctx)
        if not setup_b["match"]:
            result["signals"]["reject_b"] = setup_b.get("reject_reason", "conditions_not_met")
        if setup_b["match"]:
            result["setup"] = "B"
            result["direction"] = setup_b["direction"]
            result["score"] = setup_b["score"]
            result["hold_mode"] = "standard"
            result["sl_distance"] = setup_b["sl_dist"]
            result["tp_distance"] = setup_b["tp_dist"]
            result["signals"]["setup_b"] = setup_b
            result["reason"] = setup_b["reason"]
            return result

        # 셋업 A: 추세 모멘텀 (가장 빈번)
        setup_a = self._check_setup_a(df_1m, df_5m, ctx)
        if not setup_a["match"]:
            result["signals"]["reject_a"] = setup_a.get("reject_reason", "conditions_not_met")
        if setup_a["match"]:
            result["setup"] = "A"
            result["direction"] = setup_a["direction"]
            result["score"] = setup_a["score"]
            result["hold_mode"] = "quick"
            result["sl_distance"] = setup_a["sl_dist"]
            result["tp_distance"] = setup_a["tp_dist"]
            result["signals"]["setup_a"] = setup_a
            result["reason"] = setup_a["reason"]
            return result

        # 셋업 C: 브레이크아웃
        setup_c = self._check_setup_c(df_1m, df_5m, ctx)
        if not setup_c["match"]:
            result["signals"]["reject_c"] = setup_c.get("reject_reason", "conditions_not_met")
        if setup_c["match"]:
            result["setup"] = "C"
            result["direction"] = setup_c["direction"]
            result["score"] = setup_c["score"]
            result["hold_mode"] = "standard"
            result["sl_distance"] = setup_c["sl_dist"]
            result["tp_distance"] = setup_c["tp_dist"]
            result["signals"]["setup_c"] = setup_c
            result["reason"] = setup_c["reason"]
            return result

        return result

    # ══════════════════════════════════════════
    # 시장 컨텍스트 (HTF 분석)
    # ══════════════════════════════════════════

    def _market_context(self, df_5m, df_15m, df_1h) -> dict:
        """
        상위 타임프레임 분석 → 추세 방향 + 강도 판정.
        모든 셋업의 방향 필터로 사용됨.
        """
        ctx = {
            "trend": "neutral",       # up / down / neutral
            "trend_strength": 0.0,    # 0~1
            "structure": "unclear",   # hh_hl / lh_ll / range
            "volume_ratio": 1.0,      # 현재 거래량 / 평균
        }

        # 5m EMA 추세
        c5 = df_5m["close"].astype(float)
        ema20_5m = float(c5.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50_5m = float(c5.ewm(span=50, adjust=False).mean().iloc[-1])
        ema200_5m = float(c5.ewm(span=200, adjust=False).mean().iloc[-1])
        price = float(c5.iloc[-1])

        # 15m EMA 추세 (있으면)
        trend_15m = "neutral"
        if df_15m is not None and len(df_15m) >= 50:
            c15 = df_15m["close"].astype(float)
            ema50_15m = float(c15.ewm(span=50, adjust=False).mean().iloc[-1])
            ema200_15m = float(c15.ewm(span=200, adjust=False).mean().iloc[-1])
            p15 = float(c15.iloc[-1])
            if p15 > ema50_15m and ema50_15m > ema200_15m:
                trend_15m = "up"
            elif p15 < ema50_15m and ema50_15m < ema200_15m:
                trend_15m = "down"

        # 1h EMA 추세 (있으면)
        trend_1h = "neutral"
        if df_1h is not None and len(df_1h) >= 50:
            c1h = df_1h["close"].astype(float)
            ema50_1h = float(c1h.ewm(span=50, adjust=False).mean().iloc[-1])
            ema200_1h = float(c1h.ewm(span=200, adjust=False).mean().iloc[-1])
            p1h = float(c1h.iloc[-1])
            if p1h > ema50_1h and ema50_1h > ema200_1h:
                trend_1h = "up"
            elif p1h < ema50_1h and ema50_1h < ema200_1h:
                trend_1h = "down"

        # 5m 추세 판정
        if price > ema50_5m and ema50_5m > ema200_5m:
            trend_5m = "up"
        elif price < ema50_5m and ema50_5m < ema200_5m:
            trend_5m = "down"
        else:
            trend_5m = "neutral"

        # 종합: 2개 이상 TF가 일치하면 추세 확정
        votes = [trend_5m, trend_15m, trend_1h]
        up_votes = votes.count("up")
        down_votes = votes.count("down")

        if up_votes >= 2:
            ctx["trend"] = "up"
            ctx["trend_strength"] = up_votes / 3
        elif down_votes >= 2:
            ctx["trend"] = "down"
            ctx["trend_strength"] = down_votes / 3
        else:
            ctx["trend"] = "neutral"
            ctx["trend_strength"] = 0.0

        # 5m 스윙 구조 판정
        ctx["structure"] = self._swing_structure(df_5m)

        # 거래량 비율
        vol = df_5m["volume"].astype(float)
        vol_avg = float(vol.rolling(20).mean().iloc[-1]) if len(vol) >= 20 else float(vol.mean())
        vol_current = float(vol.iloc[-1])
        ctx["volume_ratio"] = round(vol_current / vol_avg, 2) if vol_avg > 0 else 1.0

        # EMA 정보 저장
        ctx["ema20_5m"] = ema20_5m
        ctx["ema50_5m"] = ema50_5m
        ctx["ema200_5m"] = ema200_5m
        ctx["price"] = price

        return ctx

    def _swing_structure(self, df) -> str:
        """5m 스윙 구조: HH/HL(상승), LH/LL(하락), range(횡보)"""
        high = df["high"].values.astype(float)
        low = df["low"].values.astype(float)
        swings = self._find_swing_points(high, low, order=3)

        if len(swings) < 4:
            return "unclear"

        recent = swings[-4:]
        highs = [p for t, _, p in recent if t == "sh"]
        lows = [p for t, _, p in recent if t == "sl"]

        if len(highs) >= 2 and len(lows) >= 2:
            if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
                return "hh_hl"  # 상승 구조
            elif highs[-1] < highs[-2] and lows[-1] < lows[-2]:
                return "lh_ll"  # 하락 구조

        return "range"

    # ══════════════════════════════════════════
    # 셋업 A: 추세 모멘텀
    # ══════════════════════════════════════════

    def _check_setup_a(self, df_1m, df_5m, ctx) -> dict:
        """
        셋업 A: 추세 추종 (가장 빈번 — 하루 3~5회 목표)
        조건 (완화):
          1. 추세 명확 (2+ TF 일치)
          2. 5m EMA20 위(롱) / 아래(숏) — 추세 방향 정렬
          3. 1m 모멘텀 OR 5m 최근 3봉 중 2봉 추세 방향
          4. 거래량 > 평균 (1.0x 이상)
        BOS 제거: 이미 추세 중이어도 진입 가능
        """
        r = {"match": False, "direction": "neutral", "score": 0.0,
             "reason": "", "sl_dist": 0, "tp_dist": 0, "reject_reason": ""}

        # 조건 1: 추세 명확
        if ctx["trend"] == "neutral":
            r["reject_reason"] = "no_trend"
            return r
        direction = "long" if ctx["trend"] == "up" else "short"

        # 조건 2: 5m EMA20 정렬 (추세 방향에 가격이 위치)
        price = ctx["price"]
        ema20 = ctx.get("ema20_5m", 0)
        if direction == "long" and price <= ema20:
            r["reject_reason"] = "below_ema20"
            return r
        if direction == "short" and price >= ema20:
            r["reject_reason"] = "above_ema20"
            return r

        # 조건 3: 1m 모멘텀 OR 5m 최근 캔들 방향 일치
        mom = self._check_momentum_1m(df_1m, direction)
        if not mom["confirmed"]:
            # 5m 최근 3봉 중 2봉 이상이 추세 방향인지
            close_5m = df_5m["close"].values.astype(float)
            open_5m = df_5m["open"].values.astype(float)
            recent_dir = 0
            for i in range(-3, 0):
                if direction == "long" and close_5m[i] > open_5m[i]:
                    recent_dir += 1
                elif direction == "short" and close_5m[i] < open_5m[i]:
                    recent_dir += 1
            if recent_dir < 2:
                r["reject_reason"] = "no_momentum_no_candles"
                return r

        # 조건 4: 거래량 (완화: 1.0x 이상이면 OK)
        if ctx["volume_ratio"] < 0.8:
            r["reject_reason"] = f"very_low_volume({ctx['volume_ratio']:.1f}x)"
            return r

        # 전부 충족 → 진입
        # SL: 5m EMA50 또는 직전 스윙 저점
        high_5m = df_5m["high"].values.astype(float)
        low_5m = df_5m["low"].values.astype(float)
        swings = self._find_swing_points(high_5m, low_5m, order=3)

        if direction == "long":
            sls = [p for t, _, p in swings if t == "sl"]
            sl_level = sls[-1] if sls else ctx.get("ema50_5m", price * 0.995)
        else:
            shs = [p for t, _, p in swings if t == "sh"]
            sl_level = shs[-1] if shs else ctx.get("ema50_5m", price * 1.005)

        sl_dist = abs(price - sl_level)
        sl_dist = max(sl_dist, price * 0.0035)     # 최소 0.35%
        sl_dist = min(sl_dist, price * 0.008)       # 최대 0.8% (너무 넓으면 제한)
        tp_dist = sl_dist * 1.5                     # RR 1.5

        score = 5.0
        score += min(1.5, ctx["trend_strength"] * 2)
        if mom["confirmed"]:
            score += min(1.0, mom["strength"])
        score += min(1.0, (ctx["volume_ratio"] - 0.8) * 3)
        if ctx["structure"] in ("hh_hl", "lh_ll"):
            score += 1.0

        r.update({
            "match": True,
            "direction": direction,
            "score": round(min(10.0, score), 2),
            "reason": f"trend_follow: ema20+mom+vol({ctx['volume_ratio']:.1f}x)",
            "sl_dist": round(sl_dist, 1),
            "tp_dist": round(tp_dist, 1),
            "bos": bos,
            "momentum": mom,
        })
        return r

    # ══════════════════════════════════════════
    # 셋업 B: OB 리테스트
    # ══════════════════════════════════════════

    def _check_setup_b(self, df_1m, df_5m, df_15m, ctx) -> dict:
        """
        셋업 B: OB 리테스트 (완화)
        조건:
          1. 추세 명확
          2. 5m OB 존 존재 (프레시 우선, 1회 터치도 허용)
          3. 가격이 OB 존에 도달
          4. 1m 반전 캔들 (ChoCH 대신 양/음봉 확인)
        """
        r = {"match": False, "direction": "neutral", "score": 0.0,
             "reason": "", "sl_dist": 0, "tp_dist": 0, "reject_reason": ""}

        # 조건 1: 추세
        if ctx["trend"] == "neutral":
            r["reject_reason"] = "no_trend"
            return r
        direction = "long" if ctx["trend"] == "up" else "short"

        # 조건 2: OB 탐지 (프레시 + 비프레시 모두 허용)
        obs_5m = self._find_msb_order_blocks(df_5m, swing_order=3, lookback=50)
        if not obs_5m:
            r["reject_reason"] = "no_ob"
            return r

        price = float(df_1m["close"].iloc[-1])

        # 조건 3: 가격이 OB 존에 도달 + 방향 일치 (프레시 필수 제거)
        best_ob = None
        for ob in obs_5m:
            if ob["dir"] != direction:
                continue
            # 프레시 우선이지만 비프레시도 허용
            margin = (ob["high"] - ob["low"]) * 0.15
            if (ob["low"] - margin) <= price <= (ob["high"] + margin):
                if best_ob is None or ob.get("impulse", {}).get("quality", 0) > \
                        best_ob.get("impulse", {}).get("quality", 0):
                    best_ob = ob

        if not best_ob:
            r["reject_reason"] = "no_ob_in_zone"
            return r

        # 조건 4: 1m 반전 확인 (ChoCH 완화 → 단순 반전 캔들)
        c1m = df_1m.iloc[-1]
        choch = self._check_choch_1m(df_1m, direction)
        candle_confirm = (direction == "long" and float(c1m["close"]) > float(c1m["open"])) or \
                         (direction == "short" and float(c1m["close"]) < float(c1m["open"]))
        if not choch and not candle_confirm:
            r["reject_reason"] = "no_reversal_candle"
            return r

        # 조건 5: 유동성 클리어 (감점만, 차단 안 함)
        h5 = df_5m["high"].values.astype(float)
        l5 = df_5m["low"].values.astype(float)
        liq_clear = self._check_liquidity_clear(h5, l5, len(h5), best_ob, direction)

        # SL/TP
        if direction == "long":
            sl_dist = price - best_ob["low"]
        else:
            sl_dist = best_ob["high"] - price
        sl_dist = max(sl_dist, price * 0.0035)

        # TP = 직전 구조 고/저점까지
        swings = self._find_swing_points(h5, l5, order=3)
        tp_dist = sl_dist * 2.0  # 기본 RR 2.0
        if direction == "long":
            target_highs = [p for t, _, p in swings if t == "sh" and p > price]
            if target_highs:
                tp_dist = max(tp_dist, min(target_highs) - price)
        else:
            target_lows = [p for t, _, p in swings if t == "sl" and p < price]
            if target_lows:
                tp_dist = max(tp_dist, price - max(target_lows))

        score = 6.0  # B셋업 기본 높은 점수
        score += min(1.0, best_ob.get("impulse", {}).get("quality", 0) * 1.5)
        if best_ob.get("has_fvg", False):
            score += 0.5
        if liq_clear:
            score += 0.5
        else:
            score -= 1.5  # 유동성 위험

        # 15m OB 중첩 확인
        if df_15m is not None and len(df_15m) >= 20:
            obs_15m = self._find_msb_order_blocks(df_15m, swing_order=3, lookback=30)
            for ob15 in obs_15m:
                if ob15["dir"] == direction:
                    overlap = min(best_ob["high"], ob15["high"]) - max(best_ob["low"], ob15["low"])
                    if overlap > 0:
                        score += 1.0
                        break

        r.update({
            "match": True,
            "direction": direction,
            "score": round(min(10.0, score), 2),
            "reason": f"ob_retest: fresh={best_ob['fresh']} fvg={best_ob.get('has_fvg')} choch=True liq={liq_clear}",
            "sl_dist": round(sl_dist, 1),
            "tp_dist": round(tp_dist, 1),
            "ob": best_ob,
            "liq_clear": liq_clear,
        })
        return r

    # ══════════════════════════════════════════
    # 셋업 C: 레인지 브레이크아웃
    # ══════════════════════════════════════════

    def _check_setup_c(self, df_1m, df_5m, ctx) -> dict:
        """
        셋업 C: 레인지 브레이크아웃
        조건:
          1. 5m 20봉 레인지 형성 (range_pct < 1.5%)
          2. 종가가 레인지 상/하단 돌파
          3. 돌파 봉 거래량 > 평균 2배
          4. 1m에서 리테스트 시 레인지 안으로 복귀 안 함
        """
        r = {"match": False, "direction": "neutral", "score": 0.0,
             "reason": "", "sl_dist": 0, "tp_dist": 0, "reject_reason": ""}

        if len(df_5m) < 25:
            r["reject_reason"] = "insufficient_data"
            return r

        # 조건 1: 레인지 확인 (완화: 3% → 허용)
        lookback = df_5m.iloc[-21:-1]
        range_high = float(lookback["high"].max())
        range_low = float(lookback["low"].min())
        range_size = range_high - range_low
        price = float(df_5m["close"].iloc[-1])

        if range_size <= 0:
            r["reject_reason"] = "no_range"
            return r

        range_pct = range_size / price * 100
        if range_pct > 3.0 or range_pct < 0.03:
            r["reject_reason"] = f"range_pct({range_pct:.2f}%)"
            return r

        # 조건 2: 돌파
        direction = "neutral"
        if price > range_high:
            direction = "long"
            overshoot = (price - range_high) / range_size
        elif price < range_low:
            direction = "short"
            overshoot = (range_low - price) / range_size
        else:
            r["reject_reason"] = "no_breakout"
            return r

        if overshoot < 0.03:  # 0.05→0.03 완화
            r["reject_reason"] = f"low_overshoot({overshoot:.2f})"
            return r

        # 추세 방향과 일치하면 보너스 (불일치해도 브레이크아웃은 허용)
        trend_aligned = (ctx["trend"] == "up" and direction == "long") or \
                        (ctx["trend"] == "down" and direction == "short")

        # 조건 3: 거래량
        vol = df_5m["volume"].astype(float)
        vol_avg = float(vol.iloc[-21:-1].mean())
        vol_current = float(vol.iloc[-1])
        vol_ratio = vol_current / vol_avg if vol_avg > 0 else 1.0

        if vol_ratio < 1.2:  # 1.5→1.2 완화
            r["reject_reason"] = f"low_volume({vol_ratio:.1f}x)"
            return r

        # 조건 4: 리테스트 체크 제거 — 돌파 + 거래량으로 충분
        c1m = df_1m["close"].values.astype(float)
        if direction == "long":
            retested_inside = any(c1m[i] < range_high for i in range(-3, 0))
        else:
            retested_inside = any(c1m[i] > range_low for i in range(-3, 0))

        if retested_inside:
            # 복귀했다가 다시 나왔으면 OK, 아직 안이면 차단
            if direction == "long" and c1m[-1] < range_high:
                return r
            elif direction == "short" and c1m[-1] > range_low:
                return r

        # SL/TP
        if direction == "long":
            sl_dist = price - range_high + range_size * 0.2  # 레인지 상단 약간 아래
        else:
            sl_dist = range_low - price + range_size * 0.2
        sl_dist = max(sl_dist, price * 0.0035)
        tp_dist = range_size  # TP = 레인지 높이 투사

        score = 5.0
        score += min(1.5, overshoot * 3)
        score += min(1.5, (vol_ratio - 1.0))
        if trend_aligned:
            score += 1.5
        if not retested_inside:
            score += 0.5

        r.update({
            "match": True,
            "direction": direction,
            "score": round(min(10.0, score), 2),
            "reason": f"breakout: range={range_pct:.2f}% vol={vol_ratio:.1f}x overshoot={overshoot:.2f}",
            "sl_dist": round(sl_dist, 1),
            "tp_dist": round(tp_dist, 1),
            "range_high": range_high,
            "range_low": range_low,
            "vol_ratio": round(vol_ratio, 2),
        })
        return r

    # ══════════════════════════════════════════
    # 공통 헬퍼 메서드
    # ══════════════════════════════════════════

    def _check_bos(self, df_5m, direction) -> dict:
        """5m BOS (Break of Structure) 확인"""
        r = {"confirmed": False, "swing_level": 0.0}
        high = df_5m["high"].values.astype(float)
        low = df_5m["low"].values.astype(float)
        close = df_5m["close"].values.astype(float)

        swings = self._find_swing_points(high, low, order=3)
        if len(swings) < 2:
            return r

        price = float(close[-1])
        prev_price = float(close[-2])

        if direction == "long":
            # 직전 스윙 고점 돌파
            shs = [(i, p) for t, i, p in swings if t == "sh"]
            if shs:
                last_sh = shs[-1]
                if price > last_sh[1] and prev_price <= last_sh[1]:
                    r["confirmed"] = True
                    # SL 레벨 = 직전 스윙 저점
                    sls = [(i, p) for t, i, p in swings if t == "sl"]
                    r["swing_level"] = sls[-1][1] if sls else price * 0.995
        else:
            sls = [(i, p) for t, i, p in swings if t == "sl"]
            if sls:
                last_sl = sls[-1]
                if price < last_sl[1] and prev_price >= last_sl[1]:
                    r["confirmed"] = True
                    shs = [(i, p) for t, i, p in swings if t == "sh"]
                    r["swing_level"] = shs[-1][1] if shs else price * 1.005

        return r

    def _check_momentum_1m(self, df_1m, direction) -> dict:
        """1m 모멘텀 확인: 3봉 연속 또는 0.15%+ 이동"""
        r = {"confirmed": False, "strength": 0.0}
        close = df_1m["close"].values.astype(float)
        if len(close) < 5:
            return r

        changes = np.diff(close[-4:])  # 최근 3봉 변화

        if direction == "long":
            consecutive = all(c > 0 for c in changes)
            total_pct = (close[-1] - close[-4]) / close[-4] * 100
        else:
            consecutive = all(c < 0 for c in changes)
            total_pct = (close[-4] - close[-1]) / close[-4] * 100

        if consecutive or total_pct >= 0.15:
            r["confirmed"] = True
            r["strength"] = min(1.0, max(abs(total_pct) / 0.3, 0.5 if consecutive else 0))

        return r

    def _find_swing_points(self, high, low, order=3):
        """스윙 고/저점 탐지"""
        n = len(high)
        swings = []
        for i in range(order, n - order):
            is_sh = all(high[i] >= high[i - j] for j in range(1, order + 1)) and \
                     all(high[i] >= high[i + j] for j in range(1, order + 1))
            is_sl = all(low[i] <= low[i - j] for j in range(1, order + 1)) and \
                     all(low[i] <= low[i + j] for j in range(1, order + 1))
            if is_sh:
                swings.append(("sh", i, float(high[i])))
            if is_sl:
                swings.append(("sl", i, float(low[i])))
        return sorted(swings, key=lambda x: x[1])

    def _find_msb_order_blocks(self, df, swing_order=3, lookback=50):
        """MSB 기반 OB 탐지 (scalp_engine.py에서 가져온 v3 로직)"""
        if df is None or len(df) < 20:
            return []

        high = df["high"].values.astype(float)
        low = df["low"].values.astype(float)
        close = df["close"].values.astype(float)
        open_ = df["open"].values.astype(float)
        volume = df["volume"].values.astype(float)
        n = len(close)

        vol_avg = float(np.mean(volume[max(0, n - 30):n]))
        if vol_avg <= 0:
            vol_avg = 1.0

        swings = self._find_swing_points(high, low, order=swing_order)
        if len(swings) < 2:
            return []

        swing_highs = [(i, p) for t, i, p in swings if t == "sh"]
        swing_lows = [(i, p) for t, i, p in swings if t == "sl"]

        obs = []
        start = max(swing_order + 1, n - lookback)

        for bar in range(start, n):
            prev_shs = [(i, p) for i, p in swing_highs if i < bar - 1]
            prev_sls = [(i, p) for i, p in swing_lows if i < bar - 1]

            # Bullish MSB
            if prev_shs:
                last_sh_price = prev_shs[-1][1]
                if close[bar] > last_sh_price and close[bar - 1] <= last_sh_price:
                    ob = self._extract_ob(open_, close, high, low, volume, vol_avg, n, bar, "bullish")
                    if ob:
                        obs.append(ob)

            # Bearish MSB
            if prev_sls:
                last_sl_price = prev_sls[-1][1]
                if close[bar] < last_sl_price and close[bar - 1] >= last_sl_price:
                    ob = self._extract_ob(open_, close, high, low, volume, vol_avg, n, bar, "bearish")
                    if ob:
                        obs.append(ob)

        # 프레시 + FVG 체크
        for ob in obs:
            idx = ob["bar_idx"]
            ob["fresh"] = True
            for j in range(idx + 1, n - 1):
                if ob["dir"] == "long" and low[j] <= ob["high"]:
                    ob["fresh"] = False
                    break
                elif ob["dir"] == "short" and high[j] >= ob["low"]:
                    ob["fresh"] = False
                    break

            ob["has_fvg"] = False
            for j in range(idx, min(idx + 5, n - 2)):
                if ob["dir"] == "long" and high[j] < low[j + 2]:
                    ob["has_fvg"] = True
                    break
                elif ob["dir"] == "short" and low[j] > high[j + 2]:
                    ob["has_fvg"] = True
                    break

        return obs

    def _extract_ob(self, open_, close, high, low, volume, vol_avg, n, msb_bar, msb_type):
        """MSB 직전 OB 캔들 추출"""
        for i in range(msb_bar - 1, max(msb_bar - 8, -1), -1):
            if i < 0:
                break
            total = high[i] - low[i]
            if total <= 0:
                continue

            if msb_type == "bullish" and close[i] < open_[i]:
                iq = self._impulse_quality(open_, close, high, low, volume, vol_avg, i + 1, msb_bar)
                if iq < 0.35:
                    continue
                return {"dir": "long", "bar_idx": i, "low": float(low[i]),
                        "high": float(high[i]), "age": n - 1 - i, "impulse": {"quality": iq}}
            elif msb_type == "bearish" and close[i] > open_[i]:
                iq = self._impulse_quality(open_, close, high, low, volume, vol_avg, i + 1, msb_bar)
                if iq < 0.35:
                    continue
                return {"dir": "short", "bar_idx": i, "low": float(low[i]),
                        "high": float(high[i]), "age": n - 1 - i, "impulse": {"quality": iq}}
        return None

    def _impulse_quality(self, open_, close, high, low, volume, vol_avg, start, end):
        """임펄스 품질 (0~1)"""
        if start >= end or start < 0:
            return 0
        end = min(end + 1, len(close))
        bodies, ranges, vol_sum, dir_count, bars = 0, 0, 0, 0, 0
        for j in range(start, end):
            bodies += abs(close[j] - open_[j])
            ranges += max(high[j] - low[j], 1e-10)
            vol_sum += volume[j]
            bars += 1
            dir_count += 1 if close[j] > open_[j] else -1
        br = bodies / ranges if ranges > 0 else 0
        vs = (vol_sum / bars) / vol_avg if vol_avg > 0 and bars > 0 else 1
        cons = abs(dir_count) / max(bars, 1)
        return round(min(1.0, br / 0.7) * 0.4 + min(1.0, vs / 2.0) * 0.3 + cons * 0.3, 3)

    def _check_choch_1m(self, df_1m, direction) -> bool:
        """1m ChoCH 확인"""
        if len(df_1m) < 15:
            return False
        high = df_1m["high"].values.astype(float)
        low = df_1m["low"].values.astype(float)
        close = df_1m["close"].values.astype(float)
        swings = self._find_swing_points(high, low, order=2)
        if len(swings) < 2:
            return False
        if direction == "long":
            shs = [(i, p) for t, i, p in swings if t == "sh" and i < len(close) - 1]
            return bool(shs and close[-1] > shs[-1][1])
        else:
            sls = [(i, p) for t, i, p in swings if t == "sl" and i < len(close) - 1]
            return bool(sls and close[-1] < sls[-1][1])

    def _check_liquidity_clear(self, high, low, n, ob, direction) -> bool:
        """유동성 클리어 확인"""
        swings = self._find_swing_points(high, low, order=2)
        if direction == "long":
            for t, idx, price in swings:
                if t != "sl":
                    continue
                if ob["low"] * 0.998 < price < ob["high"]:
                    if not any(low[j] < price for j in range(idx + 1, n)):
                        return False
        else:
            for t, idx, price in swings:
                if t != "sh":
                    continue
                if ob["low"] < price < ob["high"] * 1.002:
                    if not any(high[j] > price for j in range(idx + 1, n)):
                        return False
        return True

    def _calc_atr(self, df, period=14) -> float:
        """ATR 계산"""
        high = df["high"].values.astype(float)
        low = df["low"].values.astype(float)
        close = df["close"].values.astype(float)
        if len(close) < period + 1:
            return 0
        tr = np.maximum(high[1:] - low[1:],
                        np.maximum(np.abs(high[1:] - close[:-1]),
                                   np.abs(low[1:] - close[:-1])))
        return float(np.mean(tr[-period:]))
