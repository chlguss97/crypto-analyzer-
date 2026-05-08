"""
PaperLab — 다중 파라미터 A/B 테스터
SPEC v2 §10 연계

역할: 여러 파라미터 세트를 동시에 시뮬레이션하여 최적 설정 발견.
Shadow(시장 관찰)와 분리 — Paper는 "어떤 설정이 최적인가?" 에 답함.

구조:
  PaperLab
    ├── Variant A: {atr_mult: 1.0, sl_pct: 4.0}  (tight)
    ├── Variant B: {atr_mult: 1.5, sl_pct: 5.0}  (base = 실거래 동일)
    └── Variant C: {atr_mult: 2.0, sl_pct: 6.0}  (wide)

각 Variant는 독립 포지션 풀을 가짐. 같은 시그널에 서로 다른 TP/SL로 진입.
"""

import json
import logging
import math
import time
from dataclasses import dataclass, field
from src.monitoring.trade_logger import _append_jsonl

logger = logging.getLogger(__name__)


@dataclass
class LabPosition:
    """Variant별 가상 포지션"""
    variant_name: str
    trade_id: int
    direction: str
    entry_price: float
    entry_time: float
    size_btc: float
    leverage: int
    sl_price: float
    tp1_price: float
    setup: str
    regime: str = "unknown"
    atr_pct: float = 0.3
    # 추적
    best_price: float = 0.0
    worst_price: float = 0.0
    tp1_hit: bool = False
    runner_mode: bool = False
    remaining_size: float = 0.0
    realized_pnl: float = 0.0  # TP1 부분청산 실현분

    def hold_minutes(self) -> float:
        return (time.time() - self.entry_time) / 60


@dataclass
class Variant:
    """파라미터 세트 + 성과 추적"""
    name: str
    atr_mult: float
    sl_margin_pct: float
    rr_min: float = 1.3
    # 성과
    trades: int = 0
    wins: int = 0
    total_pnl_pct: float = 0.0
    positions: dict = field(default_factory=dict)  # trade_id → LabPosition
    _next_id: int = 0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades * 100 if self.trades > 0 else 0

    @property
    def avg_pnl(self) -> float:
        return self.total_pnl_pct / self.trades if self.trades > 0 else 0

    @property
    def ev(self) -> float:
        """기대값 (평균 PnL%)"""
        return self.avg_pnl


