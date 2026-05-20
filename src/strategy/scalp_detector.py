"""
ScalpDetector — 실시간 마이크로스트럭처 스캘핑 시그널 감지

4계층 파이프라인:
  [1] Raw Data (Redis) → [2] Feature Read → [3] Regime Gate → [4] Signal Check

Redis 키만 읽음. DB/캔들 접근 없음. 500ms 폴링 최적화.

시그널 2종:
  A. Micro-Momentum Burst (Hurst > 0.6 시 활성)
  B. VWAP Snap — 평균회귀 (Hurst < 0.4 시 활성)

References:
  - Cont, Kukanov, & Stoikov (2014): OFI
  - Hurst (1951): R/S analysis
  - Easley, López de Prado, & O'Hara (2012): VPIN
"""

import json
import logging
import time

from src.data.storage import RedisClient
from src.strategy.welford import FeatureNormalizer

logger = logging.getLogger(__name__)


class ScalpDetector:
    """실시간 스캘핑 시그널 감지기 — Redis only"""

    def __init__(self, redis: RedisClient, config: dict):
        self.redis = redis
        cfg = config.get("scalp", {})

        # Regime thresholds
        self.hurst_momentum = cfg.get("hurst_momentum", 0.6)
        self.hurst_mean_revert = cfg.get("hurst_mean_revert", 0.4)
        self.vpin_extreme = cfg.get("vpin_extreme", 0.7)

        # Burst signal thresholds
        self.burst_min_move_10s_pct = cfg.get("burst_min_move_10s_pct", 0.06)
        self.burst_min_move_30s_pct = cfg.get("burst_min_move_30s_pct", 0.10)
        self.burst_freshness = cfg.get("burst_freshness", 0.4)
        self.burst_min_ofi_z = cfg.get("burst_min_ofi_z", 1.0)
        self.burst_min_trade_burst = cfg.get("burst_min_trade_burst", 1.8)
        self.burst_min_bs_5s = cfg.get("burst_min_bs_5s", 0.60)
        self.burst_max_exhaustion_pct = cfg.get("burst_max_exhaustion_pct", 0.15)

        # VWAP snap thresholds
        self.snap_min_vwap_dev = cfg.get("snap_min_vwap_dev", 0.12)
        self.snap_min_absorption = cfg.get("snap_min_absorption", 1.0)
        self.snap_min_book_imbal = cfg.get("snap_min_book_imbal", 0.15)

        # Common filters
        self.max_spread = cfg.get("max_spread", 2.0)
        self.min_micro_confidence = cfg.get("min_micro_confidence", 0.3)
        self.stale_threshold_ms = cfg.get("stale_threshold_ms", 5000)

        # Welford normalizer
        self._normalizer = FeatureNormalizer(window=100)

        # State
        self._last_signal_ts = 0

    async def evaluate(self) -> dict | None:
        """
        Redis에서 피처 읽기 → Regime Gate → Signal Check.
        반환: {type, direction, strength, price, features} or None
        """
        # ── 1. 피처 수집 (Redis reads) ──
        features = await self._read_features()
        if features is None:
            return None

        price = features.get("price", 0)
        if price <= 0:
            return None

        # ── 2. Staleness check ──
        velocity_ts = features.get("velocity_ts", 0)
        now_ms = int(time.time() * 1000)
        if velocity_ts > 0 and (now_ms - velocity_ts) > self.stale_threshold_ms:
            return None

        # ── 3. Spread filter ──
        spread = features.get("spread", 999)
        if spread > self.max_spread:
            return None

        # ── 4. Regime Gate ──
        hurst = features.get("hurst", 0.5)
        vpin = features.get("vpin", 0.3)

        if vpin >= self.vpin_extreme:
            return None  # 극단 독성 → 거래 금지

        regime = "random_walk"
        if hurst > self.hurst_momentum:
            regime = "momentum"
        elif hurst < self.hurst_mean_revert:
            regime = "mean_revert"

        if regime == "random_walk":
            return None  # 랜덤워크 → 거래 금지

        # ── 5. Microstructure Regime → confidence multiplier ──
        micro_conf = self._calc_micro_confidence(features)
        if micro_conf < self.min_micro_confidence:
            return None

        # ── 6. Z-Score 정규화 ──
        raw_features = {
            "ofi": features.get("ofi", 0),
            "book_imbalance": features.get("book_imbalance", 0),
            "trade_burst": features.get("trade_burst", 1.0),
            "bs_ratio_5s": features.get("bs_ratio_5s", 0.5),
            "bs_ratio_30s": features.get("bs_ratio_30s", 0.5),
            "momentum_quality": features.get("momentum_quality", 0),
            "delta_accel": features.get("delta_accel", 0),
            "cvd_5m": features.get("cvd_5m", 0),
        }
        z_features = self._normalizer.update_all(raw_features)

        # ── 7. Signal Check ──
        signal = None
        if regime == "momentum":
            signal = self._check_burst(features, z_features, price)
        elif regime == "mean_revert":
            signal = self._check_vwap_snap(features, z_features, price)

        if signal is None:
            return None

        # ── 8. Build result ──
        signal["price"] = price
        signal["regime"] = regime
        signal["hurst"] = round(hurst, 4)
        signal["vpin"] = round(vpin, 4)
        signal["micro_confidence"] = round(micro_conf, 2)
        signal["features"] = {
            **{f"z_{k}": round(v, 4) for k, v in z_features.items()},
            "ofi_raw": round(features.get("ofi", 0), 4),
            "book_imbalance": round(features.get("book_imbalance", 0), 4),
            "spread": round(spread, 2),
            "trade_burst": round(features.get("trade_burst", 1), 2),
            "bs_ratio_5s": round(features.get("bs_ratio_5s", 0.5), 4),
            "bs_ratio_30s": round(features.get("bs_ratio_30s", 0.5), 4),
            "momentum_quality": round(features.get("momentum_quality", 0), 2),
            "delta_accel": round(features.get("delta_accel", 0), 3),
            "vwap_deviation": round(features.get("vwap_deviation", 0), 4),
            "delta_div": features.get("delta_div", 0),
            "absorption_score": round(features.get("absorption_score", 0), 2),
            "whale_bias": round(features.get("whale_bias", 0), 4),
            "cvd_5m": round(features.get("cvd_5m", 0), 2),
            "funding_rate": round(features.get("funding_rate", 0), 6),
            "hurst": round(hurst, 4),
            "vpin": round(vpin, 4),
            "parkinson_vol": round(features.get("parkinson_vol", 0), 6),
            "micro_confidence": round(micro_conf, 2),
        }

        self._last_signal_ts = time.time()
        return signal

    def _check_burst(self, feat: dict, z_feat: dict, price: float) -> dict | None:
        """Signal A: Micro-Momentum Burst"""
        move_10s = feat.get("move_10s", 0)
        move_30s = feat.get("move_30s", 0)
        move_60s = feat.get("move_60s", 0)

        # 방향 결정 (10s + 30s 동일 방향)
        if move_10s > 0 and move_30s > 0:
            direction = "long"
        elif move_10s < 0 and move_30s < 0:
            direction = "short"
        else:
            return None

        abs_10s = abs(move_10s)
        abs_30s = abs(move_30s)

        # 최소 속도
        if abs_10s < price * self.burst_min_move_10s_pct / 100:
            return None
        if abs_30s < price * self.burst_min_move_30s_pct / 100:
            return None

        # 신선도 (10s가 30s의 40%+ → 가속 중)
        if abs_30s > 0 and abs_10s / abs_30s < self.burst_freshness:
            return None

        # OFI z-score 방향 일치
        ofi_z = z_feat.get("ofi", 0)
        if direction == "long" and ofi_z < self.burst_min_ofi_z:
            return None
        if direction == "short" and ofi_z > -self.burst_min_ofi_z:
            return None

        # 체결 급증
        if feat.get("trade_burst", 0) < self.burst_min_trade_burst:
            return None

        # BS ratio 5s 확인
        bs_5s = feat.get("bs_ratio_5s", 0.5)
        if direction == "long" and bs_5s < self.burst_min_bs_5s:
            return None
        if direction == "short" and bs_5s > (1 - self.burst_min_bs_5s):
            return None

        # 소진 필터 (60s 이미 0.15%+ → 추격 방지)
        if abs(move_60s) > price * self.burst_max_exhaustion_pct / 100:
            return None

        # Strength scoring
        strength = 1.0
        cvd_5m = feat.get("cvd_5m", 0)
        if (direction == "long" and cvd_5m > 0) or (direction == "short" and cvd_5m < 0):
            strength += 0.5
        whale_bias = feat.get("whale_bias", 0)
        if (direction == "long" and whale_bias > 0.2) or (direction == "short" and whale_bias < -0.2):
            strength += 0.3
        if abs(z_feat.get("delta_accel", 0)) > 0.5:
            strength += 0.2

        return {"type": "micro_burst", "direction": direction, "strength": round(strength, 2)}

    def _check_vwap_snap(self, feat: dict, z_feat: dict, price: float) -> dict | None:
        """Signal B: VWAP Snap (평균회귀)"""
        vwap_dev = feat.get("vwap_deviation", 0)

        if abs(vwap_dev) < self.snap_min_vwap_dev:
            return None

        # 방향: VWAP 위→short (과매수 회귀), VWAP 아래→long (과매도 회귀)
        direction = "short" if vwap_dev > 0 else "long"

        # Delta divergence 확인
        delta_div = feat.get("delta_div", 0)
        if direction == "long" and delta_div != 1:
            return None
        if direction == "short" and delta_div != -1:
            return None

        # Absorption 확인
        absorption = feat.get("absorption_score", 0)
        absorption_dir = feat.get("absorption_dir", "neutral")
        if absorption < self.snap_min_absorption:
            return None
        if absorption_dir != direction:
            return None

        # Book imbalance 확인
        book_imbal = feat.get("book_imbalance", 0)
        if direction == "long" and book_imbal < self.snap_min_book_imbal:
            return None
        if direction == "short" and book_imbal > -self.snap_min_book_imbal:
            return None

        # 스프레드 더 타이트하게
        if feat.get("spread", 999) > 1.5:
            return None

        strength = 1.0
        if abs(vwap_dev) > 0.20:
            strength += 0.5
        cvd_5m = feat.get("cvd_5m", 0)
        if (direction == "long" and cvd_5m > 0) or (direction == "short" and cvd_5m < 0):
            strength += 0.3

        return {"type": "vwap_snap", "direction": direction, "strength": round(strength, 2)}

    def _calc_micro_confidence(self, feat: dict) -> float:
        """마이크로스트럭처 레짐 → 신뢰도 배수 (0.0~1.0)"""
        spread = feat.get("spread", 1.0)
        imbal = abs(feat.get("book_imbalance", 0))
        burst = feat.get("trade_burst", 1.0)

        # 스프레드: tight < $0.5 = 1.0, normal = 0.7, wide > $2 = 0.2
        if spread < 0.5:
            s_score = 1.0
        elif spread < 2.0:
            s_score = 0.7
        else:
            s_score = 0.2

        # 호가 깊이: 균형(imbal<0.3) = 1.0, 편향 = 0.6, 극단(>0.7) = 0.3
        if imbal < 0.3:
            d_score = 1.0
        elif imbal < 0.7:
            d_score = 0.6
        else:
            d_score = 0.3

        # 활성도: active(burst>1.5) = 1.0, normal = 0.6, quiet(<0.7) = 0.2
        if burst > 1.5:
            a_score = 1.0
        elif burst > 0.7:
            a_score = 0.6
        else:
            a_score = 0.2

        return round(s_score * 0.4 + d_score * 0.3 + a_score * 0.3, 2)

    async def _read_features(self) -> dict | None:
        """Redis에서 모든 피처 읽기. 필수 키 누락 시 None."""
        try:
            # 병렬 읽기는 안되지만, 각각 빠름 (로컬 Redis)
            price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
            if not price_str:
                return None
            price = float(price_str)

            # Velocity
            vel = await self.redis.hgetall("rt:velocity:BTC-USDT-SWAP")
            if not vel:
                return None

            # Microstructure (필수)
            burst_str = await self.redis.get("rt:micro:trade_burst")
            bs5_str = await self.redis.get("rt:micro:bs_ratio_5s")
            bs30_str = await self.redis.get("rt:micro:bs_ratio_30s")
            spread_str = await self.redis.get("rt:micro:spread")
            imbal_str = await self.redis.get("rt:micro:book_imbalance")

            if not all([burst_str, bs5_str, spread_str, imbal_str]):
                return None

            # Optional features (None-safe)
            ofi_str = await self.redis.get("rt:micro:ofi")
            mom_str = await self.redis.get("rt:micro:momentum_quality")
            accel_str = await self.redis.get("rt:micro:delta_accel")
            div_str = await self.redis.get("rt:micro:delta_div")
            impact_str = await self.redis.get("rt:micro:price_impact")
            hurst_str = await self.redis.get("rt:regime:hurst")
            vpin_str = await self.redis.get("rt:micro:vpin")
            pvol_str = await self.redis.get("rt:micro:parkinson_vol")
            funding_str = await self.redis.get("rt:funding:BTC-USDT-SWAP")
            whale_str = await self.redis.get("flow:combined:whale_bias")
            cvd_str = await self.redis.get("flow:combined:cvd_5m")

            # VWAP (JSON)
            vwap_str = await self.redis.get("rt:micro:vwap")
            vwap_dev = 0.0
            if vwap_str:
                vwap_data = json.loads(vwap_str)
                vwap_dev = float(vwap_data.get("deviation_pct", 0))

            # Absorption (JSON)
            abs_str = await self.redis.get("rt:micro:absorption")
            abs_score = 0.0
            abs_dir = "neutral"
            if abs_str:
                abs_data = json.loads(abs_str)
                abs_score = float(abs_data.get("score", 0))
                abs_dir = abs_data.get("direction", "neutral")

            return {
                "price": price,
                "velocity_ts": int(vel.get("ts", 0)),
                "move_10s": float(vel.get("move_10s", 0)),
                "move_30s": float(vel.get("move_30s", 0)),
                "move_60s": float(vel.get("move_60s", 0)),
                "range_60s": float(vel.get("range_60s", 0)),
                "spread": float(spread_str),
                "book_imbalance": float(imbal_str),
                "trade_burst": float(burst_str),
                "bs_ratio_5s": float(bs5_str),
                "bs_ratio_30s": float(bs30_str) if bs30_str else 0.5,
                "ofi": float(ofi_str) if ofi_str else 0,
                "momentum_quality": float(mom_str) if mom_str else 0,
                "delta_accel": float(accel_str) if accel_str else 0,
                "delta_div": int(div_str) if div_str else 0,
                "price_impact": float(impact_str) if impact_str else 0,
                "hurst": float(hurst_str) if hurst_str else 0.5,
                "vpin": float(vpin_str) if vpin_str else 0.3,
                "parkinson_vol": float(pvol_str) if pvol_str else 0,
                "funding_rate": float(funding_str) if funding_str else 0,
                "whale_bias": float(whale_str) if whale_str else 0,
                "cvd_5m": float(cvd_str) if cvd_str else 0,
                "vwap_deviation": vwap_dev,
                "absorption_score": abs_score,
                "absorption_dir": abs_dir,
            }

        except Exception as e:
            logger.debug(f"feature read error: {e}")
            return None
