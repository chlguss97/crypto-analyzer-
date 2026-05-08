"""
SimTrader — 실거래 동일 로직 가상매매
실거래와 100% 같은 확신도/게이트/AdaptiveParams 적용.
돈 안 쓰고 "실전 켰으면 어땠을까?" 판단용.

PaperLab과의 차이:
  PaperLab = 게이트 없이 전부 진입 (파라미터 A/B 실험)
  SimTrader = 게이트 있는 실전 시뮬 (확신도별 성과 추적)
"""

import logging
import time
import math
from dataclasses import dataclass, field
from src.monitoring.trade_logger import _append_jsonl

logger = logging.getLogger(__name__)


@dataclass
class SimPosition:
    """가상 실전 포지션"""
    trade_id: int
    direction: str
    entry_price: float
    entry_time: float
    size_btc: float
    leverage: int
    sl_price: float
    tp1_price: float
    conviction: int
    conviction_mult: float
    regime: str
    h1_trend: str
    h4_trend: str
    atr_pct: float
    # 추적
    best_price: float = 0.0
    worst_price: float = 0.0
    tp1_hit: bool = False
    runner_mode: bool = False
    remaining_size: float = 0.0
    realized_pnl: float = 0.0

    def hold_minutes(self) -> float:
        return (time.time() - self.entry_time) / 60


