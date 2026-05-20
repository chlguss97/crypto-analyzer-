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

        # OU Z-Score (mean reversion — replaces simple VWAP snap)
        self.ou_entry_z = cfg.get("ou_entry_z", 2.0)

        # Common filters
        self.max_spread = cfg.get("max_spread", 2.0)
        self.min_micro_confidence = cfg.get("min_micro_confidence", 0.3)
        self.stale_threshold_ms = cfg.get("stale_threshold_ms", 5000)

        # VPIN 4단계 사이징 (Easley et al. 2012)
        self.vpin_size_mult = {
            "low": 1.0,      # < 0.3
            "medium": 0.5,   # 0.3~0.5
            "high": 0.25,    # 0.5~0.7
            "extreme": 0.0,  # > 0.7
        }

        # Welford normalizer + 워밍업
        self._normalizer = FeatureNormalizer(window=100)
        self._warmup_count = 0
        self._warmup_threshold = 100  # 100 샘플 전까지 시그널 억제

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

        # ── 3. Welford 워밍업 (100샘플 미달 시 시그널 억제) ──
        self._warmup_count += 1
        if self._warmup_count < self._warmup_threshold:
            return None

        # ── 4. Spread filter ──
        spread = features.get("spread", 999)
        if spread > self.max_spread:
            return None

        # ── 4.5. Book Shock 방어 (30% 깊이 급감) ──
        depth_shock = features.get("depth_shock", False)
        if depth_shock:
            return None

        # ── 5. Regime Gate (Hurst + VPIN 4단계) ──
        hurst = features.get("hurst", 0.5)
        vpin = features.get("vpin", 0.3)
        hurst_available = features.get("hurst_available", False)

        # VPIN 4단계 사이징 (Easley et al. 2012)
        if vpin >= 0.7:
            return None  # Extreme → 거래 금지
        elif vpin >= 0.5:
            vpin_size_mult = 0.25
        elif vpin >= 0.3:
            vpin_size_mult = 0.5
        else:
            vpin_size_mult = 1.0

        # Hurst Regime (데드존 세분화)
        hurst_size_mult = 1.0
        if hurst_available:
            if hurst > self.hurst_momentum:
                regime = "momentum"
            elif hurst < self.hurst_mean_revert:
                regime = "mean_revert"
            elif 0.45 <= hurst <= 0.55:
                regime = "dead_zone"
                hurst_size_mult = 0.25  # 랜덤워크 → 0.25x (차단 대신 축소)
            else:
                regime = "neutral"
                hurst_size_mult = 0.5
        else:
            regime = "both"

        # ── 6. Microstructure Regime → confidence multiplier ──
        micro_conf = self._calc_micro_confidence(features)
        if micro_conf < self.min_micro_confidence:
            return None

        # ── 7. Z-Score 정규화 (프로: 모든 피처를 상대값으로) ──
        raw_features = {
            "ofi": features.get("ofi", 0),
            "book_imbalance": features.get("book_imbalance", 0),
            "trade_burst": features.get("trade_burst", 1.0),
            "bs_ratio_5s": features.get("bs_ratio_5s", 0.5),
            "bs_ratio_30s": features.get("bs_ratio_30s", 0.5),
            "momentum_quality": features.get("momentum_quality", 0),
            "delta_accel": features.get("delta_accel", 0),
            "cvd_5m": features.get("cvd_5m", 0),
            "move_10s": features.get("move_10s", 0),
            "move_30s": features.get("move_30s", 0),
        }
        z_features = self._normalizer.update_all(raw_features)

        # ── 8. Signal Check (앙상블: Burst + OU + CVD) ──
        signals_found = []

        # Signal A: Burst (momentum / both / neutral / dead_zone)
        if regime in ("momentum", "both", "neutral", "dead_zone"):
            burst_sig = self._check_burst(features, z_features, price)
            if burst_sig:
                signals_found.append(burst_sig)

        # Signal B: OU Z-Score 평균회귀 (mean_revert / both / neutral / dead_zone)
        if regime in ("mean_revert", "both", "neutral", "dead_zone"):
            ou_sig = self._check_ou_reversion(features, z_features, price)
            if ou_sig:
                signals_found.append(ou_sig)

        # 앙상블 합의 — 복수 시그널 있으면 방향 일치 확인
        signal = None
        if len(signals_found) == 0:
            pass
        elif len(signals_found) == 1:
            signal = signals_found[0]
        else:
            # 2개 이상: 방향 일치 → 강화, 불일치 → 차단
            dirs = [s["direction"] for s in signals_found]
            if len(set(dirs)) == 1:
                # 합의 → 가장 강한 시그널 + 보너스
                signal = max(signals_found, key=lambda s: s["strength"])
                signal["strength"] = min(3.0, signal["strength"] + 0.5)
                signal["ensemble_agree"] = True
            else:
                signal = None  # 불일치 → 차단

        if signal is None:
            return None

        # ── 8.5. CVD Divergence Override (강도 > 0.3) ──
        # Bouchaud et al. (2004): 가격↑+CVD↓ = 매수 소진 → 반전
        delta_div = features.get("delta_div", 0)
        cvd_5m = features.get("cvd_5m", 0)
        if delta_div != 0 and signal["type"] == "micro_burst":
            # CVD divergence 강도 계산 (|cvd_5m| 정규화)
            cvd_z = abs(z_features.get("cvd_5m", 0))
            if cvd_z > 0.3:
                # divergence가 모멘텀 방향과 반대 → 오버라이드
                div_dir = "long" if delta_div == 1 else "short"
                if div_dir != signal["direction"]:
                    logger.info(
                        f"[SCALP] CVD divergence override: {signal['direction']}→{div_dir} (z={cvd_z:.2f})"
                    )
                    signal["direction"] = div_dir
                    signal["type"] = "cvd_override"
                    signal["strength"] = min(3.0, signal["strength"])

        # ── 9. Build result (VPIN/Hurst 사이즈 배수 포함) ──
        combined_size_mult = round(vpin_size_mult * hurst_size_mult * micro_conf, 4)
        signal["price"] = price
        signal["regime"] = regime
        signal["hurst"] = round(hurst, 4)
        signal["vpin"] = round(vpin, 4)
        signal["micro_confidence"] = round(micro_conf, 2)
        signal["vpin_size_mult"] = vpin_size_mult
        signal["hurst_size_mult"] = hurst_size_mult
        signal["combined_size_mult"] = combined_size_mult
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
            "price_impact": round(features.get("price_impact", 0), 1),
            "cvd_5m_raw": round(features.get("cvd_5m", 0), 2),
            "funding_rate": round(features.get("funding_rate", 0), 6),
            "hurst": round(hurst, 4),
            "vpin": round(vpin, 4),
            "parkinson_vol": round(features.get("parkinson_vol", 0), 6),
            "micro_confidence": round(micro_conf, 2),
        }

        self._last_signal_ts = time.time()
        return signal

    def _check_burst(self, feat: dict, z_feat: dict, price: float) -> dict | None:
        """Signal A: Micro-Momentum Burst (z-score 기반 — 프로 동일)"""
        move_10s = feat.get("move_10s", 0)
        move_30s = feat.get("move_30s", 0)
        move_60s = feat.get("move_60s", 0)
        z_move_10s = z_feat.get("move_10s", 0)
        z_move_30s = z_feat.get("move_30s", 0)

        # 방향 결정 (10s + 30s 동일 방향)
        if move_10s > 0 and move_30s > 0:
            direction = "long"
        elif move_10s < 0 and move_30s < 0:
            direction = "short"
        else:
            return None

        # z-score 기반 속도 (절대값 아닌 상대값 — "평소 대비 얼마나 큰 움직임인가")
        if abs(z_move_10s) < 1.5:
            return None  # 10s 이동이 평소의 1.5σ 미만
        if abs(z_move_30s) < 1.0:
            return None  # 30s 이동이 평소의 1.0σ 미만

        # 신선도 (10s가 30s의 40%+ → 가속 중)
        abs_10s = abs(move_10s)
        abs_30s = abs(move_30s)
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

        # 소진 필터: 60s 이동이 10s 이동의 3배 이상 → 이미 큰 움직임 후 추격
        if abs_30s > 0 and abs(move_60s) > abs_30s * 3:
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

    def _check_ou_reversion(self, feat: dict, z_feat: dict, price: float) -> dict | None:
        """Signal B: OU Z-Score 평균회귀 (Uhlenbeck & Ornstein 1930)
        z < -2.0 → long (과매도), z > +2.0 → short (과매수)
        """
        ou_z = feat.get("ou_zscore", 0)

        if abs(ou_z) < self.ou_entry_z:
            return None

        direction = "long" if ou_z < 0 else "short"

        # Book imbalance 확인 (회귀 방향 지지)
        book_imbal = feat.get("book_imbalance", 0)
        if direction == "long" and book_imbal < 0.10:
            return None
        if direction == "short" and book_imbal > -0.10:
            return None

        # Delta divergence 추가 확인 (있으면 보너스)
        delta_div = feat.get("delta_div", 0)

        # 스프레드 타이트
        if feat.get("spread", 999) > 1.5:
            return None

        strength = 1.0
        if abs(ou_z) > 3.0:
            strength += 0.5  # 극단 이탈
        if (direction == "long" and delta_div == 1) or (direction == "short" and delta_div == -1):
            strength += 0.3  # CVD 다이버전스 확인
        # Absorption 보너스
        absorption = feat.get("absorption_score", 0)
        if absorption >= 1.0 and feat.get("absorption_dir", "") == direction:
            strength += 0.3

        return {"type": "ou_reversion", "direction": direction, "strength": round(strength, 2)}

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
            ou_z_str = await self.redis.get("rt:micro:ou_zscore")
            resilience_str = await self.redis.get("rt:micro:book_resilience")
            depth_shock_str = await self.redis.get("rt:micro:depth_shock")

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
                "hurst_available": hurst_str is not None,
                "vpin": float(vpin_str) if vpin_str else 0.3,
                "parkinson_vol": float(pvol_str) if pvol_str else 0,
                "funding_rate": float(funding_str) if funding_str else 0,
                "whale_bias": float(whale_str) if whale_str else 0,
                "cvd_5m": float(cvd_str) if cvd_str else 0,
                "ou_zscore": float(ou_z_str) if ou_z_str else 0,
                "book_resilience": float(resilience_str) if resilience_str else 1.0,
                "depth_shock": depth_shock_str == "1" if depth_shock_str else False,
                "vwap_deviation": vwap_dev,
                "absorption_score": abs_score,
                "absorption_dir": abs_dir,
            }

        except Exception as e:
            logger.debug(f"feature read error: {e}")
            return None
