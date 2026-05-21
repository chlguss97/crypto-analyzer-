"""
ScalpManager — 스캘핑 포지션 라이프사이클 관리

간소화 설계:
  - 러너 없음, 부분청산 없음, TP2/TP3 없음
  - TP 0.20% (서버 limit-on-trigger) + SL 0.15% (서버 market-on-trigger)
  - 시간 정지: 3분(수익시) / 5분(최대)
  - SL self-heal: 5초 간격 OKX 검증

executor.py의 OrderExecutor를 재사용하여 OKX 주문 실행.
"""

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field

from src.trading.executor import OrderExecutor
from src.data.storage import RedisClient, Database
from src.monitoring.trade_logger import _append_jsonl

logger = logging.getLogger(__name__)

MIN_ORDER_SIZE_BTC = 0.01


@dataclass
class ScalpPosition:
    trade_id: int
    signal_id: int
    direction: str
    entry_price: float
    size: float
    leverage: int
    sl_price: float
    tp_price: float
    entry_time: float  # unix timestamp
    algo_ids: dict = field(default_factory=lambda: {"sl": None, "tp": None})
    # tracking
    best_price: float = 0.0
    worst_price: float = 0.0
    total_fee: float = 0.0
    close_attempts: int = 0
    _sl_lost_count: int = 0
    _last_sl_verify: float = 0.0