class PaperLab:
    """다중 파라미터 A/B 테스터"""

    FEE_MAKER = 0.0002  # 0.02%

    def __init__(self, config: dict = None, adaptive=None):
        self.config = config or {}
        self._adaptive = adaptive
        self._trade_counter = 0

        # 3개 Variant 정의
        base_sl = self.config.get("hold_modes", {}).get("momentum", {}).get("sl_margin_pct", 5.0)
        self.variants = [
            Variant(name="tight", atr_mult=1.0, sl_margin_pct=max(base_sl - 1.0, 3.0)),
            Variant(name="base", atr_mult=1.5, sl_margin_pct=base_sl),
            Variant(name="wide", atr_mult=2.0, sl_margin_pct=min(base_sl + 1.0, 7.0)),
        ]

        logger.info(
            f"[LAB] 초기화: {len(self.variants)} variants — "
            + ", ".join(f"{v.name}(ATR×{v.atr_mult}, SL{v.sl_margin_pct}%)" for v in self.variants)
        )

    async def on_candidate(self, candidate: dict, regime: str):
        """시그널 발생 시 모든 variant에서 진입"""
        price = candidate.get("price", 0)
        if price <= 0:
            return

        direction = candidate.get("direction", "neutral")
        if direction == "neutral":
            return

        atr_pct = candidate.get("atr_pct", 0.3)
        ctype = candidate.get("type", "momentum")
        strength = candidate.get("strength", 0)

        lev_range = self.config.get("risk", {}).get("leverage_range", [15, 20])
        leverage = lev_range[0]  # 보수적 기본값

        for v in self.variants:
            # 이미 같은 방향 포지션 있으면 스킵 (variant당 1개)
            has_same_dir = any(p.direction == direction for p in v.positions.values())
            if has_same_dir:
                continue

            # SL/TP 계산 (variant별 파라미터)
            sl_dist = price * (v.sl_margin_pct / leverage / 100)
            tp_dist = price * min(max(atr_pct * v.atr_mult / 100, 0.0025), 0.008)
            if tp_dist < sl_dist * v.rr_min:
                tp_dist = sl_dist * v.rr_min

            if direction == "long":
                sl = price - sl_dist
                tp1 = price + tp_dist
            else:
                sl = price + sl_dist
                tp1 = price - tp_dist

            # 사이즈 (고정 0.01 BTC — 비교용이라 사이즈 무관)
            size_btc = 0.01

            self._trade_counter += 1
            tid = self._trade_counter
            pos = LabPosition(
                variant_name=v.name,
                trade_id=tid,
                direction=direction,
                entry_price=price,
                entry_time=time.time(),
                size_btc=size_btc,
                leverage=leverage,
                sl_price=sl,
                tp1_price=tp1,
                setup=ctype,
                regime=regime,
                atr_pct=atr_pct,
                best_price=price,
                worst_price=price,
                remaining_size=size_btc,
            )
            v.positions[tid] = pos

        # JSONL 진입 기록
        _append_jsonl({
            "type": "lab_entry",
            "direction": direction,
            "setup": ctype,
            "price": round(price, 1),
            "atr_pct": round(atr_pct, 4),
            "variants": [
                {"name": v.name, "tp1": round(v.positions[max(v.positions.keys())].tp1_price, 1) if v.positions else 0,
                 "sl": round(v.positions[max(v.positions.keys())].sl_price, 1) if v.positions else 0}
                for v in self.variants if v.positions
            ],
        })

    async def check_positions(self, current_price: float):
        """가격 업데이트 → 각 variant 포지션 체크"""
        if current_price <= 0:
            return

        for v in self.variants:
            closed_ids = []
            for tid, pos in list(v.positions.items()):
                # best/worst 업데이트
                if pos.direction == "long":
                    pos.best_price = max(pos.best_price, current_price)
                    pos.worst_price = min(pos.worst_price, current_price)
                else:
                    pos.best_price = min(pos.best_price, current_price)
                    pos.worst_price = max(pos.worst_price, current_price)

                # 청산 체크
                reason = self._check_exit(pos, current_price)
                if reason:
                    await self._close_position(v, pos, current_price, reason)
                    closed_ids.append(tid)

            for tid in closed_ids:
                v.positions.pop(tid, None)

    def _check_exit(self, pos: LabPosition, price: float) -> str | None:
        """간단한 청산 로직 (SL/TP1/시간)"""
        # SL
        if pos.direction == "long" and price <= pos.sl_price:
            return "sl"
        if pos.direction == "short" and price >= pos.sl_price:
            return "sl"

        # TP1 (첫 도달)
        if not pos.tp1_hit:
            hit = (pos.direction == "long" and price >= pos.tp1_price) or \
                  (pos.direction == "short" and price <= pos.tp1_price)
            if hit:
                pos.tp1_hit = True
                pos.runner_mode = True
                # 50% 부분청산 실현
                pnl_pct = abs(pos.tp1_price - pos.entry_price) / pos.entry_price * pos.leverage * 100
                fee_pct = self.FEE_MAKER * 2 * pos.leverage * 100
                pos.realized_pnl = (pnl_pct - fee_pct) * 0.5  # 50% 분
                pos.remaining_size = pos.size_btc * 0.5
                # SL → 본절
                fee_offset = pos.entry_price * (self.FEE_MAKER * 2)
                if pos.direction == "long":
                    pos.sl_price = pos.entry_price + fee_offset
                else:
                    pos.sl_price = pos.entry_price - fee_offset

        # 러너 모드: trailing SL (ATR × variant mult 기반)
        if pos.runner_mode:
            trail = abs(pos.tp1_price - pos.entry_price) * 0.5  # TP1 거리의 50%
            if pos.direction == "long":
                trail_sl = pos.best_price - trail
                if price <= trail_sl and price > pos.sl_price:
                    return "runner_trail"
            else:
                trail_sl = pos.best_price + trail
                if price >= trail_sl and price < pos.sl_price:
                    return "runner_trail"

        # 시간 초과 (4시간)
        if pos.hold_minutes() > 240:
            return "time"

        return None

    async def _close_position(self, v: Variant, pos: LabPosition, exit_price: float, reason: str):
        """포지션 청산 + 성과 기록"""
        # PnL 계산
        if pos.direction == "long":
            raw_pnl_pct = (exit_price - pos.entry_price) / pos.entry_price * pos.leverage * 100
        else:
            raw_pnl_pct = (pos.entry_price - exit_price) / pos.entry_price * pos.leverage * 100

        fee_pct = self.FEE_MAKER * 2 * pos.leverage * 100
        remaining_ratio = pos.remaining_size / pos.size_btc if pos.size_btc > 0 else 1.0
        net_pnl_pct = (raw_pnl_pct - fee_pct) * remaining_ratio + pos.realized_pnl

        # 성과 기록
        v.trades += 1
        v.total_pnl_pct += net_pnl_pct
        if net_pnl_pct > 0:
            v.wins += 1

        # reach%, mae% 계산
        tp1_dist = abs(pos.tp1_price - pos.entry_price)
        if pos.direction == "long":
            best_move = pos.best_price - pos.entry_price
            mae = pos.entry_price - pos.worst_price
        else:
            best_move = pos.entry_price - pos.best_price
            mae = pos.worst_price - pos.entry_price
        reach_pct = best_move / tp1_dist * 100 if tp1_dist > 0 else 0
        mae_pct = mae / pos.entry_price * 100

        # AdaptiveParams 피딩
        if self._adaptive:
            try:
                await self._adaptive.record_trade({
                    "direction": pos.direction,
                    "pnl_pct": round(net_pnl_pct, 2),
                    "hold_min": round(pos.hold_minutes(), 1),
                    "exit_reason": f"lab_{reason}",
                    "tp1_reach_pct": round(reach_pct, 1),
                    "mae_pct": round(mae_pct, 4),
                    "time_to_first_profit_sec": 0,
                    "entry_atr": pos.atr_pct,
                    "entry_h1_trend": "unknown",
                    "entry_h4_trend": "unknown",
                    "regime": pos.regime,
                    "entry_ts": pos.entry_time,
                    "leverage": pos.leverage,
                })
            except Exception:
                pass

        # JSONL 기록
        _append_jsonl({
            "type": "lab_exit",
            "variant": v.name,
            "direction": pos.direction,
            "setup": pos.setup,
            "entry_price": round(pos.entry_price, 1),
            "exit_price": round(exit_price, 1),
            "pnl_pct": round(net_pnl_pct, 2),
            "exit_reason": reason,
            "hold_min": round(pos.hold_minutes(), 0),
            "reach_pct": round(reach_pct, 1),
            "mae_pct": round(mae_pct, 4),
            "tp1_hit": pos.tp1_hit,
        })

    def get_best_variant(self) -> dict | None:
        """최근 성과 기준 최적 variant 반환 (최소 10건)"""
        valid = [v for v in self.variants if v.trades >= 10]
        if not valid:
            return None
        best = max(valid, key=lambda v: v.ev)
        return {
            "name": best.name,
            "atr_mult": best.atr_mult,
            "sl_margin_pct": best.sl_margin_pct,
            "ev": round(best.ev, 2),
            "win_rate": round(best.win_rate, 1),
            "trades": best.trades,
        }

    def get_stats(self) -> dict:
        """전체 variant 성과 (대시보드/텔레그램용)"""
        return {
            "variants": [
                {
                    "name": v.name,
                    "atr_mult": v.atr_mult,
                    "sl_pct": v.sl_margin_pct,
                    "trades": v.trades,
                    "win_rate": round(v.win_rate, 1),
                    "avg_pnl": round(v.avg_pnl, 2),
                    "ev": round(v.ev, 2),
                    "open_positions": len(v.positions),
                }
                for v in self.variants
            ],
            "best": self.get_best_variant(),
            "total_trades": sum(v.trades for v in self.variants),
        }

    @property
    def has_positions(self) -> bool:
        return any(v.positions for v in self.variants)

    @property
    def balance(self) -> float:
        """호환성: 총 PnL 기반 가상 잔고"""
        initial = 10000.0
        total_pnl = sum(v.total_pnl_pct for v in self.variants) / len(self.variants)
        return initial * (1 + total_pnl / 100)
