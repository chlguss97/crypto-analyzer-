"""
SignalTracker — 시그널별 기여도 추적
- 각 거래에서 어떤 시그널이 활성화됐는지 기록
- 청산 시 P&L을 활성 시그널들에 비례 분배
- 시그널별 누적 통계: 거래수, 승률, 평균 P&L, 기여도 점수
- 약한 시그널 자동 식별 → 가중치 조정 추천
"""
import json
import logging
import time
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)
DATA_DIR = Path(__file__).parent.parent.parent / "data"
TRACKER_PATH = DATA_DIR / "signal_tracker.json"


class SignalTracker:
    """시그널 기여도 추적기"""

    MIN_STRENGTH = 0.3  # 이 강도 이상이어야 "활성 시그널"로 카운트

    def __init__(self):
        # signal_name → {"trades": [], "wins": 0, "losses": 0, "total_pnl": 0, ...}
        self.stats = defaultdict(lambda: {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "best_pnl": 0.0,
            "worst_pnl": 0.0,
            "by_mode": {"swing": 0, "scalp": 0},
            "by_regime": {},
            "last_update": 0,
        })
        self.load()

    def record_trade(self, signals: dict, pnl_pct: float, mode: str = "scalp",
                     regime: str = "ranging"):
        """
        거래 결과를 활성 시그널들에 분배

        Args:
            signals: {"signal_name": {"direction": ..., "strength": ...}, ...}
            pnl_pct: 거래 손익률 (%)
            mode: "swing" | "scalp"
            regime: 마켓 레짐
        """
        if not signals:
            return

        # 활성 시그널만 추출 (강도 >= MIN_STRENGTH)
        active = {}
        for name, sig in signals.items():
            if not isinstance(sig, dict):
                continue
            strength = sig.get("strength", 0)
            direction = sig.get("direction", "neutral")
            if strength >= self.MIN_STRENGTH and direction != "neutral":
                active[name] = strength

        if not active:
            return

        # 가중치 정규화 (총합 1.0)
        total_strength = sum(active.values())
        weights = {name: s / total_strength for name, s in active.items()}

        # 각 시그널에 P&L 분배
        for name, weight in weights.items():
            allocated_pnl = pnl_pct * weight
            stat = self.stats[name]
            stat["trades"] += 1
            stat["total_pnl"] += allocated_pnl

            if pnl_pct > 0:
                stat["wins"] += 1
            else:
                stat["losses"] += 1

            stat["best_pnl"] = max(stat["best_pnl"], allocated_pnl)
            stat["worst_pnl"] = min(stat["worst_pnl"], allocated_pnl)

            # 모드별
            if mode in stat["by_mode"]:
                stat["by_mode"][mode] += 1

            # 레짐별
            if regime not in stat["by_regime"]:
                stat["by_regime"][regime] = {"trades": 0, "pnl": 0.0}
            stat["by_regime"][regime]["trades"] += 1
            stat["by_regime"][regime]["pnl"] += allocated_pnl

            stat["last_update"] = int(time.time() * 1000)

        # 10건마다 저장 (대시보드에서 빠르게 확인 가능)
        total_records = sum(s["trades"] for s in self.stats.values())
        if total_records % 10 == 0:
            self.save()

    def get_ranking(self) -> list:
        """시그널 랭킹 (기여도 점수 기준)"""
        ranking = []
        for name, stat in self.stats.items():
            if stat["trades"] < 5:
                continue  # 5건 미만은 통계 의미 없음

            wr = stat["wins"] / stat["trades"] if stat["trades"] > 0 else 0
            avg_pnl = stat["total_pnl"] / stat["trades"]

            # 기여도 점수: 평균 P&L × sqrt(거래수) × 승률
            # (거래수가 많을수록 신뢰도 ↑, sqrt로 완화)
            import math
            confidence = math.sqrt(stat["trades"]) / 10  # 100건이면 1.0
            score = avg_pnl * min(1.0, confidence) * (0.5 + wr * 0.5)

            ranking.append({
                "name": name,
                "trades": stat["trades"],
                "win_rate": round(wr * 100, 1),
                "total_pnl": round(stat["total_pnl"], 2),
                "avg_pnl": round(avg_pnl, 3),
                "best": round(stat["best_pnl"], 2),
                "worst": round(stat["worst_pnl"], 2),
                "contribution_score": round(score, 3),
                "by_mode": dict(stat["by_mode"]),
            })

        # 기여도 점수 내림차순 정렬
        ranking.sort(key=lambda x: -x["contribution_score"])
        return ranking

    def get_weak_signals(self, threshold: float = -0.1) -> list:
        """약한 시그널 자동 식별 (평균 P&L < threshold)"""
        weak = []
        for stat_item in self.get_ranking():
            if stat_item["trades"] >= 20 and stat_item["avg_pnl"] < threshold:
                weak.append({
                    "name": stat_item["name"],
                    "avg_pnl": stat_item["avg_pnl"],
                    "win_rate": stat_item["win_rate"],
                    "trades": stat_item["trades"],
                    "recommendation": "가중치 감소 또는 비활성화 권장",
                })
        return weak

    def get_strong_signals(self, threshold: float = 0.2) -> list:
        """강한 시그널 (평균 P&L > threshold)"""
        strong = []
        for stat_item in self.get_ranking():
            if stat_item["trades"] >= 20 and stat_item["avg_pnl"] > threshold:
                strong.append({
                    "name": stat_item["name"],
                    "avg_pnl": stat_item["avg_pnl"],
                    "win_rate": stat_item["win_rate"],
                    "trades": stat_item["trades"],
                    "recommendation": "가중치 강화 권장",
                })
        return strong

    def get_summary(self) -> dict:
        """전체 요약"""
        ranking = self.get_ranking()
        weak = self.get_weak_signals()
        strong = self.get_strong_signals()

        return {
            "total_signals_tracked": len(self.stats),
            "signals_with_data": len(ranking),
            "ranking": ranking,
            "weak_signals": weak,
            "strong_signals": strong,
            "last_update": int(time.time() * 1000),
        }

    def save(self):
        """JSON 파일로 원자적 저장 (temp + rename)"""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        import tempfile, shutil
        temp_path = None
        try:
            data = {name: dict(stat) for name, stat in self.stats.items()}
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=str(DATA_DIR),
                delete=False, suffix=".tmp"
            ) as f:
                temp_path = f.name
                json.dump(data, f, ensure_ascii=False, indent=2)
            shutil.move(temp_path, str(TRACKER_PATH))
        except Exception as e:
            logger.error(f"SignalTracker save error: {e}")
            if temp_path and Path(temp_path).exists():
                try:
                    Path(temp_path).unlink()
                except Exception:
                    pass

    def load(self):
        """JSON 파일에서 로드"""
        if not TRACKER_PATH.exists():
            return
        try:
            with open(TRACKER_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for name, stat in data.items():
                # defaultdict에 직접 할당
                self.stats[name] = stat
            logger.info(f"SignalTracker loaded: {len(self.stats)}개 시그널 추적 중")
        except Exception as e:
            logger.error(f"SignalTracker load error: {e}")

    def reset(self):
        """전체 리셋"""
        self.stats.clear()
        if TRACKER_PATH.exists():
            TRACKER_PATH.unlink()
        logger.info("SignalTracker 리셋 완료")
