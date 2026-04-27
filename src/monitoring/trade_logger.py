import json
import logging
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from datetime import datetime, timezone

LOG_DIR = Path(__file__).parent.parent.parent / "data" / "logs"


def _week_tag() -> str:
    """현재 ISO 주 태그: '2026-W18' (월요일 기준)"""
    now = datetime.now(timezone.utc)
    return f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"


def _jsonl_path() -> Path:
    """주간 JSONL 파일: trades_2026-W18.jsonl"""
    return LOG_DIR / f"trades_{_week_tag()}.jsonl"


def _append_jsonl(record: dict):
    """단일 JSON line 을 주간 파일에 append — fsync 로 즉시 디스크 반영"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        record["ts"] = int(time.time())
        record["ts_iso"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(_jsonl_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            try:
                import os
                os.fsync(f.fileno())
            except Exception:
                pass
    except Exception:
        pass  # 로그 실패가 매매를 막지 않게


class TradeLogger:
    """매매 전용 로거 (주간 파일 영구 보존)"""

    def __init__(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger("TradeLog")
        self.logger.setLevel(logging.DEBUG)

        if not self.logger.handlers:
            # 매매 로그 (INFO) — 매주 월요일 로테이션, 영구 보존
            trade_handler = TimedRotatingFileHandler(
                LOG_DIR / "trades.log",
                when="W0",          # 월요일 기준
                backupCount=520,    # 10년치 보존
                encoding="utf-8",
                utc=True,
            )
            trade_handler.setLevel(logging.INFO)
            trade_handler.setFormatter(
                logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
            )
            trade_handler.suffix = "%Y-W%W"  # trades.log.2026-W18
            self.logger.addHandler(trade_handler)

            # 시그널 상세 로그 (DEBUG) — 매주 월요일 로테이션
            signal_handler = TimedRotatingFileHandler(
                LOG_DIR / "signals.log",
                when="W0",
                backupCount=520,
                encoding="utf-8",
                utc=True,
            )
            signal_handler.setLevel(logging.DEBUG)
            signal_handler.setFormatter(
                logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
            )
            signal_handler.suffix = "%Y-W%W"
            self.logger.addHandler(signal_handler)

    def log_entry(self, direction: str, grade: str, score: float,
                  entry_price: float, sl_price: float, leverage: int,
                  margin: float, signals: dict = None):
        self.logger.info(
            f"ENTRY | {grade} {direction.upper()} | "
            f"${entry_price:,.1f} | SL ${sl_price:,.1f} | "
            f"{leverage}x | margin ${margin:,.0f} | score {score:.1f}"
        )
        if signals:
            active = {
                k: f"{v.get('direction','?')}({v.get('strength',0):.1f})"
                for k, v in signals.items()
                if v.get("strength", 0) > 0
            }
            self.logger.debug(f"SIGNALS | {active}")
        # JSONL 영구 기록 (DB 손상 무관)
        _append_jsonl({
            "type": "entry",
            "direction": direction,
            "grade": grade,
            "score": round(float(score), 2),
            "entry_price": round(float(entry_price), 1),
            "sl_price": round(float(sl_price), 1),
            "leverage": int(leverage),
            "margin": round(float(margin), 2),
        })

    def log_exit(self, direction: str, exit_reason: str,
                 entry_price: float, exit_price: float,
                 pnl_pct: float, pnl_usdt: float,
                 hold_min: int, fee: float, **extra):
        self.logger.info(
            f"EXIT  | {direction.upper()} {exit_reason} | "
            f"${entry_price:,.1f} -> ${exit_price:,.1f} | "
            f"{pnl_pct:+.2f}% (${pnl_usdt:+.2f}) | "
            f"{hold_min}min | fee ${fee:.2f}"
        )
        # JSONL 영구 기록 (DB 손상 무관)
        record = {
            "type": "exit",
            "direction": direction,
            "exit_reason": exit_reason,
            "entry_price": round(float(entry_price), 1),
            "exit_price": round(float(exit_price), 1),
            "pnl_pct": round(float(pnl_pct), 2),
            "pnl_usdt": round(float(pnl_usdt), 2),
            "hold_min": int(hold_min),
            "fee": round(float(fee), 2),
        }
        # 추가 필드 (grade, score, leverage, setup, regime 등)
        for k, v in extra.items():
            if v is not None:
                record[k] = v
        _append_jsonl(record)

    def log_partial_close(self, direction: str, reason: str,
                          close_pct: float, price: float):
        self.logger.info(
            f"PARTIAL | {direction.upper()} {reason} | "
            f"{close_pct*100:.0f}% @ ${price:,.1f}"
        )

    def log_trailing_update(self, direction: str, tier: int, new_sl: float):
        self.logger.info(
            f"TRAIL | {direction.upper()} Tier {tier} | SL -> ${new_sl:,.1f}"
        )

    def log_signal_summary(self, score: float, grade: str, direction: str,
                           long_score: float, short_score: float, bonus: float):
        self.logger.debug(
            f"GRADE | {grade} {direction.upper()} | "
            f"score {score:.1f} | L:{long_score:.1f} S:{short_score:.1f} | "
            f"bonus {bonus:.1f}"
        )

    def log_risk_event(self, event: str, detail: str = ""):
        self.logger.warning(f"RISK  | {event} | {detail}")

    def log_error(self, module: str, error: str):
        self.logger.error(f"ERROR | {module} | {error}")
