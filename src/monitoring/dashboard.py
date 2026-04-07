import asyncio
import json
import logging
import time as _time
from pathlib import Path
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional

from src.data.storage import Database, RedisClient
from src.data.candle_collector import CandleCollector
from src.trading.executor import OrderExecutor
from src.utils.helpers import load_config, load_env

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="CryptoAnalyzer v1.0", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def index():
    """메인 대시보드 페이지"""
    return FileResponse(STATIC_DIR / "index.html")

# 전역 인스턴스 (uvicorn 시작 시 초기화)
load_env()
db = Database()
redis = RedisClient()
config = load_config()
collector: CandleCollector | None = None
executor: OrderExecutor | None = None
_bg_task = None


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


async def _candle_loop():
    """백그라운드 캔들 수집 (30초마다)"""
    global collector
    collector = CandleCollector(db)
    try:
        await collector.init_exchange()
        logger.info("실시간 캔들 수집 시작")
    except Exception as e:
        logger.error(f"캔들 수집기 초기화 실패: {e}")
        return

    while True:
        try:
            for tf in ["15m", "1h", "4h"]:
                candles = await collector.fetch_candles(tf, limit=5)
                if candles:
                    await db.insert_candles(collector.symbol, tf, candles)

            # 마켓 데이터도 같이 캐싱
            await _update_market_cache()
        except Exception as e:
            logger.error(f"캔들 갱신 에러: {e}")
        await asyncio.sleep(30)


# 마켓 데이터 메모리 캐시 (Redis 없이도 동작)
_market_cache = {}


async def _update_market_cache():
    """OKX에서 마켓 데이터 가져와서 캐시"""
    global _market_cache
    if not collector or not collector.exchange:
        return
    try:
        symbol = config["exchange"]["symbol"]
        t = await collector.exchange.fetch_ticker(symbol)
        _market_cache["ticker"] = {
            "last": str(t.get("last", 0)),
            "bid": str(t.get("bid", 0)),
            "ask": str(t.get("ask", 0)),
            "high24h": str(t.get("high", 0)),
            "low24h": str(t.get("low", 0)),
            "vol24h": str(t.get("quoteVolume", 0)),
        }
    except Exception as e:
        logger.debug(f"마켓 데이터 캐시 실패: {e}")


