"""
ScalpState — 단타 매매 상태 관리

Redis JSON 직렬화로 크래시 복구 지원.
"""

import json
import logging
import time
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

SCALP_REDIS_KEY = "scalp:state:BTC/USDT:USDT"


@dataclass
class ScalpState:
    # 포지션
    position: str = "flat"              # "flat", "long", "short"
    entry_price: float = 0.0
    entry_time: float = 0.0
    entry_size_btc: float = 0.0
    entry_order_id: str | None = None

    # 대기 신호 (두 조건 비동시 발생)
    pending_signal: str | None = None   # "long_wait_macd", "long_wait_srsi", "short_wait_macd", "short_wait_srsi"
    signal_candle_count: int = 0

    # SL 기준
    sl_pct: float = 1.5                 # ATR 기반 동적 계산

    # 누적 통계
    total_trades: int = 0
    total_pnl: float = 0.0
    winning_trades: int = 0
    losing_trades: int = 0

    # 안전
    peak_balance: float = 0.0
    last_trade_time: float = 0.0

    # 상태
    is_active: bool = False
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ScalpState":
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid_fields}
        return cls(**filtered)

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades * 100


async def save_scalp_state(redis, state: ScalpState):
    """Redis에 스캘프 상태 저장"""
    try:
        data = json.dumps(state.to_dict())
        await redis.set(SCALP_REDIS_KEY, data, ttl=86400)
    except Exception as e:
        logger.error(f"Scalp state 저장 실패: {e}")


async def load_scalp_state(redis) -> ScalpState | None:
    """Redis에서 스캘프 상태 복원"""
    try:
        data = await redis.get(SCALP_REDIS_KEY)
        if not data:
            return None
        return ScalpState.from_dict(json.loads(data))
    except Exception as e:
        logger.error(f"Scalp state 로드 실패: {e}")
        return None
