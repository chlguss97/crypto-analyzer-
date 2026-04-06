import asyncio
import logging
import json
import time
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.engine.fast.ema import EMAIndicator
from src.engine.fast.rsi import RSIIndicator
from src.engine.fast.bollinger import BollingerIndicator
from src.engine.fast.vwap import VWAPIndicator
from src.engine.fast.market_structure import MarketStructureIndicator
from src.engine.fast.atr import ATRIndicator
from src.engine.slow.order_block import OrderBlockIndicator
from src.engine.slow.fvg import FVGIndicator
from src.engine.slow.volume_pattern import VolumePatternIndicator
from src.engine.slow.funding_rate import FundingRateIndicator
from src.engine.slow.open_interest import OpenInterestIndicator
from src.engine.slow.liquidation import LiquidationIndicator
from src.engine.slow.long_short_ratio import LongShortRatioIndicator
from src.engine.slow.cvd import CVDIndicator
from src.engine.base import BaseIndicator
from src.signal_engine.aggregator import SignalAggregator
from src.signal_engine.grader import SignalGrader
from src.trading.leverage import LeverageCalculator
from src.utils.helpers import load_config

logger = logging.getLogger(__name__)

# 백테스트 비용 설정
TAKER_FEE = 0.0005       # 0.05%
MAKER_FEE = 0.0002       # 0.02%
SLIPPAGE = 0.0005         # 0.05%
ROUND_TRIP_COST = 0.001   # 왕복 0.10% (Taker 기준)


@dataclass
class BacktestTrade:
    """백테스트 매매 기록"""
    id: int = 0
    direction: str = ""
    grade: str = ""
    score: float = 0.0
    entry_price: float = 0.0
    entry_time: int = 0
    entry_bar: int = 0
    exit_price: float = 0.0
    exit_time: int = 0
    exit_bar: int = 0
    exit_reason: str = ""
    leverage: int = 10
    sl_price: float = 0.0
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    position_size_usdt: float = 0.0
    pnl_pct: float = 0.0
    pnl_usdt: float = 0.0
    fee_total: float = 0.0
    funding_cost: float = 0.0
    hold_bars: int = 0
    tier: int = 0           # 트레일링 단계
    remaining_pct: float = 1.0  # 잔여 물량 비율
    signals_snapshot: dict = field(default_factory=dict)


@dataclass
class BacktestPosition:
    """활성 포지션 (시뮬레이션용)"""
    trade: BacktestTrade
    current_sl: float = 0.0
    tier: int = 0
    remaining_pct: float = 1.0
    partial_pnl: float = 0.0  # 부분 청산 누적 수익

    @property
    def hold_bars(self) -> int:
        return self.trade.exit_bar - self.trade.entry_bar


