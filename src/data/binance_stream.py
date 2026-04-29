"""
Binance BTCUSDT 실시간 데이터 스트림 + 마이크로스트럭처 집계.

수집 항목:
  1. aggTrades → CVD + 마이크로스트럭처 15종 피처
  2. aggTrades → 대형 체결 감지 ($50k+)
  3. ticker → Binance 가격
  4. kline → DB 직접 저장

마이크로스트럭처 Redis 키 (rt:micro:*):
  rt:micro:trade_rate        — 10초 체결 건수/초
  rt:micro:bs_ratio_5s       — 5초 buy ratio (0~1)
  rt:micro:bs_ratio_30s      — 30초 buy ratio
  rt:micro:bs_ratio_60s      — 60초 buy ratio
  rt:micro:absorption        — 흡수 스코어 (JSON: score, direction)
  rt:micro:whale_cluster     — 고래 클러스터 (JSON: score, direction, count)
  rt:micro:delta_accel       — CVD 가속도 (-2~+2)
  rt:micro:trade_burst       — 체결 폭발 비율 (1.0=평소)
  rt:micro:price_impact      — 가격 충격 계수
  rt:micro:vwap              — VWAP + deviation (JSON)
  rt:micro:delta_div         — 델타 다이버전스 (-1/0/+1)
  rt:micro:momentum_quality  — 모멘텀 품질 (0~5)
기존 Redis 키:
  flow:combined:cvd_5m/15m/1h — CVD
  flow:combined:whale_bias    — 고래 편향
  bn:price:BTCUSDT            — 가격
"""

import asyncio
import json
import logging
import time
import websockets
from collections import deque
from src.data.storage import RedisClient

logger = logging.getLogger(__name__)

