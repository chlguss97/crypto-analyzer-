"""
Scalp Engine v4 — 스캘핑 전문 엔진 (전면 개편)
횡보 중 급변동 포착 → 빠른 진입/탈출

시그널 18종 + 필터 2종:
  [기본]    1.EMA크로스  2.RSI반전  3.BB돌파  4.거래량스파이크  5.모멘텀
  [급변동]  6.변동성폭발  7.레인지브레이크아웃  8.캔들패턴  9.급속모멘텀
  [SMC]    10.오더블록  11.유동성스윕  12.FVG
  [강화]   13.VWAP  14.피봇  15.BOS
  [필터]   16.세션  17.안티첩
  [관리]   18.트레일링스탑
"""
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from src.engine.base import BaseIndicator


class ScalpEngine:
    """스캘핑 전문 엔진 v4"""

    def __init__(self):
        self.name = "scalp"
        self.default_leverage = 15  # 25→15: 과도한 레버리지 축소 (04-15)
        self.sl_atr_mult = 2.0    # 1.5→2.0: SL 거리 확대 (노이즈 즉사 방지) (04-15)
        self.tp_rr = 3.0          # 2.5→3.0: RR 개선 (04-15)

    async def analyze(self, candles_1m: pd.DataFrame, candles_5m: pd.DataFrame,
                      candles_15m: pd.DataFrame = None,
                      rt_velocity: dict = None) -> dict:
        signals = {}
        score_long = 0.0
        score_short = 0.0

        # ── 5m EMA 추세 판별 (평균회귀 시그널 필터용) ──
        _ema50_5m = float(candles_5m["close"].ewm(span=50, adjust=False).mean().iloc[-1])
        _ema200_5m = float(candles_5m["close"].ewm(span=200, adjust=False).mean().iloc[-1])
        _price_now = float(candles_5m["close"].iloc[-1])
        _macro_trend = "up" if (_price_now > _ema50_5m and _ema50_5m > _ema200_5m) else \
                       "down" if (_price_now < _ema50_5m and _ema50_5m < _ema200_5m) else "neutral"

        # ═══════════════════════════════════════════════════════════════
        # 04-15 전면 개편: 추세추종 중심 (기존 73% 방향 틀림 = MR 과다)
        # 원칙: 매크로 추세 방향으로만 진입, 역추세 시그널 최소화
        # ═══════════════════════════════════════════════════════════════

        # ── A. 추세추종 시그널 (핵심, 높은 가중치) ──
        ema_sig = self._ema_cross(candles_5m)
        signals["ema_cross"] = ema_sig
        score_long += ema_sig["strength"] * 5.0 if ema_sig["direction"] == "long" else 0   # 2.5→5.0
        score_short += ema_sig["strength"] * 5.0 if ema_sig["direction"] == "short" else 0

        mom_sig = self._momentum(candles_1m)
        signals["momentum"] = mom_sig
        score_long += mom_sig["strength"] * 4.0 if mom_sig["direction"] == "long" else 0   # 1.5→4.0
        score_short += mom_sig["strength"] * 4.0 if mom_sig["direction"] == "short" else 0

        bos_sig = self._break_of_structure(candles_1m)
        signals["bos"] = bos_sig
        score_long += bos_sig["strength"] * 5.0 if bos_sig["direction"] == "long" else 0   # 3.5→5.0
        score_short += bos_sig["strength"] * 5.0 if bos_sig["direction"] == "short" else 0

        range_brk = self._range_breakout(candles_5m, candles_1m)
        signals["range_breakout"] = range_brk
        score_long += range_brk["strength"] * 5.0 if range_brk["direction"] == "long" else 0  # 3.5→5.0
        score_short += range_brk["strength"] * 5.0 if range_brk["direction"] == "short" else 0

        rapid = self._rapid_momentum(candles_1m)
        signals["rapid_momentum"] = rapid
        score_long += rapid["strength"] * 4.0 if rapid["direction"] == "long" else 0   # 2.5→4.0
        score_short += rapid["strength"] * 4.0 if rapid["direction"] == "short" else 0

        vol_explode = self._volatility_explosion(candles_5m)
        signals["vol_explosion"] = vol_explode
        score_long += vol_explode["strength"] * 4.0 if vol_explode["direction"] == "long" else 0  # 3.5→4.0
        score_short += vol_explode["strength"] * 4.0 if vol_explode["direction"] == "short" else 0

        # ── B. SMC 시그널 (추세 방향 일치 시에만) ──
        ob_sig = self._scalp_order_block_mtf(candles_5m, candles_1m, candles_15m)
        signals["order_block"] = ob_sig
        _ob_w = 6.0  # 멀티TF OB 최고 가중치
        if ob_sig["direction"] == "long" and _macro_trend == "down":
            _ob_w = 0.0
        elif ob_sig["direction"] == "short" and _macro_trend == "up":
            _ob_w = 0.0
        score_long += ob_sig["strength"] * _ob_w if ob_sig["direction"] == "long" else 0
        score_short += ob_sig["strength"] * _ob_w if ob_sig["direction"] == "short" else 0

        liq_sig = self._liquidity_sweep(candles_1m)
        signals["liquidity_sweep"] = liq_sig
        _liq_w = 4.0  # 3.5→4.0 (유동성 스윕은 추세추종)
        if liq_sig["direction"] == "long" and _macro_trend == "down":
            _liq_w = 0.0
        elif liq_sig["direction"] == "short" and _macro_trend == "up":
            _liq_w = 0.0
        score_long += liq_sig["strength"] * _liq_w if liq_sig["direction"] == "long" else 0
        score_short += liq_sig["strength"] * _liq_w if liq_sig["direction"] == "short" else 0

        fvg_sig = self._fvg(candles_1m)
        signals["fvg"] = fvg_sig
        score_long += fvg_sig["strength"] * 3.0 if fvg_sig["direction"] == "long" else 0   # 2.5→3.0
        score_short += fvg_sig["strength"] * 3.0 if fvg_sig["direction"] == "short" else 0

        # ── C. 보조 시그널 (낮은 가중치) ──
        vol_sig = self._volume_spike(candles_5m)
        signals["volume_spike"] = vol_sig
        score_long += vol_sig["strength"] * 2.0 if vol_sig["direction"] == "long" else 0
        score_short += vol_sig["strength"] * 2.0 if vol_sig["direction"] == "short" else 0

        candle_sig = self._candle_pattern(candles_1m)
        signals["candle_pattern"] = candle_sig
        score_long += candle_sig["strength"] * 1.5 if candle_sig["direction"] == "long" else 0  # 2.0→1.5
        score_short += candle_sig["strength"] * 1.5 if candle_sig["direction"] == "short" else 0

        pivot_sig = self._pivot_points(candles_5m)
        signals["pivot_points"] = pivot_sig
        score_long += pivot_sig["strength"] * 2.0 if pivot_sig["direction"] == "long" else 0  # 2.5→2.0
        score_short += pivot_sig["strength"] * 2.0 if pivot_sig["direction"] == "short" else 0

        # VWAP 돌파만 (평균회귀 제거) — VWAP 돌파는 추세추종
        vwap_sig = self._vwap_levels(candles_5m, candles_1m)
        signals["vwap_levels"] = vwap_sig
        score_long += vwap_sig["strength"] * 2.0 if vwap_sig["direction"] == "long" else 0  # 3.0→2.0
        score_short += vwap_sig["strength"] * 2.0 if vwap_sig["direction"] == "short" else 0

        # ── D. 평균회귀 시그널 (최소 가중치, 추세 방향 일치 시에만) ──
        rsi_sig = self._rsi_reversal(candles_1m)
        signals["rsi_reversal"] = rsi_sig
        _rsi_w = 1.0 if _macro_trend == "neutral" else 0.0  # 2.0→1.0, 추세 시 비활성
        score_long += rsi_sig["strength"] * _rsi_w if rsi_sig["direction"] == "long" else 0
        score_short += rsi_sig["strength"] * _rsi_w if rsi_sig["direction"] == "short" else 0

        bb_sig = self._bb_breakout(candles_5m)
        signals["bb_breakout"] = bb_sig
        _bb_w = 1.0 if _macro_trend == "neutral" else 0.0  # 3.0→1.0, 추세 시 비활성
        score_long += bb_sig["strength"] * _bb_w if bb_sig["direction"] == "long" else 0
        score_short += bb_sig["strength"] * _bb_w if bb_sig["direction"] == "short" else 0

        rsi2_sig = self._rsi2_extreme(candles_1m, candles_5m)
        signals["rsi2_extreme"] = rsi2_sig
        score_long += 0  # 4.0→0: 완전 비활성 (73% 방향 틀림의 주범)
        score_short += 0

        vwap_mr_sig = self._vwap_mean_reversion(candles_5m, candles_1m)
        signals["vwap_mean_reversion"] = vwap_mr_sig
        score_long += 0  # 3.5→0: 완전 비활성
        score_short += 0

        liq_cascade_sig = self._liquidation_cascade_fade(candles_1m)
        signals["liq_cascade_fade"] = liq_cascade_sig
        score_long += 0  # 2.5→0: 완전 비활성
        score_short += 0

        # ── E. 실시간 급등락 감지 ──
        spike_sig = self._realtime_spike(rt_velocity)
        signals["realtime_spike"] = spike_sig
        score_long += spike_sig["strength"] * 5.0 if spike_sig["direction"] == "long" else 0
        score_short += spike_sig["strength"] * 5.0 if spike_sig["direction"] == "short" else 0

        # ── 필터 (16~17) ──
        session = self._session_filter(candles_1m)
        signals["session"] = session

        antichop = self._anti_chop(candles_5m)
        signals["anti_chop"] = antichop

        # ── 15m 추세 필터 ──
        trend_filter = "neutral"
        if candles_15m is not None and len(candles_15m) >= 20:
            ema50 = candles_15m["close"].ewm(span=50, adjust=False).mean().iloc[-1]
            trend_filter = "long" if candles_15m["close"].iloc[-1] > ema50 else "short"

        # ── 점수 계산 ──
        # 04-15 개편: 추세추종 가중치 합 ~57, 실측 한 방향 raw 8~15
        max_possible = 25.0
        # 04-13 개선: 0.8→0.6 완화 + 충돌 시에도 dominant 방향 감점 진입
        dominant = max(score_long, score_short)
        minor = min(score_long, score_short)
        if dominant > 0 and minor / dominant > 0.6:
            direction = "long" if score_long >= score_short else "short"
            raw = (dominant - minor) * 0.5  # 순차이의 50%만 반영
        elif score_long > score_short:
            direction = "long"
            raw = score_long
        elif score_short > score_long:
            direction = "short"
            raw = score_short
        else:
            direction = "neutral"
            raw = 0

        # 모드 판단 (필터 적용 전에 먼저)
        is_explosive = (vol_explode["strength"] > 0.5 or range_brk["strength"] > 0.5
                        or rapid["strength"] > 0.5)

        # SMC 진입: OB와 유동성스윕 방향이 현재 시그널 방향과 일치
        smc_ob_match = (ob_sig["strength"] > 0.5 and ob_sig["direction"] == direction)
        smc_liq_match = (liq_sig["strength"] > 0.5 and liq_sig["direction"] == direction)
        smc_entry = smc_ob_match or smc_liq_match

        # ── 필터 가산 방식 (곱셈 폭락 방지) ──
        # 기존: raw × 0.5 × 0.7 × 0.5 = 17.5% (너무 가혹)
        # 개선: 각 필터마다 정해진 비율만 감점
        penalty = 0.0

        # 15m 역방향 (04-10 강화: 역추세 일반 0.30→0.50 — 승률 핵심 필터)
        if trend_filter != "neutral" and trend_filter != direction:
            if smc_entry:
                penalty += 0.15  # SMC 카운터도 약간 감점
            elif is_explosive:
                penalty += 0.20
            else:
                penalty += 0.50  # 역추세 일반 진입 사실상 차단

        # 안티첩 (SMC/급변동 모드면 무시 — 횡보장에서도 이런 시그널 잘 먹음)
        if antichop["is_chop"] and not (smc_entry or is_explosive):
            penalty += 0.20

        # 세션 (배율로 적용)
        session_mult = session["multiplier"]

        # 최종 점수: 1) 페널티 차감 → 2) 세션 배율
        adjusted = raw * (1 - min(0.5, penalty)) * session_mult
        score = min(10.0, adjusted / max_possible * 10)

        explosive_mode = is_explosive and score >= 2.5

        # ATR 계산
        atr = self._calc_atr(candles_5m, 14)
        atr_pct = atr / candles_5m["close"].iloc[-1] * 100 if atr > 0 else 0.2

        # SL/TP 모드별 조정
        if smc_entry:
            sl_mult = 1.0    # 0.5→1.0: SMC도 노이즈 방어 필요
            tp_mult = 2.5
            use_trailing = False
        elif explosive_mode:
            sl_mult = 1.2    # 0.6→1.2: 변동성 폭발 시 흔들림 대비
            tp_mult = 2.0    # 1.5→2.0: RR 개선
            use_trailing = True
        else:
            sl_mult = self.sl_atr_mult  # 1.5
            tp_mult = self.tp_rr        # 2.5
            use_trailing = False

        # 최소 SL 가격 0.35% — 노이즈 즉사 방지 (04-15: 0.15%→0.35%)
        # BTC $75k 기준 $262 — 1분 ATR 노이즈 충분히 방어
        min_sl_dist = candles_5m["close"].iloc[-1] * 0.0035
        sl_dist = max(atr * sl_mult, min_sl_dist)

        return {
            "mode": "scalp",
            "score": round(score, 2),
            "direction": direction,
            "long_score": round(score_long, 2),
            "short_score": round(score_short, 2),
            "trend_filter": trend_filter,
            "signals": signals,
            "atr": round(atr, 2),
            "atr_pct": round(atr_pct, 4),
            "sl_distance": round(sl_dist, 2),
            "tp_distance": round(sl_dist * tp_mult, 2),
            "leverage": self.default_leverage,
            "explosive_mode": explosive_mode,
            "smc_entry": smc_entry,
            "use_trailing": use_trailing,
            "session": session["session"],
            "session_quality": session["quality"],
            "penalty": round(penalty, 2),
        }

    # ══════════════════════════════════════
    # 기본 시그널 (1~5)
    # ══════════════════════════════════════

    def _ema_cross(self, df: pd.DataFrame) -> dict:
        close = df["close"]
        ema5 = close.ewm(span=5, adjust=False).mean()
        ema13 = close.ewm(span=13, adjust=False).mean()
        ema21 = close.ewm(span=21, adjust=False).mean()
        direction, strength = "neutral", 0.0

        for i in range(-3, 0):
            if len(ema5) > abs(i) + 1:
                prev = i - 1
                if ema5.iloc[prev] <= ema13.iloc[prev] and ema5.iloc[i] > ema13.iloc[i]:
                    direction = "long"
                    strength = 0.8 if close.iloc[i] > ema21.iloc[i] else 0.5
                elif ema5.iloc[prev] >= ema13.iloc[prev] and ema5.iloc[i] < ema13.iloc[i]:
                    direction = "short"
                    strength = 0.8 if close.iloc[i] < ema21.iloc[i] else 0.5

        if ema5.iloc[-1] > ema13.iloc[-1] > ema21.iloc[-1] and direction == "long":
            strength = min(1.0, strength + 0.2)
        elif ema5.iloc[-1] < ema13.iloc[-1] < ema21.iloc[-1] and direction == "short":
            strength = min(1.0, strength + 0.2)

        return {"type": "ema_cross", "direction": direction, "strength": round(strength, 2)}

    def _rsi_reversal(self, df: pd.DataFrame) -> dict:
        """RSI 30/70 임계값 (기존 25/75는 너무 엄격)"""
        close = df["close"]
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.inf)
        rsi = 100 - (100 / (1 + rs))
        r = rsi.iloc[-1]
        r_prev = rsi.iloc[-2] if len(rsi) >= 2 else 50
        direction, strength = "neutral", 0.0

        # 30/70 임계값으로 완화
        if r < 30 and r > r_prev:
            direction = "long"
            strength = min(1.0, (35 - r) / 15)
        elif r > 70 and r < r_prev:
            direction = "short"
            strength = min(1.0, (r - 65) / 15)

        return {"type": "rsi_reversal", "direction": direction, "strength": round(strength, 2), "rsi": round(r, 1)}

    def _bb_breakout(self, df: pd.DataFrame) -> dict:
        close = df["close"]
        sma = close.rolling(20).mean()
        std = close.rolling(20).std()
        upper, lower = sma + 2 * std, sma - 2 * std
        width = ((upper - lower) / sma).dropna()
        min_width = width.tail(50).min() if len(width) >= 50 else width.min()
        is_squeeze = width.iloc[-1] <= min_width * 1.2 if min_width > 0 else False
        last = close.iloc[-1]
        direction, strength = "neutral", 0.0

        if is_squeeze:
            if last > upper.iloc[-1]:
                direction, strength = "long", 0.9
            elif last < lower.iloc[-1]:
                direction, strength = "short", 0.9
            else:
                strength = 0.3

        return {"type": "bb_breakout", "direction": direction, "strength": round(strength, 2), "squeeze": is_squeeze}

    def _volume_spike(self, df: pd.DataFrame) -> dict:
        """5m 거래량 스파이크"""
        vol, close = df["volume"], df["close"]
        if len(vol) < 20:
            return {"type": "volume_spike", "direction": "neutral", "strength": 0.0, "ratio": 1.0}
        avg = vol.rolling(20).mean()
        ratio = vol.iloc[-1] / avg.iloc[-1] if avg.iloc[-1] > 0 else 1
        direction, strength = "neutral", 0.0

        if ratio > 1.8:  # 1.8배 이상 (5m 기준)
            direction = "long" if close.iloc[-1] > close.iloc[-2] else "short"
            strength = min(1.0, (ratio - 1) / 4)

        return {"type": "volume_spike", "direction": direction, "strength": round(strength, 2), "ratio": round(ratio, 2)}

    def _momentum(self, df: pd.DataFrame) -> dict:
        close = df["close"]
        if len(close) < 4:
            return {"type": "momentum", "direction": "neutral", "strength": 0.0}
        changes = [close.iloc[-i] - close.iloc[-i-1] for i in range(1, 4)]
        if all(c > 0 for c in changes):
            return {"type": "momentum", "direction": "long",
                    "strength": min(1.0, np.mean(changes) / close.iloc[-1] * 100 / 0.1)}
        elif all(c < 0 for c in changes):
            return {"type": "momentum", "direction": "short",
                    "strength": min(1.0, abs(np.mean(changes)) / close.iloc[-1] * 100 / 0.1)}
        return {"type": "momentum", "direction": "neutral", "strength": 0.0}

    # ══════════════════════════════════════
    # 급변동 시그널 (6~9)
    # ══════════════════════════════════════

    def _volatility_explosion(self, df: pd.DataFrame) -> dict:
        """ATR 급등 + BB 확장 + 횡보 후 폭발"""
        close, high, low = df["close"].values, df["high"].values, df["low"].values
        if len(close) < 30:
            return {"type": "vol_explosion", "direction": "neutral", "strength": 0.0}

        tr = np.maximum(high[1:] - low[1:], np.maximum(
            np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])))
        recent_atr = np.mean(tr[-3:])
        avg_atr = np.mean(tr[-20:])
        atr_ratio = recent_atr / avg_atr if avg_atr > 0 else 1.0

        prev_range = np.max(high[-13:-3]) - np.min(low[-13:-3])
        recent_range = np.max(high[-3:]) - np.min(low[-3:])
        range_ratio = recent_range / prev_range if prev_range > 0 else 1.0

        direction, strength = "neutral", 0.0
        if atr_ratio >= 2.0 or range_ratio >= 0.5:
            # 방향: 최근 5봉의 누적 변화로 판단 (3봉보다 안정적)
            cum_change = close[-1] - close[-5] if len(close) >= 5 else close[-1] - close[-3]
            if abs(cum_change) > 0:
                direction = "long" if cum_change > 0 else "short"
                strength = min(1.0, max(0, (atr_ratio - 1.0) / 3.0 + (range_ratio - 0.3) / 2.0))

        return {"type": "vol_explosion", "direction": direction, "strength": round(strength, 2),
                "atr_ratio": round(atr_ratio, 2)}

    def _range_breakout(self, df_5m: pd.DataFrame, df_1m: pd.DataFrame) -> dict:
        """20봉 레인지 돌파 + 거래량 + 페이크아웃 필터"""
        if len(df_5m) < 25:
            return {"type": "range_breakout", "direction": "neutral", "strength": 0.0, "breakout": "none"}

        lookback = df_5m.iloc[-21:-1]
        range_high = float(lookback["high"].max())
        range_low = float(lookback["low"].min())
        range_size = range_high - range_low
        current_close = float(df_5m["close"].iloc[-1])

        range_pct = range_size / current_close * 100
        if range_pct > 2.0 or range_pct < 0.1:
            return {"type": "range_breakout", "direction": "neutral", "strength": 0.0, "breakout": "none"}

        direction, strength, breakout = "neutral", 0.0, "none"

        if current_close > range_high:
            overshoot = (current_close - range_high) / range_size
            if overshoot > 0.1:
                direction, breakout = "long", "upper"
                strength = min(1.0, overshoot * 2)
                if len(df_1m) >= 20:
                    vol_ratio = float(df_1m["volume"].iloc[-1]) / float(df_1m["volume"].tail(20).mean())
                    if vol_ratio > 1.5:
                        strength = min(1.0, strength + 0.2)

        elif current_close < range_low:
            overshoot = (range_low - current_close) / range_size
            if overshoot > 0.1:
                direction, breakout = "short", "lower"
                strength = min(1.0, overshoot * 2)
                if len(df_1m) >= 20:
                    vol_ratio = float(df_1m["volume"].iloc[-1]) / float(df_1m["volume"].tail(20).mean())
                    if vol_ratio > 1.5:
                        strength = min(1.0, strength + 0.2)

        return {"type": "range_breakout", "direction": direction, "strength": round(strength, 2), "breakout": breakout}

    def _candle_pattern(self, df: pd.DataFrame) -> dict:
        """장대봉, 핀바, 갭 — 핀바를 우선 체크 (반전이 더 강함)"""
        if len(df) < 10:
            return {"type": "candle_pattern", "direction": "neutral", "strength": 0.0, "pattern": "none"}

        o, h, l, c = float(df["open"].iloc[-1]), float(df["high"].iloc[-1]), float(df["low"].iloc[-1]), float(df["close"].iloc[-1])
        prev_c = float(df["close"].iloc[-2])
        body = abs(c - o)
        full_range = max(h - l, 0.0001)
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        avg_body = float((df["close"].tail(10) - df["open"].tail(10)).abs().mean())

        direction, strength, pattern = "neutral", 0.0, "none"

        # 1) 핀바 우선 (반전 시그널이 가장 강함)
        if lower_wick > body * 2 and lower_wick > full_range * 0.6:
            direction, pattern = "long", "pin_bar_bull"
            strength = min(0.85, lower_wick / full_range)
        elif upper_wick > body * 2 and upper_wick > full_range * 0.6:
            direction, pattern = "short", "pin_bar_bear"
            strength = min(0.85, upper_wick / full_range)

        # 2) 장대봉
        elif avg_body > 0 and body > avg_body * 2:
            direction = "long" if c > o else "short"
            pattern = "big_bull" if c > o else "big_bear"
            strength = min(0.8, body / avg_body / 4)

        # 3) 갭
        elif prev_c > 0 and abs(o - prev_c) / prev_c * 100 > 0.1:
            direction = "long" if o > prev_c else "short"
            pattern = "gap_up" if o > prev_c else "gap_down"
            strength = min(0.7, abs(o - prev_c) / prev_c * 100 / 0.3)

        return {"type": "candle_pattern", "direction": direction, "strength": round(strength, 2), "pattern": pattern}

    def _rapid_momentum(self, df: pd.DataFrame) -> dict:
        """1분 내 큰 가격 움직임 — high/low 기반 실변동폭 감지"""
        if len(df) < 25:
            return {"type": "rapid_momentum", "direction": "neutral", "strength": 0.0}

        close = df["close"].values
        high = df["high"].values
        low = df["low"].values

        # close-to-close 변화 (기존)
        changes = np.diff(close)

        # high/low 기반 실변동폭 (intra-candle 큰 움직임 포착)
        # 최근 3봉의 최고-최저 vs 이전 20봉 평균 범위
        recent_range = max(high[-3:]) - min(low[-3:])
        avg_range = np.mean(high[-20:] - low[-20:])
        range_ratio = recent_range / avg_range if avg_range > 0 else 1.0

        # 단일 봉 스파이크: 최근 1봉의 high-low가 평균의 3배 이상
        last_bar_range = high[-1] - low[-1]
        bar_spike = last_bar_range / avg_range if avg_range > 0 else 1.0

        last_move = abs(changes[-1])
        avg_move = np.mean(np.abs(changes[-20:]))
        move_ratio = last_move / avg_move if avg_move > 0 else 1.0

        recent_3 = changes[-3:]
        all_up = all(c > 0 for c in recent_3)
        all_down = all(c < 0 for c in recent_3)
        total_pct = abs(sum(recent_3)) / close[-4] * 100 if close[-4] > 0 else 0

        # 실변동폭 %: 최근 3봉 범위 / 가격
        range_pct = recent_range / close[-1] * 100 if close[-1] > 0 else 0

        direction, strength = "neutral", 0.0

        # 기존 조건 + 새 조건: 범위 급등 또는 단일봉 스파이크
        if (move_ratio >= 3.0 or (total_pct >= 0.15 and (all_up or all_down))
                or range_ratio >= 3.0 or bar_spike >= 3.0 or range_pct >= 0.5):
            # 방향: close 변화 기반 (범위만 크고 방향 불명이면 close 기준)
            if all_up or changes[-1] > 0:
                direction = "long"
            elif all_down or changes[-1] < 0:
                direction = "short"
            else:
                direction = "long" if close[-1] > close[-3] else "short"
            strength = min(1.0, max(move_ratio / 5.0, total_pct / 0.3,
                                    range_ratio / 5.0, range_pct / 1.0))

        return {"type": "rapid_momentum", "direction": direction, "strength": round(strength, 2),
                "move_ratio": round(move_ratio, 2), "total_pct": round(total_pct, 4),
                "range_pct": round(range_pct, 4), "bar_spike": round(bar_spike, 2)}

    # ══════════════════════════════════════
    # SMC 시그널 (10~12)
    # ══════════════════════════════════════

    def _scalp_order_block(self, df_5m: pd.DataFrame, df_1m: pd.DataFrame) -> dict:
        """Legacy — v3로 대체됨"""
        return {"type": "order_block", "direction": "neutral", "strength": 0.0, "zone": None}

    # ══════════════════════════════════════
    # 오더블록 v3 — 전문가급 멀티TF
    # ══════════════════════════════════════

    def _find_swing_points(self, high, low, order=3):
        """
        스윙 고/저점 탐지.
        swing high: 양쪽 order 봉보다 높은 고점
        swing low: 양쪽 order 봉보다 낮은 저점
        """
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
        """
        MSB(구조 돌파) 기반 오더블록 탐지 (단일 TF).

        로직:
        1. 스윙 고/저점 추적 → 구조(HH/HL/LH/LL) 파악
        2. 종가가 직전 스윙 고점 돌파 = Bullish MSB
           종가가 직전 스윙 저점 돌파 = Bearish MSB
        3. MSB 임펄스 직전 반대색 캔들 = OB
        4. 임펄스 품질 검증 (바디비율 + 거래량)
        5. 프레시 체크 (OB 존 재방문 여부)
        6. FVG 동반 체크
        """
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

        # 1. 스윙 포인트 탐지
        swings = self._find_swing_points(high, low, order=swing_order)
        if len(swings) < 2:
            return []

        swing_highs = [(i, p) for t, i, p in swings if t == "sh"]
        swing_lows = [(i, p) for t, i, p in swings if t == "sl"]

        obs = []
        start = max(swing_order + 1, n - lookback)

        # 2. MSB 감지: 각 봉에서 직전 스윙 고/저점 돌파 확인
        for bar in range(start, n):
            # 직전 스윙 고점 (이 봉 이전의 가장 최근)
            prev_shs = [(i, p) for i, p in swing_highs if i < bar - 1]
            prev_sls = [(i, p) for i, p in swing_lows if i < bar - 1]

            # ── Bullish MSB: 직전 스윙 고점 돌파 ──
            if prev_shs:
                last_sh_idx, last_sh_price = prev_shs[-1]
                # 이번 봉에서 처음 돌파 (이전 봉은 미돌파)
                if close[bar] > last_sh_price and close[bar - 1] <= last_sh_price:
                    ob = self._extract_ob_candle(
                        open_, close, high, low, volume, vol_avg, n,
                        msb_bar=bar, msb_type="bullish"
                    )
                    if ob:
                        obs.append(ob)

            # ── Bearish MSB: 직전 스윙 저점 돌파 ──
            if prev_sls:
                last_sl_idx, last_sl_price = prev_sls[-1]
                if close[bar] < last_sl_price and close[bar - 1] >= last_sl_price:
                    ob = self._extract_ob_candle(
                        open_, close, high, low, volume, vol_avg, n,
                        msb_bar=bar, msb_type="bearish"
                    )
                    if ob:
                        obs.append(ob)

        # 3. 프레시 체크 + FVG 체크
        for ob in obs:
            idx = ob["bar_idx"]

            # 프레시: OB 존에 가격이 다시 진입하지 않았는지 (현재봉 제외)
            ob["fresh"] = True
            for j in range(idx + 1, n - 1):
                if ob["dir"] == "long" and low[j] <= ob["high"]:
                    ob["fresh"] = False
                    break
                elif ob["dir"] == "short" and high[j] >= ob["low"]:
                    ob["fresh"] = False
                    break

            # FVG: OB 직후 임펄스에서 캔들 간 갭 발생 여부
            ob["has_fvg"] = False
            for j in range(idx, min(idx + 5, n - 2)):
                if ob["dir"] == "long" and high[j] < low[j + 2]:
                    ob["has_fvg"] = True
                    break
                elif ob["dir"] == "short" and low[j] > high[j + 2]:
                    ob["has_fvg"] = True
                    break

        return obs

    def _extract_ob_candle(self, open_, close, high, low, volume, vol_avg, n,
                           msb_bar, msb_type):
        """
        MSB 직전의 OB 캔들 추출 + 임펄스 품질 검증.
        - Bullish MSB → 직전 음봉 = Bullish OB
        - Bearish MSB → 직전 양봉 = Bearish OB
        - 임펄스 품질: 바디/범위 비율 0.5+ & 거래량 1.2x+ 평균
        """
        for i in range(msb_bar - 1, max(msb_bar - 8, -1), -1):
            if i < 0:
                break

            body = abs(close[i] - open_[i])
            total = high[i] - low[i]
            if total <= 0:
                continue

            # Bullish OB: 음봉 (close < open) before bullish MSB
            if msb_type == "bullish" and close[i] < open_[i]:
                iq = self._impulse_quality(open_, close, high, low, volume, vol_avg, i + 1, msb_bar)
                if iq["quality"] < 0.35:
                    continue  # 임펄스가 약하면 유효 OB 아님
                return {
                    "dir": "long",
                    "bar_idx": i,
                    "low": float(low[i]),
                    "high": float(high[i]),
                    "age": n - 1 - i,
                    "impulse": iq,
                }

            # Bearish OB: 양봉 (close > open) before bearish MSB
            elif msb_type == "bearish" and close[i] > open_[i]:
                iq = self._impulse_quality(open_, close, high, low, volume, vol_avg, i + 1, msb_bar)
                if iq["quality"] < 0.35:
                    continue
                return {
                    "dir": "short",
                    "bar_idx": i,
                    "low": float(low[i]),
                    "high": float(high[i]),
                    "age": n - 1 - i,
                    "impulse": iq,
                }

        return None

    def _impulse_quality(self, open_, close, high, low, volume, vol_avg,
                         start, end):
        """
        임펄스(MSB를 일으킨 이동) 품질 측정.
        - body_ratio: 바디/전체범위 (0.6+ = 강한 방향성, 위크 적음)
        - vol_spike: 평균 대비 거래량 (1.5x+ = 기관 참여)
        - consecutive: 같은 방향 연속봉 수
        """
        if start >= end or start < 0:
            return {"quality": 0, "body_ratio": 0, "vol_spike": 0, "bars": 0}

        end = min(end + 1, len(close))
        bodies = 0.0
        ranges = 0.0
        vol_sum = 0.0
        direction_count = 0
        bar_count = 0

        for j in range(start, end):
            b = abs(close[j] - open_[j])
            r = high[j] - low[j]
            bodies += b
            ranges += max(r, 1e-10)
            vol_sum += volume[j]
            bar_count += 1
            if close[j] > open_[j]:
                direction_count += 1
            elif close[j] < open_[j]:
                direction_count -= 1

        body_ratio = bodies / ranges if ranges > 0 else 0
        vol_spike = (vol_sum / bar_count) / vol_avg if vol_avg > 0 and bar_count > 0 else 1.0
        consistency = abs(direction_count) / max(bar_count, 1)

        # 종합 품질: 바디비율(40%) + 거래량(30%) + 방향일관성(30%)
        quality = (min(1.0, body_ratio / 0.7) * 0.4 +
                   min(1.0, vol_spike / 2.0) * 0.3 +
                   consistency * 0.3)

        return {
            "quality": round(quality, 3),
            "body_ratio": round(body_ratio, 3),
            "vol_spike": round(vol_spike, 2),
            "bars": bar_count,
        }

    def _check_choch_1m(self, df_1m, direction):
        """
        1m ChoCH(Change of Character) 확인 — 하위TF 구조전환.
        상위 OB 존에 가격 도달 후, 1m에서 구조가 전환됐는지 확인.
        - Bullish: 1m에서 최근 스윙 고점 돌파 (하락→상승 전환)
        - Bearish: 1m에서 최근 스윙 저점 돌파 (상승→하락 전환)
        """
        if len(df_1m) < 15:
            return False

        high = df_1m["high"].values.astype(float)
        low = df_1m["low"].values.astype(float)
        close = df_1m["close"].values.astype(float)

        # 1m 스윙 포인트 (order=2, 더 민감하게)
        swings = self._find_swing_points(high, low, order=2)
        if len(swings) < 2:
            return False

        if direction == "long":
            # Bullish ChoCH: 현재 종가가 최근 1m 스윙 고점 돌파
            recent_shs = [(i, p) for t, i, p in swings if t == "sh" and i < len(close) - 1]
            if recent_shs:
                last_sh_price = recent_shs[-1][1]
                if close[-1] > last_sh_price:
                    return True
        elif direction == "short":
            # Bearish ChoCH: 현재 종가가 최근 1m 스윙 저점 돌파
            recent_sls = [(i, p) for t, i, p in swings if t == "sl" and i < len(close) - 1]
            if recent_sls:
                last_sl_price = recent_sls[-1][1]
                if close[-1] < last_sl_price:
                    return True

        return False

    def _check_liquidity_clear(self, high, low, n, ob, direction):
        """
        유동성 확인 — OB 와 현재 가격 사이에 스윕 안 된 유동성이 있는지.
        있으면 OB가 관통될 위험 (세력이 스탑로스 먼저 털고 진입).
        Returns True = 유동성 클리어 (안전), False = 위험
        """
        swings = self._find_swing_points(high, low, order=2)

        if direction == "long":
            # Bullish OB 아래에 스윕 안 된 스윙 저점이 있는지
            for t, idx, price in swings:
                if t != "sl":
                    continue
                # OB 존 근처에 있는 스윙 저점
                if price < ob["high"] and price > ob["low"] * 0.998:
                    # 이 저점이 이후에 스윕됐는지
                    swept = any(low[j] < price for j in range(idx + 1, n))
                    if not swept:
                        return False  # 스윕 안 된 유동성 = 위험
        else:
            for t, idx, price in swings:
                if t != "sh":
                    continue
                if price > ob["low"] and price < ob["high"] * 1.002:
                    swept = any(high[j] > price for j in range(idx + 1, n))
                    if not swept:
                        return False

        return True

    def _scalp_order_block_mtf(self, df_5m: pd.DataFrame, df_1m: pd.DataFrame,
                                df_15m: pd.DataFrame = None) -> dict:
        """
        오더블록 v3 — 전문가급 멀티TF 구현

        참고자료 4대 조건 반영:
        1. MSB(구조 돌파) 확인된 OB만 사용
        2. 멀티TF 중첩 (15m + 5m OB 겹침 = 강력)
        3. 프레시 + FVG 동반 = 고확률
        4. 하위TF(1m) ChoCH 확인 후 진입

        추가:
        - 유동성 맵: OB 근처 스윕 안 된 유동성 체크
        - 임펄스 품질: 바디비율 + 거래량 + 방향일관성
        - 오래된 OB 감점
        """
        result = {"type": "order_block", "direction": "neutral", "strength": 0.0,
                  "zone": None, "fresh": False, "choch": False,
                  "tf_overlap": 0, "liq_clear": False}

        if len(df_5m) < 30 or len(df_1m) < 15:
            return result

        price = float(df_1m["close"].iloc[-1])

        # ── 1. 멀티TF OB 탐지 (MSB 기반) ──
        obs_5m = self._find_msb_order_blocks(df_5m, swing_order=3, lookback=50)
        obs_15m = self._find_msb_order_blocks(df_15m, swing_order=3, lookback=30) \
            if df_15m is not None and len(df_15m) >= 20 else []

        if not obs_5m:
            return result

        # ── 2. 가격이 OB 존에 도달한 것만 필터 ──
        best = None
        best_score = -1.0

        for ob in obs_5m:
            # OB 존 진입 확인 (약간의 여유)
            margin = (ob["high"] - ob["low"]) * 0.1
            in_zone = (ob["low"] - margin) <= price <= (ob["high"] + margin)
            if not in_zone:
                continue

            # ── 3. 점수 계산 ──
            score = 0.0

            # 기본: 임펄스 품질 (0~0.3)
            iq = ob.get("impulse", {}).get("quality", 0)
            score += iq * 0.3

            # 프레시 OB (+0.25)
            if ob.get("fresh", False):
                score += 0.25

            # FVG 동반 (+0.2)
            if ob.get("has_fvg", False):
                score += 0.2

            # 임펄스 거래량 스파이크 (+0.1)
            if ob.get("impulse", {}).get("vol_spike", 0) >= 1.5:
                score += 0.1

            # ── 4. 멀티TF 중첩 (+0.3) ──
            tf_overlap = 0
            for ob15 in obs_15m:
                if ob15["dir"] != ob["dir"]:
                    continue
                # 15m OB 와 5m OB 존이 겹치는지
                overlap = min(ob["high"], ob15["high"]) - max(ob["low"], ob15["low"])
                if overlap > 0:
                    tf_overlap += 1
                    score += 0.3
                    break

            # ── 5. ChoCH 1m 확인 (+0.2) ──
            choch = self._check_choch_1m(df_1m, ob["dir"])
            if choch:
                score += 0.2

            # ── 6. 유동성 체크 ──
            high_5m = df_5m["high"].values.astype(float)
            low_5m = df_5m["low"].values.astype(float)
            liq_clear = self._check_liquidity_clear(high_5m, low_5m, len(high_5m), ob, ob["dir"])
            if not liq_clear:
                score *= 0.3  # 유동성 미클리어 = 큰 감점

            # ── 7. 오래된 OB 감점 ──
            age = ob.get("age", 0)
            if age > 30:
                score *= 0.4
            elif age > 20:
                score *= 0.7

            # ── 8. 프레시 아니면 감점 ──
            if not ob.get("fresh", False):
                score *= 0.4

            # ChoCH 없으면 진입 안 함 (핵심 필터)
            if not choch:
                score *= 0.3

            if score > best_score:
                best_score = score
                best = ob
                best["_tf_overlap"] = tf_overlap
                best["_choch"] = choch
                best["_liq_clear"] = liq_clear

        if not best or best_score < 0.15:
            return result

        return {
            "type": "order_block",
            "direction": best["dir"],
            "strength": round(min(1.0, best_score), 3),
            "zone": [best["low"], best["high"]],
            "age": best.get("age", 0),
            "fresh": best.get("fresh", False),
            "has_fvg": best.get("has_fvg", False),
            "choch": best.get("_choch", False),
            "tf_overlap": best.get("_tf_overlap", 0),
            "liq_clear": best.get("_liq_clear", False),
            "impulse_quality": best.get("impulse", {}).get("quality", 0),
            "nearby_count": len(obs_5m),
        }

    def _liquidity_sweep(self, df: pd.DataFrame) -> dict:
        """유동성 스윕 — 스탑헌팅 후 반전"""
        if len(df) < 30:
            return {"type": "liquidity_sweep", "direction": "neutral", "strength": 0.0, "sweep": "none"}

        high = df["high"].values
        low = df["low"].values
        close = df["close"].values
        open_ = df["open"].values

        recent_high = float(np.max(high[-20:-1]))
        recent_low = float(np.min(low[-20:-1]))

        current_high = float(high[-1])
        current_low = float(low[-1])
        current_close = float(close[-1])
        current_open = float(open_[-1])

        direction, strength, sweep = "neutral", 0.0, "none"

        # body 0 방어
        body = max(abs(current_close - current_open), current_close * 0.0001)

        # 상단 스윕: 고점 돌파했다가 종가는 아래
        if current_high > recent_high and current_close < recent_high:
            wick_above = current_high - recent_high
            sweep = "high_swept"
            direction = "short"
            # 04-13: ATR proxy에 최소 floor 추가 (H17: tight range에서 strength 항상 1.0)
            atr_proxy = max((recent_high - recent_low) / 20, current_close * 0.001)
            wick_ratio = wick_above / atr_proxy if atr_proxy > 0 else 0
            strength = min(1.0, 0.4 + wick_ratio * 0.3)
            if current_close < current_open:
                strength = min(1.0, strength + 0.2)

        # 하단 스윕: 저점 돌파했다가 종가는 위
        elif current_low < recent_low and current_close > recent_low:
            wick_below = recent_low - current_low
            sweep = "low_swept"
            direction = "long"
            atr_proxy = max((recent_high - recent_low) / 20, current_close * 0.001)
            wick_ratio = wick_below / atr_proxy if atr_proxy > 0 else 0
            strength = min(1.0, 0.4 + wick_ratio * 0.3)
            if current_close > current_open:
                strength = min(1.0, strength + 0.2)

        return {"type": "liquidity_sweep", "direction": direction, "strength": round(strength, 2),
                "sweep": sweep, "recent_high": round(recent_high, 1), "recent_low": round(recent_low, 1)}

    def _fvg(self, df: pd.DataFrame) -> dict:
        """1m FVG — 갭 채우기 매매 + 방향 확인"""
        if len(df) < 10:
            return {"type": "fvg", "direction": "neutral", "strength": 0.0, "gap": None}

        high = df["high"].values
        low = df["low"].values
        close = df["close"].values
        price = float(close[-1])

        # 가격 추세 확인 (FVG는 추세 방향에 맞아야 강함)
        trend_up = close[-1] > close[-5] if len(close) >= 5 else True

        fvgs = []
        for i in range(len(df) - 3, max(len(df) - 12, 0), -1):
            # Bullish FVG: 1봉 고가 < 3봉 저가 (가격이 위로 갭)
            if high[i] < low[i + 2]:
                gap_size = low[i + 2] - high[i]
                gap_pct = gap_size / price * 100
                if gap_pct > 0.05:
                    fvgs.append({"dir": "long", "top": float(low[i + 2]), "bottom": float(high[i]),
                                 "size_pct": gap_pct, "age": len(df) - 1 - i})

            # Bearish FVG: 1봉 저가 > 3봉 고가 (가격이 아래로 갭)
            if low[i] > high[i + 2]:
                gap_size = low[i] - high[i + 2]
                gap_pct = gap_size / price * 100
                if gap_pct > 0.05:
                    fvgs.append({"dir": "short", "top": float(low[i]), "bottom": float(high[i + 2]),
                                 "size_pct": gap_pct, "age": len(df) - 1 - i})

        if not fvgs:
            return {"type": "fvg", "direction": "neutral", "strength": 0.0, "gap": None}

        # 가격이 FVG 존에 진입 + 추세 일치
        for fvg in fvgs:
            if not (fvg["bottom"] <= price <= fvg["top"]):
                continue

            # 방향 확인: 추세와 일치할 때만
            if fvg["dir"] == "long" and trend_up:
                strength = min(1.0, fvg["size_pct"] / 0.2)
                return {"type": "fvg", "direction": "long", "strength": round(strength, 2),
                        "gap": [fvg["bottom"], fvg["top"]], "gap_pct": round(fvg["size_pct"], 3)}

            elif fvg["dir"] == "short" and not trend_up:
                strength = min(1.0, fvg["size_pct"] / 0.2)
                return {"type": "fvg", "direction": "short", "strength": round(strength, 2),
                        "gap": [fvg["bottom"], fvg["top"]], "gap_pct": round(fvg["size_pct"], 3)}

        return {"type": "fvg", "direction": "neutral", "strength": 0.0, "gap": None}

    # ══════════════════════════════════════
    # 강화 시그널 (13~15)
    # ══════════════════════════════════════

    def _vwap_levels(self, df_5m: pd.DataFrame, df_1m: pd.DataFrame) -> dict:
        """VWAP 일중 레벨"""
        if len(df_5m) < 50:
            return {"type": "vwap_levels", "direction": "neutral", "strength": 0.0, "vwap": 0}

        recent = df_5m.tail(288) if len(df_5m) >= 288 else df_5m
        typical = (recent["high"] + recent["low"] + recent["close"]) / 3
        vol_sum = recent["volume"].sum()
        if vol_sum <= 0:
            return {"type": "vwap_levels", "direction": "neutral", "strength": 0.0, "vwap": 0}

        vwap = float((typical * recent["volume"]).sum() / vol_sum)
        current_price = float(df_5m["close"].iloc[-1])
        prev_price = float(df_5m["close"].iloc[-2])
        distance_pct = (current_price - vwap) / vwap * 100

        direction = "neutral"
        strength = 0.0

        # VWAP 돌파
        if prev_price < vwap < current_price:
            direction = "long"
            strength = 0.7
        elif prev_price > vwap > current_price:
            direction = "short"
            strength = 0.7
        # 04-15: 평균회귀 제거 — VWAP 돌파만 추세추종으로 사용
        # (기존 평균회귀가 73% 역방향의 원인 중 하나)

        return {"type": "vwap_levels", "direction": direction, "strength": round(strength, 2),
                "vwap": round(vwap, 1), "distance_pct": round(distance_pct, 3)}

    def _pivot_points(self, df: pd.DataFrame) -> dict:
        """피봇 포인트 — 가용 데이터로 동작 (최소 100봉)"""
        if len(df) < 100:
            return {"type": "pivot_points", "direction": "neutral", "strength": 0.0}

        # 데이터가 부족하면 가용 범위로
        if len(df) >= 576:
            prev_day = df.iloc[-576:-288]
        elif len(df) >= 288:
            prev_day = df.iloc[-288:]
        else:
            prev_day = df.iloc[-min(288, len(df)//2):-1]

        prev_high = float(prev_day["high"].max())
        prev_low = float(prev_day["low"].min())
        prev_close = float(prev_day["close"].iloc[-1])

        pp = (prev_high + prev_low + prev_close) / 3
        r1 = 2 * pp - prev_low
        s1 = 2 * pp - prev_high
        r2 = pp + (prev_high - prev_low)
        s2 = pp - (prev_high - prev_low)

        current = float(df["close"].iloc[-1])

        direction = "neutral"
        strength = 0.0
        level = None

        # 피봇 레벨 근접 (0.15% 이내로 완화)
        levels = [("R2", r2, "short"), ("R1", r1, "short"),
                  ("S1", s1, "long"), ("S2", s2, "long")]

        for name, lvl, dir_hint in levels:
            if abs(current - lvl) / current < 0.0015:
                direction = dir_hint
                level = name
                strength = 0.8 if name in ("R2", "S2") else 0.6
                break

        return {"type": "pivot_points", "direction": direction, "strength": round(strength, 2),
                "level": level, "pp": round(pp, 1), "r1": round(r1, 1), "s1": round(s1, 1)}

    def _break_of_structure(self, df: pd.DataFrame) -> dict:
        """BOS — 1m 스윙 돌파 (스윙 조건 완화)"""
        if len(df) < 20:
            return {"type": "bos", "direction": "neutral", "strength": 0.0, "bos": "none"}

        high = df["high"].values
        low = df["low"].values
        close = df["close"].values

        # 스윙 포인트: 양옆 1봉만 비교 (3봉 패턴, 더 자주 발생)
        swing_highs = []
        swing_lows = []
        for i in range(1, len(high) - 1):
            if high[i] > high[i-1] and high[i] > high[i+1]:
                swing_highs.append((i, high[i]))
            if low[i] < low[i-1] and low[i] < low[i+1]:
                swing_lows.append((i, low[i]))

        if not swing_highs or not swing_lows:
            return {"type": "bos", "direction": "neutral", "strength": 0.0, "bos": "none"}

        last_swing_high = float(swing_highs[-1][1])
        last_swing_low = float(swing_lows[-1][1])
        current = float(close[-1])
        prev = float(close[-2]) if len(close) >= 2 else current

        direction = "neutral"
        strength = 0.0
        bos_type = "none"

        # 상승 BOS
        if current > last_swing_high and prev <= last_swing_high:
            direction = "long"
            bos_type = "bullish_bos"
            overshoot = (current - last_swing_high) / last_swing_high
            # 0.1% → 0.35, 0.5% → 0.55, 1% → 0.8, 2% → 1.0 (노이즈 돌파 과대평가 방지)
            strength = min(1.0, max(0.3, overshoot * 50 + 0.3))

        # 하락 BOS
        elif current < last_swing_low and prev >= last_swing_low:
            direction = "short"
            bos_type = "bearish_bos"
            overshoot = (last_swing_low - current) / last_swing_low
            strength = min(1.0, max(0.3, overshoot * 50 + 0.3))

        return {"type": "bos", "direction": direction, "strength": round(strength, 2),
                "bos": bos_type, "swing_high": round(last_swing_high, 1),
                "swing_low": round(last_swing_low, 1)}

    # ══════════════════════════════════════
    # 필터 (16~17)
    # ══════════════════════════════════════

    def _session_filter(self, df: pd.DataFrame) -> dict:
        """세션 필터"""
        if len(df) == 0:
            return {"type": "session", "session": "unknown", "quality": "low", "multiplier": 0.8}

        ts = int(df["timestamp"].iloc[-1])
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        hour = dt.hour
        weekday = dt.weekday()

        if weekday >= 5:
            return {"type": "session", "session": "weekend", "quality": "poor", "multiplier": 0.6}

        if 13 <= hour < 17:
            return {"type": "session", "session": "us_eu_overlap", "quality": "best", "multiplier": 1.2}
        elif 13 <= hour < 21:
            return {"type": "session", "session": "us", "quality": "good", "multiplier": 1.1}
        elif 7 <= hour < 16:
            return {"type": "session", "session": "eu", "quality": "normal", "multiplier": 1.0}
        elif 0 <= hour < 8:
            return {"type": "session", "session": "asia", "quality": "low", "multiplier": 0.8}  # 0.7 → 0.8
        else:
            return {"type": "session", "session": "late_us", "quality": "normal", "multiplier": 0.9}

    def _anti_chop(self, df: pd.DataFrame) -> dict:
        """안티첩 — Wilder's smoothing 적용 정통 ADX"""
        if len(df) < 30:
            return {"type": "anti_chop", "is_chop": False, "adx": 20}

        high = df["high"].values
        low = df["low"].values
        close = df["close"].values
        n = len(high)

        # True Range
        tr = np.zeros(n)
        for i in range(1, n):
            tr[i] = max(high[i] - low[i],
                        abs(high[i] - close[i-1]),
                        abs(low[i] - close[i-1]))

        # Directional Movement
        plus_dm = np.zeros(n)
        minus_dm = np.zeros(n)
        for i in range(1, n):
            up = high[i] - high[i-1]
            down = low[i-1] - low[i]
            plus_dm[i] = up if (up > down and up > 0) else 0
            minus_dm[i] = down if (down > up and down > 0) else 0

        # Wilder's smoothing
        period = 14
        if n < period + 1:
            return {"type": "anti_chop", "is_chop": False, "adx": 20}

        atr = np.zeros(n)
        smooth_plus = np.zeros(n)
        smooth_minus = np.zeros(n)
        atr[period] = np.mean(tr[1:period + 1])
        smooth_plus[period] = np.mean(plus_dm[1:period + 1])
        smooth_minus[period] = np.mean(minus_dm[1:period + 1])

        for i in range(period + 1, n):
            atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
            smooth_plus[i] = (smooth_plus[i-1] * (period - 1) + plus_dm[i]) / period
            smooth_minus[i] = (smooth_minus[i-1] * (period - 1) + minus_dm[i]) / period

        # DX → ADX
        dx_values = []
        for i in range(period, n):
            if atr[i] > 0:
                pdi = smooth_plus[i] / atr[i] * 100
                mdi = smooth_minus[i] / atr[i] * 100
                if (pdi + mdi) > 0:
                    dx_values.append(abs(pdi - mdi) / (pdi + mdi) * 100)

        adx = float(np.mean(dx_values[-period:])) if len(dx_values) >= period else 20.0

        # 방향 전환 횟수 (보조 지표)
        direction_changes = 0
        for i in range(-10, -1):
            if i - 1 >= -len(close) and i + 1 < 0:
                if (close[i] > close[i-1]) != (close[i+1] > close[i]):
                    direction_changes += 1

        # 첩 판단 — OR 조건으로 강화 (04-09 박스권 미감지 패턴 fix)
        # (1) ADX < 18 + 방향 전환 6회+ (옛 AND 조건)
        # (2) ADX 매우 낮음 (< 14) → 즉시 chop (확실한 횡보)
        # (3) 방향 전환 8회+ (강한 진동) → 즉시 chop
        is_chop = (adx < 18 and direction_changes >= 6) \
                  or (adx < 14) \
                  or (direction_changes >= 8)

        return {"type": "anti_chop", "is_chop": is_chop, "adx": round(adx, 1),
                "direction_changes": direction_changes}

    # ══════════════════════════════════════
    # 해외 유명 기법 (19~21)
    # ══════════════════════════════════════

    def _rsi2_extreme(self, candles_1m: pd.DataFrame, candles_5m: pd.DataFrame) -> dict:
        """
        RSI(2) Extreme Mean Reversion v2 — 승률 65~72%
        출처: Larry Connors / Renaissance Technologies 변형
        강화: 다중 RSI 확인 + Stochastic 보조 + BB %B 겹침 + 연속봉 카운트
        """
        result = {"type": "rsi2_extreme", "direction": "neutral", "strength": 0.0,
                  "rsi2": 50.0, "stoch_k": 50.0, "consecutive": 0}
        if len(candles_1m) < 20 or len(candles_5m) < 200:
            return result

        close_1m = candles_1m["close"]
        price = float(close_1m.iloc[-1])

        # RSI(2) — 초민감 과매도/과매수 감지
        delta = close_1m.diff()
        gain = delta.clip(lower=0).rolling(2).mean()
        loss = (-delta.clip(upper=0)).rolling(2).mean()
        rs = gain / loss.replace(0, 1e-10)
        rsi2 = 100 - (100 / (1 + rs))
        current_rsi2 = float(rsi2.iloc[-1])
        if np.isnan(current_rsi2):
            return result

        # Stochastic(5,3,3) — 추가 과매도/과매수 확인
        low_5 = candles_1m["low"].rolling(5).min()
        high_5 = candles_1m["high"].rolling(5).max()
        stoch_raw = (close_1m - low_5) / (high_5 - low_5 + 1e-10) * 100
        stoch_k = float(stoch_raw.rolling(3).mean().iloc[-1])
        if np.isnan(stoch_k):
            stoch_k = 50.0

        # BB %B — 밴드 대비 위치 (0 미만 = 하단 이탈)
        sma20 = close_1m.rolling(20).mean()
        std20 = close_1m.rolling(20).std()
        bb_upper = sma20 + 2.0 * std20
        bb_lower = sma20 - 2.0 * std20
        bb_range = bb_upper - bb_lower
        pct_b = float(((close_1m - bb_lower) / bb_range.replace(0, 1)).iloc[-1])

        # 5m EMA(50) + EMA(200) 이중 추세 필터
        ema50 = float(candles_5m["close"].ewm(span=50, adjust=False).mean().iloc[-1])
        ema200 = float(candles_5m["close"].ewm(span=200, adjust=False).mean().iloc[-1])
        trend_up = price > ema200 and ema50 > ema200    # 확실한 상승 추세
        trend_down = price < ema200 and ema50 < ema200  # 확실한 하락 추세

        # 연속 하락/상승 봉 카운트 (mean reversion 강도 지표)
        consecutive_down = 0
        consecutive_up = 0
        for i in range(-1, -min(8, len(close_1m)), -1):
            if close_1m.iloc[i] < close_1m.iloc[i-1]:
                consecutive_down += 1
            else:
                break
        for i in range(-1, -min(8, len(close_1m)), -1):
            if close_1m.iloc[i] > close_1m.iloc[i-1]:
                consecutive_up += 1
            else:
                break

        # 볼륨 필터: 현재 봉 < 2x 평균 (폭락/펌프 중 잡지 않기)
        vol_avg = float(candles_1m["volume"].rolling(20).mean().iloc[-1])
        vol_current = float(candles_1m["volume"].iloc[-1])
        vol_ok = vol_current < vol_avg * 2.0 if vol_avg > 0 else True

        # ── 롱 조건: RSI(2) < 10 + Stoch < 20 + 추세 상승 ──
        if current_rsi2 < 10 and stoch_k < 20 and trend_up and vol_ok:
            base = 0.4
            # RSI 극단 보너스 (5 미만 강한 시그널)
            base += min(0.2, (10 - current_rsi2) / 10 * 0.2)
            # BB 하단 이탈 보너스
            if pct_b < 0.05:
                base += 0.15
            # 연속 하락봉 보너스 (3봉+ = 강한 MR)
            if consecutive_down >= 3:
                base += min(0.15, consecutive_down * 0.03)
            # Stoch 극단 보너스
            if stoch_k < 10:
                base += 0.1

            result["direction"] = "long"
            result["strength"] = round(min(1.0, base), 3)
            result["rsi2"] = round(current_rsi2, 2)
            result["stoch_k"] = round(stoch_k, 2)
            result["consecutive"] = consecutive_down
            result["bb_pct_b"] = round(pct_b, 3)

        # ── 숏 조건: RSI(2) > 90 + Stoch > 80 + 추세 하락 ──
        elif current_rsi2 > 90 and stoch_k > 80 and trend_down and vol_ok:
            base = 0.4
            base += min(0.2, (current_rsi2 - 90) / 10 * 0.2)
            if pct_b > 0.95:
                base += 0.15
            if consecutive_up >= 3:
                base += min(0.15, consecutive_up * 0.03)
            if stoch_k > 90:
                base += 0.1

            result["direction"] = "short"
            result["strength"] = round(min(1.0, base), 3)
            result["rsi2"] = round(current_rsi2, 2)
            result["stoch_k"] = round(stoch_k, 2)
            result["consecutive"] = consecutive_up
            result["bb_pct_b"] = round(pct_b, 3)

        return result

    def _vwap_mean_reversion(self, candles_5m: pd.DataFrame, candles_1m: pd.DataFrame) -> dict:
        """
        VWAP Band Mean Reversion v2 — 승률 60~68%
        출처: Brian Shannon "VWAP Bible", CryptoFace 크립토 적용
        강화: 3σ 밴드 + 볼륨 클라이맥스 + VWAP 슬로프 필터 + 1m 캔들 확인
        """
        result = {"type": "vwap_mean_reversion", "direction": "neutral", "strength": 0.0,
                  "vwap": 0.0, "band_touch": "none", "vwap_slope": 0.0}
        if len(candles_5m) < 30 or len(candles_1m) < 10:
            return result

        # 세션 VWAP + σ 밴드
        lookback = min(288, len(candles_5m))
        df = candles_5m.iloc[-lookback:]
        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        volume = df["volume"].replace(0, 1)
        cum_vol = volume.cumsum()
        cum_tp_vol = (typical_price * volume).cumsum()
        vwap = cum_tp_vol / cum_vol
        sq_diff = ((typical_price - vwap) ** 2 * volume).cumsum() / cum_vol
        std = np.sqrt(sq_diff.clip(lower=0))

        cv = float(vwap.iloc[-1])
        cs1 = float(std.iloc[-1])
        upper_2 = cv + 2 * cs1
        lower_2 = cv - 2 * cs1
        upper_3 = cv + 3 * cs1
        lower_3 = cv - 3 * cs1
        price = float(candles_5m["close"].iloc[-1])

        if cs1 < price * 0.0001:  # 밴드 너무 좁으면 스킵
            return result

        result["vwap"] = round(cv, 1)

        # VWAP 슬로프 (기울기 — 양수=상승, 음수=하락)
        if len(vwap) >= 6:
            vwap_slope = (float(vwap.iloc[-1]) - float(vwap.iloc[-6])) / float(vwap.iloc[-6]) * 100
        else:
            vwap_slope = 0.0
        result["vwap_slope"] = round(vwap_slope, 4)

        # 레인징 판별: ADX < 25 (기존 was_inside는 상승장에서도 통과하는 버그)
        _high = candles_5m["high"].astype(float).values
        _low = candles_5m["low"].astype(float).values
        _cls = candles_5m["close"].astype(float).values
        _adx_val = self._calc_adx_simple(_high, _low, _cls, period=14)
        is_ranging = _adx_val < 25  # ADX < 25 = 비추세 (횡보/약추세)

        # 볼륨 클라이맥스: 현재 봉 볼륨 > 2x 평균 (피로 신호)
        vol_avg = float(df["volume"].rolling(20).mean().iloc[-1])
        vol_current = float(candles_5m["volume"].iloc[-1])
        vol_climax = vol_current > vol_avg * 2.0 if vol_avg > 0 else False

        # RSI(9) on 1m
        delta = candles_1m["close"].diff()
        gain = delta.clip(lower=0).rolling(9).mean()
        loss_s = (-delta.clip(upper=0)).rolling(9).mean()
        rs = gain / loss_s.replace(0, 1e-10)
        rsi9 = float((100 - (100 / (1 + rs))).iloc[-1])
        if np.isnan(rsi9):
            rsi9 = 50.0

        # 1m 확인 캔들: 반전 봉 (해머/인걸핑)
        c1m = candles_1m.iloc[-1]
        c1m_bull = c1m["close"] > c1m["open"]  # 양봉
        c1m_bear = c1m["close"] < c1m["open"]  # 음봉

        # ── 롱: -2σ 이하 터치 + RSI < 30 + 레인징 + 확인 봉 ──
        if price <= lower_2 and rsi9 < 30 and is_ranging:
            base = 0.4
            # -3σ 터치 보너스 (극단 이탈)
            if price <= lower_3:
                base += 0.2
                result["band_touch"] = "-3sigma"
            else:
                result["band_touch"] = "-2sigma"
            # 볼륨 클라이맥스 보너스 (매도 피로)
            if vol_climax:
                base += 0.15
            # 1m 양봉 확인
            if c1m_bull:
                base += 0.1
            # VWAP 상향 슬로프 = 매수 방향 OK
            if vwap_slope > 0:
                base += 0.05
            result["direction"] = "long"
            result["strength"] = round(min(1.0, base), 3)

        # ── 숏: +2σ 이상 터치 + RSI > 70 + 레인징 + 확인 봉 ──
        elif price >= upper_2 and rsi9 > 70 and is_ranging:
            base = 0.4
            if price >= upper_3:
                base += 0.2
                result["band_touch"] = "+3sigma"
            else:
                result["band_touch"] = "+2sigma"
            if vol_climax:
                base += 0.15
            if c1m_bear:
                base += 0.1
            if vwap_slope < 0:
                base += 0.05
            result["direction"] = "short"
            result["strength"] = round(min(1.0, base), 3)

        return result

    def _liquidation_cascade_fade(self, candles_1m: pd.DataFrame) -> dict:
        """
        Liquidation Cascade Fade v2 — RR 2:1+, 승률 50~55%
        출처: Coinglass/Hyblock Capital, PhoenixBTC
        강화: 다중 봉 캐스케이드 감지 + V자 반전 패턴 + 볼륨 감소 확인 + 스프레드 분석
        """
        result = {"type": "liq_cascade_fade", "direction": "neutral", "strength": 0.0,
                  "cascade_type": "none", "cascade_bars": 0, "recovery_pct": 0.0}
        if len(candles_1m) < 25:
            return result

        vol_avg = float(candles_1m["volume"].iloc[-22:-2].mean()) if len(candles_1m) > 22 else 1.0
        if vol_avg <= 0:
            vol_avg = 1.0

        # ── 다중 봉 캐스케이드 감지 (최근 1~3봉 연속 대형봉) ──
        cascade_bars = 0
        cascade_direction = "none"  # "down" or "up"
        total_move = 0.0
        max_vol_spike = 0.0

        for i in range(-3, 0):  # -3, -2, -1 (마지막 3봉 검사, 현재봉 제외는 아래서)
            if abs(i) > len(candles_1m) - 1:
                continue
            bar = candles_1m.iloc[i]
            bar_move = (bar["close"] - bar["open"]) / bar["open"] * 100 if bar["open"] > 0 else 0
            bar_vol_spike = bar["volume"] / vol_avg

            if abs(bar_move) >= 0.15 and bar_vol_spike >= 2.0:
                if cascade_direction == "none":
                    cascade_direction = "down" if bar_move < 0 else "up"
                # 같은 방향이면 캐스케이드 연장
                if (cascade_direction == "down" and bar_move < 0) or \
                   (cascade_direction == "up" and bar_move > 0):
                    cascade_bars += 1
                    total_move += bar_move
                    max_vol_spike = max(max_vol_spike, bar_vol_spike)

        if cascade_bars == 0 or abs(total_move) < 0.25:
            return result

        # ── 현재 봉 (확인 봉): 반전 패턴 ──
        curr = candles_1m.iloc[-1]
        prev = candles_1m.iloc[-2]
        curr_body = abs(curr["close"] - curr["open"])
        curr_range = curr["high"] - curr["low"]
        if curr_range <= 0:
            return result

        # V자 반전: 현재 봉이 캐스케이드 반대 방향
        curr_bull = curr["close"] > curr["open"]
        curr_bear = curr["close"] < curr["open"]

        # 하단 위크 비율 (롱 반전) / 상단 위크 비율 (숏 반전)
        lower_wick = min(curr["open"], curr["close"]) - curr["low"]
        upper_wick = curr["high"] - max(curr["open"], curr["close"])
        lower_wick_pct = lower_wick / curr_range
        upper_wick_pct = upper_wick / curr_range

        # 볼륨 감소 확인 (캐스케이드 소진 = 볼륨 줄어듦)
        curr_vol_ratio = curr["volume"] / vol_avg if vol_avg > 0 else 1.0
        volume_declining = curr_vol_ratio < max_vol_spike * 0.7  # 캐스케이드 봉 대비 30%+ 감소

        # 회복률: 캐스케이드 범위 대비 현재 봉 되돌림
        cascade_range = abs(prev["close"] - prev["open"]) if prev["open"] > 0 else 1
        if cascade_direction == "down":
            recovery = (curr["close"] - curr["open"]) / cascade_range if cascade_range > 0 else 0
        else:
            recovery = (curr["open"] - curr["close"]) / cascade_range if cascade_range > 0 else 0

        result["cascade_bars"] = cascade_bars
        result["recovery_pct"] = round(recovery * 100, 1)

        # ── 롱 페이드: 하방 캐스케이드 + 양봉 반전 + 하단 위크 ──
        if cascade_direction == "down" and curr_bull:
            base = 0.35
            # 캐스케이드 강도 보너스
            base += min(0.2, cascade_bars * 0.07)
            base += min(0.15, abs(total_move) * 0.15)
            # 볼륨 스파이크 보너스
            base += min(0.1, max_vol_spike * 0.015)
            # 하단 위크 보너스 (V자 바닥 확인)
            if lower_wick_pct > 0.4:
                base += 0.1
            # 볼륨 감소 = 매도 피로
            if volume_declining:
                base += 0.1

            result["direction"] = "long"
            result["strength"] = round(min(1.0, base), 3)
            result["cascade_type"] = "down_fade"

        # ── 숏 페이드: 상방 캐스케이드 + 음봉 반전 + 상단 위크 ──
        elif cascade_direction == "up" and curr_bear:
            base = 0.35
            base += min(0.2, cascade_bars * 0.07)
            base += min(0.15, abs(total_move) * 0.15)
            base += min(0.1, max_vol_spike * 0.015)
            if upper_wick_pct > 0.4:
                base += 0.1
            if volume_declining:
                base += 0.1

            result["direction"] = "short"
            result["strength"] = round(min(1.0, base), 3)
            result["cascade_type"] = "up_fade"

        return result

    # ══════════════════════════════════════
    # 실시간 급등락 감지 (22)
    # ══════════════════════════════════════

    def _realtime_spike(self, velocity: dict = None) -> dict:
        """
        WebSocket 실시간 가격 변속도 기반 급등락 감지
        $200+ 이동 in 10~60초 = 스파이크 → 추세 추종 진입
        """
        result = {"type": "realtime_spike", "direction": "neutral", "strength": 0.0,
                  "move_30s": 0.0, "range_60s": 0.0}
        if not velocity:
            return result

        try:
            move_10s = float(velocity.get("move_10s", 0))
            move_30s = float(velocity.get("move_30s", 0))
            move_60s = float(velocity.get("move_60s", 0))
            range_60s = float(velocity.get("range_60s", 0))
        except (ValueError, TypeError):
            return result

        result["move_30s"] = move_30s
        result["range_60s"] = range_60s

        # ── 스파이크 판별: 방향성 있는 큰 이동 ──
        # 10초 내 $150+, 30초 내 $300+, 60초 내 $500+ = 급등락
        direction = "neutral"
        strength = 0.0

        # 10초 급등락 (가장 빠른 반응)
        if abs(move_10s) >= 150:
            direction = "long" if move_10s > 0 else "short"
            strength = max(strength, min(1.0, abs(move_10s) / 500))

        # 30초 급등락
        if abs(move_30s) >= 300:
            direction = "long" if move_30s > 0 else "short"
            strength = max(strength, min(1.0, abs(move_30s) / 800))

        # 60초 급등락
        if abs(move_60s) >= 500:
            direction = "long" if move_60s > 0 else "short"
            strength = max(strength, min(1.0, abs(move_60s) / 1200))

        if strength > 0:
            result["direction"] = direction
            result["strength"] = round(strength, 3)

        return result

    # ══════════════════════════════════════
    # 유틸
    # ══════════════════════════════════════

    def _calc_adx_simple(self, high, low, close, period=14) -> float:
        """ADX 간이 계산 (VWAP MR 레인징 판별용)"""
        n = len(high)
        if n < period + 1:
            return 15.0
        tr = np.zeros(n)
        plus_dm = np.zeros(n)
        minus_dm = np.zeros(n)
        for i in range(1, n):
            tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
            up = high[i] - high[i-1]
            dn = low[i-1] - low[i]
            plus_dm[i] = up if (up > dn and up > 0) else 0
            minus_dm[i] = dn if (dn > up and dn > 0) else 0
        atr = np.zeros(n)
        sp = np.zeros(n)
        sm = np.zeros(n)
        atr[period] = np.mean(tr[1:period+1])
        sp[period] = np.mean(plus_dm[1:period+1])
        sm[period] = np.mean(minus_dm[1:period+1])
        for i in range(period+1, n):
            atr[i] = (atr[i-1]*(period-1) + tr[i]) / period
            sp[i] = (sp[i-1]*(period-1) + plus_dm[i]) / period
            sm[i] = (sm[i-1]*(period-1) + minus_dm[i]) / period
        dx_vals = []
        for i in range(period, n):
            if atr[i] > 0:
                pdi = sp[i] / atr[i] * 100
                mdi = sm[i] / atr[i] * 100
                if pdi + mdi > 0:
                    dx_vals.append(abs(pdi - mdi) / (pdi + mdi) * 100)
        return float(np.mean(dx_vals[-period:])) if len(dx_vals) >= period else 15.0

    def _calc_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift(1)).abs(),
            (df["low"] - df["close"].shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(period).mean()
        return float(atr.iloc[-1]) if not np.isnan(atr.iloc[-1]) else 0