async def _signal_loop():
    """백그라운드 시그널 분석 (60초마다)"""
    import pandas as pd
    from src.engine.fast.ema import EMAIndicator
    from src.engine.fast.rsi import RSIIndicator
    from src.engine.fast.bollinger import BollingerIndicator
    from src.engine.fast.vwap import VWAPIndicator
    from src.engine.fast.market_structure import MarketStructureIndicator
    from src.engine.fast.atr import ATRIndicator
    from src.engine.fast.fractal import FractalIndicator
    from src.engine.slow.order_block import OrderBlockIndicator
    from src.engine.slow.fvg import FVGIndicator
    from src.engine.slow.volume_pattern import VolumePatternIndicator
    from src.engine.slow.funding_rate import FundingRateIndicator
    from src.engine.slow.open_interest import OpenInterestIndicator
    from src.engine.slow.liquidation import LiquidationIndicator
    from src.engine.slow.long_short_ratio import LongShortRatioIndicator
    from src.engine.slow.cvd import CVDIndicator
    from src.engine.base import BaseIndicator
    from src.signal_engine.aggregator import SignalAggregator
    from src.signal_engine.grader import SignalGrader

    fast_engines = [
        EMAIndicator(), RSIIndicator(), BollingerIndicator(),
        VWAPIndicator(), MarketStructureIndicator(), ATRIndicator(),
        FractalIndicator(),
    ]
    slow_engines = [
        OrderBlockIndicator(), FVGIndicator(), VolumePatternIndicator(),
        FundingRateIndicator(), OpenInterestIndicator(),
        LiquidationIndicator(), LongShortRatioIndicator(), CVDIndicator(),
    ]
    aggregator = SignalAggregator()
    grader = SignalGrader()

    await asyncio.sleep(10)  # 캔들 수집 대기

    while True:
        try:
            symbol = config["exchange"]["symbol"]
            candles_raw = await db.get_candles(symbol, "15m", limit=300)
            if not candles_raw or len(candles_raw) < 50:
                await asyncio.sleep(30)
                continue

            df = BaseIndicator.to_dataframe(candles_raw)

            # 1H 추세
            candles_1h = await db.get_candles(symbol, "1h", limit=100)
            htf_trend = "unknown"
            if candles_1h and len(candles_1h) >= 20:
                df_1h = BaseIndicator.to_dataframe(candles_1h)
                ms = MarketStructureIndicator()
                htf_result = await ms.calculate(df_1h)
                htf_trend = htf_result.get("trend", "unknown")

            context = {"htf_trend": htf_trend}

            # Fast Path
            fast_signals = {}
            for engine in fast_engines:
                try:
                    result = await engine.calculate(df, context)
                    fast_signals[result["type"]] = result
                    if result["type"] == "bollinger":
                        context["bb_position"] = result["bb_position"]
                except Exception:
                    pass

            # Slow Path
            slow_context = {
                "funding_rate": 0, "funding_next_min": 999,
                "oi_current": 0, "oi_history": [],
                "ls_ratio_account": 1.0, "ls_history": [],
                "cvd_15m": 0, "cvd_1h": 0, "funding_history": [],
            }
            slow_signals = {}
            for engine in slow_engines:
                try:
                    result = await engine.calculate(df, slow_context)
                    slow_signals[result["type"]] = result
                    if result["type"] == "order_block" and result.get("ob_zone"):
                        slow_context["ob_zones"] = [result["ob_zone"]]
                    if result["type"] == "open_interest":
                        slow_context["oi_spike"] = result.get("oi_spike", False)
                except Exception:
                    pass

            # 합산 + 등급
            aggregated = aggregator.aggregate(fast_signals, slow_signals)
            grade_result = grader.grade(aggregated, {
                "daily_pnl_pct": 0, "current_drawdown_pct": 0,
                "open_positions": 0, "same_direction_count": 0,
                "streak": 0, "cooldown_active": False,
                "funding_blackout": False, "has_same_symbol": False,
            })

            # Redis에 저장 (또는 메모리 캐시)
            await redis.set(f"sig:fast:{symbol}", fast_signals, ttl=120)
            await redis.set(f"sig:slow:{symbol}", slow_signals, ttl=120)
            await redis.set(f"sig:aggregated:{symbol}", {
                "aggregated": aggregated, "grade": grade_result,
            }, ttl=120)

            # 메모리에도 보관 (Redis 없을 때용)
            _market_cache["fast_signals"] = fast_signals
            _market_cache["slow_signals"] = slow_signals
            _market_cache["aggregated"] = {"aggregated": aggregated, "grade": grade_result}

            logger.info(
                f"시그널 분석: {aggregated['direction'].upper()} "
                f"점수 {aggregated['score']:.1f} 등급 {grade_result['grade']}"
            )

        except Exception as e:
            logger.error(f"시그널 분석 에러: {e}")

        await asyncio.sleep(60)  # 60초마다 갱신


@app.on_event("startup")
async def startup():
    global _bg_task, executor
    await db.connect()
    await redis.connect()
    # 캔들/시그널 루프는 main.py에서 실행 — 대시보드는 조회만
    # _bg_task = asyncio.create_task(_candle_loop())
    # asyncio.create_task(_signal_loop())

    # 매매 실행기 초기화 (API 키 있을 때만)
    try:
        executor = OrderExecutor()
        await executor.initialize()
        logger.info("OrderExecutor 초기화 완료 (수동매매 가능)")
    except Exception as e:
        logger.warning(f"OrderExecutor 초기화 실패 (수동매매 불가): {e}")
        executor = None

    logger.info("Dashboard 시작")


@app.on_event("shutdown")
async def shutdown():
    if _bg_task:
        _bg_task.cancel()
    if collector:
        await collector.close()
    if executor:
        await executor.close()
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

    # Redis 없으면 메모리 캐시
    if not fast:
        fast = _market_cache.get("fast_signals")
    if not slow:
        slow = _market_cache.get("slow_signals")
    if not aggregated:
        aggregated = _market_cache.get("aggregated")

    return {
        "fast_signals": fast,
        "slow_signals": slow,
        "aggregated": aggregated,
    }


@app.get("/api/trades")
async def get_trades(days: int = 7, mode: str = "all"):
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
    """일일 성과 요약"""
    cursor = await db._db.execute(
        """SELECT * FROM daily_summary
           ORDER BY date DESC LIMIT 30"""
    )
    rows = await cursor.fetchall()
    return {"summaries": [dict(r) for r in rows]}