BINANCE_WS = "wss://stream.binance.com:9443"  # spot WS (futures fstream 차단)
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

        # ── 마이크로스트럭처 버퍼 ──
        # 체결 이력 (최근 120초, 개별 체결 기록)
        self._trades: deque = deque(maxlen=50000)  # (ts, side, qty, price, size_usd)
        # CVD 스냅샷 (5초 간격, 가속도 계산용)
        self._cvd_snapshots: deque = deque(maxlen=30)  # (ts, cvd_5m)
        self._last_cvd_snap = 0
        # VWAP 데이터 (5분 윈도우)
        self._vwap_vol_sum = 0.0    # sum(price * qty)
        self._vwap_qty_sum = 0.0    # sum(qty)
        self._vwap_reset = 0
        # 가격 추적 (흡수/가격충격용)
        self._price_30s_ago = 0.0
        self._last_price_snap = 0
        self._price_history: deque = deque(maxlen=60)  # (ts, price)
        # 마이크로 Redis 갱신 주기
        self._last_micro_flush = 0

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
        # Binance Spot WS — 구독 메시지 방식 (futures WS 차단)
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
        ]
        url = f"{BINANCE_WS}/ws"

        while self._running:
            try:
                ws = await websockets.connect(url, ping_interval=20, open_timeout=10)
                self._reconnect_count = 0

                # 구독 메시지 전송 (공식 방식)
                subscribe_msg = json.dumps({
                    "method": "SUBSCRIBE",
                    "params": streams,
                    "id": 1,
                })
                await ws.send(subscribe_msg)
                logger.info(f"Binance WS 구독 전송: {len(streams)}개 스트림")

                # 재연결 시 CVD/마이크로 버퍼 리셋 (stale 데이터 방지)
                self._cvd_5m = 0.0
                self._cvd_15m = 0.0
                self._cvd_1h = 0.0
                self._cvd_reset_5m = 0
                self._cvd_reset_15m = 0
                self._cvd_reset_1h = 0
                self._trades.clear()
                self._cvd_snapshots.clear()
                self._last_micro_flush = 0
                logger.info(f"Binance WS 연결 성공: {SYMBOL} (버퍼 리셋) URL: {url[:60]}...")
                try:
                    while self._running:
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            # 30초 무응답 → ping 체크
                            logger.warning(f"Binance WS 30초 무응답 → ping 체크 (count={self._trade_count})")
                            try:
                                pong = await asyncio.wait_for(ws.ping(), timeout=5)
                                continue
                            except Exception as e:
                                logger.error(f"Binance WS ping 실패 → 재연결: {e}")
                                break
                        try:
                            data = json.loads(message)
                            await self._handle(data)
                        except json.JSONDecodeError:
                            continue
                        except Exception as e:
                            logger.error(f"Binance WS 처리 에러: {e}")
                finally:
                    await ws.close()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._reconnect_count += 1
                wait = min(5 * min(self._reconnect_count, 12), 60)
                logger.warning(f"Binance WS 끊김: {e} → {wait}초 후 재연결")
                await asyncio.sleep(wait)

    def stop(self):
        self._running = False

    async def _handle(self, raw: dict):
        # Binance combined stream: {"stream":"...","data":{...}}
        data = raw.get("data", raw)
        event = data.get("e", "")
        # 첫 100메시지 디버그 (이후 제거)
        if self._trade_count < 3:
            logger.warning(f"[BN-DEBUG] raw keys={list(raw.keys())[:5]} event='{event}' stream={raw.get('stream','?')[:30]}")
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
        is_buyer_maker = t.get("m", False)
        ts = int(t.get("T", 0))

        if price <= 0 or qty <= 0:
            return

        size_usd = price * qty
        # Binance: m=True → buyer is maker → seller aggressed → taker SELL
        # m=False → seller is maker → buyer aggressed → taker BUY
        side = "sell" if is_buyer_maker else "buy"
        delta = qty if side == "buy" else -qty

        # CVD 누적
        MAX_CVD = 1e9
        self._cvd_5m = max(-MAX_CVD, min(MAX_CVD, self._cvd_5m + delta))
        self._cvd_15m = max(-MAX_CVD, min(MAX_CVD, self._cvd_15m + delta))
        self._cvd_1h = max(-MAX_CVD, min(MAX_CVD, self._cvd_1h + delta))

        # ── 마이크로스트럭처 데이터 수집 ──
        now_f = time.time()
        self._trades.append((now_f, side, qty, price, size_usd))
        self._price_history.append((now_f, price))

        # VWAP 누적 (5분 윈도우)
        now_sec = int(now_f)
        if now_sec // 300 != self._vwap_reset:
            self._vwap_reset = now_sec // 300
            self._vwap_vol_sum = 0.0
            self._vwap_qty_sum = 0.0
        self._vwap_vol_sum += price * qty
        self._vwap_qty_sum += qty

        # CVD 스냅샷 (5초 간격 — 가속도 계산용)
        if now_f - self._last_cvd_snap >= 5:
            self._cvd_snapshots.append((now_f, self._cvd_5m))
            self._last_cvd_snap = now_f

        # Redis 저장 (매 체결마다는 과부하 → 100체결마다 or 대형 체결 시)
        self._trade_count += 1
        flush = self._trade_count % 100 == 0 or size_usd >= WHALE_THRESHOLD_USD

        # 마이크로스트럭처 Redis 갱신 (2초마다)
        if now_f - self._last_micro_flush >= 2:
            self._last_micro_flush = now_f
            await self._flush_microstructure(price)

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

        # CVD 윈도우 리셋 — 이전 윈도우 합계 저장 후 새 윈도우는 현재 delta부터 시작
        now_sec = int(time.time())
        if now_sec // 300 != self._cvd_reset_5m:
            self._cvd_reset_5m = now_sec // 300
            self._cvd_5m = delta  # 이번 거래만 남기고 리셋
        if now_sec // 900 != self._cvd_reset_15m:
            self._cvd_reset_15m = now_sec // 900
            self._cvd_15m = delta
        if now_sec // 3600 != self._cvd_reset_1h:
            self._cvd_reset_1h = now_sec // 3600
            self._cvd_1h = delta

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

    # ══════════════════════════════════════════════════
    #  마이크로스트럭처 집계 — 프롭 트레이더의 눈
    # ══════════════════════════════════════════════════

    async def _flush_microstructure(self, current_price: float):
        """2초마다 호출 — 체결 이력에서 15종 마이크로 피처 계산 → Redis"""
        now = time.time()
        try:
            # ── 1. Trade Rate (10초 체결건수/초) ──
            cutoff_10s = now - 10
            trades_10s = [(t, s, q, p, u) for t, s, q, p, u in self._trades if t >= cutoff_10s]
            trade_rate = len(trades_10s) / 10.0
            await self.redis.set("rt:micro:trade_rate", str(round(trade_rate, 1)), ttl=15)

            # ── 2. Trade Burst (10초 / 60초 비율) ──
            cutoff_60s = now - 60
            trades_60s = [(t, s, q, p, u) for t, s, q, p, u in self._trades if t >= cutoff_60s]
            rate_60s = len(trades_60s) / 60.0 if trades_60s else 1.0
            burst = trade_rate / max(rate_60s, 0.1)
            await self.redis.set("rt:micro:trade_burst", str(round(burst, 2)), ttl=15)

            # ── 3. Buy/Sell Ratio 다중 윈도우 (5s / 30s / 60s) ──
            for window_sec, key_suffix in [(5, "5s"), (30, "30s"), (60, "60s")]:
                cutoff = now - window_sec
                win_trades = [(s, q) for t, s, q, p, u in self._trades if t >= cutoff]
                buy_vol = sum(q for s, q in win_trades if s == "buy")
                sell_vol = sum(q for s, q in win_trades if s == "sell")
                total = buy_vol + sell_vol
                ratio = buy_vol / total if total > 0 else 0.5
                await self.redis.set(f"rt:micro:bs_ratio_{key_suffix}", str(round(ratio, 4)), ttl=15)

            # ── 4. Absorption Score (30초) ──
            # 매도량 > 매수량 1.5배인데 가격이 안 빠짐 = 매도 흡수 (롱 시그널)
            cutoff_30s = now - 30
            trades_30s = [(s, q, u) for t, s, q, p, u in self._trades if t >= cutoff_30s]
            buy_vol_30 = sum(q for s, q, u in trades_30s if s == "buy")
            sell_vol_30 = sum(q for s, q, u in trades_30s if s == "sell")

            price_30s_ago = 0
            for t, p in self._price_history:
                if t >= cutoff_30s:
                    price_30s_ago = p
                    break
            if price_30s_ago == 0 and self._price_history:
                price_30s_ago = self._price_history[0][1]

            price_change_30s = abs(current_price - price_30s_ago) if price_30s_ago > 0 else 999
            atr_proxy = current_price * 0.002  # 0.2% = ~$156 at $78k

            absorption_score = 0.0
            absorption_dir = "neutral"
            if sell_vol_30 > buy_vol_30 * 1.5 and price_change_30s < atr_proxy * 0.3:
                # 매도 압도인데 가격 안 빠짐 → 매수 흡수
                absorption_score = sell_vol_30 / max(price_change_30s + 0.01, 1) * 0.001
                absorption_dir = "long"
            elif buy_vol_30 > sell_vol_30 * 1.5 and price_change_30s < atr_proxy * 0.3:
                # 매수 압도인데 가격 안 올라감 → 매도 흡수
                absorption_score = buy_vol_30 / max(price_change_30s + 0.01, 1) * 0.001
                absorption_dir = "short"
            absorption_score = min(5.0, absorption_score)

            await self.redis.set("rt:micro:absorption", json.dumps({
                "score": round(absorption_score, 2),
                "direction": absorption_dir,
            }), ttl=15)

            # ── 5. Large Trade Clustering (60초, $50K+) ──
            cutoff_60 = now - 60
            recent_whales = [(t, s, sz) for t, s, sz, _ in self._whales if t >= cutoff_60]
            if len(recent_whales) >= 2:
                # 같은 방향 연속 건수
                max_streak = 0
                cur_streak = 1
                cur_dir = recent_whales[0][1]
                for i in range(1, len(recent_whales)):
                    if recent_whales[i][1] == cur_dir:
                        cur_streak += 1
                    else:
                        max_streak = max(max_streak, cur_streak)
                        cur_streak = 1
                        cur_dir = recent_whales[i][1]
                max_streak = max(max_streak, cur_streak)
                avg_size = sum(sz for _, _, sz in recent_whales) / len(recent_whales)
                cluster_score = max_streak * (avg_size / 100_000)
                cluster_dir = max(set(s for _, s, _ in recent_whales), key=lambda x: sum(1 for _, s2, _ in recent_whales if s2 == x))
            else:
                cluster_score = 0.0
                cluster_dir = "neutral"
                max_streak = 0

            await self.redis.set("rt:micro:whale_cluster", json.dumps({
                "score": round(min(10.0, cluster_score), 2),
                "direction": cluster_dir,
                "count": len(recent_whales),
                "max_streak": max_streak,
            }), ttl=15)

            # ── 6. Delta Acceleration (CVD 가속도) ──
            delta_accel = 0.0
            if len(self._cvd_snapshots) >= 4:
                snaps = list(self._cvd_snapshots)[-6:]
                if len(snaps) >= 3:
                    # 최근 3개 간격의 변화율 → 기울기의 기울기
                    deltas = [snaps[i+1][1] - snaps[i][1] for i in range(len(snaps)-1)]
                    if len(deltas) >= 2:
                        accel = deltas[-1] - deltas[0]
                        # 정규화 (-2 ~ +2)
                        delta_accel = max(-2.0, min(2.0, accel / max(abs(deltas[0]) + 1, 1)))
            await self.redis.set("rt:micro:delta_accel", str(round(delta_accel, 3)), ttl=15)

            # ── 7. Price Impact (60초, $1 가격변동 / $거래량) ──
            total_vol_60 = sum(u for _, _, _, _, u in trades_60s) if trades_60s else 0
            prices_60 = [p for t, _, _, p, _ in self._trades if t >= cutoff_60s]
            if len(prices_60) >= 2 and total_vol_60 > 0:
                price_range_60 = max(prices_60) - min(prices_60)
                impact = price_range_60 / (total_vol_60 / 1_000_000)  # $ per $1M volume
                impact = min(500.0, impact)  # cap
            else:
                impact = 0.0
            await self.redis.set("rt:micro:price_impact", str(round(impact, 1)), ttl=15)

            # ── 8. VWAP + Deviation ──
            vwap = self._vwap_vol_sum / self._vwap_qty_sum if self._vwap_qty_sum > 0 else current_price
            vwap_dev = (current_price - vwap) / vwap * 100 if vwap > 0 else 0
            await self.redis.set("rt:micro:vwap", json.dumps({
                "vwap": round(vwap, 1),
                "deviation_pct": round(vwap_dev, 4),
                "price": round(current_price, 1),
            }), ttl=30)

            # ── 9. Delta Divergence (5분 CVD vs 가격) ──
            delta_div = 0
            if len(prices_60) >= 10:
                # 최근 5분 가격 고점/저점 vs CVD
                price_high = max(prices_60)
                price_low = min(prices_60)
                is_price_high = current_price >= price_high * 0.999
                is_price_low = current_price <= price_low * 1.001

                if len(self._cvd_snapshots) >= 3:
                    cvd_vals = [v for _, v in list(self._cvd_snapshots)[-12:]]
                    cvd_max = max(cvd_vals) if cvd_vals else 0
                    cvd_min = min(cvd_vals) if cvd_vals else 0
                    is_cvd_high = self._cvd_5m >= cvd_max * 0.95
                    is_cvd_low = self._cvd_5m <= cvd_min * 0.95

                    if is_price_high and not is_cvd_high:
                        delta_div = -1  # bearish divergence
                    elif is_price_low and not is_cvd_low:
                        delta_div = 1   # bullish divergence

            await self.redis.set("rt:micro:delta_div", str(delta_div), ttl=15)

            # ── 10. Momentum Quality (종합) ──
            # body_ratio × vol_ratio × delta_alignment
            bs_60 = buy_vol_30 / max(buy_vol_30 + sell_vol_30, 1e-10)  # 0~1
            delta_alignment = 1.0
            if bs_60 > 0.6:
                delta_alignment = 1.5 if self._cvd_5m > 0 else 0.5
            elif bs_60 < 0.4:
                delta_alignment = 1.5 if self._cvd_5m < 0 else 0.5

            mom_quality = burst * (abs(bs_60 - 0.5) * 4) * delta_alignment
            mom_quality = min(5.0, mom_quality)
            await self.redis.set("rt:micro:momentum_quality", str(round(mom_quality, 2)), ttl=15)

        except Exception as e:
            logger.error(f"마이크로스트럭처 집계 에러: {e}", exc_info=True)
