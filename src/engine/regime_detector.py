"""
MarketRegimeDetector — 마켓 레짐(국면) 감지
4가지 레짐: TRENDING_UP / TRENDING_DOWN / RANGING / VOLATILE

판별 기준:
┌─────────────┬──────────┬──────────┬──────────┬──────────┐
│             │ ADX      │ 추세방향  │ BB Width │ ATR%     │
├─────────────┼──────────┼──────────┼──────────┼──────────┤
│ TRENDING_UP │ > 25     │ +DI > -DI│ 확장     │ 보통     │
│ TRENDING_DN │ > 25     │ -DI > +DI│ 확장     │ 보통     │
│ RANGING     │ < 20     │ 약함     │ 수축     │ 낮음     │
│ VOLATILE    │ any      │ 불안정    │ 급확장   │ > 0.5%   │
└─────────────┴──────────┴──────────┴──────────┴──────────┘

추가 확인:
- EMA20/50/200 배열 (정배열/역배열/혼합)
- 캔들 바디 대비 꼬리 비율 (횡보에선 꼬리 길음)
- 최근 20봉 고가-저가 레인지
"""
import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)

# 레짐 상수
REGIME_TRENDING_UP = "trending_up"
REGIME_TRENDING_DOWN = "trending_down"
REGIME_RANGING = "ranging"
REGIME_VOLATILE = "volatile"

# 레짐별 추천 전략
REGIME_STRATEGY = {
    REGIME_TRENDING_UP: {
        "description": "상승 추세 — 눌림목 매수, 돌파 매수 유리",
        "prefer": "long",
        "avoid": "counter_trend_short",
        "leverage_mult": 1.0,
    },
    REGIME_TRENDING_DOWN: {
        "description": "하락 추세 — 반등 매도, 돌파 매도 유리",
        "prefer": "short",
        "avoid": "counter_trend_long",
        "leverage_mult": 1.0,
    },
    REGIME_RANGING: {
        "description": "횡보 — 지지/저항 반전매매 유리, 돌파 매매 위험",
        "prefer": "reversal",
        "avoid": "breakout",
        "leverage_mult": 0.7,
    },
    REGIME_VOLATILE: {
        "description": "고변동 — 리스크 축소, 진입 자제 권장",
        "prefer": "none",
        "avoid": "all",
        "leverage_mult": 0.5,
    },
}


