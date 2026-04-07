"""
MetaLearner — ML 모델 자가 업그레이드 시스템
1) 하이퍼파라미터 자동 튜닝 (Grid Search)
2) 피처 중요도 기반 가지치기
3) 모델 종류 자동 선택 (GBM vs RF vs LightGBM)
4) 동적 학습률 + 재학습 주기 조정
5) 자가 진단 + 자동 복구
"""
import logging
import time
import numpy as np
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger(__name__)


# 튜닝할 하이퍼파라미터 그리드
PARAM_GRID = [
    {"n_estimators": 100, "max_depth": 3, "learning_rate": 0.1, "min_samples_leaf": 5},
    {"n_estimators": 150, "max_depth": 4, "learning_rate": 0.08, "min_samples_leaf": 5},
    {"n_estimators": 200, "max_depth": 4, "learning_rate": 0.05, "min_samples_leaf": 10},
    {"n_estimators": 100, "max_depth": 5, "learning_rate": 0.1, "min_samples_leaf": 3},
    {"n_estimators": 150, "max_depth": 3, "learning_rate": 0.05, "min_samples_leaf": 10},
]


class MetaLearner:
    """ML 모델 메타 학습 + 자가 업그레이드"""

    def __init__(self, ml_swing, ml_scalp):
        self.ml_swing = ml_swing
        self.ml_scalp = ml_scalp
        self.history = []  # 메타 학습 이력

    async def run_meta_learning(self):
        """전체 메타 학습 실행"""
        logger.info("[META] ═══ 메타 학습 시작 ═══")
        start = time.time()
        result = {"timestamp": int(time.time() * 1000), "models": {}}

        for ml in [self.ml_swing, self.ml_scalp]:
            mode = ml.mode
            logger.info(f"[META] {mode} 모델 메타 학습")
            mode_result = {}

            # 1) 하이퍼파라미터 튜닝
            best_params = self._tune_hyperparameters(ml)
            mode_result["best_params"] = best_params

            # 2) 피처 중요도 분석
            feature_importance = self._analyze_features(ml)
            mode_result["feature_importance"] = feature_importance

            # 3) 약한 시그널 가중치 자동 조정
            adjustments = self._prune_weak_signals(ml, feature_importance)
            mode_result["weight_adjustments"] = adjustments

            # 4) 모델 종류 자동 선택
            best_model = self._select_best_model(ml)
            mode_result["best_model"] = best_model

            # 5) 재학습 주기 조정
            new_interval = self._adjust_retrain_interval(ml)
            mode_result["new_retrain_interval"] = new_interval

            # 6) 자가 진단
            health = self._diagnose(ml)
            mode_result["health"] = health

            # 7) 자동 복구
            if health["needs_recovery"]:
                self._recover(ml, health)
                mode_result["recovered"] = True

            ml.save()
            result["models"][mode] = mode_result

        elapsed = time.time() - start
        result["elapsed_sec"] = round(elapsed, 1)
        self.history.append(result)
        logger.info(f"[META] ═══ 메타 학습 완료 ({elapsed:.1f}초) ═══")
        return result

    # ── 1. 하이퍼파라미터 튜닝 ──

    def _tune_hyperparameters(self, ml) -> dict:
        """Grid Search로 최적 하이퍼파라미터 탐색"""
        if len(ml.X_buffer) < 100:
            return {"skipped": True, "reason": "insufficient data"}

        X = np.array(list(ml.X_buffer))
        y = np.array(list(ml.y_buffer))

        if len(set(y)) < 2:
            return {"skipped": True, "reason": "single class"}

        # Walk-forward 분할
        split = int(len(X) * 0.8)
        X_train, X_test = X[:split], X[split:]
        y_train, y_test = y[:split], y[split:]

        if len(set(y_test)) < 1:
            return {"skipped": True, "reason": "no test data"}

        scaler = StandardScaler()
        scaler.fit(X_train)
        X_train_s = scaler.transform(X_train)
        X_test_s = scaler.transform(X_test)

        best_score = -1
        best_params = None

        for params in PARAM_GRID:
            try:
                model = GradientBoostingClassifier(
                    n_estimators=params["n_estimators"],
                    max_depth=params["max_depth"],
                    learning_rate=params["learning_rate"],
                    min_samples_leaf=params["min_samples_leaf"],
                    subsample=0.8, random_state=42,
                )
                model.fit(X_train_s, y_train)
                score = model.score(X_test_s, y_test)

                if score > best_score:
                    best_score = score
                    best_params = params
            except Exception as e:
                logger.debug(f"[META] 파라미터 {params} 실패: {e}")

        if best_params:
            # 최적 파라미터로 글로벌 모델 재학습
            try:
                model = GradientBoostingClassifier(
                    **best_params, subsample=0.8, random_state=42
                )
                model.fit(X_train_s, y_train)
                ml.global_model = model
                ml.global_scaler = scaler
                ml.train_accuracy = float(model.score(X_train_s, y_train))
                ml.oos_accuracy = float(best_score)
                logger.info(f"[META] {ml.mode} 최적 파라미터: {best_params} | OOS={best_score:.3f}")
            except Exception as e:
                logger.error(f"[META] 모델 갱신 실패: {e}")

        return {"params": best_params, "oos_score": round(best_score, 3)}

    # ── 2. 피처 중요도 분석 ──

    def _analyze_features(self, ml) -> dict:
        """글로벌 모델의 피처 중요도 추출"""
        if not ml.global_model or not hasattr(ml.global_model, "feature_importances_"):
            return {}

        importances = ml.global_model.feature_importances_

        # 시그널별 피처 인덱스 (extract_features 순서와 일치)
        # 시그널 14개 × 3 (dir, strength, dir*strength) + 메타 피처
        signal_keys = sorted(ml.weights.keys())
        signal_importance = {}

        for i, key in enumerate(signal_keys):
            # 각 시그널이 차지하는 3개 피처 인덱스
            base = i * 3
            if base + 2 < len(importances):
                imp = float(importances[base] + importances[base + 1] + importances[base + 2])
                signal_importance[key] = round(imp, 4)

        # 정렬
        sorted_imp = dict(sorted(signal_importance.items(), key=lambda x: -x[1]))
        return sorted_imp

    # ── 3. 약한 시그널 가중치 자동 조정 ──

    def _prune_weak_signals(self, ml, feature_importance: dict) -> dict:
        """피처 중요도 낮은 시그널의 가중치 감소"""
        if not feature_importance:
            return {}

        adjustments = {}
        max_imp = max(feature_importance.values()) if feature_importance else 0

        if max_imp <= 0:
            return {}

        for key, imp in feature_importance.items():
            ratio = imp / max_imp
            old_weight = ml.weights.get(key, 0)

            # 중요도가 30% 이하인 시그널은 가중치 감소
            if ratio < 0.3:
                new_weight = max(0.5, old_weight * 0.9)
            # 중요도가 70% 이상인 시그널은 가중치 증가
            elif ratio > 0.7:
                new_weight = min(5.0, old_weight * 1.05)
            else:
                continue

            if abs(new_weight - old_weight) > 0.01:
                ml.weights[key] = round(new_weight, 2)
                adjustments[key] = {"old": round(old_weight, 2), "new": round(new_weight, 2)}

        if adjustments:
            logger.info(f"[META] {ml.mode} 가중치 조정: {len(adjustments)}개")

        return adjustments

    # ── 4. 모델 종류 자동 선택 ──

    def _select_best_model(self, ml) -> str:
        """GBM vs RF vs LR 중 OOS 가장 높은 모델 선택"""
        if len(ml.X_buffer) < 100:
            return "skipped"

        X = np.array(list(ml.X_buffer))
        y = np.array(list(ml.y_buffer))

        if len(set(y)) < 2:
            return "skipped"

        split = int(len(X) * 0.8)
        X_train, X_test = X[:split], X[split:]
        y_train, y_test = y[:split], y[split:]

        scaler = StandardScaler()
        scaler.fit(X_train)
        X_train_s = scaler.transform(X_train)
        X_test_s = scaler.transform(X_test)

        models = {
            "gbm": GradientBoostingClassifier(n_estimators=150, max_depth=4, random_state=42),
            "rf": RandomForestClassifier(n_estimators=150, max_depth=6, random_state=42),
            "lr": LogisticRegression(max_iter=500, random_state=42),
        }

        scores = {}
        for name, model in models.items():
            try:
                model.fit(X_train_s, y_train)
                scores[name] = float(model.score(X_test_s, y_test))
            except Exception:
                scores[name] = 0

        best = max(scores, key=scores.get)
        logger.info(f"[META] {ml.mode} 모델 비교: {scores} → {best}")
        return best

    # ── 5. 재학습 주기 동적 조정 ──

    def _adjust_retrain_interval(self, ml) -> int:
        """변동성 기반 재학습 주기 조정"""
        recent = list(ml.recent_results)[-50:]
        if len(recent) < 20:
            return ml.retrain_interval

        pnls = [r.get("pnl_pct", 0) if isinstance(r, dict) else r for r in recent]
        std_pnl = np.std(pnls)

        # 변동성 높음 → 자주 재학습 (50)
        # 변동성 낮음 → 드물게 재학습 (200)
        if std_pnl > 5:
            new_interval = 50
        elif std_pnl > 2:
            new_interval = 100
        else:
            new_interval = 200

        if new_interval != ml.retrain_interval:
            logger.info(f"[META] {ml.mode} 재학습 주기: {ml.retrain_interval} → {new_interval}")
            ml.retrain_interval = new_interval

        return new_interval

    # ── 6. 자가 진단 ──

    def _diagnose(self, ml) -> dict:
        """모델 건강 상태 진단"""
        recent = list(ml.recent_results)[-100:]
        if len(recent) < 30:
            return {"healthy": True, "needs_recovery": False, "reason": "insufficient data"}

        pnls = [r.get("pnl_pct", 0) if isinstance(r, dict) else r for r in recent]
        wins = sum(1 for p in pnls if p > 0)
        win_rate = wins / len(pnls)
        avg_pnl = np.mean(pnls)

        issues = []
        needs_recovery = False

        # OOS 너무 낮음
        if ml.oos_accuracy < 0.5:
            issues.append(f"OOS too low ({ml.oos_accuracy:.2f})")
            needs_recovery = True

        # 승률 너무 낮음
        if win_rate < 0.2:
            issues.append(f"Win rate too low ({win_rate:.1%})")
            needs_recovery = True

        # Train >> OOS (과적합)
        if ml.train_accuracy - ml.oos_accuracy > 0.3:
            issues.append(f"Overfitting (Train {ml.train_accuracy:.2f} >> OOS {ml.oos_accuracy:.2f})")
            needs_recovery = True

        # 평균 손실
        if avg_pnl < -2:
            issues.append(f"Avg PnL too negative ({avg_pnl:.2f})")
            needs_recovery = True

        return {
            "healthy": len(issues) == 0,
            "needs_recovery": needs_recovery,
            "issues": issues,
            "win_rate": round(win_rate, 3),
            "avg_pnl": round(avg_pnl, 3),
            "train_acc": ml.train_accuracy,
            "oos_acc": ml.oos_accuracy,
        }

    # ── 7. 자동 복구 ──

    def _recover(self, ml, health: dict):
        """문제 발견 시 자동 복구"""
        logger.warning(f"[META] {ml.mode} 자동 복구 시작: {health['issues']}")

        # 1) 진입 임계값 상향 (보수적으로)
        old_threshold = ml.entry_threshold
        ml.entry_threshold = min(ml.entry_threshold + 0.5,
                                 8.0 if ml.mode == "swing" else 5.0)
        logger.info(f"[META] 임계값 상향: {old_threshold:.1f} → {ml.entry_threshold:.1f}")

        # 2) 가중치 리셋 (기본값으로)
        if "Win rate too low" in str(health["issues"]):
            ml.weights = ml._default_weights()
            logger.info(f"[META] 가중치 기본값 리셋")

        # 3) 오래된 버퍼 일부 제거 (과적합 방지)
        if "Overfitting" in str(health["issues"]):
            keep = int(len(ml.X_buffer) * 0.7)
            from collections import deque
            ml.X_buffer = deque(list(ml.X_buffer)[-keep:], maxlen=ml.X_buffer.maxlen)
            ml.y_buffer = deque(list(ml.y_buffer)[-keep:], maxlen=ml.y_buffer.maxlen)
            logger.info(f"[META] 버퍼 30% 제거 (과적합 대응)")
