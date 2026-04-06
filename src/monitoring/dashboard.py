import json
import logging
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from src.data.storage import Database, RedisClient
from src.utils.helpers import load_config

logger = logging.getLogger(__name__)

app = FastAPI(title="CryptoAnalyzer v1.0", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 전역 인스턴스 (uvicorn 시작 시 초기화)
db = Database()
redis = RedisClient()
config = load_config()


@app.on_event("startup")
async def startup():
    await db.connect()
    await redis.connect()
    logger.info("Dashboard 시작")


@app.on_event("shutdown")
async def shutdown():
    await db.close()
    await redis.close()


# ── GET 엔드포인트 ──

@app.get("/api/status")
async def get_status():
    """봇 상태 조회"""
    status = await redis.get("sys:bot_status") or "unknown"
    heartbeat = await redis.get("sys:last_heartbeat") or "0"

    # 리스크 상태
    streak = await redis.get("risk:streak") or "0"
    daily_pnl = await redis.get("risk:daily_pnl") or "0"

    return {
        "status": status,
        "last_heartbeat": heartbeat,
        "streak": int(streak),
        "daily_pnl_pct": float(daily_pnl),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/position")
async def get_position():
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
async def get_trades(days: int = 7):
    """최근 매매 내역"""
    import time
    since = int((time.time() - days * 86400) * 1000)

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
        # signals_snapshot은 큰 데이터이므로 요약만
        if trade.get("signals_snapshot"):
            trade["signals_snapshot"] = "(생략)"
        trades.append(trade)

    return {"trades": trades, "count": len(trades)}


@app.get("/api/daily-summary")
async def get_daily_summary():
    """일일 성과 요약"""
    cursor = await db._db.execute(
        """SELECT * FROM daily_summary
           ORDER BY date DESC LIMIT 30"""
    )
    rows = await cursor.fetchall()
    return {"summaries": [dict(r) for r in rows]}


@app.get("/api/equity-curve")
async def get_equity_curve():
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
    """봇 일시정지"""
    await redis.set("sys:bot_status", "paused")
    logger.warning("봇 일시정지 (대시보드)")
    return {"status": "paused"}


@app.post("/api/resume")
async def resume_bot():
    """봇 재개"""
    await redis.set("sys:bot_status", "running")
    logger.info("봇 재개 (대시보드)")
    return {"status": "running"}


@app.post("/api/close-all")
async def close_all():
    """전 포지션 청산 (킬 스위치)"""
    await redis.set("sys:bot_status", "stopped")
    # 실제 청산은 main.py의 position_manager가 Redis 상태 변경 감지 후 처리
    # 여기서는 상태 변경만
    logger.warning("킬 스위치 작동 (대시보드)")
    return {"status": "stopped", "message": "전 포지션 청산 요청됨"}


# ── 실시간 데이터 ──

@app.get("/api/market")
async def get_market():
    """실시간 시장 데이터"""
    ticker = await redis.hgetall("rt:ticker:BTC-USDT-SWAP")
    oi = await redis.get("rt:oi:BTC-USDT-SWAP")
    funding = await redis.get("rt:funding:BTC-USDT-SWAP")
    ls_ratio = await redis.get("rt:ls_ratio:BTC-USDT-SWAP")

    return {
        "ticker": ticker,
        "open_interest": float(oi) if oi else None,
        "funding_rate": float(funding) if funding else None,
        "long_short_ratio": float(ls_ratio) if ls_ratio else None,
    }
