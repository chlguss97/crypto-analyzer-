import asyncio
import logging
from datetime import datetime, timezone
from telegram import Bot
from telegram.constants import ParseMode
from src.utils.helpers import get_env, load_config

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """텔레그램 알림 발송"""

    def __init__(self):
        self.config = load_config()
        self.enabled = self.config.get("telegram", {}).get("enabled", False)
        self.bot: Bot | None = None
        self.chat_id: str = ""

    async def initialize(self):
        if not self.enabled:
            logger.info("텔레그램 알림 비활성")
            return

        try:
            token = get_env("TELEGRAM_BOT_TOKEN", "")
            self.chat_id = get_env("TELEGRAM_CHAT_ID", "")
            if not token or not self.chat_id:
                logger.warning("텔레그램 토큰/채팅ID 미설정 → 비활성")
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
                chat_id=self.chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error(f"텔레그램 발송 실패: {e}")

    # ── 진입 알림 ──

    async def notify_entry(self, direction: str, grade: str, score: float,
                           entry_price: float, sl_price: float,
                           tp1_price: float, tp2_price: float,
                           leverage: int, margin: float):
        icon = "\U0001f7e2" if direction == "long" else "\U0001f534"
        text = (
            f"{icon} <b>진입 | {grade} {direction.upper()}</b>\n"
            f"\n"
            f"점수: {score:.1f}/10\n"
            f"진입가: ${entry_price:,.1f}\n"
            f"SL: ${sl_price:,.1f}\n"
            f"TP1: ${tp1_price:,.1f} | TP2: ${tp2_price:,.1f}\n"
            f"레버리지: {leverage}x\n"
            f"마진: ${margin:,.0f}\n"
            f"\n"
            f"{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
        )
        await self._send(text)

    # ── 청산 알림 ──

    async def notify_exit(self, direction: str, exit_reason: str,
                          entry_price: float, exit_price: float,
                          pnl_pct: float, pnl_usdt: float,
                          hold_minutes: int):
        icon = "\U0001f4b0" if pnl_usdt > 0 else "\U0001f4a5"
        text = (
            f"{icon} <b>청산 | {direction.upper()} | {exit_reason}</b>\n"
            f"\n"
            f"진입: ${entry_price:,.1f} -> 청산: ${exit_price:,.1f}\n"
            f"수익률: {pnl_pct:+.2f}%\n"
            f"손익: ${pnl_usdt:+,.2f}\n"
            f"보유: {hold_minutes}분\n"
            f"\n"
            f"{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
        )
        await self._send(text)

    # ── 경고 알림 ──

    async def notify_warning(self, message: str):
        text = f"\u26a0\ufe0f <b>경고</b>\n\n{message}"
        await self._send(text)

    async def notify_cooldown(self, streak: int, cooldown_min: int):
        text = (
            f"\u26a0\ufe0f <b>쿨다운 진입</b>\n"
            f"\n"
            f"연패: {streak}회\n"
            f"쿨다운: {cooldown_min}분\n"
        )
        await self._send(text)

    # ── 긴급 알림 ──

    async def notify_emergency(self, message: str):
        text = f"\U0001f6d1 <b>긴급</b>\n\n{message}"
        await self._send(text)

    async def notify_bot_status(self, status: str):
        icons = {"running": "\u2705", "paused": "\u23f8", "stopped": "\U0001f6d1"}
        icon = icons.get(status, "\u2753")
        text = f"{icon} <b>봇 상태: {status.upper()}</b>"
        await self._send(text)

    # ── 일일 리포트 ──

    async def notify_daily_report(self, date: str, total_trades: int,
                                  wins: int, losses: int,
                                  total_pnl: float, balance: float):
        win_rate = wins / total_trades * 100 if total_trades > 0 else 0
        icon = "\U0001f4c8" if total_pnl >= 0 else "\U0001f4c9"
        text = (
            f"\U0001f4ca <b>일일 리포트 | {date}</b>\n"
            f"\n"
            f"매매: {total_trades}회\n"
            f"승리: {wins} | 패배: {losses} ({win_rate:.0f}%)\n"
            f"손익: ${total_pnl:+,.2f}\n"
            f"{icon} 잔고: ${balance:,.2f}\n"
        )
        await self._send(text)

    # ── Grade A+ 시그널 알림 ──

    async def notify_signal(self, direction: str, grade: str, score: float):
        text = (
            f"\U0001f4c8 <b>시그널 감지 | {grade} {direction.upper()}</b>\n"
            f"점수: {score:.1f}/10"
        )
        await self._send(text)
