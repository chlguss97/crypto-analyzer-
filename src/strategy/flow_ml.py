"""
FlowML — FlowEngine 전용 경량 ML.

역할: FlowEngine이 "진입" 판단한 후, ML이 "이 상황에서 이기는 패턴인가" 검증.
- FlowEngine은 3가지 규칙(추세+레벨+플로우)으로 YES/NO 결정
- ML은 과거 데이터 기반으로 "이 조합이 이겼는지" 확률 제공
- 확률 높으면 가점, 낮으면 감점 (차단 아님)

피처: FlowEngine 시그널 기반 (~15개, 단순)
모델: GradientBoosting (단일, 레짐별 분리 불필요 — 피처에 레짐 포함)
학습: 매 거래 결과 기록 → 50건마다 자동 재학습
"""

import json
import logging
import time
import pickle
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent.parent / "data"
MODEL_PATH = DATA_DIR / "flow_ml.pkl"
MIN_SAMPLES = 30  # 최소 학습 데이터
RETRAIN_EVERY = 50  # N거래마다 재학습


class FlowML:

    def __init__(self):
        self.model = None
        self.scaler = None
        self.buffer_X = deque(maxlen=5000)
        self.buffer_y = deque(maxlen=5000)
        self.trained = False
        self.oos_accuracy = 0.0
        self.train_accuracy = 0.0
        self.trade_count = 0
        self.recent_results = deque(maxlen=20)
        self.feature_names = self._feature_names()
        self._load()

    @staticmethod
    def _feature_names() -> list:
        """피처 이름 목록 — 순서 고정"""
        return [
            # 추세 (4)
            "trend_1d",       # 1=up, -1=down, 0=neutral
            "trend_4h",
            "trend_1h",
            "trends_agree",   # 1d==4h ? 1 : 0

            # 레벨 (3)
            "near_support",   # 0 or 1
            "near_resistance",
            "level_strength", # 병합 강도

            # 오더플로우 (5)
            "cvd_direction",  # 1=buy, -1=sell, 0=mixed
            "cvd_strength",   # 0~1
            "whale_confirm",  # 0 or 1
            "liq_confirm",    # 0 or 1
            "flow_agrees",    # flow방향==진입방향 ? 1 : 0

            # 컨텍스트 (4)
            "atr_pct",        # 변동성
            "hour",           # 0~23 (UTC)
            "is_weekend",     # 0 or 1
            "conviction",     # FlowEngine score / 10
        ]

    def extract_features(self, flow_result: dict) -> list:
        """FlowEngine analyze() 결과 → 피처 벡터"""
        sigs = flow_result.get("signals", {})
        flow = sigs.get("flow", {})
        direction = flow_result.get("direction", "neutral")

        def trend_val(t):
            return 1 if t == "up" else -1 if t == "down" else 0

        t1d = trend_val(sigs.get("trend_1d", "neutral"))
        t4h = trend_val(sigs.get("trend_4h", "neutral"))
        t1h = trend_val(sigs.get("trend_1h", "neutral"))

        cvd_dir = 1 if flow.get("direction") == "long" else -1 if flow.get("direction") == "short" else 0
        flow_agrees = 1 if flow.get("direction") == direction else 0

        # 레벨 강도
        levels = sigs.get("levels", {})
        supports = levels.get("supports", [])
        resistances = levels.get("resistances", [])
        if direction == "long" and supports:
            level_strength = supports[0].get("strength", 1.0)
        elif direction == "short" and resistances:
            level_strength = resistances[0].get("strength", 1.0)
        else:
            level_strength = 0

        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)

        return [
            t1d, t4h, t1h,
            1 if t1d == t4h and t1d != 0 else 0,
            1 if sigs.get("near_support") else 0,
            1 if sigs.get("near_resistance") else 0,
            min(5.0, level_strength),
            cvd_dir,
            min(1.0, flow.get("strength", 0)),
            1 if flow.get("whale_confirm") else 0,
            1 if flow.get("liquidation_confirm") else 0,
            flow_agrees,
            flow_result.get("atr_pct", 0.3),
            now.hour,
            1 if now.weekday() >= 5 else 0,
            flow_result.get("score", 6.0) / 10.0,
        ]

    def predict(self, flow_result: dict) -> dict:
        """
        FlowEngine 결과 → ML 보정값.

        Returns:
            {
                "ml_score": float (-2.0 ~ +3.0),
                "win_prob": float (0~1),
                "trained": bool,
                "samples": int,
            }
        """
        if not self.trained or self.model is None:
            return {"ml_score": 0, "win_prob": 0.5, "trained": False,
                    "samples": len(self.buffer_X)}

        try:
            features = self.extract_features(flow_result)
            X = [features]

            if self.scaler:
                X = self.scaler.transform(X)

            proba = self.model.predict_proba(X)[0]
            win_prob = float(proba[1]) if len(proba) > 1 else 0.5

            # 확률 → 점수 보정
            if win_prob > 0.6:
                ml_score = min(3.0, (win_prob - 0.5) * 6)
            elif win_prob > 0.52:
                ml_score = min(1.5, (win_prob - 0.5) * 8)
            elif win_prob < 0.35:
                ml_score = max(-2.0, (win_prob - 0.5) * 4)
            elif win_prob < 0.45:
                ml_score = max(-1.0, (win_prob - 0.5) * 3)
            else:
                ml_score = 0

            return {
                "ml_score": round(ml_score, 2),
                "win_prob": round(win_prob, 3),
                "trained": True,
                "samples": len(self.buffer_X),
                "oos_accuracy": round(self.oos_accuracy, 3),
            }

        except Exception as e:
            logger.debug(f"FlowML predict error: {e}")
            return {"ml_score": 0, "win_prob": 0.5, "trained": False,
                    "samples": len(self.buffer_X)}

    def record_trade(self, flow_result: dict, pnl_pct: float, fee_pct: float = 0):
        """거래 결과 기록 → 자동 재학습"""
        try:
            features = self.extract_features(flow_result)
            net_pnl = pnl_pct - fee_pct
            label = 1 if net_pnl > 0 else 0

            self.buffer_X.append(features)
            self.buffer_y.append(label)
            self.trade_count += 1
            self.recent_results.append(net_pnl)

            # N거래마다 재학습
            if self.trade_count % RETRAIN_EVERY == 0 and len(self.buffer_X) >= MIN_SAMPLES:
                self.train()

        except Exception as e:
            logger.debug(f"FlowML record error: {e}")

    def train(self):
        """GBM 학습 — Walk-forward 검증"""
        if len(self.buffer_X) < MIN_SAMPLES:
            logger.info(f"[FlowML] 학습 데이터 부족: {len(self.buffer_X)}/{MIN_SAMPLES}")
            return

        try:
            from sklearn.ensemble import GradientBoostingClassifier
            from sklearn.preprocessing import StandardScaler
            import numpy as np

            X = np.array(list(self.buffer_X))
            y = np.array(list(self.buffer_y))

            # 80/20 Walk-forward
            split = int(len(X) * 0.8)
            X_train, X_test = X[:split], X[split:]
            y_train, y_test = y[:split], y[split:]

            # 스케일링
            scaler = StandardScaler()
            X_train_s = scaler.fit_transform(X_train)
            X_test_s = scaler.transform(X_test)

            # 클래스 균형
            n_pos = int(y_train.sum())
            n_neg = len(y_train) - n_pos
            spw = n_neg / max(n_pos, 1)

            model = GradientBoostingClassifier(
                n_estimators=100,
                max_depth=3,
                learning_rate=0.1,
                min_samples_leaf=5,
                subsample=0.8,
            )
            # sample_weight로 클래스 균형
            weights = np.where(y_train == 1, spw, 1.0)
            model.fit(X_train_s, y_train, sample_weight=weights)

            # 정확도
            train_acc = float(model.score(X_train_s, y_train))
            oos_acc = float(model.score(X_test_s, y_test)) if len(X_test) > 0 else 0

            self.model = model
            self.scaler = scaler
            self.trained = True
            self.train_accuracy = train_acc
            self.oos_accuracy = oos_acc

            logger.info(
                f"[FlowML] 학습 완료: {len(X)}건 | "
                f"Train={train_acc:.1%} OOS={oos_acc:.1%}"
            )

            self.save()

        except Exception as e:
            logger.error(f"[FlowML] 학습 실패: {e}")

    def get_stats(self) -> dict:
        """대시보드 표시용 통계"""
        wr = 0
        if self.recent_results:
            wins = sum(1 for r in self.recent_results if r > 0)
            wr = wins / len(self.recent_results)

        return {
            "trained": self.trained,
            "samples": len(self.buffer_X),
            "trade_count": self.trade_count,
            "train_accuracy": round(self.train_accuracy, 3),
            "oos_accuracy": round(self.oos_accuracy, 3),
            "recent_wr": round(wr, 3),
            "recent_trades": len(self.recent_results),
            "feature_count": len(self.feature_names),
        }

    def save(self):
        """모델 + 버퍼 저장"""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "model": self.model,
                "scaler": self.scaler,
                "buffer_X": list(self.buffer_X),
                "buffer_y": list(self.buffer_y),
                "trained": self.trained,
                "train_accuracy": self.train_accuracy,
                "oos_accuracy": self.oos_accuracy,
                "trade_count": self.trade_count,
                "recent_results": list(self.recent_results),
                "feature_names": self.feature_names,
            }
            # 원자적 저장
            tmp = MODEL_PATH.with_suffix(".tmp")
            with open(tmp, "wb") as f:
                pickle.dump(data, f)
            tmp.replace(MODEL_PATH)
            logger.debug(f"[FlowML] 저장 완료: {len(self.buffer_X)}건")
        except Exception as e:
            logger.error(f"[FlowML] 저장 실패: {e}")

    def _load(self):
        """모델 로드"""
        if not MODEL_PATH.exists():
            logger.info("[FlowML] 모델 없음 → cold start")
            return

        try:
            with open(MODEL_PATH, "rb") as f:
                data = pickle.load(f)

            # 피처 호환성 체크
            saved_features = data.get("feature_names", [])
            if saved_features != self.feature_names:
                logger.warning(
                    f"[FlowML] 피처 변경 감지 ({len(saved_features)}→{len(self.feature_names)}) "
                    f"→ 모델 리셋"
                )
                return

            self.model = data.get("model")
            self.scaler = data.get("scaler")
            self.buffer_X = deque(data.get("buffer_X", []), maxlen=5000)
            self.buffer_y = deque(data.get("buffer_y", []), maxlen=5000)
            self.trained = data.get("trained", False)
            self.train_accuracy = data.get("train_accuracy", 0)
            self.oos_accuracy = data.get("oos_accuracy", 0)
            self.trade_count = data.get("trade_count", 0)
            self.recent_results = deque(data.get("recent_results", []), maxlen=20)

            logger.info(
                f"[FlowML] 로드: {len(self.buffer_X)}건 | "
                f"trained={self.trained} OOS={self.oos_accuracy:.1%}"
            )

        except Exception as e:
            logger.warning(f"[FlowML] 로드 실패 → cold start: {e}")