class SimTrader:
    """실거래 동일 로직 가상매매"""

    FEE_MAKER = 0.0002

    def __init__(self, config: dict = None, adaptive=None):
        self.config = config or {}
        self._adaptive = adaptive
        self.position = None  # 1포지션 제한 (실거래와 동일)
        self._trade_counter = 0
        self._stats = {"total": 0, "wins": 0, "total_pnl": 0.0}

    async def try_entry(self, candidate: dict, conviction: int,
                        conviction_mult: float, sl_price: float,
                        tp1_price: float, leverage: int, margin: float,
                        regime: str, h1_trend: str, h4_trend: str):
        """실거래 _execute와 동일 조건으로 가상 진입"""
        if self.position is not None:
            return  # 포지션 1개 제한

        direction = candidate["direction"]
        price = candidate["price"]
        atr_pct = candidate.get("atr_pct", 0.3)

        # 사이즈 (실거래와 동일 계산)
        size_btc = margin * leverage / price if price > 0 else 0
        size_btc = max(math.floor(size_btc / 0.01) * 0.01, 0.01)

        self._trade_counter += 1
        self.position = SimPosition(
            trade_id=self._trade_counter,
            direction=direction,
            entry_price=price,
            entry_time=time.time(),
            size_btc=size_btc,
            leverage=leverage,
            sl_price=sl_price,
            tp1_price=tp1_price,
            conviction=conviction,
            conviction_mult=conviction_mult,
            regime=regime,
            h1_trend=h1_trend,
            h4_trend=h4_trend,
            atr_pct=atr_pct,
            best_price=price,
            worst_price=price,
            remaining_size=size_btc,
        )

        _append_jsonl({
            "type": "sim_entry",
            "direction": direction,
            "entry_price": round(price, 1),
            "sl_price": round(sl_price, 1),
            "tp1_price": round(tp1_price, 1),
            "leverage": leverage,
            "conviction": conviction,
            "conviction_mult": round(conviction_mult, 2),
            "size_btc": size_btc,
            "regime": regime,
            "h1_trend": h1_trend,
            "h4_trend": h4_trend,
        })

    async def check_position(self, current_price: float):
        """가격 업데이트 → SL/TP1/러너 체크"""
        if self.position is None or current_price <= 0:
            return

        pos = self.position

        # best/worst 추적
        if pos.direction == "long":
            pos.best_price = max(pos.best_price, current_price)
            pos.worst_price = min(pos.worst_price, current_price)
        else:
            pos.best_price = min(pos.best_price, current_price)
            pos.worst_price = max(pos.worst_price, current_price)

        reason = self._check_exit(pos, current_price)
        if reason:
            await self._close(pos, current_price, reason)

    def _check_exit(self, pos: SimPosition, price: float) -> str | None:
        """청산 조건 (실거래 position_manager와 동일 구조)"""
        # SL
        if pos.direction == "long" and price <= pos.sl_price:
            return "sl"
        if pos.direction == "short" and price >= pos.sl_price:
            return "sl"

        # TP1
        if not pos.tp1_hit:
            hit = (pos.direction == "long" and price >= pos.tp1_price) or \
                  (pos.direction == "short" and price <= pos.tp1_price)
            if hit:
                pos.tp1_hit = True
                pos.runner_mode = True
                pnl_pct = abs(pos.tp1_price - pos.entry_price) / pos.entry_price * pos.leverage * 100
                fee_pct = self.FEE_MAKER * 2 * pos.leverage * 100
                pos.realized_pnl = (pnl_pct - fee_pct) * 0.5
                pos.remaining_size = pos.size_btc * 0.5
                # SL → 본절
                fee_offset = pos.entry_price * (self.FEE_MAKER * 2)
                if pos.direction == "long":
                    pos.sl_price = pos.entry_price + fee_offset
                else:
                    pos.sl_price = pos.entry_price - fee_offset

        # 러너 트레일
        if pos.runner_mode:
            trail = abs(pos.tp1_price - pos.entry_price) * 0.5
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

    async def _close(self, pos: SimPosition, exit_price: float, reason: str):
        """가상 청산 + 데이터 기록"""
        if pos.direction == "long":
            raw_pnl = (exit_price - pos.entry_price) / pos.entry_price * pos.leverage * 100
        else:
            raw_pnl = (pos.entry_price - exit_price) / pos.entry_price * pos.leverage * 100

        fee_pct = self.FEE_MAKER * 2 * pos.leverage * 100
        remaining_ratio = pos.remaining_size / pos.size_btc if pos.size_btc > 0 else 1.0
        net_pnl = (raw_pnl - fee_pct) * remaining_ratio + pos.realized_pnl

        # 통계
        self._stats["total"] += 1
        self._stats["total_pnl"] += net_pnl
        if net_pnl > 0:
            self._stats["wins"] += 1

        # reach% / mae%
        tp1_dist = abs(pos.tp1_price - pos.entry_price)
        if pos.direction == "long":
            best_move = pos.best_price - pos.entry_price
            mae = pos.entry_price - pos.worst_price
        else:
            best_move = pos.entry_price - pos.best_price
            mae = pos.worst_price - pos.entry_price
        reach_pct = best_move / tp1_dist * 100 if tp1_dist > 0 else 0
        mae_pct = mae / pos.entry_price * 100

        # AdaptiveParams 피딩 (h1/h4/regime 포함 — 실거래와 동일 품질)
        if self._adaptive:
            try:
                await self._adaptive.record_trade({
                    "direction": pos.direction,
                    "pnl_pct": round(net_pnl, 2),
                    "hold_min": round(pos.hold_minutes(), 1),
                    "exit_reason": f"sim_{reason}",
                    "tp1_reach_pct": round(reach_pct, 1),
                    "mae_pct": round(mae_pct, 4),
                    "time_to_first_profit_sec": 0,
                    "entry_atr": pos.atr_pct,
                    "entry_h1_trend": pos.h1_trend,
                    "entry_h4_trend": pos.h4_trend,
                    "regime": pos.regime,
                    "entry_ts": pos.entry_time,
                    "leverage": pos.leverage,
                })
            except Exception:
                pass

        # JSONL
        _append_jsonl({
            "type": "sim_exit",
            "direction": pos.direction,
            "entry_price": round(pos.entry_price, 1),
            "exit_price": round(exit_price, 1),
            "pnl_pct": round(net_pnl, 2),
            "exit_reason": reason,
            "hold_min": round(pos.hold_minutes(), 0),
            "conviction": pos.conviction,
            "conviction_mult": round(pos.conviction_mult, 2),
            "h1_trend": pos.h1_trend,
            "h4_trend": pos.h4_trend,
            "regime": pos.regime,
            "reach_pct": round(reach_pct, 1),
            "mae_pct": round(mae_pct, 4),
            "tp1_hit": pos.tp1_hit,
        })

        logger.info(
            f"[SIM] {pos.direction.upper()} {reason} | "
            f"pnl={net_pnl:+.1f}% | conv={pos.conviction} | "
            f"h1={pos.h1_trend} h4={pos.h4_trend}"
        )

        self.position = None

    def get_stats(self) -> dict:
        wr = self._stats["wins"] / self._stats["total"] * 100 if self._stats["total"] > 0 else 0
        return {
            "total": self._stats["total"],
            "wins": self._stats["wins"],
            "win_rate": round(wr, 1),
            "total_pnl": round(self._stats["total_pnl"], 2),
            "has_position": self.position is not None,
        }
