"""
GridEngine — BTC ATR-Adaptive Grid Trading

4레벨 양방향 그리드 (2 buy + 2 sell).
방향 예측 불필요 — 가격 진동에서 구조적 수익.
"""

import asyncio
import json
import logging
import signal
import sys
import time as _time
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.helpers import load_config, load_env
from src.data.storage import Database, RedisClient
from src.data.candle_collector import CandleCollector
from src.data.ws_stream import WebSocketStream
from src.strategy.grid_engine import GridEngine
from src.strategy.regime_detector import RegimeDetector
from src.trading.risk_manager import RiskManager
from src.trading.executor import OrderExecutor
from src.monitoring.telegram_bot import TelegramNotifier
from src.monitoring.trade_logger import TradeLogger, _append_jsonl

import os
os.environ["TZ"] = "Asia/Seoul"
try:
    _time.tzset()
except AttributeError:
    pass

# ── 로깅 ──
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
from logging.handlers import TimedRotatingFileHandler as _TRH
_P = Path
_log_dir = _P(__file__).parent.parent / "data" / "logs"
_log_dir.mkdir(parents=True, exist_ok=True)
_fh = _TRH(_log_dir / "bot.log", when="W0", backupCount=520, encoding="utf-8", utc=True)
_fh.suffix = "%Y-W%W"
_fh.setLevel(logging.WARNING)
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logging.getLogger().addHandler(_fh)

logger = logging.getLogger("GridBot")


