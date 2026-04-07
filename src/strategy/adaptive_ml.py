"""
AdaptiveML v2 — 레짐별 멀티모델 + 앙상블 + 강화 피처 + Walk-forward 검증
- 레짐별 GBM 분리 (trending_up / trending_down / ranging / volatile)
- 앙상블: GBM + RandomForest + LogisticRegression 투표
- 피처 강화: 세션, 요일, 시그널 변화율, 크로스 피처, 연속성
- Walk-forward: 80/20 분할 학습 후 OOS 정확도 추적
"""
import numpy as np
import pickle
import logging
import time as _time
from pathlib import Path
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from collections import deque

logger = logging.getLogger(__name__)
DATA_DIR = Path(__file__).parent.parent.parent / "data"

REGIMES = ["trending_up", "trending_down", "ranging", "volatile"]


class AdaptiveML:
    """적응형 ML v2 — 레짐별 앙상블 + 강화 피처"""

    def __init__(self, mode: str = "swing"):
        self.mode = mode
        self.is_trained = False

        # 앙상블 모델 (레짐별)
        self.models = {}       # regime → {"gbm": model, "rf": model, "lr": model}
        self.scalers = {}      # regime → StandardScaler
        self.global_model = None  # 전체 데이터 모델 (레짐 부족 시 폴백)
        self.global_scaler = StandardScaler()

        # 레짐별 학습 데이터 버퍼
        self.regime_buffers = {r: {"X": deque(maxlen=3000), "y": deque(maxlen=3000)} for r in REGIMES}
        self.X_buffer = deque(maxlen=10000)  # 전체 버퍼 (글로벌 모델용)
        self.y_buffer = deque(maxlen=10000)
        self.regime_labels = deque(maxlen=10000)  # 각 샘플의 레짐

        # 적응형 가중치
        self.weights = self._default_weights()

        # 임계값
        self.entry_threshold = 5.5 if mode == "swing" else 4.5
        self.min_trades_to_train = 30
        self.retrain_interval = 100  # 100거래마다 재학습 (모델 안정화)

        self.trade_count = 0
        self.recent_results = deque(maxlen=200)

        # Walk-forward 성능 추적
        self.train_accuracy = 0.0
        self.oos_accuracy = 0.0      # Out-of-Sample 정확도
        self.regime_performance = {r: {"trades": 0, "wins": 0, "oos_acc": 0.0} for r in REGIMES}

        # 이전 시그널 캐시 (변화율 피처용)
        self._prev_signals = {}

        # 모델 경로
        self.model_path = DATA_DIR / f"adaptive_v2_{mode}.pkl"
        self._legacy_path = DATA_DIR / f"adaptive_{mode}.pkl"  # v1 호환

    def _default_weights(self) -> dict:
        if self.mode == "swing":
            return {
                "order_block": 3.0, "market_structure": 2.5, "fractal": 2.0,
                "bollinger": 2.0, "funding_rate": 2.0, "open_interest": 2.0,
                "rsi": 1.5, "volume": 1.5, "fvg": 1.5, "cvd": 1.5,
                "liquidation": 1.5, "ema": 1.0, "long_short_ratio": 1.0,
                "vwap": 1.0,
            }
        else:
            return {
                "order_block": 4.0, "bos": 3.5, "vol_explosion": 3.5, "range_breakout": 3.5,
                "liquidity_sweep": 3.5, "bb_breakout": 3.0, "vwap_levels": 3.0,
                "ema_cross": 2.5, "rapid_momentum": 2.5, "fvg": 2.5,
                "pivot_points": 2.5, "rsi_reversal": 2.0, "volume_spike": 2.0,
                "candle_pattern": 2.0, "momentum": 1.5,
            }

    # ── 강화 피처 엔지니어링 ──

    def extract_features(self, signals: dict, meta: dict = None) -> list:
        """시그널 → 강화 ML 피처 벡터"""
        if meta is None:
            meta = {}

        features = []

        # 1) 기본 피처: 각 시그널의 (direction, strength, dir*strength)
        for key in sorted(self.weights.keys()):
            sig = signals.get(key, {})
            direction = sig.get("direction", "neutral")
            strength = sig.get("strength", 0)
            dir_val = 1 if direction == "long" else -1 if direction == "short" else 0
            features.extend([dir_val, strength, dir_val * strength])

        # 2) 메타 피처
        features.append(meta.get("atr_pct", 0.3))
        hour = meta.get("hour", 12)
        features.append(hour / 24)
        features.append(meta.get("streak", 0) / 5)
        features.append(meta.get("daily_pnl", 0) / 5)

        # 3) 세션 피처 (아시아/유럽/미국)
        #    아시아: 00-08 UTC, 유럽: 08-16 UTC, 미국: 16-24 UTC
        features.append(1.0 if 0 <= hour < 8 else 0.0)   # 아시아
        features.append(1.0 if 8 <= hour < 16 else 0.0)   # 유럽
        features.append(1.0 if 16 <= hour < 24 else 0.0)  # 미국

        # 4) 요일 피처 (0=월 ~ 6=일)
        import datetime
        weekday = meta.get("weekday", datetime.datetime.utcnow().weekday())
        features.append(weekday / 6)
        features.append(1.0 if weekday >= 5 else 0.0)  # 주말

        # 5) 시그널 합산 피처
        long_count = sum(1 for s in signals.values()
                         if isinstance(s, dict) and s.get("direction") == "long")
        short_count = sum(1 for s in signals.values()
                          if isinstance(s, dict) and s.get("direction") == "short")
        total_strength = sum(s.get("strength", 0) for s in signals.values() if isinstance(s, dict))
        features.append(long_count / max(len(signals), 1))
        features.append(short_count / max(len(signals), 1))
        features.append(total_strength / max(len(signals), 1))
        features.append((long_count - short_count) / max(long_count + short_count, 1))  # 방향 합의도

        # 6) 시그널 변화율 (이전 대비)
        delta_score = 0.0
        if self._prev_signals:
            prev_strength = sum(s.get("strength", 0) for s in self._prev_signals.values() if isinstance(s, dict))
            delta_score = total_strength - prev_strength
        features.append(delta_score / max(total_strength, 1))
        self._prev_signals = signals.copy()

        # 7) 레짐 피처 (meta에서)
        regime = meta.get("regime", "ranging")
        for r in REGIMES:
            features.append(1.0 if regime == r else 0.0)

        # 8) 프랙탈 특수 피처
        fractal = signals.get("fractal", {})
        features.append(1.0 if fractal.get("breakout", "none") != "none" else 0.0)
        features.append(fractal.get("resistance_strength", 0) / 5)
        features.append(fractal.get("support_strength", 0) / 5)
        cluster = fractal.get("cluster_zone")
        features.append(cluster.get("distance_pct", 5) / 5 if cluster else 1.0)

        return features

    # ── 예측 ──

    def predict(self, signals: dict, meta: dict = None) -> dict:
        """앙상블 예측: 레짐별 모델 + 글로벌 모델 투표"""
        features = self.extract_features(signals, meta)

        if not self.is_trained:
            return {"ml_score": 0.0, "ml_direction": "neutral", "ml_confidence": 0.0, "trained": False}

        try:
            X = np.array([features])
            regime = (meta or {}).get("regime", "ranging")

            probas = []
            model_names = []

            # 1) 레짐별 모델 (있으면)
            if regime in self.models and self.models[regime]:
                scaler = self.scalers.get(regime, self.global_scaler)
                X_s = scaler.transform(X)
                for name, model in self.models[regime].items():
                    try:
                        p = model.predict_proba(X_s)[0]
                        # GBM 가중치 높게
                        weight = 2.0 if name == "gbm" else 1.0
                        probas.append((p, weight))
                        model_names.append(f"{regime}_{name}")
                    except Exception:
                        pass

            # 2) 글로벌 모델 (항상)
            if self.global_model is not None:
                try:
                    X_g = self.global_scaler.transform(X)
                    p = self.global_model.predict_proba(X_g)[0]
                    probas.append((p, 1.5))
                    model_names.append("global")
                except Exception:
                    pass

            if not probas:
                return {"ml_score": 0.0, "ml_direction": "neutral", "ml_confidence": 0.0, "trained": False}

            # 가중 평균 확률
            total_weight = sum(w for _, w in probas)
            avg_proba = np.zeros(len(probas[0][0]))
            for p, w in probas:
                if len(p) == len(avg_proba):
                    avg_proba += p * w
            avg_proba /= total_weight

            # class: 0=loss, 1=small_win, 2=big_win
            if len(avg_proba) >= 3:
                win_prob = avg_proba[1] + avg_proba[2]
                big_win_prob = float(avg_proba[2])
            elif len(avg_proba) == 2:
                win_prob = float(avg_proba[1])
                big_win_prob = 0.0
            else:
                win_prob = 0.5
                big_win_prob = 0.0

            confidence = abs(win_prob - 0.5) * 2

            if win_prob > 0.55:
                ml_direction = "confirm"
                ml_score = min(3.0, (win_prob - 0.5) * 6)
            elif win_prob < 0.4:
                ml_direction = "reject"
                ml_score = -2.0
            else:
                ml_direction = "neutral"
                ml_score = 0.0

            return {
                "ml_score": round(ml_score, 2),
                "ml_direction": ml_direction,
                "ml_confidence": round(confidence, 2),
                "win_prob": round(float(win_prob), 3),
                "big_win_prob": round(big_win_prob, 3),
                "trained": True,
                "models_used": len(probas),
                "model_names": model_names,
            }

        except Exception as e:
            logger.error(f"ML predict error: {e}")
            return {"ml_score": 0.0, "ml_direction": "neutral", "ml_confidence": 0.0, "trained": False}

    # ── 학습 기록 ──

    def record_trade(self, signals: dict, meta: dict, pnl_pct: float):
        """거래 결과 기록 → 레짐별 버퍼 + 글로벌 버퍼"""
        features = self.extract_features(signals, meta)
        regime = (meta or {}).get("regime", "ranging")

        if pnl_pct <= -0.05:
            label = 0
        elif pnl_pct < 1.0:
            label = 1
        else:
            label = 2

        # 글로벌 버퍼
        self.X_buffer.append(features)
        self.y_buffer.append(label)
        self.regime_labels.append(regime)

        # 레짐별 버퍼
        if regime in self.regime_buffers:
            self.regime_buffers[regime]["X"].append(features)
            self.regime_buffers[regime]["y"].append(label)

        self.recent_results.append({
            "pnl_pct": pnl_pct,
            "timestamp": int(_time.time() * 1000),
            "label": label,
            "regime": regime,
        })
        self.trade_count += 1

        # 레짐별 성능 추적
        if regime in self.regime_performance:
            self.regime_performance[regime]["trades"] += 1
            if pnl_pct > 0:
                self.regime_performance[regime]["wins"] += 1

        self._adjust_weights(signals, pnl_pct)

        if self.trade_count % self.retrain_interval == 0 and len(self.X_buffer) >= self.min_trades_to_train:
            self.train()
        elif self.trade_count % 50 == 0:
            # 50건마다만 저장 (매건 저장하면 I/O 병목)
            self.save()

        if self.trade_count % 100 == 0:
            logger.info(
                f"[{self.mode}] ML 학습 {self.trade_count}건 | 레짐:{regime} | "
                f"버퍼 {len(self.X_buffer)} | {'TRAINED' if self.is_trained else 'LEARNING'}"
            )

    # ── 앙상블 학습 ──

    def train(self):
        """앙상블 학습: 글로벌 + 레짐별 모델"""
        if len(self.X_buffer) < self.min_trades_to_train:
            return

        X_all = np.array(list(self.X_buffer))
        y_all = np.array(list(self.y_buffer))

        if len(set(y_all)) < 2:
            return

        try:
            # ── Walk-forward 분할 (80/20) ──
            split = int(len(X_all) * 0.8)
            X_train, X_test = X_all[:split], X_all[split:]
            y_train, y_test = y_all[:split], y_all[split:]

            # ── 글로벌 앙상블 학습 ──
            self.global_scaler.fit(X_train)
            X_train_s = self.global_scaler.transform(X_train)
            X_test_s = self.global_scaler.transform(X_test)

            gbm = GradientBoostingClassifier(
                n_estimators=150, max_depth=4, min_samples_leaf=5,
                learning_rate=0.08, subsample=0.8, random_state=42,
            )
            gbm.fit(X_train_s, y_train)
            self.global_model = gbm

            # Walk-forward 정확도
            self.train_accuracy = float(gbm.score(X_train_s, y_train))
            if len(X_test_s) > 0 and len(set(y_test)) >= 1:
                self.oos_accuracy = float(gbm.score(X_test_s, y_test))
            else:
                self.oos_accuracy = 0.0

            self.is_trained = True
            logger.info(
                f"[{self.mode}] 글로벌 모델: {len(X_train)}건 학습 | "
                f"Train acc={self.train_accuracy:.3f} | OOS acc={self.oos_accuracy:.3f}"
            )

            # ── 레짐별 앙상블 학습 ──
            for regime in REGIMES:
                buf = self.regime_buffers[regime]
                if len(buf["X"]) < 20:
                    continue

                X_r = np.array(list(buf["X"]))
                y_r = np.array(list(buf["y"]))

                if len(set(y_r)) < 2:
                    continue

                r_split = int(len(X_r) * 0.8)
                X_rt, X_rv = X_r[:max(r_split, 1)], X_r[max(r_split, 1):]
                y_rt, y_rv = y_r[:max(r_split, 1)], y_r[max(r_split, 1):]

                if len(set(y_rt)) < 2:
                    continue

                scaler_r = StandardScaler()
                scaler_r.fit(X_rt)
                X_rt_s = scaler_r.transform(X_rt)

                ensemble = {}

                # GBM
                try:
                    g = GradientBoostingClassifier(
                        n_estimators=100, max_depth=3, min_samples_leaf=5,
                        learning_rate=0.1, subsample=0.8, random_state=42,
                    )
                    g.fit(X_rt_s, y_rt)
                    ensemble["gbm"] = g
                except Exception as e:
                    logger.debug(f"[{self.mode}] {regime} GBM 학습 실패: {e}")

                # RandomForest
                try:
                    rf = RandomForestClassifier(
                        n_estimators=100, max_depth=5, min_samples_leaf=3, random_state=42,
                    )
                    rf.fit(X_rt_s, y_rt)
                    ensemble["rf"] = rf
                except Exception as e:
                    logger.debug(f"[{self.mode}] {regime} RF 학습 실패: {e}")

                # LogisticRegression
                try:
                    lr = LogisticRegression(max_iter=500, random_state=42, multi_class="multinomial")
                    lr.fit(X_rt_s, y_rt)
                    ensemble["lr"] = lr
                except Exception as e:
                    logger.debug(f"[{self.mode}] {regime} LR 학습 실패: {e}")

                if ensemble:
                    self.models[regime] = ensemble
                    self.scalers[regime] = scaler_r

                    # OOS 정확도
                    if len(X_rv) > 0 and len(set(y_rv)) >= 1:
                        X_rv_s = scaler_r.transform(X_rv)
                        oos = float(ensemble.get("gbm", list(ensemble.values())[0]).score(X_rv_s, y_rv))
                        self.regime_performance[regime]["oos_acc"] = oos
                        logger.info(
                            f"[{self.mode}] {regime} 모델: {len(X_rt)}건 | "
                            f"앙상블 {list(ensemble.keys())} | OOS={oos:.3f}"
                        )

            self.save()

        except Exception as e:
            logger.error(f"ML train error: {e}")

    def _adjust_weights(self, signals: dict, pnl_pct: float):
        """기법별 가중치 미세 조정"""
        lr = 0.05

        for key in self.weights:
            sig = signals.get(key, {})
            strength = sig.get("strength", 0)
            direction = sig.get("direction", "neutral")

            if strength == 0 or direction == "neutral":
                continue

            if pnl_pct > 0:
                self.weights[key] = min(5.0, self.weights[key] + lr * strength)
            else:
                self.weights[key] = max(0.3, self.weights[key] - lr * strength * 0.5)

        recent = list(self.recent_results)
        if len(recent) >= 10:
            recent_wr = sum(1 for r in recent[-10:] if self._get_pnl(r) > 0) / 10
            # 모드별 임계값 상한/하한
            if self.mode == "scalp":
                max_threshold, min_threshold = 5.0, 2.5
            else:
                max_threshold, min_threshold = 8.0, 4.0

            if recent_wr < 0.3:
                self.entry_threshold = min(max_threshold, self.entry_threshold + 0.1)
            elif recent_wr > 0.5:
                self.entry_threshold = max(min_threshold, self.entry_threshold - 0.05)

    def get_adjusted_score(self, raw_score: float, signals: dict, meta: dict = None) -> float:
        """ML 예측으로 점수 조정"""
        ml = self.predict(signals, meta)
        adjusted = raw_score
        if ml["trained"]:
            adjusted += ml["ml_score"]
            adjusted = max(0, min(10, adjusted))
        return adjusted

    # ── 저장/로드 ──

    def save(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        # 안전장치: 빈 상태로 기존 파일 덮어쓰기 방지
        if len(self.X_buffer) == 0 and self.model_path.exists():
            existing_size = self.model_path.stat().st_size
            if existing_size > 10000:  # 기존 파일이 10KB+ 면 보호
                logger.warning(
                    f"[{self.mode}] save() 빈 버퍼로 호출됨 → 기존 파일 보호 (크기: {existing_size})"
                )
                return

        try:
            data = {
                "version": 2,
                "global_model": self.global_model,
                "global_scaler": self.global_scaler,
                "models": self.models,
                "scalers": self.scalers,
                "weights": self.weights,
                "entry_threshold": self.entry_threshold,
                "trade_count": self.trade_count,
                "X_buffer": list(self.X_buffer),
                "y_buffer": list(self.y_buffer),
                "regime_labels": list(self.regime_labels),
                "regime_buffers": {r: {"X": list(b["X"]), "y": list(b["y"])}
                                   for r, b in self.regime_buffers.items()},
                "recent_results": list(self.recent_results),
                "train_accuracy": self.train_accuracy,
                "oos_accuracy": self.oos_accuracy,
                "regime_performance": self.regime_performance,
            }
            with open(self.model_path, "wb") as f:
                pickle.dump(data, f)
        except Exception as e:
            logger.error(f"ML save error: {e}")

    def load(self):
        """v2 모델 로드 (v1 호환)"""
        # v2 먼저 시도
        if self.model_path.exists():
            try:
                with open(self.model_path, "rb") as f:
                    data = pickle.load(f)
                if data.get("version") == 2:
                    self._load_v2(data)
                    return
            except Exception as e:
                logger.warning(f"v2 load failed: {e}")

        # v1 폴백
        if self._legacy_path.exists():
            try:
                with open(self._legacy_path, "rb") as f:
                    data = pickle.load(f)
                self._load_v1(data)
            except Exception as e:
                logger.error(f"v1 load failed: {e}")

    def _load_v2(self, data):
        self.global_model = data["global_model"]
        self.global_scaler = data["global_scaler"]
        self.models = data.get("models", {})
        self.scalers = data.get("scalers", {})
        self.weights = data["weights"]

        # ── 시그널 이름 마이그레이션 (scalp_ob → order_block, scalp_fvg → fvg) ──
        if self.mode == "scalp":
            renames = {"scalp_ob": "order_block", "scalp_fvg": "fvg"}
            for old_key, new_key in renames.items():
                if old_key in self.weights and new_key not in self.weights:
                    self.weights[new_key] = self.weights.pop(old_key)
                    logger.info(f"[{self.mode}] 시그널 이름 마이그레이션: {old_key} → {new_key}")

            # 기본 가중치에 있는데 로드된 weights에 없는 키 추가
            defaults = self._default_weights()
            for k, v in defaults.items():
                if k not in self.weights:
                    self.weights[k] = v
                    logger.info(f"[{self.mode}] 누락된 시그널 추가: {k}={v}")

        self.entry_threshold = data["entry_threshold"]
        self.trade_count = data["trade_count"]
        self.X_buffer = deque(data.get("X_buffer", []), maxlen=10000)
        self.y_buffer = deque(data.get("y_buffer", []), maxlen=10000)
        self.regime_labels = deque(data.get("regime_labels", []), maxlen=10000)
        for r in REGIMES:
            rb = data.get("regime_buffers", {}).get(r, {"X": [], "y": []})
            self.regime_buffers[r] = {"X": deque(rb["X"], maxlen=3000), "y": deque(rb["y"], maxlen=3000)}
        self.recent_results = deque(data.get("recent_results", []), maxlen=200)
        self.train_accuracy = data.get("train_accuracy", 0)
        self.oos_accuracy = data.get("oos_accuracy", 0)
        self.regime_performance = data.get("regime_performance", self.regime_performance)
        self.is_trained = self.global_model is not None
        logger.info(
            f"[{self.mode}] ML v2 loaded: {self.trade_count}건 | "
            f"레짐 모델: {list(self.models.keys())} | OOS={self.oos_accuracy:.3f}"
        )

    def _load_v1(self, data):
        """v1 데이터 마이그레이션"""
        self.global_model = data.get("model")
        self.global_scaler = data.get("scaler", StandardScaler())
        self.weights = data.get("weights", self._default_weights())
        self.entry_threshold = data.get("entry_threshold", 5.5)
        self.trade_count = data.get("trade_count", 0)

        # v1 버퍼를 글로벌 버퍼로 마이그레이션
        old_x = data.get("X_buffer", [])
        old_y = data.get("y_buffer", [])
        self.X_buffer = deque(maxlen=10000)
        self.y_buffer = deque(maxlen=10000)

        # v1 피처는 길이가 다를 수 있으므로 버퍼는 비움 (재학습 필요)
        self.recent_results = deque(data.get("recent_results", []), maxlen=200)
        self.is_trained = self.global_model is not None

        logger.info(
            f"[{self.mode}] ML v1→v2 마이그레이션: {self.trade_count}건 | "
            f"버퍼 리셋 (피처 구조 변경) | 재학습 필요"
        )

        # v1 pkl 기반으로 즉시 v2 저장
        self.save()

    # ── 유틸 ──

    def _get_pnl(self, r):
        return r.get("pnl_pct", 0) if isinstance(r, dict) else r

    def get_stats(self) -> dict:
        """현재 학습 상태 (대시보드용)"""
        recent = list(self.recent_results)
        wr = sum(1 for r in recent if self._get_pnl(r) > 0) / len(recent) * 100 if recent else 0

        regime_stats = {}
        for r in REGIMES:
            rp = self.regime_performance[r]
            buf_size = len(self.regime_buffers[r]["X"])
            regime_stats[r] = {
                "trades": rp["trades"],
                "win_rate": round(rp["wins"] / max(rp["trades"], 1) * 100, 1),
                "oos_accuracy": round(rp["oos_acc"], 3),
                "buffer_size": buf_size,
                "has_model": r in self.models,
            }

        return {
            "mode": self.mode,
            "trained": self.is_trained,
            "trade_count": self.trade_count,
            "buffer_size": len(self.X_buffer),
            "entry_threshold": round(self.entry_threshold, 2),
            "recent_win_rate": round(wr, 1),
            "weights": {k: round(v, 2) for k, v in self.weights.items()},
            "train_accuracy": round(self.train_accuracy, 3),
            "oos_accuracy": round(self.oos_accuracy, 3),
            "regime_models": list(self.models.keys()),
            "regime_stats": regime_stats,
            "ensemble_count": sum(len(m) for m in self.models.values()),
        }
