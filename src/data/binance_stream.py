"""
Binance BTCUSDT 실시간 데이터 스트림 — OKX 보완용 (인증 불필요).

수집 항목:
  1. aggTrades → CVD (OKX + Binance 합산 = 시장 전체 플로우)
  2. aggTrades → 대형 체결 감지 ($50k+)
  3. ticker → Binance 가격 (OKX 대비 프리미엄 추적)

Redis 키:
  bn:cvd:5m:BTCUSDT          — Binance 5분 CVD (진행 중)
  bn:cvd:15m:BTCUSDT         — Binance 15분 CVD (진행 중)
  bn:cvd:1h:BTCUSDT          — Binance 1시간 CVD (진행 중)
  bn:whale:BTCUSDT           — 최근 대형 체결 리스트 (JSON)
  bn:price:BTCUSDT           — Binance 현재가
  flow:combined:cvd_5m       — OKX + Binance 합산 CVD 5분
  flow:combined:cvd_15m      — OKX + Binance 합산 CVD 15분
  flow:combined:cvd_1h       — OKX + Binance 합산 CVD 1시간
  flow:combined:whale_bias   — 대형 체결 방향 편향 (-1~+1)
"""

import asyncio
import json
import logging
import time
import websockets
from collections import deque
from src.data.storage import RedisClient

logger = logging.getLogger(__name__)

BINANCE_WS = "wss://fstream.binance.com/ws"
SYMBOL = "btcusdt"
WHALE_THRESHOLD_USD = 50_000  # $50k 이상 = 대형 체결
WHALE_WINDOW_SEC = 300        # 최근 5분간 대형 체결 추적


