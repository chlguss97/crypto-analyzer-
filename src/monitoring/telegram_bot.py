import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from telegram import Bot
from telegram.constants import ParseMode
from src.utils.helpers import get_env, load_config

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """텔레그램 알림 + 명령어 — v3 (Grid-only)"""

    def __init__(self):
        self.config = load_config()
        self.enabled = self.config.get("telegram", {}).get("enabled", False)
        self.bot: Bot | None = None
        self.chat_id: str = ""
        self._last_update_id = 0
        # main.py에서 주입
        self.redis = None
        self.executor = None
        self.grid_engine = None  # GridEngine
        self.risk_manager = None

    async def initialize(self):
        if not self.enabled:
            logger.info("텔레그램 알림 비활성")
            return
        try:
            token = get_env("TELEGRAM_BOT_TOKEN", "")
            self.chat_id = get_env("TELEGRAM_CHAT_ID", "")
            if not token or not self.chat_id:
                logger.warning("텔레그램 토큰/채팅ID 미설정")
                self.enabled = False
                return
            self.bot = Bot(token=token)
            await self.bot.get_me()
            logger.info("텔레그램 봇 연결 완료")
        except Exception as e:
            logger.error(f"텔레그램 초기화 실패: {e}")
            self.enabled = False

    async def _send(self, text: str):
        if not self.enabled or not self.bot:
            return
        try:
            await self.bot.send_message(
                chat_id=self.chat_id, text=text,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error(f"텔레그램 발송 실패: {e}")

    # ══════════════════════════════════════════
    #  명령어 Polling
    # ══════════════════════════════════════════

    async def poll_commands(self):
        if not self.enabled or not self.bot:
            return
        logger.info("텔레그램 명령어 polling 시작")
        while True:
            try:
                updates = await self.bot.get_updates(
                    offset=self._last_update_id + 1, timeout=3
                )
                for update in updates:
                    self._last_update_id = update.update_id
                    if not update.message or not update.message.text:
                        continue
                    if str(update.message.chat.id) != str(self.chat_id):
                        continue
                    await self._handle_command(update.message.text.strip().lower())
            except Exception as e:
                logger.debug(f"텔레그램 polling 에러: {e}")
            await asyncio.sleep(5)

    async def _handle_command(self, cmd: str):
        handlers = {
            "/on": self._cmd_on,
            "/off": self._cmd_off,
            "/status": self._cmd_status,
            "/balance": self._cmd_balance,
            "/close": self._cmd_close,
            "/clear": self._cmd_clear,
            "/market": self._cmd_market,
            "/stats": self._cmd_stats,
            "/trades": self._cmd_trades,
            "/risk": self._cmd_risk,
            "/help": self._cmd_help,
        }
        handler = handlers.get(cmd)
        if handler:
            await handler()
        elif cmd == "/\uba38\ub2c8":
            await self._send("\U0001f436 \uc608\ubed0\uc694!! \uba4d\uba4d! \U0001f43e")

    # ══════════════════════════════════════════
    #  명령어 핸들러
    # ══════════════════════════════════════════

    async def _cmd_on(self):
        if self.redis:
            await self.redis.set("sys:autotrading", "on")
        await self._send("\U0001f7e2 <b>자동매매 ON</b>\n\u26a0\ufe0f 실거래 활성화")

    async def _cmd_off(self):
        if self.redis:
            await self.redis.set("sys:autotrading", "off")
        await self._send("\U0001f534 <b>자동매매 OFF</b>")

    async def _cmd_status(self):
        try:
            balance = (await self.redis.get("sys:balance")) if self.redis else "?"
            mode = (await self.redis.get("regime:mode")) if self.redis else "ACTIVE"
            crs = (await self.redis.get("regime:crs")) if self.redis else "0"

            grid_info = ""
            if self.grid_engine:
                gs = self.grid_engine.get_status()
                grid_info = (
                    f"Grid: {'ACTIVE' if gs.get('active') else 'PAUSED'}"
                    f" | center: ${gs.get('center', 0):.0f}"
                    f" | spacing: {gs.get('spacing_pct', 0):.2f}%"
                    f"\n사이클: {gs.get('total_cycles', 0)}회"
                    f" | PnL: ${gs.get('total_pnl', 0):+.2f}"
                )
                if gs.get("pause_reason"):
                    grid_info += f"\n⚠️ 일시정지: {gs['pause_reason']}"

            text = (
                "\U0001f4ca <b>GridBot 상태</b>\n\n"
                f"잔고: ${balance}\n"
                f"Mode: {mode or 'ACTIVE'} | CRS: {crs or '0'}\n"
                f"{grid_info}"
            )
            await self._send(text)
        except Exception as e:
            await self._send(f"\u26a0\ufe0f 상태 조회 실패: {e}")

    async def _cmd_balance(self):
        try:
            if self.executor:
                bal = await self.executor.get_balance()
                await self._send(f"\U0001f4b0 <b>잔고: ${bal:,.2f}</b>")
            else:
                cached = (await self.redis.get("sys:balance")) if self.redis else "?"
                await self._send(f"\U0001f4b0 <b>잔고: ${cached}</b> (캐시)")
        except Exception as e:
            await self._send(f"\u26a0\ufe0f 잔고 조회 실패: {e}")

    async def _cmd_close(self):
        if not self.grid_engine:
            return await self._send("\u26a0\ufe0f GridEngine 미초기화")
        try:
            await self.grid_engine.stop()
            await self._send("\u2705 <b>그리드 정지 + 전 주문 취소 완료</b>")
        except Exception as e:
            await self._send(f"\u26a0\ufe0f 정지 에러: {e}")

    async def _cmd_clear(self):
        if not self.grid_engine:
            return await self._send("\u26a0\ufe0f GridEngine 미초기화")
        try:
            await self.grid_engine.stop()
            if self.executor:
                await self.executor.cancel_all_orders()
                await self.executor.cancel_all_algos()
            await self._send("\U0001f9f9 <b>/clear 완료</b> — 전 주문 취소")
        except Exception as e:
            await self._send(f"\u26a0\ufe0f clear 에러: {e}")

    async def _cmd_market(self):
        try:
            price = (await self.redis.get("rt:price:BTC-USDT-SWAP")) if self.redis else "?"
            regime = (await self.redis.get("sys:regime")) if self.redis else "?"

            icons = {"trending_up": "\U0001f4c8", "trending_down": "\U0001f4c9",
                     "ranging": "\u2194\ufe0f", "volatile": "\U0001f30a"}
            icon = icons.get(regime, "\U0001f4ca")

            text = f"\U0001f310 <b>Market</b>\n\nBTC: ${float(price):,.1f}\nRegime: {icon} {regime}"

            # 속도
            if self.redis:
                vel = await self.redis.hgetall("rt:velocity:BTC-USDT-SWAP")
                if vel:
                    text += f"\n60s Range: ${float(vel.get('range_60s', 0)):.0f}"
                    text += f"\n60s Move: ${float(vel.get('move_60s', 0)):+.0f}"

            await self._send(text)
        except Exception as e:
            await self._send(f"\u26a0\ufe0f Market 조회 실패: {e}")

    async def _cmd_stats(self):
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            log_dir = Path(__file__).parent.parent.parent / "data" / "logs"

            grid_cycles = []
            for f in sorted(log_dir.glob("trades_*.jsonl"), reverse=True)[:2]:
                for line in open(f, encoding="utf-8"):
                    try:
                        r = json.loads(line)
                        if r.get("ts_iso", "")[:10] != today:
                            continue
                        if r["type"] == "grid_cycle":
                            grid_cycles.append(r)
                    except Exception:
                        continue

            total = len(grid_cycles)
            total_pnl = sum(t.get("pnl", 0) for t in grid_cycles)
            avg_pnl = total_pnl / total if total > 0 else 0
            wins = sum(1 for t in grid_cycles if t.get("pnl", 0) > 0)

            text = (
                f"\U0001f4c8 <b>Grid Stats ({today})</b>\n\n"
                f"사이클: {total}건 | {wins}W\n"
                f"Total PnL: ${total_pnl:+,.2f}\n"
                f"Avg PnL: ${avg_pnl:+,.2f}\n"
            )

            await self._send(text)
        except Exception as e:
            await self._send(f"\u26a0\ufe0f Stats 조회 실패: {e}")

    async def _cmd_trades(self):
        try:
            log_dir = Path(__file__).parent.parent.parent / "data" / "logs"
            cycles = []
            for f in sorted(log_dir.glob("trades_*.jsonl"), reverse=True)[:2]:
                for line in open(f, encoding="utf-8"):
                    try:
                        r = json.loads(line)
                        if r["type"] == "grid_cycle":
                            cycles.append(r)
                    except Exception:
                        continue

            last10 = cycles[-10:]
            if not last10:
                return await self._send("\U0001f4cb 최근 그리드 사이클 없음")

            lines = ["\U0001f4cb <b>Recent Grid Cycles</b>\n"]
            for t in last10:
                icon = "\U0001f7e2" if t.get("pnl", 0) > 0 else "\U0001f534"
                ts = t.get("ts_iso", "")[:16]
                lvl = t.get("level", "?")
                entry = t.get("entry", 0)
                exit_p = t.get("exit", 0)
                pnl = t.get("pnl", 0)
                lines.append(f"{icon} {ts} L{lvl} ${entry:.0f}->${exit_p:.0f} {pnl:+.2f}$")

            await self._send("\n".join(lines))
        except Exception as e:
            await self._send(f"\u26a0\ufe0f Trades 조회 실패: {e}")

    async def _cmd_risk(self):
        try:
            balance = await self.executor.get_balance() if self.executor else 0

            # GridEngine 상태
            grid_active = False
            grid_cycles = 0
            grid_pnl = 0.0
            if self.grid_engine:
                gs = self.grid_engine.get_status()
                grid_active = gs.get("active", False)
                grid_cycles = gs.get("total_cycles", 0)
                grid_pnl = gs.get("total_pnl", 0)

            # Drawdown
            dd_pct = 0
            if self.risk_manager:
                state = self.risk_manager._state
                peak = state.get("peak_balance", balance)
                dd_pct = (peak - balance) / peak * 100 if peak > 0 else 0

            text = (
                f"\U0001f6e1 <b>Risk Status</b>\n\n"
                f"Balance: ${balance:,.2f}\n"
                f"Grid: {'ACTIVE' if grid_active else 'PAUSED'}\n"
                f"Cycles: {grid_cycles} | PnL: ${grid_pnl:+.2f}\n"
                f"Drawdown: {dd_pct:.1f}% (kill 20%)\n"
            )
            await self._send(text)
        except Exception as e:
            await self._send(f"\u26a0\ufe0f Risk 조회 실패: {e}")


    async def _cmd_help(self):
        await self._send(
            "<b>GridBot v3</b>\n\n"
            "\U0001f7e2 /on \u2014 Autotrading ON\n"
            "\U0001f534 /off \u2014 Autotrading OFF\n"
            "\U0001f4ca /status \u2014 Grid + Regime 상태\n"
            "\U0001f4b0 /balance \u2014 Balance\n"
            "\U0001f310 /market \u2014 Price/Regime\n"
            "\U0001f4c8 /stats \u2014 Today Grid Stats\n"
            "\U0001f4cb /trades \u2014 Recent 10 Cycles\n"
            "\U0001f6e1 /risk \u2014 Risk/DD\n"
            "\U0001f6d1 /close \u2014 Grid Stop\n"
            "\U0001f9f9 /clear \u2014 Clear All Orders\n"
        )

    # ══════════════════════════════════════════
    #  알림 (notify_*)
    # ══════════════════════════════════════════

    async def notify_grid_cycle(self, level: int, entry: float,
                               exit_price: float, pnl: float,
                               total_cycles: int, total_pnl: float):
        icon = "\U0001f7e2" if pnl > 0 else "\U0001f534"
        text = (
            f"{icon} <b>Grid Cycle L{level}</b>\n\n"
            f"${entry:,.1f} -> ${exit_price:,.1f}\n"
            f"PnL: ${pnl:+.2f}\n"
            f"Total: {total_cycles}회 | ${total_pnl:+.2f}\n"
            f"{datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )
        await self._send(text)

    async def notify_warning(self, message: str):
        await self._send(f"\u26a0\ufe0f <b>Warning</b>\n\n{message}")

    async def notify_emergency(self, message: str):
        await self._send(f"\U0001f198 <b>EMERGENCY</b>\n\n{message}")

    async def notify_bot_status(self, status: str):
        icons = {"running": "\u26a1", "paused": "\u23f8\ufe0f", "stopped": "\u26d4"}
        await self._send(f"{icons.get(status, '?')} <b>Bot: {status.upper()}</b>")

    async def notify_regime_change(self, old_regime: str, new_regime: str,
                                   confidence: float = 0):
        icons = {"trending_up": "\U0001f4c8", "trending_down": "\U0001f4c9",
                 "ranging": "\u2194\ufe0f", "volatile": "\U0001f30a"}
        icon = icons.get(new_regime, "\U0001f4ca")
        await self._send(
            f"{icon} <b>Regime: {old_regime} \u2192 {new_regime}</b>\n"
            f"Confidence: {confidence*100:.0f}%"
        )
