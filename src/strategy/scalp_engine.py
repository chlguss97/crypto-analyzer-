"""
Scalp Engine v3 — 스캘핑 전문 엔진
횡보 중 급변동 포착 → 빠른 진입/탈출

시그널 구성 (15종):
  [기본]    1.EMA크로스  2.RSI반전  3.BB돌파  4.거래량스파이크  5.모멘텀
  [급변동]  6.변동성폭발  7.레인지브레이크아웃  8.캔들패턴  9.급속모멘텀
  [SMC]    10.오더블록(1m/5m)  11.유동성스윕  12.FVG(1m)
  [필터]   13.세션필터  14.안티첩필터
  [관리]   15.트레일링스탑 모드
"""
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from src.engine.base import BaseIndicator


class ScalpEngine:
    """스캘핑 전문 엔진 v3"""

    def __init__(self):
        self.name = "scalp"
        self.default_leverage = 25
        self.sl_atr_mult = 0.8
        self.tp_rr = 2.0

    async def analyze(self, candles_1m: pd.DataFrame, candles_5m: pd.DataFrame,
                      candles_15m: pd.DataFrame = None) -> dict:
        signals = {}
        score_long = 0.0
        score_short = 0.0

        # ── 기본 시그널 (1~5) ──
        ema_sig = self._ema_cross(candles_5m)
        signals["ema_cross"] = ema_sig
        score_long += ema_sig["strength"] * 2.5 if ema_sig["direction"] == "long" else 0
        score_short += ema_sig["strength"] * 2.5 if ema_sig["direction"] == "short" else 0

        rsi_sig = self._rsi_reversal(candles_1m)
        signals["rsi_reversal"] = rsi_sig
        score_long += rsi_sig["strength"] * 2.0 if rsi_sig["direction"] == "long" else 0
        score_short += rsi_sig["strength"] * 2.0 if rsi_sig["direction"] == "short" else 0

        bb_sig = self._bb_breakout(candles_5m)
        signals["bb_breakout"] = bb_sig
        score_long += bb_sig["strength"] * 3.0 if bb_sig["direction"] == "long" else 0
        score_short += bb_sig["strength"] * 3.0 if bb_sig["direction"] == "short" else 0

        vol_sig = self._volume_spike(candles_1m)
        signals["volume_spike"] = vol_sig
        score_long += vol_sig["strength"] * 2.0 if vol_sig["direction"] == "long" else 0
        score_short += vol_sig["strength"] * 2.0 if vol_sig["direction"] == "short" else 0

        mom_sig = self._momentum(candles_1m)
        signals["momentum"] = mom_sig
        score_long += mom_sig["strength"] * 1.5 if mom_sig["direction"] == "long" else 0
        score_short += mom_sig["strength"] * 1.5 if mom_sig["direction"] == "short" else 0

        # ── 급변동 시그널 (6~9) ──
        vol_explode = self._volatility_explosion(candles_5m)
        signals["vol_explosion"] = vol_explode
        score_long += vol_explode["strength"] * 3.5 if vol_explode["direction"] == "long" else 0
        score_short += vol_explode["strength"] * 3.5 if vol_explode["direction"] == "short" else 0

        range_brk = self._range_breakout(candles_5m, candles_1m)
        signals["range_breakout"] = range_brk
        score_long += range_brk["strength"] * 3.5 if range_brk["direction"] == "long" else 0
        score_short += range_brk["strength"] * 3.5 if range_brk["direction"] == "short" else 0

        candle_sig = self._candle_pattern(candles_1m)
        signals["candle_pattern"] = candle_sig
        score_long += candle_sig["strength"] * 2.0 if candle_sig["direction"] == "long" else 0
        score_short += candle_sig["strength"] * 2.0 if candle_sig["direction"] == "short" else 0

        rapid = self._rapid_momentum(candles_1m)
        signals["rapid_momentum"] = rapid
        score_long += rapid["strength"] * 2.5 if rapid["direction"] == "long" else 0
        score_short += rapid["strength"] * 2.5 if rapid["direction"] == "short" else 0

        # ── SMC 시그널 (10~12) ──
        ob_sig = self._scalp_order_block(candles_5m, candles_1m)
        signals["scalp_ob"] = ob_sig
        score_long += ob_sig["strength"] * 4.0 if ob_sig["direction"] == "long" else 0
        score_short += ob_sig["strength"] * 4.0 if ob_sig["direction"] == "short" else 0

        liq_sig = self._liquidity_sweep(candles_1m)
        signals["liquidity_sweep"] = liq_sig
        score_long += liq_sig["strength"] * 3.5 if liq_sig["direction"] == "long" else 0
        score_short += liq_sig["strength"] * 3.5 if liq_sig["direction"] == "short" else 0

        fvg_sig = self._scalp_fvg(candles_1m)
        signals["scalp_fvg"] = fvg_sig
        score_long += fvg_sig["strength"] * 2.5 if fvg_sig["direction"] == "long" else 0
        score_short += fvg_sig["strength"] * 2.5 if fvg_sig["direction"] == "short" else 0

        # ── 필터 (13~14) ──
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
        max_possible = 32.5  # 기본11 + 급변동11.5 + SMC10
        if score_long > score_short:
            direction = "long"
            raw = score_long
        elif score_short > score_long:
            direction = "short"
            raw = score_short
        else:
            direction = "neutral"
            raw = 0

        # 필터 적용
        is_explosive = (vol_explode["strength"] > 0.5 or range_brk["strength"] > 0.5
                        or rapid["strength"] > 0.5)

        # 15m 역방향 감점
        if trend_filter != "neutral" and trend_filter != direction:
            raw *= 0.7 if is_explosive else 0.5

        # 세션 배율
        raw *= session["multiplier"]

        # 안티첩: 횡보 잡음이면 감점
        if antichop["is_chop"]:
            raw *= 0.5

        score = min(10.0, raw / max_possible * 10)

        # 급변동 + SMC 모드 판단
        explosive_mode = is_explosive and score >= 2.5
        smc_entry = ob_sig["strength"] > 0.5 or liq_sig["strength"] > 0.5

        # ATR 계산
        atr = self._calc_atr(candles_5m, 14)
        atr_pct = atr / candles_5m["close"].iloc[-1] * 100 if atr > 0 else 0.2

        # SL/TP 모드별 조정
        if smc_entry:
            # SMC 진입: OB 존 기반 타이트 SL, 높은 RR
            sl_mult = 0.5
            tp_mult = 2.5
            use_trailing = False
        elif explosive_mode:
            # 급변동: 타이트 SL + 트레일링
            sl_mult = 0.6
            tp_mult = 1.5
            use_trailing = True
        else:
            sl_mult = self.sl_atr_mult
            tp_mult = self.tp_rr
            use_trailing = False

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
            "smc_entry": smc_entry,
            "use_trailing": use_trailing,
            "session": session["session"],
            "session_quality": session["quality"],
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
        close = df["close"]
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1/7, min_periods=7, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/7, min_periods=7, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.inf)
        rsi = 100 - (100 / (1 + rs))
        r, r_prev = rsi.iloc[-1], rsi.iloc[-2] if len(rsi) >= 2 else 50
        direction, strength = "neutral", 0.0

        if r < 25 and r > r_prev:
            direction, strength = "long", min(1.0, (30 - r) / 15)
        elif r > 75 and r < r_prev:
            direction, strength = "short", min(1.0, (r - 70) / 15)

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
        vol, close = df["volume"], df["close"]
        avg = vol.rolling(20).mean()
        ratio = vol.iloc[-1] / avg.iloc[-1] if avg.iloc[-1] > 0 else 1
        direction, strength = "neutral", 0.0

        if ratio > 2.0:
            direction = "long" if close.iloc[-1] > close.iloc[-2] else "short"
            strength = min(1.0, ratio / 5.0)

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
            direction = "long" if close[-1] > close[-3] else "short"
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
                if len(df_1m) >= 5:
                    vol_ratio = float(df_1m["volume"].iloc[-1]) / float(df_1m["volume"].tail(20).mean())
                    if vol_ratio > 1.5:
                        strength = min(1.0, strength + 0.2)

        elif current_close < range_low:
            overshoot = (range_low - current_close) / range_size
            if overshoot > 0.1:
                direction, breakout = "short", "lower"
                strength = min(1.0, overshoot * 2)
                if len(df_1m) >= 5:
                    vol_ratio = float(df_1m["volume"].iloc[-1]) / float(df_1m["volume"].tail(20).mean())
                    if vol_ratio > 1.5:
                        strength = min(1.0, strength + 0.2)

        return {"type": "range_breakout", "direction": direction, "strength": round(strength, 2), "breakout": breakout}

    def _candle_pattern(self, df: pd.DataFrame) -> dict:
        """장대봉, 핀바, 갭"""
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

        if avg_body > 0 and body > avg_body * 2:
            direction = "long" if c > o else "short"
            pattern = "big_bull" if c > o else "big_bear"
            strength = min(1.0, body / avg_body / 4)
        elif lower_wick > body * 2 and lower_wick > full_range * 0.6:
            direction, pattern, strength = "long", "pin_bar_bull", min(0.8, lower_wick / full_range)
        elif upper_wick > body * 2 and upper_wick > full_range * 0.6:
            direction, pattern, strength = "short", "pin_bar_bear", min(0.8, upper_wick / full_range)
        elif prev_c > 0 and abs(o - prev_c) / prev_c * 100 > 0.1:
            direction = "long" if o > prev_c else "short"
            pattern = "gap_up" if o > prev_c else "gap_down"
            strength = min(0.7, abs(o - prev_c) / prev_c * 100 / 0.3)

        return {"type": "candle_pattern", "direction": direction, "strength": round(strength, 2), "pattern": pattern}

    def _rapid_momentum(self, df: pd.DataFrame) -> dict:
        """1분 내 큰 가격 움직임"""
        if len(df) < 25:
            return {"type": "rapid_momentum", "direction": "neutral", "strength": 0.0}

        close = df["close"].values
        changes = np.diff(close)
        last_move = abs(changes[-1])
        avg_move = np.mean(np.abs(changes[-20:]))
        move_ratio = last_move / avg_move if avg_move > 0 else 1.0

        recent_3 = changes[-3:]
        all_up = all(c > 0 for c in recent_3)
        all_down = all(c < 0 for c in recent_3)
        total_pct = abs(sum(recent_3)) / close[-4] * 100 if close[-4] > 0 else 0

        direction, strength = "neutral", 0.0
        if move_ratio >= 3.0 or (total_pct >= 0.15 and (all_up or all_down)):
            direction = "long" if (all_up or changes[-1] > 0) else "short"
            strength = min(1.0, max(move_ratio / 5.0, total_pct / 0.3))

        return {"type": "rapid_momentum", "direction": direction, "strength": round(strength, 2),
                "move_ratio": round(move_ratio, 2), "total_pct": round(total_pct, 4)}

    # ══════════════════════════════════════
    # SMC 시그널 (10~12) — 스캘핑 핵심
    # ══════════════════════════════════════

    def _scalp_order_block(self, df_5m: pd.DataFrame, df_1m: pd.DataFrame) -> dict:
        """
        1m/5m 오더블록 — 스캘핑 정밀 진입
        - 5m에서 OB 존 탐지 (임펄스 직전 반대 캔들)
        - 1m 가격이 OB 존에 도달 → 반전 확인 → 진입
        """
        if len(df_5m) < 30 or len(df_1m) < 10:
            return {"type": "scalp_ob", "direction": "neutral", "strength": 0.0, "zone": None}

        close_5m = df_5m["close"].values
        open_5m = df_5m["open"].values
        high_5m = df_5m["high"].values
        low_5m = df_5m["low"].values

        # ATR (5m)
        tr = np.maximum(high_5m[1:] - low_5m[1:], np.maximum(
            np.abs(high_5m[1:] - close_5m[:-1]), np.abs(low_5m[1:] - close_5m[:-1])))
        atr = np.mean(tr[-14:]) if len(tr) >= 14 else np.mean(tr) if len(tr) > 0 else 1

        # OB 탐지 (최근 30봉)
        obs = []
        for i in range(3, min(30, len(df_5m) - 3)):
            if i + 3 >= len(df_5m):
                continue

            move = close_5m[-(i-2)] - close_5m[-(i+1)]
            if abs(move) < atr * 1.2:
                continue

            idx = len(df_5m) - i - 1
            is_bull_ob = move > 0 and close_5m[idx] < open_5m[idx]  # 음봉 후 상승
            is_bear_ob = move < 0 and close_5m[idx] > open_5m[idx]  # 양봉 후 하락

            if is_bull_ob:
                obs.append({"dir": "long", "low": low_5m[idx], "high": high_5m[idx],
                            "strength": min(1.0, abs(move) / atr / 3), "age": i})
            elif is_bear_ob:
                obs.append({"dir": "short", "low": low_5m[idx], "high": high_5m[idx],
                            "strength": min(1.0, abs(move) / atr / 3), "age": i})

        if not obs:
            return {"type": "scalp_ob", "direction": "neutral", "strength": 0.0, "zone": None}

        # 현재 1m 가격
        price = float(df_1m["close"].iloc[-1])
        prev_price = float(df_1m["close"].iloc[-2]) if len(df_1m) >= 2 else price

        # 가장 가까운 OB 존에 가격 도달 + 반전 확인
        best = None
        for ob in obs:
            # Bullish OB: 가격이 OB 존까지 내려왔다가 반등
            if ob["dir"] == "long" and price <= ob["high"] * 1.001 and price >= ob["low"] * 0.999:
                if price > prev_price:  # 반등 확인
                    if best is None or ob["strength"] > best["strength"]:
                        best = ob

            # Bearish OB: 가격이 OB 존까지 올라갔다가 하락
            elif ob["dir"] == "short" and price >= ob["low"] * 0.999 and price <= ob["high"] * 1.001:
                if price < prev_price:  # 하락 확인
                    if best is None or ob["strength"] > best["strength"]:
                        best = ob

        if not best:
            return {"type": "scalp_ob", "direction": "neutral", "strength": 0.0,
                    "zone": None, "nearby_count": len(obs)}

        return {
            "type": "scalp_ob", "direction": best["dir"],
            "strength": round(best["strength"], 2),
            "zone": [best["low"], best["high"]],
            "age": best["age"],
            "nearby_count": len(obs),
        }

    def _liquidity_sweep(self, df: pd.DataFrame) -> dict:
        """
        유동성 스윕 — 스탑헌팅 후 반전
        - 최근 고점/저점을 돌파(스윕) 후 빠르게 되돌림
        - 고래가 개미 스탑을 털고 방향 전환
        """
        if len(df) < 30:
            return {"type": "liquidity_sweep", "direction": "neutral", "strength": 0.0, "sweep": "none"}

        high = df["high"].values
        low = df["low"].values
        close = df["close"].values
        open_ = df["open"].values

        # 최근 20봉의 주요 고점/저점 (스윙 포인트)
        recent_high = np.max(high[-20:-1])  # 현재 봉 제외
        recent_low = np.min(low[-20:-1])

        current_high = high[-1]
        current_low = low[-1]
        current_close = close[-1]
        current_open = open_[-1]

        direction, strength, sweep = "neutral", 0.0, "none"

        # 상단 스윕: 고점 돌파했다가 종가는 아래로 마감 (베어리시 반전)
        if current_high > recent_high and current_close < recent_high:
            wick_above = current_high - recent_high
            body = abs(current_close - current_open)
            if wick_above > 0 and body > 0:
                sweep = "high_swept"
                direction = "short"
                strength = min(1.0, wick_above / body * 0.5 + 0.3)
                # 종가가 시가 아래면 더 강함 (음봉 마감)
                if current_close < current_open:
                    strength = min(1.0, strength + 0.2)

        # 하단 스윕: 저점 돌파했다가 종가는 위로 마감 (불리시 반전)
        elif current_low < recent_low and current_close > recent_low:
            wick_below = recent_low - current_low
            body = abs(current_close - current_open)
            if wick_below > 0 and body > 0:
                sweep = "low_swept"
                direction = "long"
                strength = min(1.0, wick_below / body * 0.5 + 0.3)
                if current_close > current_open:
                    strength = min(1.0, strength + 0.2)

        return {"type": "liquidity_sweep", "direction": direction, "strength": round(strength, 2),
                "sweep": sweep, "recent_high": round(recent_high, 1), "recent_low": round(recent_low, 1)}

    def _scalp_fvg(self, df: pd.DataFrame) -> dict:
        """
        1m FVG — 급등/급락 후 갭 채우기 매매
        - 3봉 패턴: 1봉 고가 < 3봉 저가 (불리시 FVG) 또는 반대
        - 가격이 갭으로 되돌아오면 진입
        """
        if len(df) < 10:
            return {"type": "scalp_fvg", "direction": "neutral", "strength": 0.0, "gap": None}

        high = df["high"].values
        low = df["low"].values
        close = df["close"].values
        price = close[-1]

        # 최근 10봉에서 FVG 찾기
        fvgs = []
        for i in range(len(df) - 3, max(len(df) - 12, 0), -1):
            # Bullish FVG: 1봉 고가 < 3봉 저가 (위로 갭)
            if high[i] < low[i + 2]:
                gap_size = low[i + 2] - high[i]
                gap_pct = gap_size / price * 100
                if gap_pct > 0.05:
                    fvgs.append({"dir": "long", "top": low[i + 2], "bottom": high[i],
                                 "size_pct": gap_pct, "age": len(df) - 1 - i})

            # Bearish FVG: 1봉 저가 > 3봉 고가 (아래로 갭)
            if low[i] > high[i + 2]:
                gap_size = low[i] - high[i + 2]
                gap_pct = gap_size / price * 100
                if gap_pct > 0.05:
                    fvgs.append({"dir": "short", "top": low[i], "bottom": high[i + 2],
                                 "size_pct": gap_pct, "age": len(df) - 1 - i})

        if not fvgs:
            return {"type": "scalp_fvg", "direction": "neutral", "strength": 0.0, "gap": None}

        # 가격이 FVG 존에 진입했는지 확인
        for fvg in fvgs:
            if fvg["dir"] == "long" and fvg["bottom"] <= price <= fvg["top"]:
                # 갭 채우러 내려온 상태 → 롱 진입
                strength = min(1.0, fvg["size_pct"] / 0.2)
                return {"type": "scalp_fvg", "direction": "long", "strength": round(strength, 2),
                        "gap": [fvg["bottom"], fvg["top"]], "gap_pct": round(fvg["size_pct"], 3)}

            elif fvg["dir"] == "short" and fvg["bottom"] <= price <= fvg["top"]:
                strength = min(1.0, fvg["size_pct"] / 0.2)
                return {"type": "scalp_fvg", "direction": "short", "strength": round(strength, 2),
                        "gap": [fvg["bottom"], fvg["top"]], "gap_pct": round(fvg["size_pct"], 3)}

        return {"type": "scalp_fvg", "direction": "neutral", "strength": 0.0, "gap": None}

    # ══════════════════════════════════════
    # 필터 (13~14)
    # ══════════════════════════════════════

    def _session_filter(self, df: pd.DataFrame) -> dict:
        """
        세션 필터 — 시간대별 스캘핑 적합도
        - 유럽+미국 겹침 (13:00~17:00 UTC / 22:00~02:00 KST): 최적 → 1.2x
        - 미국 세션 (13:00~21:00 UTC): 좋음 → 1.1x
        - 유럽 세션 (07:00~16:00 UTC): 보통 → 1.0x
        - 아시아 세션 (00:00~08:00 UTC): 변동성 낮음 → 0.7x
        - 주말: 유동성 부족 → 0.6x
        """
        if len(df) == 0:
            return {"type": "session", "session": "unknown", "quality": "low", "multiplier": 0.8}

        ts = int(df["timestamp"].iloc[-1])
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        hour = dt.hour
        weekday = dt.weekday()

        if weekday >= 5:  # 주말
            return {"type": "session", "session": "weekend", "quality": "poor", "multiplier": 0.6}

        if 13 <= hour < 17:
            return {"type": "session", "session": "us_eu_overlap", "quality": "best", "multiplier": 1.2}
        elif 13 <= hour < 21:
            return {"type": "session", "session": "us", "quality": "good", "multiplier": 1.1}
        elif 7 <= hour < 16:
            return {"type": "session", "session": "eu", "quality": "normal", "multiplier": 1.0}
        elif 0 <= hour < 8:
            return {"type": "session", "session": "asia", "quality": "low", "multiplier": 0.7}
        else:
            return {"type": "session", "session": "late_us", "quality": "normal", "multiplier": 0.9}

    def _anti_chop(self, df: pd.DataFrame) -> dict:
        """
        안티첩 필터 — 횡보 잡음 구간 감지
        - 5m ADX < 15 → 추세 없음 (첩)
        - 최근 10봉이 좁은 레인지에서 왔다갔다 → 첩
        """
        if len(df) < 20:
            return {"type": "anti_chop", "is_chop": False, "adx": 0}

        close = df["close"].values
        high = df["high"].values
        low = df["low"].values

        # 간이 ADX
        tr = np.maximum(high[1:] - low[1:], np.maximum(
            np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])))

        plus_dm = np.zeros(len(high) - 1)
        minus_dm = np.zeros(len(high) - 1)
        for i in range(len(high) - 1):
            up = high[i + 1] - high[i]
            down = low[i] - low[i + 1]
            plus_dm[i] = up if up > down and up > 0 else 0
            minus_dm[i] = down if down > up and down > 0 else 0

        period = 14
        if len(tr) < period:
            return {"type": "anti_chop", "is_chop": False, "adx": 20}

        atr = np.mean(tr[-period:])
        pdi = np.mean(plus_dm[-period:]) / atr * 100 if atr > 0 else 0
        mdi = np.mean(minus_dm[-period:]) / atr * 100 if atr > 0 else 0
        dx = abs(pdi - mdi) / (pdi + mdi) * 100 if (pdi + mdi) > 0 else 0
        adx = dx  # 간이 계산

        # 레인지 첩 감지: 최근 10봉의 방향 전환 횟수
        direction_changes = 0
        for i in range(-10, -1):
            if (close[i] > close[i-1]) != (close[i+1] > close[i]):
                direction_changes += 1

        is_chop = adx < 15 or direction_changes >= 7

        return {"type": "anti_chop", "is_chop": is_chop, "adx": round(adx, 1),
                "direction_changes": direction_changes}

    # ══════════════════════════════════════
    # 유틸
    # ══════════════════════════════════════

    def _calc_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift(1)).abs(),
            (df["low"] - df["close"].shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(period).mean()
        return float(atr.iloc[-1]) if not np.isnan(atr.iloc[-1]) else 0
