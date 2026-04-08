import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 기법별 가중치
WEIGHTS = {
    "order_block": 3.0,
    "market_structure": 2.5,
    "fractal": 2.0,
    "bollinger": 2.0,
    "funding_rate": 2.0,
    "open_interest": 2.0,
    "rsi": 1.5,
    "volume": 1.5,
    "fvg": 1.5,
    "cvd": 1.5,
    "liquidation": 1.5,
    "ema": 1.0,
    "long_short_ratio": 1.0,
    "vwap": 1.0,
    "ml_prediction": 2.5,
}

# 컨플루언스 보너스 조건
CONFLUENCE_BONUSES = [
    {
        "name": "Golden Zone (OB + FVG)",
        "conditions": lambda s: (
            s.get("order_block", {}).get("strength", 0) > 0.5
            and s.get("fvg", {}).get("overlaps_ob", False)
        ),
        "bonus": 2.0,
    },
    {
        "name": "OB + VWAP 겹침",
        "conditions": lambda s: (
            s.get("order_block", {}).get("strength", 0) > 0.5
            and s.get("vwap", {}).get("touch_recent", False)
            and _zones_overlap(s)
        ),
        "bonus": 1.5,
    },
    {
        "name": "BB 스퀴즈 + 거래량 스파이크",
        "conditions": lambda s: (
            s.get("bollinger", {}).get("is_squeeze", False)
            and s.get("volume", {}).get("spike_ratio", 0) > 2.0
        ),
        "bonus": 1.5,
    },
    {
        "name": "RSI 극단 + BB 극단",
        "conditions": lambda s: s.get("rsi", {}).get("bb_rsi_combo", False),
        "bonus": 1.0,
    },
    {
        "name": "OI 급증 + BB 스퀴즈",
        "conditions": lambda s: (
            s.get("open_interest", {}).get("oi_spike", False)
            and s.get("bollinger", {}).get("is_squeeze", False)
        ),
        "bonus": 1.0,
    },
    {
        "name": "프랙탈 돌파 + Market Structure",
        "conditions": lambda s: (
            s.get("fractal", {}).get("breakout", "none") != "none"
            and s.get("market_structure", {}).get("aligned", False)
        ),
        "bonus": 1.5,
    },
    {
        "name": "프랙탈 클러스터 + OB",
        "conditions": lambda s: (
            s.get("fractal", {}).get("cluster_zone") is not None
            and s.get("order_block", {}).get("strength", 0) > 0.5
        ),
        "bonus": 1.0,
    },
]

# 정규화 기준: 한 방향 시그널의 현실적 최대값
# 12.0 = grader.GRADES 임계값 (A+=9.0, A=8.0, ..., 거부=6.0) 과 비례 호환
# 이전에 18.0 으로 올렸다가 grader 임계값 6.0 과 호환성 깨져 모든 진입 거부 → 12.0 복원
# 보너스 폭주 (점수 인플레이션) 는 MAX_CONFLUENCE_BONUS cap 으로만 제어
REALISTIC_MAX_SCORE = 12.0
# 보너스 cap — CONFLUENCE_BONUSES 합산 최대 9.5점이지만, 2~3개 동시 발동까지만 인정
# (BUG #4 잠재 위험 — 보너스 폭주로 약한 raw 가 A+ 등급 트리거하는 것 방지)
MAX_CONFLUENCE_BONUS = 3.0


def _zones_overlap(signals: dict) -> bool:
    """OB 영역과 VWAP이 겹치는지 체크"""
    import math
    ob = signals.get("order_block", {})
    vwap = signals.get("vwap", {})
    ob_zone = ob.get("ob_zone")
    vwap_price = vwap.get("session_vwap", 0)
    if not ob_zone or not vwap_price or len(ob_zone) < 2:
        return False
    try:
        zone_low = float(ob_zone[0])
        zone_high = float(ob_zone[1])
        vp = float(vwap_price)
        if math.isnan(zone_low) or math.isnan(zone_high) or math.isnan(vp):
            return False
        return zone_low <= vp <= zone_high
    except (TypeError, ValueError):
        return False


class SignalAggregator:
    """시그널 가중 합산 + 컨플루언스 보너스"""

    def aggregate(
        self,
        fast_signals: dict,
        slow_signals: dict,
        ml_prediction: Optional[dict] = None,
    ) -> dict:
        """
        모든 시그널을 합산하여 최종 점수 + 방향 산출.

        Returns:
            {
                'score': float,           # 정규화 점수 (0~10)
                'raw_score': float,       # 원시 가중합
                'direction': str,         # 'long' | 'short' | 'neutral'
                'confluence_bonus': float,
                'confluence_details': list,
                'signals_detail': dict,
                'long_score': float,
                'short_score': float,
            }
        """
        # 전체 시그널 합치기
        all_signals = {**fast_signals, **slow_signals}
        if ml_prediction:
            all_signals["ml_prediction"] = ml_prediction

        # 방향별 가중 점수 합산
        long_score = 0.0
        short_score = 0.0

        for sig_type, signal in all_signals.items():
            weight = WEIGHTS.get(sig_type, 0)
            if weight == 0:
                continue

            direction = signal.get("direction", "neutral")
            strength = signal.get("strength", 0)

            weighted = weight * strength

            if direction == "long":
                long_score += weighted
            elif direction == "short":
                short_score += weighted

        # 컨플루언스 보너스
        confluence_bonus = 0.0
        confluence_details = []

        for bonus_def in CONFLUENCE_BONUSES:
            try:
                if bonus_def["conditions"](all_signals):
                    confluence_bonus += bonus_def["bonus"]
                    confluence_details.append(bonus_def["name"])
            except Exception:
                pass

        # 보너스 cap (점수 인플레이션 방지)
        confluence_bonus = min(MAX_CONFLUENCE_BONUS, confluence_bonus)

        # 최종 방향 결정
        if long_score > short_score:
            direction = "long"
            raw_score = long_score + confluence_bonus
        elif short_score > long_score:
            direction = "short"
            raw_score = short_score + confluence_bonus
        else:
            direction = "neutral"
            raw_score = 0

        # 정규화 (0~10)
        score = raw_score / REALISTIC_MAX_SCORE * 10
        score = min(10.0, max(0.0, score))

        result = {
            "score": round(score, 2),
            "raw_score": round(raw_score, 2),
            "direction": direction,
            "confluence_bonus": round(confluence_bonus, 1),
            "confluence_details": confluence_details,
            "signals_detail": all_signals,
            "long_score": round(long_score, 2),
            "short_score": round(short_score, 2),
        }

        logger.info(
            f"합산 결과: {direction.upper()} | 점수: {score:.1f}/10 | "
            f"L:{long_score:.1f} S:{short_score:.1f} | 보너스: {confluence_bonus:.1f}"
        )

        return result
