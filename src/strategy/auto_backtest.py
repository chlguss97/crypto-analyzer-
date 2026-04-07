"""
AutoBacktest — 자동 백테스트 검증 시스템
- 매일 1회 최근 30일 데이터로 현재 전략 백테스트
- 결과 → DB 저장 + 대시보드 표시
- 전략 성능 저하 감지 → 알림
"""
import asyncio
import logging
import time
import json
import numpy as np
import pandas as pd
from datetime import datetime, timezone

from src.engine.base import BaseIndicator
from src.strategy.scalp_engine import ScalpEngine
from src.engine.regime_detector import MarketRegimeDetector

logger = logging.getLogger(__name__)


class AutoBacktest:
    """자동 백테스트 검증"""

    FEE_RATE = 0.0005
    LEVERAGE = 25
    INITIAL_BALANCE = 1000.0  # 가상 시작 잔고

    def __init__(self, db, ml_swing, ml_scalp):
        self.db = db
        self.ml_swing = ml_swing
        self.ml_scalp = ml_scalp
        self.scalp_engine = ScalpEngine()
        self.regime_detector = MarketRegimeDetector()

    async def run(self, days: int = 30, symbol: str = "BTC/USDT:USDT") -> dict:
        """
        최근 N일 백테스트 실행

        Returns:
            {
                "trades": int, "wins": int, "losses": int,
                "win_rate": float, "total_pnl_pct": float,
                "max_drawdown": float, "sharpe": float,
                "best_trade": float, "worst_trade": float,
                "avg_trade": float,
            }
        """
        logger.info(f"[BACKTEST] 자동 백테스트 시작: {days}일")
        start = time.time()

        # 5m 캔들 (스캘핑 시뮬용)
        bars = days * 24 * 12  # 5m = 12봉/시간
        candles_5m = await self.db.get_candles(symbol, "5m", limit=bars)
        candles_1m = await self.db.get_candles(symbol, "1m", limit=bars * 5)

        if not candles_5m or len(candles_5m) < 200:
            logger.warning("[BACKTEST] 캔들 부족")
            return self._empty_result()

        df_5m = BaseIndicator.to_dataframe(candles_5m)
        df_1m = BaseIndicator.to_dataframe(candles_1m) if candles_1m else None

        balance = self.INITIAL_BALANCE
        peak_balance = balance
        max_dd = 0.0
        trades = []
        position = None

        for i in range(60, len(df_5m) - 6):
            df_5m_slice = df_5m.iloc[:i + 1].copy().reset_index(drop=True)
            ts = float(df_5m_slice["timestamp"].iloc[-1])
            current_price = float(df_5m_slice["close"].iloc[-1])

            # 1m 슬라이스
            if df_1m is not None:
                df_1m_slice = df_1m[df_1m["timestamp"] <= ts].tail(100).copy().reset_index(drop=True)
                if len(df_1m_slice) < 25:
                    continue
            else:
                continue

            # 포지션 청산 체크
            if position:
                end_idx = min(i + 2, len(df_5m))
                if end_idx <= i:
                    continue
                exit_price = self._check_exit(position, df_5m.iloc[i:end_idx])
                if exit_price is not None:
                    pnl_pct = self._calc_pnl(position, exit_price)
                    trades.append({
                        "entry": position["entry_price"],
                        "exit": exit_price,
                        "direction": position["direction"],
                        "pnl_pct": pnl_pct,
                        "regime": position["regime"],
                    })
                    balance *= (1 + pnl_pct / 100)
                    if balance > peak_balance:
                        peak_balance = balance
                    dd = (peak_balance - balance) / peak_balance * 100
                    if dd > max_dd:
                        max_dd = dd
                    position = None

            # 새 진입 체크
            if position is None:
                try:
                    result = await self.scalp_engine.analyze(df_1m_slice, df_5m_slice, None)
                except Exception:
                    continue

                # ML 점수 조정 (현재 학습된 모델 사용)
                regime = self.regime_detector.detect(df_5m_slice)
                meta = {"atr_pct": result.get("atr_pct", 0.2), "regime": regime["regime"],
                        "hour": datetime.fromtimestamp(ts/1000, tz=timezone.utc).hour}

                base_score = result["score"]
                ml_score = self.ml_scalp.get_adjusted_score(
                    base_score, result.get("signals", {}), meta
                )

                # 임계값 통과 + neutral 아님
                if ml_score >= self.ml_scalp.entry_threshold and result["direction"] != "neutral":
                    sl_dist = result.get("sl_distance", current_price * 0.002)
                    tp_dist = result.get("tp_distance", sl_dist * 2)
                    direction = result["direction"]

                    position = {
                        "entry_price": current_price,
                        "direction": direction,
                        "sl_price": current_price - sl_dist if direction == "long" else current_price + sl_dist,
                        "tp_price": current_price + tp_dist if direction == "long" else current_price - tp_dist,
                        "regime": regime["regime"],
                    }

        # 통계 계산
        if not trades:
            return self._empty_result()

        wins = [t for t in trades if t["pnl_pct"] > 0]
        losses = [t for t in trades if t["pnl_pct"] <= 0]
        pnls = [t["pnl_pct"] for t in trades]
        total_pnl = (balance - self.INITIAL_BALANCE) / self.INITIAL_BALANCE * 100

        # 샤프 비율 (간이)
        if len(pnls) > 1:
            mean_pnl = np.mean(pnls)
            std_pnl = np.std(pnls)
            sharpe = (mean_pnl / std_pnl * np.sqrt(252)) if std_pnl > 0 else 0
        else:
            sharpe = 0

        result = {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1),
            "total_pnl_pct": round(total_pnl, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "sharpe_ratio": round(sharpe, 2),
            "best_trade": round(max(pnls), 2),
            "worst_trade": round(min(pnls), 2),
            "avg_trade": round(np.mean(pnls), 3),
            "elapsed_sec": round(time.time() - start, 1),
            "days_tested": days,
            "timestamp": int(time.time() * 1000),
        }

        logger.info(
            f"[BACKTEST] 완료: {result['trades']}건 | 승률 {result['win_rate']}% | "
            f"수익 {result['total_pnl_pct']:+.1f}% | MDD {result['max_drawdown_pct']}% | "
            f"샤프 {result['sharpe_ratio']}"
        )

        return result

    def _check_exit(self, pos: dict, future: pd.DataFrame) -> float | None:
        """포지션 청산 가격 확인"""
        for _, row in future.iterrows():
            high = float(row["high"])
            low = float(row["low"])
            if pos["direction"] == "long":
                if low <= pos["sl_price"]:
                    return pos["sl_price"]
                if high >= pos["tp_price"]:
                    return pos["tp_price"]
            else:
                if high >= pos["sl_price"]:
                    return pos["sl_price"]
                if low <= pos["tp_price"]:
                    return pos["tp_price"]
        return None

    def _calc_pnl(self, pos: dict, exit_price: float) -> float:
        """P&L 계산"""
        if pos["direction"] == "long":
            raw = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100 * self.LEVERAGE
        else:
            raw = (pos["entry_price"] - exit_price) / pos["entry_price"] * 100 * self.LEVERAGE
        fee = self.FEE_RATE * 2 * self.LEVERAGE * 100
        return raw - fee

    def _empty_result(self):
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
            "total_pnl_pct": 0, "max_drawdown_pct": 0, "sharpe_ratio": 0,
            "best_trade": 0, "worst_trade": 0, "avg_trade": 0,
            "elapsed_sec": 0, "days_tested": 0,
            "timestamp": int(time.time() * 1000),
        }
