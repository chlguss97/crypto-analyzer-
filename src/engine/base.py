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
        """캔들 리스트 → DataFrame 변환"""
        df = pd.DataFrame(candles)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        return df
