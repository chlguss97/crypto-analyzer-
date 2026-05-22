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
    title="GridBot v3",
    version="3.0.0",
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
    """레짐 시그널 상태"""
    signals = await redis.hgetall("regime:signals")
    if not signals:
        signals = {"obi": "0", "cvd": "0", "vol": "0", "cusum": "0", "crs": "0", "mode": "ACTIVE"}
    return signals


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
    funding = await redis.get("rt:funding:BTC-USDT-SWAP")
    oi = await redis.get("rt:oi:BTC-USDT-SWAP")

    return {
        "ticker": ticker or {},
        "open_interest": float(oi) if oi else None,
        "funding_rate": float(funding) if funding else None,
        "long_short_ratio": None,
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


@app.get("/api/risk/state")
async def get_risk_state():
    await _ensure_initialized()
    """리스크 상태 (모니터링용)"""
    daily = await redis.get("risk:daily_pnl") or "0"
    streak = await redis.get("risk:streak") or "0"
    balance = await redis.get("sys:balance") or "0"
    return {
        "daily_pnl_pct": float(daily),
        "streak": int(streak),
        "balance": float(balance),
    }


@app.get("/api/engine/state")
async def get_engine_state():
    """그리드 엔진 + 레짐 실시간 상태"""
    await _ensure_initialized()
    mode = await redis.get("regime:mode") or "ACTIVE"
    crs = await redis.get("regime:crs") or "0"
    signals = await redis.hgetall("regime:signals")
    return {
        "mode": mode,
        "crs": float(crs),
        "signals": signals or {},
    }


@app.get("/api/regime")
async def get_regime():
    """현재 레짐 모드 + CRS"""
    await _ensure_initialized()
    mode = await redis.get("regime:mode") or "ACTIVE"
    crs = await redis.get("regime:crs") or "0"
    signals = await redis.hgetall("regime:signals")
    return {
        "mode": mode,
        "crs": float(crs),
        "signals": signals or {},
    }


@app.get("/api/engine/overview")
async def get_engine_overview():
    """그리드 통합 데이터 — 레짐 + 사이클 통계"""
    await _ensure_initialized()

    # 1) regime
    mode = await redis.get("regime:mode") or "ACTIVE"
    crs = await redis.get("regime:crs") or "0"

    # 2) grid trade stats (grid_trades 테이블에 status 컬럼 없음)
    grid_stats = {"total_cycles": 0, "total_pnl": 0.0, "avg_pnl": 0.0}
    try:
        cur = await db._db.execute(
            """SELECT COUNT(*) as total,
                      COALESCE(SUM(pnl_usdt), 0) as total_pnl,
                      COALESCE(AVG(pnl_usdt), 0) as avg_pnl
               FROM grid_trades"""
        )
        row = dict(await cur.fetchone())
        grid_stats["total_cycles"] = row["total"] or 0
        grid_stats["total_pnl"] = round(row["total_pnl"] or 0, 2)
        grid_stats["avg_pnl"] = round(row["avg_pnl"] or 0, 4)
    except Exception as e:
        logger.debug(f"grid trade stats 집계 실패: {e}")

    return {
        "regime": {"mode": mode, "crs": float(crs)},
        "grid_trades": grid_stats,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


