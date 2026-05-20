"""
Welford Online Z-Score Normalizer

O(1) memory per feature, sliding window variant.
Reference: Welford (1962), "Note on a Method for Calculating Corrected Sums of Squares"
"""

from collections import deque
import math


class WelfordZScore:
    """단일 피처의 온라인 z-score 정규화 (슬라이딩 윈도우)"""

    def __init__(self, window: int = 100):
        self._window = window
        self._values: deque[float] = deque(maxlen=window)
        self._mean = 0.0
        self._m2 = 0.0
        self._count = 0

    def update(self, x: float) -> float:
        """새 값 추가 + z-score 반환. 데이터 부족 시 0.0 반환."""
        # 윈도우가 찬 경우: 가장 오래된 값 제거 효과 반영
        if self._count >= self._window:
            old = self._values[0]
            self._count -= 1
            old_mean = self._mean
            self._mean = (old_mean * (self._count + 1) - old) / max(self._count, 1)
            self._m2 -= (old - old_mean) * (old - self._mean)
            self._m2 = max(0.0, self._m2)  # 부동소수점 보정

        self._values.append(x)
        self._count += 1
        delta = x - self._mean
        self._mean += delta / self._count
        delta2 = x - self._mean
        self._m2 += delta * delta2

        if self._count < 10:
            return 0.0  # 최소 10개 관측 전까지는 정규화 안 함

        std = math.sqrt(self._m2 / self._count)
        if std < 1e-10:
            return 0.0

        z = (x - self._mean) / std
        return max(-5.0, min(5.0, z))  # 극단값 클리핑

    @property
    def mean(self) -> float:
        return self._mean

    @property
    def std(self) -> float:
        if self._count < 2:
            return 0.0
        return math.sqrt(self._m2 / self._count)

    @property
    def count(self) -> int:
        return self._count


class FeatureNormalizer:
    """다수 피처의 온라인 z-score 정규화 관리자"""

    def __init__(self, window: int = 100):
        self._window = window
        self._normalizers: dict[str, WelfordZScore] = {}

    def update(self, name: str, value: float) -> float:
        """피처 값 업데이트 + z-score 반환"""
        if name not in self._normalizers:
            self._normalizers[name] = WelfordZScore(self._window)
        return self._normalizers[name].update(value)

    def update_all(self, features: dict[str, float]) -> dict[str, float]:
        """여러 피처를 한번에 정규화"""
        return {name: self.update(name, val) for name, val in features.items()}

    def get_stats(self) -> dict:
        """디버깅용: 각 피처의 mean/std/count"""
        return {
            name: {"mean": n.mean, "std": n.std, "count": n.count}
            for name, n in self._normalizers.items()
        }
