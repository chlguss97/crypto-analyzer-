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
    setup: str = "momentum"
    tp1_hit: bool = False
    tp2_hit: bool = False
    runner_mode: bool = False   # TP1 후 러너 모드
    best_price: float = 0.0    # 트레일링용
    last_new_high_time: float = 0.0
    remaining_size: float = 0.0  # 초기값은 size_btc로 설정
    realized_pnl_usdt: float = 0.0  # TP1 부분청산 실현 손익

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
    #  진입 판단 (v2 — CandidateDetector 연동)
    # ══════════════════════════════════════════

    async def try_candidate_entry(self, candidate: dict, regime: str):
        """CandidateDetector 후보 → 가상 진입 (SPEC v2)"""
        now = time.time()
        current_price = candidate.get("price", 0)
        if current_price <= 0:
            return None

        # 일일 리셋
        today = datetime.now(timezone.utc).timetuple().tm_yday
        if today != self._current_day:
            self._reset_daily()
            self._current_day = today

        direction = candidate.get("direction", "neutral")
        ctype = candidate.get("type", "momentum")
        strength = candidate.get("strength", 0)

        if direction == "neutral":
            return None

        # ── paper는 벤치마크용 — 모든 게이트/필터 없음 ──

        # ── 사이징 ──
        hold_mode = candidate.get("hold_mode", "momentum")
        hm_cfg = self.config.get("hold_modes", {}).get(hold_mode, {})
        sl_margin_pct = hm_cfg.get("sl_margin_pct", 5.0)
        tp1_margin_pct = hm_cfg.get("tp1_margin_pct", 10.0)
        tp2_mult = hm_cfg.get("tp2_mult", 2.5)
        tp3_mult = hm_cfg.get("tp3_mult", 4.0)

        # 레버리지 (설정에서 직접 계산 — LeverageCalculator 재생성 방지)
        lev_range = self.config.get("risk", {}).get("leverage_range", [15, 20])
        if strength >= 2.0:
            leverage = lev_range[1]  # 최대
        elif strength >= 1.0:
            leverage = (lev_range[0] + lev_range[1]) // 2  # 중간
        else:
            leverage = lev_range[0]  # 최소
        # 연패 축소
        if self._loss_streak >= 5:
            leverage = lev_range[0]
        grade = "A+" if strength >= 2.0 else "A" if strength >= 1.5 else "B+" if strength >= 1.0 else "B"

        # SL/TP (min SL 0.5% — 실전과 동일)
        sl_dist = current_price * (sl_margin_pct / leverage / 100)
        sl_dist = max(sl_dist, current_price * 0.005)

        # TP1: ATR 기반 (하한 0.25%, 상한 0.80%)
        atr_pct = candidate.get("atr_pct", 0.3)
        atr_tp1 = current_price * min(max(atr_pct * 1.5 / 100, 0.0025), 0.008)
        tp1_dist = atr_tp1
        if tp1_dist < sl_dist * 1.3:
            tp1_dist = sl_dist * 1.3

        if direction == "long":
            sl = current_price - sl_dist
            tp1 = current_price + tp1_dist
            tp2 = current_price + sl_dist * tp2_mult
            tp3 = current_price + sl_dist * tp3_mult
        else:
            sl = current_price + sl_dist
            tp1 = current_price - tp1_dist
            tp2 = current_price - sl_dist * tp2_mult
            tp3 = current_price - sl_dist * tp3_mult

        # 마진
        margin_pct = self.config.get("risk", {}).get("margin_pct", 0.40)
        streak_sizing = self.config.get("risk", {}).get("streak_sizing", {})
        size_mult = 1.0
        for threshold, mult in sorted(streak_sizing.items(), key=lambda x: int(x[0]), reverse=True):
            if self._loss_streak >= int(threshold):
                size_mult = mult
                break

        margin = self.balance * margin_pct * size_mult
        size_btc = margin * leverage / current_price
        size_btc = math.floor(size_btc / 0.01) * 0.01
        if size_btc < 0.01 or margin <= 0:
            return None

        # ── 가상 진입 ──
        self._stats["total"] += 0  # 진입 시점에서는 카운트 안 함 (청산 시 카운트)
        trade_id = await self._record_entry(
            direction, current_price, sl, tp1, tp2, tp3,
            size_btc, margin, leverage, ctype, strength, hold_mode,
            candidate.get("features_raw", {}), regime
        )

        if trade_id:
            grade = "A" if strength >= 1.5 else "B+" if strength >= 1.0 else "B"
            pos = PaperPosition(
                trade_id=trade_id, symbol="BTC-USDT-SWAP",
                direction=direction,
                grade=f"PAPER_{ctype}",
                score=strength,
                entry_price=current_price,
                entry_time=now,
                margin=margin, size_btc=size_btc, leverage=leverage,
                sl_price=sl, tp1_price=tp1, tp2_price=tp2, tp3_price=tp3,
                setup=ctype, hold_mode=hold_mode,
                flow_result=candidate.get("features_raw", {}),
                signals_snapshot=candidate.get("features_raw", {}),
            )
            pos.remaining_size = size_btc
            pos.best_price = current_price
            pos.last_new_high_time = now
            self.positions[trade_id] = pos
            self._last_trade_time = now
            self._last_dir = direction

            logger.info(
                f"[PAPER] ▶ {direction.upper()} @ ${current_price:,.0f} | "
                f"{ctype} str={strength:.1f} | SL ${sl:,.0f} TP ${tp1:,.0f} | "
                f"{leverage}x | margin ${margin:,.0f}"
            )

            # JSONL 기록
            _append_jsonl({
                "type": "paper_entry", "paper": True,
                "trade_id": trade_id, "direction": direction,
                "entry_price": round(current_price, 1),
                "margin": round(margin, 2), "size_btc": round(size_btc, 2),
                "leverage": leverage, "sl_price": round(sl, 1),
                "tp1_price": round(tp1, 1),
                "score": strength, "setup": ctype,
                "hold_mode": hold_mode,
                "balance": round(self.balance, 2),
            })

            return trade_id
        return None

    async def _record_entry(self, direction, price, sl, tp1, tp2, tp3,
                            size, margin, leverage, setup, score, hold_mode,
                            signals, regime):
        """DB에 가상 진입 기록"""
        try:
            import json
            trade_id = await self.db.insert_trade({
                "symbol": "BTC-USDT-SWAP",
                "direction": direction,
                "grade": f"PAPER_{setup}",
                "score": score,
                "entry_price": round(price, 1),
                "entry_time": int(time.time() * 1000),
                "leverage": leverage,
                "position_size": round(size, 4),
                "signals_snapshot": json.dumps(signals, default=str),
            })
            return trade_id
        except Exception as e:
            logger.error(f"[PAPER] DB 진입 기록 실패: {e}")
            return None

    # ── 레거시 호환: 기존 try_entry (FlowEngine result 기반) ──

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

        # ── paper는 벤치마크용 — 게이트 없음 (try_candidate_entry와 동일) ──

        # 같은 방향 포지션 중복 방지
        for pos in self.positions.values():
            if pos.symbol == "BTC-USDT-SWAP" and pos.direction == direction:
                return None

        # 04-28: RANGING 레짐 진입 차단 — 횡보장 전패 근절
        regime = self._get_regime()
        if regime == "ranging":
            logger.debug(f"[PAPER] 레짐 게이트: RANGING → {setup} {direction.upper()} 차단")
            return None

        # 모멘텀 게이트: 급락 중 Long / 급등 중 Short 차단
        try:
            vel = await self.redis.hgetall("rt:velocity:BTC-USDT-SWAP") if self.redis else {}
            if vel:
                move_60s = float(vel.get("move_60s", 0))
                move_30s = float(vel.get("move_30s", 0))
                if direction == "long" and (move_60s < -150 or move_30s < -100):
                    return None
                if direction == "short" and (move_60s > 150 or move_30s > 100):
                    return None
        except Exception:
            pass

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

        # 최소 SL 0.35%
        min_sl = current_price * 0.005  # 5m 노이즈 밖으로 (0.35%→0.5%)
        sl_dist = max(sl_dist, min_sl)

        # TP1: ATR 기반 (하한 0.25%, 상한 0.80%)
        atr_tp1 = current_price * min(max(atr_pct * 1.5 / 100, 0.0025), 0.008)
        tp1_dist = atr_tp1
        # RR 최소 1.3 보장
        if tp1_dist < sl_dist * 1.3:
            tp1_dist = sl_dist * 1.3

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

        # 모멘텀 소진 체크
        recent_move_pct = signal_result.get("recent_move_pct", 0)
        tp1_pct = tp1_dist / current_price * 100
        if recent_move_pct > tp1_pct * 0.5 and recent_move_pct > 0:
            return None

        # 수수료 필터 (04-28: maker 강제 정책)
        fee_cost = self.FEE_MAKER * 2 * leverage * 100
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
        pos.remaining_size = size_btc
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
        """청산 조건 — 실전 position_manager와 동일 구조"""
        now = time.time()
        hold_min = pos.hold_minutes()

        # ── 0. Adverse Selection (진입 후 90초) ──
        as_cfg = self.config.get("adverse_selection", {})
        if as_cfg.get("enabled", False) and not pos.runner_mode:
            elapsed_sec = hold_min * 60
            window = as_cfg.get("window_sec", 90)
            if elapsed_sec <= window:
                margin_pct = pos.unrealized_pnl_pct(price)
                threshold = -as_cfg.get("margin_threshold_pct", 2.5)
                if margin_pct <= threshold:
                    return "adverse_selection"

        # ── 1. SL ──
        if pos.direction == "long" and price <= pos.sl_price:
            return "sl_hit"
        if pos.direction == "short" and price >= pos.sl_price:
            return "sl_hit"

        # ── 2. TP1 → 50% 부분청산 + 본절 + 러너 모드 ──
        if not pos.tp1_hit:
            tp1_hit = (pos.direction == "long" and price >= pos.tp1_price) or \
                      (pos.direction == "short" and price <= pos.tp1_price)
            if tp1_hit:
                pos.tp1_hit = True
                pos.runner_mode = True
                pos.best_price = price
                pos.last_new_high_time = now

                # 50% 부분청산 실현 PnL
                close_pct = self.config.get("trailing", {}).get("tp1_close_pct", 0.5)
                partial_size = pos.size_btc * close_pct
                raw_pnl = pos.unrealized_pnl_pct(price)
                fee_pct = self.FEE_MAKER * 2 * pos.leverage * 100
                net_pnl = raw_pnl - fee_pct
                pos.realized_pnl_usdt += pos.margin * close_pct * net_pnl / 100
                pos.remaining_size = pos.size_btc - partial_size

                # SL → 본절 (진입가 + 수수료)
                fee_offset = pos.entry_price * (self.FEE_MAKER * 2)
                if pos.direction == "long":
                    pos.sl_price = pos.entry_price + fee_offset
                else:
                    pos.sl_price = pos.entry_price - fee_offset

                logger.info(
                    f"[PAPER] TP1 50% 청산: {pos.direction.upper()} "
                    f"+{net_pnl:.1f}% (${pos.realized_pnl_usdt:+.2f}) "
                    f"| 러너 {pos.remaining_size:.4f} BTC | SL→본절 ${pos.sl_price:.0f}"
                )

        # ── 3. TP3 → 전량 청산 ──
        if pos.direction == "long" and price >= pos.tp3_price:
            return "tp3_hit"
        if pos.direction == "short" and price <= pos.tp3_price:
            return "tp3_hit"

        # ── 4. 러너 트레일링 (실전 동일 — 동적 거리 + R-lock + 시간감쇠) ──
        if pos.runner_mode:
            # 신고/신저 갱신
            if pos.direction == "long":
                if price > pos.best_price:
                    pos.best_price = price
                    pos.last_new_high_time = now
            else:
                if pos.best_price <= 0 or price < pos.best_price:
                    pos.best_price = price
                    pos.last_new_high_time = now

            # 동적 트레일 거리 계산
            profit = abs(pos.best_price - pos.entry_price)
            atr_proxy = price * 0.002  # 0.2%

            # 시간 감쇠: 보유 시간이 길수록 트레일 좁힘
            if hold_min < 15:
                giveback = 0.35
            elif hold_min < 30:
                giveback = 0.25
            else:
                giveback = 0.15

            # 정체 감지: 10분 이상 신고가 없으면 giveback 절반
            stale_min = (now - pos.last_new_high_time) / 60
            if stale_min > 10:
                giveback *= 0.5

            trail_dist = max(atr_proxy * 1.5, profit * giveback)
            trail_dist = min(trail_dist, price * 0.008)  # cap 0.8%
            trail_dist = max(trail_dist, price * 0.001)  # floor 0.1%

            # R-lock: 큰 수익 보호
            sl_dist_orig = abs(pos.entry_price - pos.sl_price) if not pos.tp1_hit else abs(pos.entry_price * 0.005)
            r_val = sl_dist_orig if sl_dist_orig > 0 else atr_proxy
            if profit >= 3 * r_val:
                min_sl_lock = pos.entry_price + 2 * r_val if pos.direction == "long" else pos.entry_price - 2 * r_val
            elif profit >= 2 * r_val:
                min_sl_lock = pos.entry_price + 1 * r_val if pos.direction == "long" else pos.entry_price - 1 * r_val
            else:
                min_sl_lock = pos.sl_price

            # 새 SL 계산
            if pos.direction == "long":
                new_sl = pos.best_price - trail_dist
                new_sl = max(new_sl, min_sl_lock)
                if new_sl > pos.sl_price:
                    pos.sl_price = round(new_sl, 1)
            else:
                new_sl = pos.best_price + trail_dist
                new_sl = min(new_sl, min_sl_lock)
                if new_sl < pos.sl_price:
                    pos.sl_price = round(new_sl, 1)

        # 시간 청산 제거 (2026-04-30) — SL/TP/트레일링에 위임

        return None

    async def _close_position(self, pos: PaperPosition, exit_price: float, reason: str):
        """가상 포지션 청산 → 잔고 반영 + DB + ML"""
        # PnL 계산 (수수료 포함)
        # 러너 모드면 남은 사이즈 비율로 미실현 PnL 계산
        remaining_ratio = pos.remaining_size / pos.size_btc if pos.size_btc > 0 else 1.0
        raw_pnl_pct = pos.unrealized_pnl_pct(exit_price)
        fee_pct = self.FEE_MAKER * 2 * pos.leverage * 100
        net_pnl_pct = raw_pnl_pct - fee_pct

        # 남은 부분의 PnL + TP1 부분청산 실현 PnL
        remaining_pnl_usdt = pos.margin * remaining_ratio * net_pnl_pct / 100
        pnl_usdt = remaining_pnl_usdt + pos.realized_pnl_usdt

        # 전체 마진 대비 순수익률 재계산
        if pos.margin > 0:
            net_pnl_pct = pnl_usdt / pos.margin * 100

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
        # fee_total: 양쪽 maker (04-28 강제 정책)
        notional = pos.margin * pos.leverage
        fee_total = notional * self.FEE_MAKER * 2
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

        # ── ML 결과 기록 ──
        regime = self._get_regime()
        if self.flow_ml:
            label = 1 if net_pnl_pct > 0 else 0
            self.flow_ml.record_decision_result(True, label)

        # 시그널 기여도 추적
        if self.signal_tracker:
            try:
                self.signal_tracker.record_trade(
                    pos.signals_snapshot, net_pnl_pct, mode="unified", regime=regime
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

        # ── AdaptiveParams TP/SL 데이터 (paper도 기여) ──
        if hasattr(self, '_adaptive') and self._adaptive:
            ep = pos.entry_price
            tp1_dist = abs(pos.tp1_price - ep) if pos.tp1_price > 0 else 1
            hold_min = pos.hold_minutes()
            if pos.direction == "long":
                reach = (pos.best_price - ep) / tp1_dist * 100 if tp1_dist > 0 else 0
                mae_pct = (ep - pos.worst_price) / ep * 100 if pos.worst_price > 0 else 0
            else:
                reach = (ep - pos.best_price) / tp1_dist * 100 if tp1_dist > 0 else 0
                mae_pct = (pos.worst_price - ep) / ep * 100 if pos.worst_price > 0 else 0
            try:
                await self._adaptive.record_trade({
                    "direction": pos.direction,
                    "pnl_pct": net_pnl_pct,
                    "hold_min": hold_min,
                    "exit_reason": reason,
                    "tp1_reach_pct": round(reach, 1),
                    "mae_pct": round(mae_pct, 4),
                    "time_to_first_profit_sec": 0,
                    "entry_atr": getattr(pos, 'entry_atr', 0.3),
                    "entry_h1_trend": "unknown",
                    "entry_h4_trend": "unknown",
                    "regime": regime,
                    "entry_ts": pos.entry_time,
                    "leverage": pos.leverage,
                })
            except Exception:
                pass

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
            if self.flow_ml:
                self.flow_ml.record_decision_result(False, 1)  # 놓친 수익 기회
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
            await self.redis.set("paper:state", state)

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
                await self.redis.set("paper:positions", pos_list)
            else:
                await self.redis.set("paper:positions", [])
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
