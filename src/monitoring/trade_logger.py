"""
TradeLogger — Grid Trading JSONL 로거

주간 JSONL 파일 + 텍스트 로그 파일.

이벤트 타입:
  - grid_cycle: 그리드 사이클 완성
  - hourly_snapshot: 시간별 스냅샷
  - regime_change: 모드 전환 (ACTIVE/PAUSED/FROZEN)
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

    def log_grid_cycle(self, level_id: int, side: str,
                       entry_price: float, exit_price: float,
                       pnl: float, total_cycles: int):
        self.logger.info(
            f"GRID CYCLE | Lv{level_id} {side.upper()} | "
            f"${entry_price:,.1f} → ${exit_price:,.1f} | "
            f"PnL ${pnl:+.3f} | 총 {total_cycles}사이클"
        )

    def log_risk_event(self, event: str, detail: str = ""):
        self.logger.warning(f"RISK  | {event} | {detail}")

    def log_error(self, module: str, error: str):
        self.logger.error(f"ERROR | {module} | {error}")
