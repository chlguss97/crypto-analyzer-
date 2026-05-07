"""
CandidateDetector v1 — 단순 후보 감지 + 피처 추출

3종 후보:
  A. Momentum Ignition  — 큰 캔들 + 큰 거래량 (Jegadeesh & Titman)
  B. Volatility Breakout — BB 스퀴즈→돌파 (Bollinger, Turtle Trading)
  C. Liquidation Cascade — $500K+ 청산 폭주 (crypto 고유)

후보 감지만 담당. 진입 결정은 ML(ml_engine.py)이 함.
"""

import json
import logging
import math
import numpy as np
import pandas as pd
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class CandidateDetector:
    """
    시장 후보 감지기.
    "시장이 움직이고 있는가? 어느 방향?" 만 판단.
    """

    def __init__(self, redis=None, config=None):
        self.redis = redis
        cfg = (config or {}).get("candidate", {})
        # Momentum 설정
        mom = cfg.get("momentum", {})
        self.mom_body_atr = mom.get("min_body_atr_ratio", 0.8)
        self.mom_vol_ratio = mom.get("min_vol_ratio", 1.3)
        self.mom_body_ratio = mom.get("min_body_ratio", 0.6)
        # Breakout 설정
        brk = cfg.get("breakout", {})
        self.brk_squeeze_pctl = brk.get("bb_squeeze_pctl", 25)
        self.brk_vol_ratio = brk.get("min_vol_ratio", 1.2)
        # Cascade 설정
        cas = cfg.get("cascade", {})
        self.cas_min_liq = cas.get("min_liq_usd", 500_000)
        self.cas_min_bias = cas.get("min_bias_pct", 0.80)
        self.cas_min_change = cas.get("min_price_change_pct", 0.2)

    async def detect(self, df_1m, df_5m, df_15m=None, df_1h=None,
                     df_4h=None, df_1d=None) -> dict | None:
        """
        후보 감지. 3종 중 가장 강한 것 1개 반환. 없으면 None.

        Returns:
            {
                "type": "momentum"|"breakout"|"cascade",
                "direction": "long"|"short",
                "strength": float,
                "hold_mode": str,
                "price": float,
                "atr": float,
                "atr_pct": float,
                "features_raw": dict,  # 피처 추출용 원시 데이터
            }
        """
        if df_5m is None or len(df_5m) < 30:
            return None

        price = float(df_5m["close"].iloc[-1])
        atr = self._atr(df_5m, 14)
        if atr <= 0 or price <= 0:
            return None
        atr_pct = atr / price * 100

        # 공통 데이터
        vol_20avg = float(df_5m["volume"].astype(float).tail(20).mean()) if len(df_5m) >= 20 else 1.0
        flow = await self._get_flow_data()

        # 3종 후보 평가
        candidates = []

        mom = self._check_momentum_ignition(df_5m, price, atr, vol_20avg)
        if mom:
            mom["features_raw"] = await self._build_raw_features(
                df_5m, df_15m, df_1h, df_4h, df_1d, price, atr, atr_pct, flow, vol_20avg, mom["direction"], df_1m=df_1m
            )
            candidates.append(mom)

        brk = self._check_volatility_breakout(df_5m, price, atr, vol_20avg)
        if brk:
            brk["features_raw"] = await self._build_raw_features(
                df_5m, df_15m, df_1h, df_4h, df_1d, price, atr, atr_pct, flow, vol_20avg, brk["direction"], df_1m=df_1m
            )
            candidates.append(brk)

        cas = await self._check_liquidation_cascade(df_5m, price, atr, flow)
        if cas:
            cas["features_raw"] = await self._build_raw_features(
                df_5m, df_15m, df_1h, df_4h, df_1d, price, atr, atr_pct, flow, vol_20avg, cas["direction"], df_1m=df_1m
            )
            candidates.append(cas)

        if not candidates:
            # 약한 후보 감지 (shadow 전용 — ML 데이터 가속)
            weak = self._check_weak_momentum(df_5m, price, atr, vol_20avg)
            if weak:
                weak["features_raw"] = await self._build_raw_features(
                    df_5m, df_15m, df_1h, df_4h, df_1d, price, atr, atr_pct, flow, vol_20avg, weak["direction"], df_1m=df_1m
                )
                weak["atr"] = round(atr, 2)
                weak["atr_pct"] = round(atr_pct, 4)
                weak["price"] = round(price, 1)
                weak["weak"] = True  # shadow 전용 표시
                return weak
            return None

        # 가장 강한 후보 선택
        best = max(candidates, key=lambda x: x["strength"])
        best["atr"] = round(atr, 2)
        best["atr_pct"] = round(atr_pct, 4)
        best["price"] = round(price, 1)
        best["weak"] = False

        # 최근 3캔들 대비 소진된 모멘텀 비율
        recent_highs = df_5m["high"].astype(float).iloc[-4:-1]
        recent_lows = df_5m["low"].astype(float).iloc[-4:-1]
        if best["direction"] == "long":
            recent_move = price - float(recent_lows.min())
        else:
            recent_move = float(recent_highs.max()) - price
        best["recent_move_pct"] = round(recent_move / price * 100, 4)
        return best

    # ════════════════════════════════════════
    #  1분 고속 감지 (Fast Momentum)
    # ════════════════════════════════════════

    def detect_fast(self, df_1m, df_5m=None) -> dict | None:
        """1분 캔들 기반 고속 모멘텀 감지 (ATR×1.5 이상만)
        5분 detect()보다 빠르게 강한 움직임 포착.
        """
        if df_1m is None or len(df_1m) < 20:
            return None

        price = float(df_1m["close"].iloc[-1])
        atr_1m = self._atr(df_1m, 14)
        if atr_1m <= 0 or price <= 0:
            return None

        c = df_1m.iloc[-2]  # 직전 완성 1분봉
        o, h, l, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
        vol = float(c["volume"])
        body = cl - o
        candle_range = h - l
        if candle_range <= 0:
            return None

        body_ratio = abs(body) / candle_range
        body_atr_ratio = abs(body) / atr_1m if atr_1m > 0 else 0
        vol_20avg = float(df_1m["volume"].astype(float).tail(20).mean()) if len(df_1m) >= 20 else 1.0
        vol_ratio = vol / vol_20avg if vol_20avg > 0 else 0

        # 엄격 기준: ATR×1.5 + 거래량 1.5배 + 몸통비 0.6 (노이즈 방지)
        if body_atr_ratio < 1.5:
            return None
        if vol_ratio < 1.5:
            return None
        if body_ratio < 0.6:
            return None

        direction = "long" if body > 0 else "short"
        atr_pct = atr_1m / price * 100

        # 5분 ATR 대비 확인 (5분 기준으로도 의미 있는 움직임인지)
        if df_5m is not None and len(df_5m) >= 14:
            atr_5m = self._atr(df_5m, 14)
            if abs(body) < atr_5m * 0.5:
                return None  # 5분 ATR의 절반도 안 되면 무시

        # 최근 3캔들 모멘텀 소진 체크
        recent_lows = df_1m["low"].astype(float).iloc[-4:-1]
        recent_highs = df_1m["high"].astype(float).iloc[-4:-1]
        if direction == "long":
            recent_move = price - float(recent_lows.min())
        else:
            recent_move = float(recent_highs.max()) - price
        recent_move_pct = round(recent_move / price * 100, 4)

        return {
            "type": "fast_momentum",
            "direction": direction,
            "strength": round(body_atr_ratio, 2),
            "hold_mode": "quick",
            "vol_ratio": round(vol_ratio, 2),
            "price": round(price, 1),
            "atr": round(atr_1m, 2),
            "atr_pct": round(atr_pct, 4),
            "weak": False,
            "recent_move_pct": recent_move_pct,
        }

    # ════════════════════════════════════════
    #  약한 후보 (Shadow 전용 — ML 데이터 가속)
    # ════════════════════════════════════════

    def _check_weak_momentum(self, df_5m, price, atr, vol_20avg) -> dict | None:
        """정규 후보 조건의 절반으로 감지 — 진입 안 하고 shadow로만 추적"""
        c = df_5m.iloc[-2]
        body = float(c["close"]) - float(c["open"])
        candle_range = float(c["high"]) - float(c["low"])
        if candle_range <= 0:
            return None

        body_ratio = abs(body) / candle_range
        body_atr_ratio = abs(body) / atr if atr > 0 else 0
        vol = float(c["volume"])
        vol_ratio = vol / vol_20avg if vol_20avg > 0 else 0

        # 정규: 0.8/1.3/0.6 → 약한: 0.4/1.0/0.4
        if body_atr_ratio < 0.4:
            return None
        if vol_ratio < 1.0:
            return None
        if body_ratio < 0.4:
            return None

        direction = "long" if body > 0 else "short"
        return {
            "type": "weak_momentum",
            "direction": direction,
            "strength": round(body_atr_ratio, 2),
            "hold_mode": "momentum",
            "vol_ratio": round(vol_ratio, 2),
        }

    # ════════════════════════════════════════
    #  후보 A: Momentum Ignition
    # ════════════════════════════════════════

    def _check_momentum_ignition(self, df_5m, price, atr, vol_20avg) -> dict | None:
        """큰 캔들 + 큰 거래량 = 진짜 움직임의 시작"""
        c = df_5m.iloc[-2]  # 직전 완성 봉 (현재 봉은 미완성)
        o, h, l, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
        vol = float(c["volume"])
        body = cl - o
        candle_range = h - l
        if candle_range <= 0:
            return None

        body_ratio = abs(body) / candle_range
        body_atr_ratio = abs(body) / atr if atr > 0 else 0
        vol_ratio = vol / vol_20avg if vol_20avg > 0 else 0

        if body_atr_ratio < self.mom_body_atr:
            return None
        if vol_ratio < self.mom_vol_ratio:
            return None
        if body_ratio < self.mom_body_ratio:
            return None

        direction = "long" if body > 0 else "short"
        return {
            "type": "momentum",
            "direction": direction,
            "strength": round(body_atr_ratio, 2),
            "hold_mode": "momentum",
            "vol_ratio": round(vol_ratio, 2),
        }

    # ════════════════════════════════════════
    #  후보 B: Volatility Breakout (BB 스퀴즈→돌파)
    # ════════════════════════════════════════

    def _check_volatility_breakout(self, df_5m, price, atr, vol_20avg) -> dict | None:
        """BB 스퀴즈 상태에서 돌파 감지"""
        if len(df_5m) < 100:
            return None

        closes = df_5m["close"].astype(float)
        vol = float(df_5m.iloc[-2]["volume"])
        vol_ratio = vol / vol_20avg if vol_20avg > 0 else 0

        # BB 계산 (20, 2)
        bb_mid = closes.rolling(20).mean()
        bb_std = closes.rolling(20).std()
        bb_upper = bb_mid + 2 * bb_std
        bb_lower = bb_mid - 2 * bb_std
        bb_width = (bb_upper - bb_lower) / bb_mid * 100  # % 기준

        current_width = float(bb_width.iloc[-1])
        # 최근 100봉 중 현재 BB 폭의 백분위
        recent_widths = bb_width.tail(100).dropna()
        if len(recent_widths) < 20:
            return None
        pctl = float((recent_widths < current_width).sum() / len(recent_widths) * 100)

        upper = float(bb_upper.iloc[-1])
        lower = float(bb_lower.iloc[-1])

        # 스퀴즈 상태 (하위 25%) + 돌파
        if pctl > self.brk_squeeze_pctl:
            return None
        if vol_ratio < self.brk_vol_ratio:
            return None

        if price > upper:
            direction = "long"
            mid = float(bb_mid.iloc[-1])
            strength = (price - mid) / (upper - mid) if upper > mid else 1.0
        elif price < lower:
            direction = "short"
            mid = float(bb_mid.iloc[-1])
            strength = (mid - price) / (mid - lower) if mid > lower else 1.0
        else:
            return None  # 밴드 안 = 돌파 아님

        return {
            "type": "breakout",
            "direction": direction,
            "strength": round(min(3.0, strength), 2),
            "hold_mode": "breakout",
            "bb_width_pctl": round(pctl, 1),
            "vol_ratio": round(vol_ratio, 2),
        }

    # ════════════════════════════════════════
    #  후보 C: Liquidation Cascade
    # ════════════════════════════════════════

    async def _check_liquidation_cascade(self, df_5m, price, atr, flow) -> dict | None:
        """대량 청산 폭주 감지"""
        if not self.redis:
            return None
        try:
            liq_total = float(await self.redis.get("flow:liq:1m_total") or 0)
            liq_long = float(await self.redis.get("flow:liq:1m_long") or 0)
            liq_short = float(await self.redis.get("flow:liq:1m_short") or 0)
        except Exception:
            return None

        if liq_total < self.cas_min_liq:
            return None

        # 편중도
        bias = max(liq_long, liq_short) / liq_total if liq_total > 0 else 0
        if bias < self.cas_min_bias:
            return None

        # 5분 가격 변동
        if len(df_5m) < 2:
            return None
        prev_close = float(df_5m["close"].iloc[-2])
        change_pct = abs(price - prev_close) / prev_close * 100
        if change_pct < self.cas_min_change:
            return None

        # 롱 청산 폭주 → short 진입 (가격 떨어지는 쪽), 반대도 동일
        if liq_long > liq_short:
            direction = "short"
        else:
            direction = "long"

        strength = liq_total / 1_000_000  # $1M = 1.0

        return {
            "type": "cascade",
            "direction": direction,
            "strength": round(min(5.0, strength), 2),
            "hold_mode": "cascade",
            "liq_total": round(liq_total, 0),
            "liq_bias": round(bias, 2),
        }

    # ════════════════════════════════════════
    #  피처 추출
    # ════════════════════════════════════════

    async def _build_raw_features(self, df_5m, df_15m, df_1h, df_4h, df_1d,
                                  price, atr, atr_pct, flow, vol_20avg, direction,
                                  df_1m=None) -> dict:
        """ML용 피처 원시 데이터 구축 (8개 핵심 + 확장용)"""
        closes_5m = df_5m["close"].astype(float)

        # 1. price_momentum: 5봉(25분) 변동%
        pm = 0.0
        if len(closes_5m) >= 6:
            pm = (float(closes_5m.iloc[-1]) - float(closes_5m.iloc[-6])) / float(closes_5m.iloc[-6]) * 100

        # 2. trend_strength: (EMA8 - EMA21) / ATR
        ema8 = float(closes_5m.ewm(span=8, adjust=False).mean().iloc[-1])
        ema21 = float(closes_5m.ewm(span=min(21, len(closes_5m)-1), adjust=False).mean().iloc[-1])
        ts = (ema8 - ema21) / atr if atr > 0 else 0

        # 3. cvd_norm: CVD_5m / 거래량 (정규화)
        cvd_5m_raw = flow.get("cvd_5m", 0)
        total_vol_5m = float(df_5m["volume"].astype(float).tail(1).iloc[0]) if len(df_5m) > 0 else 1.0
        cvd_norm = cvd_5m_raw / max(total_vol_5m, 1e-10)
        cvd_norm = max(-1.0, min(1.0, cvd_norm))

        # 4. cvd_matches: CVD 부호가 방향과 일치
        cvd_matches = 1 if (direction == "long" and cvd_5m_raw > 0) or \
                           (direction == "short" and cvd_5m_raw < 0) else 0

        # 5. vol_ratio
        last_vol = float(df_5m["volume"].astype(float).iloc[-2]) if len(df_5m) >= 2 else 0
        vol_ratio = last_vol / vol_20avg if vol_20avg > 0 else 1.0

        # 6. adx (15m에서 계산, 없으면 5m)
        adx = self._calc_adx(df_15m if df_15m is not None and len(df_15m) >= 30 else df_5m)

        # 7. bb_position: (price - BB_lower) / (BB_upper - BB_lower)
        bb_pos = 0.5
        if len(closes_5m) >= 20:
            bb_mid = float(closes_5m.rolling(20).mean().iloc[-1])
            bb_std = float(closes_5m.rolling(20).std().iloc[-1])
            if bb_std > 0:
                bb_upper = bb_mid + 2 * bb_std
                bb_lower = bb_mid - 2 * bb_std
                bb_pos = (price - bb_lower) / (bb_upper - bb_lower) if bb_upper > bb_lower else 0.5

        # 8. hour_sin
        hour = datetime.now(timezone.utc).hour
        hour_sin = math.sin(2 * math.pi * hour / 24)

        features = {
            # 핵심 8개
            "price_momentum": round(pm, 4),
            "trend_strength": round(ts, 4),
            "cvd_norm": round(cvd_norm, 4),
            "cvd_matches": cvd_matches,
            "vol_ratio": round(vol_ratio, 2),
            "adx": round(adx, 1),
            "bb_position": round(bb_pos, 4),
            "hour_sin": round(hour_sin, 4),
        }

        # 확장 피처 (500건 후 사용, 지금은 수집만)
        hour_cos = math.cos(2 * math.pi * hour / 24)
        price_15m = 0.0
        if df_15m is not None and len(df_15m) >= 2:
            c15 = df_15m["close"].astype(float)
            price_15m = (float(c15.iloc[-1]) - float(c15.iloc[-2])) / float(c15.iloc[-2]) * 100
        price_1h = 0.0
        if df_1h is not None and len(df_1h) >= 2:
            c1h = df_1h["close"].astype(float)
            price_1h = (float(c1h.iloc[-1]) - float(c1h.iloc[-2])) / float(c1h.iloc[-2]) * 100

        cvd_15m_raw = flow.get("cvd_15m", 0)
        whale_bias = flow.get("whale_bias", 0)
        liq_pressure = 1.0 if flow.get("liq_active") else 0.0

        # EMA50 거리
        ema50 = float(closes_5m.ewm(span=min(50, len(closes_5m)-1), adjust=False).mean().iloc[-1])
        price_vs_ema50 = (price - ema50) / ema50 * 100 if ema50 > 0 else 0

        # 캔들 몸통 비율 (최근 5봉 평균)
        body_ratios = []
        for i in range(-6, -1):
            if abs(i) <= len(df_5m):
                row = df_5m.iloc[i]
                r = abs(float(row["close"]) - float(row["open"])) / max(float(row["high"]) - float(row["low"]), 1e-10)
                body_ratios.append(r)
        avg_body_ratio = sum(body_ratios) / len(body_ratios) if body_ratios else 0.5

        # 거래량 추세 (3봉)
        vol_trend = 0
        if len(df_5m) >= 4:
            v = df_5m["volume"].astype(float)
            if float(v.iloc[-2]) > float(v.iloc[-3]) > float(v.iloc[-4]):
                vol_trend = 1
            elif float(v.iloc[-2]) < float(v.iloc[-3]) < float(v.iloc[-4]):
                vol_trend = -1

        # 1분 거래량
        vol_ratio_1m = 0.0
        if df_1m is not None and len(df_1m) >= 21:
            v1m = df_1m["volume"].astype(float)
            avg_1m = float(v1m.tail(20).mean())
            vol_ratio_1m = float(v1m.iloc[-1]) / max(avg_1m, 1e-10)

        features.update({
            "price_change_15m": round(price_15m, 4),
            "price_change_1h": round(price_1h, 4),
            "cvd_15m_norm": round(cvd_15m_raw / max(total_vol_5m, 1e-10), 4),
            "whale_bias": round(whale_bias, 4),
            "liq_pressure": liq_pressure,
            "atr_pct": round(atr_pct, 4),
            "bb_width_pctl": 0.0,
            "vol_trend": vol_trend,
            "regime_score": adx,
            "di_spread": 0.0,
            "hour_cos": round(hour_cos, 4),
            "candle_body_ratio": round(avg_body_ratio, 4),
            "price_vs_ema50": round(price_vs_ema50, 4),
            "vol_ratio_1m": round(vol_ratio_1m, 2),
        })

        # ── 마이크로스트럭처 피처 (Redis에서 실시간 읽기) ──
        micro = await self._get_micro_features() if self.redis else {}
        features.update(micro)

        return features

    # ════════════════════════════════════════
    #  유틸리티
    # ════════════════════════════════════════

    def _atr(self, df, period=14) -> float:
        if df is None or len(df) < period:
            return 0.0
        h = df["high"].astype(float)
        l = df["low"].astype(float)
        c = df["close"].astype(float)
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        return float(tr.rolling(period).mean().iloc[-1]) if len(tr) >= period else float(tr.mean())

    def _ema(self, df, span) -> float:
        if df is None or len(df) < 2:
            return 0.0
        return float(df["close"].astype(float).ewm(span=min(span, len(df)-1), adjust=False).mean().iloc[-1])

    def _rsi(self, df, period=14) -> float:
        if df is None or len(df) < period + 1:
            return 50.0
        delta = df["close"].astype(float).diff()
        gain = delta.where(delta > 0, 0.0).ewm(alpha=1/period, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/period, adjust=False).mean()
        rs = gain / loss.replace(0, 1e-10)
        rsi = 100 - 100 / (1 + rs)
        return float(rsi.iloc[-1])

    def _calc_adx(self, df, period=14) -> float:
        """ADX 계산 (추세 강도 0~100)"""
        if df is None or len(df) < period * 2:
            return 20.0  # 기본값: 약한 추세
        h = df["high"].astype(float)
        l = df["low"].astype(float)
        c = df["close"].astype(float)

        # +DM, -DM
        up = h.diff()
        down = -l.diff()
        plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0), index=h.index)
        minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0), index=h.index)

        # TR
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)

        # Wilder smoothing
        atr = tr.ewm(alpha=1/period, adjust=False).mean()
        plus_di = 100 * (plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, 1e-10))
        minus_di = 100 * (minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, 1e-10))

        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-10)
        adx = dx.ewm(alpha=1/period, adjust=False).mean()

        return float(adx.iloc[-1]) if not pd.isna(adx.iloc[-1]) else 20.0

    def _ema_trend(self, df) -> str:
        """상위 TF 추세 판단 (호환용)"""
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

    async def _get_micro_features(self) -> dict:
        """Redis에서 마이크로스트럭처 피처 읽기 (binance_stream이 2초마다 갱신)"""
        micro = {}
        if not self.redis:
            return micro
        try:
            # 단순 숫자 키
            for key, name, default in [
                ("rt:micro:trade_rate", "micro_trade_rate", 0.0),
                ("rt:micro:trade_burst", "micro_burst", 1.0),
                ("rt:micro:bs_ratio_5s", "micro_bs_5s", 0.5),
                ("rt:micro:bs_ratio_30s", "micro_bs_30s", 0.5),
                ("rt:micro:bs_ratio_60s", "micro_bs_60s", 0.5),
                ("rt:micro:delta_accel", "micro_delta_accel", 0.0),
                ("rt:micro:price_impact", "micro_price_impact", 0.0),
                ("rt:micro:delta_div", "micro_delta_div", 0),
                ("rt:micro:momentum_quality", "micro_momentum_quality", 0.0),
            ]:
                val = await self.redis.get(key)
                micro[name] = float(val) if val else default

            # JSON 키
            absorption_str = await self.redis.get("rt:micro:absorption")
            if absorption_str:
                ab = json.loads(absorption_str)
                micro["micro_absorption_score"] = float(ab.get("score", 0))
                micro["micro_absorption_dir"] = 1 if ab.get("direction") == "long" else (-1 if ab.get("direction") == "short" else 0)
            else:
                micro["micro_absorption_score"] = 0.0
                micro["micro_absorption_dir"] = 0

            cluster_str = await self.redis.get("rt:micro:whale_cluster")
            if cluster_str:
                cl = json.loads(cluster_str)
                micro["micro_whale_cluster"] = float(cl.get("score", 0))
                micro["micro_whale_streak"] = int(cl.get("max_streak", 0))
            else:
                micro["micro_whale_cluster"] = 0.0
                micro["micro_whale_streak"] = 0

            vwap_str = await self.redis.get("rt:micro:vwap")
            if vwap_str:
                vw = json.loads(vwap_str)
                micro["micro_vwap_dev"] = float(vw.get("deviation_pct", 0))
            else:
                micro["micro_vwap_dev"] = 0.0

        except Exception as e:
            logger.debug(f"micro features read error: {e}")
        return micro

    async def _get_flow_data(self) -> dict:
        """CVD + 고래 + 청산 데이터 조회"""
        flow = {
            "direction": "neutral", "strength": 0.0,
            "cvd_5m": 0, "cvd_15m": 0, "cvd_1h": 0,
            "whale_bias": 0.0, "liq_active": False,
        }

        if not self.redis:
            return flow

        try:
            cvd_5m = float(await self.redis.get("flow:combined:cvd_5m") or 0)
            cvd_15m = float(await self.redis.get("flow:combined:cvd_15m") or 0)
            cvd_1h = float(await self.redis.get("flow:combined:cvd_1h") or 0)

            flow["cvd_5m"] = round(cvd_5m, 2)
            flow["cvd_15m"] = round(cvd_15m, 2)
            flow["cvd_1h"] = round(cvd_1h, 2)

            # CVD 방향
            if cvd_5m > 0.3:
                flow["direction"] = "long"
                flow["strength"] = min(1.0, abs(cvd_5m) / 50)
            elif cvd_5m < -0.3:
                flow["direction"] = "short"
                flow["strength"] = min(1.0, abs(cvd_5m) / 50)

            # 고래
            whale_str = await self.redis.get("flow:combined:whale_bias")
            if whale_str:
                flow["whale_bias"] = float(whale_str)

            # 청산
            liq_total = float(await self.redis.get("flow:liq:1m_total") or 0)
            flow["liq_active"] = liq_total > 100_000

        except Exception as e:
            logger.debug(f"flow data error: {e}")

        return flow
