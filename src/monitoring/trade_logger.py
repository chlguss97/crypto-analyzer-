"""
TradeLogger v3 — 스캘핑 JSONL 로거

주간 JSONL 파일 + 텍스트 로그 파일.
모든 이벤트는 _append_jsonl()로 기록.

이벤트 타입:
  - scalp_entry: 스캘핑 진입
  - scalp_exit: 스캘핑 청산
  - candidate: 시그널 감지 (Shadow 포함)
  - shadow_result: Shadow 라벨 확정
  - gate_block: 게이트 차단
  - hourly_snapshot: 시간별 스냅샷
  - adaptive_update: AdaptiveParams 갱신
"""

import json
import logging
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from datetime import datetime, timezone

LOG_DIR = Path(__file__).parent.parent.parent / "data" / "logs"


def _week_tag() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-W%W")


def _jsonl_path() -> Path:
    return LOG_DIR / f"trades_{_week_tag()}.jsonl"


def _append_jsonl(record: dict):
    """단일 JSON line을 주간 파일에 append"""
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
        pass


class TradeLogger:
    """매매 전용 텍스트 로거 (주간 파일 영구 보존)"""

    def __init__(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger("TradeLog")
        self.logger.setLevel(logging.INFO)

        if not self.logger.handlers:
            handler = TimedRotatingFileHandler(
                LOG_DIR / "trades.log",
                when="W0",
                backupCount=520,
                encoding="utf-8",
                utc=True,
            )
            handler.setLevel(logging.INFO)
            handler.setFormatter(
                logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
            )
            handler.suffix = "%Y-W%W"
            self.logger.addHandler(handler)

    def log_scalp_entry(self, direction: str, entry_price: float,
                        sl_price: float, tp_price: float,
                        size: float, leverage: int, regime: str = "?"):
        self.logger.info(
            f"SCALP ENTRY | {direction.upper()} | "
            f"${entry_price:,.1f} | SL ${sl_price:,.1f} TP ${tp_price:,.1f} | "
            f"{size} BTC {leverage}x | regime={regime}"
        )

    def log_scalp_exit(self, direction: str, exit_reason: str,
                       entry_price: float, exit_price: float,
                       pnl_pct: float, pnl_usdt: float,
                       hold_sec: int, fee: float):
        self.logger.info(
            f"SCALP EXIT  | {direction.upper()} {exit_reason} | "
            f"${entry_price:,.1f} → ${exit_price:,.1f} | "
            f"{pnl_pct:+.2f}% (${pnl_usdt:+.2f}) | "
            f"{hold_sec}s | fee ${fee:.2f}"
        )

    def log_risk_event(self, event: str, detail: str = ""):
        self.logger.warning(f"RISK  | {event} | {detail}")

    def log_error(self, module: str, error: str):
        self.logger.error(f"ERROR | {module} | {error}")
