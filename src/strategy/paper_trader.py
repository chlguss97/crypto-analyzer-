"""
PaperTrader v2 — 독립 가상 계좌 매매 엔진
- 가상 잔고(1000만원 ≈ $7,500)로 시작, 실전과 동일한 룰 적용
- 사이징: margin_loss_cap (settings.yaml 동일)
- 리스크: 일일 손실한도, 연패 쿨다운, 최대 포지션 수
- 에퀴티 커브 추적 → DB + JSONL 기록
- 실전 코드 경로와 완전 독립 (executor 호출 없음)
"""
import logging
import math
import time
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from src.monitoring.trade_logger import _append_jsonl
from src.utils.helpers import load_config

logger = logging.getLogger(__name__)

# 초기 가상 잔고 (1000만원 ≈ $7,500)
INITIAL_BALANCE_USDT = 7500.0

# 리스크 한도
MAX_DAILY_LOSS_PCT = 10.0     # 일일 -10%
COOLDOWN_AFTER_LOSS_SEC = 180  # 손절 후 3분
COOLDOWN_AFTER_WIN_SEC = 60    # 익절 후 1분
MAX_SAME_DIR = 6               # 같은 방향 연속 최대 6회 (데이터 축적)

# 점수→등급 매핑
SCORE_GRADE_MAP = [
    (9.0, "A+"),
    (8.0, "A"),
    (7.0, "B+"),
    (6.0, "B"),
    (0.0, "B-"),
]

# 등급→최대 레버리지
GRADE_MAX_LEVERAGE = {
    "A+": 20, "A": 20, "B+": 20, "B": 15, "B-": 15,
}

# 연패→레버리지 배율
STREAK_MULTIPLIER = {0: 1.0, 1: 0.8, 2: 0.6, 3: 0.4, 4: 0.3}


@dataclass
class PaperPosition:
    """가상 포지션 — 실전 PositionManager와 동일한 필드"""
    trade_id: int
    symbol: str
    direction: str
    grade: str
    score: float
    entry_price: float
    entry_time: float          # timestamp (seconds)
    margin: float              # USDT 마진
    size_btc: float            # BTC 수량
    leverage: int
    sl_price: float
    tp1_price: float
    tp2_price: float
    tp3_price: float
    hold_mode: str = "standard"
    signals_snapshot: dict = field(default_factory=dict)
    flow_result: dict = field(default_factory=dict)  # FlowML 학습용 원본
    setup: str = "FLOW"
    tp1_hit: bool = False
    tp2_hit: bool = False
    best_price: float = 0.0    # 트레일링용

    def unrealized_pnl_pct(self, current_price: float) -> float:
        """미실현 마진 수익률 %"""
        if self.direction == "long":
            return ((current_price - self.entry_price) / self.entry_price) * 100 * self.leverage
        else:
            return ((self.entry_price - current_price) / self.entry_price) * 100 * self.leverage

    def unrealized_pnl_usdt(self, current_price: float) -> float:
        """미실현 손익 USDT"""
        return self.margin * self.unrealized_pnl_pct(current_price) / 100

    def hold_minutes(self) -> float:
        return (time.time() - self.entry_time) / 60


@dataclass
class ShadowTrack:
    """미진입 시그널 추적 (진입 안 한 게 맞았나 검증)"""
    direction: str
    score: float
    entry_price: float
    entry_time: float
    signals_snapshot: dict = field(default_factory=dict)
    flow_result: dict = field(default_factory=dict)  # FlowML 학습용 원본
    best_price: float = 0.0
    worst_price: float = 0.0

    def potential_pnl_pct(self, leverage: int = 15) -> float:
        if self.direction == "long":
            return ((self.best_price - self.entry_price) / self.entry_price) * 100 * leverage
        else:
            return ((self.entry_price - self.best_price) / self.entry_price) * 100 * leverage

    def hold_minutes(self) -> float:
        return (time.time() - self.entry_time) / 60


