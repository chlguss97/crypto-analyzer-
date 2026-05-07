"""
AdaptiveParams — 거래 결과 기반 수치 자동 보정 엔진
SPEC v2 §10 구현

Phase 0~30건: 수집만 (보정 없음)
Phase 30~100건: Direction + EntryQuality 활성
Phase 100~300건: + TP/SL Calibrator
Phase 300건+: 전체 활성
"""

import json
import logging
import time
from collections import defaultdict

logger = logging.getLogger(__name__)


class _EMATracker:
    """지수 이동 평균 추적기 (최근 데이터 가중)"""

    def __init__(self, span: int = 20):
        self.span = span
        self.values: list[float] = []

    def add(self, value: float):
        self.values.append(value)
        if len(self.values) > self.span * 3:
            self.values = self.values[-self.span * 2:]

    def mean(self) -> float:
        if not self.values:
            return 0.0
        if len(self.values) <= self.span:
            return sum(self.values) / len(self.values)
        # EMA
        mult = 2 / (self.span + 1)
        ema = sum(self.values[:self.span]) / self.span
        for v in self.values[self.span:]:
            ema = v * mult + ema * (1 - mult)
        return ema

    @property
    def count(self) -> int:
        return len(self.values)


class EntryQualityScorer:
    """진입 품질 추적 — time_to_first_profit, 초기 이동 속도"""

    def __init__(self):
        self.time_to_profit = _EMATracker(20)
        self.no_profit_count = 0  # 수익 전환 없이 SL 맞은 횟수

    def update(self, result: dict):
        ttp = result.get("time_to_first_profit_sec", 0)
        if ttp > 0:
            self.time_to_profit.add(ttp)
        else:
            self.no_profit_count += 1

    def get_quality_warning(self) -> str | None:
        if self.time_to_profit.count < 10:
            return None
        avg = self.time_to_profit.mean()
        if avg > 1800:  # 30분
            return f"진입 타이밍 늦음 (avg {avg/60:.0f}분)"
        return None


class DirectionScorer:
    """방향별 기대값(EV) 추적 — 계층적 폴백"""

    def __init__(self):
        # key: (h1_trend, h4_trend, direction) → [pnl_pct, ...]
        self.results: dict[tuple, list[float]] = defaultdict(list)

    def update(self, result: dict):
        h1 = result.get("entry_h1_trend", "unknown")
        h4 = result.get("entry_h4_trend", "unknown")
        d = result.get("direction", "unknown")
        pnl = result.get("pnl_pct", 0)

        # Level 3: regime + htf + direction
        self.results[(h1, h4, d)].append(pnl)
        # Level 2: htf + direction (1h만)
        self.results[("*", "*", d, h1)].append(pnl)
        # Level 1: direction만
        self.results[("*", "*", d)].append(pnl)

    def get_size_mult(self, direction: str, h1_trend: str, h4_trend: str) -> float:
        """EV 기반 사이즈 배수. 0=차단, 0.5=축소, 1.0=정상, 1.2=확대"""
        # Level 3 시도
        key3 = (h1_trend, h4_trend, direction)
        if len(self.results.get(key3, [])) >= 10:
            ev = self._calc_ev(self.results[key3])
            return self._ev_to_mult(ev)

        # Level 2 폴백 (1h + direction)
        key2 = ("*", "*", direction, h1_trend)
        if len(self.results.get(key2, [])) >= 10:
            ev = self._calc_ev(self.results[key2])
            return self._ev_to_mult(ev)

        # Level 1 폴백 (direction만)
        key1 = ("*", "*", direction)
        if len(self.results.get(key1, [])) >= 10:
            ev = self._calc_ev(self.results[key1])
            return self._ev_to_mult(ev)

        return 1.0  # 데이터 부족 → 기본값

    def _calc_ev(self, pnls: list[float]) -> float:
        if not pnls:
            return 0.0
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        wr = len(wins) / len(pnls) if pnls else 0
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0
        return wr * avg_win - (1 - wr) * avg_loss

    def _ev_to_mult(self, ev: float) -> float:
        if ev < -1.0:
            return 0.0   # 차단
        elif ev < 0:
            return 0.5   # 축소
        elif ev > 2.0:
            return 1.2   # 확대
        return 1.0


