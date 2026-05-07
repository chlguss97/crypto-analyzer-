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
    """텔레그램 알림 + 명령어 — v2 (3경로 시스템 대응)"""

    def __init__(self):
        self.config = load_config()
        self.enabled = self.config.get("telegram", {}).get("enabled", False)
        self.bot: Bot | None = None
        self.chat_id: str = ""
        self._last_update_id = 0
        # main.py에서 주입
        self.redis = None
        self.executor = None
        self.position_manager = None
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
            "/adaptive": self._cmd_adaptive,
            "/lab": self._cmd_lab,
            "/shadow": self._cmd_shadow,
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
            autotrading = "ON" if self.redis and (await self.redis.get("sys:autotrading")) == "on" else "OFF"
            regime = (await self.redis.get("sys:regime")) if self.redis else "?"
            balance = (await self.redis.get("sys:balance")) if self.redis else "?"
            positions = len(self.position_manager.positions) if self.position_manager else 0

            # ML 상태
            ml_phase = "?"
            ml_labeled = 0
            if self.redis:
                ts = await self.redis.get_json("sys:trade_state")
                if ts:
                    ml_phase = ts.get("ml_phase", "?")
                    ml_labeled = ts.get("ml_labeled", 0)

            text = (
                "\U0001f4ca <b>봇 상태</b>\n\n"
                f"자동매매: {autotrading}\n"
                f"잔고: ${balance}\n"
                f"포지션: {positions}개\n"
                f"레짐: {regime}\n"
                f"ML: Phase {ml_phase} ({ml_labeled}건 labeled)"
            )

            # PaperLab 요약
            if self.redis:
                lab = await self.redis.get_json("lab:stats")
                if lab and isinstance(lab, dict):
                    total = lab.get("total_trades", 0)
                    best = lab.get("best")
                    best_str = f"{best['name']}(EV {best['ev']:+.1f})" if best else "데이터 부족"
                    text += f"\n\nLab: {total}건 | Best: {best_str}"

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
        if not self.position_manager:
            return await self._send("\u26a0\ufe0f position_manager 미주입")
        positions = self.position_manager.positions
        if not positions:
            return await self._send("\u2705 활성 포지션 없음")
        count = len(positions)
        await self._send(f"\U0001f6d1 <b>전 포지션 청산 시작 ({count}개)</b>")
        try:
            await self.position_manager.close_all("telegram_cmd")
            await self._send(f"\u2705 <b>{count}개 청산 완료</b>")
        except Exception as e:
            await self._send(f"\u26a0\ufe0f 청산 에러: {e}")

    async def _cmd_clear(self):
        if not self.position_manager:
            return await self._send("\u26a0\ufe0f position_manager 미주입")
        positions = dict(self.position_manager.positions)
        if not positions:
            return await self._send("\u2705 정리할 포지션 없음")

        count = len(positions)
        for symbol, pos in positions.items():
            try:
                try:
                    await self.position_manager._cancel_all_algos(pos)
                except Exception:
                    pass
                await self.position_manager._finalize_position(pos, "force_clear_telegram", exit_price=0)
            except Exception as e:
                logger.error(f"/clear 실패: {symbol}: {e}")

        # 잔여 Redis 키 정리
        if self.redis:
            try:
                keys = await self.redis._client.keys("pos:active:*")
                for k in (keys or []):
                    await self.redis._client.delete(k)
            except Exception:
                pass

        await self._send(f"\U0001f9f9 <b>/clear 완료 ({count}건)</b>\nOKX 직접 확인 권장")

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
                vel = await self.redis.get_json("rt:velocity:BTC-USDT-SWAP")
                if vel:
                    text += f"\n60s Range: ${vel.get('range_60s', 0):.0f}"
                    text += f"\n60s Move: ${vel.get('move_60s', 0):+.0f}"

            await self._send(text)
        except Exception as e:
            await self._send(f"\u26a0\ufe0f Market 조회 실패: {e}")

    async def _cmd_stats(self):
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            log_dir = Path(__file__).parent.parent.parent / "data" / "logs"

            real_trades = []
            lab_trades = []
            for f in sorted(log_dir.glob("trades_*.jsonl"), reverse=True)[:2]:
                for line in open(f, encoding="utf-8"):
                    try:
                        r = json.loads(line)
                        if r.get("ts_iso", "")[:10] != today:
                            continue
                        if r["type"] == "exit":
                            real_trades.append(r)
                        elif r["type"] == "lab_exit":
                            lab_trades.append(r)
                    except Exception:
                        continue

            # 실거래
            r_wins = sum(1 for t in real_trades if t.get("pnl_usdt", 0) > 0)
            r_pnl = sum(t.get("pnl_usdt", 0) for t in real_trades)
            r_total = len(real_trades)
            r_wr = r_wins / r_total * 100 if r_total > 0 else 0

            text = (
                f"\U0001f4c8 <b>Today ({today})</b>\n\n"
                f"<b>Real:</b> {r_total}건 | {r_wins}W ({r_wr:.0f}%)\n"
                f"PnL: ${r_pnl:+,.2f}\n"
            )

            # Lab
            if lab_trades:
                from collections import Counter
                variants = Counter(t.get("variant", "?") for t in lab_trades)
                text += f"\n<b>Lab:</b> {len(lab_trades)}건\n"
                for v, cnt in variants.most_common():
                    v_pnl = sum(t.get("pnl_pct", 0) for t in lab_trades if t.get("variant") == v)
                    text += f"  {v}: {cnt}건 avg {v_pnl/cnt:+.1f}%\n"

            await self._send(text)
        except Exception as e:
            await self._send(f"\u26a0\ufe0f Stats 조회 실패: {e}")

    async def _cmd_trades(self):
        try:
            log_dir = Path(__file__).parent.parent.parent / "data" / "logs"
            exits = []
            for f in sorted(log_dir.glob("trades_*.jsonl"), reverse=True)[:2]:
                for line in open(f, encoding="utf-8"):
                    try:
                        r = json.loads(line)
                        if r["type"] == "exit":
                            exits.append(r)
                    except Exception:
                        continue

            last5 = exits[-5:]
            if not last5:
                return await self._send("\U0001f4cb 최근 거래 없음")

            lines = ["\U0001f4cb <b>Recent Trades</b>\n"]
            for t in last5:
                icon = "\U0001f7e2" if t.get("pnl_usdt", 0) > 0 else "\U0001f534"
                ts = t.get("ts_iso", "")[:16]
                d = t["direction"][:1].upper()
                reason = t.get("exit_reason", "?")[:12]
                pnl = t.get("pnl_usdt", 0)
                hold = t.get("hold_min", 0)
                lines.append(f"{icon} {ts} {d} {reason} {pnl:+.1f}$ ({hold}m)")

            await self._send("\n".join(lines))
        except Exception as e:
            await self._send(f"\u26a0\ufe0f Trades 조회 실패: {e}")

    async def _cmd_risk(self):
        try:
            balance = await self.executor.get_balance() if self.executor else 0
            positions = len(self.position_manager.positions) if self.position_manager else 0

            # risk_manager에서 상태
            streak = 0
            daily_pnl = 0
            dd_pct = 0
            if self.risk_manager:
                streak = self.risk_manager.get_streak()
                daily_pnl = self.risk_manager.get_daily_pnl()
                state = self.risk_manager._state
                peak = state.get("peak_balance", balance)
                dd_pct = (peak - balance) / peak * 100 if peak > 0 else 0

            text = (
                f"\U0001f6e1 <b>Risk Status</b>\n\n"
                f"Balance: ${balance:,.2f}\n"
                f"Positions: {positions}\n"
                f"Streak: {streak}\n"
                f"Daily PnL: {daily_pnl:+.1f}%\n"
                f"Drawdown: {dd_pct:.1f}% (limit 12%)\n"
                f"Margin: {self.config.get('risk', {}).get('margin_pct', 0.40)*100:.0f}%"
            )
            await self._send(text)
        except Exception as e:
            await self._send(f"\u26a0\ufe0f Risk 조회 실패: {e}")

    async def _cmd_adaptive(self):
        """AdaptiveParams 보정 상태"""
        try:
            if not self.redis:
                return await self._send("\u26a0\ufe0f Redis 미연결")

            state_raw = await self.redis.get("adaptive:state")
            if not state_raw:
                return await self._send("\U0001f4ca <b>Adaptive</b>\n\n데이터 없음 (수집 중)")

            state = json.loads(state_raw)
            total = state.get("total_trades", 0)
            tp = state.get("tp_mult", 1.5)
            sl = state.get("sl_pct", 5.0)

            phase = "collect" if total < 10 else ("phase1" if total < 30 else ("phase2" if total < 300 else "full"))

            text = (
                f"\U0001f4ca <b>AdaptiveParams</b>\n\n"
                f"Phase: {phase} ({total}건)\n"
                f"TP mult: {tp:.3f} (기본 1.500)\n"
                f"SL pct: {sl:.1f}% (기본 5.0%)\n"
            )

            # Direction EV
            dir_results = state.get("direction_results", {})
            if dir_results:
                text += "\n<b>Direction EV:</b>\n"
                for key, vals in sorted(dir_results.items())[:6]:
                    if len(vals) >= 5:
                        ev = sum(vals[-20:]) / len(vals[-20:])
                        text += f"  {key}: {ev:+.1f}% ({len(vals)}건)\n"

            await self._send(text)
        except Exception as e:
            await self._send(f"\u26a0\ufe0f Adaptive 조회 실패: {e}")

    async def _cmd_lab(self):
        """PaperLab 3 Variant 비교"""
        try:
            if not self.redis:
                return await self._send("\u26a0\ufe0f Redis 미연결")

            lab_raw = await self.redis.get("lab:stats")
            if not lab_raw:
                return await self._send("\U0001f9ea <b>PaperLab</b>\n\n데이터 없음")

            lab = json.loads(lab_raw) if isinstance(lab_raw, str) else lab_raw
            variants = lab.get("variants", [])
            best = lab.get("best")

            text = "\U0001f9ea <b>PaperLab A/B Test</b>\n\n"
            for v in variants:
                marker = " \u2b50" if best and v["name"] == best.get("name") else ""
                text += (
                    f"<b>{v['name']}</b> (ATR\u00d7{v['atr_mult']}, SL{v['sl_pct']}%){marker}\n"
                    f"  {v['trades']}건 | WR {v['win_rate']}% | EV {v['ev']:+.1f}%\n"
                )

            if best:
                text += f"\n\U0001f3c6 Best: <b>{best['name']}</b> (EV {best['ev']:+.1f}%)"

            await self._send(text)
        except Exception as e:
            await self._send(f"\u26a0\ufe0f Lab 조회 실패: {e}")

    async def _cmd_shadow(self):
        """Shadow 라벨링 현황"""
        try:
            log_dir = Path(__file__).parent.parent.parent / "data" / "logs"
            shadows = []
            for f in sorted(log_dir.glob("trades_*.jsonl"), reverse=True)[:2]:
                for line in open(f, encoding="utf-8"):
                    try:
                        r = json.loads(line)
                        if r["type"] == "shadow_result":
                            shadows.append(r)
                    except Exception:
                        continue

            if not shadows:
                return await self._send("\U0001f441 <b>Shadow</b>\n\n결과 없음")

            wins = sum(1 for s in shadows if s.get("label") == 1)
            losses = sum(1 for s in shadows if s.get("label") == 0)
            total = wins + losses

            # reach% 평균 (있는 것만)
            reaches = [s.get("reach_pct", 0) for s in shadows if s.get("reach_pct") is not None and s.get("reach_pct") != 0]
            avg_reach = sum(reaches) / len(reaches) if reaches else 0

            text = (
                f"\U0001f441 <b>Shadow Tracking</b>\n\n"
                f"Total: {total}건 (W:{wins} L:{losses})\n"
                f"Win Rate: {wins/total*100:.0f}%\n" if total > 0 else ""
                f"Avg Reach: {avg_reach:.1f}%\n"
                f"With reach data: {len(reaches)}/{total}건"
            )
            await self._send(text)
        except Exception as e:
            await self._send(f"\u26a0\ufe0f Shadow 조회 실패: {e}")

    async def _cmd_help(self):
        await self._send(
            "<b>CryptoAnalyzer v2</b>\n\n"
            "\U0001f7e2 /on \u2014 Autotrading ON\n"
            "\U0001f534 /off \u2014 Autotrading OFF\n"
            "\U0001f4ca /status \u2014 Bot + ML + Lab\n"
            "\U0001f4b0 /balance \u2014 Balance\n"
            "\U0001f310 /market \u2014 Price/Regime\n"
            "\U0001f4c8 /stats \u2014 Today Stats\n"
            "\U0001f4cb /trades \u2014 Recent 5\n"
            "\U0001f6e1 /risk \u2014 Risk/DD/Streak\n"
            "\U0001f4ca /adaptive \u2014 TP/SL Tuning\n"
            "\U0001f9ea /lab \u2014 A/B Variants\n"
            "\U0001f441 /shadow \u2014 Label Stats\n"
            "\U0001f6d1 /close \u2014 Close All\n"
            "\U0001f9f9 /clear \u2014 Clear Zombie\n"
        )

    # ══════════════════════════════════════════
    #  알림 (notify_*)
    # ══════════════════════════════════════════

    async def notify_entry(self, direction: str, grade: str, score: float,
                           entry_price: float, sl_price: float,
                           tp1_price: float, tp2_price: float,
                           leverage: int, margin: float,
                           tp3_price: float | None = None,
                           conviction: int = 0, conviction_mult: float = 1.0):
        icon = "\U0001f4c8" if direction == "long" else "\U0001f4c9"
        tp_line = f"TP1: ${tp1_price:,.1f}"
        conv_str = f"\U0001f3af 확신도: {conviction}/5 ({conviction_mult:.0%})"
        text = (
            f"{icon} <b>Entry | {grade} {direction.upper()}</b>\n\n"
            f"{conv_str}\n"
            f"진입: ${entry_price:,.1f}\n"
            f"SL: ${sl_price:,.1f} | {tp_line}\n"
            f"{leverage}x | ${margin:,.0f}\n"
            f"{datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )
        await self._send(text)

    async def notify_exit(self, direction: str, exit_reason: str,
                          entry_price: float, exit_price: float,
                          pnl_pct: float, pnl_usdt: float,
                          hold_minutes: int,
                          fee: float = 0.0, funding: float = 0.0):
        icon = "\U0001f4b5" if pnl_usdt > 0 else "\u2716\ufe0f"
        text = (
            f"{icon} <b>Exit | {direction.upper()} | {exit_reason}</b>\n\n"
            f"${entry_price:,.1f} \u2192 ${exit_price:,.1f}\n"
            f"PnL: {pnl_pct:+.2f}% (${pnl_usdt:+,.2f})\n"
            f"Hold: {hold_minutes}min\n"
            f"{datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )
        await self._send(text)

    async def notify_tp1_hit(self, direction: str, tp1_price: float,
                             new_sl: float, runner_active: bool,
                             trail_distance: float = 0):
        text = (
            f"\U0001f3af <b>TP1 Hit | {direction.upper()}</b>\n\n"
            f"50% closed @ ${tp1_price:,.1f}\n"
            f"SL \u2192 BE ${new_sl:,.1f}\n"
        )
        if runner_active:
            text += f"Runner ON (trail ${trail_distance:.1f})"
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
