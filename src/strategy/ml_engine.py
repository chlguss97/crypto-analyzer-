"""
ML DecisionEngine — Meta-Label Go/NoGo 분류기

역할: "이 후보를 실행하면 돈을 버는가?" → Yes(1) / No(0) 이진 분류
근거: Lopez de Prado (2018) "Advances in Financial Machine Learning"
      - Meta-Labeling: 방향은 시그널이 결정, ML은 품질만 판단
      - Triple Barrier: TP/SL/Time 중 먼저 도달한 것으로 라벨링

Phase A (< min_samples): 룰 기반 필터 (CVD + vol)
Phase B (>= min_samples): GBM 분류기 P(Win) > threshold → Go
"""

import json
import logging
import time
import pickle
import numpy as np
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent.parent / "data"
MODEL_PATH = DATA_DIR / "ml_meta_label.pkl"

# 핵심 8 피처 이름 (순서 고정)
CORE_FEATURES = [
    "price_momentum",    # 5봉 변동%
    "trend_strength",    # (EMA8-EMA21)/ATR
    "cvd_norm",          # CVD/거래량 (-1~+1)
    "cvd_matches",       # CVD 방향 일치 (0/1)
    "vol_ratio",         # 거래량/평균
    "adx",               # 추세 강도
    "bb_position",       # BB 내 위치
    "hour_sin",          # 시간대
]

# 확장 피처 (500건 이후 추가)
EXTENDED_FEATURES = CORE_FEATURES + [
    "price_change_15m",
    "price_change_1h",
    "cvd_15m_norm",
    "whale_bias",
    "liq_pressure",
    "atr_pct",
    "vol_trend",
    "hour_cos",
    "candle_body_ratio",
    "price_vs_ema50",
    "vol_ratio_1m",
    # 마이크로스트럭처 (binance_stream 2초 갱신)
    "micro_trade_rate",
    "micro_burst",
    "micro_bs_30s",
    "micro_absorption_score",
    "micro_whale_cluster",
    "micro_delta_accel",
    "micro_price_impact",
    "micro_vwap_dev",
    "micro_delta_div",
    "micro_momentum_quality",
]