class TPCalibrator:
    """TP1 ATR 배수 자동 보정 — reach% 기반"""

    def __init__(self, initial_mult: float = 1.5):
        self.current_mult = initial_mult
        self.reach_pcts: dict[str, _EMATracker] = {
            "trending": _EMATracker(20),
            "ranging": _EMATracker(20),
            "other": _EMATracker(20),
        }
        self.overshoot = _EMATracker(20)  # 승리 시 TP 초과 도달%

    def update(self, result: dict):
        reach = result.get("tp1_reach_pct", 0)
        regime = result.get("regime", "other")
        bucket = "trending" if "trending" in regime else ("ranging" if regime == "ranging" else "other")
        pnl = result.get("pnl_pct", 0)

        if pnl <= 0:
            self.reach_pcts[bucket].add(reach)
        else:
            self.overshoot.add(reach - 100)

    def get_mult(self, regime: str = "other") -> float:
        """현재 보정된 ATR 배수"""
        bucket = "trending" if "trending" in regime else ("ranging" if regime == "ranging" else "other")
        tracker = self.reach_pcts[bucket]

        if tracker.count < 10:
            return self.current_mult

        # 패배 near_miss 비율
        recent = tracker.values[-20:]
        near_miss = sum(1 for r in recent if 60 <= r < 100) / len(recent)

        # 승리 overshoot
        avg_over = self.overshoot.mean() if self.overshoot.count >= 5 else 0

        new_mult = self.current_mult
        if near_miss > 0.4:
            new_mult *= 0.95
        elif avg_over > 80:
            new_mult *= 1.05

        new_mult = max(0.8, min(2.5, new_mult))

        if abs(new_mult - self.current_mult) > 0.001:
            logger.info(f"[ADAPTIVE] TP mult: {self.current_mult:.3f} -> {new_mult:.3f} "
                        f"(near_miss={near_miss:.0%}, overshoot={avg_over:.0f}%)")
            self.current_mult = new_mult

        return self.current_mult


class SLCalibrator:
    """SL 거리 자동 보정 — MAE(최대 역행) 기반"""

    def __init__(self, initial_pct: float = 5.0):
        self.current_pct = initial_pct
        self.winning_mae_atr = _EMATracker(20)  # 승리 거래의 MAE (ATR 배수)
        self.losing_mae_atr = _EMATracker(20)   # 패배 거래의 MAE

    def update(self, result: dict):
        mae_pct = result.get("mae_pct", 0)
        atr = result.get("entry_atr", 0.3)
        mae_atr = mae_pct / atr if atr > 0 else 0
        pnl = result.get("pnl_pct", 0)

        if pnl > 0:
            self.winning_mae_atr.add(mae_atr)
        else:
            self.losing_mae_atr.add(mae_atr)

    def get_sl_margin_pct(self, leverage: int = 15) -> float:
        if self.winning_mae_atr.count < 10:
            return self.current_pct

        # 승리 MAE의 95번째 백분위 (근사)
        sorted_mae = sorted(self.winning_mae_atr.values[-20:])
        idx = int(len(sorted_mae) * 0.95)
        mae_95 = sorted_mae[min(idx, len(sorted_mae) - 1)]

        # SL = MAE_95 × 1.2 × leverage (마진% 단위)
        optimal_pct = mae_95 * 1.2 * leverage / 100 * leverage
        # 실제로는: optimal_sl_price_pct = mae_95_atr × 1.2 × atr → margin_pct = price_pct × leverage
        # 단순화: optimal_margin_pct = mae_95 × 1.2
        optimal_pct = mae_95 * 1.2

        new_pct = max(3.0, min(8.0, optimal_pct))

        if abs(new_pct - self.current_pct) > 0.1:
            logger.info(f"[ADAPTIVE] SL margin: {self.current_pct:.1f}% -> {new_pct:.1f}% "
                        f"(mae_95={mae_95:.2f}x ATR)")
            self.current_pct = new_pct

        return self.current_pct


