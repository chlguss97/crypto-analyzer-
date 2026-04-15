"""
PaperTrader — 가상매매 + 학습 엔진
1) 학습: 점수 5.0+ 시그널에서 가상 진입 (ML 학습 데이터 품질 확보)
2) 미진입 추적(Shadow): neutral/저점수 시그널도 추적하여 "진입 안 한 게 맞았나?" 검증
3) 모든 결과 → DB 기록 + ML 실시간 학습
"""
import logging
import time
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from src.monitoring.trade_logger import _append_jsonl

logger = logging.getLogger(__name__)


@dataclass
class PaperPosition:
    """가상 포지션"""
    trade_id: int
    symbol: str
    direction: str
    grade: str
    score: float
    entry_price: float
    entry_time: int  # ms
    size_usdt: float
    leverage: int
    sl_price: float
    tp1_price: float
    tp2_price: float
    signals_snapshot: dict = field(default_factory=dict)
    mode: str = "swing"
    tp1_hit: bool = False
    use_trailing: bool = False
    best_price: float = 0.0  # 트레일링용 최고/최저가

    def pnl_pct(self, current_price: float) -> float:
        if self.direction == "long":
            return ((current_price - self.entry_price) / self.entry_price) * 100 * self.leverage
        else:
            return ((self.entry_price - current_price) / self.entry_price) * 100 * self.leverage

    def hold_minutes(self) -> float:
        return (time.time() * 1000 - self.entry_time) / 60_000


@dataclass
class ShadowTrack:
    """미진입 시그널 추적 (진입 안 한 게 맞았나 검증)"""
    symbol: str
    direction: str  # 시그널이 제안한 방향
    score: float
    entry_price: float  # 시그널 시점 가격
    entry_time: int
    signals_snapshot: dict = field(default_factory=dict)
    mode: str = "swing"
    best_price: float = 0.0  # 추적 중 최고/최저가
    worst_price: float = 0.0

    def potential_pnl_pct(self) -> float:
        """만약 진입했다면 최대 수익률"""
        if self.direction == "long":
            return ((self.best_price - self.entry_price) / self.entry_price) * 100 * 15
        else:
            return ((self.entry_price - self.best_price) / self.entry_price) * 100 * 15

    def hold_minutes(self) -> float:
        return (time.time() * 1000 - self.entry_time) / 60_000


