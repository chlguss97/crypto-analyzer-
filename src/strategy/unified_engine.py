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
    """멀티TF 셋업 매칭 엔진 + 오더플로우 방향 확인"""

    def __init__(self, redis=None):
        self.name = "unified"
        self.redis = redis  # 04-17: Redis 에서 CVD/펀딩/OI 실시간 데이터 읽기

    async def analyze(self, df_1m: pd.DataFrame, df_5m: pd.DataFrame,
                      df_15m: pd.DataFrame = None, df_1h: pd.DataFrame = None,
                      rt_velocity: dict = None,
                      df_4h: pd.DataFrame = None, df_1d: pd.DataFrame = None) -> dict:
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

        # ═══ 1단계: 시장 컨텍스트 (HTF 분석 — 4h/1d 큰 추세 포함) ═══
        ctx = await self._market_context(df_5m, df_15m, df_1h, df_4h, df_1d)
        result["signals"]["context"] = ctx

        # ATR 계산 (SL/TP용)
        atr = self._calc_atr(df_5m, 14)
        atr_pct = atr / float(df_5m["close"].iloc[-1]) * 100 if atr > 0 else 0.3
        result["atr"] = round(atr, 2)
        result["atr_pct"] = round(atr_pct, 4)

        # ═══ 2단계: 셋업 매칭 (우선순위: B > A > C) ═══

        # HTF 편향 점수 (방향 일치 가점, 역방향 감점 — 차단 아님)
        htf_bias = ctx.get("htf_bias", 0)

        def _apply_htf_bias(setup_result, setup_name, hold_mode):
            """셋업 score에 HTF 편향 반영 + result 채우기"""
            direction = setup_result["direction"]
            raw_score = setup_result["score"]

            # HTF 순방향 = 가점, 역방향 = 감점
            # htf_bias > 0 = 롱 유리. direction이 long이면 가점, short이면 감점
            if direction == "long":
                adjusted = raw_score + htf_bias
            else:  # short
                adjusted = raw_score - htf_bias  # htf_bias<0 이면 숏 유리 → 가점
            adjusted = round(max(0, min(10, adjusted)), 2)

            result["setup"] = setup_name
            result["direction"] = direction
            result["score"] = adjusted
            result["hold_mode"] = hold_mode
            result["sl_distance"] = setup_result["sl_dist"]
            result["tp_distance"] = setup_result["tp_dist"]
            result["signals"][f"setup_{setup_name.lower()}"] = setup_result
            result["signals"]["htf_bias_applied"] = round(htf_bias, 1)
            result["signals"]["raw_score"] = raw_score
            result["reason"] = setup_result["reason"]
            # HTF 추세 돌파 정보
            if ctx.get("breakout_1d"):
                result["reason"] += f" | 1d_breakout={ctx['breakout_1d']}"
            if ctx.get("breakout_4h"):
                result["reason"] += f" | 4h_breakout={ctx['breakout_4h']}"

        # 셋업 B: OB 리테스트 (최고 RR, 우선 체크)
        setup_b = self._check_setup_b(df_1m, df_5m, df_15m, ctx)
        if not setup_b["match"]:
            result["signals"]["reject_b"] = setup_b.get("reject_reason", "conditions_not_met")
        if setup_b["match"]:
            _apply_htf_bias(setup_b, "B", "standard")
            return result

        # 셋업 A: 추세 풀백 (가장 빈번)
        setup_a = self._check_setup_a(df_1m, df_5m, ctx)
        if not setup_a["match"]:
            result["signals"]["reject_a"] = setup_a.get("reject_reason", "conditions_not_met")
        if setup_a["match"]:
            _apply_htf_bias(setup_a, "A", "quick")
            return result

        # 셋업 C: 브레이크아웃
        setup_c = self._check_setup_c(df_1m, df_5m, ctx)
        if not setup_c["match"]:
            result["signals"]["reject_c"] = setup_c.get("reject_reason", "conditions_not_met")
        if setup_c["match"]:
            _apply_htf_bias(setup_c, "C", "standard")
            return result

        return result

    # ══════════════════════════════════════════
    # 시장 컨텍스트 (HTF 분석)
    # ══════════════════════════════════════════

    async def _market_context(self, df_5m, df_15m, df_1h, df_4h=None, df_1d=None) -> dict:
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

        # ═══ HTF 큰 추세 분석 (4h / 1d) ═══
        # 큰 추세 = 방향 편향 (가점/감점). 차단이 아님.
        # 하락 추세 속에서도 롱 가능 (지지 바운스, 과매도 반등, 숏스퀴즈)

        def _htf_trend(df, label):
            """HTF 추세 판단 + 추세 돌파 감지"""
            info = {"trend": "neutral", "breakout": None, "ema20": 0, "ema50": 0}
            if df is None or len(df) < 20:
                return info
            c = df["close"].astype(float)
            h = df["high"].astype(float)
            l = df["low"].astype(float)
            ema20 = float(c.ewm(span=20, adjust=False).mean().iloc[-1])
            ema50 = float(c.ewm(span=50, adjust=False).mean().iloc[-1])
            p = float(c.iloc[-1])
            info["ema20"] = round(ema20, 1)
            info["ema50"] = round(ema50, 1)

            if p > ema20 and ema20 > ema50:
                info["trend"] = "up"
            elif p < ema20 and ema20 < ema50:
                info["trend"] = "down"

            # 추세 돌파 감지 — 최근 캔들이 20봉 고/저점 돌파
            if len(df) >= 21:
                recent_high = float(h.iloc[-21:-1].max())
                recent_low = float(l.iloc[-21:-1].min())
                if p > recent_high:
                    info["breakout"] = "up"  # 상방 돌파
                elif p < recent_low:
                    info["breakout"] = "down"  # 하방 돌파

            return info

        htf_4h = _htf_trend(df_4h, "4h")
        htf_1d = _htf_trend(df_1d, "1d")

        ctx["trend_4h"] = htf_4h["trend"]
        ctx["trend_1d"] = htf_1d["trend"]
        ctx["breakout_4h"] = htf_4h["breakout"]
        ctx["breakout_1d"] = htf_1d["breakout"]

        # ═══ 종합 추세: 단기(5m/15m/1h)로 추세 판정 ═══
        # HTF는 "편향"으로만 사용 (차단 아님)
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

        # ═══ HTF 편향 점수 (셋업 score에 가감) ═══
        # 큰 추세 순방향이면 가점, 역방향이면 감점 (차단 아님)
        htf_bias = 0.0  # -3.0 ~ +3.0

        # 1d 편향 (가장 강함)
        if htf_1d["trend"] == "up":
            htf_bias += 1.5   # 일봉 상승 → 롱 가점
        elif htf_1d["trend"] == "down":
            htf_bias -= 1.5   # 일봉 하락 → 숏 가점(롱 감점)

        # 4h 편향
        if htf_4h["trend"] == "up":
            htf_bias += 1.0
        elif htf_4h["trend"] == "down":
            htf_bias -= 1.0

        # 추세 돌파 보너스 (매우 강한 시그널)
        if htf_1d["breakout"] == "up":
            htf_bias += 2.0   # 일봉 상방 돌파 → 강한 롱
        elif htf_1d["breakout"] == "down":
            htf_bias -= 2.0
        if htf_4h["breakout"] == "up":
            htf_bias += 1.0
        elif htf_4h["breakout"] == "down":
            htf_bias -= 1.0

        ctx["htf_bias"] = round(htf_bias, 1)
        # htf_bias > 0 → 롱 유리, < 0 → 숏 유리
        # 셋업 score 계산 시: 방향 일치하면 +htf_bias, 역방향이면 -htf_bias

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

        # ═══ 04-17: 오더플로우 방향 확인 (선행 지표) ═══
        # EMA 는 후행 → 이미 움직인 후 확인. CVD/펀딩/OI 는 움직이기 전 힘의 방향.
        # flow_bias: -1.0(강한 매도) ~ +1.0(강한 매수)
        flow_bias = 0.0
        ctx["flow_signals"] = {}

        if self.redis:
            try:
                # 1) CVD — OKX + Binance 합산 (시장 전체 플로우)
                #    합산 키가 있으면 사용, 없으면 OKX 단독
                combined_15m = await self.redis.get("flow:combined:cvd_15m")
                combined_1h = await self.redis.get("flow:combined:cvd_1h")
                if combined_15m is not None:
                    cvd_15m = float(combined_15m)
                    cvd_1h = float(combined_1h or 0)
                    ctx["flow_signals"]["cvd_source"] = "okx+binance"
                else:
                    cvd_15m = float(await self.redis.get("cvd:15m:current:BTC-USDT-SWAP") or 0)
                    cvd_1h = float(await self.redis.get("cvd:1h:current:BTC-USDT-SWAP") or 0)
                    ctx["flow_signals"]["cvd_source"] = "okx_only"

                cvd_dir = 0
                if cvd_15m > 0 and cvd_1h > 0:
                    cvd_dir = 0.4
                elif cvd_15m < 0 and cvd_1h < 0:
                    cvd_dir = -0.4
                elif cvd_1h > 0:
                    cvd_dir = 0.15
                elif cvd_1h < 0:
                    cvd_dir = -0.15
                flow_bias += cvd_dir
                ctx["flow_signals"]["cvd_15m"] = round(cvd_15m, 2)
                ctx["flow_signals"]["cvd_1h"] = round(cvd_1h, 2)
                ctx["flow_signals"]["cvd_dir"] = cvd_dir

                # 2) 대형 체결 편향 (Binance 고래 추적)
                whale_bias_str = await self.redis.get("flow:combined:whale_bias")
                whale_dir = 0
                if whale_bias_str:
                    wb = float(whale_bias_str)
                    # -1~+1 → ±0.2 가중치
                    whale_dir = round(wb * 0.2, 3)
                    flow_bias += whale_dir
                    ctx["flow_signals"]["whale_bias"] = round(wb, 3)
                    ctx["flow_signals"]["whale_dir"] = whale_dir

                # 3) 펀딩율 — 극단값이면 역방향 (군중 반대)
                funding = float(await self.redis.get("rt:funding:BTC-USDT-SWAP") or 0)
                fund_dir = 0
                if funding > 0.0003:
                    fund_dir = -0.2
                elif funding > 0.0001:
                    fund_dir = -0.1
                elif funding < -0.0003:
                    fund_dir = 0.2
                elif funding < -0.0001:
                    fund_dir = 0.1
                flow_bias += fund_dir
                ctx["flow_signals"]["funding"] = funding
                ctx["flow_signals"]["funding_dir"] = fund_dir

                # 4) 롱/숏 비율 — 극단이면 역방향
                ls_ratio = float(await self.redis.get("rt:ls_ratio:BTC-USDT-SWAP") or 1.0)
                ls_dir = 0
                if ls_ratio > 2.0:
                    ls_dir = -0.15
                elif ls_ratio > 1.5:
                    ls_dir = -0.05
                elif ls_ratio < 0.5:
                    ls_dir = 0.15
                elif ls_ratio < 0.67:
                    ls_dir = 0.05
                flow_bias += ls_dir
                ctx["flow_signals"]["ls_ratio"] = round(ls_ratio, 2)
                ctx["flow_signals"]["ls_dir"] = ls_dir

                # 5) 청산 폭발 감지 (Binance forceOrder)
                liq_surge_str = await self.redis.get("flow:liq:surge")
                if liq_surge_str:
                    try:
                        import json as _j
                        liq = _j.loads(liq_surge_str)
                        liq_bias = liq.get("bias", "")
                        liq_total = liq.get("total", 0)
                        liq_dir = 0.3 if liq_bias == "long" else -0.3 if liq_bias == "short" else 0
                        flow_bias += liq_dir
                        ctx["flow_signals"]["liq_surge"] = liq_total
                        ctx["flow_signals"]["liq_bias"] = liq_bias
                        ctx["flow_signals"]["liq_dir"] = liq_dir
                    except Exception:
                        pass

                # 6) OKX-Binance 프리미엄 (거래소간 가격 차이)
                premium_str = await self.redis.get("flow:okx_bn_premium")
                if premium_str:
                    premium = float(premium_str)
                    ctx["flow_signals"]["okx_bn_premium"] = round(premium, 4)
                    # 프리미엄 > 0.02% = OKX가 비쌈 → OKX 매수 과열
                    # 프리미엄 < -0.02% = OKX가 쌈 → OKX 매도 과열
                    if abs(premium) > 0.02:
                        prem_dir = -0.1 if premium > 0 else 0.1
                        flow_bias += prem_dir
                        ctx["flow_signals"]["premium_dir"] = prem_dir

            except Exception as e:
                import logging
                logging.getLogger(__name__).debug(f"오더플로우 조회 실패: {e}")

        ctx["flow_bias"] = round(flow_bias, 3)

        # ═══ 오더플로우와 EMA 추세가 충돌하면 → neutral 로 격하 ═══
        # EMA 는 up 인데 CVD+펀딩이 매도 → 추세 전환 직전일 가능성
        if ctx["trend"] == "up" and flow_bias < -0.2:
            ctx["trend"] = "neutral"
            ctx["trend_strength"] = 0.0
            ctx["flow_signals"]["override"] = "trend_up_but_flow_bearish"
        elif ctx["trend"] == "down" and flow_bias > 0.2:
            ctx["trend"] = "neutral"
            ctx["trend_strength"] = 0.0
            ctx["flow_signals"]["override"] = "trend_down_but_flow_bullish"

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
        셋업 A: 추세 내 풀백 진입 (Buy the Dip / Sell the Rally)

        핵심: 추세 방향은 맞지만 가격이 눌렸을 때 진입
        → 꼭대기 추격 대신 저점 매수

        조건:
          1. 15m/5m 추세 명확 (2+ TF)
          2. 가격이 5m EMA20 근처까지 풀백 (EMA20 ± 0.2% 이내)
          3. 1m 반등 캔들 확인 (풀백 후 추세 방향 캔들)
          4. 추세 구조 유지 (HH/HL 또는 LH/LL)
        """
        r = {"match": False, "direction": "neutral", "score": 0.0,
             "reason": "", "sl_dist": 0, "tp_dist": 0, "reject_reason": ""}

        # 조건 1: 추세 명확
        if ctx["trend"] == "neutral":
            r["reject_reason"] = "no_trend"
            return r
        direction = "long" if ctx["trend"] == "up" else "short"

        # 추세 구조 확인 (횡보 차단)
        if ctx["structure"] not in ("hh_hl", "lh_ll"):
            r["reject_reason"] = f"no_trend_structure({ctx['structure']})"
            return r
        # 구조와 추세 방향 일치 확인
        if direction == "long" and ctx["structure"] != "hh_hl":
            r["reject_reason"] = "structure_mismatch"
            return r
        if direction == "short" and ctx["structure"] != "lh_ll":
            r["reject_reason"] = "structure_mismatch"
            return r

        price = ctx["price"]
        ema20 = ctx.get("ema20_5m", 0)
        ema50 = ctx.get("ema50_5m", 0)

        if ema20 <= 0:
            r["reject_reason"] = "no_ema20"
            return r

        # 조건 2: 풀백 확인 — 가격이 EMA20 근처 (± 0.3%)
        distance_to_ema20 = (price - ema20) / ema20 * 100  # %

        if direction == "long":
            # 롱: 가격이 EMA20 바로 위 ~ EMA20 약간 아래 (풀백 상태)
            # -0.1% ~ +0.3% 범위 = EMA20 터치 근처
            if distance_to_ema20 > 0.3:
                r["reject_reason"] = f"too_far_above_ema20({distance_to_ema20:+.2f}%)"
                return r
            if distance_to_ema20 < -0.3:
                r["reject_reason"] = f"too_far_below_ema20({distance_to_ema20:+.2f}%)"
                return r
        else:
            # 숏: 가격이 EMA20 바로 아래 ~ EMA20 약간 위 (풀백 상태)
            if distance_to_ema20 < -0.3:
                r["reject_reason"] = f"too_far_below_ema20({distance_to_ema20:+.2f}%)"
                return r
            if distance_to_ema20 > 0.3:
                r["reject_reason"] = f"too_far_above_ema20({distance_to_ema20:+.2f}%)"
                return r

        # 조건 3: 1m 반등 캔들 확인 (풀백 후 추세 방향으로 반등)
        c1m = df_1m.iloc[-1]
        c1m_prev = df_1m.iloc[-2] if len(df_1m) >= 2 else c1m

        if direction == "long":
            # 이전 봉이 하락(풀백)이고 현재 봉이 양봉(반등)
            prev_bearish = float(c1m_prev["close"]) < float(c1m_prev["open"])
            cur_bullish = float(c1m["close"]) > float(c1m["open"])
            bounce = prev_bearish and cur_bullish
            # 또는: 현재 봉이 양봉이고 저가가 EMA20 근처
            touch_ema = abs(float(c1m["low"]) - ema20) / ema20 * 100 < 0.15
            if not (bounce or (cur_bullish and touch_ema)):
                r["reject_reason"] = "no_bounce_candle"
                return r
        else:
            prev_bullish = float(c1m_prev["close"]) > float(c1m_prev["open"])
            cur_bearish = float(c1m["close"]) < float(c1m["open"])
            bounce = prev_bullish and cur_bearish
            touch_ema = abs(float(c1m["high"]) - ema20) / ema20 * 100 < 0.15
            if not (bounce or (cur_bearish and touch_ema)):
                r["reject_reason"] = "no_bounce_candle"
                return r

        # 전부 충족 → 풀백 저점에서 진입
        # SL: 풀백 저점 아래 (롱) 또는 고점 위 (숏)
        high_5m = df_5m["high"].values.astype(float)
        low_5m = df_5m["low"].values.astype(float)

        # 최근 5봉의 저점/고점을 SL로
        if direction == "long":
            recent_low = float(np.min(low_5m[-5:]))
            sl_level = recent_low
            # TP: 최근 5m 고점
            recent_high = float(np.max(high_5m[-20:]))
            tp_target = recent_high
        else:
            recent_high = float(np.max(high_5m[-5:]))
            sl_level = recent_high
            recent_low = float(np.min(low_5m[-20:]))
            tp_target = recent_low

        sl_dist = abs(price - sl_level)
        sl_dist = max(sl_dist, price * 0.002)      # 최소 0.2% (풀백이라 타이트)
        sl_dist = min(sl_dist, price * 0.006)       # 최대 0.6%
        tp_dist = abs(price - tp_target)
        tp_dist = max(tp_dist, sl_dist * 1.5)       # 최소 RR 1.5

        score = 6.0  # 풀백 진입은 높은 기본 점수
        score += min(1.5, ctx["trend_strength"] * 2)
        if bounce:
            score += 1.0  # 반등 캔들 보너스
        if touch_ema:
            score += 0.5  # EMA 터치 보너스

        r.update({
            "match": True,
            "direction": direction,
            "score": round(min(10.0, score), 2),
            "reason": f"pullback_entry: ema20_dist={distance_to_ema20:+.2f}% bounce={'Y' if bounce else 'N'}",
            "sl_dist": round(sl_dist, 1),
            "tp_dist": round(tp_dist, 1),
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

        # 조건 4: 1m 반전 확인 (ChoCH OR 최근 2봉 중 1봉+ 반전 방향)
        choch = self._check_choch_1m(df_1m, direction)
        c1m_cur = df_1m.iloc[-1]
        c1m_prev = df_1m.iloc[-2] if len(df_1m) >= 2 else c1m_cur
        cur_confirm = (direction == "long" and float(c1m_cur["close"]) > float(c1m_cur["open"])) or \
                      (direction == "short" and float(c1m_cur["close"]) < float(c1m_cur["open"]))
        prev_confirm = (direction == "long" and float(c1m_prev["close"]) > float(c1m_prev["open"])) or \
                       (direction == "short" and float(c1m_prev["close"]) < float(c1m_prev["open"]))
        if not choch and not (cur_confirm and prev_confirm):
            r["reject_reason"] = "no_reversal_2candles"
            return r

        # 비프레시 OB 감점
        if not best_ob.get("fresh", False):
            # 비프레시 = 이미 한 번 터치된 OB → 신뢰도 낮음

            pass  # 아래 score에서 감점

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
        if not best_ob.get("fresh", False):
            score -= 1.5  # 비프레시 OB 감점
        if liq_clear:
            score += 0.5
        else:
            score -= 1.0  # 유동성 위험 (1.5→1.0 완화)

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

        if overshoot < 0.05:
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

        if vol_ratio < 1.3:
            r["reject_reason"] = f"low_volume({vol_ratio:.1f}x)"
            return r

        # 조건 4: 현재 봉이 레인지 밖에 있는지 (페이크아웃 방지)
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