class PaperTrader:
    """
    독립 가상 계좌 매매 엔진
    - 실전과 동일한 사이징/SL/TP/리스크 룰
    - 자체 잔고 관리 (실전 executor와 무관)
    - 모든 결과 → DB + JSONL + ML 학습
    """

    FEE_MAKER = 0.0002   # 0.02%
    FEE_TAKER = 0.0005   # 0.05%

    def __init__(self, db, redis, flow_ml=None, regime_detector=None,
                 signal_tracker=None, setup_tracker=None):
        self.db = db
        self.redis = redis
        self.flow_ml = flow_ml
        self.regime_detector = regime_detector
        self.signal_tracker = signal_tracker
        self.setup_tracker = setup_tracker
        self.config = load_config()
        self.risk_cfg = self.config.get("risk", {})

        # ── 가상 계좌 ──
        self.initial_balance = INITIAL_BALANCE_USDT
        self.balance = INITIAL_BALANCE_USDT
        self.peak_balance = INITIAL_BALANCE_USDT

        # ── 포지션/추적 ──
        self.positions: dict[int, PaperPosition] = {}
        self.shadows: list[ShadowTrack] = []
        self.max_positions = self.risk_cfg.get("max_positions", 1)
        self.max_shadows = 30

        # ── 리스크 상태 ──
        self._daily_pnl_usdt = 0.0
        self._loss_streak = 0           # 연패 카운트 (win까지 유지, 일일 리셋 안 함)
        self._cooldown_until = 0.0
        self._last_trade_time = 0.0
        self._last_dir = None
        self._same_dir_count = 0
        self._current_day = 0

        # ── 통계 ──
        self._stats = {
            "total": 0, "wins": 0, "losses": 0,
            "total_pnl_usdt": 0.0,
            "best_trade_pct": 0.0, "worst_trade_pct": 0.0,
            "shadow_total": 0, "shadow_missed": 0, "shadow_correct": 0,
        }

        # ── 에퀴티 히스토리 (1시간마다 스냅샷) ──
        self._equity_history: list[dict] = []
        self._last_equity_snap = 0.0

    async def restore_from_db(self):
        """DB의 과거 페이퍼 매매 기록에서 잔고/통계 복원 (재시작 시 호출)"""
        try:
            cursor = await self.db._db.execute(
                """SELECT pnl_usdt, pnl_pct, direction
                   FROM trades
                   WHERE grade LIKE 'PAPER_%' AND exit_time IS NOT NULL
                   ORDER BY exit_time ASC"""
            )
            rows = await cursor.fetchall()
            if not rows:
                logger.info("[PAPER] 복원할 과거 매매 없음 — 초기 잔고 유지")
                return

            total_pnl = 0.0
            wins = 0
            losses = 0
            best_pct = 0.0
            worst_pct = 0.0
            streak = 0
            peak = self.initial_balance

            for row in rows:
                pnl_usdt = row[0] or 0
                pnl_pct = row[1] or 0
                total_pnl += pnl_usdt

                current_bal = self.initial_balance + total_pnl
                if current_bal > peak:
                    peak = current_bal

                if pnl_pct > 0:
                    wins += 1
                    streak = 0
                elif pnl_pct < 0:
                    losses += 1
                    streak += 1

                best_pct = max(best_pct, pnl_pct)
                worst_pct = min(worst_pct, pnl_pct)

            self.balance = max(0, self.initial_balance + total_pnl)
            self.peak_balance = max(peak, self.balance)
            self._loss_streak = streak
            self._stats["total"] = wins + losses
            self._stats["wins"] = wins
            self._stats["losses"] = losses
            self._stats["total_pnl_usdt"] = round(total_pnl, 2)
            self._stats["best_trade_pct"] = round(best_pct, 2)
            self._stats["worst_trade_pct"] = round(worst_pct, 2)

            total_return = (self.balance - self.initial_balance) / self.initial_balance * 100
            win_rate = wins / max(wins + losses, 1) * 100
            logger.info(
                f"[PAPER] DB 복원 완료: {wins + losses}건 | "
                f"잔고 ${self.balance:,.0f} ({total_return:+.1f}%) | "
                f"승률 {win_rate:.0f}% | 연패 {streak}"
            )

            # Redis에도 즉시 반영
            await self._update_redis_state()

        except Exception as e:
            logger.error(f"[PAPER] DB 복원 실패: {e}")

    # ══════════════════════════════════════════
    #  진입 판단
    # ══════════════════════════════════════════

    async def try_entry(self, signal_result: dict, mode: str, current_price: float):
        """시그널 평가 → 가상 진입 or shadow 추적"""
        now = time.time()

        # 가격 방어
        if current_price <= 0:
            return None

        # 일일 리셋
        today = datetime.now(timezone.utc).timetuple().tm_yday  # 연중 일수 (1~366)
        if today != self._current_day:
            self._reset_daily()
            self._current_day = today

        direction = signal_result.get("direction", "neutral")
        score = signal_result.get("score", 0)
        setup = signal_result.get("setup", "")

        # neutral / 셋업 없음 → shadow
        if direction == "neutral" or not setup:
            self._add_shadow(signal_result, current_price)
            return None

        # 최소 점수 5.5
        MIN_ENTRY_SCORE = 5.0
        if score < MIN_ENTRY_SCORE:
            self._add_shadow(signal_result, current_price)
            return None

        # ── 리스크 게이트 ──
        # 일일 손실 한도 (계좌 수준 %)
        daily_pnl_pct = (self._daily_pnl_usdt / self.balance * 100) if self.balance > 0 else 0
        if daily_pnl_pct <= -MAX_DAILY_LOSS_PCT:
            logger.debug(f"[PAPER] 일일 손실 한도 도달: {daily_pnl_pct:.1f}%")
            return None

        # 쿨다운
        if now < self._cooldown_until:
            return None

        # 최대 포지션
        if len(self.positions) >= self.max_positions:
            return None

        # 같은 방향 연속 제한
        if self._last_dir == direction and self._same_dir_count >= MAX_SAME_DIR:
            return None

        # 최소 진입 간격 (60초)
        cooldown_cfg = self.config.get("cooldown", {})
        min_interval = cooldown_cfg.get("min_interval_sec", 60)
        if now - self._last_trade_time < min_interval:
            return None

        # 같은 방향 포지션 중복 방지
        for pos in self.positions.values():
            if pos.symbol == "BTC-USDT-SWAP" and pos.direction == direction:
                return None

        # ── 사이징 (실전과 동일) ──
        hold_mode = signal_result.get("hold_mode", "standard")
        hm_cfg = self.config.get("hold_modes", {}).get(hold_mode, {})
        sl_margin_pct = hm_cfg.get("sl_margin_pct", 8.0)
        tp1_margin_pct = hm_cfg.get("tp1_margin_pct", 12.0)
        tp2_mult = hm_cfg.get("tp2_mult", 2.5)
        tp3_mult = hm_cfg.get("tp3_mult", 4.0)

        # 등급 + 레버리지
        grade = self._score_to_grade(score)
        atr_pct = signal_result.get("atr_pct", 0.3)
        leverage = self._calc_leverage(grade, atr_pct)

        # SL/TP 거리
        sl_dist = signal_result.get("sl_distance", 0)
        tp_dist = signal_result.get("tp_distance", 0)

        if sl_dist <= 0:
            sl_dist = current_price * (sl_margin_pct / leverage / 100)
        if tp_dist <= 0:
            tp_dist = current_price * (tp1_margin_pct / leverage / 100)

        # 최소 SL 0.35%
        min_sl = current_price * 0.0035
        sl_dist = max(sl_dist, min_sl)

        tp1_dist = tp_dist
        tp2_dist = sl_dist * tp2_mult
        tp3_dist = sl_dist * tp3_mult

        if direction == "long":
            sl = current_price - sl_dist
            tp1 = current_price + tp1_dist
            tp2 = current_price + tp2_dist
            tp3 = current_price + tp3_dist
        else:
            sl = current_price + sl_dist
            tp1 = current_price - tp1_dist
            tp2 = current_price - tp2_dist
            tp3 = current_price - tp3_dist

        # 수수료 필터
        fee_cost = self.FEE_TAKER * 2 * leverage * 100
        tp1_gain = tp1_dist / current_price * leverage * 100
        if tp1_gain <= fee_cost:
            return None

        # 마진 계산 (margin_loss_cap)
        margin_pct = self.risk_cfg.get("margin_pct", 0.50)

        # 연패 사이즈 축소
        streak_sizing = self.risk_cfg.get("streak_sizing", {})
        size_mult = 1.0
        for threshold, mult in sorted(streak_sizing.items(), key=lambda x: int(x[0]), reverse=True):
            if self._loss_streak >= int(threshold):
                size_mult = mult
                break

        margin = self.balance * margin_pct * size_mult
        if margin <= 0:
            return None

        # BTC 사이즈 (OKX 최소 단위 0.01 BTC)
        raw_size = margin * leverage / current_price
        size_btc = math.floor(raw_size / 0.01) * 0.01
        size_btc = round(size_btc, 4)
        if size_btc < 0.01:
            logger.debug(f"[PAPER] 사이즈 부족: {size_btc} BTC < 0.01")
            return None

        # 실제 마진 재계산 (스냅 반영)
        margin = size_btc * current_price / leverage

        # ── DB 기록 + 포지션 생성 ──
        signals_data = signal_result.get("signals", {})
        try:
            trade_id = await self.db.insert_trade({
                "symbol": "BTC-USDT-SWAP",
                "direction": direction,
                "grade": f"PAPER_{grade}",
                "score": score,
                "entry_price": current_price,
                "entry_time": int(now * 1000),
                "leverage": leverage,
                "position_size": margin,
                "signals_snapshot": json.dumps(signals_data, default=str),
            })
        except Exception as e:
            logger.error(f"[PAPER] DB insert_trade 실패: {e}")
            return None

        pos = PaperPosition(
            trade_id=trade_id, symbol="BTC-USDT-SWAP",
            direction=direction, grade=grade, score=score,
            entry_price=current_price, entry_time=now,
            margin=round(margin, 2), size_btc=size_btc, leverage=leverage,
            sl_price=round(sl, 1), tp1_price=round(tp1, 1),
            tp2_price=round(tp2, 1), tp3_price=round(tp3, 1),
            hold_mode=hold_mode, signals_snapshot=signals_data,
            flow_result=signal_result,  # FlowML 학습용 원본 보존
            setup=setup, best_price=current_price,
        )
        self.positions[trade_id] = pos

        # 진입 시 같은 방향 카운터 업데이트
        if self._last_dir == direction:
            self._same_dir_count += 1
        else:
            self._same_dir_count = 1
        self._last_dir = direction
        self._last_trade_time = now

        # Redis에 페이퍼 상태 저장
        await self._update_redis_state()

        logger.info(
            f"[PAPER] ▶ {direction.upper()} @ ${current_price:,.0f} | "
            f"마진 ${margin:.1f} × {leverage}x = ${margin * leverage:,.0f} | "
            f"SL ${sl:,.0f} TP1 ${tp1:,.0f} | 점수 {score:.1f} | "
            f"잔고 ${self.balance:,.0f} (ID:{trade_id})"
        )

        _append_jsonl({
            "type": "paper_entry",
            "paper": True,
            "trade_id": trade_id,
            "direction": direction,
            "entry_price": round(current_price, 1),
            "margin": round(margin, 2),
            "size_btc": size_btc,
            "leverage": leverage,
            "sl_price": round(sl, 1),
            "tp1_price": round(tp1, 1),
            "score": round(score, 2),
            "setup": setup,
            "hold_mode": hold_mode,
            "balance": round(self.balance, 2),
        })

        return pos

    # ══════════════════════════════════════════
    #  포지션 체크 + 청산
    # ══════════════════════════════════════════

    async def check_positions(self, current_price: float):
        """모든 가상 포지션 SL/TP/시간 체크 + shadow + 에퀴티 스냅"""
        if current_price <= 0:
            return

        closed = []
        for tid, pos in list(self.positions.items()):
            reason = self._check_exit(pos, current_price)
            if reason:
                await self._close_position(pos, current_price, reason)
                closed.append(tid)

        for tid in closed:
            del self.positions[tid]

        # shadow 체크
        await self._check_shadows(current_price)

        # 에퀴티 스냅샷 (1시간마다)
        now = time.time()
        if now - self._last_equity_snap >= 3600:
            self._snap_equity(current_price)
            self._last_equity_snap = now

        # Redis 상태 업데이트
        if closed:
            await self._update_redis_state()

    def _check_exit(self, pos: PaperPosition, price: float) -> str | None:
        """청산 조건 체크"""
        # SL
        if pos.direction == "long" and price <= pos.sl_price:
            return "sl_hit"
        if pos.direction == "short" and price >= pos.sl_price:
            return "sl_hit"

        # TP1 → 반익본절
        if not pos.tp1_hit:
            if pos.direction == "long" and price >= pos.tp1_price:
                pos.tp1_hit = True
                pos.sl_price = pos.entry_price  # 본절 이동
                logger.info(f"[PAPER] TP1 도달 → SL 본절 이동 (ID:{pos.trade_id})")
            if pos.direction == "short" and price <= pos.tp1_price:
                pos.tp1_hit = True
                pos.sl_price = pos.entry_price
                logger.info(f"[PAPER] TP1 도달 → SL 본절 이동 (ID:{pos.trade_id})")

        # TP2 → 기록
        if not pos.tp2_hit:
            if pos.direction == "long" and price >= pos.tp2_price:
                pos.tp2_hit = True
                logger.info(f"[PAPER] TP2 도달 (ID:{pos.trade_id})")
            if pos.direction == "short" and price <= pos.tp2_price:
                pos.tp2_hit = True
                logger.info(f"[PAPER] TP2 도달 (ID:{pos.trade_id})")

        # TP3 → 전량 청산
        if pos.direction == "long" and price >= pos.tp3_price:
            return "tp3_hit"
        if pos.direction == "short" and price <= pos.tp3_price:
            return "tp3_hit"

        # 트레일링 (TP1 이후)
        if pos.tp1_hit:
            trail_cfg = self.config.get("trailing", {})
            trail_margin_pct = trail_cfg.get("trail_margin_pct", 4.0)
            trail_dist = pos.entry_price * (trail_margin_pct / pos.leverage / 100)

            if pos.direction == "long":
                pos.best_price = max(pos.best_price, price)
                trail_sl = pos.best_price - trail_dist
                if trail_sl > pos.sl_price:
                    pos.sl_price = round(trail_sl, 1)
            else:
                if pos.best_price <= 0 or price < pos.best_price:
                    pos.best_price = price
                trail_sl = pos.best_price + trail_dist
                if trail_sl < pos.sl_price:
                    pos.sl_price = round(trail_sl, 1)

        # 시간 청산
        hold_min = pos.hold_minutes()
        hm_cfg = self.config.get("hold_modes", {}).get(pos.hold_mode, {})
        max_hold = hm_cfg.get("max_hold_min", 60)

        if hold_min >= max_hold:
            return "time_exit"

        # 중간 시간 체크 (50% 시간 경과 + 손실 중)
        if hold_min >= max_hold * 0.5:
            pnl = pos.unrealized_pnl_pct(price)
            if pnl < 0:  # 손실일 때만 (breakeven은 유지)
                return f"time_exit_{int(hold_min)}m"

        return None

    async def _close_position(self, pos: PaperPosition, exit_price: float, reason: str):
        """가상 포지션 청산 → 잔고 반영 + DB + ML"""
        # PnL 계산 (수수료 포함)
        raw_pnl_pct = pos.unrealized_pnl_pct(exit_price)
        # 진입: maker, 청산: sl은 taker, 나머지 maker
        if "sl" in reason:
            fee_pct = (self.FEE_MAKER + self.FEE_TAKER) * pos.leverage * 100
        else:
            fee_pct = self.FEE_MAKER * 2 * pos.leverage * 100
        net_pnl_pct = raw_pnl_pct - fee_pct
        pnl_usdt = pos.margin * net_pnl_pct / 100

        # ── 잔고 반영 ──
        self.balance += pnl_usdt
        self.balance = max(self.balance, 0)  # 마이너스 방어
        if self.balance > self.peak_balance:
            self.peak_balance = self.balance

        # ── 리스크 상태 업데이트 ──
        self._daily_pnl_usdt += pnl_usdt

        if net_pnl_pct > 0:
            self._stats["wins"] += 1
            self._loss_streak = 0
            self._cooldown_until = time.time() + COOLDOWN_AFTER_WIN_SEC
        elif net_pnl_pct < 0:
            self._stats["losses"] += 1
            self._loss_streak += 1
            self._cooldown_until = time.time() + COOLDOWN_AFTER_LOSS_SEC
        # breakeven (net_pnl_pct == 0): 승/패 어느 쪽에도 카운트하지 않음

        self._stats["total"] += 1
        self._stats["total_pnl_usdt"] += pnl_usdt
        self._stats["best_trade_pct"] = max(self._stats["best_trade_pct"], net_pnl_pct)
        self._stats["worst_trade_pct"] = min(self._stats["worst_trade_pct"], net_pnl_pct)

        # ── DB 업데이트 ──
        exit_time = int(time.time() * 1000)
        # fee_total: 실제 적용된 수수료와 일치시킴
        notional = pos.margin * pos.leverage
        if "sl" in reason:
            fee_total = notional * (self.FEE_MAKER + self.FEE_TAKER)  # 진입maker + 청산taker
        else:
            fee_total = notional * self.FEE_MAKER * 2  # 양쪽 maker
        try:
            await self.db.update_trade_exit(pos.trade_id, {
                "exit_price": exit_price,
                "exit_time": exit_time,
                "exit_reason": f"paper_{reason}",
                "pnl_usdt": round(pnl_usdt, 4),
                "pnl_pct": round(net_pnl_pct, 4),
                "fee_total": round(fee_total, 4),
                "funding_cost": 0,
            })
        except Exception as e:
            logger.error(f"[PAPER] DB update_trade_exit 실패 (ID:{pos.trade_id}): {e}")

        # ── ML 학습 (FlowML — flow_result 원본 전달) ──
        regime = self._get_regime()
        if self.flow_ml and pos.flow_result:
            # FlowML.record_trade(flow_result: dict, pnl_pct: float, fee_pct: float)
            self.flow_ml.record_trade(pos.flow_result, net_pnl_pct, fee_pct)

        # 시그널 기여도 추적
        if self.signal_tracker:
            try:
                self.signal_tracker.record_trade(
                    pos.signals_snapshot, net_pnl_pct, mode="paper", regime=regime
                )
            except Exception as e:
                logger.debug(f"[PAPER] signal_tracker 기록 실패: {e}")

        # 셋업 성과 추적
        if self.setup_tracker and pos.setup:
            try:
                hold_min = pos.hold_minutes()
                trend = pos.signals_snapshot.get("context", {}).get("trend", "neutral")
                self.setup_tracker.record_trade(
                    setup=pos.setup, direction=pos.direction,
                    pnl_pct=net_pnl_pct, pnl_usdt=pnl_usdt,
                    hold_min=hold_min, exit_reason=reason,
                    trend=trend, regime=regime,
                )
            except Exception as e:
                logger.debug(f"[PAPER] setup_tracker 기록 실패: {e}")

        # ── 로그 ──
        win_rate = self._stats["wins"] / max(self._stats["total"], 1) * 100
        total_return = (self.balance - self.initial_balance) / self.initial_balance * 100
        drawdown = (self.peak_balance - self.balance) / self.peak_balance * 100 if self.peak_balance > 0 else 0

        logger.info(
            f"[PAPER] ◀ {pos.direction.upper()} 청산 @ ${exit_price:,.0f} | "
            f"사유: {reason} | PnL: {net_pnl_pct:+.2f}% (${pnl_usdt:+.2f}) | "
            f"잔고: ${self.balance:,.0f} ({total_return:+.1f}%) | "
            f"DD: {drawdown:.1f}% | 승률: {win_rate:.0f}% ({self._stats['total']}건) | "
            f"연패: {self._loss_streak}"
        )

        # ── JSONL 영구 기록 ──
        _append_jsonl({
            "type": "paper_exit",
            "paper": True,
            "trade_id": pos.trade_id,
            "direction": pos.direction,
            "setup": pos.setup,
            "exit_reason": reason,
            "entry_price": round(pos.entry_price, 1),
            "exit_price": round(exit_price, 1),
            "margin": round(pos.margin, 2),
            "size_btc": pos.size_btc,
            "leverage": pos.leverage,
            "pnl_pct": round(net_pnl_pct, 2),
            "pnl_usdt": round(pnl_usdt, 2),
            "fee_pct": round(fee_pct, 2),
            "fee_total": round(fee_total, 4),
            "score": round(pos.score, 2),
            "grade": pos.grade,
            "hold_min": round(pos.hold_minutes()),
            "hold_mode": pos.hold_mode,
            "regime": regime,
            "balance_after": round(self.balance, 2),
            "total_return_pct": round(total_return, 2),
            "drawdown_pct": round(drawdown, 2),
            "stats_total": self._stats["total"],
            "stats_win_rate": round(win_rate, 1),
            "loss_streak": self._loss_streak,
        })

    # ══════════════════════════════════════════
    #  Shadow (미진입 추적)
    # ══════════════════════════════════════════

    def _add_shadow(self, signal_result: dict, current_price: float):
        direction = signal_result.get("direction", "neutral")
        score = signal_result.get("score", 0)
        signals = signal_result.get("signals", {})

        if direction == "neutral":
            # neutral: 양방향 추적 (2개 추가하므로 여유 확인)
            if len(self.shadows) >= self.max_shadows - 1:
                return
            for d in ["long", "short"]:
                self.shadows.append(ShadowTrack(
                    direction=d, score=score, entry_price=current_price,
                    entry_time=time.time(), signals_snapshot=signals,
                    flow_result=signal_result,
                    best_price=current_price, worst_price=current_price,
                ))
        else:
            if len(self.shadows) >= self.max_shadows:
                return
            self.shadows.append(ShadowTrack(
                direction=direction, score=score, entry_price=current_price,
                entry_time=time.time(), signals_snapshot=signals,
                flow_result=signal_result,
                best_price=current_price, worst_price=current_price,
            ))

    async def _check_shadows(self, current_price: float):
        expired = []
        for s in self.shadows:
            if s.direction == "long":
                s.best_price = max(s.best_price, current_price)
                s.worst_price = min(s.worst_price, current_price)
            else:
                if s.best_price <= 0 or current_price < s.best_price:
                    s.best_price = current_price
                s.worst_price = max(s.worst_price, current_price)

            if s.hold_minutes() >= 30:
                expired.append(s)
                await self._evaluate_shadow(s)

        for s in expired:
            if s in self.shadows:
                self.shadows.remove(s)

    async def _evaluate_shadow(self, s: ShadowTrack):
        potential = s.potential_pnl_pct()
        self._stats["shadow_total"] += 1

        if potential > 1.0:
            self._stats["shadow_missed"] += 1
            if self.flow_ml and s.flow_result:
                # FlowML.record_trade(flow_result, pnl_pct, fee_pct)
                self.flow_ml.record_trade(s.flow_result, potential * 0.5, 0)
            logger.info(
                f"[PAPER-SHADOW] 놓친 기회: {s.direction.upper()} "
                f"잠재 PnL: {potential:+.1f}% 점수 {s.score:.1f}"
            )
        elif potential < -1.0:
            self._stats["shadow_correct"] += 1

    # ══════════════════════════════════════════
    #  유틸
    # ══════════════════════════════════════════

    def _score_to_grade(self, score: float) -> str:
        for threshold, grade in SCORE_GRADE_MAP:
            if score >= threshold:
                return grade
        return "B-"

    def _calc_leverage(self, grade: str, atr_pct: float) -> int:
        lev_range = self.risk_cfg.get("leverage_range", [15, 20])
        min_lev = max(1, lev_range[0])  # 최소 1 보장
        max_lev = max(1, lev_range[1])

        grade_limit = GRADE_MAX_LEVERAGE.get(grade, 15)
        streak_mult = STREAK_MULTIPLIER.get(min(self._loss_streak, 4), 0.3)
        streak_limit = max(min_lev, int(grade_limit * streak_mult))

        final = min(grade_limit, streak_limit)
        final = max(min_lev, min(max_lev, final))
        return max(1, final)  # 절대 0 방지

    def _get_regime(self) -> str:
        if self.regime_detector and hasattr(self.regime_detector, "_regime_history"):
            history = self.regime_detector._regime_history
            if history:
                return history[-1]
        return "unknown"

    def _reset_daily(self):
        """일일 리셋 — loss_streak는 유지 (win까지 살아있어야 함)"""
        if self._daily_pnl_usdt != 0:
            logger.info(
                f"[PAPER] 일일 정산: ${self._daily_pnl_usdt:+.2f} | "
                f"잔고 ${self.balance:,.0f}"
            )
        self._daily_pnl_usdt = 0.0

    def _snap_equity(self, current_price: float):
        """에퀴티 커브 스냅샷"""
        unrealized = sum(
            pos.unrealized_pnl_usdt(current_price)
            for pos in self.positions.values()
        )
        equity = self.balance + unrealized
        snap = {
            "ts": int(time.time()),
            "balance": round(self.balance, 2),
            "equity": round(equity, 2),
            "unrealized": round(unrealized, 2),
            "positions": len(self.positions),
            "total_trades": self._stats["total"],
        }
        self._equity_history.append(snap)
        # 최대 720개 (30일 × 24시간)
        if len(self._equity_history) > 720:
            self._equity_history = self._equity_history[-720:]

        _append_jsonl({"type": "paper_equity", "paper": True, **snap})

    async def _update_redis_state(self):
        """Redis에 페이퍼 계좌 상태 저장 (대시보드/텔레그램용)"""
        total_return = (self.balance - self.initial_balance) / self.initial_balance * 100
        drawdown = (self.peak_balance - self.balance) / self.peak_balance * 100 if self.peak_balance > 0 else 0
        win_rate = self._stats["wins"] / max(self._stats["total"], 1) * 100
        daily_pnl_pct = (self._daily_pnl_usdt / self.balance * 100) if self.balance > 0 else 0

        state = {
            "balance": round(self.balance, 2),
            "initial_balance": self.initial_balance,
            "total_return_pct": round(total_return, 2),
            "peak_balance": round(self.peak_balance, 2),
            "drawdown_pct": round(drawdown, 2),
            "daily_pnl_pct": round(daily_pnl_pct, 2),
            "daily_pnl_usdt": round(self._daily_pnl_usdt, 2),
            "total_trades": self._stats["total"],
            "wins": self._stats["wins"],
            "losses": self._stats["losses"],
            "win_rate": round(win_rate, 1),
            "total_pnl_usdt": round(self._stats["total_pnl_usdt"], 2),
            "best_trade_pct": round(self._stats["best_trade_pct"], 2),
            "worst_trade_pct": round(self._stats["worst_trade_pct"], 2),
            "loss_streak": self._loss_streak,
            "active_positions": len(self.positions),
            "shadow_missed": self._stats["shadow_missed"],
            "shadow_correct": self._stats["shadow_correct"],
        }

        try:
            await self.redis.set("paper:state", state, ttl=3600)

            # 활성 포지션 정보
            if self.positions:
                price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
                cp = float(price_str) if price_str else 0
                pos_list = []
                for pos in self.positions.values():
                    p = cp if cp > 0 else pos.entry_price
                    pos_list.append({
                        "trade_id": pos.trade_id,
                        "direction": pos.direction,
                        "entry_price": pos.entry_price,
                        "sl_price": pos.sl_price,
                        "tp1_price": pos.tp1_price,
                        "margin": pos.margin,
                        "leverage": pos.leverage,
                        "pnl_pct": round(pos.unrealized_pnl_pct(p), 2),
                        "pnl_usdt": round(pos.unrealized_pnl_usdt(p), 2),
                        "hold_min": round(pos.hold_minutes()),
                    })
                await self.redis.set("paper:positions", pos_list, ttl=3600)
            else:
                await self.redis.set("paper:positions", [], ttl=3600)
        except Exception as e:
            logger.debug(f"[PAPER] Redis 상태 저장 실패: {e}")

    # ══════════════════════════════════════════
    #  외부 인터페이스
    # ══════════════════════════════════════════

    def get_stats(self) -> dict:
        total_return = (self.balance - self.initial_balance) / self.initial_balance * 100
        drawdown = (self.peak_balance - self.balance) / self.peak_balance * 100 if self.peak_balance > 0 else 0
        win_rate = self._stats["wins"] / max(self._stats["total"], 1) * 100
        daily_pnl_pct = (self._daily_pnl_usdt / self.balance * 100) if self.balance > 0 else 0
        return {
            "balance": round(self.balance, 2),
            "initial_balance": self.initial_balance,
            "total_return_pct": round(total_return, 2),
            "drawdown_pct": round(drawdown, 2),
            "total_trades": self._stats["total"],
            "win_rate": round(win_rate, 1),
            "daily_pnl_pct": round(daily_pnl_pct, 2),
            "loss_streak": self._loss_streak,
            "active_positions": len(self.positions),
            "shadow_missed": self._stats["shadow_missed"],
            "shadow_correct": self._stats["shadow_correct"],
        }
