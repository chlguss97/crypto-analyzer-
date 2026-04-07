"""
Scalp Engine v2 — 급변동 스캘핑 특화
TF: 1m/5m 실행, 15m 확인
보유: 5~15분 (급변동), 5~30분 (일반)
핵심: 횡보 중 급변동 포착 → 빠른 진입/탈출

시그널 구성:
  [기존] 1. EMA 크로스  2. RSI 반전  3. BB 돌파  4. 거래량 스파이크  5. 모멘텀
  [신규] 6. 변동성 폭발  7. 레인지 브레이크아웃  8. 캔들 패턴  9. 급속 모멘텀
"""
import pandas as pd
import numpy as np
from src.engine.base import BaseIndicator


class ScalpEngine:
    """단타 전용 시그널 엔진 v2 — 급변동 스캘핑 강화"""

    def __init__(self):
        self.name = "scalp"
        self.target_daily_pct = 5.0
        self.max_hold_bars_1m = 30
        self.max_hold_bars_5m = 6
        self.default_leverage = 25
        self.sl_atr_mult = 0.8
        self.tp_rr = 2.0

    async def analyze(self, candles_1m: pd.DataFrame, candles_5m: pd.DataFrame,
                      candles_15m: pd.DataFrame = None) -> dict:
        """단타 시그널 분석 — 기존 + 급변동 시그널"""
        signals = {}
        score_long = 0.0
        score_short = 0.0

        # ── 기존 시그널 (1~5) ──

        # 1. EMA 크로스 (5m)
        ema_sig = self._ema_cross(candles_5m)
        signals["ema_cross"] = ema_sig
        if ema_sig["direction"] == "long":
            score_long += ema_sig["strength"] * 2.5
        elif ema_sig["direction"] == "short":
            score_short += ema_sig["strength"] * 2.5

        # 2. RSI 극단 반전 (1m)
        rsi_sig = self._rsi_reversal(candles_1m)
        signals["rsi_reversal"] = rsi_sig
        if rsi_sig["direction"] == "long":
            score_long += rsi_sig["strength"] * 2.0
        elif rsi_sig["direction"] == "short":
            score_short += rsi_sig["strength"] * 2.0

        # 3. BB 스퀴즈 돌파 (5m)
        bb_sig = self._bb_breakout(candles_5m)
        signals["bb_breakout"] = bb_sig
        if bb_sig["direction"] == "long":
            score_long += bb_sig["strength"] * 3.0
        elif bb_sig["direction"] == "short":
            score_short += bb_sig["strength"] * 3.0

        # 4. 거래량 스파이크 (1m)
        vol_sig = self._volume_spike(candles_1m)
        signals["volume_spike"] = vol_sig
        if vol_sig["direction"] == "long":
            score_long += vol_sig["strength"] * 2.0
        elif vol_sig["direction"] == "short":
            score_short += vol_sig["strength"] * 2.0

        # 5. 모멘텀 (1m - 3봉 연속 방향)
        mom_sig = self._momentum(candles_1m)
        signals["momentum"] = mom_sig
        if mom_sig["direction"] == "long":
            score_long += mom_sig["strength"] * 1.5
        elif mom_sig["direction"] == "short":
            score_short += mom_sig["strength"] * 1.5

        # ── 급변동 시그널 (6~9) ──

        # 6. 변동성 폭발 감지 (ATR 급등 + BB 확장)
        vol_explode = self._volatility_explosion(candles_5m)
        signals["vol_explosion"] = vol_explode
        if vol_explode["direction"] == "long":
            score_long += vol_explode["strength"] * 3.5
        elif vol_explode["direction"] == "short":
            score_short += vol_explode["strength"] * 3.5

        # 7. 레인지 브레이크아웃 (횡보 후 돌파)
        range_brk = self._range_breakout(candles_5m, candles_1m)
        signals["range_breakout"] = range_brk
        if range_brk["direction"] == "long":
            score_long += range_brk["strength"] * 3.5
        elif range_brk["direction"] == "short":
            score_short += range_brk["strength"] * 3.5

        # 8. 캔들 패턴 (급변동 시그널)
        candle_sig = self._candle_pattern(candles_1m)
        signals["candle_pattern"] = candle_sig
        if candle_sig["direction"] == "long":
            score_long += candle_sig["strength"] * 2.0
        elif candle_sig["direction"] == "short":
            score_short += candle_sig["strength"] * 2.0

        # 9. 급속 모멘텀 (1분 내 큰 움직임)
        rapid = self._rapid_momentum(candles_1m)
        signals["rapid_momentum"] = rapid
        if rapid["direction"] == "long":
            score_long += rapid["strength"] * 2.5
        elif rapid["direction"] == "short":
            score_short += rapid["strength"] * 2.5

        # ── 15m 추세 필터 ──
        trend_filter = "neutral"
        if candles_15m is not None and len(candles_15m) >= 20:
            ema50_15m = candles_15m["close"].ewm(span=50, adjust=False).mean().iloc[-1]
            if candles_15m["close"].iloc[-1] > ema50_15m:
                trend_filter = "long"
            else:
                trend_filter = "short"

        # ── 점수 계산 ──
        max_possible = 22.5  # 2.5+2+3+2+1.5 + 3.5+3.5+2+2.5
        if score_long > score_short:
            direction = "long"
            raw = score_long
        elif score_short > score_long:
            direction = "short"
            raw = score_short
        else:
            direction = "neutral"
            raw = 0

        # 15m 추세 역방향이면 감점 (단, 급변동 시그널이 강하면 감점 약화)
        is_explosive = (vol_explode["strength"] > 0.5 or range_brk["strength"] > 0.5
                        or rapid["strength"] > 0.5)
        if trend_filter != "neutral" and trend_filter != direction:
            raw *= 0.7 if is_explosive else 0.5

        score = min(10.0, raw / max_possible * 10)

        # 급변동 모드 판단
        explosive_mode = is_explosive and score >= 2.5

        # ATR 계산
        atr = self._calc_atr(candles_5m, 14)
        atr_pct = atr / candles_5m["close"].iloc[-1] * 100 if atr > 0 else 0.2

        # 급변동 시 SL/TP 조정 (타이트하게)
        if explosive_mode:
            sl_mult = 0.6   # 더 타이트한 SL
            tp_mult = 1.5   # 빠른 TP (1.5R)
        else:
            sl_mult = self.sl_atr_mult
            tp_mult = self.tp_rr

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
            "sl_distance": round(atr * sl_mult, 2),
            "tp_distance": round(atr * sl_mult * tp_mult, 2),
            "leverage": self.default_leverage,
            "explosive_mode": explosive_mode,
        }

    # ── 기존 시그널 ──

    def _ema_cross(self, df: pd.DataFrame) -> dict:
        close = df["close"]
        ema5 = close.ewm(span=5, adjust=False).mean()
        ema13 = close.ewm(span=13, adjust=False).mean()
        ema21 = close.ewm(span=21, adjust=False).mean()

        direction = "neutral"
        strength = 0.0

        for i in range(-3, 0):
            if len(ema5) > abs(i) + 1:
                prev = i - 1
                if ema5.iloc[prev] <= ema13.iloc[prev] and ema5.iloc[i] > ema13.iloc[i]:
                    direction = "long"
                    strength = 0.8 if close.iloc[i] > ema21.iloc[i] else 0.5
                elif ema5.iloc[prev] >= ema13.iloc[prev] and ema5.iloc[i] < ema13.iloc[i]:
                    direction = "short"
                    strength = 0.8 if close.iloc[i] < ema21.iloc[i] else 0.5

        if ema5.iloc[-1] > ema13.iloc[-1] > ema21.iloc[-1]:
            if direction == "long":
                strength = min(1.0, strength + 0.2)
        elif ema5.iloc[-1] < ema13.iloc[-1] < ema21.iloc[-1]:
            if direction == "short":
                strength = min(1.0, strength + 0.2)

        return {"type": "ema_cross", "direction": direction, "strength": round(strength, 2)}

    def _rsi_reversal(self, df: pd.DataFrame) -> dict:
        close = df["close"]
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1/7, min_periods=7, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/7, min_periods=7, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.inf)
        rsi = 100 - (100 / (1 + rs))

        r = rsi.iloc[-1]
        r_prev = rsi.iloc[-2] if len(rsi) >= 2 else 50

        direction = "neutral"
        strength = 0.0

        if r < 25 and r > r_prev:
            direction = "long"
            strength = min(1.0, (30 - r) / 15)
        elif r > 75 and r < r_prev:
            direction = "short"
            strength = min(1.0, (r - 70) / 15)

        return {"type": "rsi_reversal", "direction": direction, "strength": round(strength, 2), "rsi": round(r, 1)}

    def _bb_breakout(self, df: pd.DataFrame) -> dict:
        close = df["close"]
        sma = close.rolling(20).mean()
        std = close.rolling(20).std()
        upper = sma + 2 * std
        lower = sma - 2 * std

        width = ((upper - lower) / sma).dropna()
        min_width = width.tail(50).min() if len(width) >= 50 else width.min()
        current_width = width.iloc[-1] if len(width) > 0 else 0

        is_squeeze = current_width <= min_width * 1.2 if min_width > 0 else False
        last = close.iloc[-1]

        direction = "neutral"
        strength = 0.0

        if is_squeeze:
            if last > upper.iloc[-1]:
                direction = "long"
                strength = 0.9
            elif last < lower.iloc[-1]:
                direction = "short"
                strength = 0.9
            else:
                strength = 0.3

        return {"type": "bb_breakout", "direction": direction, "strength": round(strength, 2), "squeeze": is_squeeze}

    def _volume_spike(self, df: pd.DataFrame) -> dict:
        vol = df["volume"]
        close = df["close"]
        avg = vol.rolling(20).mean()
        ratio = vol.iloc[-1] / avg.iloc[-1] if avg.iloc[-1] > 0 else 1

        direction = "neutral"
        strength = 0.0

        if ratio > 2.0:
            if close.iloc[-1] > close.iloc[-2]:
                direction = "long"
            else:
                direction = "short"
            strength = min(1.0, ratio / 5.0)

        return {"type": "volume_spike", "direction": direction, "strength": round(strength, 2), "ratio": round(ratio, 2)}

    def _momentum(self, df: pd.DataFrame) -> dict:
        close = df["close"]
        if len(close) < 4:
            return {"type": "momentum", "direction": "neutral", "strength": 0.0}

        changes = [close.iloc[-i] - close.iloc[-i-1] for i in range(1, 4)]
        up = all(c > 0 for c in changes)
        down = all(c < 0 for c in changes)

        if up:
            avg_move = np.mean(changes) / close.iloc[-1] * 100
            return {"type": "momentum", "direction": "long", "strength": min(1.0, avg_move / 0.1)}
        elif down:
            avg_move = abs(np.mean(changes)) / close.iloc[-1] * 100
            return {"type": "momentum", "direction": "short", "strength": min(1.0, avg_move / 0.1)}

        return {"type": "momentum", "direction": "neutral", "strength": 0.0}

    # ── 급변동 시그널 (신규) ──

    def _volatility_explosion(self, df: pd.DataFrame) -> dict:
        """
        변동성 폭발 감지
        - ATR이 최근 평균 대비 2배+ 급등
        - BB Width가 하위 20%에서 상위 80%로 급확장
        - 이전 N봉 횡보 후 갑작스런 움직임
        """
        close = df["close"].values
        high = df["high"].values
        low = df["low"].values

        if len(close) < 30:
            return {"type": "vol_explosion", "direction": "neutral", "strength": 0.0,
                    "atr_ratio": 1.0, "expanding": False}

        # ATR 급등 비율
        tr = np.maximum(high[1:] - low[1:],
                        np.maximum(np.abs(high[1:] - close[:-1]),
                                   np.abs(low[1:] - close[:-1])))
        recent_atr = np.mean(tr[-3:])     # 최근 3봉 ATR
        avg_atr = np.mean(tr[-20:])       # 20봉 평균 ATR
        atr_ratio = recent_atr / avg_atr if avg_atr > 0 else 1.0

        # BB Width 변화
        sma = pd.Series(close).rolling(20).mean()
        std = pd.Series(close).rolling(20).std()
        bb_width = ((sma + 2*std - (sma - 2*std)) / sma).dropna()

        expanding = False
        if len(bb_width) >= 5:
            prev_width = float(bb_width.iloc[-5])
            curr_width = float(bb_width.iloc[-1])
            expanding = curr_width > prev_width * 1.5  # 50% 이상 확장

        # 횡보 후 폭발 감지
        # 이전 10봉의 변동 범위 vs 최근 3봉의 변동 범위
        prev_range = np.max(high[-13:-3]) - np.min(low[-13:-3])
        recent_range = np.max(high[-3:]) - np.min(low[-3:])
        range_ratio = recent_range / prev_range if prev_range > 0 else 1.0

        direction = "neutral"
        strength = 0.0

        if atr_ratio >= 2.0 or (expanding and range_ratio >= 0.5):
            # 방향 판단: 최근 3봉의 종가 방향
            if close[-1] > close[-3]:
                direction = "long"
            elif close[-1] < close[-3]:
                direction = "short"

            # 강도: ATR 비율과 레인지 비율 결합
            strength = min(1.0, (atr_ratio - 1.0) / 3.0 + (range_ratio - 0.3) / 2.0)
            strength = max(0.0, strength)

        return {
            "type": "vol_explosion", "direction": direction, "strength": round(strength, 2),
            "atr_ratio": round(atr_ratio, 2), "expanding": expanding,
            "range_ratio": round(range_ratio, 2),
        }

    def _range_breakout(self, df_5m: pd.DataFrame, df_1m: pd.DataFrame) -> dict:
        """
        레인지 브레이크아웃
        - 최근 20봉(5m = 100분)의 고가/저가를 레인지로 정의
        - 돌파 + 거래량 확인 → 진입
        - 페이크아웃 필터: 돌파 봉의 종가가 레인지 밖에 있어야 함
        """
        if len(df_5m) < 25:
            return {"type": "range_breakout", "direction": "neutral", "strength": 0.0,
                    "range_high": 0, "range_low": 0, "breakout": "none"}

        # 최근 20봉 레인지 (현재 봉 제외)
        lookback = df_5m.iloc[-21:-1]
        range_high = float(lookback["high"].max())
        range_low = float(lookback["low"].min())
        range_size = range_high - range_low

        current_close = float(df_5m["close"].iloc[-1])
        current_high = float(df_5m["high"].iloc[-1])
        current_low = float(df_5m["low"].iloc[-1])

        # 레인지 폭이 너무 넓으면 횡보가 아님 → 스킵
        range_pct = range_size / current_close * 100
        if range_pct > 2.0 or range_pct < 0.1:
            return {"type": "range_breakout", "direction": "neutral", "strength": 0.0,
                    "range_high": round(range_high, 1), "range_low": round(range_low, 1),
                    "breakout": "none"}

        direction = "neutral"
        strength = 0.0
        breakout = "none"

        # 상단 돌파
        if current_close > range_high and current_low < range_high:
            # 종가가 레인지 밖 (페이크아웃 필터)
            overshoot = (current_close - range_high) / range_size
            if overshoot > 0.1:  # 10% 이상 돌파
                direction = "long"
                breakout = "upper"
                strength = min(1.0, overshoot * 2)

                # 1m 거래량 확인 (돌파 시점 거래량 스파이크)
                if len(df_1m) >= 5:
                    vol_avg = float(df_1m["volume"].tail(20).mean())
                    vol_now = float(df_1m["volume"].iloc[-1])
                    if vol_now > vol_avg * 1.5:
                        strength = min(1.0, strength + 0.2)

        # 하단 돌파
        elif current_close < range_low and current_high > range_low:
            overshoot = (range_low - current_close) / range_size
            if overshoot > 0.1:
                direction = "short"
                breakout = "lower"
                strength = min(1.0, overshoot * 2)

                if len(df_1m) >= 5:
                    vol_avg = float(df_1m["volume"].tail(20).mean())
                    vol_now = float(df_1m["volume"].iloc[-1])
                    if vol_now > vol_avg * 1.5:
                        strength = min(1.0, strength + 0.2)

        return {
            "type": "range_breakout", "direction": direction, "strength": round(strength, 2),
            "range_high": round(range_high, 1), "range_low": round(range_low, 1),
            "range_pct": round(range_pct, 3), "breakout": breakout,
        }

    def _candle_pattern(self, df: pd.DataFrame) -> dict:
        """
        급변동 캔들 패턴 감지
        - 대형 장대봉 (바디가 최근 평균의 2배+)
        - 긴 꼬리 반전 (핀바)
        - 갭 캔들 (이전 종가 대비 갭)
        """
        if len(df) < 10:
            return {"type": "candle_pattern", "direction": "neutral", "strength": 0.0, "pattern": "none"}

        o = float(df["open"].iloc[-1])
        h = float(df["high"].iloc[-1])
        l = float(df["low"].iloc[-1])
        c = float(df["close"].iloc[-1])
        prev_c = float(df["close"].iloc[-2])

        body = abs(c - o)
        full_range = h - l if h > l else 0.0001
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l

        # 최근 10봉 평균 바디
        bodies = (df["close"].tail(10) - df["open"].tail(10)).abs()
        avg_body = float(bodies.mean())

        direction = "neutral"
        strength = 0.0
        pattern = "none"

        # 1) 대형 장대봉 (바디 2배+)
        if avg_body > 0 and body > avg_body * 2:
            if c > o:
                direction = "long"
                pattern = "big_bull"
            else:
                direction = "short"
                pattern = "big_bear"
            strength = min(1.0, body / avg_body / 4)

        # 2) 핀바 (긴 꼬리 반전)
        elif lower_wick > body * 2 and lower_wick > full_range * 0.6:
            direction = "long"
            pattern = "pin_bar_bull"
            strength = min(0.8, lower_wick / full_range)

        elif upper_wick > body * 2 and upper_wick > full_range * 0.6:
            direction = "short"
            pattern = "pin_bar_bear"
            strength = min(0.8, upper_wick / full_range)

        # 3) 갭 (이전 종가 대비 0.1%+ 갭)
        elif prev_c > 0:
            gap_pct = abs(o - prev_c) / prev_c * 100
            if gap_pct > 0.1:
                if o > prev_c:
                    direction = "long"
                    pattern = "gap_up"
                else:
                    direction = "short"
                    pattern = "gap_down"
                strength = min(0.7, gap_pct / 0.3)

        return {"type": "candle_pattern", "direction": direction, "strength": round(strength, 2), "pattern": pattern}

    def _rapid_momentum(self, df: pd.DataFrame) -> dict:
        """
        급속 모멘텀 — 1분 내 큰 가격 움직임 감지
        - 최근 1봉의 변동이 20봉 평균의 3배+
        - 최근 3봉 합산 변동이 크고 같은 방향
        """
        if len(df) < 25:
            return {"type": "rapid_momentum", "direction": "neutral", "strength": 0.0,
                    "move_ratio": 0, "consecutive": 0}

        close = df["close"].values
        changes = np.diff(close)

        # 최근 1봉 vs 20봉 평균
        last_move = abs(changes[-1])
        avg_move = np.mean(np.abs(changes[-20:]))
        move_ratio = last_move / avg_move if avg_move > 0 else 1.0

        # 최근 3봉 연속 같은 방향 + 크기
        recent_3 = changes[-3:]
        all_up = all(c > 0 for c in recent_3)
        all_down = all(c < 0 for c in recent_3)
        total_move = abs(sum(recent_3))
        total_pct = total_move / close[-4] * 100 if close[-4] > 0 else 0

        direction = "neutral"
        strength = 0.0
        consecutive = 0

        if move_ratio >= 3.0 or (total_pct >= 0.15 and (all_up or all_down)):
            if all_up or changes[-1] > 0:
                direction = "long"
            elif all_down or changes[-1] < 0:
                direction = "short"

            # 연속 같은 방향 봉 수
            d = 1 if changes[-1] > 0 else -1
            for i in range(len(changes) - 1, -1, -1):
                if (changes[i] > 0 and d > 0) or (changes[i] < 0 and d < 0):
                    consecutive += 1
                else:
                    break

            strength = min(1.0, max(move_ratio / 5.0, total_pct / 0.3))

        return {
            "type": "rapid_momentum", "direction": direction, "strength": round(strength, 2),
            "move_ratio": round(move_ratio, 2), "total_pct": round(total_pct, 4),
            "consecutive": consecutive,
        }

    # ── 공통 유틸 ──

    def _calc_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift(1)).abs(),
            (df["low"] - df["close"].shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(period).mean()
        return float(atr.iloc[-1]) if not np.isnan(atr.iloc[-1]) else 0
