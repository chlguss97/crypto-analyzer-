from abc import ABC, abstractmethod
import pandas as pd


class BaseIndicator(ABC):
    """모든 기법 엔진의 추상 베이스 클래스"""

    @abstractmethod
    async def calculate(self, candles: pd.DataFrame, context: dict = None) -> dict:
        """
        시그널 계산 후 dict 반환.

        필수 키:
          - type: str (기법 이름)
          - direction: str ('long' | 'short' | 'neutral')
          - strength: float (0~1)

        Args:
            candles: OHLCV 데이터프레임 (columns: timestamp, open, high, low, close, volume)
            context: 추가 컨텍스트 (상위 TF 데이터, Redis 캐시 등)

        Returns:
            시그널 dict
        """
        pass

    @property
    @abstractmethod
    def path(self) -> str:
        """'fast' | 'slow'"""
        pass

    @property
    @abstractmethod
    def weight(self) -> float:
        """시그널 가중치"""
        pass

    @staticmethod
    def to_dataframe(candles: list[dict]) -> pd.DataFrame:
        """캔들 리스트 → DataFrame 변환 (입력 검증 포함)"""
        if not candles:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        df = pd.DataFrame(candles)
        required = ["open", "high", "low", "close", "volume"]
        for col in required:
            if col not in df.columns:
                raise ValueError(f"to_dataframe: 필수 컬럼 누락 - {col}")
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # NaN/Inf 행 제거
        df = df.dropna(subset=required)
        return df.reset_index(drop=True)
