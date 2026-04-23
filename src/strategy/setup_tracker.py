"""
SetupTracker — 셋업 성과 추적 + 자동 비활성화

FlowEngine v1: "FLOW" 단일 셋업 추적
- 승률, RR, 평균 PnL, 거래 수
- 시간대별(hour) / 추세별(trend) / 방향별 성과
- 자동 비활성화: 10+ 거래 & 승률 < 35%

상태 저장: data/setup_tracker.json
"""

import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock as Lock  # 04-17: RLock 으로 변경 — get_summary → is_setup_enabled 재진입 deadlock 수정
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent.parent / "data"
DEFAULT_PATH = DATA_DIR / "setup_tracker.json"

# 자동 비활성 임계값
MIN_TRADES_FOR_DISABLE = 10
MIN_WIN_RATE = 0.35  # 35%

SETUP_NAMES = {"FLOW": "OrderFlow Engine"}


class SetupTracker:
    """셋업별 성과 추적 및 자동 비활성화"""

    def __init__(self, path: str = None):
        self._path = Path(path) if path else DEFAULT_PATH
        self._lock = Lock()

        # 셋업별 통계
        self._stats: dict[str, dict] = {}
        # 감지 로그 (최근 500건)
        self._detections: list[dict] = []
        # 매매 로그 (최근 500건)
        self._trades: list[dict] = []
        # 수동 오버라이드 (관리자가 강제 활성/비활성)
        self._manual_overrides: dict[str, bool] = {}

        self._init_stats()
        self.load()

    # ── 초기화 ──

    def _init_stats(self):
        """빈 통계 구조 생성"""
        for setup in SETUP_NAMES:
            if setup not in self._stats:
                self._stats[setup] = self._empty_stat()

    @staticmethod
    def _empty_stat() -> dict:
        return {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl_pct": 0.0,
            "total_pnl_usdt": 0.0,
            "total_rr": 0.0,
            "detections": 0,
            "by_hour": {},       # "0"~"23" -> {total, wins, pnl}
            "by_trend": {},      # "up"/"down"/"neutral" -> {total, wins, pnl}
            "by_regime": {},     # "trending_up"/"trending_down"/"ranging"/"volatile" -> {total, wins, pnl}
            "by_direction": {},  # "long"/"short" -> {total, wins, pnl}
            "recent_results": [],  # 최근 20건 PnL 리스트
            "avg_hold_min": 0.0,
            "last_trade_ts": 0,
        }

    # ── 감지 기록 ──

    def record_detection(self, setup: str, direction: str, score: float, price: float):
        """셋업 감지 시 호출 (진입 여부와 무관)"""
        if setup not in SETUP_NAMES:
            return

        ts = time.time()
        hour = datetime.now(timezone.utc).hour

        with self._lock:
            self._stats[setup]["detections"] += 1

            entry = {
                "ts": ts,
                "setup": setup,
                "direction": direction,
                "score": round(score, 2),
                "price": round(price, 2),
                "hour": hour,
                "entered": False,  # record_trade에서 매칭 시 True로 갱신
            }
            self._detections.append(entry)

            # 최근 500건만 유지
            if len(self._detections) > 500:
                self._detections = self._detections[-500:]

        logger.debug(f"[SetupTracker] 감지: {setup}({SETUP_NAMES[setup]}) "
                     f"{direction} score={score:.1f} @ ${price:.0f}")

    # ── 매매 결과 기록 ──

    def record_trade(self, setup: str, direction: str, pnl_pct: float,
                     pnl_usdt: float = 0.0, hold_min: float = 0.0,
                     exit_reason: str = "", trend: str = "neutral",
                     regime: str = "unknown"):
        """매매 종료 시 호출"""
        if setup not in SETUP_NAMES:
            logger.warning(f"[SetupTracker] 알 수 없는 셋업: {setup}")
            return

        ts = time.time()
        hour = datetime.now(timezone.utc).hour
        is_win = pnl_pct > 0

        with self._lock:
            stat = self._stats[setup]

            # 기본 통계
            stat["total"] += 1
            if is_win:
                stat["wins"] += 1
            else:
                stat["losses"] += 1
            stat["total_pnl_pct"] += pnl_pct
            stat["total_pnl_usdt"] += pnl_usdt
            stat["last_trade_ts"] = ts

            # 평균 보유 시간 (이동 평균)
            n = stat["total"]
            stat["avg_hold_min"] = round(
                stat["avg_hold_min"] * (n - 1) / n + hold_min / n, 1
            )

            # 최근 결과 (슬라이딩 윈도우 20)
            stat["recent_results"].append(round(pnl_pct, 3))
            if len(stat["recent_results"]) > 20:
                stat["recent_results"] = stat["recent_results"][-20:]

            # 시간대별
            h_key = str(hour)
            if h_key not in stat["by_hour"]:
                stat["by_hour"][h_key] = {"total": 0, "wins": 0, "pnl": 0.0}
            stat["by_hour"][h_key]["total"] += 1
            if is_win:
                stat["by_hour"][h_key]["wins"] += 1
            stat["by_hour"][h_key]["pnl"] = round(
                stat["by_hour"][h_key]["pnl"] + pnl_pct, 3
            )

            # 추세별
            if trend not in stat["by_trend"]:
                stat["by_trend"][trend] = {"total": 0, "wins": 0, "pnl": 0.0}
            stat["by_trend"][trend]["total"] += 1
            if is_win:
                stat["by_trend"][trend]["wins"] += 1
            stat["by_trend"][trend]["pnl"] = round(
                stat["by_trend"][trend]["pnl"] + pnl_pct, 3
            )

            # 레짐별
            if regime not in stat["by_regime"]:
                stat["by_regime"][regime] = {"total": 0, "wins": 0, "pnl": 0.0}
            stat["by_regime"][regime]["total"] += 1
            if is_win:
                stat["by_regime"][regime]["wins"] += 1
            stat["by_regime"][regime]["pnl"] = round(
                stat["by_regime"][regime]["pnl"] + pnl_pct, 3
            )

            # 방향별
            if direction not in stat["by_direction"]:
                stat["by_direction"][direction] = {"total": 0, "wins": 0, "pnl": 0.0}
            stat["by_direction"][direction]["total"] += 1
            if is_win:
                stat["by_direction"][direction]["wins"] += 1
            stat["by_direction"][direction]["pnl"] = round(
                stat["by_direction"][direction]["pnl"] + pnl_pct, 3
            )

            # 감지 로그에서 최근 매칭 항목 entered=True 갱신
            for det in reversed(self._detections):
                if (det["setup"] == setup and det["direction"] == direction
                        and not det["entered"] and ts - det["ts"] < 600):
                    det["entered"] = True
                    break

            # 매매 로그
            trade_entry = {
                "ts": ts,
                "setup": setup,
                "direction": direction,
                "pnl_pct": round(pnl_pct, 3),
                "pnl_usdt": round(pnl_usdt, 3),
                "hold_min": round(hold_min, 1),
                "exit_reason": exit_reason,
                "trend": trend,
                "regime": regime,
                "hour": hour,
            }
            self._trades.append(trade_entry)
            if len(self._trades) > 500:
                self._trades = self._trades[-500:]

        # 자동 저장 (매 거래마다)
        self.save()

        win_rate = stat["wins"] / stat["total"] * 100 if stat["total"] > 0 else 0
        logger.info(
            f"[SetupTracker] 기록: {setup}({SETUP_NAMES[setup]}) "
            f"{direction} PnL={pnl_pct:+.2f}% | "
            f"통산 {stat['total']}건 승률 {win_rate:.0f}%"
        )

    # ── 활성 여부 판정 ──

    def is_setup_enabled(self, setup: str) -> bool:
        """셋업 활성 여부 판정. False면 해당 셋업 진입 차단."""
        if setup not in SETUP_NAMES:
            return False

        # 수동 오버라이드 우선
        if setup in self._manual_overrides:
            return self._manual_overrides[setup]

        with self._lock:
            stat = self._stats[setup]
            total = stat["total"]

            # 최소 거래 수 미달 → 아직 판단 불가 → 활성 유지
            if total < MIN_TRADES_FOR_DISABLE:
                return True

            win_rate = stat["wins"] / total
            avg_pnl = stat["total_pnl_pct"] / total if total > 0 else 0

            # 승률 낮아도 평균 PnL 양수면 활성 유지 (RR 좋은 전략)
            if win_rate < MIN_WIN_RATE and avg_pnl <= 0:
                logger.warning(
                    f"[SetupTracker] 셋업 {setup}({SETUP_NAMES[setup]}) 비활성 "
                    f"(승률 {win_rate:.0%} < {MIN_WIN_RATE:.0%}, "
                    f"avg_pnl={avg_pnl:+.2f}%, {total}건)"
                )
                return False

        return True

    def set_override(self, setup: str, enabled: bool):
        """수동으로 셋업 활성/비활성 강제 설정"""
        if setup in SETUP_NAMES:
            self._manual_overrides[setup] = enabled
            logger.info(f"[SetupTracker] 수동 오버라이드: {setup} = {'ON' if enabled else 'OFF'}")
            self.save()

    def clear_override(self, setup: str):
        """수동 오버라이드 제거 → 자동 판정으로 복귀"""
        self._manual_overrides.pop(setup, None)
        self.save()

    # ── 통계 요약 ──

    def get_summary(self) -> dict:
        """전체 통계 요약 반환"""
        summary = {}

        with self._lock:
            for setup, stat in self._stats.items():
                total = stat["total"]
                wins = stat["wins"]
                win_rate = wins / total if total > 0 else 0
                avg_pnl = stat["total_pnl_pct"] / total if total > 0 else 0

                # 최근 10건 승률
                recent = stat["recent_results"][-10:]
                recent_wr = len([r for r in recent if r > 0]) / len(recent) if recent else 0

                # 감지 대비 실행률
                detections = stat["detections"]
                exec_rate = total / detections if detections > 0 else 0

                # 최적/최악 시간대
                best_hour, worst_hour = None, None
                if stat["by_hour"]:
                    sorted_hours = sorted(
                        stat["by_hour"].items(),
                        key=lambda x: x[1]["pnl"], reverse=True
                    )
                    if sorted_hours:
                        best_hour = {"hour": int(sorted_hours[0][0]), **sorted_hours[0][1]}
                        worst_hour = {"hour": int(sorted_hours[-1][0]), **sorted_hours[-1][1]}

                enabled = self.is_setup_enabled(setup)
                override = self._manual_overrides.get(setup)

                summary[setup] = {
                    "name": SETUP_NAMES[setup],
                    "enabled": enabled,
                    "override": override,
                    "total": total,
                    "wins": wins,
                    "losses": stat["losses"],
                    "win_rate": round(win_rate, 3),
                    "avg_pnl_pct": round(avg_pnl, 3),
                    "total_pnl_pct": round(stat["total_pnl_pct"], 3),
                    "total_pnl_usdt": round(stat["total_pnl_usdt"], 2),
                    "avg_hold_min": stat["avg_hold_min"],
                    "detections": detections,
                    "exec_rate": round(exec_rate, 3),
                    "recent_win_rate": round(recent_wr, 3),
                    "best_hour": best_hour,
                    "worst_hour": worst_hour,
                    "by_trend": dict(stat["by_trend"]),
                    "by_regime": dict(stat.get("by_regime", {})),
                    "by_direction": dict(stat["by_direction"]),
                    "last_trade_ts": stat["last_trade_ts"],
                }

        return summary

    # ── 저장 / 로드 ──

    def save(self):
        """상태를 JSON 파일로 저장"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)

            with self._lock:
                data = {
                    "version": 1,
                    "saved_at": time.time(),
                    "stats": self._stats,
                    "detections": self._detections[-200:],  # 저장 시 200건으로 축소
                    "trades": self._trades[-200:],
                    "manual_overrides": self._manual_overrides,
                }

            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)

            logger.debug(f"[SetupTracker] 저장 완료: {self._path}")
        except Exception as e:
            logger.error(f"[SetupTracker] 저장 실패: {e}")

    def load(self):
        """JSON 파일에서 상태 복원"""
        if not self._path.exists():
            logger.info(f"[SetupTracker] 파일 없음, 새로 시작: {self._path}")
            return

        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)

            with self._lock:
                loaded_stats = data.get("stats", {})
                for setup in SETUP_NAMES:
                    if setup in loaded_stats:
                        # 기존 빈 stat에 로드된 값 머지
                        base = self._empty_stat()
                        base.update(loaded_stats[setup])
                        self._stats[setup] = base
                    # else: 이미 _init_stats에서 빈 것 생성됨

                self._detections = data.get("detections", [])
                self._trades = data.get("trades", [])
                self._manual_overrides = data.get("manual_overrides", {})

            total_trades = sum(s["total"] for s in self._stats.values())
            parts = " ".join(f"{k}={v['total']}" for k, v in self._stats.items())
            logger.info(f"[SetupTracker] 로드 완료: {total_trades}건 ({parts})")
        except Exception as e:
            logger.error(f"[SetupTracker] 로드 실패 (새로 시작): {e}")
            self._init_stats()

    # ── 유틸리티 ──

    def reset(self, setup: Optional[str] = None):
        """통계 리셋 (전체 또는 특정 셋업)"""
        with self._lock:
            if setup and setup in SETUP_NAMES:
                self._stats[setup] = self._empty_stat()
                logger.info(f"[SetupTracker] 셋업 {setup} 리셋")
            elif setup is None:
                self._stats = {}
                self._detections = []
                self._trades = []
                self._manual_overrides = {}
                self._init_stats()
                logger.info("[SetupTracker] 전체 리셋")
        self.save()

    def get_detection_vs_execution(self, setup: Optional[str] = None) -> dict:
        """감지 vs 실행 분석"""
        with self._lock:
            dets = self._detections
            if setup:
                dets = [d for d in dets if d["setup"] == setup]

            total_detected = len(dets)
            entered = sum(1 for d in dets if d["entered"])
            missed = total_detected - entered

            return {
                "total_detected": total_detected,
                "entered": entered,
                "missed": missed,
                "exec_rate": round(entered / total_detected, 3) if total_detected > 0 else 0,
            }