class ScalpManager:
    """스캘핑 포지션 관리 — 진입/청산/시간정지/SL보호"""

    def __init__(self, executor: OrderExecutor, db: Database,
                 redis: RedisClient, telegram=None, config: dict = None):
        self.executor = executor
        self.db = db
        self.redis = redis
        self.telegram = telegram
        cfg = (config or {}).get("scalp", {})

        self.tp_pct = cfg.get("tp_price_pct", 0.20) / 100  # fallback
        self.sl_pct = cfg.get("sl_price_pct", 0.15) / 100  # fallback
        self.sl_k = cfg.get("sl_k_vol", 2.0)  # 동적 SL: k × Parkinson Vol
        self.tp_k = cfg.get("tp_k_vol", 2.0)  # 동적 TP: k × Parkinson Vol (SL과 대칭)
        self.time_stop_sec = cfg.get("time_stop_sec", 180)
        self.time_stop_max_sec = cfg.get("time_stop_max_sec", 300)
        self.time_stop_loss_margin_pct = cfg.get("time_stop_loss_margin_pct", 1.5)
        self.leverage = cfg.get("leverage", 20)
        self.margin_pct = cfg.get("margin_pct", 0.80)

        self.symbol = (config or {}).get("exchange", {}).get("symbol", "BTC/USDT:USDT")
        self.position: ScalpPosition | None = None

    def has_position(self) -> bool:
        return self.position is not None

    # ══════════════════════════════════════════
    #  진입
    # ══════════════════════════════════════════

    async def open_scalp(self, signal: dict, balance: float,
                         streak_mult: float = 1.0) -> ScalpPosition | None:
        """시그널 → OKX 주문 → SL/TP 등록 → ScalpPosition 반환"""
        if self.position:
            return None

        direction = signal["direction"]
        price = signal["price"]

        # VPIN/Hurst 기반 사이즈 배수
        combined_size_mult = signal.get("combined_size_mult", 1.0)

        # 사이즈 계산 (VPIN + Hurst + streak 반영)
        margin = balance * self.margin_pct * streak_mult * combined_size_mult
        if margin <= 0:
            return None
        raw_size = margin * self.leverage / price
        size = math.floor(raw_size / MIN_ORDER_SIZE_BTC) * MIN_ORDER_SIZE_BTC
        size = max(round(size, 4), MIN_ORDER_SIZE_BTC)

        # 동적 SL/TP (k × Parkinson Vol) — Parkinson (1980)
        parkinson_vol_str = await self.redis.get("rt:micro:parkinson_vol")
        if parkinson_vol_str and float(parkinson_vol_str) > 0:
            p_vol = float(parkinson_vol_str)
            sl_dist_pct = min(max(self.sl_k * p_vol, 0.001), 0.005)  # 0.1% ~ 0.5%
            tp_dist_pct = min(max(self.tp_k * p_vol, 0.001), 0.005)  # 동적 TP (대칭)
        else:
            sl_dist_pct = self.sl_pct
            tp_dist_pct = self.tp_pct

        # RR 최소 1.0 보장
        if tp_dist_pct < sl_dist_pct:
            tp_dist_pct = sl_dist_pct

        if direction == "long":
            tp_price = round(price * (1 + tp_dist_pct), 1)
            sl_price = round(price * (1 - sl_dist_pct), 1)
        else:
            tp_price = round(price * (1 - tp_dist_pct), 1)
            sl_price = round(price * (1 + sl_dist_pct), 1)

        # 레버리지 설정
        try:
            await self.executor.set_leverage(self.leverage, direction)
        except Exception as e:
            logger.warning(f"레버리지 설정 실패: {e}")

        # 진입 주문 — 시장가 (taker 0.05%, 즉시 체결)
        logger.info(
            f"[SCALP] {direction.upper()} 진입 시도 @ ${price:.0f} | "
            f"TP ${tp_price:.0f} SL ${sl_price:.0f} | "
            f"size={size} BTC, margin=${margin:.1f}"
        )

        side = "buy" if direction == "long" else "sell"
        pos_side = "long" if direction == "long" else "short"
        order = await self.executor._market_order(side, size, pos_side)
        if not order:
            logger.info("[SCALP] 진입 실패 (시장가 미체결) → 포기")
            return None

        fill_price = float(order.get("average", order.get("price", price)) or price)
        filled_size = size  # 시장가는 전량 체결
        fee_info = order.get("fee") or {}
        fee = abs(float(fee_info.get("cost", 0) or 0)) if isinstance(fee_info, dict) else 0

        if filled_size < MIN_ORDER_SIZE_BTC:
            logger.warning(f"[SCALP] 체결 사이즈 부족: {filled_size}")
            return None

        # fill price 기준으로 TP/SL 재계산 (동적 vol 거리 유지)
        if direction == "long":
            tp_price = round(fill_price * (1 + tp_dist_pct), 1)
            sl_price = round(fill_price * (1 - sl_dist_pct), 1)
        else:
            tp_price = round(fill_price * (1 - tp_dist_pct), 1)
            sl_price = round(fill_price * (1 + sl_dist_pct), 1)

        # SL/TP 알고 등록
        algo_ids = await self.executor.set_protection(
            direction=direction,
            total_size=filled_size,
            sl_price=sl_price,
            tp_levels=[(tp_price, 1.0)],  # 100% TP
        )

        # SL 등록 검증 (2초 대기 + 3회 재시도)
        if algo_ids.get("sl"):
            await asyncio.sleep(2.0)
            for attempt in range(3):
                try:
                    inst_id = self.executor.exchange.market(self.symbol)["id"]
                    resp = await self.executor.exchange.private_get_trade_orders_algo_pending(
                        {"instType": "SWAP", "instId": inst_id, "ordType": "trigger"}
                    )
                    pending = resp.get("data", []) if isinstance(resp, dict) else []
                    sl_found = any(
                        p.get("algoClOrdId") == algo_ids["sl"] or p.get("algoId") == algo_ids["sl"]
                        for p in pending
                    )
                    if sl_found:
                        logger.info(f"SL 검증 OK: {algo_ids['sl']}")
                        break
                    else:
                        new_sl = await self.executor.set_stop_loss(direction, filled_size, sl_price)
                        if new_sl:
                            algo_ids["sl"] = new_sl
                            await asyncio.sleep(1.5)
                        else:
                            algo_ids["sl"] = None
                            break
                except Exception:
                    if attempt < 2:
                        await asyncio.sleep(1.0)

        # SL 등록 실패 → 즉시 청산
        if not algo_ids.get("sl"):
            logger.error("[SCALP] SL 등록 실패 → 포지션 즉시 청산")
            if algo_ids.get("tp1"):
                await self.executor.cancel_algo_order(algo_ids["tp1"])
            await self.executor.close_position(direction, filled_size, "sl_protect_failed")
            return None

        # DB 기록
        trade_id = await self.db.insert_scalp_trade({
            "signal_id": signal.get("signal_id", 0),
            "direction": direction,
            "entry_price": fill_price,
            "entry_time": int(time.time() * 1000),
            "size_btc": filled_size,
            "leverage": self.leverage,
            "regime": signal.get("regime", "unknown"),
            "hurst": signal.get("hurst", 0),
            "features_snapshot": json.dumps(signal.get("features", {})),
        })

        pos = ScalpPosition(
            trade_id=trade_id,
            signal_id=signal.get("signal_id", 0),
            direction=direction,
            entry_price=fill_price,
            size=filled_size,
            leverage=self.leverage,
            sl_price=sl_price,
            tp_price=tp_price,
            entry_time=time.time(),
            algo_ids={"sl": algo_ids.get("sl"), "tp": algo_ids.get("tp1")},
        )
        pos.best_price = fill_price
        pos.worst_price = fill_price
        pos.total_fee = abs(fee)

        self.position = pos
        await self.redis.hset(f"pos:active:{self.symbol}", {
            "direction": direction, "entry_price": str(fill_price),
            "size": str(filled_size), "sl_price": str(sl_price),
            "tp_price": str(tp_price), "entry_time": str(pos.entry_time),
            "trade_id": str(trade_id),
        }, ttl=600)

        logger.info(
            f"[SCALP] 진입 완료: {direction.upper()} ${fill_price:.0f} | "
            f"TP ${tp_price:.0f} SL ${sl_price:.0f} | {filled_size} BTC"
        )

        _append_jsonl({
            "type": "scalp_entry",
            "direction": direction,
            "entry_price": round(fill_price, 1),
            "sl_price": round(sl_price, 1),
            "tp_price": round(tp_price, 1),
            "size_btc": round(filled_size, 4),
            "leverage": self.leverage,
            "regime": signal.get("regime", "unknown"),
            "hurst": round(signal.get("hurst", 0), 4),
            "signal_type": signal.get("type", "unknown"),
        })

        return pos

    # ══════════════════════════════════════════
    #  포지션 체크 (500ms 폴링)
    # ══════════════════════════════════════════

    async def check_position(self, current_price: float):
        """시간정지 + SL failsafe + SL self-heal + 외부 청산 감지"""
        pos = self.position
        if not pos:
            return

        now = time.time()
        hold_sec = now - pos.entry_time

        # Best/Worst 추적
        if pos.direction == "long":
            pos.best_price = max(pos.best_price, current_price)
            pos.worst_price = min(pos.worst_price, current_price)
        else:
            pos.best_price = min(pos.best_price, current_price) if pos.best_price > 0 else current_price
            pos.worst_price = max(pos.worst_price, current_price)

        # ── 1. 외부 청산 감지 (서버 TP/SL 체결) ──
        try:
            ex_size = await self.executor.get_position_size(self.symbol)
            if 0 <= ex_size < 1e-8:
                # TP 또는 SL이 서버에서 체결됨
                logger.info(f"[SCALP] 외부 청산 감지 (사이즈≈0)")
                exit_reason = self._infer_exit_reason(current_price, pos)
                await self._finalize(pos, current_price, exit_reason, hold_sec)
                return
        except Exception:
            pass

        # ── 2. SL Failsafe ──
        sl_breached = (
            (pos.direction == "long" and current_price <= pos.sl_price) or
            (pos.direction == "short" and current_price >= pos.sl_price)
        )
        if sl_breached:
            logger.error(f"[SCALP] SL failsafe: ${current_price:.0f} vs SL ${pos.sl_price:.0f}")
            await self._close_and_finalize(pos, "sl_failsafe", hold_sec)
            return

        # ── 3. SL Self-Heal (5초 간격) ──
        # 시간 정지 없음 — 프로 동일 (SL/TP/시그널반전으로만 청산)
        sl_id = pos.algo_ids.get("sl")
        if sl_id and (now - pos._last_sl_verify) >= 5:
            pos._last_sl_verify = now
            try:
                inst_id = self.executor.exchange.market(self.symbol)["id"]
                resp = await self.executor.exchange.private_get_trade_orders_algo_pending(
                    {"instType": "SWAP", "instId": inst_id, "ordType": "trigger"}
                )
                pending = resp.get("data", []) if isinstance(resp, dict) else []
                sl_found = any(
                    p.get("algoClOrdId") == sl_id or p.get("algoId") == sl_id
                    for p in pending
                )
                if not sl_found:
                    pos._sl_lost_count += 1
                    logger.warning(f"SL 소실 ({pos._sl_lost_count}회) → 재등록")
                    new_id = await self.executor.set_stop_loss(
                        pos.direction, pos.size, pos.sl_price
                    )
                    pos.algo_ids["sl"] = new_id

                    if pos._sl_lost_count >= 3:
                        logger.error(f"SL 3회 소실 → 강제 청산")
                        await self._close_and_finalize(pos, "sl_repeated_loss", hold_sec)
                        return
            except Exception as e:
                logger.debug(f"SL 검증 예외: {e}")

        elif not sl_id:
            # SL ID 없음 → 재등록
            new_id = await self.executor.set_stop_loss(
                pos.direction, pos.size, pos.sl_price
            )
            if new_id:
                pos.algo_ids["sl"] = new_id

    # ══════════════════════════════════════════
    #  청산 + 정리
    # ══════════════════════════════════════════

    async def _close_and_finalize(self, pos: ScalpPosition, reason: str, hold_sec: float):
        """알고 취소 → market 청산 → 정리"""
        # 알고 취소
        for key in ("sl", "tp"):
            aid = pos.algo_ids.get(key)
            if aid:
                await self.executor.cancel_algo_order(aid)
        try:
            await self.executor.cancel_all_algos()
        except Exception:
            pass

        # 청산
        exit_price = 0.0
        try:
            order = await self.executor.close_position(pos.direction, pos.size, reason)
            if order and isinstance(order, dict):
                exit_price = float(order.get("price", 0) or 0)
                fee_info = order.get("fee") or {}
                if isinstance(fee_info, dict):
                    pos.total_fee += abs(float(fee_info.get("cost", 0) or 0))
        except Exception as e:
            logger.error(f"청산 실패: {e}")

        if exit_price <= 0:
            try:
                ep, _ = await self.executor.get_position_entry(self.symbol)
                exit_price = ep if ep > 0 else pos.entry_price
            except Exception:
                exit_price = pos.entry_price

        await self._finalize(pos, exit_price, reason, hold_sec)

    async def _finalize(self, pos: ScalpPosition, exit_price: float,
                        reason: str, hold_sec: float):
        """PnL 계산 + DB 업데이트 + 로깅 + 정리"""
        if pos.direction == "long":
            pnl_pct = (exit_price - pos.entry_price) / pos.entry_price * pos.leverage * 100
        else:
            pnl_pct = (pos.entry_price - exit_price) / pos.entry_price * pos.leverage * 100

        pnl_usdt = pos.size * abs(exit_price - pos.entry_price)
        if (pos.direction == "long" and exit_price < pos.entry_price) or \
           (pos.direction == "short" and exit_price > pos.entry_price):
            pnl_usdt = -pnl_usdt
        pnl_usdt -= pos.total_fee

        # DB 업데이트
        try:
            await self.db.update_scalp_trade_exit(pos.trade_id, {
                "exit_price": round(exit_price, 1),
                "exit_time": int(time.time() * 1000),
                "exit_reason": reason,
                "pnl_usdt": round(pnl_usdt, 2),
                "pnl_pct": round(pnl_pct, 2),
                "fee_total": round(pos.total_fee, 2),
                "hold_sec": int(hold_sec),
            })
        except Exception as e:
            logger.error(f"DB 업데이트 실패: {e}")

        # JSONL
        _append_jsonl({
            "type": "scalp_exit",
            "direction": pos.direction,
            "entry_price": round(pos.entry_price, 1),
            "exit_price": round(exit_price, 1),
            "pnl_pct": round(pnl_pct, 2),
            "pnl_usdt": round(pnl_usdt, 2),
            "hold_sec": int(hold_sec),
            "exit_reason": reason,
            "fee": round(pos.total_fee, 2),
        })

        marker = "+" if pnl_usdt > 0 else ""
        logger.info(
            f"[SCALP] 청산: {pos.direction.upper()} {reason} | "
            f"${pos.entry_price:.0f}→${exit_price:.0f} | "
            f"{marker}${pnl_usdt:.2f} ({pnl_pct:+.1f}%) | {int(hold_sec)}초"
        )

        # Redis 정리
        await self.redis.delete(f"pos:active:{self.symbol}")

        # 포지션 해제
        self.position = None

    def _calc_margin_pct(self, pos: ScalpPosition, current_price: float) -> float:
        """현재 마진 수익률 (%) 계산"""
        if pos.direction == "long":
            return (current_price - pos.entry_price) / pos.entry_price * pos.leverage * 100
        else:
            return (pos.entry_price - current_price) / pos.entry_price * pos.leverage * 100

    def _infer_exit_reason(self, current_price: float, pos: ScalpPosition) -> str:
        """서버 청산 시 사유 추론"""
        if pos.direction == "long":
            if current_price >= pos.tp_price * 0.999:
                return "tp"
            if current_price <= pos.sl_price * 1.001:
                return "sl"
        else:
            if current_price <= pos.tp_price * 1.001:
                return "tp"
            if current_price >= pos.sl_price * 0.999:
                return "sl"
        return "external"
