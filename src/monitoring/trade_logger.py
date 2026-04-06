import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime, timezone

LOG_DIR = Path(__file__).parent.parent.parent / "data" / "logs"


class TradeLogger:
    """매매 전용 로거 (파일 기록)"""

    def __init__(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger("TradeLog")
        self.logger.setLevel(logging.DEBUG)

        if not self.logger.handlers:
            # 매매 로그 (INFO)
            trade_handler = RotatingFileHandler(
                LOG_DIR / "trades.log",
                maxBytes=10 * 1024 * 1024,  # 10MB
                backupCount=5,
                encoding="utf-8",
            )
            trade_handler.setLevel(logging.INFO)
            trade_handler.setFormatter(
                logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
            )
            self.logger.addHandler(trade_handler)

            # 시그널 상세 로그 (DEBUG)
            signal_handler = RotatingFileHandler(
                LOG_DIR / "signals.log",
                maxBytes=10 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            signal_handler.setLevel(logging.DEBUG)
            signal_handler.setFormatter(
                logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
            )
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

    def log_exit(self, direction: str, exit_reason: str,
                 entry_price: float, exit_price: float,
                 pnl_pct: float, pnl_usdt: float,
                 hold_min: int, fee: float):
        self.logger.info(
            f"EXIT  | {direction.upper()} {exit_reason} | "
            f"${entry_price:,.1f} -> ${exit_price:,.1f} | "
            f"{pnl_pct:+.2f}% (${pnl_usdt:+.2f}) | "
            f"{hold_min}min | fee ${fee:.2f}"
        )

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
