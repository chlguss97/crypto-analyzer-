import logging
import time
from datetime import datetime, timezone
from src.data.storage import RedisClient
from src.utils.helpers import load_config

logger = logging.getLogger(__name__)

# 확신도 사이즈 시스템에서는 게이트 최소화 — 사이즈가 리스크 관리
BOT_KILL_DRAWDOWN_PCT = 20.0  # DD -20%: 봇 완전 정지 (유일한 DD 게이트)


class RiskManager:
    """리스크 관리: 봇킬 DD + 포지션/간격 제한 (확신도 사이즈가 주 리스크 관리)"""

    def __init__(self, redis_client: RedisClient, executor=None):
        self.redis = redis_client
        self.executor = executor  # OKX 잔고 실시간 동기화용
        self.config = load_config()
        self.risk_cfg = self.config["risk"]
        self.cooldown_cfg = self.config["cooldown"]
        self._last_balance_sync = 0  # 잔고 동기화 throttle

        iso = datetime.now(timezone.utc).isocalendar()
        self._state = {
            "daily_pnl_pct": 0.0,
            "weekly_pnl_pct": 0.0,
            "peak_balance": 0.0,
            "current_balance": 0.0,
            "streak": 0,
            "cooldown_until": 0,
            "trade_count_today": 0,
            "current_week": (iso[0], iso[1]),  # (year, week) 연도+주차
        }

    async def initialize(self, balance: float):
        """초기 잔고로 상태 초기화 — Redis 우선, 없으면 파일 백업에서 복원"""
        self._state["peak_balance"] = balance
        self._state["current_balance"] = balance

        # Redis에서 복원 시도
        streak = await self.redis.get("risk:streak")
        daily_pnl = await self.redis.get("risk:daily_pnl")

        if streak or daily_pnl:
            # Redis에 데이터 있음
            if streak:
                self._state["streak"] = int(streak)
            if daily_pnl:
                self._state["daily_pnl_pct"] = float(daily_pnl)
            weekly_pnl = await self.redis.get("risk:weekly_pnl")
            if weekly_pnl:
                self._state["weekly_pnl_pct"] = float(weekly_pnl)
            cooldown = await self.redis.get("risk:cooldown_until")
            if cooldown:
                self._state["cooldown_until"] = int(cooldown)
            logger.info(f"리스크 상태 Redis 복원: streak={self._state['streak']} daily={self._state['daily_pnl_pct']:.2f}%")
        else:
            # Redis 비어있음 → 파일 백업에서 복원
            backup = self._load_backup()
            if backup:
                self._state["streak"] = backup.get("streak", 0)
                self._state["daily_pnl_pct"] = backup.get("daily_pnl_pct", 0.0)
                self._state["weekly_pnl_pct"] = backup.get("weekly_pnl_pct", 0.0)
                self._state["cooldown_until"] = backup.get("cooldown_until", 0)
                logger.warning(f"Redis 비어있음 → 파일 백업 복원: streak={self._state['streak']} daily={self._state['daily_pnl_pct']:.2f}%")
                # Redis에도 동기화
                await self.redis.set("risk:streak", str(self._state["streak"]))
                await self.redis.set("risk:daily_pnl", str(self._state["daily_pnl_pct"]))
                await self.redis.set("risk:weekly_pnl", str(self._state["weekly_pnl_pct"]))

        logger.info(
            f"리스크 매니저 초기화: 잔고 ${balance:.2f} | "
            f"연패 {self._state['streak']} | 일일 P&L {self._state['daily_pnl_pct']:.2f}%"
        )

    def _save_backup(self):
        """파일 백업 — Redis 유실 대비"""
        try:
            import json
            from pathlib import Path
            backup_path = Path(__file__).parent.parent.parent / "data" / "risk_state.json"
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            with open(backup_path, "w") as f:
                json.dump({
                    "streak": self._state["streak"],
                    "daily_pnl_pct": round(self._state["daily_pnl_pct"], 4),
                    "weekly_pnl_pct": round(self._state["weekly_pnl_pct"], 4),
                    "cooldown_until": self._state["cooldown_until"],
                    "ts": int(time.time()),
                }, f)
        except Exception:
            pass  # 백업 실패가 매매를 막지 않음

    def _load_backup(self) -> dict | None:
        """파일 백업에서 복원"""
        try:
            import json
            from pathlib import Path
            backup_path = Path(__file__).parent.parent.parent / "data" / "risk_state.json"
            if not backup_path.exists():
                return None
            with open(backup_path) as f:
                data = json.load(f)
            # 24시간 이상 된 백업은 무시 (stale)
            if time.time() - data.get("ts", 0) > 86400:
                logger.info("리스크 백업 24시간 초과 → 무시")
                return None
            return data
        except Exception:
            return None

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
        # 주차 변경 체크 (연도+주차)
        iso = datetime.now(timezone.utc).isocalendar()
        current_week = (iso[0], iso[1])
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

        # Redis + 파일 동기화 (Redis 재시작 시 파일에서 복원)
        await self.redis.set("risk:streak", str(self._state["streak"]))
        await self.redis.set("risk:daily_pnl", str(round(self._state["daily_pnl_pct"], 4)))
        await self.redis.set("risk:weekly_pnl", str(round(self._state["weekly_pnl_pct"], 4)))
        await self.redis.set("risk:cooldown_until", str(self._state["cooldown_until"]))
        self._save_backup()

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
        self._state["streak"] = 0
        self._state["cooldown_until"] = 0
        await self.redis.set("risk:daily_pnl", "0")
        await self.redis.set("risk:trade_count_today", "0")
        await self.redis.set("risk:streak", "0")
        await self.redis.set("risk:cooldown_until", "0")
        logger.info("일일 리스크 카운터 리셋 (streak/cooldown 포함)")

    def get_streak(self) -> int:
        """현재 연패 수 반환"""
        return self._state["streak"]

    def get_daily_pnl(self) -> float:
        """일일 P&L % 반환"""
        return self._state["daily_pnl_pct"]

    def get_cooldown_until(self) -> float:
        """쿨다운 종료 시각(epoch) 반환"""
        return self._state["cooldown_until"]

    def get_trade_count_today(self) -> int:
        """오늘 매매 횟수 반환"""
        return self._state["trade_count_today"]

    def is_trading_allowed(self) -> tuple[bool, str]:
        """매매 가능 여부 판단 — 봇킬 DD만 체크 (나머지는 확신도 사이즈가 관리)"""
        # 봇 킬: DD -20% → 완전 정지
        peak = self._state["peak_balance"]
        current = self._state["current_balance"]
        if peak > 0 and (peak - current) / peak >= BOT_KILL_DRAWDOWN_PCT / 100:
            return False, f"봇 킬 DD (-{BOT_KILL_DRAWDOWN_PCT}%)"

        return True, "OK"
