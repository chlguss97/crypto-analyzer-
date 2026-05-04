"""
OKX WebSocket — 전체 데이터 수집 (Binance 대체)

수집 항목:
  1. trades → CVD 5m/15m/1h + 마이크로스트럭처 15종
  2. tickers → 가격/ticker
  3. candle 7종 → DB 직접 저장 + 이벤트 발행
  4. books5 → 호가 불균형 (향후)

Redis 키 (기존과 동일 — candidate_detector/dashboard 변경 없음):
  flow:combined:cvd_5m/15m/1h   — CVD
  flow:combined:whale_bias       — 고래 편향
  rt:price:BTC-USDT-SWAP         — 가격
  rt:ticker:BTC-USDT-SWAP        — ticker
  rt:velocity:BTC-USDT-SWAP      — 가격 변속도
  rt:micro:*                     — 마이크로스트럭처 15종
"""

import asyncio
import json
import logging
import math
import time
import websockets
from collections import deque
from src.data.storage import RedisClient

logger = logging.getLogger(__name__)

OKX_WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
OKX_WS_BUSINESS = "wss://ws.okx.com:8443/ws/v5/business"  # 캔들 전용
SYMBOL = "BTC-USDT-SWAP"
WHALE_THRESHOLD_USD = 50_000
WHALE_WINDOW_SEC = 300