class MarketRegimeDetector:
    """마켓 레짐 감지기"""

    def __init__(self):
        self._last_regime = REGIME_RANGING
        self._regime_history = []  # 최근 레짐 기록
        self._max_history = 50

    def detect(self, df: pd.DataFrame) -> dict:
        """
        캔들 DataFrame으로 현재 마켓 레짐 판별.

        Args:
            df: OHLCV DataFrame (최소 50봉 이상)

        Returns:
            {
                "regime": str,
                "confidence": float (0~1),
                "adx": float,
                "plus_di": float,
                "minus_di": float,
                "bb_width": float,
                "atr_pct": float,
                "ema_alignment": str,
                "strategy": dict,
                "scores": dict,  # 레짐별 점수
            }
        """
        if len(df) < 50:
            return self._default_result()

        high = df["high"].astype(float).values
        low = df["low"].astype(float).values
        close = df["close"].astype(float).values

        # ── 1. ADX + DI 계산 ──
        adx, plus_di, minus_di = self._calc_adx(high, low, close, period=14)

        # ── 2. Bollinger Band Width ──
        bb_width, bb_width_pctile = self._calc_bb_width(close, period=20)

        # ── 3. ATR % ──
        atr_pct = self._calc_atr_pct(high, low, close, period=14)

        # ── 4. EMA 배열 ──
        ema20 = self._ema(close, 20)
        ema50 = self._ema(close, 50)
        ema_alignment = self._check_ema_alignment(close, ema20, ema50)

        # ── 5. 캔들 특성 ──
        body_ratio = self._calc_body_ratio(df)
        range_pct = self._calc_range_pct(high, low, close)

        # ── 레짐 점수 계산 ──
        scores = self._score_regimes(
            adx, plus_di, minus_di, bb_width, bb_width_pctile,
            atr_pct, ema_alignment, body_ratio, range_pct
        )

        # 최고 점수 레짐 선택
        regime = max(scores, key=scores.get)
        confidence = scores[regime] / max(sum(scores.values()), 1)

        # 레짐 안정화 (급변 방지 — 2회 연속 같아야 전환)
        # 04-13: raw(감지된) 레짐을 history에 기록 (기존: stabilized 기록 → 전환 불가 버그)
        raw_regime = regime
        if regime != self._last_regime:
            if len(self._regime_history) >= 2 and self._regime_history[-1] == regime:
                self._last_regime = regime
            else:
                regime = self._last_regime

        self._regime_history.append(raw_regime)  # raw 값 기록 (안정화 전)
        if len(self._regime_history) > self._max_history:
            self._regime_history.pop(0)

        strategy = REGIME_STRATEGY[regime]

        return {
            "regime": regime,
            "confidence": round(confidence, 3),
            "adx": round(adx, 2),
            "plus_di": round(plus_di, 2),
            "minus_di": round(minus_di, 2),
            "bb_width": round(bb_width, 4),
            "bb_width_pctile": round(bb_width_pctile, 1),
            "atr_pct": round(atr_pct, 4),
            "ema_alignment": ema_alignment,
            "body_ratio": round(body_ratio, 3),
            "range_pct": round(range_pct, 4),
            "strategy": strategy,
            "scores": {k: round(v, 2) for k, v in scores.items()},
        }

    def _score_regimes(self, adx, plus_di, minus_di, bb_width, bb_pctile,
                       atr_pct, ema_align, body_ratio, range_pct) -> dict:
        """각 레짐별 점수 산출"""
        scores = {
            REGIME_TRENDING_UP: 0.0,
            REGIME_TRENDING_DOWN: 0.0,
            REGIME_RANGING: 0.0,
            REGIME_VOLATILE: 0.0,
        }

        # ── ADX 기반 ──
        if adx > 30:
            # 강한 추세
            if plus_di > minus_di:
                scores[REGIME_TRENDING_UP] += 4.0
            else:
                scores[REGIME_TRENDING_DOWN] += 4.0
        elif adx > 25:
            if plus_di > minus_di:
                scores[REGIME_TRENDING_UP] += 3.0
            else:
                scores[REGIME_TRENDING_DOWN] += 3.0
        elif adx < 15:
            scores[REGIME_RANGING] += 4.0  # 04-13: 순서 교정 (M10: dead code)
        elif adx < 20:
            scores[REGIME_RANGING] += 3.0

        # ── BB Width 기반 ──
        if bb_pctile > 80:
            # BB 확장 → 추세 or 고변동
            scores[REGIME_VOLATILE] += 2.0
            if plus_di > minus_di:
                scores[REGIME_TRENDING_UP] += 1.0
            else:
                scores[REGIME_TRENDING_DOWN] += 1.0
        elif bb_pctile < 20:
            # BB 수축 → 횡보 (스퀴즈)
            scores[REGIME_RANGING] += 3.0

        # ── ATR% 기반 ──
        if atr_pct > 0.6:
            scores[REGIME_VOLATILE] += 4.0
        elif atr_pct > 0.4:
            scores[REGIME_VOLATILE] += 2.0
        elif atr_pct < 0.15:
            scores[REGIME_RANGING] += 2.0

        # ── EMA 배열 기반 ──
        if ema_align == "bullish":
            scores[REGIME_TRENDING_UP] += 2.0
        elif ema_align == "bearish":
            scores[REGIME_TRENDING_DOWN] += 2.0
        elif ema_align == "mixed":
            scores[REGIME_RANGING] += 1.5

        # ── 캔들 바디 비율 ──
        if body_ratio < 0.3:
            # 꼬리가 긴 캔들 → 횡보/불확실
            scores[REGIME_RANGING] += 1.5
        elif body_ratio > 0.7:
            # 바디가 큰 캔들 → 강한 추세
            if plus_di > minus_di:
                scores[REGIME_TRENDING_UP] += 1.0
            else:
                scores[REGIME_TRENDING_DOWN] += 1.0

        # ── 가격 레인지 ──
        if range_pct < 1.0:
            scores[REGIME_RANGING] += 1.0
        elif range_pct > 3.0:
            scores[REGIME_VOLATILE] += 1.5

        return scores

    # ── 지표 계산 ──

    def _calc_adx(self, high, low, close, period=14):
        """ADX + Directional Indicators"""
        n = len(high)
        if n < period + 1:
            return 15.0, 0.0, 0.0

        # True Range
        tr = np.zeros(n)
        plus_dm = np.zeros(n)
        minus_dm = np.zeros(n)

        for i in range(1, n):
            tr[i] = max(high[i] - low[i],
                        abs(high[i] - close[i - 1]),
                        abs(low[i] - close[i - 1]))
            up_move = high[i] - high[i - 1]
            down_move = low[i - 1] - low[i]

            plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0
            minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0

        # Smoothed averages (Wilder's smoothing)
        atr = np.zeros(n)
        smooth_plus = np.zeros(n)
        smooth_minus = np.zeros(n)

        atr[period] = np.mean(tr[1:period + 1])
        smooth_plus[period] = np.mean(plus_dm[1:period + 1])
        smooth_minus[period] = np.mean(minus_dm[1:period + 1])

        for i in range(period + 1, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
            smooth_plus[i] = (smooth_plus[i - 1] * (period - 1) + plus_dm[i]) / period
            smooth_minus[i] = (smooth_minus[i - 1] * (period - 1) + minus_dm[i]) / period

        # DI
        plus_di = (smooth_plus[-1] / atr[-1] * 100) if atr[-1] > 0 else 0
        minus_di = (smooth_minus[-1] / atr[-1] * 100) if atr[-1] > 0 else 0

        # DX → ADX
        dx_values = []
        for i in range(period, n):
            if atr[i] > 0:
                pdi = smooth_plus[i] / atr[i] * 100
                mdi = smooth_minus[i] / atr[i] * 100
                if (pdi + mdi) > 0:
                    dx_values.append(abs(pdi - mdi) / (pdi + mdi) * 100)

        adx = np.mean(dx_values[-period:]) if len(dx_values) >= period else 15.0

        return float(adx), float(plus_di), float(minus_di)

    def _calc_bb_width(self, close, period=20):
        """Bollinger Band Width + 백분위"""
        if len(close) < period:
            return 0.02, 50.0

        sma = pd.Series(close).rolling(period).mean()
        std = pd.Series(close).rolling(period).std()
        upper = sma + 2 * std
        lower = sma - 2 * std

        width = ((upper - lower) / sma).dropna()
        if len(width) == 0:
            return 0.02, 50.0

        current_width = float(width.iloc[-1])

        # 최근 100봉 대비 백분위
        lookback = min(100, len(width))
        recent = width.iloc[-lookback:]
        percentile = float((recent < current_width).sum() / len(recent) * 100)

        return current_width, percentile

    def _calc_atr_pct(self, high, low, close, period=14):
        """ATR as percentage of price"""
        if len(high) < period + 1:
            return 0.3

        tr = np.maximum(
            high[1:] - low[1:],
            np.maximum(
                np.abs(high[1:] - close[:-1]),
                np.abs(low[1:] - close[:-1])
            )
        )
        atr = np.mean(tr[-period:])
        return float(atr / close[-1] * 100) if close[-1] > 0 else 0.3

    def _ema(self, data, period):
        """Exponential Moving Average"""
        ema = np.zeros(len(data))
        ema[0] = data[0]
        mult = 2 / (period + 1)
        for i in range(1, len(data)):
            ema[i] = data[i] * mult + ema[i - 1] * (1 - mult)
        return ema

    def _check_ema_alignment(self, close, ema20, ema50):
        """EMA 배열 확인"""
        c = close[-1]
        e20 = ema20[-1]
        e50 = ema50[-1]

        if c > e20 > e50:
            return "bullish"    # 정배열
        elif c < e20 < e50:
            return "bearish"    # 역배열
        else:
            return "mixed"      # 혼합

    def _calc_body_ratio(self, df, lookback=20):
        """최근 N봉의 평균 바디/전체 비율 (바디 크면 추세)"""
        recent = df.tail(lookback)
        body = (recent["close"] - recent["open"]).abs()
        total = recent["high"] - recent["low"]
        total = total.replace(0, np.nan)
        ratio = (body / total).dropna()
        return float(ratio.mean()) if len(ratio) > 0 else 0.5

    def _calc_range_pct(self, high, low, close, lookback=20):
        """최근 N봉의 가격 레인지 (%)"""
        recent_high = np.max(high[-lookback:])
        recent_low = np.min(low[-lookback:])
        mid = (recent_high + recent_low) / 2
        return float((recent_high - recent_low) / mid * 100) if mid > 0 else 1.0

    def _default_result(self):
        return {
            "regime": REGIME_RANGING,
            "confidence": 0.0,
            "adx": 0, "plus_di": 0, "minus_di": 0,
            "bb_width": 0, "bb_width_pctile": 50,
            "atr_pct": 0.3, "ema_alignment": "mixed",
            "body_ratio": 0.5, "range_pct": 1.0,
            "strategy": REGIME_STRATEGY[REGIME_RANGING],
            "scores": {},
        }

    def get_regime_history(self) -> dict:
        """최근 레짐 변화 통계"""
        if not self._regime_history:
            return {"current": REGIME_RANGING, "distribution": {}}

        total = len(self._regime_history)
        dist = {}
        for r in [REGIME_TRENDING_UP, REGIME_TRENDING_DOWN, REGIME_RANGING, REGIME_VOLATILE]:
            count = self._regime_history.count(r)
            dist[r] = round(count / total * 100, 1)

        return {
            "current": self._regime_history[-1],
            "distribution": dist,
            "history_length": total,
        }
