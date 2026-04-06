import logging
import pickle
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import TimeSeriesSplit

from src.utils.helpers import DATA_DIR

logger = logging.getLogger(__name__)

MODEL_PATH = DATA_DIR / "ml_model.pkl"
MIN_SAMPLES = 200  # 최소 학습 데이터 수


class MLEngine:
    """Random Forest Walk-Forward ML 엔진"""

    def __init__(self):
        self.model: RandomForestClassifier | None = None
        self.is_active = False
        self.last_train_time = None
        self.feature_names = self._get_feature_names()

    def _get_feature_names(self) -> list[str]:
        return [
            # Fast Path
            "ema_alignment_score",
            "ema50_slope",
            "rsi_14",
            "rsi_7",
            "rsi_divergence",
            "bb_position",
            "bb_width",
            "bb_squeeze_bars",
            "vwap_distance_pct",
            "market_structure_trend",
            "ms_aligned",
            "bos_bars_ago",
            # Slow Path
            "ob_distance_pct",
            "ob_strength",
            "ob_htf_aligned",
            "fvg_distance_pct",
            "fvg_overlaps_ob",
            "volume_spike_ratio",
            "taker_buy_ratio",
            "funding_rate",
            "funding_extreme",
            "oi_change_1h_pct",
            "oi_price_combo",
            "ls_ratio_account",
            "cvd_slope",
            "cvd_divergence",
            "liq_nearest_distance_pct",
            "liq_magnet_direction",
            # 파생
            "atr_pct",
            "hour_of_day",
            "day_of_week",
            "streak_count",
        ]

    def extract_features(self, signals: dict, meta: dict = None) -> dict:
        """시그널 dict에서 ML 피처 추출"""
        if meta is None:
            meta = {}

        ema = signals.get("ema", {})
        rsi = signals.get("rsi", {})
        bb = signals.get("bollinger", {})
        vwap = signals.get("vwap", {})
        ms = signals.get("market_structure", {})
        ob = signals.get("order_block", {})
        fvg = signals.get("fvg", {})
        vol = signals.get("volume", {})
        fr = signals.get("funding_rate", {})
        oi = signals.get("open_interest", {})
        ls = signals.get("long_short_ratio", {})
        cvd = signals.get("cvd", {})
        liq = signals.get("liquidation", {})
        atr = signals.get("atr", {})

        # 트렌드 인코딩
        trend_map = {"bullish": 1, "bearish": -1, "ranging": 0, "unknown": 0}
        combo_map = {
            "new_longs": 1, "short_covering": 0.5,
            "new_shorts": -1, "long_liquidation": -0.5,
            "neutral": 0,
        }
        magnet_map = {"up": 1, "down": -1, "neutral": 0}

        now = datetime.utcnow()

        features = {
            "ema_alignment_score": ema.get("alignment_score", 0),
            "ema50_slope": ema.get("ema50_slope", 0),
            "rsi_14": rsi.get("rsi_14", 50),
            "rsi_7": rsi.get("rsi_7", 50),
            "rsi_divergence": 1 if rsi.get("divergence") else 0,
            "bb_position": bb.get("bb_position", 0.5),
            "bb_width": bb.get("bb_width", 0),
            "bb_squeeze_bars": bb.get("squeeze_bars", 0),
            "vwap_distance_pct": vwap.get("dist_pct", 0),
            "market_structure_trend": trend_map.get(ms.get("trend", "unknown"), 0),
            "ms_aligned": 1 if ms.get("aligned") else 0,
            "bos_bars_ago": ms.get("last_event_bars_ago", 0) or 0,
            "ob_distance_pct": ob.get("distance_pct", 0) or 0,
            "ob_strength": ob.get("strength", 0),
            "ob_htf_aligned": 1 if ob.get("htf_aligned") else 0,
            "fvg_distance_pct": fvg.get("distance_pct", 0) or 0,
            "fvg_overlaps_ob": 1 if fvg.get("overlaps_ob") else 0,
            "volume_spike_ratio": vol.get("spike_ratio", 1),
            "taker_buy_ratio": vol.get("taker_buy_ratio", 0.5),
            "funding_rate": fr.get("current_rate", 0),
            "funding_extreme": 1 if fr.get("extreme") else 0,
            "oi_change_1h_pct": oi.get("oi_change_1h_pct", 0),
            "oi_price_combo": combo_map.get(oi.get("oi_price_combo", "neutral"), 0),
            "ls_ratio_account": ls.get("ratio_account", 1),
            "cvd_slope": cvd.get("cvd_slope", 0),
            "cvd_divergence": 1 if cvd.get("price_cvd_divergence") else 0,
            "liq_nearest_distance_pct": liq.get("distance_to_nearest_pct", 5),
            "liq_magnet_direction": magnet_map.get(liq.get("magnet_direction", "neutral"), 0),
            "atr_pct": atr.get("atr_pct", 0.3),
            "hour_of_day": now.hour,
            "day_of_week": now.weekday(),
            "streak_count": meta.get("streak", 0),
        }

        return features

    def predict(self, signals: dict, meta: dict = None) -> dict:
        """ML 예측 실행"""
        if not self.is_active or self.model is None:
            return {
                "type": "ml_prediction",
                "active": False,
                "direction": "neutral",
                "strength": 0.0,
                "probability": 0.5,
            }

        features = self.extract_features(signals, meta)
        feature_array = np.array([[features[f] for f in self.feature_names]])

        try:
            proba = self.model.predict_proba(feature_array)[0]
            # class 0 = loss, class 1 = win
            win_prob = proba[1] if len(proba) > 1 else 0.5

            if win_prob > 0.6:
                direction = "long"  # 승률 높음 → 현재 방향 유지
                strength = min(1.0, (win_prob - 0.5) * 2)
            elif win_prob < 0.4:
                direction = "short"  # 승률 낮음 → 주의
                strength = min(1.0, (0.5 - win_prob) * 2)
            else:
                direction = "neutral"
                strength = 0.0

            return {
                "type": "ml_prediction",
                "active": True,
                "direction": direction,
                "strength": round(strength, 2),
                "probability": round(win_prob, 3),
            }

        except Exception as e:
            logger.error(f"ML 예측 에러: {e}")
            return {
                "type": "ml_prediction",
                "active": False,
                "direction": "neutral",
                "strength": 0.0,
                "probability": 0.5,
            }

    def train(self, trade_history: list[dict]):
        """
        Walk-Forward 학습.

        Args:
            trade_history: trades 테이블에서 가져온 완료된 매매 리스트
                각 항목: {signals_snapshot: JSON, pnl_pct: float, ...}
        """
        if len(trade_history) < MIN_SAMPLES:
            logger.info(
                f"ML 학습 데이터 부족: {len(trade_history)}/{MIN_SAMPLES} → 비활성"
            )
            self.is_active = False
            return

        logger.info(f"ML 학습 시작: {len(trade_history)}개 샘플")

        # 피처 + 라벨 구성
        X = []
        y = []
        import json

        for trade in trade_history:
            try:
                snapshot = trade.get("signals_snapshot", "{}")
                if isinstance(snapshot, str):
                    signals = json.loads(snapshot)
                else:
                    signals = snapshot

                features = self.extract_features(signals)
                feature_row = [features[f] for f in self.feature_names]

                pnl = trade.get("pnl_pct", 0)
                # 라벨: TP1(+1.5R) 이상 = Win(1), SL or 손실 = Loss(0)
                # 본전 부근(±0.1%)은 제외
                if abs(pnl) < 0.1:
                    continue
                label = 1 if pnl > 0 else 0

                X.append(feature_row)
                y.append(label)

            except Exception as e:
                logger.debug(f"ML 피처 추출 실패: {e}")
                continue

        if len(X) < MIN_SAMPLES:
            logger.info(f"유효 샘플 부족: {len(X)}/{MIN_SAMPLES}")
            self.is_active = False
            return

        X = np.array(X)
        y = np.array(y)

        # Walk-Forward: 마지막 30일 학습 → 7일 검증
        self.model = RandomForestClassifier(
            n_estimators=200,
            max_depth=8,
            min_samples_leaf=10,
            class_weight="balanced",
            random_state=42,
            n_jobs=-1,
        )

        # TimeSeriesSplit으로 검증
        tscv = TimeSeriesSplit(n_splits=3)
        scores = []

        for train_idx, val_idx in tscv.split(X):
            X_train, X_val = X[train_idx], X[val_idx]
            y_train, y_val = y[train_idx], y[val_idx]

            self.model.fit(X_train, y_train)
            score = self.model.score(X_val, y_val)
            scores.append(score)

        avg_score = np.mean(scores)
        logger.info(f"ML 학습 완료: 평균 정확도 {avg_score:.3f} (CV {len(scores)} folds)")

        # 최종 모델: 전체 데이터로 재학습
        self.model.fit(X, y)
        self.is_active = True
        self.last_train_time = datetime.utcnow()

        # 모델 저장
        self._save_model()

        # 피처 중요도 로깅
        importances = self.model.feature_importances_
        top_features = sorted(
            zip(self.feature_names, importances),
            key=lambda x: x[1],
            reverse=True,
        )[:5]
        logger.info(
            "Top 5 피처: "
            + ", ".join(f"{name}({imp:.3f})" for name, imp in top_features)
        )

    def _save_model(self):
        """모델 파일 저장"""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(MODEL_PATH, "wb") as f:
                pickle.dump(
                    {
                        "model": self.model,
                        "feature_names": self.feature_names,
                        "train_time": self.last_train_time,
                    },
                    f,
                )
            logger.info(f"ML 모델 저장: {MODEL_PATH}")
        except Exception as e:
            logger.error(f"ML 모델 저장 실패: {e}")

    def load_model(self):
        """저장된 모델 로드"""
        if MODEL_PATH.exists():
            try:
                with open(MODEL_PATH, "rb") as f:
                    data = pickle.load(f)
                self.model = data["model"]
                self.feature_names = data.get("feature_names", self.feature_names)
                self.last_train_time = data.get("train_time")
                self.is_active = True
                logger.info(
                    f"ML 모델 로드 완료 (학습: {self.last_train_time})"
                )
            except Exception as e:
                logger.error(f"ML 모델 로드 실패: {e}")
                self.is_active = False
        else:
            logger.info("ML 모델 파일 없음 → 비활성 (데이터 축적 후 학습)")
            self.is_active = False

    def needs_retrain(self, days_interval: int = 7) -> bool:
        """재학습 필요 여부"""
        if self.last_train_time is None:
            return True
        elapsed = datetime.utcnow() - self.last_train_time
        return elapsed > timedelta(days=days_interval)