class MLDecisionEngine:
    """
    ML 기반 Go/NoGo 결정 엔진.
    Phase A: 룰 필터 (데이터 부족)
    Phase B: GBM 분류기
    """

    def __init__(self, config=None):
        ml_cfg = (config or {}).get("ml", {})
        self.min_samples = ml_cfg.get("phase_a_min_samples", 200)
        self.retrain_interval = ml_cfg.get("retrain_interval", 100)
        self.window_size = ml_cfg.get("window_size", 500)
        self.min_oos_accuracy = ml_cfg.get("min_oos_accuracy", 0.52)
        self.go_threshold = ml_cfg.get("go_threshold", 0.55)
        self.expanded_features_at = ml_cfg.get("expanded_features_at", 500)

        # 알림 콜백 (텔레그램 등 — main.py에서 설정)
        self.on_phase_change = None  # async def(old_phase, new_phase, details_str)

        self.model = None
        self.scaler = None
        self.trained = False
        self.phase = "A"  # "A" = 룰, "B" = ML
        self.oos_accuracy = 0.0
        self.train_accuracy = 0.0
        self.total_labeled = 0
        self.last_train_count = 0
        self.feature_names = list(CORE_FEATURES)
        self.consecutive_bad_oos = 0

        # 최근 성과 추적
        self.recent_decisions = deque(maxlen=50)  # (go, actual_label)

        self._load()

    # ════════════════════════════════════════
    #  결정 (Go / NoGo)
    # ════════════════════════════════════════

    def decide(self, features_raw: dict) -> tuple[bool, float]:
        """
        진입 여부 결정.

        Args:
            features_raw: CandidateDetector._build_raw_features()의 출력

        Returns:
            (go: bool, probability: float)
            go=True → 진입, go=False → NoGo (shadow 추적)
            probability: P(Win) 또는 룰 기반 시 -1.0
        """
        if self.phase == "A":
            return self._decide_rule_based(features_raw)
        else:
            return self._decide_ml(features_raw)

    def _decide_rule_based(self, features: dict) -> tuple[bool, float]:
        """Phase A: 룰 기반 Go/NoGo (CVD 일치 + 거래량 평균 이상)"""
        cvd_matches = features.get("cvd_matches", 0)
        vol_ratio = features.get("vol_ratio", 0)

        go = (cvd_matches == 1) and (vol_ratio > 1.0)
        return go, -1.0  # 룰 기반이므로 확률 없음

    def _decide_ml(self, features_raw: dict) -> tuple[bool, float]:
        """Phase B: ML Go/NoGo"""
        if self.model is None:
            return self._decide_rule_based(features_raw)

        try:
            x = self._extract_feature_vector(features_raw)
            if self.scaler:
                x = self.scaler.transform([x])
            else:
                x = [x]

            prob = float(self.model.predict_proba(x)[0][1])  # P(Win)
            go = prob > self.go_threshold
            return go, prob

        except Exception as e:
            logger.warning(f"ML predict 실패 → 룰 폴백: {e}")
            return self._decide_rule_based(features_raw)

    def _extract_feature_vector(self, features_raw: dict) -> list[float]:
        """features_raw dict → 모델 입력 벡터"""
        return [float(features_raw.get(name, 0)) for name in self.feature_names]

    # ════════════════════════════════════════
    #  학습
    # ════════════════════════════════════════

    def check_and_train(self, labeled_signals: list[dict]):
        """
        signals 테이블에서 라벨 확정된 데이터로 학습 여부 판단.

        Args:
            labeled_signals: DB에서 가져온 [{"features": json, "label": 0|1, "entry_executed": 0|1}, ...]
        """
        prev_labeled = self.total_labeled
        self.total_labeled = len(labeled_signals)

        # 마일스톤 알림 (100, 200, 500, 1000건)
        for ms in [100, 200, 500, 1000]:
            if prev_labeled < ms <= self.total_labeled:
                self._notify_phase_change(
                    self.phase, self.phase,
                    f"마일스톤: {ms}건 라벨 도달! (총 {self.total_labeled}건)"
                )

        # Phase A → B 전환 체크
        if self.total_labeled >= self.min_samples and self.phase == "A":
            logger.info(f"[ML] {self.total_labeled}건 도달 → Phase B 학습 시도")
            self._train(labeled_signals)
            return

        # Phase B: 재학습 주기 체크
        if self.phase == "B" and (self.total_labeled - self.last_train_count) >= self.retrain_interval:
            logger.info(f"[ML] 재학습 트리거: {self.total_labeled}건 (이전 {self.last_train_count})")
            self._train(labeled_signals)

    def _train(self, labeled_signals: list[dict]):
        """GBM 학습 + Walk-Forward 검증"""
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.preprocessing import StandardScaler

        # 데이터 준비
        X, y, weights = [], [], []

        # 피처 확장 시점 체크
        if self.total_labeled >= self.expanded_features_at:
            self.feature_names = list(EXTENDED_FEATURES)
            logger.info(f"[ML] 피처 확장: {len(CORE_FEATURES)} → {len(EXTENDED_FEATURES)}")
        else:
            self.feature_names = list(CORE_FEATURES)

        for sig in labeled_signals[-self.window_size:]:
            try:
                features = json.loads(sig["features"]) if isinstance(sig["features"], str) else sig["features"]
                vec = [float(features.get(name, 0)) for name in self.feature_names]
                label = int(sig["label"])
                X.append(vec)
                y.append(label)
                # 실 진입 데이터에 더 높은 가중치
                w = 2.0 if sig.get("entry_executed", 0) == 1 else 1.0
                weights.append(w)
            except Exception as e:
                logger.debug(f"[ML] 데이터 파싱 실패: {e}")
                continue

        if len(X) < self.min_samples:
            logger.info(f"[ML] 학습 데이터 부족: {len(X)} < {self.min_samples}")
            return

        X = np.array(X, dtype=np.float64)
        y = np.array(y)
        weights = np.array(weights)

        # NaN/Inf 처리
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

        # Walk-Forward: 80% 학습, 20% OOS 검증
        split = int(len(X) * 0.8)
        if split < 50 or (len(X) - split) < 20:
            logger.info(f"[ML] 학습/검증 분할 불충분: {split}/{len(X)-split}")
            return

        X_train, X_test = X[:split], X[split:]
        y_train, y_test = y[:split], y[split:]
        w_train = weights[:split]

        # 클래스 다양성 체크
        if len(set(y_train)) < 2 or len(set(y_test)) < 2:
            logger.warning("[ML] 클래스 다양성 부족 (전부 승 or 전부 패)")
            return

        # Scaler
        scaler = StandardScaler()
        X_train_scaled = scaler.fit_transform(X_train)
        X_test_scaled = scaler.transform(X_test)

        # 모델
        model = GradientBoostingClassifier(
            n_estimators=100,
            max_depth=3,
            learning_rate=0.1,
            min_samples_leaf=20,
            subsample=0.8,
            max_features=min(0.7, len(self.feature_names)),
            random_state=42,
        )
        model.fit(X_train_scaled, y_train, sample_weight=w_train)

        # 검증
        train_acc = float(model.score(X_train_scaled, y_train))
        oos_acc = float(model.score(X_test_scaled, y_test))

        logger.info(
            f"[ML] 학습 완료: train={train_acc:.3f} OOS={oos_acc:.3f} "
            f"(피처 {len(self.feature_names)}, 데이터 {len(X)}건)"
        )

        # OOS 정확도 검증
        if oos_acc < self.min_oos_accuracy:
            self.consecutive_bad_oos += 1
            logger.warning(
                f"[ML] OOS {oos_acc:.3f} < {self.min_oos_accuracy} "
                f"→ 모델 미교체 (연속 {self.consecutive_bad_oos}회)"
            )
            if self.consecutive_bad_oos >= 2:
                logger.error("[ML] OOS 2연속 미달 → Phase A 복귀")
                old_phase = self.phase
                self.phase = "A"
                self.trained = False
                self.model = None
                self.scaler = None
                self._notify_phase_change(old_phase, "A",
                    f"OOS {oos_acc:.1%} 2연속 미달 → 룰 기반 복귀")
            return

        # 모델 교체
        self.consecutive_bad_oos = 0
        old_phase = self.phase
        self.model = model
        self.scaler = scaler
        self.trained = True
        self.phase = "B"
        self.train_accuracy = train_acc
        self.oos_accuracy = oos_acc
        self.last_train_count = self.total_labeled

        # 피처 중요도 로깅
        importances = model.feature_importances_
        sorted_idx = np.argsort(importances)[::-1]
        top5 = [(self.feature_names[i], round(importances[i], 3)) for i in sorted_idx[:5]]
        logger.info(f"[ML] Top 5 피처: {top5}")

        # Phase 전환 알림
        if old_phase != "B":
            self._notify_phase_change(old_phase, "B",
                f"OOS {oos_acc:.1%} | {len(self.feature_names)}피처 | Top: {top5[0][0]}")

        # 피처 확장 알림
        if len(self.feature_names) > len(CORE_FEATURES) and old_phase == "B":
            self._notify_phase_change("B", "B+",
                f"피처 {len(CORE_FEATURES)}→{len(self.feature_names)} 확장 | OOS {oos_acc:.1%}")

        self._save()

    # ════════════════════════════════════════
    #  성과 추적
    # ════════════════════════════════════════

    def record_decision_result(self, go: bool, actual_label: int):
        """ML 결정 결과 기록 (성과 추적용)"""
        self.recent_decisions.append((go, actual_label))

    def get_stats(self) -> dict:
        """ML 상태 정보 (대시보드/텔레그램용)"""
        # 최근 Go 결정의 정확도
        go_decisions = [(g, l) for g, l in self.recent_decisions if g]
        go_accuracy = 0.0
        if len(go_decisions) >= 5:
            go_accuracy = sum(1 for _, l in go_decisions if l == 1) / len(go_decisions) * 100

        # NoGo 중 실제 Win 비율 (ML이 걸러낸 것 중 좋았던 비율 = 후회 비율)
        nogo_decisions = [(g, l) for g, l in self.recent_decisions if not g]
        nogo_miss_rate = 0.0
        if len(nogo_decisions) >= 5:
            nogo_miss_rate = sum(1 for _, l in nogo_decisions if l == 1) / len(nogo_decisions) * 100

        return {
            "phase": self.phase,
            "trained": self.trained,
            "total_labeled": self.total_labeled,
            "oos_accuracy": round(self.oos_accuracy * 100, 1),
            "train_accuracy": round(self.train_accuracy * 100, 1),
            "go_threshold": self.go_threshold,
            "feature_count": len(self.feature_names),
            "recent_go_accuracy": round(go_accuracy, 1),
            "recent_nogo_miss": round(nogo_miss_rate, 1),
            "consecutive_bad_oos": self.consecutive_bad_oos,
        }

    def _notify_phase_change(self, old_phase: str, new_phase: str, details: str):
        """Phase 전환 알림 (텔레그램 등)"""
        logger.warning(f"[ML] Phase 전환: {old_phase} → {new_phase} | {details}")
        if self.on_phase_change:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.on_phase_change(old_phase, new_phase, details))
            except RuntimeError:
                # 이벤트 루프 없음 (테스트 등) → 무시
                logger.debug("Phase 알림: 이벤트 루프 없음 → 스킵")
            except Exception as e:
                logger.debug(f"Phase 알림 전송 실패: {e}")

    # ════════════════════════════════════════
    #  저장 / 로드
    # ════════════════════════════════════════

    def _save(self):
        """모델 + 상태 저장"""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        state = {
            "model": self.model,
            "scaler": self.scaler,
            "trained": self.trained,
            "phase": self.phase,
            "oos_accuracy": self.oos_accuracy,
            "train_accuracy": self.train_accuracy,
            "total_labeled": self.total_labeled,
            "last_train_count": self.last_train_count,
            "feature_names": self.feature_names,
            "consecutive_bad_oos": self.consecutive_bad_oos,
        }
        tmp = MODEL_PATH.with_suffix(".tmp")
        try:
            with open(tmp, "wb") as f:
                pickle.dump(state, f)
            tmp.replace(MODEL_PATH)
            logger.info(f"[ML] 모델 저장: {MODEL_PATH.name} (OOS {self.oos_accuracy:.3f})")
        except Exception as e:
            logger.error(f"[ML] 모델 저장 실패: {e}")
            tmp.unlink(missing_ok=True)

    def _load(self):
        """모델 + 상태 로드"""
        if not MODEL_PATH.exists():
            logger.info("[ML] 저장된 모델 없음 → Phase A 시작")
            return

        try:
            with open(MODEL_PATH, "rb") as f:
                state = pickle.load(f)

            self.model = state.get("model")
            self.scaler = state.get("scaler")
            self.trained = state.get("trained", False)
            self.phase = state.get("phase", "A")
            self.oos_accuracy = state.get("oos_accuracy", 0.0)
            self.train_accuracy = state.get("train_accuracy", 0.0)
            self.total_labeled = state.get("total_labeled", 0)
            self.last_train_count = state.get("last_train_count", 0)
            self.feature_names = state.get("feature_names", list(CORE_FEATURES))
            self.consecutive_bad_oos = state.get("consecutive_bad_oos", 0)

            logger.info(
                f"[ML] 모델 로드: Phase {self.phase}, "
                f"OOS {self.oos_accuracy:.3f}, {self.total_labeled}건"
            )
        except Exception as e:
            logger.warning(f"[ML] 모델 로드 실패: {e} → Phase A 시작")
            self.model = None
            self.trained = False
            self.phase = "A"


# ── 하위 호환: 기존 코드가 FlowML로 import하는 경우 ──
FlowML = MLDecisionEngine
