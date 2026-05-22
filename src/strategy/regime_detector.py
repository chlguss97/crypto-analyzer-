"""
RegimeDetector — 선행 레짐 감지 (Leading Regime Detection)

4개 선행 시그널로 시장 상태를 실시간 판별:
  1. OBI (Order Book Imbalance) — 호가창 불균형, 0.5~5초 선행
  2. CVD Acceleration — 체결 방향 가속도, 2~15초 선행
  3. Volume Spike — 거래량 폭증, 1~10초 선행
  4. CUSUM — 가격 변화점 감지, 3~20초 선행

Composite Regime Score (CRS):
  |CRS| < 0.20 → ACTIVE (그리드 가동)
  |CRS| ≥ 0.35 (5초 지속) → PAUSED (주문 취소)
  서킷브레이커 → FROZEN (60초 동결)

SPEC: SPEC_V3.md 섹션 2
"""

import asyncio
import logging
import time
from collections import deque

from src.data.storage import RedisClient

logger = logging.getLogger(__name__)


class CUSUM:
    """Cumulative Sum 변화점 감지"""

    def __init__(self, threshold: float = 4.0, drift: float = 0.4):
        self.threshold = threshold
        self.drift = drift
        self.pos = 0.0
        self.neg = 0.0

    def update(self, z: float) -> str:
        """정규화된 값(z-score) 입력 → "up" | "down" | "none" """
        self.pos = max(0.0, self.pos + z - self.drift)
        self.neg = max(0.0, self.neg - z - self.drift)

        if self.pos > self.threshold:
            self.pos = 0.0
            return "up"
        if self.neg > self.threshold:
            self.neg = 0.0
            return "down"
        return "none"

    def reset(self):
        self.pos = 0.0
        self.neg = 0.0