@app.get("/api/paper/stats")
async def get_paper_stats():
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
    """실시간 시장 데이터 (Redis → OKX 직접 폴백)"""
    # Redis에서 먼저 시도
    ticker = await redis.hgetall("rt:ticker:BTC-USDT-SWAP")
    oi = await redis.get("rt:oi:BTC-USDT-SWAP")
    funding = await redis.get("rt:funding:BTC-USDT-SWAP")
    ls_ratio = await redis.get("rt:ls_ratio:BTC-USDT-SWAP")

    # Redis 데이터 없으면 메모리 캐시에서
    if not ticker:
        ticker = _market_cache.get("ticker", {})

    return {
        "ticker": ticker,
        "open_interest": float(oi) if oi else None,
        "funding_rate": float(funding) if funding else None,
        "long_short_ratio": float(ls_ratio) if ls_ratio else None,
    }


# ── 매매 엔드포인트 ──

@app.post("/api/trade/open")
async def manual_open(req: ManualOrderRequest):
    """수동 매매 진입"""
    if not executor:
        raise HTTPException(400, "OrderExecutor 미초기화 (API 키 확인)")

    symbol = config["exchange"]["symbol"]
    try:
        # 레버리지 설정
        await executor.set_leverage(req.leverage, req.direction)

        # 수량 계산
        positions = await executor.get_positions()
        balance = await executor.get_balance()

        # 현재가 조회
        ticker = await executor.exchange.fetch_ticker(symbol)
        current_price = ticker["last"]

        size_usdt = req.margin_usdt * req.leverage
        size_btc = size_usdt / current_price

        # 주문 실행
        side = "buy" if req.direction == "long" else "sell"
        pos_side = req.direction

        if req.order_type == "limit" and req.limit_price:
            order = await executor.exchange.create_order(
                symbol=symbol, type="limit", side=side,
                amount=size_btc, price=req.limit_price,
                params={"tdMode": "isolated", "posSide": pos_side},
            )
        else:
            order = await executor.exchange.create_order(
                symbol=symbol, type="market", side=side,
                amount=size_btc,
                params={"tdMode": "isolated", "posSide": pos_side},
            )

        fill_price = order.get("average") or order.get("price") or current_price

        # SL 설정
        if req.sl_price:
            await executor._set_stop_loss(req.direction, size_btc, req.sl_price)

        # DB 기록
        await db.insert_trade({
            "symbol": symbol,
            "direction": req.direction,
            "grade": "MANUAL",
            "score": 0,
            "entry_price": fill_price,
            "entry_time": int(_time.time() * 1000),
            "leverage": req.leverage,
            "position_size": size_usdt,
            "signals_snapshot": "{}",
        })

        logger.info(f"수동 매매: {req.direction.upper()} ${req.margin_usdt} x{req.leverage} @ ${fill_price}")

        return {
            "success": True,
            "order_id": order.get("id"),
            "direction": req.direction,
            "fill_price": fill_price,
            "size_btc": round(size_btc, 6),
            "size_usdt": round(size_usdt, 2),
            "leverage": req.leverage,
            "sl_price": req.sl_price,
        }

    except Exception as e:
        logger.error(f"수동 매매 실패: {e}")
        raise HTTPException(500, str(e))


@app.post("/api/trade/close")
async def manual_close(req: CloseRequest):
    """수동 포지션 청산"""
    if not executor:
        raise HTTPException(400, "OrderExecutor 미초기화")

    symbol = config["exchange"]["symbol"]
    try:
        positions = await executor.get_positions()
        target = None
        for p in positions:
            if p["direction"] == req.direction:
                target = p
                break

        if not target:
            raise HTTPException(404, f"{req.direction} 포지션 없음")

        close_size = target["size"] * req.close_pct
        order = await executor.close_position(req.direction, close_size, "manual_web")

        fill_price = order.get("average") or order.get("price") or 0 if order else 0
        logger.info(f"수동 청산: {req.direction.upper()} {req.close_pct*100:.0f}% @ ${fill_price}")

        return {
            "success": True,
            "direction": req.direction,
            "close_pct": req.close_pct,
            "fill_price": fill_price,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"수동 청산 실패: {e}")
        raise HTTPException(500, str(e))


