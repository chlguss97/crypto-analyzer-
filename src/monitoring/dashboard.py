import asyncio
import json
import logging
import os
import secrets
import time as _time
from pathlib import Path
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Depends, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import Optional

from src.data.storage import Database, RedisClient
from src.utils.helpers import load_config, load_env

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# ── Basic Auth — /health 는 bypass (모니터링/헬스체크용) ──
load_env()
_security = HTTPBasic(auto_error=False)
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "")

_PUBLIC_PATHS = {"/health", "/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"}


async def verify_auth(
    request: Request,
    credentials: Optional[HTTPBasicCredentials] = Depends(_security),
):
    """Basic Auth — /health 등 public path는 bypass"""
    if request.url.path in _PUBLIC_PATHS:
        return None
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Auth required",
            headers={"WWW-Authenticate": "Basic"},
        )
    if not DASHBOARD_PASS:
        # 비밀번호 미설정 시 읽기 전용 모드 (매매 API 는 _require_auth 에서 별도 차단)
        return credentials.username
    correct_user = secrets.compare_digest(credentials.username, DASHBOARD_USER)
    correct_pass = secrets.compare_digest(credentials.password, DASHBOARD_PASS)
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


app = FastAPI(
    title="CryptoAnalyzer v1.0",
    version="1.0.0",
    dependencies=[Depends(verify_auth)],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    """컨테이너 헬스체크 — 인증 불필요. uvicorn 이벤트 루프 살아있으면 즉시 200"""
    return {
        "status": "ok",
        "initialized": _initialized,
        "db": db is not None and db._db is not None,
        "redis": redis is not None and redis.connected,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/")
async def index():
    """메인 대시보드 페이지"""
    return FileResponse(STATIC_DIR / "index.html")

# 전역 — lazy init 에서 생성
load_env()
config = load_config()
db: Optional[Database] = None
redis: Optional[RedisClient] = None
executor = None  # 별도 컨테이너에서는 항상 None (매매 엔드포인트 비활성)

logger.info(f"Dashboard 모듈 로드 완료 — user={DASHBOARD_USER} pass_set={bool(DASHBOARD_PASS)}")


# ── Request Models ──

class ManualOrderRequest(BaseModel):
    direction: str          # 'long' | 'short'
    leverage: int = 10      # 1~100
    margin_usdt: float      # 마진 금액 (USDT)
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    order_type: str = "market"  # 'market' | 'limit'
    limit_price: Optional[float] = None

class CloseRequest(BaseModel):
    direction: str          # 'long' | 'short'
    close_pct: float = 1.0  # 0~1 (부분 청산)


# ── Lazy Init (race-safe + timeout) ──

_initialized = False
_init_lock = asyncio.Lock()


async def _ensure_initialized():
    """첫 API 호출 시 lazy DB/Redis 연결. Lock 으로 race 차단 + timeout 으로 hang 방지."""
    global _initialized, db, redis
    if _initialized and db is not None and redis is not None:
        return
    async with _init_lock:
        if _initialized and db is not None and redis is not None:
            return
        try:
            _db = Database()
            _rd = RedisClient()
            # 각 connect 에 8초 timeout — 네트워크 이슈로 이벤트 루프 행 방지
            try:
                await asyncio.wait_for(_db.connect(), timeout=8.0)
            except asyncio.TimeoutError:
                logger.error("Dashboard DB connect timeout (8s) — 일부 API 불가")
            try:
                await asyncio.wait_for(_rd.connect(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.error("Dashboard Redis connect timeout (5s) — 일부 API 불가")
            db = _db
            redis = _rd
            _initialized = True
            logger.info(
                f"Dashboard lazy-init 완료 — "
                f"db={'OK' if db._db else 'FAIL'} redis={'OK' if redis.connected else 'FAIL'}"
            )
        except Exception as e:
            logger.exception(f"Dashboard 초기화 에러: {e}")


@app.on_event("startup")
async def startup():
    port = os.getenv("DASHBOARD_PORT", "8000")
    logger.info(f"Dashboard 시작 — uvicorn listening on 0.0.0.0:{port} (lazy init mode)")
    # Redis 를 백그라운드에서 미리 초기화해둠 — 첫 요청 지연 방지. 실패해도 서버는 뜸.
    asyncio.create_task(_ensure_initialized())


@app.on_event("shutdown")
async def shutdown():
    logger.info("Dashboard 종료 중...")
    if db is not None:
        try:
            await db.close()
        except Exception as e:
            logger.debug(f"DB close 에러: {e}")
    if redis is not None:
        try:
            await redis.close()
        except Exception as e:
            logger.debug(f"Redis close 에러: {e}")


# ── GET 엔드포인트 ──

@app.get("/api/status")
async def get_status():
    """봇 상태 조회"""
    await _ensure_initialized()
    status = await redis.get("sys:bot_status") or "unknown"
    heartbeat = await redis.get("sys:last_heartbeat") or "0"

    # 리스크 상태
    streak = await redis.get("risk:streak") or "0"
    daily_pnl = await redis.get("risk:daily_pnl") or "0"

    autotrading = await redis.get("sys:autotrading") or "off"

    return {
        "status": status,
        "last_heartbeat": heartbeat,
        "streak": int(streak),
        "daily_pnl_pct": float(daily_pnl),
        "autotrading": autotrading,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/position")
async def get_position():
    await _ensure_initialized()
    """현재 활성 포지션"""
    symbol = config["exchange"]["symbol"]
    pos_data = await redis.hgetall(f"pos:active:{symbol}")

    if not pos_data:
        return {"active": False, "position": None}

    # 현재가
    price = await redis.get("rt:price:BTC-USDT-SWAP")

    return {
        "active": True,
        "position": pos_data,
        "current_price": float(price) if price else None,
    }


@app.get("/api/signals")
async def get_signals():
    await _ensure_initialized()
    """최신 시그널 합산 결과"""
    symbol = config["exchange"]["symbol"]

    fast = await redis.get_json(f"sig:fast:{symbol}")
    slow = await redis.get_json(f"sig:slow:{symbol}")
    aggregated = await redis.get_json(f"sig:aggregated:{symbol}")

    return {
        "fast_signals": fast,
        "slow_signals": slow,
        "aggregated": aggregated,
    }


@app.get("/api/trades")
async def get_trades(days: int = 7, mode: str = "all"):
    await _ensure_initialized()
    """최근 매매 내역 (mode: all, paper, real)"""
    import time
    since = int((time.time() - days * 86400) * 1000)

    if mode == "paper":
        cursor = await db._db.execute(
            """SELECT * FROM trades
               WHERE entry_time >= ? AND grade LIKE 'PAPER_%'
               ORDER BY entry_time DESC""",
            (since,),
        )
    elif mode == "real":
        cursor = await db._db.execute(
            """SELECT * FROM trades
               WHERE entry_time >= ? AND grade NOT LIKE 'PAPER_%'
               ORDER BY entry_time DESC""",
            (since,),
        )
    else:
        cursor = await db._db.execute(
            """SELECT * FROM trades
               WHERE entry_time >= ?
               ORDER BY entry_time DESC""",
            (since,),
        )
    rows = await cursor.fetchall()

    trades = []
    for row in rows:
        trade = dict(row)
        if trade.get("signals_snapshot"):
            trade["signals_snapshot"] = "(생략)"
        trades.append(trade)

    return {"trades": trades, "count": len(trades)}


@app.get("/api/candles")
async def get_candles(timeframe: str = "15m", limit: int = 200):
    await _ensure_initialized()
    """캔들 데이터 조회 (차트용)"""
    symbol = config["exchange"]["symbol"]
    cursor = await db._db.execute(
        """SELECT timestamp, open, high, low, close, volume
           FROM candles
           WHERE symbol=? AND timeframe=?
           ORDER BY timestamp DESC LIMIT ?""",
        (symbol, timeframe, limit),
    )
    rows = await cursor.fetchall()
    candles = [dict(r) for r in reversed(rows)]

    # lightweight-charts 포맷: timestamp를 초 단위로
    for c in candles:
        c["time"] = c["timestamp"] // 1000

    return {"candles": candles, "count": len(candles), "timeframe": timeframe}


@app.get("/api/daily-summary")
async def get_daily_summary():
    await _ensure_initialized()
    """일일 성과 요약"""
    cursor = await db._db.execute(
        """SELECT * FROM daily_summary
           ORDER BY date DESC LIMIT 30"""
    )
    rows = await cursor.fetchall()
    return {"summaries": [dict(r) for r in rows]}


@app.get("/api/paper/stats")
async def get_paper_stats():
    await _ensure_initialized()
    """가상매매 통계"""
    cursor = await db._db.execute(
        """SELECT
            COUNT(*) as total,
            SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN pnl_pct <= 0 THEN 1 ELSE 0 END) as losses,
            COALESCE(SUM(pnl_usdt), 0) as total_pnl,
            COALESCE(AVG(pnl_pct), 0) as avg_pnl_pct
        FROM trades
        WHERE grade LIKE 'PAPER_%' AND exit_time IS NOT NULL"""
    )
    row = dict(await cursor.fetchone())

    # 최근 20건
    cursor2 = await db._db.execute(
        """SELECT id, direction, grade, score, entry_price, exit_price,
                  entry_time, exit_time, exit_reason, pnl_pct, pnl_usdt, leverage
           FROM trades
           WHERE grade LIKE 'PAPER_%' AND exit_time IS NOT NULL
           ORDER BY exit_time DESC LIMIT 20"""
    )
    recent = [dict(r) for r in await cursor2.fetchall()]

    # 진행 중 가상 포지션 수
    cursor3 = await db._db.execute(
        """SELECT COUNT(*) FROM trades
           WHERE grade LIKE 'PAPER_%' AND exit_time IS NULL"""
    )
    active = (await cursor3.fetchone())[0]

    return {
        "total": row["total"],
        "wins": row["wins"] or 0,
        "losses": row["losses"] or 0,
        "win_rate": (row["wins"] or 0) / max(row["total"], 1) * 100,
        "total_pnl": round(row["total_pnl"], 2),
        "avg_pnl_pct": round(row["avg_pnl_pct"], 4),
        "active_positions": active,
        "recent_trades": recent,
    }


@app.get("/api/equity-curve")
async def get_equity_curve():
    await _ensure_initialized()
    """자산 곡선 (trades 기반 계산)"""
    cursor = await db._db.execute(
        """SELECT entry_time, pnl_usdt, pnl_pct
           FROM trades
           WHERE exit_time IS NOT NULL
           ORDER BY exit_time ASC"""
    )
    rows = await cursor.fetchall()

    curve = []
    cumulative = 0
    for row in rows:
        r = dict(row)
        cumulative += r.get("pnl_usdt", 0) or 0
        curve.append({
            "timestamp": r["entry_time"],
            "cumulative_pnl": round(cumulative, 2),
        })

    return {"equity_curve": curve}


# ── POST 엔드포인트 ──

@app.post("/api/pause")
async def pause_bot():
    await _ensure_initialized()
    """봇 일시정지"""
    await redis.set("sys:bot_status", "paused")
    logger.warning("봇 일시정지 (대시보드)")
    return {"status": "paused"}


@app.post("/api/resume")
async def resume_bot():
    await _ensure_initialized()
    """봇 재개"""
    await redis.set("sys:bot_status", "running")
    logger.info("봇 재개 (대시보드)")
    return {"status": "running"}


@app.post("/api/close-all")
async def close_all():
    """전 포지션 청산 (킬 스위치) — Redis 플래그 + 명령 큐"""
    await _ensure_initialized()
    _require_auth()
    await redis.set("sys:bot_status", "stopped")
    # bot 컨테이너가 명령 큐 구독 → close_all 실행
    await redis.rpush("cmd:bot", json.dumps({"action": "close_all", "reason": "dashboard_kill"}))
    logger.warning("킬 스위치 작동 (대시보드)")
    return {"status": "stopped", "message": "전 포지션 청산 요청 전송됨"}


# ── 실시간 데이터 ──

@app.get("/api/market")
async def get_market():
    """실시간 시장 데이터 (Redis 기반)"""
    await _ensure_initialized()
    ticker = await redis.hgetall("rt:ticker:BTC-USDT-SWAP")
    oi = await redis.get("rt:oi:BTC-USDT-SWAP")
    funding = await redis.get("rt:funding:BTC-USDT-SWAP")
    ls_ratio = await redis.get("rt:ls_ratio:BTC-USDT-SWAP")

    return {
        "ticker": ticker or {},
        "open_interest": float(oi) if oi else None,
        "funding_rate": float(funding) if funding else None,
        "long_short_ratio": float(ls_ratio) if ls_ratio else None,
    }


# ── 매매 엔드포인트 (별도 컨테이너: Redis 명령 큐 → bot 컨테이너가 실행) ──

def _require_auth():
    """매매 API는 비밀번호 필수"""
    if not DASHBOARD_PASS:
        raise HTTPException(403, "매매 API는 DASHBOARD_PASS 설정 필수")


@app.post("/api/trade/open")
async def manual_open(req: ManualOrderRequest):
    """수동 매매 진입 — Redis 명령 큐로 bot 컨테이너에 위임"""
    _require_auth()
    await _ensure_initialized()
    await redis.rpush("cmd:bot", json.dumps({
        "action": "open",
        "direction": req.direction,
        "leverage": req.leverage,
        "margin_usdt": req.margin_usdt,
        "sl_price": req.sl_price,
        "tp_price": req.tp_price,
        "order_type": req.order_type,
        "limit_price": req.limit_price,
    }))
    return {"success": True, "queued": True, "msg": "bot 컨테이너에 명령 전달됨"}


@app.post("/api/trade/close")
async def manual_close(req: CloseRequest):
    """수동 포지션 청산 — Redis 명령 큐"""
    _require_auth()
    await _ensure_initialized()
    await redis.rpush("cmd:bot", json.dumps({
        "action": "close",
        "direction": req.direction,
        "close_pct": req.close_pct,
    }))
    return {"success": True, "queued": True, "msg": "bot 컨테이너에 명령 전달됨"}


@app.get("/api/trade/positions")
async def get_live_positions():
    """거래소 실시간 포지션 조회 (Redis 캐시)"""
    await _ensure_initialized()
    symbol = config["exchange"]["symbol"]
    pos_data = await redis.hgetall(f"pos:active:{symbol}")
    balance_raw = await redis.get("sys:balance") or "0"
    positions = []
    if pos_data:
        positions.append(pos_data)
    try:
        balance = float(balance_raw)
    except (TypeError, ValueError):
        balance = 0.0
    return {"positions": positions, "balance": round(balance, 2)}


@app.post("/api/autotrading")
async def toggle_autotrading():
    """자동매매 ON/OFF 토글 — Redis 플래그 + 알림 요청 큐"""
    await _ensure_initialized()
    current = await redis.get("sys:autotrading") or "off"
    new_state = "off" if current == "on" else "on"
    await redis.set("sys:autotrading", new_state)
    await redis.rpush("cmd:bot", json.dumps({
        "action": "notify", "msg": f"자동매매 {new_state.upper()}",
    }))
    logger.info(f"자동매매: {new_state.upper()}")
    return {"autotrading": new_state}


# ── 사용자 수동 SL/TP 수정 ──

class ManualSlRequest(BaseModel):
    symbol: Optional[str] = None
    price: float

class ManualTpRequest(BaseModel):
    symbol: Optional[str] = None
    price: float


@app.post("/api/position/sl")
async def manual_update_sl(req: ManualSlRequest):
    """사용자 SL 가격 수동 수정 — Redis 명령 큐"""
    _require_auth()
    await _ensure_initialized()
    sym = req.symbol or config["exchange"]["symbol"]
    await redis.rpush("cmd:bot", json.dumps({
        "action": "update_sl", "symbol": sym, "price": float(req.price),
    }))
    return {"ok": True, "queued": True, "symbol": sym, "price": req.price}


@app.post("/api/position/tp")
async def manual_update_tp(req: ManualTpRequest):
    """사용자 TP1 가격 수동 수정 — Redis 명령 큐"""
    _require_auth()
    await _ensure_initialized()
    sym = req.symbol or config["exchange"]["symbol"]
    await redis.rpush("cmd:bot", json.dumps({
        "action": "update_tp", "symbol": sym, "price": float(req.price),
    }))
    return {"ok": True, "queued": True, "symbol": sym, "price": req.price}


# ── 모델/ML 엔드포인트 ──

# ML 인스턴스 캐시 (매 요청마다 load 방지)
_ml_cache = {"swing": None, "scalp": None, "loaded_at": 0}


def _get_ml_instances():
    """ML 인스턴스 캐시 (60초마다 리로드)"""
    import time as t
    now = t.time()
    if now - _ml_cache["loaded_at"] > 60 or _ml_cache["swing"] is None:
        from src.strategy.adaptive_ml import AdaptiveML
        sw = AdaptiveML(mode="swing")
        sc = AdaptiveML(mode="scalp")
        sw.load()
        sc.load()
        _ml_cache["swing"] = sw
        _ml_cache["scalp"] = sc
        _ml_cache["loaded_at"] = now
    return _ml_cache["swing"], _ml_cache["scalp"]


_signal_tracker_cache = {"instance": None, "loaded_at": 0}


@app.get("/api/signal-tracker")
async def get_signal_tracker():
    await _ensure_initialized()
    """시그널 기여도 추적 결과 (10초 캐시)"""
    import time as _t
    from src.strategy.signal_tracker import SignalTracker
    now = _t.time()
    if now - _signal_tracker_cache["loaded_at"] > 10 or _signal_tracker_cache["instance"] is None:
        _signal_tracker_cache["instance"] = SignalTracker()
        _signal_tracker_cache["loaded_at"] = now
    return _signal_tracker_cache["instance"].get_summary()


@app.post("/api/signal-tracker/reset")
async def reset_signal_tracker():
    """시그널 추적 리셋"""
    from src.strategy.signal_tracker import SignalTracker
    tracker = SignalTracker()
    tracker.reset()
    return {"status": "reset"}


@app.get("/api/meta")
async def get_meta():
    """최근 메타 학습 결과 (자가 업그레이드)"""
    meta = await redis.get_json("sys:last_meta")
    if not meta:
        return {"available": False}
    meta["available"] = True
    return meta


@app.post("/api/meta/run")
async def trigger_meta():
    """레거시 — TradeEngine에서는 SetupTracker가 대체"""
    return {"status": "disabled", "note": "Use /api/setup-tracker instead"}


@app.get("/api/setup-tracker")
async def get_setup_tracker():
    await _ensure_initialized()
    """SetupTracker 셋업별 성과 조회"""
    try:
        from src.strategy.setup_tracker import SetupTracker
        tracker = SetupTracker()
        return tracker.get_summary()
    except Exception as e:
        return {"error": str(e), "A": {"total": 0}, "B": {"total": 0}, "C": {"total": 0}}


@app.get("/api/backtest")
async def get_backtest():
    """최근 자동 백테스트 결과"""
    bt = await redis.get_json("sys:last_backtest")
    if not bt:
        return {"trades": 0, "available": False}
    bt["available"] = True
    return bt


@app.post("/api/backtest/run")
async def trigger_backtest():
    """레거시 — TradeEngine 백테스트 미구현"""
    return {"status": "disabled", "note": "TradeEngine backtest not yet implemented"}


@app.get("/api/news")
async def get_news_status():
    """뉴스 필터 상태 + 다음 이벤트"""
    from src.trading.news_filter import NewsFilter
    nf = NewsFilter()
    blocked, reason = nf.is_news_blackout()
    upcoming = nf.get_upcoming_events(days=7)
    return {
        "blocked": blocked,
        "reason": reason,
        "upcoming": upcoming,
    }


@app.get("/api/risk/state")
async def get_risk_state():
    await _ensure_initialized()
    """실거래 리스크 상태"""
    daily = await redis.get("risk:daily_pnl") or "0"
    weekly = await redis.get("risk:weekly_pnl") or "0"
    streak = await redis.get("risk:streak") or "0"
    cooldown = await redis.get("risk:cooldown_until") or "0"
    import time as _t
    return {
        "daily_pnl_pct": float(daily),
        "weekly_pnl_pct": float(weekly),
        "streak": int(streak),
        "cooldown_remaining_min": max(0, (int(cooldown) - int(_t.time())) // 60),
        "daily_limit": -10.0,
        "weekly_limit": -20.0,
        "daily_blocked": float(daily) <= -10.0,
        "weekly_blocked": float(weekly) <= -20.0,
    }


@app.get("/api/scalp/state")
async def get_scalp_state():
    await _ensure_initialized()
    """TradeEngine 실시간 상태 (레거시 엔드포인트명 유지)"""
    state = await redis.get_json("sys:trade_state")
    if not state:
        state = {"setup": None, "direction": "neutral", "score": 0,
                 "trend": "neutral", "structure": "unknown", "streak": 0}
    return state


@app.get("/api/regime")
async def get_regime():
    await _ensure_initialized()
    """현재 마켓 레짐 조회"""
    regime_detail = await redis.get_json("sys:regime_detail")
    regime = await redis.get("sys:regime") or "ranging"

    if not regime_detail:
        regime_detail = {"regime": regime, "confidence": 0, "scores": {}}

    return regime_detail


@app.get("/api/ml/status")
async def ml_status():
    await _ensure_initialized()
    """TradeEngine 상태 조회"""
    regime = await redis.get("sys:regime") or "ranging"
    return {
        "engine": "TradeEngine v1",
        "mode": "setup_abc",
        "ml_status": "cold_start",
        "active_model": "trade_engine",
        "ml_enabled": False,
        "current_regime": regime,
    }


@app.post("/api/ml/toggle")
async def toggle_ml():
    """ML ON/OFF"""
    current = await redis.get("sys:ml_enabled") or "on"
    new_state = "off" if current == "on" else "on"
    await redis.set("sys:ml_enabled", new_state)
    return {"ml_enabled": new_state == "on"}


class ModelSelectRequest(BaseModel):
    model: str

@app.post("/api/ml/model")
async def select_model(req: ModelSelectRequest):
    """모델 선택 (레거시 — TradeEngine에서는 단일 모델)"""
    return {"active_model": "trade_engine", "note": "unified model, no selection needed"}


@app.post("/api/ml/retrain")
async def retrain_ml():
    """ML 재학습 (TradeEngine — 현재 비활성)"""
    return {"status": "cold_start", "note": "ML disabled, using raw signals"}


@app.post("/api/ml/history-learn")
async def trigger_history_learn():
    """역사 학습 (TradeEngine — 현재 비활성)"""
    return {"status": "disabled", "note": "ML cold start, historical learning disabled"}


@app.get("/api/ml/history")
async def ml_history():
    """ML 학습 결과 내역"""
    swing, scalp = _get_ml_instances()

    def parse_result(r):
        if isinstance(r, dict):
            return r.get("pnl_pct", 0), r.get("timestamp", 0)
        return r, 0

    swing_trades = []
    for i, r in enumerate(list(swing.recent_results)):
        pnl, ts = parse_result(r)
        swing_trades.append({
            "id": i + 1, "mode": "swing",
            "pnl_pct": round(pnl, 3),
            "result": "WIN" if pnl > 0 else "LOSS",
            "timestamp": ts,
        })

    scalp_trades = []
    for i, r in enumerate(list(scalp.recent_results)):
        pnl, ts = parse_result(r)
        scalp_trades.append({
            "id": i + 1, "mode": "scalp",
            "pnl_pct": round(pnl, 3),
            "result": "WIN" if pnl > 0 else "LOSS",
            "timestamp": ts,
        })

    return {
        "swing_trades": swing_trades[-50:],  # 최근 50개
        "scalp_trades": scalp_trades[-50:],
        "swing_total": len(swing_trades),
        "scalp_total": len(scalp_trades),
    }
