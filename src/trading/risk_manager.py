import logging
import time
from datetime import datetime, timezone
from src.data.storage import RedisClient
from src.utils.helpers import load_config

logger = logging.getLogger(__name__)

# 실거래 손실 한도 (사용자 지정)
MAX_DAILY_LOSS_PCT = 10.0   # 일일 -10%
MAX_WEEKLY_LOSS_PCT = 20.0  # 주간 -20%


class RiskManager:
    """리스크 관리: 일일/주간 한도, 드로다운, 연패 쿨다운"""

    def __init__(self, redis_client: RedisClient, executor=None):
        self.redis = redis_client
        self.executor = executor  # OKX 잔고 실시간 동기화용
        self.config = load_config()
        self.risk_cfg = self.config["risk"]
        self.cooldown_cfg = self.config["cooldown"]
        self._last_balance_sync = 0  # 잔고 동기화 throttle

        self._state = {
            "daily_pnl_pct": 0.0,
            "weekly_pnl_pct": 0.0,
            "peak_balance": 0.0,
            "current_balance": 0.0,
            "streak": 0,
            "cooldown_until": 0,
            "trade_count_today": 0,
            "current_week": datetime.now(timezone.utc).isocalendar()[1],
        }

    async def initialize(self, balance: float):
        """초기 잔고로 상태 초기화"""
        self._state["peak_balance"] = balance
        self._state["current_balance"] = balance

        streak = await self.redis.get("risk:streak")
        if streak:
            self._state["streak"] = int(streak)
        daily_pnl = await self.redis.get("risk:daily_pnl")
        if daily_pnl:
            self._state["daily_pnl_pct"] = float(daily_pnl)
        weekly_pnl = await self.redis.get("risk:weekly_pnl")
        if weekly_pnl:
            self._state["weekly_pnl_pct"] = float(weekly_pnl)
        cooldown = await self.redis.get("risk:cooldown_until")
        if cooldown:
            self._state["cooldown_until"] = int(cooldown)

        logger.info(
            f"리스크 매니저 초기화: 잔고 ${balance:.2f} | "
            f"연패 {self._state['streak']} | 일일 P&L {self._state['daily_pnl_pct']:.2f}%"
        )

    async def get_risk_state(self, open_positions: list = None) -> dict:
        """현재 리스크 상태 조회 — 30초마다 OKX 실잔고 재동기화"""
        if open_positions is None:
            open_positions = []

        now = int(time.time())
        cooldown_active = now < self._state["cooldown_until"]

        # OKX 실제 잔고 동기화 (외부 입금/출금/펀딩비/타거래 반영)
        # throttle: 30초마다 1회 (rate limit + 호출 횟수 절약)
        if self.executor and (now - self._last_balance_sync) >= 30:
            try:
                live_balance = await self.executor.get_balance()
                if live_balance > 0:
                    self._state["current_balance"] = live_balance
                    if live_balance > self._state["peak_balance"]:
                        self._state["peak_balance"] = live_balance
                self._last_balance_sync = now
            except Exception as e:
                logger.debug(f"OKX 잔고 동기화 실패: {e}")

        # 드로다운 계산
        peak = self._state["peak_balance"]
        current = self._state["current_balance"]
        drawdown_pct = ((peak - current) / peak * 100) if peak > 0 else 0

        # 같은 방향 포지션 수
        long_count = sum(1 for p in open_positions if p.get("direction") == "long")
        short_count = sum(1 for p in open_positions if p.get("direction") == "short")

        return {
            "daily_pnl_pct": self._state["daily_pnl_pct"],
            "current_drawdown_pct": drawdown_pct,
            "open_positions": len(open_positions),
            "same_direction_count": max(long_count, short_count),
            "streak": self._state["streak"],
            "cooldown_active": cooldown_active,
            "funding_blackout": False,  # 외부에서 설정
            "has_same_symbol": False,   # 외부에서 설정
            "trade_count_today": self._state["trade_count_today"],
            "balance": self._state["current_balance"],
            "peak_balance": self._state["peak_balance"],
        }

    async def record_trade_result(self, pnl_pct: float, pnl_usdt: float):
        """매매 결과 기록 → 연패/쿨다운/일일·주간 P&L 갱신"""
        # 주차 변경 체크
        current_week = datetime.now(timezone.utc).isocalendar()[1]
        if current_week != self._state["current_week"]:
            self._state["weekly_pnl_pct"] = 0.0
            self._state["current_week"] = current_week
            logger.info("[RISK] 주간 P&L 리셋")

        # 일일/주간 P&L
        self._state["daily_pnl_pct"] += pnl_pct
        self._state["weekly_pnl_pct"] += pnl_pct
        self._state["trade_count_today"] += 1

        # 잔고 갱신
        self._state["current_balance"] += pnl_usdt
        if self._state["current_balance"] > self._state["peak_balance"]:
            self._state["peak_balance"] = self._state["current_balance"]

        # 연패 추적
        if pnl_pct < 0:
            self._state["streak"] += 1
            logger.warning(f"손실 기록: {pnl_pct:+.2f}% | 연패: {self._state['streak']}")

            # 쿨다운 설정
            now = int(time.time())
            if self._state["streak"] >= 5:
                cooldown_sec = self.cooldown_cfg["streak_5_min"] * 60
                self._state["cooldown_until"] = now + cooldown_sec
                logger.warning(f"5연패 → {self.cooldown_cfg['streak_5_min']}분 쿨다운")
            elif self._state["streak"] >= 3:
                cooldown_sec = self.cooldown_cfg["streak_3_min"] * 60
                self._state["cooldown_until"] = now + cooldown_sec
                logger.warning(f"3연패 → {self.cooldown_cfg['streak_3_min']}분 쿨다운")
        else:
            self._state["streak"] = 0
            logger.info(f"수익 기록: {pnl_pct:+.2f}% | 연패 리셋")

        # Redis 동기화
        await self.redis.set("risk:streak", str(self._state["streak"]))
        await self.redis.set("risk:daily_pnl", str(round(self._state["daily_pnl_pct"], 4)))
        await self.redis.set("risk:weekly_pnl", str(round(self._state["weekly_pnl_pct"], 4)))
        await self.redis.set("risk:cooldown_until", str(self._state["cooldown_until"]))

        # 일일 손실 한도 (-10%)
        if self._state["daily_pnl_pct"] <= -MAX_DAILY_LOSS_PCT:
            logger.error(
                f"[RISK] 일일 손실 한도 -10% 도달: {self._state['daily_pnl_pct']:.2f}% → 당일 매매 중단"
            )

        # 주간 손실 한도 (-20%)
        if self._state["weekly_pnl_pct"] <= -MAX_WEEKLY_LOSS_PCT:
            logger.error(
                f"[RISK] 주간 손실 한도 -20% 도달: {self._state['weekly_pnl_pct']:.2f}% → 주간 매매 중단"
            )

        # 최대 드로다운 체크
        peak = self._state["peak_balance"]
        current = self._state["current_balance"]
        drawdown = (peak - current) / peak if peak > 0 else 0
        if drawdown >= self.risk_cfg["max_drawdown"]:
            logger.error(
                f"최대 드로다운 도달: {drawdown*100:.1f}% → 전체 매매 중단"
            )

    async def reset_daily(self):
        """일일 카운터 리셋 (매일 00:00 UTC)"""
        self._state["daily_pnl_pct"] = 0.0
        self._state["trade_count_today"] = 0
        await self.redis.set("risk:daily_pnl", "0")
        await self.redis.set("risk:trade_count_today", "0")
        logger.info("일일 리스크 카운터 리셋")

    def is_trading_allowed(self) -> tuple[bool, str]:
        """매매 가능 여부 판단"""
        # 일일 손실 -10%
        if self._state["daily_pnl_pct"] <= -MAX_DAILY_LOSS_PCT:
            return False, f"일일 손실 한도 (-10%): {self._state['daily_pnl_pct']:.2f}%"

        # 주간 손실 -20%
        if self._state["weekly_pnl_pct"] <= -MAX_WEEKLY_LOSS_PCT:
            return False, f"주간 손실 한도 (-20%): {self._state['weekly_pnl_pct']:.2f}%"

        # 드로다운
        peak = self._state["peak_balance"]
        current = self._state["current_balance"]
        if peak > 0 and (peak - current) / peak >= self.risk_cfg["max_drawdown"]:
            return False, "최대 드로다운"

        # 쿨다운
        if int(time.time()) < self._state["cooldown_until"]:
            remaining = self._state["cooldown_until"] - int(time.time())
            return False, f"쿨다운 중 ({remaining//60}분 남음)"

        return True, "OK"
