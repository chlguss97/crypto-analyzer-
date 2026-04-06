import logging
from src.utils.helpers import load_config

logger = logging.getLogger(__name__)

# 등급별 최대 레버리지
GRADE_MAX_LEVERAGE = {
    "A+": 30,
    "A": 25,
    "B+": 20,
    "B": 15,
    "B-": 10,
}

# 연패별 배율 감소
STREAK_MULTIPLIER = {
    0: 1.0,
    1: 1.0,
    2: 0.7,
    3: 0.5,
    4: 0.5,
}


class LeverageCalculator:
    """동적 레버리지 계산: min(등급 상한, ATR 제한, 연패 제한)"""

    def __init__(self):
        self.config = load_config()
        self.risk_cfg = self.config["risk"]
        self.min_leverage = self.risk_cfg["leverage_range"][0]  # 10
        self.max_leverage = self.risk_cfg["leverage_range"][1]  # 30
        self.risk_per_trade = self.risk_cfg["risk_per_trade"]   # 0.005

    def calculate(self, grade: str, atr_pct: float, streak: int = 0) -> dict:
        """
        최종 레버리지 + 포지션 사이즈 계산.

        Args:
            grade: 등급 ('A+', 'A', 'B+', 'B', 'B-')
            atr_pct: ATR(14) 변동성 % (예: 0.3)
            streak: 연패 수

        Returns:
            {
                'leverage': int,
                'grade_limit': int,
                'atr_limit': int,
                'streak_limit': int,
                'sl_pct': float,
                'position_size_ratio': float,
            }
        """
        # 1. 등급 상한
        grade_limit = GRADE_MAX_LEVERAGE.get(grade, 10)

        # 2. ATR 기반 제한: risk_per_trade / atr_pct
        #    변동성 높으면 레버리지 ↓, 낮으면 ↑
        if atr_pct > 0:
            sl_pct = atr_pct * self.risk_cfg["sl_atr_multiplier"]  # ATR × 1.2
            sl_pct = max(self.risk_cfg["sl_min_pct"] * 100, min(self.risk_cfg["sl_max_pct"] * 100, sl_pct))
            atr_limit = int(self.risk_per_trade * 100 / sl_pct * 100)
        else:
            sl_pct = self.risk_cfg["sl_min_pct"] * 100
            atr_limit = self.max_leverage

        atr_limit = max(self.min_leverage, min(self.max_leverage, atr_limit))

        # 3. 연패 제한
        streak_mult = STREAK_MULTIPLIER.get(min(streak, 4), 0.5)
        streak_limit = int(grade_limit * streak_mult)
        streak_limit = max(self.min_leverage, streak_limit)

        # 최종: 세 가지 중 최소값
        final_leverage = min(grade_limit, atr_limit, streak_limit)
        final_leverage = max(self.min_leverage, final_leverage)

        # 포지션 사이즈 비율: 계좌 × risk / (SL% × leverage)
        #   = risk_per_trade / (sl_pct/100 × leverage) ... 의 계좌 대비 비율
        if sl_pct > 0 and final_leverage > 0:
            position_size_ratio = self.risk_per_trade / (sl_pct / 100)
        else:
            position_size_ratio = 0

        logger.debug(
            f"레버리지 계산: 등급({grade})={grade_limit}x | "
            f"ATR({atr_pct:.2f}%)={atr_limit}x | "
            f"연패({streak})={streak_limit}x → 최종: {final_leverage}x"
        )

        return {
            "leverage": final_leverage,
            "grade_limit": grade_limit,
            "atr_limit": atr_limit,
            "streak_limit": streak_limit,
            "sl_pct": round(sl_pct, 4),
            "position_size_ratio": round(position_size_ratio, 6),
        }

    def calculate_position_size(
        self, balance: float, leverage: int, sl_pct: float, size_pct: float = 1.0
    ) -> float:
        """
        포지션 사이즈 (USDT) 계산.

        Args:
            balance: 계좌 잔고 (USDT)
            leverage: 최종 레버리지
            sl_pct: SL 거리 %
            size_pct: 등급별 사이즈 비율 (A=1.0, B+=0.75 등)

        Returns:
            포지션 사이즈 (USDT, 마진 기준)
        """
        if sl_pct <= 0 or leverage <= 0:
            return 0

        # 1회 리스크 금액
        risk_amount = balance * self.risk_per_trade  # 계좌의 0.5%

        # 마진 = 리스크 / (SL% / 100)
        margin = risk_amount / (sl_pct / 100)

        # 등급별 사이즈 조절
        margin *= size_pct

        # 최대 마진 제한: 계좌의 30% 이내
        max_margin = balance * 0.3
        margin = min(margin, max_margin)

        logger.info(
            f"포지션 사이즈: 잔고 ${balance:.0f} × 리스크 0.5% = ${risk_amount:.2f} | "
            f"SL {sl_pct:.2f}% | 마진 ${margin:.2f} × {leverage}x = ${margin * leverage:.0f}"
        )

        return round(margin, 2)