class PaperTrader:
    """
    가상매매 + 전수학습 + 미진입 추적 엔진
    - 점수 5.0+ 시그널 → 가상 진입 (max 10개 동시, 04-10 품질 상향)
    - neutral/저점수 시그널 → shadow 추적 (30분간)
    - 모든 결과 → DB + ML 피드백
    """

    TIME_EXITS = {
        "swing": [60, 120, 240, 360],
        "scalp": [15, 30, 60],
    }

    FEE_RATE = 0.0005  # 편도

    def __init__(self, db, redis, ml_swing, ml_scalp, regime_detector=None, signal_tracker=None):
        self.db = db
        self.redis = redis
        self.ml_swing = ml_swing
        self.ml_scalp = ml_scalp
        self.regime_detector = regime_detector
        self.signal_tracker = signal_tracker
        self.positions: dict[int, PaperPosition] = {}
        self.shadows: list[ShadowTrack] = []  # 미진입 추적
        self.max_positions = 10  # 전수 학습이므로 넉넉하게
        self.max_shadows = 20
        self._stats = {"total": 0, "wins": 0, "losses": 0,
                       "shadow_total": 0, "shadow_missed": 0, "shadow_correct": 0}

    # ── 전수 학습 진입 (점수 2.0+) ──

    async def try_entry(self, signal_result: dict, mode: str, current_price: float):
        """학습: 점수 5.0+ 시그널에서 가상 진입 (04-10 품질 상향)"""
        direction = signal_result.get("direction", "neutral")
        score = signal_result.get("score", 0)

        # neutral이면 shadow로 추적
        if direction == "neutral":
            self._add_shadow(signal_result, mode, current_price)
            return None

        # 통합 모델: setup 있으면 진입 허용, 없으면 shadow
        if mode == "unified":
            if not signal_result.get("setup"):
                self._add_shadow(signal_result, mode, current_price)
                return None
        # 레거시: 점수 5.0 미만 shadow
        elif score < 5.0:
            self._add_shadow(signal_result, mode, current_price)
            return None

        # 동시 포지션 제한
        if len(self.positions) >= self.max_positions:
            return None

        # 같은 모드+방향 중복 방지
        for pos in self.positions.values():
            if pos.direction == direction and pos.mode == mode:
                return None

        # SL/TP 계산
        atr_pct = signal_result.get("atr_pct", 0.3)
        sl_distance = current_price * atr_pct / 100 * 1.2

        if mode == "unified":
            leverage = 15
            sl_distance = signal_result.get("sl_distance", current_price * 0.004)
            tp1_dist = signal_result.get("tp_distance", sl_distance * 1.5)
            tp2_dist = tp1_dist * 2.0
            sl_distance = max(sl_distance, current_price * 0.0035)
        elif mode == "swing":
            leverage = self._calc_leverage(score, atr_pct)
            tp1_dist = sl_distance * 1.5
            tp2_dist = sl_distance * 2.5
        else:
            leverage = 25
            sl_distance = signal_result.get("sl_distance", current_price * 0.002)
            tp1_dist = signal_result.get("tp_distance", sl_distance * 1.5)
            tp2_dist = tp1_dist * 1.5

        if direction == "long":
            sl = current_price - sl_distance
            tp1 = current_price + tp1_dist
            tp2 = current_price + tp2_dist
        else:
            sl = current_price + sl_distance
            tp1 = current_price - tp1_dist
            tp2 = current_price - tp2_dist

        size_usdt = 100.0

        # 점수 구간별 grade 라벨
        if score >= 6.0:
            grade_label = f"PAPER_{mode.upper()}"
        elif score >= 4.0:
            grade_label = f"PAPER_{mode.upper()}_MID"
        else:
            grade_label = f"PAPER_{mode.upper()}_LOW"

        entry_time = int(time.time() * 1000)
        signals_data = signal_result.get("signals_detail", signal_result.get("signals", {}))

        signals_json = json.dumps(signals_data, default=str)

        trade_id = await self.db.insert_trade({
            "symbol": "BTC-USDT-SWAP",
            "direction": direction,
            "grade": grade_label,
            "score": score,
            "entry_price": current_price,
            "entry_time": entry_time,
            "leverage": leverage,
            "position_size": size_usdt,
            "signals_snapshot": signals_json,
        })

        use_trailing = signal_result.get("use_trailing", False)

        pos = PaperPosition(
            trade_id=trade_id, symbol="BTC-USDT-SWAP",
            direction=direction, grade=grade_label, score=score,
            entry_price=current_price, entry_time=entry_time,
            size_usdt=size_usdt, leverage=leverage,
            sl_price=round(sl, 1), tp1_price=round(tp1, 1), tp2_price=round(tp2, 1),
            signals_snapshot=signals_data, mode=mode,
            use_trailing=use_trailing, best_price=current_price,
        )

        self.positions[trade_id] = pos
        logger.info(
            f"[PAPER-{mode.upper()}] {direction.upper()} 진입 @ ${current_price:.0f} "
            f"SL ${sl:.0f} TP ${tp1:.0f} 점수 {score:.1f} [{grade_label}] (ID:{trade_id})"
        )
        return pos

    # ── 미진입 추적 (Shadow) ──

    def _add_shadow(self, signal_result: dict, mode: str, current_price: float):
        """진입 안 한 시그널을 shadow로 추적"""
        if len(self.shadows) >= self.max_shadows:
            return

        direction = signal_result.get("direction", "neutral")
        if direction == "neutral":
            # neutral도 양방향 추적
            for d in ["long", "short"]:
                self.shadows.append(ShadowTrack(
                    symbol="BTC-USDT-SWAP", direction=d,
                    score=signal_result.get("score", 0),
                    entry_price=current_price,
                    entry_time=int(time.time() * 1000),
                    signals_snapshot=signal_result.get("signals_detail", signal_result.get("signals", {})),
                    mode=mode, best_price=current_price, worst_price=current_price,
                ))
        else:
            self.shadows.append(ShadowTrack(
                symbol="BTC-USDT-SWAP", direction=direction,
                score=signal_result.get("score", 0),
                entry_price=current_price,
                entry_time=int(time.time() * 1000),
                signals_snapshot=signal_result.get("signals_detail", signal_result.get("signals", {})),
                mode=mode, best_price=current_price, worst_price=current_price,
            ))

    async def check_shadows(self, current_price: float):
        """shadow 추적 업데이트 + 30분 후 결과 평가"""
        # 안전: 현재 shadows의 스냅샷으로 작업
        snapshot = list(self.shadows)
        expired_objs = []

        for s in snapshot:
            # 가격 갱신
            if s.direction == "long":
                s.best_price = max(s.best_price, current_price)
                s.worst_price = min(s.worst_price, current_price)
            else:
                s.best_price = min(s.best_price, current_price)
                s.worst_price = max(s.worst_price, current_price)

            # 30분 경과 → 결과 평가
            if s.hold_minutes() >= 30:
                expired_objs.append(s)
                await self._evaluate_shadow(s, current_price)

        # 객체 기반 제거 (인덱스 변경 안전)
        for s in expired_objs:
            if s in self.shadows:
                self.shadows.remove(s)

    async def _evaluate_shadow(self, s: ShadowTrack, current_price: float):
        """미진입 시그널 결과 평가 → ML에 역학습"""
        potential_pnl = s.potential_pnl_pct()
        ml = self.ml_swing if s.mode == "swing" else self.ml_scalp

        self._stats["shadow_total"] += 1

        if potential_pnl > 1.0:
            # 진입 안 했는데 수익이었을 시그널 → "놓친 기회"
            # ML에 긍정 결과로 기록 (이런 시그널은 진입했어야 함)
            self._stats["shadow_missed"] += 1
            regime = "ranging"
            if self.regime_detector and self.regime_detector._regime_history:
                regime = self.regime_detector._regime_history[-1]
            meta = {"atr_pct": 0.3, "hour": datetime.now(timezone.utc).hour, "shadow": True, "regime": regime}
            shadow_fee = self.FEE_RATE * 2 * 15 * 100  # 추정 15x
            ml.record_trade(s.signals_snapshot, meta, potential_pnl * 0.5, fee_pct=shadow_fee)
            logger.info(
                f"[SHADOW-{s.mode.upper()}] 놓친 기회! {s.direction.upper()} "
                f"잠재 PnL: {potential_pnl:+.1f}% 점수 {s.score:.1f}"
            )
        elif potential_pnl < -1.0:
            # 진입 안 해서 손실 회피 → "올바른 거부"
            self._stats["shadow_correct"] += 1
            regime = "ranging"
            if self.regime_detector and self.regime_detector._regime_history:
                regime = self.regime_detector._regime_history[-1]
            meta = {"atr_pct": 0.3, "hour": datetime.now(timezone.utc).hour, "shadow": True, "regime": regime}
            shadow_fee = self.FEE_RATE * 2 * 15 * 100
            ml.record_trade(s.signals_snapshot, meta, potential_pnl * 0.3, fee_pct=shadow_fee)
            logger.debug(f"[SHADOW] 올바른 거부: {s.direction} PnL {potential_pnl:+.1f}%")

    # ── 포지션 체크 + 청산 ──

    async def check_positions(self, current_price: float):
        """모든 가상 포지션 SL/TP/시간 체크 + shadow 업데이트"""
        closed = []
        for tid, pos in list(self.positions.items()):
            reason = self._check_exit(pos, current_price)
            if reason:
                await self._close_position(pos, current_price, reason)
                closed.append(tid)

        for tid in closed:
            del self.positions[tid]

        # shadow도 체크
        await self.check_shadows(current_price)

    def _check_exit(self, pos: PaperPosition, price: float) -> str | None:
        """청산 조건 체크 (트레일링 스탑 지원)"""
        if pos.direction == "long" and price <= pos.sl_price:
            return "sl_hit"
        if pos.direction == "short" and price >= pos.sl_price:
            return "sl_hit"

        # 트레일링 스탑 (급변동 모드)
        if pos.use_trailing:
            if pos.direction == "long":
                if price > pos.best_price:
                    pos.best_price = price
                    # 최고가 대비 0.3% 하락하면 청산
                    pos.sl_price = max(pos.sl_price, price * 0.997)
                if price >= pos.tp1_price:
                    pos.sl_price = max(pos.sl_price, pos.entry_price)  # 최소 손익분기
            else:
                if price < pos.best_price or pos.best_price == 0:
                    pos.best_price = price
                    pos.sl_price = min(pos.sl_price, price * 1.003)
                if price <= pos.tp1_price:
                    pos.sl_price = min(pos.sl_price, pos.entry_price)
            # 트레일링 모드는 TP2 없이 트레일링으로 나감
            return None

        if not pos.tp1_hit:
            if pos.direction == "long" and price >= pos.tp1_price:
                pos.tp1_hit = True
                pos.sl_price = pos.entry_price
            if pos.direction == "short" and price <= pos.tp1_price:
                pos.tp1_hit = True
                pos.sl_price = pos.entry_price

        if pos.direction == "long" and price >= pos.tp2_price:
            return "tp2_hit"
        if pos.direction == "short" and price <= pos.tp2_price:
            return "tp2_hit"

        hold_min = pos.hold_minutes()
        time_limits = self.TIME_EXITS.get(pos.mode, [360])
        max_time = time_limits[-1]

        if hold_min >= max_time:
            return "time_exit"

        for t in time_limits[:-1]:
            if hold_min >= t:
                pnl = pos.pnl_pct(price)
                if pnl <= 0:
                    return f"time_exit_{t}m"

        return None

    async def _close_position(self, pos: PaperPosition, exit_price: float, reason: str):
        """가상 포지션 청산 → DB 업데이트 + ML 학습"""
        raw_pnl_pct = pos.pnl_pct(exit_price)
        fee_pct = self.FEE_RATE * 2 * pos.leverage * 100
        net_pnl_pct = raw_pnl_pct - fee_pct
        pnl_usdt = pos.size_usdt * net_pnl_pct / 100
        exit_time = int(time.time() * 1000)

        await self.db.update_trade_exit(pos.trade_id, {
            "exit_price": exit_price,
            "exit_time": exit_time,
            "exit_reason": f"paper_{reason}",
            "pnl_usdt": round(pnl_usdt, 4),
            "pnl_pct": round(net_pnl_pct, 4),
            "fee_total": round(pos.size_usdt * self.FEE_RATE * 2, 4),
            "funding_cost": 0,
        })

        ml = self.ml_swing if pos.mode == "swing" else self.ml_scalp
        regime = "ranging"
        if self.regime_detector and getattr(self.regime_detector, "_regime_history", None):
            history = self.regime_detector._regime_history
            if len(history) > 0:
                regime = history[-1]
        meta = {"atr_pct": 0.3, "hour": datetime.now(timezone.utc).hour, "regime": regime}
        ml.record_trade(pos.signals_snapshot, meta, net_pnl_pct, fee_pct=fee_pct)

        # 시그널 기여도 추적
        if self.signal_tracker:
            self.signal_tracker.record_trade(
                pos.signals_snapshot, net_pnl_pct, mode=pos.mode, regime=regime
            )

        self._stats["total"] += 1
        if net_pnl_pct > 0:
            self._stats["wins"] += 1
        else:
            self._stats["losses"] += 1

        win_rate = self._stats["wins"] / max(self._stats["total"], 1) * 100

        logger.info(
            f"[PAPER-{pos.mode.upper()}] {pos.direction.upper()} 청산 @ ${exit_price:.0f} "
            f"사유: {reason} | PnL: {net_pnl_pct:+.2f}% (${pnl_usdt:+.2f}) | "
            f"누적 {self._stats['total']}건 승률 {win_rate:.0f}% | "
            f"Shadow 놓침:{self._stats['shadow_missed']} 올바름:{self._stats['shadow_correct']}"
        )

        # JSONL 영구 기록 — Claude 가 logs 브랜치에서 직접 분석
        _append_jsonl({
            "type": "paper_exit",
            "paper": True,
            "mode": pos.mode,
            "direction": pos.direction,
            "exit_reason": reason,
            "entry_price": round(pos.entry_price, 1),
            "exit_price": round(exit_price, 1),
            "pnl_pct": round(net_pnl_pct, 2),
            "pnl_usdt": round(pnl_usdt, 2),
            "score": round(pos.score, 2),
            "grade": pos.grade,
            "hold_min": round((exit_time / 1000 - pos.entry_time / 1000) / 60),
            "regime": regime,
            "stats_total": self._stats["total"],
            "stats_win_rate": round(win_rate, 1),
            "shadow_missed": self._stats["shadow_missed"],
            "shadow_correct": self._stats["shadow_correct"],
        })

        if self._stats["total"] % 10 == 0:
            logger.info(
                f"[ML] Swing: {'TRAINED' if self.ml_swing.is_trained else 'LEARNING'} "
                f"({len(self.ml_swing.X_buffer)}) | "
                f"Scalp: {'TRAINED' if self.ml_scalp.is_trained else 'LEARNING'} "
                f"({len(self.ml_scalp.X_buffer)})"
            )

    # ── 유틸 ──

    def _calc_leverage(self, score: float, atr_pct: float) -> int:
        if score >= 9.0:
            base = 30
        elif score >= 8.0:
            base = 25
        elif score >= 7.5:
            base = 20
        elif score >= 6.5:
            base = 15
        else:
            base = 10

        if atr_pct > 0.5:
            base = int(base * 0.7)
        elif atr_pct > 0.3:
            base = int(base * 0.85)

        return max(10, min(base, 30))

    def get_stats(self) -> dict:
        return {
            "total_trades": self._stats["total"],
            "wins": self._stats["wins"],
            "losses": self._stats["losses"],
            "win_rate": self._stats["wins"] / max(self._stats["total"], 1) * 100,
            "active_positions": len(self.positions),
            "active_shadows": len(self.shadows),
            "shadow_missed": self._stats["shadow_missed"],
            "shadow_correct": self._stats["shadow_correct"],
        }
