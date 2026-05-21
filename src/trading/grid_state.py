"""
GridState — 그리드 트레이딩 상태 관리

데이터 구조:
  GridLevel: 개별 그리드 레벨 (주문 + counter-order + 사이클 추적)
  GridState: 전체 그리드 상태 (레벨들 + 메타데이터)

Redis 직렬화: JSON으로 저장/복원 (크래시 복구용)
"""

import json
import logging
import time
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

GRID_REDIS_KEY = "grid:state:BTC/USDT:USDT"


@dataclass
class GridLevel:
    level_id: int               # -2,-1,+1,+2 (음수=buy, 양수=sell)
    side: str                   # "buy" / "sell"
    price: float                # 그리드 주문 가격
    order_id: str | None = None
    status: str = "pending"     # pending/placed/filled/cancelled
    fill_price: float = 0.0
    fill_time: float = 0.0
    counter_order_id: str | None = None
    counter_status: str = "none"  # none/placed/filled
    counter_fill_price: float = 0.0
    cycle_count: int = 0
    cycle_pnl: float = 0.0


@dataclass
class GridState:
    center_price: float = 0.0
    spacing_pct: float = 0.0
    spacing_abs: float = 0.0
    levels: dict = field(default_factory=dict)  # level_id(int) -> GridLevel
    total_cycles: int = 0
    total_pnl: float = 0.0
    is_active: bool = False
    pause_reason: str | None = None
    created_at: float = 0.0
    last_rebalance: float = 0.0

    def to_dict(self) -> dict:
        d = {
            "center_price": self.center_price,
            "spacing_pct": self.spacing_pct,
            "spacing_abs": self.spacing_abs,
            "total_cycles": self.total_cycles,
            "total_pnl": round(self.total_pnl, 4),
            "is_active": self.is_active,
            "pause_reason": self.pause_reason,
            "created_at": self.created_at,
            "last_rebalance": self.last_rebalance,
            "levels": {},
        }
        for lid, lv in self.levels.items():
            d["levels"][str(lid)] = asdict(lv)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "GridState":
        gs = cls(
            center_price=d.get("center_price", 0),
            spacing_pct=d.get("spacing_pct", 0),
            spacing_abs=d.get("spacing_abs", 0),
            total_cycles=d.get("total_cycles", 0),
            total_pnl=d.get("total_pnl", 0),
            is_active=d.get("is_active", False),
            pause_reason=d.get("pause_reason"),
            created_at=d.get("created_at", 0),
            last_rebalance=d.get("last_rebalance", 0),
        )
        for lid_str, lv_dict in d.get("levels", {}).items():
            lid = int(lid_str)
            gs.levels[lid] = GridLevel(**lv_dict)
        return gs


async def save_grid_state(redis, state: GridState):
    """Redis에 그리드 상태 저장"""
    try:
        data = json.dumps(state.to_dict())
        await redis.set(GRID_REDIS_KEY, data, ttl=86400)
    except Exception as e:
        logger.error(f"Grid state 저장 실패: {e}")


async def load_grid_state(redis) -> GridState | None:
    """Redis에서 그리드 상태 복원"""
    try:
        data = await redis.get(GRID_REDIS_KEY)
        if not data:
            return None
        return GridState.from_dict(json.loads(data))
    except Exception as e:
        logger.error(f"Grid state 로드 실패: {e}")
        return None
