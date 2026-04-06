import logging
from src.utils.helpers import load_config

logger = logging.getLogger(__name__)

# 등급 정의
GRADES = [
    {"grade": "A+", "min_score": 9.0, "max_leverage": 30, "size_pct": 1.0, "execution": "market"},
    {"grade": "A",  "min_score": 8.0, "max_leverage": 25, "size_pct": 1.0, "execution": "market"},
    {"grade": "B+", "min_score": 7.5, "max_leverage": 20, "size_pct": 0.75, "execution": "limit"},
    {"grade": "B",  "min_score": 6.5, "max_leverage": 15, "size_pct": 0.50, "execution": "limit"},
    {"grade": "B-", "min_score": 6.0, "max_leverage": 10, "size_pct": 0.30, "execution": "limit"},
]


class SignalGrader:
    """시그널 점수 → 등급 판정 + 필수 필터"""

    def __init__(self):
        self.config = load_config()
        self.risk_cfg = self.config["risk"]

    def grade(self, aggregated: dict, risk_state: dict = None) -> dict:
        """
        합산 결과 → 등급 판정.

        Args:
            aggregated: SignalAggregator 출력
            risk_state: 현재 리스크 상태
                {
                    'daily_pnl_pct': float,
                    'current_drawdown_pct': float,
                    'open_positions': int,
                    'same_direction_count': int,
                    'streak': int,
                    'cooldown_active': bool,
                    'funding_blackout': bool,
                    'has_same_symbol': bool,
                }

        Returns:
            {
                'grade': str,
                'tradeable': bool,
                'reject_reason': str | None,
                'max_leverage': int,
                'size_pct': float,
                'execution': str,
                'direction': str,
                'score': float,
            }
        """
        score = aggregated.get("score", 0)
        direction = aggregated.get("direction", "neutral")

        if risk_state is None:
            risk_state = {}

        # ── 필수 필터 체크 ──
        reject_reason = self._check_filters(aggregated, risk_state)
        if reject_reason:
            logger.info(f"진입 거부: {reject_reason} (점수: {score:.1f})")
            return {
                "grade": "D",
                "tradeable": False,
                "reject_reason": reject_reason,
                "max_leverage": 0,
                "size_pct": 0,
                "execution": "none",
                "direction": direction,
                "score": score,
            }

        # ── 등급 판정 ──
        matched_grade = None
        for g in GRADES:
            if score >= g["min_score"]:
                matched_grade = g
                break

        if not matched_grade:
            logger.info(f"등급 C/D: 점수 부족 ({score:.1f})")
            return {
                "grade": "C" if score >= 4.0 else "D",
                "tradeable": False,
                "reject_reason": "점수 부족",
                "max_leverage": 0,
                "size_pct": 0,
                "execution": "none",
                "direction": direction,
                "score": score,
            }

        # ── 등급별 필수조건 추가 체크 ──
        signals = aggregated.get("signals_detail", {})
        grade_name = matched_grade["grade"]

        if grade_name in ("A+", "A"):
            # A 이상: OB 또는 BB돌파 + 구조 일치 + 최소 3개 보조
            has_ob = signals.get("order_block", {}).get("strength", 0) > 0.5
            has_bb = signals.get("bollinger", {}).get("pattern") in ("squeeze", "band_walk")
            has_structure = signals.get("market_structure", {}).get("aligned", False)
            supporting = sum(
                1 for k, v in signals.items()
                if v.get("direction") == direction and v.get("strength", 0) > 0.3
                and k not in ("order_block", "bollinger", "market_structure", "atr")
            )
            if not ((has_ob or has_bb) and has_structure and supporting >= 3):
                # 조건 미달 → 한 단계 하향
                matched_grade = GRADES[2] if grade_name == "A+" else GRADES[2]  # B+
                grade_name = matched_grade["grade"]

        # 연패 시 레버리지 감소
        streak = risk_state.get("streak", 0)
        leverage_mult = 1.0
        if streak >= 5:
            leverage_mult = 0  # 매매 중단
        elif streak >= 3:
            leverage_mult = 0.5
        elif streak >= 2:
            leverage_mult = 0.7

        if leverage_mult == 0:
            return {
                "grade": grade_name,
                "tradeable": False,
                "reject_reason": f"연패 {streak}회 → 쿨다운",
                "max_leverage": 0,
                "size_pct": 0,
                "execution": "none",
                "direction": direction,
                "score": score,
            }

        max_lev = int(matched_grade["max_leverage"] * leverage_mult)
        max_lev = max(max_lev, self.risk_cfg["leverage_range"][0])

        result = {
            "grade": grade_name,
            "tradeable": True,
            "reject_reason": None,
            "max_leverage": max_lev,
            "size_pct": matched_grade["size_pct"],
            "execution": matched_grade["execution"],
            "direction": direction,
            "score": score,
        }

        logger.info(
            f"등급 판정: {grade_name} | {direction.upper()} | "
            f"점수: {score:.1f} | 레버리지: ~{max_lev}x | "
            f"사이즈: {matched_grade['size_pct']*100:.0f}%"
        )

        return result

    def _check_filters(self, aggregated: dict, risk_state: dict) -> str | None:
        """필수 필터 체크 → 실패 시 사유 반환"""
        direction = aggregated.get("direction", "neutral")

        if direction == "neutral":
            return "방향 미정"

        # 일일 손실 한도
        daily_pnl = risk_state.get("daily_pnl_pct", 0)
        if daily_pnl <= -self.risk_cfg["max_daily_loss"] * 100:
            return f"일일 손실 한도 초과 ({daily_pnl:.1f}%)"

        # 최대 드로다운
        drawdown = risk_state.get("current_drawdown_pct", 0)
        if drawdown >= self.risk_cfg["max_drawdown"] * 100:
            return f"최대 드로다운 초과 ({drawdown:.1f}%)"

        # 최대 동시 포지션
        positions = risk_state.get("open_positions", 0)
        if positions >= self.risk_cfg["max_positions"]:
            return f"최대 동시 포지션 ({positions}/{self.risk_cfg['max_positions']})"

        # 같은 방향 최대
        same_dir = risk_state.get("same_direction_count", 0)
        if same_dir >= self.risk_cfg["max_same_direction"]:
            return f"같은 방향 포지션 초과 ({same_dir})"

        # 쿨다운
        if risk_state.get("cooldown_active", False):
            return "연패 쿨다운 중"

        # 펀딩비 정산 블랙아웃
        if risk_state.get("funding_blackout", False):
            return "펀딩비 정산 15분 전"

        # 같은 심볼 중복
        if risk_state.get("has_same_symbol", False):
            return "같은 심볼 포지션 존재"

        # 수수료 필터: 기대수익 > 0.15%
        score = aggregated.get("score", 0)
        if score < 6.0:
            return "기대수익 부족"

        # 15m-1H 구조 정렬 체크
        ms = aggregated.get("signals_detail", {}).get("market_structure", {})
        if ms.get("trend") == "ranging":
            return "시장 구조 횡보 (방향 불명)"

        return None