class RegimeDetector:
    """선행 시그널 기반 실시간 레짐 감지"""

    def __init__(self, redis: RedisClient, ws_stream=None, config: dict = None):
        self.redis = redis
        self.ws = ws_stream
        cfg = (config or {}).get("regime", {})

        # 임계값
        self.pause_threshold = cfg.get("pause_threshold", 0.35)
        self.resume_threshold = cfg.get("resume_threshold", 0.15)
        self.pause_confirm_sec = cfg.get("pause_confirm_sec", 5)
        self.resume_confirm_sec = cfg.get("resume_confirm_sec", 30)
        self.cooldown_sec = cfg.get("mode_switch_cooldown_sec", 30)

        # 서킷브레이커
        self.cb_pct = cfg.get("circuit_breaker_pct", 2.0)
        self.cb_window_sec = cfg.get("circuit_breaker_window_sec", 10)
        self.cb_freeze_sec = cfg.get("circuit_breaker_freeze_sec", 60)

        # 가중치
        self.w_obi = cfg.get("weight_obi", 0.25)
        self.w_cvd = cfg.get("weight_cvd", 0.30)
        self.w_vol = cfg.get("weight_volume", 0.20)
        self.w_cusum = cfg.get("weight_cusum", 0.25)

        # CUSUM
        self.cusum = CUSUM(
            threshold=cfg.get("cusum_threshold", 3.0),
            drift=cfg.get("cusum_drift", 0.3),
        )
        self.cusum_decay = cfg.get("cusum_decay", 0.85)

        # Z-Score 정규화 계수
        self.zscore_divisor = cfg.get("zscore_divisor", 3.0)
        # 볼륨 스파이크 정규화
        self.vol_spike_divisor = cfg.get("vol_spike_divisor", 4.0)
        self.vol_direction_ratio = cfg.get("vol_direction_ratio", 1.5)

        # 상태
        self.mode = "ACTIVE"  # ACTIVE | PAUSED | FROZEN
        self.crs = 0.0
        self._last_mode_switch = 0.0
        self._pause_since = 0.0  # CRS가 임계 초과한 시점
        self._resume_since = 0.0  # CRS가 임계 미만인 시점
        self._frozen_until = 0.0

        # 시그널 버퍼
        self._price_buf: deque = deque(maxlen=15)  # (ts, price) 서킷브레이커 윈도우
        self._cvd_velocity_buf: deque = deque(maxlen=60)  # CVD 속도 히스토리
        self._returns_buf: deque = deque(maxlen=300)  # 1초 수익률 히스토리

        # OBI Z-Score (300초 rolling)
        self._obi_history: deque = deque(maxlen=300)

        # 콜백 (grid_engine이 등록)
        self.on_mode_change = None  # async callback(new_mode, crs)

    # ══════════════════════════════════════════
    #  메인 루프 (1초 주기)
    # ══════════════════════════════════════════

    async def run(self):
        """1초 주기로 시그널 계산 + 모드 판정"""
        logger.info("[REGIME] 레짐 감지 시작")
        last_tick = 0

        while True:
            try:
                now = time.time()
                if now - last_tick < 1.0:
                    await asyncio.sleep(0.1)
                    continue
                last_tick = now

                # FROZEN 해제 체크
                if self.mode == "FROZEN":
                    if now >= self._frozen_until:
                        await self._set_mode("PAUSED", 0.0)
                    await asyncio.sleep(0.2)
                    continue

                # 가격 수집
                price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
                if not price_str:
                    await asyncio.sleep(1)
                    continue
                price = float(price_str)
                self._price_buf.append((now, price))

                # 서킷브레이커 체크
                if self._check_circuit_breaker(now, price):
                    self._frozen_until = now + self.cb_freeze_sec
                    await self._set_mode("FROZEN", 99.0)
                    continue

                # 시그널 계산
                obi_signal = self._calc_obi()
                cvd_signal = self._calc_cvd_accel(now)
                vol_signal = self._calc_volume_spike(now)
                cusum_signal = self._calc_cusum(now, price)

                # CRS 합산
                self.crs = (
                    self.w_obi * obi_signal
                    + self.w_cvd * cvd_signal
                    + self.w_vol * vol_signal
                    + self.w_cusum * cusum_signal
                )
                self.crs = max(-1.0, min(1.0, self.crs))

                # 모드 판정
                await self._evaluate_mode(now)

                # Redis 저장 (모니터링용)
                await self.redis.set("regime:crs", f"{self.crs:.4f}", ttl=10)
                await self.redis.set("regime:mode", self.mode, ttl=10)
                await self.redis.hset("regime:signals", {
                    "obi": f"{obi_signal:.3f}",
                    "cvd": f"{cvd_signal:.3f}",
                    "vol": f"{vol_signal:.3f}",
                    "cusum": f"{cusum_signal:.3f}",
                    "crs": f"{self.crs:.4f}",
                    "mode": self.mode,
                }, ttl=10)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[REGIME] 에러: {e}", exc_info=True)
                await asyncio.sleep(2)

    # ══════════════════════════════════════════
    #  시그널 계산
    # ══════════════════════════════════════════

    def _calc_obi(self) -> float:
        """OBI Z-Score → [-1, +1] (변화 감지, 절대값 아님)"""
        if not self.ws:
            return 0.0
        raw_obi = self.ws.obi
        self._obi_history.append(raw_obi)

        if len(self._obi_history) < 30:
            return 0.0

        values = list(self._obi_history)
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        std = variance ** 0.5

        if std < 1e-6:
            return 0.0

        z = (raw_obi - mean) / std
        # Z-score [-3, +3] → [-1, +1]
        return max(-1.0, min(1.0, z / self.zscore_divisor))

    def _calc_cvd_accel(self, now: float) -> float:
        """CVD 가속도 Z-score → [-1, +1]"""
        if not self.ws:
            return 0.0

        snapshots = self.ws.cvd_snapshots
        if len(snapshots) < 3:
            return 0.0

        # CVD 5초 변화량
        recent = snapshots[-1]
        prev = snapshots[-2]
        dt = recent[0] - prev[0]
        if dt <= 0:
            return 0.0
        cvd_velocity = (recent[1] - prev[1]) / dt

        self._cvd_velocity_buf.append(cvd_velocity)

        if len(self._cvd_velocity_buf) < 5:
            return 0.0

        # Z-score (60초 lookback)
        values = list(self._cvd_velocity_buf)
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        std = variance ** 0.5
        if std < 1e-10:
            return 0.0

        z = (cvd_velocity - mean) / std
        # 정규화: z / 3.0 클리핑
        return max(-1.0, min(1.0, z / self.zscore_divisor))

    def _calc_volume_spike(self, now: float) -> float:
        """거래량 스파이크 → [-1, +1] (방향 포함)"""
        if not self.ws:
            return 0.0

        trades = self.ws.trades
        if len(trades) < 10:
            return 0.0

        # 최근 1초 거래량
        cutoff_1s = now - 1.0
        cutoff_60s = now - 60.0

        vol_1s = 0.0
        buy_vol_1s = 0.0
        sell_vol_1s = 0.0
        vol_60s_list = []

        for ts, side, size, price, size_usd in reversed(trades):
            if ts < cutoff_60s:
                break
            if ts >= cutoff_1s:
                vol_1s += size
                if side == "buy":
                    buy_vol_1s += size
                else:
                    sell_vol_1s += size

        # 60초 평균 (1초 단위)
        # 간단히: 전체 60초 볼륨 / 60
        total_60s = 0.0
        for ts, side, size, price, size_usd in reversed(trades):
            if ts < cutoff_60s:
                break
            total_60s += size
        avg_1s = total_60s / 60.0 if total_60s > 0 else 1e-10

        vol_ratio = vol_1s / avg_1s if avg_1s > 1e-10 else 1.0

        # 방향
        if buy_vol_1s + sell_vol_1s > 0:
            direction = 1.0 if buy_vol_1s > sell_vol_1s * self.vol_direction_ratio else (
                -1.0 if sell_vol_1s > buy_vol_1s * self.vol_direction_ratio else 0.0
            )
        else:
            direction = 0.0

        # 정규화: (ratio - 1) / 4 클리핑 × 방향
        spike = max(0.0, min(1.0, (vol_ratio - 1.0) / self.vol_spike_divisor))
        return spike * direction

    def _calc_cusum(self, now: float, price: float) -> float:
        """CUSUM 연속형 — 누적 상태를 비율로 출력 [-1, +1]"""
        if len(self._price_buf) < 3:
            return 0.0

        # 1초 수익률
        prev_price = self._price_buf[-2][1]
        if prev_price <= 0:
            return 0.0
        ret = (price - prev_price) / prev_price
        self._returns_buf.append(ret)

        if len(self._returns_buf) < 30:
            return 0.0

        # rolling mean/std (300초)
        values = list(self._returns_buf)
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        std = variance ** 0.5
        if std < 1e-12:
            return 0.0

        z = (ret - mean) / std

        # 누적 (리셋 없이 연속 출력)
        self.cusum.pos = max(0.0, self.cusum.pos + z - self.cusum.drift)
        self.cusum.neg = max(0.0, self.cusum.neg - z - self.cusum.drift)

        # 감쇄 (무한 누적 방지)
        self.cusum.pos *= self.cusum_decay
        self.cusum.neg *= self.cusum_decay

        # 연속 출력: (pos - neg) / threshold → [-1, +1]
        signal = (self.cusum.pos - self.cusum.neg) / self.cusum.threshold
        return max(-1.0, min(1.0, signal))

    # ══════════════════════════════════════════
    #  서킷브레이커
    # ══════════════════════════════════════════

    def _check_circuit_breaker(self, now: float, price: float) -> bool:
        """10초 내 2% 이동 감지"""
        if len(self._price_buf) < 2:
            return False

        cutoff = now - self.cb_window_sec
        for ts, p in self._price_buf:
            if ts >= cutoff:
                change_pct = abs(price - p) / p * 100
                if change_pct >= self.cb_pct:
                    logger.warning(
                        f"[REGIME] 서킷브레이커! ${p:.0f}→${price:.0f} "
                        f"({change_pct:.1f}%) in {now - ts:.0f}s → FROZEN"
                    )
                    return True
        return False

    # ══════════════════════════════════════════
    #  모드 판정
    # ══════════════════════════════════════════

    async def _evaluate_mode(self, now: float):
        """CRS + 히스테리시스 + 확인시간 → 모드 전환"""
        abs_crs = abs(self.crs)

        if self.mode == "ACTIVE":
            # ACTIVE → PAUSED: |CRS| ≥ 0.35, 5초 연속
            if abs_crs >= self.pause_threshold:
                if self._pause_since == 0:
                    self._pause_since = now
                elif now - self._pause_since >= self.pause_confirm_sec:
                    # 쿨다운 체크
                    if now - self._last_mode_switch >= self.cooldown_sec:
                        await self._set_mode("PAUSED", self.crs)
                        self._pause_since = 0
            else:
                self._pause_since = 0

        elif self.mode == "PAUSED":
            # PAUSED → ACTIVE: |CRS| < 0.15, 30초 연속 + vol_ratio < 2.0
            if abs_crs < self.resume_threshold:
                if self._resume_since == 0:
                    self._resume_since = now
                elif now - self._resume_since >= self.resume_confirm_sec:
                    if now - self._last_mode_switch >= self.cooldown_sec:
                        # 거래량 정상 확인 (vol_ratio < 2.0)
                        vol_ok = self._check_vol_normal(now)
                        if vol_ok:
                            await self._set_mode("ACTIVE", self.crs)
                            self._resume_since = 0
                        # vol 높으면 대기 유지
            else:
                self._resume_since = 0

    def _check_vol_normal(self, now: float) -> bool:
        """거래량이 정상 수준인지 확인 (vol_ratio < 2.0)"""
        if not self.ws:
            return True
        trades = self.ws.trades
        if len(trades) < 10:
            return True

        cutoff_1s = now - 1.0
        cutoff_60s = now - 60.0
        vol_1s = 0.0
        total_60s = 0.0

        for ts, side, size, price, size_usd in reversed(trades):
            if ts < cutoff_60s:
                break
            total_60s += size
            if ts >= cutoff_1s:
                vol_1s += size

        avg_1s = total_60s / 60.0 if total_60s > 0 else 1e-10
        vol_ratio = vol_1s / avg_1s if avg_1s > 1e-10 else 1.0
        return vol_ratio < 2.0

    async def _set_mode(self, new_mode: str, crs: float):
        """모드 전환 + 콜백 + 로그"""
        old_mode = self.mode
        self.mode = new_mode
        self._last_mode_switch = time.time()

        logger.info(f"[REGIME] {old_mode} → {new_mode} (CRS={crs:.3f})")

        await self.redis.set("regime:mode", new_mode, ttl=30)
        await self.redis.set("regime:crs", f"{crs:.4f}", ttl=30)

        if self.on_mode_change:
            try:
                await self.on_mode_change(new_mode, crs)
            except Exception as e:
                logger.error(f"[REGIME] 콜백 에러: {e}")

    # ══════════════════════════════════════════
    #  외부 인터페이스
    # ══════════════════════════════════════════

    def get_status(self) -> dict:
        """현재 레짐 상태 (대시보드/텔레그램용)"""
        return {
            "mode": self.mode,
            "crs": round(self.crs, 4),
            "frozen_until": self._frozen_until,
        }
