import json
import logging
import numpy as np
from pathlib import Path
from datetime import datetime
from backtest.simulator import BacktestSimulator, BacktestTrade

logger = logging.getLogger(__name__)

REPORT_DIR = Path(__file__).parent.parent / "data" / "reports"


class BacktestReport:
    """백테스트 성과 리포트 생성"""

    def __init__(self, simulator: BacktestSimulator):
        self.sim = simulator
        self.results = simulator.get_results()

    def print_summary(self):
        """콘솔 요약 출력"""
        r = self.results
        if "error" in r:
            print(f"\n[X] {r['error']}")
            return

        print("\n" + "=" * 60)
        print("  백테스트 결과 요약")
        print("=" * 60)

        print(f"\n  기간: {len(self.sim.trades)}거래")
        print(f"  초기 잔고:   ${r['initial_balance']:,.2f}")
        print(f"  최종 잔고:   ${r['final_balance']:,.2f}")
        print(f"  총 수익률:   {r['total_return_pct']:+.2f}%")

        print(f"\n  --- 매매 통계 ---")
        print(f"  총 거래:     {r['total_trades']}")
        print(f"  승리:        {r['wins']} ({r['win_rate']:.1f}%)")
        print(f"  패배:        {r['losses']}")
        print(f"  평균 수익:   ${r['avg_pnl_per_trade']:+.2f}/거래")
        print(f"  평균 승:     ${r['avg_win']:+.2f}")
        print(f"  평균 패:     ${r['avg_loss']:+.2f}")
        print(f"  Profit Factor: {r['profit_factor']:.2f}")

        print(f"\n  --- 리스크 ---")
        print(f"  최대 드로다운: {r['max_drawdown_pct']:.2f}%")
        print(f"  최대 연승:   {r['max_streak_win']}")
        print(f"  최대 연패:   {r['max_streak_loss']}")
        print(f"  총 수수료:   ${r['total_fees']:.2f}")

        print(f"\n  --- 보유 시간 ---")
        print(f"  평균 보유:   {r['avg_hold_minutes']:.0f}분")

        # 등급별 통계
        if r.get("grade_stats"):
            print(f"\n  --- 등급별 성과 ---")
            print(f"  {'등급':>4} | {'횟수':>4} | {'승률':>6} | {'평균P&L':>10} | {'총P&L':>10}")
            print(f"  {'-'*4} | {'-'*4} | {'-'*6} | {'-'*10} | {'-'*10}")
            for grade, stats in r["grade_stats"].items():
                print(
                    f"  {grade:>4} | {stats['count']:>4} | "
                    f"{stats['win_rate']:>5.1f}% | "
                    f"${stats['avg_pnl']:>+9.2f} | "
                    f"${stats['total_pnl']:>+9.2f}"
                )

        # 청산사유별
        if r.get("exit_stats"):
            print(f"\n  --- 청산 사유별 ---")
            print(f"  {'사유':<12} | {'횟수':>4} | {'평균P&L':>10}")
            print(f"  {'-'*12} | {'-'*4} | {'-'*10}")
            for reason, stats in sorted(r["exit_stats"].items(), key=lambda x: -x[1]["count"]):
                print(f"  {reason:<12} | {stats['count']:>4} | ${stats['avg_pnl']:>+9.2f}")

        print("\n" + "=" * 60)

    def print_monthly(self):
        """월별 수익률"""
        if not self.sim.trades:
            return

        monthly = {}
        for trade in self.sim.trades:
            month = datetime.fromtimestamp(trade.entry_time / 1000).strftime("%Y-%m")
            if month not in monthly:
                monthly[month] = {"pnl": 0, "count": 0, "wins": 0}
            monthly[month]["pnl"] += trade.pnl_usdt
            monthly[month]["count"] += 1
            if trade.pnl_usdt > 0:
                monthly[month]["wins"] += 1

        print(f"\n  --- 월별 수익률 ---")
        print(f"  {'월':>7} | {'거래':>4} | {'승률':>6} | {'P&L':>10}")
        print(f"  {'-'*7} | {'-'*4} | {'-'*6} | {'-'*10}")
        for month, data in sorted(monthly.items()):
            wr = data["wins"] / data["count"] * 100 if data["count"] > 0 else 0
            print(f"  {month:>7} | {data['count']:>4} | {wr:>5.1f}% | ${data['pnl']:>+9.2f}")

    def save_json(self, filename: str = None):
        """결과를 JSON 파일로 저장"""
        REPORT_DIR.mkdir(parents=True, exist_ok=True)

        if filename is None:
            filename = f"backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

        filepath = REPORT_DIR / filename

        # 거래 상세
        trades_detail = []
        for t in self.sim.trades:
            trades_detail.append({
                "id": t.id,
                "direction": t.direction,
                "grade": t.grade,
                "score": t.score,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "exit_reason": t.exit_reason,
                "leverage": t.leverage,
                "pnl_pct": t.pnl_pct,
                "pnl_usdt": t.pnl_usdt,
                "fee_total": t.fee_total,
                "hold_bars": t.hold_bars,
                "hold_minutes": t.hold_bars * 15,
            })

        output = {
            "summary": self.results,
            "trades": trades_detail,
            "equity_curve": self.sim.equity_curve,
            "generated_at": datetime.now().isoformat(),
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        logger.info(f"리포트 저장: {filepath}")
        print(f"\n  리포트 저장: {filepath}")
        return filepath

    def calculate_sharpe(self, risk_free_rate: float = 0.0) -> float:
        """샤프 비율 (일별 기준)"""
        if not self.sim.trades:
            return 0

        # 일별 수익률 계산
        daily_pnl = {}
        for t in self.sim.trades:
            day = datetime.fromtimestamp(t.entry_time / 1000).strftime("%Y-%m-%d")
            if day not in daily_pnl:
                daily_pnl[day] = 0
            daily_pnl[day] += t.pnl_pct

        if len(daily_pnl) < 2:
            return 0

        returns = list(daily_pnl.values())
        mean_return = np.mean(returns)
        std_return = np.std(returns)

        if std_return == 0:
            return 0

        # 연환산 (365일)
        sharpe = (mean_return - risk_free_rate) / std_return * np.sqrt(365)
        return round(sharpe, 2)


async def run_backtest(candles_15m_path: str = None, initial_balance: float = 10000):
    """백테스트 실행 헬퍼"""
    import pandas as pd
    from src.data.storage import Database

    sim = BacktestSimulator(initial_balance=initial_balance)

    # DB에서 캔들 로드
    if candles_15m_path:
        candles_15m = pd.read_csv(candles_15m_path)
    else:
        db = Database()
        await db.connect()
        config = load_config()
        symbol = config["exchange"]["symbol"]

        candles_raw = await db.get_candles(symbol, "15m", limit=50000)
        candles_1h_raw = await db.get_candles(symbol, "1h", limit=10000)
        await db.close()

        if not candles_raw:
            print("캔들 데이터 없음. 먼저 데이터를 수집하세요.")
            return

        candles_15m = pd.DataFrame(candles_raw)
        candles_1h = pd.DataFrame(candles_1h_raw) if candles_1h_raw else None

    print(f"캔들 데이터: {len(candles_15m)}봉")

    await sim.run(candles_15m, candles_1h if 'candles_1h' in dir() else None)

    report = BacktestReport(sim)
    report.print_summary()
    report.print_monthly()

    sharpe = report.calculate_sharpe()
    print(f"\n  샤프 비율: {sharpe}")

    report.save_json()
    return report


if __name__ == "__main__":
    import asyncio
    from src.utils.helpers import load_config

    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run_backtest())
