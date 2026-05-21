"""
ModelManager — LSTM 모델 관리 (프로 원문 일치)

Phase 1: 모델 없음 (LSTMModel은 tanh fallback 사용)
Phase 3: DeepLOB5 모델 로드 + 정확도 모니터링

프로 원문: "LSTM은 enhancement, not requirement"
프로 원문: "accuracy < 52% → model adds noise, not signal"
"""

import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent.parent / "data"
MODEL_PATH = DATA_DIR / "deeplob5.pt"


class ModelManager:
    """LSTM 모델 관리 — 로드/정확도 추적/비활성화"""

    def __init__(self, config: dict = None):
        cfg = (config or {}).get("scalp", {})
        self.lstm_enabled = cfg.get("lstm_enabled", False)
        self.min_accuracy = cfg.get("lstm_min_accuracy", 0.52)
        self.model = None
        self.accuracy = 0.0
        self.last_check = 0

        if self.lstm_enabled:
            self._try_load()

    def has_valid_model(self) -> bool:
        """학습된 LSTM 모델이 사용 가능한지"""
        return self.model is not None and self.accuracy >= self.min_accuracy

    def _try_load(self):
        """모델 파일이 있으면 로드"""
        if not MODEL_PATH.exists():
            logger.info("[ModelManager] 모델 파일 없음 → tanh fallback")
            return

        try:
            import torch
            self.model = torch.load(MODEL_PATH, map_location="cpu", weights_only=False)
            self.model.eval()
            self.accuracy = 0.55  # 초기값 (검증 전)
            logger.info(f"[ModelManager] DeepLOB5 로드 완료: {MODEL_PATH.name}")
        except Exception as e:
            logger.warning(f"[ModelManager] 모델 로드 실패: {e}")
            self.model = None

    def check_for_update(self):
        """5분마다 모델 파일 갱신 체크 (핫 리로드)"""
        now = time.time()
        if now - self.last_check < 300:
            return
        self.last_check = now

        if not self.lstm_enabled:
            return

        if MODEL_PATH.exists():
            try:
                mtime = MODEL_PATH.stat().st_mtime
                if self.model is None or mtime > self.last_check - 300:
                    self._try_load()
            except Exception:
                pass

    def update_accuracy(self, accuracy: float):
        """정확도 업데이트 — 52% 미만 시 비활성화"""
        self.accuracy = accuracy
        if accuracy < self.min_accuracy and self.model is not None:
            logger.warning(
                f"[ModelManager] 정확도 {accuracy:.1%} < {self.min_accuracy:.0%} "
                f"→ LSTM 비활성화 (noise, not signal)"
            )
            self.model = None

    def get_stats(self) -> dict:
        """상태 정보 (대시보드/텔레그램용)"""
        return {
            "lstm_enabled": self.lstm_enabled,
            "has_model": self.model is not None,
            "accuracy": round(self.accuracy * 100, 1),
        }
