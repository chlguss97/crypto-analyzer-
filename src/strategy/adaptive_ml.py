"""
AdaptiveML — 자동 학습 파이프라인
거래 결과로부터 실시간 학습하여 가중치/임계값 자동 조정
"""
import numpy as np
import pickle
import logging
from pathlib import Path
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from collections import deque

logger = logging.getLogger(__name__)
DATA_DIR = Path(__file__).parent.parent.parent / "data"


class AdaptiveML:
    """적응형 ML — 거래마다 학습, 가중치 자동 조정"""

    def __init__(self, mode: str = "swing"):
        """
        Args:
            mode: 'swing' | 'scalp'
        """
        self.mode = mode
        self.model = None
        self.scaler = StandardScaler()
        self.is_trained = False

        # 학습 데이터 버퍼
        self.X_buffer = deque(maxlen=5000)
        self.y_buffer = deque(maxlen=5000)

        # 적응형 가중치 (기법별)
        self.weights = self._default_weights()

        # 적응형 임계값
        self.entry_threshold = 5.5 if mode == "swing" else 4.5
        self.min_trades_to_train = 30
        self.retrain_interval = 20  # 20거래마다 재학습

        self.trade_count = 0
        self.recent_results = deque(maxlen=100)

        # 모델 경로
        self.model_path = DATA_DIR / f"adaptive_{mode}.pkl"

    def _default_weights(self) -> dict:
        if self.mode == "swing":
            return {
                "order_block": 3.0, "market_structure": 2.5,
                "bollinger": 2.0, "funding_rate": 2.0, "open_interest": 2.0,
                "rsi": 1.5, "volume": 1.5, "fvg": 1.5, "cvd": 1.5,
                "liquidation": 1.5, "ema": 1.0, "long_short_ratio": 1.0,
                "vwap": 1.0,
            }
        else:  # scalp
            return {
                "bb_breakout": 3.0, "ema_cross": 2.5, "rsi_reversal": 2.0,
                "volume_spike": 2.0, "momentum": 1.5,
            }

    def extract_features(self, signals: dict, meta: dict = None) -> list:
        """시그널 → ML 피처 벡터"""
        if meta is None:
            meta = {}

        features = []
        for key in sorted(self.weights.keys()):
            sig = signals.get(key, {})
            direction = sig.get("direction", "neutral")
            strength = sig.get("strength", 0)

            # 방향 인코딩: long=+1, short=-1, neutral=0
            dir_val = 1 if direction == "long" else -1 if direction == "short" else 0
            features.extend([dir_val, strength, dir_val * strength])

        # 메타 피처
        features.append(meta.get("atr_pct", 0.3))
        features.append(meta.get("hour", 12) / 24)
        features.append(meta.get("streak", 0) / 5)
        features.append(meta.get("daily_pnl", 0) / 5)

        return features

    def predict(self, signals: dict, meta: dict = None) -> dict:
        """ML 예측: 진입 여부 + 방향 + 확신도"""
        features = self.extract_features(signals, meta)

        if not self.is_trained or self.model is None:
            return {
                "ml_score": 0.0,
                "ml_direction": "neutral",
                "ml_confidence": 0.0,
                "trained": False,
            }

        try:
            X = np.array([features])
            X_scaled = self.scaler.transform(X)
            proba = self.model.predict_proba(X_scaled)[0]

            # class: 0=loss, 1=small_win, 2=big_win
            if len(proba) == 3:
                win_prob = proba[1] + proba[2]
                big_win_prob = proba[2]
            else:
                win_prob = proba[1] if len(proba) > 1 else 0.5
                big_win_prob = 0

            confidence = abs(win_prob - 0.5) * 2  # 0~1

            if win_prob > 0.55:
                ml_direction = "confirm"  # 현재 방향 확인
                ml_score = min(3.0, (win_prob - 0.5) * 6)
            elif win_prob < 0.4:
                ml_direction = "reject"  # 진입 거부
                ml_score = -2.0
            else:
                ml_direction = "neutral"
                ml_score = 0.0

            return {
                "ml_score": round(ml_score, 2),
                "ml_direction": ml_direction,
                "ml_confidence": round(confidence, 2),
                "win_prob": round(win_prob, 3),
                "big_win_prob": round(big_win_prob, 3),
                "trained": True,
            }

        except Exception as e:
            logger.error(f"ML predict error: {e}")
            return {"ml_score": 0.0, "ml_direction": "neutral", "ml_confidence": 0.0, "trained": False}

    def record_trade(self, signals: dict, meta: dict, pnl_pct: float):
        """거래 결과 기록 → 버퍼에 추가"""
        features = self.extract_features(signals, meta)

        # 라벨: 0=loss, 1=small_win (0~1%), 2=big_win (1%+)
        if pnl_pct <= -0.05:
            label = 0
        elif pnl_pct < 1.0:
            label = 1
        else:
            label = 2

        self.X_buffer.append(features)
        self.y_buffer.append(label)
        self.recent_results.append(pnl_pct)
        self.trade_count += 1

        # 가중치 자동 조정
        self._adjust_weights(signals, pnl_pct)

        # 재학습 체크
        if self.trade_count % self.retrain_interval == 0 and len(self.X_buffer) >= self.min_trades_to_train:
            self.train()

    def _adjust_weights(self, signals: dict, pnl_pct: float):
        """거래 결과에 따라 기법별 가중치 미세 조정"""
        lr = 0.05  # 학습률

        for key in self.weights:
            sig = signals.get(key, {})
            strength = sig.get("strength", 0)
            direction = sig.get("direction", "neutral")

            if strength == 0 or direction == "neutral":
                continue

            if pnl_pct > 0:
                # 수익 → 해당 기법 가중치 소폭 증가
                self.weights[key] = min(5.0, self.weights[key] + lr * strength)
            else:
                # 손실 → 해당 기법 가중치 소폭 감소
                self.weights[key] = max(0.3, self.weights[key] - lr * strength * 0.5)

        # 임계값 조정
        recent = list(self.recent_results)
        if len(recent) >= 10:
            recent_wr = sum(1 for r in recent[-10:] if r > 0) / 10
            if recent_wr < 0.3:
                # 승률 낮으면 진입 기준 올림
                self.entry_threshold = min(8.0, self.entry_threshold + 0.1)
            elif recent_wr > 0.5:
                # 승률 높으면 진입 기준 내림
                self.entry_threshold = max(3.0, self.entry_threshold - 0.05)

    def train(self):
        """GBM 모델 학습"""
        if len(self.X_buffer) < self.min_trades_to_train:
            return

        X = np.array(list(self.X_buffer))
        y = np.array(list(self.y_buffer))

        # 클래스가 최소 2개 이상이어야
        if len(set(y)) < 2:
            return

        try:
            self.scaler.fit(X)
            X_scaled = self.scaler.transform(X)

            self.model = GradientBoostingClassifier(
                n_estimators=100,
                max_depth=4,
                min_samples_leaf=5,
                learning_rate=0.1,
                subsample=0.8,
                random_state=42,
            )
            self.model.fit(X_scaled, y)
            self.is_trained = True

            # 학습 정확도
            score = self.model.score(X_scaled, y)
            logger.info(f"[{self.mode}] ML trained: {len(X)} samples, acc={score:.3f}, threshold={self.entry_threshold:.1f}")

            self.save()

        except Exception as e:
            logger.error(f"ML train error: {e}")

    def get_adjusted_score(self, raw_score: float, signals: dict, meta: dict = None) -> float:
        """ML 예측으로 점수 조정"""
        ml = self.predict(signals, meta)

        adjusted = raw_score
        if ml["trained"]:
            adjusted += ml["ml_score"]
            adjusted = max(0, min(10, adjusted))

        return adjusted

    def save(self):
        """모델 + 가중치 저장"""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            data = {
                "model": self.model,
                "scaler": self.scaler,
                "weights": self.weights,
                "entry_threshold": self.entry_threshold,
                "trade_count": self.trade_count,
                "X_buffer": list(self.X_buffer),
                "y_buffer": list(self.y_buffer),
                "recent_results": list(self.recent_results),
            }
            with open(self.model_path, "wb") as f:
                pickle.dump(data, f)
        except Exception as e:
            logger.error(f"ML save error: {e}")

    def load(self):
        """저장된 모델 로드"""
        if self.model_path.exists():
            try:
                with open(self.model_path, "rb") as f:
                    data = pickle.load(f)
                self.model = data["model"]
                self.scaler = data["scaler"]
                self.weights = data["weights"]
                self.entry_threshold = data["entry_threshold"]
                self.trade_count = data["trade_count"]
                self.X_buffer = deque(data.get("X_buffer", []), maxlen=5000)
                self.y_buffer = deque(data.get("y_buffer", []), maxlen=5000)
                self.recent_results = deque(data.get("recent_results", []), maxlen=100)
                self.is_trained = self.model is not None
                logger.info(f"[{self.mode}] ML loaded: {self.trade_count} trades, threshold={self.entry_threshold:.1f}")
            except Exception as e:
                logger.error(f"ML load error: {e}")

    def get_stats(self) -> dict:
        """현재 학습 상태"""
        recent = list(self.recent_results)
        wr = sum(1 for r in recent if r > 0) / len(recent) * 100 if recent else 0
        return {
            "mode": self.mode,
            "trained": self.is_trained,
            "trade_count": self.trade_count,
            "buffer_size": len(self.X_buffer),
            "entry_threshold": round(self.entry_threshold, 2),
            "recent_win_rate": round(wr, 1),
            "weights": {k: round(v, 2) for k, v in self.weights.items()},
        }