class BinanceStream:
    """Binance Futures BTCUSDT WebSocket — CVD + 대형 체결 + 가격 + 실시간 캔들"""

    def __init__(self, redis_client: RedisClient, db=None):
        self.redis = redis_client
        self.db = db  # DB 직접 저장 (REST 폴링 대체)
        self._running = False
        self._reconnect_count = 0

        # CVD 누적 (5m / 15m / 1h)
        self._cvd_5m = 0.0
        self._cvd_15m = 0.0
        self._cvd_1h = 0.0
        self._cvd_reset_5m = 0
        self._cvd_reset_15m = 0
        self._cvd_reset_1h = 0

        # 대형 체결 추적
        self._whales: deque = deque(maxlen=200)  # (ts, side, size_usd, price)

        # 통계
        self._trade_count = 0
        self._last_log = 0

        # DB 저장 심볼 — config와 일치
        from src.utils.helpers import load_config
        self._db_symbol = load_config().get("exchange", {}).get("symbol", "BTC/USDT:USDT")

    async def start(self):
        """WebSocket 연결 시작 (무한 재시도)"""
        self._running = True
        self._reconnect_count = 0

        # 10 스트림: aggTrades + miniTicker + 캔들 7종 + 강제 청산
        streams = [
            f"{SYMBOL}@aggTrade",
            f"{SYMBOL}@miniTicker",
            f"{SYMBOL}@kline_1m",
            f"{SYMBOL}@kline_5m",
            f"{SYMBOL}@kline_15m",
            f"{SYMBOL}@kline_1h",
            f"{SYMBOL}@kline_4h",
            f"{SYMBOL}@kline_1d",
            f"{SYMBOL}@kline_1w",
            f"{SYMBOL}@forceOrder",
        ]
        url = f"{BINANCE_WS}/{'/'.join(streams)}"

        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    self._reconnect_count = 0
                    logger.info(f"Binance WS 연결 성공: {SYMBOL}")
                    async for message in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(message)
                            await self._handle(data)
                        except json.JSONDecodeError:
                            continue
                        except Exception as e:
                            logger.debug(f"Binance WS 처리 에러: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._reconnect_count += 1
                wait = min(5 * min(self._reconnect_count, 12), 60)
                logger.warning(f"Binance WS 끊김: {e} → {wait}초 후 재연결")
                await asyncio.sleep(wait)

    def stop(self):
        self._running = False

    async def _handle(self, data: dict):
        event = data.get("e", "")
        if event == "aggTrade":
            await self._on_agg_trade(data)
        elif event == "24hrMiniTicker":
            await self._on_ticker(data)
        elif event == "kline":
            await self._on_kline(data)
        elif event == "forceOrder":
            await self._on_liquidation(data)

    async def _on_agg_trade(self, t: dict):
        """체결 → CVD 누적 + 대형 체결 감지"""
        price = float(t.get("p", 0))
        qty = float(t.get("q", 0))
        is_buyer_maker = t.get("m", False)  # True = 매도 체결 (seller aggressor)
        ts = int(t.get("T", 0))

        if price <= 0 or qty <= 0:
            return

        size_usd = price * qty
        # Binance: m=True → seller is maker → buyer aggressed → "buy" volume
        # m=False → buyer is maker → seller aggressed → "sell" volume
        # NOTE: Binance aggTrade m 필드는 OKX와 반대! m=True = taker sell
        side = "sell" if is_buyer_maker else "buy"
        delta = qty if side == "buy" else -qty

        # CVD 누적
        MAX_CVD = 1e9
        self._cvd_5m = max(-MAX_CVD, min(MAX_CVD, self._cvd_5m + delta))
        self._cvd_15m = max(-MAX_CVD, min(MAX_CVD, self._cvd_15m + delta))
        self._cvd_1h = max(-MAX_CVD, min(MAX_CVD, self._cvd_1h + delta))

        # Redis 저장 (매 체결마다는 과부하 → 100체결마다 or 대형 체결 시)
        self._trade_count += 1
        flush = self._trade_count % 100 == 0 or size_usd >= WHALE_THRESHOLD_USD

        if flush:
            await self.redis.set("bn:cvd:5m:BTCUSDT", str(round(self._cvd_5m, 4)), ttl=400)
            await self.redis.set("bn:cvd:15m:BTCUSDT", str(round(self._cvd_15m, 4)), ttl=1200)
            await self.redis.set("bn:cvd:1h:BTCUSDT", str(round(self._cvd_1h, 4)), ttl=4800)

            # 합산 CVD 계산 (OKX + Binance)
            await self._update_combined_cvd()

        # 대형 체결 감지
        if size_usd >= WHALE_THRESHOLD_USD:
            now = time.time()
            self._whales.append((now, side, round(size_usd), round(price, 1)))
            logger.info(f"🐋 Binance 대형 체결: {side.upper()} ${size_usd:,.0f} @ ${price:,.1f}")

            # 오래된 거 정리 (5분 초과)
            while self._whales and self._whales[0][0] < now - WHALE_WINDOW_SEC:
                self._whales.popleft()

            # 대형 체결 방향 편향 계산
            buy_vol = sum(s for _, sd, s, _ in self._whales if sd == "buy")
            sell_vol = sum(s for _, sd, s, _ in self._whales if sd == "sell")
            total = buy_vol + sell_vol
            whale_bias = (buy_vol - sell_vol) / total if total > 0 else 0
            # -1 (숏 압도) ~ +1 (롱 압도)

            await self.redis.set("flow:combined:whale_bias", str(round(whale_bias, 3)), ttl=600)
            await self.redis.set("bn:whale:BTCUSDT", json.dumps({
                "count": len(self._whales),
                "buy_vol": buy_vol,
                "sell_vol": sell_vol,
                "bias": round(whale_bias, 3),
                "recent": list(self._whales)[-10:],  # 최근 10건
            }), ttl=600)

        # CVD 윈도우 리셋
        now_sec = int(time.time())
        if now_sec // 300 != self._cvd_reset_5m:
            self._cvd_reset_5m = now_sec // 300
            self._cvd_5m = 0.0
        if now_sec // 900 != self._cvd_reset_15m:
            self._cvd_reset_15m = now_sec // 900
            self._cvd_15m = 0.0
        if now_sec // 3600 != self._cvd_reset_1h:
            self._cvd_reset_1h = now_sec // 3600
            self._cvd_1h = 0.0

        # 5분마다 로그
        if now_sec - self._last_log >= 300:
            self._last_log = now_sec
            logger.info(
                f"Binance CVD: 5m={self._cvd_5m:+.2f} 15m={self._cvd_15m:+.2f} "
                f"1h={self._cvd_1h:+.2f} | whales={len(self._whales)}"
            )

    async def _on_ticker(self, t: dict):
        """미니티커 → Binance 가격 + OKX 대비 프리미엄"""
        price = float(t.get("c", 0))
        if price > 0:
            await self.redis.set("bn:price:BTCUSDT", str(price), ttl=30)

            # OKX 가격과 비교 → 프리미엄 계산
            okx_price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
            if okx_price_str:
                okx_price = float(okx_price_str)
                if okx_price > 0:
                    premium_pct = (price - okx_price) / okx_price * 100
                    await self.redis.set("flow:okx_bn_premium", str(round(premium_pct, 4)), ttl=60)

    async def _on_kline(self, data: dict):
        """
        Binance kline → DB 직접 저장 (REST 폴링 완전 대체).
        매 틱마다 오는 진행 중 캔들 + 확정 캔들 모두 저장.
        확정(is_closed=True) 시 즉시 DB upsert → 지연 0.
        """
        k = data.get("k", {})
        if not k:
            return

        interval = k.get("i", "")  # "1m", "5m", "15m", "1h"
        is_closed = k.get("x", False)  # 캔들 확정 여부

        candle = {
            "timestamp": int(k.get("t", 0)),  # 캔들 시작 시간
            "open": float(k.get("o", 0)),
            "high": float(k.get("h", 0)),
            "low": float(k.get("l", 0)),
            "close": float(k.get("c", 0)),
            "volume": float(k.get("v", 0)),
        }

        if candle["timestamp"] <= 0 or candle["close"] <= 0:
            return

        # Redis에 현재 진행 중 캔들 캐시 (실시간 가격 참조용)
        await self.redis.set(
            f"bn:kline:{interval}:BTCUSDT",
            json.dumps(candle),
            ttl={"1m": 120, "5m": 600, "15m": 1800, "1h": 7200}.get(interval, 300),
        )

        # DB 저장 — 확정 캔들은 즉시, 진행 중은 5초마다 (DB 부하 줄이기)
        if self.db:
            should_save = is_closed
            if not is_closed:
                # 진행 중 캔들: 1m은 매번, 나머지는 5초마다
                cache_key = f"_kline_last_save_{interval}"
                last = getattr(self, cache_key, 0)
                now = time.time()
                if interval == "1m" or now - last >= 5:
                    should_save = True
                    setattr(self, cache_key, now)

            if should_save:
                try:
                    await self.db.insert_candles(self._db_symbol, interval, [candle])
                except Exception as e:
                    logger.debug(f"Binance kline DB 저장 실패 ({interval}): {e}")

        if is_closed:
            logger.debug(f"Binance {interval} 캔들 확정: ${candle['close']:,.1f} vol={candle['volume']:.2f}")
            # 캔들 확정 → Redis 이벤트 발행 → 평가 루프 즉시 트리거
            try:
                await self.redis.publish("ch:kline:ready", json.dumps({
                    "tf": interval, "close": candle["close"], "ts": candle["timestamp"],
                }))
            except Exception:
                pass

    async def _on_liquidation(self, data: dict):
        """강제 청산 감지 — 대량 청산 = 변동성 폭발 선행 시그널.
        1분 내 청산 $1M+ 누적 시 flow:liquidation_surge 이벤트.
        """
        o = data.get("o", {})
        side = o.get("S", "")  # BUY(숏 청산) or SELL(롱 청산)
        price = float(o.get("p", 0))
        qty = float(o.get("q", 0))
        if price <= 0 or qty <= 0:
            return

        size_usd = price * qty
        now = time.time()

        # 1분 윈도우 청산 누적
        liq_key = "_liq_window"
        if not hasattr(self, liq_key):
            setattr(self, liq_key, deque(maxlen=500))
        window = getattr(self, liq_key)
        window.append((now, side, size_usd))

        # 1분 초과 제거
        while window and window[0][0] < now - 60:
            window.popleft()

        # 1분간 합산
        long_liq = sum(s for _, sd, s in window if sd == "SELL")   # 롱 청산
        short_liq = sum(s for _, sd, s in window if sd == "BUY")   # 숏 청산
        total_liq = long_liq + short_liq

        # Redis에 저장
        await self.redis.set("flow:liq:1m_total", str(round(total_liq)), ttl=120)
        await self.redis.set("flow:liq:1m_long", str(round(long_liq)), ttl=120)
        await self.redis.set("flow:liq:1m_short", str(round(short_liq)), ttl=120)

        # $500k+ 누적 = 변동성 폭발 임박
        if total_liq >= 500_000:
            # 어느 쪽이 더 많이 청산되는지 = 반대 방향이 강함
            bias = "long" if short_liq > long_liq else "short"  # 숏 청산 많으면 롱 강세
            await self.redis.set("flow:liq:surge", json.dumps({
                "total": round(total_liq),
                "long_liq": round(long_liq),
                "short_liq": round(short_liq),
                "bias": bias,
                "ts": now,
            }), ttl=120)
            logger.warning(
                f"💥 청산 폭발: 1분간 ${total_liq:,.0f} "
                f"(롱청산 ${long_liq:,.0f} / 숏청산 ${short_liq:,.0f}) → {bias.upper()} 강세"
            )

    async def _update_combined_cvd(self):
        """OKX + Binance CVD 합산 → Redis"""
        try:
            # OKX CVD 읽기
            okx_15m = float(await self.redis.get("cvd:15m:current:BTC-USDT-SWAP") or 0)
            okx_1h = float(await self.redis.get("cvd:1h:current:BTC-USDT-SWAP") or 0)

            # 합산 (Binance 가중치 높게 — 거래량 2~3배)
            combined_5m = self._cvd_5m * 1.0  # Binance만 (OKX에 5m CVD 없음)
            combined_15m = okx_15m + self._cvd_15m
            combined_1h = okx_1h + self._cvd_1h

            await self.redis.set("flow:combined:cvd_5m", str(round(combined_5m, 4)), ttl=400)
            await self.redis.set("flow:combined:cvd_15m", str(round(combined_15m, 4)), ttl=1200)
            await self.redis.set("flow:combined:cvd_1h", str(round(combined_1h, 4)), ttl=4800)
        except Exception as e:
            logger.debug(f"합산 CVD 계산 실패: {e}")
