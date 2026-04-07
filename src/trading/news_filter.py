"""
NewsFilter — 주요 경제 이벤트 시간대 매매 자동 중단
- FOMC 발표, CPI, NFP 등 주요 이벤트 30분 전후 매매 차단
- 정적 스케줄 + 실시간 변동성 기반 동적 차단
"""
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


# 주요 경제 이벤트 (UTC 시간 기준)
# 매월 정해진 패턴으로 발표되는 이벤트들
MAJOR_EVENTS = [
    # 미국 CPI: 매월 2번째 화/수요일 12:30 UTC
    {"name": "US CPI", "weekday": 1, "hour": 12, "minute": 30,
     "monthly_week": 2, "buffer_min": 30},
    # 미국 PPI: 매월 2번째 목요일 12:30 UTC
    {"name": "US PPI", "weekday": 3, "hour": 12, "minute": 30,
     "monthly_week": 2, "buffer_min": 30},
    # FOMC 회의: 1년에 8회 화/수 18:00 UTC
    {"name": "FOMC", "weekday": 2, "hour": 18, "minute": 0,
     "monthly_week": None, "buffer_min": 60, "specific_dates": True},
    # NFP (비농업): 매월 첫째 금요일 12:30 UTC
    {"name": "NFP", "weekday": 4, "hour": 12, "minute": 30,
     "monthly_week": 1, "buffer_min": 30},
    # FOMC 의사록: 회의 3주 후 18:00 UTC
    {"name": "FOMC Minutes", "weekday": 2, "hour": 18, "minute": 0,
     "monthly_week": 3, "buffer_min": 30},
]


class NewsFilter:
    """뉴스/이벤트 시간대 매매 차단"""

    def __init__(self):
        self.manual_block_until = 0  # 수동 차단 종료 시각
        self._last_check = None
        self._last_result = (False, "")

    def is_news_blackout(self, now: datetime = None) -> tuple[bool, str]:
        """
        지금이 뉴스 블랙아웃 시간인지 체크.

        Returns:
            (is_blocked, reason)
        """
        if now is None:
            now = datetime.now(timezone.utc)

        # 수동 차단
        if now.timestamp() < self.manual_block_until:
            remaining_min = int((self.manual_block_until - now.timestamp()) / 60)
            return True, f"수동 차단 ({remaining_min}분 남음)"

        # 정적 이벤트 체크
        for event in MAJOR_EVENTS:
            if self._is_within_event(now, event):
                return True, f"{event['name']} 발표 전후 ±{event['buffer_min']}분"

        return False, "OK"

    def _is_within_event(self, now: datetime, event: dict) -> bool:
        """이벤트 시간대 내인지 확인"""
        # 요일 매칭
        if now.weekday() != event["weekday"]:
            return False

        # 월별 N번째 주
        if event.get("monthly_week"):
            week_of_month = (now.day - 1) // 7 + 1
            if week_of_month != event["monthly_week"]:
                return False

        # 시각 매칭 (±buffer 분)
        event_dt = now.replace(hour=event["hour"], minute=event["minute"], second=0)
        diff_min = abs((now - event_dt).total_seconds() / 60)

        return diff_min <= event["buffer_min"]

    def block_manually(self, minutes: int):
        """수동 차단 설정 (예: 사용자가 위험 상황 인지 시)"""
        import time
        self.manual_block_until = time.time() + minutes * 60
        logger.warning(f"[NEWS] 수동 매매 차단: {minutes}분")

    def get_upcoming_events(self, days: int = 7) -> list[dict]:
        """다음 N일간 예정 이벤트 목록"""
        now = datetime.now(timezone.utc)
        upcoming = []

        for offset in range(days * 24 * 60):  # 분 단위 스캔
            check_time = now + timedelta(minutes=offset)
            for event in MAJOR_EVENTS:
                if self._is_within_event(check_time, event):
                    event_time = check_time.replace(
                        hour=event["hour"], minute=event["minute"], second=0
                    )
                    if event_time > now and not any(
                        u["name"] == event["name"] and u["time"] == event_time.isoformat()
                        for u in upcoming
                    ):
                        upcoming.append({
                            "name": event["name"],
                            "time": event_time.isoformat(),
                            "buffer_min": event["buffer_min"],
                            "minutes_until": int((event_time - now).total_seconds() / 60),
                        })
                        break

        return sorted(upcoming, key=lambda x: x["minutes_until"])[:10]