class BacktestSimulator:
    """과거 데이터 기반 백테스트 시뮬레이터"""

    def __init__(self, initial_balance: float = 10000.0):
        self.config = load_config()
        self.trailing_cfg = self.config["trailing"]
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.peak_balance = initial_balance

        # 엔진
        self.fast_engines = [
            EMAIndicator(), RSIIndicator(), BollingerIndicator(),
            VWAPIndicator(), MarketStructureIndicator(), ATRIndicator(),
        ]
        self.slow_engines = [
            OrderBlockIndicator(), FVGIndicator(), VolumePatternIndicator(),
            FundingRateIndicator(), OpenInterestIndicator(),
            LiquidationIndicator(), LongShortRatioIndicator(), CVDIndicator(),
        ]
        self.aggregator = SignalAggregator()
        self.grader = SignalGrader()
        self.leverage_calc = LeverageCalculator()

        # 결과
        self.trades: list[BacktestTrade] = []
        self.equity_curve: list[dict] = []
        self.position: Optional[BacktestPosition] = None
        self.streak = 0
        self.daily_pnl = 0.0
        self.current_day = 0
        self.trade_count = 0

    async def run(self, candles_15m: pd.DataFrame,
                  candles_1h: pd.DataFrame = None,
                  funding_rates: list[dict] = None):
        """
        백테스트 실행.

        Args:
            candles_15m: 15m OHLCV DataFrame
            candles_1h: 1H OHLCV DataFrame (없으면 15m에서 리샘플링)
            funding_rates: 펀딩비 히스토리 [{timestamp, rate}, ...]
        """
        logger.info(
            f"백테스트 시작: {len(candles_15m)}봉 | "
            f"초기 잔고 ${self.initial_balance}"
        )

        # 1H 캔들 없으면 15m에서 생성
        if candles_1h is None:
            candles_1h = self._resample_to_1h(candles_15m)

        lookback = 300  # 시그널 계산에 필요한 최소 봉
        total_bars = len(candles_15m)

        for i in range(lookback, total_bars):
            # 현재 바까지의 슬라이스
            window = candles_15m.iloc[max(0, i - lookback):i + 1].reset_index(drop=True)
            current_bar = candles_15m.iloc[i]
            current_price = current_bar["close"]
            current_ts = current_bar["timestamp"]

            # 일일 리셋
            day = current_ts // 86_400_000
            if day != self.current_day:
                self.current_day = day
                self.daily_pnl = 0.0

            # 활성 포지션 체크
            if self.position:
                closed = self._check_position(current_bar, i)
                if closed:
                    continue

            # 새 진입 시그널 (15m봉 완성 시마다)
            if not self.position:
                await self._check_entry(window, candles_1h, i, current_price, current_ts, funding_rates)

            # 에퀴티 기록 (4시간마다)
            if i % 16 == 0:
                unrealized = 0
                if self.position:
                    unrealized = self._calc_unrealized(current_price)
                self.equity_curve.append({
                    "timestamp": current_ts,
                    "balance": round(self.balance + unrealized, 2),
                    "bar": i,
                })

        # 미청산 포지션 강제 종료
        if self.position:
            last_price = candles_15m.iloc[-1]["close"]
            self._close_position(last_price, len(candles_15m) - 1,
                                 candles_15m.iloc[-1]["timestamp"], "backtest_end")

        logger.info(
            f"백테스트 완료: {len(self.trades)}거래 | "
            f"최종 잔고 ${self.balance:.2f} | "
            f"수익률 {(self.balance / self.initial_balance - 1) * 100:.1f}%"
        )

    async def _check_entry(self, window: pd.DataFrame, candles_1h: pd.DataFrame,
                           bar_idx: int, current_price: float, current_ts: int,
                           funding_rates: list[dict] = None):
        """진입 시그널 체크"""
        # 일일 손실 한도
        if self.daily_pnl <= -5.0:
            return

        # 쿨다운
        if self.streak >= 5:
            return

        # Fast Path
        fast_signals = {}
        context = {}

        # 1H 추세
        ts_idx = candles_1h["timestamp"].searchsorted(current_ts)
        htf_window = candles_1h.iloc[max(0, ts_idx - 100):ts_idx + 1].reset_index(drop=True)
        if len(htf_window) >= 20:
            ms = MarketStructureIndicator()
            htf_result = await ms.calculate(htf_window)
            context["htf_trend"] = htf_result.get("trend", "unknown")

        for engine in self.fast_engines:
            try:
                result = await engine.calculate(window, context)
                fast_signals[result["type"]] = result
                if result["type"] == "bollinger":
                    context["bb_position"] = result["bb_position"]
            except Exception:
                pass

        # Slow Path (간소화 — context 없이 캔들만)
        slow_signals = {}
        slow_context = {
            "funding_rate": 0, "funding_next_min": 999,
            "oi_current": 0, "oi_history": [],
            "ls_ratio_account": 1.0, "ls_history": [],
            "cvd_15m": 0, "cvd_1h": 0,
            "funding_history": [],
        }

        # 펀딩비 주입
        if funding_rates:
            matching = [f for f in funding_rates if f.get("timestamp", 0) <= current_ts]
            if matching:
                slow_context["funding_rate"] = matching[-1].get("rate", 0)
                slow_context["funding_history"] = matching[-3:]

        for engine in self.slow_engines:
            try:
                result = await engine.calculate(window, slow_context)
                slow_signals[result["type"]] = result
                if result["type"] == "order_block" and result.get("ob_zone"):
                    slow_context["ob_zones"] = [result["ob_zone"]]
                if result["type"] == "open_interest":
                    slow_context["oi_spike"] = result.get("oi_spike", False)
            except Exception:
                pass

        # 합산 + 등급
        aggregated = self.aggregator.aggregate(fast_signals, slow_signals)

        risk_state = {
            "daily_pnl_pct": self.daily_pnl,
            "current_drawdown_pct": max(0, (self.peak_balance - self.balance) / self.peak_balance * 100),
            "open_positions": 0,
            "same_direction_count": 0,
            "streak": self.streak,
            "cooldown_active": False,
            "funding_blackout": False,
            "has_same_symbol": False,
        }

        grade_result = self.grader.grade(aggregated, risk_state)

        if not grade_result["tradeable"]:
            return

        # 레버리지 계산
        atr_signal = fast_signals.get("atr", {})
        atr_pct = atr_signal.get("atr_pct", 0.3)
        lev = self.leverage_calc.calculate(grade_result["grade"], atr_pct, self.streak)

        # 포지션 사이즈
        margin = self.leverage_calc.calculate_position_size(
            self.balance, lev["leverage"], lev["sl_pct"], grade_result["size_pct"]
        )
        if margin <= 0:
            return

        # SL/TP
        direction = grade_result["direction"]
        sl_dist = atr_signal.get("sl_distance", current_price * 0.003)
        tp1_dist = atr_signal.get("tp1_distance", sl_dist * 1.5)
        tp2_dist = atr_signal.get("tp2_distance", sl_dist * 2.5)

        # 슬리피지 적용
        if direction == "long":
            entry = current_price * (1 + SLIPPAGE)
            sl = entry - sl_dist
            tp1 = entry + tp1_dist
            tp2 = entry + tp2_dist
        else:
            entry = current_price * (1 - SLIPPAGE)
            sl = entry + sl_dist
            tp1 = entry - tp1_dist
            tp2 = entry - tp2_dist

        # 진입 수수료
        size_usdt = margin * lev["leverage"]
        entry_fee = size_usdt * TAKER_FEE

        self.trade_count += 1
        trade = BacktestTrade(
            id=self.trade_count,
            direction=direction,
            grade=grade_result["grade"],
            score=grade_result["score"],
            entry_price=entry,
            entry_time=current_ts,
            entry_bar=bar_idx,
            leverage=lev["leverage"],
            sl_price=sl,
            tp1_price=tp1,
            tp2_price=tp2,
            position_size_usdt=size_usdt,
            fee_total=entry_fee,
            signals_snapshot=aggregated.get("signals_detail", {}),
        )

        self.position = BacktestPosition(
            trade=trade,
            current_sl=sl,
        )

    def _check_position(self, bar: pd.Series, bar_idx: int) -> bool:
        """활성 포지션 체크 (SL/TP/시간/트레일링)"""
        pos = self.position
        trade = pos.trade
        high = bar["high"]
        low = bar["low"]
        close = bar["close"]
        ts = bar["timestamp"]

        hold_bars = bar_idx - trade.entry_bar
        hours = hold_bars * 15 / 60  # 15m봉 기준

        # P&L 계산
        if trade.direction == "long":
            pnl_pct = (close - trade.entry_price) / trade.entry_price * 100
        else:
            pnl_pct = (trade.entry_price - close) / trade.entry_price * 100

        # 1. SL 체크 (바 내에서 SL 터치 여부)
        sl_hit = False
        if trade.direction == "long" and low <= pos.current_sl:
            sl_hit = True
            exit_price = pos.current_sl * (1 - SLIPPAGE)
        elif trade.direction == "short" and high >= pos.current_sl:
            sl_hit = True
            exit_price = pos.current_sl * (1 + SLIPPAGE)

        if sl_hit:
            self._close_position(exit_price, bar_idx, ts, "sl")
            return True

        # 2. 시간 청산
        if hours >= 6:
            self._close_position(close, bar_idx, ts, "time_6h")
            return True
        if hours >= 4 and pos.tier < 3:
            self._partial_close(pos, 1.0, close, bar_idx, ts, "time_4h")
            return True
        if hours >= 2 and pos.tier < 2 and pnl_pct < 1.5:
            self._partial_close(pos, 0.75, close, bar_idx, ts, "time_2h")
        if hours >= 1 and pos.tier < 1 and pnl_pct < 0.3:
            self._partial_close(pos, 0.5, close, bar_idx, ts, "time_1h")

        # 3. 트레일링 업데이트
        cfg = self.trailing_cfg

        # Tier 1: +0.8% → 본전 확보
        if pnl_pct >= cfg["breakeven_trigger"] * 100 and pos.tier < 1:
            pos.tier = 1
            fee_offset = trade.entry_price * 0.001
            if trade.direction == "long":
                pos.current_sl = trade.entry_price + fee_offset
            else:
                pos.current_sl = trade.entry_price - fee_offset

        # Tier 2: TP1 (+1.5%) → 50% 청산
        if pnl_pct >= cfg["tp1_trigger"] * 100 and pos.tier < 2:
            pos.tier = 2
            self._partial_close(pos, cfg["tp1_close_pct"], close, bar_idx, ts, "tp1")
            if trade.direction == "long":
                pos.current_sl = trade.entry_price * 1.005
            else:
                pos.current_sl = trade.entry_price * 0.995

        # Tier 3: TP2 (+2.5%) → 30% 청산
        if pnl_pct >= cfg["tp2_trigger"] * 100 and pos.tier < 3:
            pos.tier = 3
            self._partial_close(pos, cfg["tp2_close_pct"], close, bar_idx, ts, "tp2")
            if trade.direction == "long":
                pos.current_sl = trade.entry_price * 1.015
            else:
                pos.current_sl = trade.entry_price * 0.985

        # Tier 4: +3.5% → ATR 트레일링
        if pnl_pct >= 3.5 and pos.tier >= 3:
            pos.tier = 4
            trail = abs(close - trade.entry_price) * 0.2
            if trade.direction == "long":
                new_sl = close - trail
                if new_sl > pos.current_sl:
                    pos.current_sl = new_sl
            else:
                new_sl = close + trail
                if new_sl < pos.current_sl:
                    pos.current_sl = new_sl

        return False

    def _partial_close(self, pos: BacktestPosition, close_pct: float,
                       price: float, bar_idx: int, ts: int, reason: str):
        """부분 청산 시뮬레이션"""
        trade = pos.trade
        closing = pos.remaining_pct * close_pct
        if closing <= 0:
            return

        # 부분 P&L
        if trade.direction == "long":
            partial_pnl_pct = (price - trade.entry_price) / trade.entry_price * 100
        else:
            partial_pnl_pct = (trade.entry_price - price) / trade.entry_price * 100

        partial_pnl_usdt = trade.position_size_usdt * closing * partial_pnl_pct / 100
        exit_fee = trade.position_size_usdt * closing * TAKER_FEE
        partial_pnl_usdt -= exit_fee

        pos.partial_pnl += partial_pnl_usdt
        pos.remaining_pct -= closing
        trade.fee_total += exit_fee

        # 전량 청산됨
        if pos.remaining_pct <= 0.01:
            self._finalize_trade(trade, price, bar_idx, ts, reason, pos.partial_pnl)
            self.position = None

    def _close_position(self, exit_price: float, bar_idx: int, ts: int, reason: str):
        """전량 청산"""
        pos = self.position
        trade = pos.trade

        # 잔여 물량 P&L
        if trade.direction == "long":
            remaining_pnl_pct = (exit_price - trade.entry_price) / trade.entry_price * 100
        else:
            remaining_pnl_pct = (trade.entry_price - exit_price) / trade.entry_price * 100

        remaining_usdt = trade.position_size_usdt * pos.remaining_pct * remaining_pnl_pct / 100
        exit_fee = trade.position_size_usdt * pos.remaining_pct * TAKER_FEE
        remaining_usdt -= exit_fee
        trade.fee_total += exit_fee

        total_pnl = pos.partial_pnl + remaining_usdt
        self._finalize_trade(trade, exit_price, bar_idx, ts, reason, total_pnl)
        self.position = None

    def _finalize_trade(self, trade: BacktestTrade, exit_price: float,
                        bar_idx: int, ts: int, reason: str, total_pnl: float):
        """매매 기록 확정"""
        trade.exit_price = exit_price
        trade.exit_time = ts
        trade.exit_bar = bar_idx
        trade.exit_reason = reason
        trade.hold_bars = bar_idx - trade.entry_bar
        trade.pnl_usdt = round(total_pnl, 2)
        trade.pnl_pct = round(total_pnl / (trade.position_size_usdt / trade.leverage) * 100, 2) \
            if trade.position_size_usdt > 0 else 0

        self.balance += total_pnl
        self.daily_pnl += trade.pnl_pct

        if self.balance > self.peak_balance:
            self.peak_balance = self.balance

        # 연패 추적
        if total_pnl < 0:
            self.streak += 1
        else:
            self.streak = 0

        self.trades.append(trade)

    def _calc_unrealized(self, current_price: float) -> float:
        """미실현 손익"""
        if not self.position:
            return 0
        trade = self.position.trade
        if trade.direction == "long":
            pnl_pct = (current_price - trade.entry_price) / trade.entry_price * 100
        else:
            pnl_pct = (trade.entry_price - current_price) / trade.entry_price * 100
        return trade.position_size_usdt * self.position.remaining_pct * pnl_pct / 100

    def _resample_to_1h(self, candles_15m: pd.DataFrame) -> pd.DataFrame:
        """15m → 1H 리샘플링"""
        df = candles_15m.copy()
        df["dt"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("dt", inplace=True)

        resampled = df.resample("1h").agg({
            "timestamp": "first",
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }).dropna().reset_index(drop=True)

        return resampled

    def get_results(self) -> dict:
        """백테스트 결과 요약"""
        if not self.trades:
            return {"error": "거래 없음"}

        wins = [t for t in self.trades if t.pnl_usdt > 0]
        losses = [t for t in self.trades if t.pnl_usdt <= 0]

        total_pnl = sum(t.pnl_usdt for t in self.trades)
        total_fees = sum(t.fee_total for t in self.trades)

        # 최대 드로다운
        max_dd = 0
        peak = self.initial_balance
        running = self.initial_balance
        for t in self.trades:
            running += t.pnl_usdt
            if running > peak:
                peak = running
            dd = (peak - running) / peak * 100
            if dd > max_dd:
                max_dd = dd

        # 평균 보유 시간 (분)
        avg_hold = np.mean([t.hold_bars * 15 for t in self.trades])

        # 등급별 통계
        grade_stats = {}
        for grade in ["A+", "A", "B+", "B", "B-"]:
            gt = [t for t in self.trades if t.grade == grade]
            if gt:
                grade_stats[grade] = {
                    "count": len(gt),
                    "win_rate": len([t for t in gt if t.pnl_usdt > 0]) / len(gt) * 100,
                    "avg_pnl": np.mean([t.pnl_usdt for t in gt]),
                    "total_pnl": sum(t.pnl_usdt for t in gt),
                }

        # 청산사유별 통계
        exit_stats = {}
        for reason in set(t.exit_reason for t in self.trades):
            rt = [t for t in self.trades if t.exit_reason == reason]
            exit_stats[reason] = {
                "count": len(rt),
                "avg_pnl": round(np.mean([t.pnl_usdt for t in rt]), 2),
            }

        return {
            "initial_balance": self.initial_balance,
            "final_balance": round(self.balance, 2),
            "total_return_pct": round((self.balance / self.initial_balance - 1) * 100, 2),
            "total_trades": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(self.trades) * 100, 1),
            "total_pnl": round(total_pnl, 2),
            "total_fees": round(total_fees, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "avg_pnl_per_trade": round(total_pnl / len(self.trades), 2),
            "avg_win": round(np.mean([t.pnl_usdt for t in wins]), 2) if wins else 0,
            "avg_loss": round(np.mean([t.pnl_usdt for t in losses]), 2) if losses else 0,
            "profit_factor": round(
                abs(sum(t.pnl_usdt for t in wins)) /
                abs(sum(t.pnl_usdt for t in losses))
                if losses and sum(t.pnl_usdt for t in losses) != 0 else 0, 2
            ),
            "avg_hold_minutes": round(avg_hold, 0),
            "max_streak_loss": self._max_streak(self.trades, "loss"),
            "max_streak_win": self._max_streak(self.trades, "win"),
            "grade_stats": grade_stats,
            "exit_stats": exit_stats,
        }

    def _max_streak(self, trades: list, streak_type: str) -> int:
        """최대 연승/연패"""
        max_s = 0
        current = 0
        for t in trades:
            if (streak_type == "win" and t.pnl_usdt > 0) or \
               (streak_type == "loss" and t.pnl_usdt <= 0):
                current += 1
                max_s = max(max_s, current)
            else:
                current = 0
        return max_s
