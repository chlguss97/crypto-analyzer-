import asyncio
import logging
from datetime import datetime, timezone
from telegram import Bot
from telegram.constants import ParseMode
from src.utils.helpers import get_env, load_config

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """텔레그램 알림 발송 + 명령어 수신"""

    def __init__(self):
        self.config = load_config()
        self.enabled = self.config.get("telegram", {}).get("enabled", False)
        self.bot: Bot | None = None
        self.chat_id: str = ""
        self._last_update_id = 0
        # main.py 에서 주입 — 명령어 처리용
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

    # ── 명령어 polling (5초마다) ──

    async def poll_commands(self):
        """
        텔레그램 명령어 수신 (getUpdates polling).
        main.py 에서 asyncio.create_task 로 실행.
        보안: chat_id 일치하는 사용자만 처리.
        """
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
                    # 보안: chat_id 일치 확인
                    if str(update.message.chat.id) != str(self.chat_id):
                        continue
                    cmd = update.message.text.strip().lower()
                    await self._handle_command(cmd)
            except Exception as e:
                logger.debug(f"텔레그램 명령 polling 에러: {e}")
            await asyncio.sleep(5)

    async def _handle_command(self, cmd: str):
        """명령어 처리"""
        if cmd == "/on":
            if self.redis:
                await self.redis.set("sys:autotrading", "on")
            await self._send("\U0001f7e2 <b>자동매매 ON</b>\n⚠️ 실거래 활성화됨 — 페이퍼 모드 해제")
            logger.info("[TG-CMD] /on → 자동매매 ON (⚠️ 실거래 활성화)")

        elif cmd == "/off":
            if self.redis:
                await self.redis.set("sys:autotrading", "off")
            await self._send("\U0001f534 <b>자동매매 OFF</b>")
            logger.info("[TG-CMD] /off → 자동매매 OFF")

        elif cmd == "/status":
            await self._cmd_status()

        elif cmd == "/balance":
            await self._cmd_balance()

        elif cmd == "/close":
            await self._cmd_close()

        elif cmd == "/clear":
            await self._cmd_clear()

        elif cmd == "/market":
            await self._cmd_market()

        elif cmd == "/stats":
            await self._cmd_stats()

        elif cmd == "/trades":
            await self._cmd_trades()

        elif cmd == "/risk":
            await self._cmd_risk()

        elif cmd == "/help":
            await self._send(
                "<b>FlowEngine v1 Commands</b>\n\n"
                "\U0001f7e2 /on — Autotrading ON\n"
                "\U0001f534 /off — Autotrading OFF\n"
                "\U0001f4ca /status — Bot Status\n"
                "\U0001f4b0 /balance — Check Balance\n"
                "\U0001f310 /market — Market Overview\n"
                "\U0001f4c8 /stats — Today's Stats\n"
                "\U0001f4cb /trades — Recent 5 Trades\n"
                "\U0001f6e1 /risk — Risk Status\n"
                "\U0001f6d1 /close — Close All Positions\n"
                "\U0001f9f9 /clear — Force Clear Zombie\n"
                "\u2753 /help — Command List"
            )

        elif cmd == "/\uba70\ub2c8":
            await self._send("\U0001f436 \uc608\ubed0\uc694!! \uba4d\uba4d! \U0001f43e")

    async def _cmd_status(self):
        """봇 상태 조회 (실전 + 페이퍼)"""
        try:
            autotrading = "ON" if self.redis and (await self.redis.get("sys:autotrading")) == "on" else "OFF"
            regime = (await self.redis.get("sys:regime")) if self.redis else "?"
            balance = (await self.redis.get("sys:balance")) if self.redis else "?"
            positions = len(self.position_manager.positions) if self.position_manager else 0
            learning = "YES" if self.redis and (await self.redis.get("sys:learning")) == "1" else "NO"

            text = (
                "\U0001f4ca <b>봇 상태</b>\n\n"
                f"자동매매: {autotrading}\n"
                f"잔고: ${balance}\n"
                f"활성 포지션: {positions}개\n"
                f"레짐: {regime}\n"
                f"학습 중: {learning}"
            )

            # 페이퍼 계좌 상태
            if self.redis:
                paper = await self.redis.get_json("paper:state")
                if paper and isinstance(paper, dict):
                    p_bal = paper.get("balance", 0)
                    p_ret = paper.get("total_return_pct", 0)
                    p_dd = paper.get("drawdown_pct", 0)
                    p_trades = paper.get("total_trades", 0)
                    p_wr = paper.get("win_rate", 0)
                    p_daily = paper.get("daily_pnl_pct", 0)
                    p_pos = paper.get("active_positions", 0)
                    p_streak = paper.get("loss_streak", 0)

                    text += (
                        f"\n\n\U0001f4dd <b>페이퍼 계좌</b>\n"
                        f"잔고: ${p_bal:,.0f} ({p_ret:+.1f}%)\n"
                        f"DD: {p_dd:.1f}% | 일일: {p_daily:+.1f}%\n"
                        f"매매: {p_trades}건 | 승률: {p_wr:.0f}%\n"
                        f"포지션: {p_pos}개 | 연패: {p_streak}"
                    )

            await self._send(text)
        except Exception as e:
            await self._send(f"\u26a0\ufe0f 상태 조회 실패: {e}")

    async def _cmd_balance(self):
        """잔고 조회"""
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
        """전 포지션 청산"""
        if not self.position_manager:
            await self._send("\u26a0\ufe0f position_manager 미주입")
            return
        positions = self.position_manager.positions
        if not positions:
            await self._send("\u2705 활성 포지션 없음")
            return
        count = len(positions)
        await self._send(f"\U0001f6d1 <b>전 포지션 청산 시작 ({count}개)</b>")
        try:
            await self.position_manager.close_all("telegram_cmd")
            await self._send(f"\u2705 <b>{count}개 포지션 청산 완료</b>")
        except Exception as e:
            await self._send(f"\u26a0\ufe0f 청산 에러: {e}")

    async def _cmd_clear(self):
        """
        좀비 포지션 강제 정리 — 거래소 close 안 함, 봇 메모리/Redis만 정리.
        어제 같은 "포지션 없는데 close 무한루프" 사고 시 사용.
        """
        if not self.position_manager:
            await self._send("\u26a0\ufe0f position_manager 미주입")
            return

        positions = dict(self.position_manager.positions)
        if not positions:
            await self._send("\u2705 활성 포지션 없음 (정리할 것 없음)")
            return

        count = len(positions)
        cleared = []

        for symbol, pos in positions.items():
            try:
                # 알고 주문 cancel 시도 (실패해도 OK)
                try:
                    await self.position_manager._cancel_all_algos(pos)
                except Exception:
                    pass

                # 메모리 + Redis + DB 정리 (거래소 close 안 함)
                reason = "force_clear_telegram"
                await self.position_manager._finalize_position(pos, reason, exit_price=0)
                cleared.append(f"{symbol} {pos.direction} {pos.size:.4f} BTC")

                logger.warning(
                    f"[TG-CMD] /clear 강제 정리: {symbol} {pos.direction} "
                    f"{pos.size:.4f} BTC @ ${pos.entry_price:.0f} → "
                    f"메모리/Redis/DB 정리 (거래소 close X)"
                )
            except Exception as e:
                cleared.append(f"{symbol} 정리 실패: {e}")
                # 최소한 메모리에서 삭제
                if symbol in self.position_manager.positions:
                    del self.position_manager.positions[symbol]

        # Redis stale 키도 정리
        if self.redis:
            try:
                stale_keys = await self.redis.keys("pos:active:*")
                for key in stale_keys:
                    key_str = key.decode() if isinstance(key, bytes) else key
                    await self.redis.delete(key_str)
            except Exception:
                pass

        result = "\n".join(cleared)
        await self._send(
            f"\U0001f9f9 <b>/clear 강제 정리 완료 ({count}건)</b>\n\n"
            f"{result}\n\n"
            f"거래소 close 안 함 (이미 없는 포지션용)\n"
            f"OKX 직접 확인 권장"
        )
        logger.info(f"[TG-CMD] /clear 완료: {count}건 강제 정리")

    # ── 새 명령어 (04-15) ──

    async def _cmd_market(self):
        """시장 상태 한눈에"""
        try:
            price_str = await self.redis.get("rt:price:BTC-USDT-SWAP") if self.redis else None
            price = float(price_str) if price_str else 0

            # FlowEngine 상태
            state = await self.redis.get_json("sys:trade_state") if self.redis else None

            # 거래량
            vel = await self.redis.hgetall("rt:velocity:BTC-USDT-SWAP") if self.redis else {}
            range_60s = float(vel.get("range_60s", 0)) if vel else 0
            move_60s = float(vel.get("move_60s", 0)) if vel else 0

            trend = state.get("trend", "?") if state else "?"
            direction = state.get("direction", "?") if state else "?"
            score = state.get("score", 0) if state else 0

            trend_icon = "\U0001f7e2" if trend == "up" else "\U0001f534" if trend == "down" else "\u26aa"

            text = (
                f"\U0001f310 <b>Market Overview</b>\n\n"
                f"BTC: <b>${price:,.1f}</b>\n"
                f"Trend: {trend_icon} {trend.upper()}\n"
                f"Flow: {direction.upper()} (score {score:.1f})\n"
                f"\n"
                f"60s Range: ${range_60s:,.0f}\n"
                f"60s Move: ${move_60s:+,.0f}"
            )
            await self._send(text)
        except Exception as e:
            await self._send(f"\u26a0\ufe0f Market 조회 실패: {e}")

    async def _cmd_stats(self):
        """오늘 매매 통계"""
        try:
            import json
            trades_file = "/app/data/logs/trades.jsonl"
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            real_today = []
            paper_today = []
            try:
                with open(trades_file) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        d = json.loads(line)
                        if d.get("ts_iso", "")[:10] != today_str:
                            continue
                        if d.get("type") == "exit":
                            real_today.append(d)
                        elif d.get("type") == "paper_exit":
                            paper_today.append(d)
            except FileNotFoundError:
                pass

            # Real
            r_w = sum(1 for t in real_today if t["pnl_usdt"] > 0)
            r_l = sum(1 for t in real_today if t["pnl_usdt"] < 0)
            r_pnl = sum(t["pnl_usdt"] for t in real_today)
            r_fee = sum(t.get("fee", 0) for t in real_today)

            # Paper
            p_w = sum(1 for t in paper_today if t["pnl_usdt"] > 0)
            p_l = sum(1 for t in paper_today if t["pnl_usdt"] < 0)
            p_pnl = sum(t["pnl_usdt"] for t in paper_today)

            r_wr = r_w / (r_w + r_l) * 100 if r_w + r_l > 0 else 0
            p_wr = p_w / (p_w + p_l) * 100 if p_w + p_l > 0 else 0

            text = (
                f"\U0001f4c8 <b>Today's Stats ({today_str})</b>\n\n"
                f"<b>Real:</b> {len(real_today)} trades\n"
                f"  {r_w}W / {r_l}L ({r_wr:.0f}%)\n"
                f"  PnL: {r_pnl:+.2f} USDT\n"
                f"  Fee: {r_fee:.2f} USDT\n"
                f"  Net: {r_pnl - r_fee:+.2f} USDT\n\n"
                f"<b>Paper:</b> {len(paper_today)} trades\n"
                f"  {p_w}W / {p_l}L ({p_wr:.0f}%)\n"
                f"  PnL: {p_pnl:+.2f}"
            )
            await self._send(text)
        except Exception as e:
            await self._send(f"\u26a0\ufe0f Stats 조회 실패: {e}")

    async def _cmd_trades(self):
        """최근 5건 매매 내역"""
        try:
            import json
            trades_file = "/app/data/logs/trades.jsonl"
            recent = []
            try:
                with open(trades_file) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        d = json.loads(line)
                        if d.get("type") == "exit":
                            recent.append(d)
                recent = recent[-5:]
            except FileNotFoundError:
                pass

            if not recent:
                await self._send("\U0001f4cb 매매 내역 없음")
                return

            lines = ["\U0001f4cb <b>Recent 5 Trades</b>\n"]
            for t in recent:
                icon = "\U0001f7e2" if t["pnl_usdt"] > 0 else "\U0001f534"
                ts = t.get("ts_iso", "?")[:16]
                dr = t["direction"][:1].upper()
                reason = t["exit_reason"][:15]
                pnl = t["pnl_usdt"]
                hold = t.get("hold_min", 0)
                lines.append(f"{icon} {ts} {dr} {reason} {pnl:+.2f} ({hold}m)")

            await self._send("\n".join(lines))
        except Exception as e:
            await self._send(f"\u26a0\ufe0f Trades 조회 실패: {e}")

    async def _cmd_risk(self):
        """리스크 상태"""
        try:
            state = await self.redis.get_json("sys:trade_state") if self.redis else {}
            streak = state.get("streak", 0) if state else 0

            balance = 0
            if self.executor:
                balance = await self.executor.get_balance()

            positions = len(self.position_manager.positions) if self.position_manager else 0

            # 사이즈 축소 계산
            if streak >= 10:
                size_pct = "25%"
            elif streak >= 5:
                size_pct = "50%"
            elif streak >= 3:
                size_pct = "70%"
            else:
                size_pct = "100%"

            text = (
                f"\U0001f6e1 <b>Risk Status</b>\n\n"
                f"Balance: ${balance:,.2f}\n"
                f"Open Positions: {positions}\n"
                f"Loss Streak: {streak}\n"
                f"Position Size: {size_pct}\n"
                f"Margin Mode: 30% of balance"
            )
            await self._send(text)
        except Exception as e:
            await self._send(f"\u26a0\ufe0f Risk 조회 실패: {e}")

    async def notify_setup_detected(self, setup: str, direction: str, score: float,
                                     price: float, reason: str):
        """셋업 감지 알림 (진입 전)"""
        icon = "\U0001f4c8" if direction == "long" else "\U0001f4c9"
        text = (
            f"\U0001f50d <b>Flow Signal</b>\n\n"
            f"{icon} {direction.upper()} @ ${price:,.1f}\n"
            f"Score: {score:.1f}/10\n"
            f"{reason}"
        )
        await self._send(text)

    # ── 진입 알림 ──

    async def notify_entry(self, direction: str, grade: str, score: float,
                           entry_price: float, sl_price: float,
                           tp1_price: float, tp2_price: float,
                           leverage: int, margin: float,
                           tp3_price: float | None = None):
        icon = "\U0001f4c8" if direction == "long" else "\U0001f4c9"
        tp_line = f"TP1: ${tp1_price:,.1f} | TP2: ${tp2_price:,.1f}"
        if tp3_price:
            tp_line += f" | TP3: ${tp3_price:,.1f}"
        text = (
            f"{icon} <b>Entry | {grade} {direction.upper()}</b>\n"
            f"\n"
            f"점수: {score:.1f}/10\n"
            f"진입가: ${entry_price:,.1f}\n"
            f"SL: ${sl_price:,.1f}\n"
            f"{tp_line}\n"
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
                          hold_minutes: int,
                          fee: float = 0.0, funding: float = 0.0):
        net_pnl = pnl_usdt - fee - funding
        icon = "\U0001f4b5" if net_pnl > 0 else "\u2716\ufe0f"
        text = (
            f"{icon} <b>Exit | {direction.upper()} | {exit_reason}</b>\n"
            f"\n"
            f"진입: ${entry_price:,.1f} -> 청산: ${exit_price:,.1f}\n"
            f"수익률: {pnl_pct:+.2f}%\n"
            f"손익: ${pnl_usdt:+,.2f}\n"
            f"수수료: ${fee:,.4f}\n"
            f"펀딩비: ${funding:,.4f}\n"
            f"<b>순손익: ${net_pnl:+,.2f}</b>\n"
            f"보유: {hold_minutes}분\n"
            f"\n"
            f"{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
        )
        await self._send(text)

    # ── 경고 알림 ──

    async def notify_warning(self, message: str):
        text = f"\u26a0\ufe0f <b>Warning</b>\n\n{message}"
        await self._send(text)

    async def notify_cooldown(self, streak: int, cooldown_min: int):
        text = (
            f"\u23f8\ufe0f <b>Cooldown</b>\n"
            f"\n"
            f"연패: {streak}회\n"
            f"쿨다운: {cooldown_min}분\n"
        )
        await self._send(text)

    # ── 긴급 알림 ──

    async def notify_emergency(self, message: str):
        text = f"\U0001f198 <b>EMERGENCY</b>\n\n{message}"
        await self._send(text)

    async def notify_bot_status(self, status: str):
        icons = {"running": "\u26a1", "paused": "\u23f8\ufe0f", "stopped": "\u26d4"}
        icon = icons.get(status, "\u2753")
        text = f"{icon} <b>Bot: {status.upper()}</b>"
        await self._send(text)

    # ── TP1 hit + 본절 이동 알림 (러너 모드 진입 순간) ──

    async def notify_tp1_hit(self, direction: str, tp1_price: float,
                             new_sl: float, runner_active: bool,
                             trail_distance: float = 0):
        runner_line = ""
        if runner_active:
            runner_line = f"\nRunner ON (trail ${trail_distance:.1f})"
        text = (
            f"\U0001f3af <b>TP1 Hit | {direction.upper()}</b>\n"
            f"\n"
            f"50% closed @ ${tp1_price:,.1f}\n"
            f"SL \u2192 BE ${new_sl:,.1f}{runner_line}\n"
            f"\n"
            f"Remaining 50% trailing"
        )
        await self._send(text)

    # ── 레짐 변경 알림 ──

    async def notify_regime_change(self, old_regime: str, new_regime: str,
                                   confidence: float = 0):
        icons = {
            "trending_up": "\U0001f4c8",
            "trending_down": "\U0001f4c9",
            "ranging": "\u2194\ufe0f",
            "volatile": "\U0001f30a",
        }
        icon = icons.get(new_regime, "\U0001f4ca")
        text = (
            f"{icon} <b>Regime Change</b>\n"
            f"\n"
            f"{old_regime} \u2192 {new_regime}\n"
            f"Confidence: {confidence*100:.0f}%"
        )
        await self._send(text)

    # ── 학습 알림 ──

    async def notify_study_start(self, label: str):
        text = (
            f"\U0001f4d6 <b>학습 시작 | {label}</b>\n"
            f"\n"
            f"신규 진입 일시 정지\n"
            f"{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
        )
        await self._send(text)

    async def notify_study_done(self, label: str, result,
                                elapsed_sec: float = 0, **kwargs):
        learned = result if isinstance(result, (int, float)) else 0
        m, s = divmod(int(elapsed_sec), 60)
        text = (
            f"\u2705 <b>학습 완료 | {label}</b>\n\n"
            f"학습 건수: {learned:,}건\n"
            f"소요 시간: {m}분 {s}초\n\n"
            f"매매 재개\n"
            f"{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
        )
        await self._send(text)

    # ── 일일 리포트 ──

    async def notify_daily_report(self, date: str, total_trades: int,
                                  wins: int, losses: int,
                                  total_pnl: float, balance: float):
        win_rate = wins / total_trades * 100 if total_trades > 0 else 0
        icon = "\U0001f4c8" if total_pnl >= 0 else "\U0001f4c9"
        text = (
            f"\U0001f4ca <b>Daily Report | {date}</b>\n"
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
