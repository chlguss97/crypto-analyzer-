"""
ScalpBot — Jay 단타법 (StochRSI + MACD)

BTC 무기한 선물 단타 자동매매.
후행 확인 진입, 먹고 나감.
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
from src.strategy.scalp_engine import ScalpEngine
from src.data.order_stream import OrderStream
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

logger = logging.getLogger("ScalpBot")


class ScalpBot:
    """BTC Jay 단타법 자동매매 Bot"""

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

        # 전략 + 주문 WS
        self.engine: ScalpEngine | None = None
        self.order_stream: OrderStream = OrderStream()

        # 모니터링
        self.telegram = TelegramNotifier()
        self.trade_logger = TradeLogger()

        # 상태
        self._running = False

    async def initialize(self):
        logger.info("=" * 50)
        logger.info("ScalpBot — Jay 단타법 (StochRSI + MACD)")
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

        # 캔들 백필 (지표 계산용)
        logger.info("캔들 백필 시작...")
        await self.candle_collector.backfill_all()
        logger.info("캔들 백필 완료")

        # ScalpEngine 초기화
        self.engine = ScalpEngine(
            executor=self.executor, db=self.db, redis=self.redis,
            telegram=self.telegram, risk_manager=self.risk_manager,
            config=self.config,
        )
        # OrderStream → ScalpEngine 콜백 연결
        self.order_stream.on_order_update = self.engine.on_order_update
        logger.info("[SCALP] 스캘프 엔진 + WS 체결 감지 초기화 완료")

    # ══════════════════════════════════════════════════
    #  지원 루프들
    # ══════════════════════════════════════════════════

    async def periodic_candle_update(self):
        """캔들 REST 백업 (120초, WS가 실시간 처리하므로 백업용)"""
        while self._running:
            try:
                for tf in ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]:
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
                    engine_status = self.engine.get_status() if self.engine else {}
                    scalp_pnl = await self.db.get_scalp_pnl_summary(hours=24)

                    report = (
                        f"\U0001f4ca <b>Daily Report | {now_dt.strftime('%Y-%m-%d')}</b>\n\n"
                        f"Balance: ${bal:,.2f}\n"
                        f"Scalp: {'ACTIVE' if engine_status.get('active') else 'PAUSED'}\n"
                        f"24h: {scalp_pnl['trades']}건 ${scalp_pnl['pnl']:+.2f}"
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
                    engine_status = self.engine.get_status() if self.engine else {}
                    _append_jsonl({
                        "type": "hourly_snapshot",
                        "balance": round(bal, 2),
                        "scalp_active": engine_status.get("active", False),
                        "scalp_trades": engine_status.get("total_trades", 0),
                        "scalp_pnl": engine_status.get("total_pnl", 0),
                        "win_rate": engine_status.get("win_rate", 0),
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

                if action == "close_all" and self.engine:
                    await self.engine.stop()
                    logger.info("[CMD] 엔진 정지")
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

        logger.info("봇 시작 — ScalpBot")
        await self.redis.set("sys:bot_status", "running")
        await self.redis.set("sys:autotrading", "on")

        self.telegram.redis = self.redis
        self.telegram.executor = self.executor
        self.telegram.risk_manager = self.risk_manager
        self.telegram.engine = self.engine

        await self.telegram.notify_bot_status("running")
        try:
            bal = await self.executor.get_balance()
            scalp_cfg = self.config.get("scalp", {})
            await self.telegram._send(
                f"\U0001f7e2 <b>ScalpBot v5 — Jay 단타법</b>\n"
                f"Balance: ${bal:,.2f} | Leverage: {scalp_cfg.get('leverage', 10)}x\n"
                f"TF: {scalp_cfg.get('timeframe', '1h')} | Size: {scalp_cfg.get('size_btc', 0.01)} BTC\n"
                f"MACD({scalp_cfg.get('macd_fast', 8)},{scalp_cfg.get('macd_slow', 26)},{scalp_cfg.get('macd_signal', 9)})"
            )
        except Exception:
            pass

        tasks = [
            # 데이터
            asyncio.create_task(self.ws_stream.start()),
            asyncio.create_task(self.order_stream.start()),
            asyncio.create_task(self.periodic_candle_update()),
            # 전략
            asyncio.create_task(self.engine.run()),
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
        if self.engine:
            await self.engine.stop()
        await self.candle_collector.close()
        await self.executor.close()
        await self.redis.close()
        await self.db.close()


# ── 엔트리포인트 ──

async def main():
    engine = ScalpBot()

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