class GridBot:
    """BTC ATR-Adaptive Grid Trading Bot"""

    def __init__(self):
        load_env()
        self.config = load_config()
        self.symbol = self.config["exchange"]["symbol"]

        # 인프라
        self.db = Database()
        self.redis = RedisClient()
        self.candle_collector = CandleCollector(self.db)
        self.ws_stream = WebSocketStream(self.redis, db=self.db)

        # 매매 엔진
        self.executor = OrderExecutor()
        self.risk_manager = RiskManager(self.redis, executor=self.executor)

        # 레짐 감지 + 그리드
        self.regime_detector: RegimeDetector | None = None
        self.grid_engine: GridEngine | None = None

        # 모니터링
        self.telegram = TelegramNotifier()
        self.trade_logger = TradeLogger()

        # 상태
        self._running = False

    async def initialize(self):
        logger.info("=" * 50)
        logger.info("GridBot — ATR-Adaptive Grid Trading")
        logger.info("=" * 50)

        await self.db.connect()
        await self.redis.connect()
        await self.candle_collector.init_exchange()
        await self.telegram.initialize()
        await self.executor.initialize()

        # 잔고 + 리스크
        balance = await self.executor.get_balance()
        await self.risk_manager.initialize(balance)
        logger.info(f"잔고: ${balance:.2f}")

        # 캔들 백필 (ATR 계산용)
        logger.info("캔들 백필 시작...")
        await self.candle_collector.backfill_all()
        logger.info("캔들 백필 완료")

        # ATR 계산용 캔들 캐시 확인
        try:
            candles_5m = await self.db.get_candles(self.symbol, "5m", limit=60)
            if candles_5m:
                logger.info(f"5분봉 캐시: {len(candles_5m)}개 (ATR 계산 준비)")
        except Exception as e:
            logger.warning(f"5분봉 캐시 확인 실패: {e}")

        # RegimeDetector 초기화
        self.regime_detector = RegimeDetector(
            redis=self.redis, ws_stream=self.ws_stream, config=self.config,
        )

        # GridEngine 초기화
        self.grid_engine = GridEngine(
            executor=self.executor, db=self.db, redis=self.redis,
            telegram=self.telegram, risk_manager=self.risk_manager,
            config=self.config, regime_detector=self.regime_detector,
        )
        logger.info("[GRID] 그리드 엔진 + 레짐 감지 초기화 완료")

    # ══════════════════════════════════════════════════
    #  지원 루프들
    # ══════════════════════════════════════════════════

    async def periodic_candle_update(self):
        """캔들 REST 백업 (120초, WS가 실시간 처리하므로 백업용)"""
        while self._running:
            try:
                for tf in ["1m", "5m", "15m", "1h", "4h", "1d", "1w"]:
                    candles = await self.candle_collector.fetch_candles(tf, limit=5)
                    if candles:
                        await self.db.insert_candles(self.symbol, tf, candles)
            except Exception as e:
                logger.error(f"캔들 REST 에러: {e}")
            await asyncio.sleep(120)

    async def periodic_daily_reset(self):
        last_reset_date = None
        while self._running:
            now_dt = datetime.now(timezone.utc)
            today = now_dt.date()
            if last_reset_date is None:
                last_reset_date = today

            if today > last_reset_date:
                last_reset_date = today
                await self.risk_manager.reset_daily()

                try:
                    bal = await self.executor.get_balance()
                    grid_status = self.grid_engine.get_status() if self.grid_engine else {}
                    grid_pnl = await self.db.get_grid_pnl_summary(hours=24)

                    report = (
                        f"\U0001f4ca <b>Daily Report | {now_dt.strftime('%Y-%m-%d')}</b>\n\n"
                        f"Balance: ${bal:,.2f}\n"
                        f"Grid: {'ACTIVE' if grid_status.get('active') else 'PAUSED'}\n"
                        f"24h: {grid_pnl['cycles']}사이클 ${grid_pnl['pnl']:+.2f}"
                    )
                    await self.telegram._send(report)
                except Exception:
                    pass

            await asyncio.sleep(60)

    async def periodic_heartbeat(self):
        while self._running:
            await self.redis.set("sys:last_heartbeat", str(int(_time.time())), ttl=120)
            try:
                bal = await asyncio.wait_for(self.executor.get_balance(), timeout=5.0)
                if bal and bal > 0:
                    await self.redis.set("sys:balance", f"{bal:.2f}", ttl=300)
            except Exception:
                pass

            # 1시간마다 스냅샷
            if _time.time() - getattr(self, "_last_snap", 0) >= 3600:
                self._last_snap = _time.time()
                try:
                    bal = float(await self.redis.get("sys:balance") or 0)
                    mode = await self.redis.get("regime:mode") or "ACTIVE"
                    grid_status = self.grid_engine.get_status() if self.grid_engine else {}
                    _append_jsonl({
                        "type": "hourly_snapshot",
                        "balance": round(bal, 2),
                        "regime_mode": mode,
                        "grid_active": grid_status.get("active", False),
                        "grid_cycles": grid_status.get("total_cycles", 0),
                        "grid_pnl": grid_status.get("total_pnl", 0),
                    })
                except Exception:
                    pass

            await asyncio.sleep(60)

    async def periodic_dashboard_commands(self):
        while self._running:
            try:
                if not self.redis._client:
                    await asyncio.sleep(5)
                    continue
                raw = await self.redis._client.blpop("cmd:bot", timeout=5)
                if not raw:
                    continue
                _, payload = raw
                cmd = json.loads(payload)
                action = cmd.get("action")
                logger.info(f"[CMD] {action}: {cmd}")

                if action == "close_all" and self.grid_engine:
                    await self.grid_engine.stop()
                    logger.info("[CMD] 그리드 정지")
                elif action == "notify":
                    await self.telegram._send(cmd.get("msg", ""))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[CMD] 에러: {e}")
                await asyncio.sleep(2)

    # ══════════════════════════════════════════════════
    #  메인
    # ══════════════════════════════════════════════════

    async def run(self):
        await self.initialize()
        self._running = True

        logger.info("봇 시작 — GridBot")
        await self.redis.set("sys:bot_status", "running")
        await self.redis.set("sys:autotrading", "on")

        self.telegram.redis = self.redis
        self.telegram.executor = self.executor
        self.telegram.risk_manager = self.risk_manager
        self.telegram.grid_engine = self.grid_engine

        await self.telegram.notify_bot_status("running")
        try:
            bal = await self.executor.get_balance()
            grid_cfg = self.config.get("grid", {})
            await self.telegram._send(
                f"\U0001f7e2 <b>GridBot v3 — Leading Regime Detection</b>\n"
                f"Balance: ${bal:,.2f} | Target Lev: {grid_cfg.get('target_leverage', 8)}x\n"
                f"Size: {grid_cfg.get('size_btc', 0.01)} BTC/level | Spacing: {grid_cfg.get('spacing_min_pct', 0.15)}%~{grid_cfg.get('spacing_max_pct', 0.50)}%"
            )
        except Exception:
            pass

        tasks = [
            # 데이터
            asyncio.create_task(self.ws_stream.start()),
            asyncio.create_task(self.periodic_candle_update()),
            # 레짐 감지 + 그리드
            asyncio.create_task(self.regime_detector.run()),
            asyncio.create_task(self.grid_engine.run()),
            # 지원
            asyncio.create_task(self.periodic_daily_reset()),
            asyncio.create_task(self.periodic_heartbeat()),
            asyncio.create_task(self.periodic_dashboard_commands()),
            asyncio.create_task(self.telegram.poll_commands()),
        ]

        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, r in enumerate(results):
                if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                    logger.error(f"태스크 {i} 종료: {r}", exc_info=r)
        except asyncio.CancelledError:
            logger.info("봇 종료 중...")
        finally:
            self._running = False
            self.ws_stream.stop()
            await self.redis.set("sys:bot_status", "stopped")
            await self.telegram.notify_bot_status("stopped")
            await self.cleanup()

    async def cleanup(self):
        logger.info("=== Graceful Shutdown ===")
        if self.grid_engine:
            await self.grid_engine.stop()
        await self.candle_collector.close()
        await self.executor.close()
        await self.redis.close()
        await self.db.close()


# ── 엔트리포인트 ──

async def main():
    engine = GridBot()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown(engine)))
        except NotImplementedError:
            pass

    await engine.run()


async def shutdown(engine):
    logger.info("종료 신호 수신")
    engine._running = False


if __name__ == "__main__":
    asyncio.run(main())