@app.get("/api/trade/positions")
async def get_live_positions():
    """거래소 실시간 포지션 조회"""
    if not executor:
        return {"positions": [], "balance": 0}

    try:
        positions = await executor.get_positions()
        balance = await executor.get_balance()
        return {"positions": positions, "balance": round(balance, 2)}
    except Exception as e:
        logger.error(f"포지션 조회 실패: {e}")
        return {"positions": [], "balance": 0, "error": str(e)}


@app.post("/api/autotrading")
async def toggle_autotrading():
    """자동매매 ON/OFF 토글"""
    current = await redis.get("sys:autotrading") or "off"
    new_state = "off" if current == "on" else "on"
    await redis.set("sys:autotrading", new_state)
    logger.info(f"자동매매: {new_state.upper()}")
    return {"autotrading": new_state}


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
    """수동 백테스트 실행"""
    from src.strategy.auto_backtest import AutoBacktest
    swing, scalp = _get_ml_instances()
    bt = AutoBacktest(db, swing, scalp)
    result = await bt.run(days=30)
    await redis.set("sys:last_backtest", result, ttl=86400)
    return result


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
    """스캘핑 실시간 상태"""
    state = await redis.get_json("sys:scalp_state")
    if not state:
        state = {"daily_pnl": 0, "streak": 0, "cooldown": False,
                 "score": 0, "direction": "neutral", "explosive": False,
                 "smc": False, "session": "unknown"}
    return state


@app.get("/api/regime")
async def get_regime():
    """현재 마켓 레짐 조회"""
    regime_detail = await redis.get_json("sys:regime_detail")
    regime = await redis.get("sys:regime") or "ranging"

    if not regime_detail:
        regime_detail = {"regime": regime, "confidence": 0, "scores": {}}

    return regime_detail


@app.get("/api/ml/status")
async def ml_status():
    """ML 모델 상태 조회"""
    swing, scalp = _get_ml_instances()
    regime = await redis.get("sys:regime") or "ranging"

    return {
        "swing": swing.get_stats(),
        "scalp": scalp.get_stats(),
        "active_model": await redis.get("sys:active_model") or "both",
        "ml_enabled": (await redis.get("sys:ml_enabled") or "on") == "on",
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
    model: str  # 'swing' | 'scalp' | 'both'

@app.post("/api/ml/model")
async def select_model(req: ModelSelectRequest):
    """활성 모델 선택"""
    if req.model not in ("swing", "scalp", "both"):
        raise HTTPException(400, "model must be swing, scalp, or both")
    await redis.set("sys:active_model", req.model)
    logger.info(f"활성 모델: {req.model.upper()}")
    return {"active_model": req.model}


@app.post("/api/ml/retrain")
async def retrain_ml():
    """ML 수동 재학습 트리거"""
    swing, scalp = _get_ml_instances()
    _ml_cache["loaded_at"] = 0  # 강제 리로드

    result = {}
    if len(swing.X_buffer) >= swing.min_trades_to_train:
        swing.train()
        result["swing"] = f"Trained with {len(swing.X_buffer)} samples"
    else:
        result["swing"] = f"Not enough data ({len(swing.X_buffer)}/{swing.min_trades_to_train})"

    if len(scalp.X_buffer) >= scalp.min_trades_to_train:
        scalp.train()
        result["scalp"] = f"Trained with {len(scalp.X_buffer)} samples"
    else:
        result["scalp"] = f"Not enough data ({len(scalp.X_buffer)}/{scalp.min_trades_to_train})"

    return result


@app.post("/api/ml/history-learn")
async def trigger_history_learn():
    """수동 역사 백필 학습 트리거"""
    swing, scalp = _get_ml_instances()
    _ml_cache["loaded_at"] = 0

    from src.strategy.historical_learner import HistoricalLearner
    learner = HistoricalLearner(db, swing, scalp)
    stats = await learner.run_backfill("15m", lookback=2000, step=5)

    return {
        "total_learned": stats["total"],
        "wins": stats["wins"],
        "losses": stats["losses"],
        "win_rate": stats["wins"] / max(stats["total"], 1) * 100,
        "swing_buffer": len(swing.X_buffer),
        "scalp_buffer": len(scalp.X_buffer),
    }


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
