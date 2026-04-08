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

        # 거래량은 5m 사용 (1m은 너무 작음)
        vol_sig = self._volume_spike(candles_5m)
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
        signals["order_block"] = ob_sig
        score_long += ob_sig["strength"] * 4.0 if ob_sig["direction"] == "long" else 0
        score_short += ob_sig["strength"] * 4.0 if ob_sig["direction"] == "short" else 0

        liq_sig = self._liquidity_sweep(candles_1m)
        signals["liquidity_sweep"] = liq_sig
        score_long += liq_sig["strength"] * 3.5 if liq_sig["direction"] == "long" else 0
        score_short += liq_sig["strength"] * 3.5 if liq_sig["direction"] == "short" else 0

        fvg_sig = self._fvg(candles_1m)
        signals["fvg"] = fvg_sig
        score_long += fvg_sig["strength"] * 2.5 if fvg_sig["direction"] == "long" else 0
        score_short += fvg_sig["strength"] * 2.5 if fvg_sig["direction"] == "short" else 0

        # ── 강화 시그널 (13~15) ──
        vwap_sig = self._vwap_levels(candles_5m, candles_1m)
        signals["vwap_levels"] = vwap_sig
        score_long += vwap_sig["strength"] * 3.0 if vwap_sig["direction"] == "long" else 0
        score_short += vwap_sig["strength"] * 3.0 if vwap_sig["direction"] == "short" else 0

        pivot_sig = self._pivot_points(candles_5m)
        signals["pivot_points"] = pivot_sig
        score_long += pivot_sig["strength"] * 2.5 if pivot_sig["direction"] == "long" else 0
        score_short += pivot_sig["strength"] * 2.5 if pivot_sig["direction"] == "short" else 0

        bos_sig = self._break_of_structure(candles_1m)
        signals["bos"] = bos_sig
        score_long += bos_sig["strength"] * 3.5 if bos_sig["direction"] == "long" else 0
        score_short += bos_sig["strength"] * 3.5 if bos_sig["direction"] == "short" else 0

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
        # max_possible = 41.5 는 18 시그널 모두 한 방향 strength=1.0 가정 (비현실적)
        # 실측: 한 방향 발동률 ~30%, strength 평균 ~0.4 → 한 방향 raw 평균 5~10
        # 정규화 분모를 15.0 으로 낮춰 임계값(0.5~2.5) 대비 합리적 점수 분포 확보
        max_possible = 15.0
        if score_long > score_short:
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

        # 15m 역방향 (급변동/SMC면 약화)
        if trend_filter != "neutral" and trend_filter != direction:
            if smc_entry:
                penalty += 0.10  # SMC는 카운터 트레이드 가능
            elif is_explosive:
                penalty += 0.15
            else:
                penalty += 0.30

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
            sl_mult = 0.5
            tp_mult = 2.5
            use_trailing = False
        elif explosive_mode:
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
    # SMC 시그널 (10~12)
    # ══════════════════════════════════════

    def _scalp_order_block(self, df_5m: pd.DataFrame, df_1m: pd.DataFrame) -> dict:
        """
        1m/5m 오더블록 — 정확한 인덱싱 (양수 인덱스 사용)
        - 5m에서 OB 탐지: 임펄스 직전 반대 캔들
        - 1m 가격이 OB 존에 도달 + 반전 → 진입
        """
        if len(df_5m) < 30 or len(df_1m) < 10:
            return {"type": "order_block", "direction": "neutral", "strength": 0.0, "zone": None}

        close_5m = df_5m["close"].values
        open_5m = df_5m["open"].values
        high_5m = df_5m["high"].values
        low_5m = df_5m["low"].values
        n = len(df_5m)

        # ATR (5m)
        tr = np.maximum(high_5m[1:] - low_5m[1:], np.maximum(
            np.abs(high_5m[1:] - close_5m[:-1]), np.abs(low_5m[1:] - close_5m[:-1])))
        atr = float(np.mean(tr[-14:])) if len(tr) >= 14 else float(np.mean(tr)) if len(tr) > 0 else 1.0

        # OB 탐지: 양수 인덱스 사용 (최근 30봉 중 최근 5봉은 제외 — OB는 발생 후 시간 필요)
        obs = []
        start = max(0, n - 30)
        end = n - 5  # 최근 5봉은 제외 (OB가 형성될 시간 필요)

        for i in range(start, end):
            # i 봉 이후 3봉 이내에 임펄스 발생 체크
            if i + 3 >= n:
                continue

            # 임펄스: i+1 ~ i+3 봉의 최대 이동
            future_close = close_5m[i + 3]
            move = future_close - close_5m[i]

            if abs(move) < atr * 1.2:
                continue

            # OB 캔들: i 봉이 임펄스 방향과 반대 색이어야 함
            is_bull_ob = move > 0 and close_5m[i] < open_5m[i]  # 음봉 후 강한 상승
            is_bear_ob = move < 0 and close_5m[i] > open_5m[i]  # 양봉 후 강한 하락

            if is_bull_ob:
                obs.append({
                    "dir": "long",
                    "low": float(low_5m[i]),
                    "high": float(high_5m[i]),
                    "strength": min(1.0, abs(move) / atr / 3),
                    "age": n - 1 - i,
                })
            elif is_bear_ob:
                obs.append({
                    "dir": "short",
                    "low": float(low_5m[i]),
                    "high": float(high_5m[i]),
                    "strength": min(1.0, abs(move) / atr / 3),
                    "age": n - 1 - i,
                })

        if not obs:
            return {"type": "order_block", "direction": "neutral", "strength": 0.0,
                    "zone": None, "nearby_count": 0}

        # 현재 1m 가격
        price = float(df_1m["close"].iloc[-1])
        prev_price = float(df_1m["close"].iloc[-2]) if len(df_1m) >= 2 else price

        # 가격이 OB 존에 도달 + 반전 확인
        best = None
        for ob in obs:
            in_zone = ob["low"] * 0.999 <= price <= ob["high"] * 1.001

            if not in_zone:
                continue

            # 방향 일치 + 반전 확인
            if ob["dir"] == "long" and price > prev_price:
                if best is None or ob["strength"] > best["strength"]:
                    best = ob
            elif ob["dir"] == "short" and price < prev_price:
                if best is None or ob["strength"] > best["strength"]:
                    best = ob

        if not best:
            return {"type": "order_block", "direction": "neutral", "strength": 0.0,
                    "zone": None, "nearby_count": len(obs)}

        return {
            "type": "order_block",
            "direction": best["dir"],
            "strength": round(best["strength"], 2),
            "zone": [best["low"], best["high"]],
            "age": best["age"],
            "nearby_count": len(obs),
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
            # 정규화: wick / atr 비율로 계산
            atr_proxy = (recent_high - recent_low) / 20  # 평균 봉 크기
            wick_ratio = wick_above / atr_proxy if atr_proxy > 0 else 0
            strength = min(1.0, 0.4 + wick_ratio * 0.3)
            if current_close < current_open:
                strength = min(1.0, strength + 0.2)

        # 하단 스윕: 저점 돌파했다가 종가는 위
        elif current_low < recent_low and current_close > recent_low:
            wick_below = recent_low - current_low
            sweep = "low_swept"
            direction = "long"
            atr_proxy = (recent_high - recent_low) / 20
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
        # 평균회귀
        elif abs(distance_pct) > 1.0:
            direction = "short" if distance_pct > 0 else "long"
            strength = min(0.6, abs(distance_pct) / 2.0)

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

        # 첩 판단 완화: ADX < 18 + 방향 전환 7회+ (하나만 만족하면 첩 아님)
        is_chop = adx < 18 and direction_changes >= 6

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