class HoldOptimizer:
    """보유시간 vs 승률 분석"""

    def __init__(self):
        self.buckets: dict[str, list[float]] = {
            "0_30": [], "30_120": [], "120_plus": []
        }

    def update(self, result: dict):
        hold = result.get("hold_min", 0)
        pnl = result.get("pnl_pct", 0)
        label = 1 if pnl > 0 else 0

        if hold < 30:
            self.buckets["0_30"].append(label)
        elif hold < 120:
            self.buckets["30_120"].append(label)
        else:
            self.buckets["120_plus"].append(label)

    def get_win_rates(self) -> dict:
        rates = {}
        for k, v in self.buckets.items():
            if len(v) >= 5:
                rates[k] = sum(v) / len(v) * 100
        return rates

    def should_tighten_trail(self, hold_min: float, pnl_pct: float) -> bool:
        if len(self.buckets["120_plus"]) < 10:
            return False
        wr_120 = sum(self.buckets["120_plus"]) / len(self.buckets["120_plus"])
        return hold_min > 120 and pnl_pct < 0 and wr_120 < 0.25


class RegimeScorer:
    """레짐별 EV 추적"""

    def __init__(self):
        self.results: dict[str, list[float]] = defaultdict(list)

    def update(self, result: dict):
        regime = result.get("regime", "unknown")
        pnl = result.get("pnl_pct", 0)
        self.results[regime].append(pnl)

    def get_size_mult(self, regime: str) -> float:
        pnls = self.results.get(regime, [])
        if len(pnls) < 10:
            return 1.0
        ev = sum(pnls[-20:]) / len(pnls[-20:])
        if ev < -1.0:
            return 0.5
        elif ev > 1.0:
            return 1.2
        return 1.0


class TimeOfDayTracker:
    """UTC 4시간 구간별 EV"""

    def __init__(self):
        self.results: dict[int, list[float]] = defaultdict(list)

    def update(self, result: dict):
        entry_ts = result.get("entry_ts", 0)
        if entry_ts > 0:
            hour = time.gmtime(entry_ts).tm_hour
            bucket = hour // 4  # 0~5 (6 buckets)
            self.results[bucket].append(result.get("pnl_pct", 0))

    def get_size_mult(self) -> float:
        hour = time.gmtime().tm_hour
        bucket = hour // 4
        pnls = self.results.get(bucket, [])
        if len(pnls) < 10:
            return 1.0
        ev = sum(pnls[-20:]) / len(pnls[-20:])
        if ev < -1.0:
            return 0.5
        return 1.0