class WebSocketStream:
    """OKX WebSocket — 전체 시장 데이터 수집 + 마이크로스트럭처"""

    def __init__(self, redis_client: RedisClient, db=None):
        self.redis = redis_client
        self.db = db
        self.ws = None
        self._running = False
        self._reconnect_count = 0

        # CVD 누적 (5m / 15m / 1h)
        self._cvd_5m = 0.0
        self._cvd_15m = 0.0
        self._cvd_1h = 0.0
        self._cvd_reset_5m = 0
        self._cvd_reset_15m = 0
        self._cvd_reset_1h = 0

        # 가격 변속도 추적
        self._price_window = []
        self._price_window_max = 120

        # 마이크로스트럭처 버퍼
        self._trades: deque = deque(maxlen=50000)
        self._cvd_snapshots: deque = deque(maxlen=30)
        self._last_cvd_snap = 0
        self._vwap_vol_sum = 0.0
        self._vwap_qty_sum = 0.0
        self._vwap_reset = 0
        self._price_history: deque = deque(maxlen=60)
        self._last_micro_flush = 0

        # 고래 추적
        self._whales: deque = deque(maxlen=200)

        # 통계
        self._trade_count = 0
        self._last_log = 0

        # DB 저장 심볼
        from src.utils.helpers import load_config
        self._db_symbol = load_config().get("exchange", {}).get("symbol", "BTC/USDT:USDT")

    async def start(self, symbol: str = SYMBOL):
        """OKX WS 연결 시작 (무한 재시도)"""
        self._running = True
        self._reconnect_count = 0

        while self._running:
            try:
                await self._connect(symbol)
                self._reconnect_count = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._reconnect_count += 1
                wait = min(5 * min(self._reconnect_count, 12), 60)
                logger.warning(f"OKX WS 끊김: {e} → {wait}초 후 재연결 (시도 {self._reconnect_count})")
                await asyncio.sleep(wait)

    async def _connect(self, symbol: str):
        """WS 연결 + 전체 채널 구독"""
        ws = await websockets.connect(OKX_WS_PUBLIC, ping_interval=20, open_timeout=10)
        self.ws = ws
        self._reconnect_count = 0

        # 버퍼 리셋
        self._cvd_5m = 0.0
        self._cvd_15m = 0.0
        self._cvd_1h = 0.0
        self._cvd_reset_5m = 0
        self._cvd_reset_15m = 0
        self._cvd_reset_1h = 0
        self._trades.clear()
        self._cvd_snapshots.clear()
        self._whales.clear()
        self._last_micro_flush = 0
        self._trade_count = 0

        logger.info(f"OKX WS Public 연결 성공: {symbol} (버퍼 리셋)")

        # Business WS (캔들 전용 — OKX는 캔들을 /business 엔드포인트에서만 제공)
        ws_biz = await websockets.connect(OKX_WS_BUSINESS, ping_interval=20, open_timeout=10)
        logger.info(f"OKX WS Business 연결 성공 (캔들)")

        try:
            # Public: trades + tickers + books
            await ws.send(json.dumps({
                "op": "subscribe",
                "args": [
                    {"channel": "tickers", "instId": symbol},
                    {"channel": "trades", "instId": symbol},
                    {"channel": "books5", "instId": symbol},
                ],
            }))

            # Business: 캔들 7종
            await ws_biz.send(json.dumps({
                "op": "subscribe",
                "args": [
                    {"channel": "candle1m", "instId": symbol},
                    {"channel": "candle5m", "instId": symbol},
                    {"channel": "candle15m", "instId": symbol},
                    {"channel": "candle1H", "instId": symbol},
                    {"channel": "candle4H", "instId": symbol},
                    {"channel": "candle1D", "instId": symbol},
                    {"channel": "candle1W", "instId": symbol},
                ],
            }))
            logger.info("OKX WS 구독: Public 3채널 + Business 7채널")

            # 두 WS에서 동시 수신 — 한쪽이라도 끊기면 양쪽 다 재연결
            self._ws_tasks_done = asyncio.Event()

            async def _recv_loop(ws_conn, name):
                while self._running and not self._ws_tasks_done.is_set():
                    try:
                        message = await asyncio.wait_for(ws_conn.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        try:
                            await ws_conn.ping()
                            continue
                        except Exception:
                            logger.warning(f"OKX WS {name} ping 실패 → 재연결")
                            break
                    except Exception as e:
                        logger.warning(f"OKX WS {name} 수신 끊김: {e} → 재연결")
                        break
                    try:
                        data = json.loads(message)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    try:
                        await self._handle_message(data)
                    except Exception as e:
                        logger.error(f"OKX WS {name} 처리 에러: {e}", exc_info=True)
                # 한쪽이 끊기면 다른 쪽도 종료시킴
                self._ws_tasks_done.set()

            await asyncio.gather(
                _recv_loop(ws, "public"),
                _recv_loop(ws_biz, "business"),
            )
        finally:
            await ws.close()
            await ws_biz.close()

    async def _handle_message(self, data: dict):
        """수신 메시지 라우팅"""
        if "event" in data:
            if data["event"] == "subscribe":
                logger.info(f"OKX WS 구독 확인: {data.get('arg', {}).get('channel')}")
            elif data["event"] == "error":
                logger.error(f"OKX WS 에러: {data}")
            return

        arg = data.get("arg", {})
        channel = arg.get("channel", "")
        items = data.get("data", [])
        if not items:
            logger.warning(f"[DEBUG] 빈 data: channel={channel}")
            return

        if channel == "tickers":
            await self._handle_ticker(items[0])
        elif channel == "trades":
            for trade in items:
                await self._handle_trade(trade)
        elif channel.startswith("candle"):
            tf = channel.replace("candle", "")
            for candle in items:
                await self._handle_candle(candle, tf)
        elif channel == "books5":
            await self._handle_books(items[0])
        else:
            logger.debug(f"알 수 없는 channel: {channel}")

    # ══════════════════════════════════════════
    #  Ticker
    # ══════════════════════════════════════════

    async def _handle_ticker(self, ticker: dict):
        symbol = ticker.get("instId", SYMBOL)
        await self.redis.set(f"rt:price:{symbol}", ticker.get("last", "0"), ttl=30)
        await self.redis.hset(f"rt:ticker:{symbol}", {
            "last": ticker.get("last", "0"),
            "bid": ticker.get("bidPx", "0"),
            "ask": ticker.get("askPx", "0"),
            "high24h": ticker.get("high24h", "0"),
            "low24h": ticker.get("low24h", "0"),
            "vol24h": ticker.get("volCcy24h", "0"),
            "timestamp": ticker.get("ts", "0"),
        })

    # ══════════════════════════════════════════
    #  Trades → CVD + 마이크로스트럭처
    # ══════════════════════════════════════════

    async def _handle_trade(self, trade: dict):
        price = float(trade.get("px", 0))
        size = float(trade.get("sz", 0))
        side = trade.get("side", "")
        ts = int(trade.get("ts", 0))

        if price <= 0 or size <= 0:
            return

        size_usd = price * size
        delta = size if side == "buy" else -size
        now_f = time.time()

        # ── CVD 누적 ──
        MAX_CVD = 1e9
        self._cvd_5m = max(-MAX_CVD, min(MAX_CVD, self._cvd_5m + delta))
        self._cvd_15m = max(-MAX_CVD, min(MAX_CVD, self._cvd_15m + delta))
        self._cvd_1h = max(-MAX_CVD, min(MAX_CVD, self._cvd_1h + delta))

        # ── 마이크로 버퍼 ──
        self._trades.append((now_f, side, size, price, size_usd))
        self._price_history.append((now_f, price))

        # VWAP (5분 윈도우)
        now_sec = int(now_f)
        if now_sec // 300 != self._vwap_reset:
            self._vwap_reset = now_sec // 300
            self._vwap_vol_sum = 0.0
            self._vwap_qty_sum = 0.0
        self._vwap_vol_sum += price * size
        self._vwap_qty_sum += size

        # CVD 스냅샷 (5초 간격)
        if now_f - self._last_cvd_snap >= 5:
            self._cvd_snapshots.append((now_f, self._cvd_5m))
            self._last_cvd_snap = now_f

        # ── Redis 갱신 (100체결마다 or 고래) ──
        self._trade_count += 1
        flush = self._trade_count % 100 == 0 or size_usd >= WHALE_THRESHOLD_USD

        if flush:
            await self.redis.set("flow:combined:cvd_5m", str(round(self._cvd_5m, 4)), ttl=400)
            await self.redis.set("flow:combined:cvd_15m", str(round(self._cvd_15m, 4)), ttl=1200)
            await self.redis.set("flow:combined:cvd_1h", str(round(self._cvd_1h, 4)), ttl=4800)

        # 마이크로 Redis (2초마다)
        if now_f - self._last_micro_flush >= 2:
            self._last_micro_flush = now_f
            await self._flush_microstructure(price)

        # ── 고래 감지 ──
        if size_usd >= WHALE_THRESHOLD_USD:
            self._whales.append((now_f, side, round(size_usd), round(price, 1)))
            while self._whales and self._whales[0][0] < now_f - WHALE_WINDOW_SEC:
                self._whales.popleft()

            buy_vol = sum(s for _, sd, s, _ in self._whales if sd == "buy")
            sell_vol = sum(s for _, sd, s, _ in self._whales if sd == "sell")
            total = buy_vol + sell_vol
            whale_bias = (buy_vol - sell_vol) / total if total > 0 else 0
            await self.redis.set("flow:combined:whale_bias", str(round(whale_bias, 3)), ttl=600)

        # ── 가격 변속도 ──
        if price > 0 and ts > 0:
            self._price_window.append((ts, price))
            cutoff = ts - 60_000
            while self._price_window and self._price_window[0][0] < cutoff:
                self._price_window.pop(0)

            if len(self._price_window) >= 5:
                prices_in_window = [p for _, p in self._price_window]
                win_high = max(prices_in_window)
                win_low = min(prices_in_window)
                oldest_price = self._price_window[0][1]

                ts_10s = ts - 10_000
                prices_10s = [p for t, p in self._price_window if t >= ts_10s]
                ts_30s = ts - 30_000
                prices_30s = [p for t, p in self._price_window if t >= ts_30s]

                await self.redis.hset(f"rt:velocity:{SYMBOL}", {
                    "range_60s": str(round(win_high - win_low, 1)),
                    "move_60s": str(round(price - oldest_price, 1)),
                    "range_30s": str(round((max(prices_30s) - min(prices_30s)) if len(prices_30s) >= 2 else 0, 1)),
                    "move_30s": str(round((price - prices_30s[0]) if prices_30s else 0, 1)),
                    "range_10s": str(round((max(prices_10s) - min(prices_10s)) if len(prices_10s) >= 2 else 0, 1)),
                    "move_10s": str(round((price - prices_10s[0]) if prices_10s else 0, 1)),
                    "ts": str(ts),
                })

        # ── CVD 윈도우 리셋 ──
        if now_sec // 300 != self._cvd_reset_5m:
            self._cvd_reset_5m = now_sec // 300
            self._cvd_5m = delta
        if now_sec // 900 != self._cvd_reset_15m:
            self._cvd_reset_15m = now_sec // 900
            self._cvd_15m = delta
        if now_sec // 3600 != self._cvd_reset_1h:
            self._cvd_reset_1h = now_sec // 3600
            self._cvd_1h = delta

        # 주기 로그 (5분마다)
        if now_sec - self._last_log >= 300:
            self._last_log = now_sec
            logger.info(
                f"OKX CVD: 5m={self._cvd_5m:+.2f} 15m={self._cvd_15m:+.2f} "
                f"1h={self._cvd_1h:+.2f} | whales={len(self._whales)} | trades={self._trade_count}"
            )

    # ══════════════════════════════════════════
    #  Candle → DB 저장 + 이벤트 발행
    # ══════════════════════════════════════════

    async def _handle_candle(self, candle: list, tf: str):
        """OKX candle: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]"""
        if len(candle) < 9:
            return

        is_closed = candle[8] == "1"
        candle_dict = {
            "timestamp": int(candle[0]),
            "open": float(candle[1]),
            "high": float(candle[2]),
            "low": float(candle[3]),
            "close": float(candle[4]),
            "volume": float(candle[5]),
        }

        if candle_dict["close"] <= 0:
            return

        # TF 매핑 (OKX → 표준)
        tf_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1H": "1h", "4H": "4h", "1D": "1d", "1W": "1w"}
        std_tf = tf_map.get(tf, tf.lower())

        # DB 저장
        if self.db:
            should_save = is_closed
            if not is_closed:
                cache_key = f"_candle_last_save_{std_tf}"
                last = getattr(self, cache_key, 0)
                now = time.time()
                if std_tf == "1m" or now - last >= 5:
                    should_save = True
                    setattr(self, cache_key, now)

            if should_save:
                try:
                    await self.db.insert_candles(self._db_symbol, std_tf, [candle_dict])
                except Exception as e:
                    logger.debug(f"OKX candle DB 저장 실패 ({std_tf}): {e}")

        # 캔들 확정 → 이벤트 발행 (eval 루프 트리거)
        if is_closed:
            try:
                await self.redis.publish("ch:kline:ready", json.dumps({
                    "tf": std_tf, "close": candle_dict["close"], "ts": candle_dict["timestamp"],
                }))
            except Exception:
                pass

    # ══════════════════════════════════════════
    #  Books (호가창 — 향후 피처 확장용)
    # ══════════════════════════════════════════

    async def _handle_books(self, book: dict):
        """호가 불균형 계산 (향후 ML 피처)"""
        # OKX books5: {"asks":[[price,size,0,count],...], "bids":[[price,size,0,count],...]}
        try:
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if not bids or not asks:
                return

            bid_total = sum(float(b[1]) for b in bids[:5])
            ask_total = sum(float(a[1]) for a in asks[:5])
            total = bid_total + ask_total
            if total <= 0:
                return

            imbalance = (bid_total - ask_total) / total  # -1 ~ +1
            spread = float(asks[0][0]) - float(bids[0][0])

            await self.redis.set("rt:micro:book_imbalance", str(round(imbalance, 4)), ttl=15)
            await self.redis.set("rt:micro:spread", str(round(spread, 2)), ttl=15)
        except Exception:
            pass

    # ══════════════════════════════════════════
    #  마이크로스트럭처 집계 (2초마다)
    # ══════════════════════════════════════════

    async def _flush_microstructure(self, current_price: float):
        """trades 이력에서 15종 마이크로 피처 계산 → Redis"""
        now = time.time()
        try:
            # 1. Trade Rate (10초)
            cutoff_10s = now - 10
            trades_10s = [(t, s, q, p, u) for t, s, q, p, u in self._trades if t >= cutoff_10s]
            trade_rate = len(trades_10s) / 10.0
            await self.redis.set("rt:micro:trade_rate", str(round(trade_rate, 1)), ttl=15)

            # 2. Trade Burst
            cutoff_60s = now - 60
            trades_60s = [(t, s, q, p, u) for t, s, q, p, u in self._trades if t >= cutoff_60s]
            rate_60s = len(trades_60s) / 60.0 if trades_60s else 1.0
            burst = trade_rate / max(rate_60s, 0.1)
            await self.redis.set("rt:micro:trade_burst", str(round(burst, 2)), ttl=15)

            # 3. Buy/Sell Ratio (5s/30s/60s)
            for window_sec, key_suffix in [(5, "5s"), (30, "30s"), (60, "60s")]:
                cutoff = now - window_sec
                win_trades = [(s, q) for t, s, q, p, u in self._trades if t >= cutoff]
                buy_vol = sum(q for s, q in win_trades if s == "buy")
                sell_vol = sum(q for s, q in win_trades if s == "sell")
                total = buy_vol + sell_vol
                ratio = buy_vol / total if total > 0 else 0.5
                await self.redis.set(f"rt:micro:bs_ratio_{key_suffix}", str(round(ratio, 4)), ttl=15)

            # 4. Absorption
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
            atr_proxy = current_price * 0.002

            absorption_score = 0.0
            absorption_dir = "neutral"
            if sell_vol_30 > buy_vol_30 * 1.5 and price_change_30s < atr_proxy * 0.3:
                absorption_score = sell_vol_30 / max(price_change_30s + 0.01, 1) * 0.001
                absorption_dir = "long"
            elif buy_vol_30 > sell_vol_30 * 1.5 and price_change_30s < atr_proxy * 0.3:
                absorption_score = buy_vol_30 / max(price_change_30s + 0.01, 1) * 0.001
                absorption_dir = "short"
            absorption_score = min(5.0, absorption_score)
            await self.redis.set("rt:micro:absorption", json.dumps({
                "score": round(absorption_score, 2), "direction": absorption_dir,
            }), ttl=15)

            # 5. Whale Cluster
            cutoff_wh = now - 60
            recent_whales = [(t, s, sz) for t, s, sz, _ in self._whales if t >= cutoff_wh]
            if len(recent_whales) >= 2:
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
                cluster_dir = max(set(s for _, s, _ in recent_whales),
                                  key=lambda x: sum(1 for _, s2, _ in recent_whales if s2 == x))
            else:
                cluster_score, cluster_dir, max_streak = 0.0, "neutral", 0
            await self.redis.set("rt:micro:whale_cluster", json.dumps({
                "score": round(min(10.0, cluster_score), 2),
                "direction": cluster_dir, "count": len(recent_whales),
                "max_streak": max_streak,
            }), ttl=15)

            # 6. Delta Acceleration
            delta_accel = 0.0
            if len(self._cvd_snapshots) >= 4:
                snaps = list(self._cvd_snapshots)[-6:]
                if len(snaps) >= 3:
                    deltas = [snaps[i+1][1] - snaps[i][1] for i in range(len(snaps)-1)]
                    if len(deltas) >= 2:
                        accel = deltas[-1] - deltas[0]
                        delta_accel = max(-2.0, min(2.0, accel / max(abs(deltas[0]) + 1, 1)))
            await self.redis.set("rt:micro:delta_accel", str(round(delta_accel, 3)), ttl=15)

            # 7. Price Impact
            total_vol_60 = sum(u for _, _, _, _, u in trades_60s) if trades_60s else 0
            prices_60 = [p for t, _, _, p, _ in self._trades if t >= cutoff_60s]
            if len(prices_60) >= 2 and total_vol_60 > 0:
                price_range_60 = max(prices_60) - min(prices_60)
                impact = price_range_60 / (total_vol_60 / 1_000_000)
                impact = min(500.0, impact)
            else:
                impact = 0.0
            await self.redis.set("rt:micro:price_impact", str(round(impact, 1)), ttl=15)

            # 8. VWAP
            vwap = self._vwap_vol_sum / self._vwap_qty_sum if self._vwap_qty_sum > 0 else current_price
            vwap_dev = (current_price - vwap) / vwap * 100 if vwap > 0 else 0
            await self.redis.set("rt:micro:vwap", json.dumps({
                "vwap": round(vwap, 1), "deviation_pct": round(vwap_dev, 4),
                "price": round(current_price, 1),
            }), ttl=30)

            # 9. Delta Divergence
            delta_div = 0
            if len(prices_60) >= 10 and len(self._cvd_snapshots) >= 3:
                price_high = max(prices_60)
                price_low = min(prices_60)
                is_price_high = current_price >= price_high * 0.999
                is_price_low = current_price <= price_low * 1.001
                cvd_vals = [v for _, v in list(self._cvd_snapshots)[-12:]]
                if cvd_vals:
                    cvd_max = max(cvd_vals)
                    cvd_min = min(cvd_vals)
                    is_cvd_high = self._cvd_5m >= cvd_max * 0.95 if cvd_max > 0 else self._cvd_5m >= cvd_max
                    is_cvd_low = self._cvd_5m <= cvd_min * 0.95 if cvd_min < 0 else self._cvd_5m <= cvd_min
                    if is_price_high and not is_cvd_high:
                        delta_div = -1
                    elif is_price_low and not is_cvd_low:
                        delta_div = 1
            await self.redis.set("rt:micro:delta_div", str(delta_div), ttl=15)

            # 10. Momentum Quality
            bs_ratio = buy_vol_30 / max(buy_vol_30 + sell_vol_30, 1e-10)
            delta_alignment = 1.0
            if bs_ratio > 0.6:
                delta_alignment = 1.5 if self._cvd_5m > 0 else 0.5
            elif bs_ratio < 0.4:
                delta_alignment = 1.5 if self._cvd_5m < 0 else 0.5
            mom_quality = burst * (abs(bs_ratio - 0.5) * 4) * delta_alignment
            mom_quality = min(5.0, mom_quality)
            await self.redis.set("rt:micro:momentum_quality", str(round(mom_quality, 2)), ttl=15)

        except Exception as e:
            logger.error(f"마이크로스트럭처 집계 에러: {e}", exc_info=True)

    def stop(self):
        self._running = False