class AdaptiveParams:
    """거래 결과 기반 수치 자동 보정 엔진"""

    MIN_TRADES_PHASE1 = 30   # Direction + EntryQuality 활성
    MIN_TRADES_PHASE2 = 100  # + TP/SL Calibrator
    MIN_TRADES_PHASE3 = 300  # 전체 활성

    def __init__(self, config: dict = None, redis=None):
        self.config = config or {}
        self.redis = redis
        self.total_trades = 0

        self.entry_quality = EntryQualityScorer()
        self.direction = DirectionScorer()
        self.tp_cal = TPCalibrator(initial_mult=1.5)
        self.sl_cal = SLCalibrator(initial_pct=5.0)
        self.hold_opt = HoldOptimizer()
        self.regime = RegimeScorer()
        self.time_of_day = TimeOfDayTracker()

    async def record_trade(self, result: dict):
        """거래 종료 시 호출 — 모든 하위 모듈에 결과 전달"""
        self.total_trades += 1
        self.entry_quality.update(result)
        self.direction.update(result)
        self.tp_cal.update(result)
        self.sl_cal.update(result)
        self.hold_opt.update(result)
        self.regime.update(result)
        self.time_of_day.update(result)

        # JSONL 로깅
        from src.monitoring.trade_logger import _append_jsonl
        _append_jsonl({
            "type": "adaptive_update",
            "total_trades": self.total_trades,
            "tp_mult": round(self.tp_cal.current_mult, 3),
            "sl_pct": round(self.sl_cal.current_pct, 1),
            "hold_rates": self.hold_opt.get_win_rates(),
            "entry_warning": self.entry_quality.get_quality_warning(),
        })

        # Redis 저장
        if self.redis:
            await self._save_state()

    def get_tp_mult(self, regime: str = "other") -> float:
        """TP1 ATR 배수 (Phase 2+에서 보정)"""
        if self.total_trades < self.MIN_TRADES_PHASE2:
            return 1.5  # 기본값
        return self.tp_cal.get_mult(regime)

    def get_sl_margin_pct(self) -> float:
        """SL 마진% (Phase 2+에서 보정)"""
        if self.total_trades < self.MIN_TRADES_PHASE2:
            return 5.0  # 기본값
        return self.sl_cal.get_sl_margin_pct()

    def get_entry_size_mult(self, direction: str, h1_trend: str, h4_trend: str,
                            regime: str) -> float:
        """진입 사이즈 배수 (0=차단). Phase 1+에서 활성."""
        if self.total_trades < self.MIN_TRADES_PHASE1:
            return 1.0  # 기본값 (기존 하드코딩 게이트가 처리)

        d_mult = self.direction.get_size_mult(direction, h1_trend, h4_trend)
        r_mult = self.regime.get_size_mult(regime)
        t_mult = self.time_of_day.get_size_mult()

        combined = d_mult * r_mult * t_mult
        return max(0.0, min(1.5, combined))

    def should_tighten_trail(self, hold_min: float, pnl_pct: float) -> bool:
        """보유시간 기반 트레일 축소 (Phase 3+)"""
        if self.total_trades < self.MIN_TRADES_PHASE3:
            return False
        return self.hold_opt.should_tighten_trail(hold_min, pnl_pct)

    def get_stats(self) -> dict:
        """대시보드/텔레그램용 상태"""
        return {
            "total_trades": self.total_trades,
            "phase": ("collect" if self.total_trades < self.MIN_TRADES_PHASE1
                      else "phase1" if self.total_trades < self.MIN_TRADES_PHASE2
                      else "phase2" if self.total_trades < self.MIN_TRADES_PHASE3
                      else "full"),
            "tp_mult": round(self.tp_cal.current_mult, 3),
            "sl_pct": round(self.sl_cal.current_pct, 1),
            "entry_warning": self.entry_quality.get_quality_warning(),
            "hold_win_rates": self.hold_opt.get_win_rates(),
        }

    async def _save_state(self):
        """Redis에 상태 저장"""
        try:
            state = {
                "total_trades": self.total_trades,
                "tp_mult": self.tp_cal.current_mult,
                "sl_pct": self.sl_cal.current_pct,
                "tp_reach_trending": self.tp_cal.reach_pcts["trending"].values[-50:],
                "tp_reach_ranging": self.tp_cal.reach_pcts["ranging"].values[-50:],
                "winning_mae": self.sl_cal.winning_mae_atr.values[-50:],
                "direction_results": {str(k): v[-50:] for k, v in self.direction.results.items()},
                "regime_results": {k: v[-50:] for k, v in self.regime.results.items()},
            }
            await self.redis.set("adaptive:state", json.dumps(state), ttl=86400 * 30)
        except Exception as e:
            logger.debug(f"adaptive state save 실패: {e}")

    async def load_state(self):
        """Redis에서 상태 복원"""
        if not self.redis:
            return
        try:
            raw = await self.redis.get("adaptive:state")
            if not raw:
                return
            state = json.loads(raw)
            self.total_trades = state.get("total_trades", 0)
            self.tp_cal.current_mult = state.get("tp_mult", 1.5)
            self.sl_cal.current_pct = state.get("sl_pct", 5.0)

            for v in state.get("tp_reach_trending", []):
                self.tp_cal.reach_pcts["trending"].add(v)
            for v in state.get("tp_reach_ranging", []):
                self.tp_cal.reach_pcts["ranging"].add(v)
            for v in state.get("winning_mae", []):
                self.sl_cal.winning_mae_atr.add(v)

            for k, vals in state.get("direction_results", {}).items():
                key = eval(k) if k.startswith("(") else k
                self.direction.results[key] = vals
            for k, vals in state.get("regime_results", {}).items():
                self.regime.results[k] = vals

            logger.info(f"[ADAPTIVE] 상태 복원: {self.total_trades}건, "
                        f"tp_mult={self.tp_cal.current_mult:.3f}, sl_pct={self.sl_cal.current_pct:.1f}")
        except Exception as e:
            logger.warning(f"adaptive state 복원 실패: {e}")
