"""
EnsembleDetector — 4모델 앙상블 시그널 감지 (프로 원문 일치)

4개 독립 모델이 각각 방향(-1/0/+1)을 출력 → 앙상블 신뢰도로 거래 결정.
  [1] OFI Model — Order Flow Imbalance (Cont et al. 2014)
  [2] OU Model — Ornstein-Uhlenbeck Z-Score (Uhlenbeck & Ornstein 1930)
  [3] CVD Model — Cumulative Volume Delta Divergence (Bouchaud et al. 2004)
  [4] LSTM Model — DeepLOB fallback: tanh(mean(z_features))

앙상블 신뢰도: conf = max(0, 1 - σ/|d̄|)
  - conf ≥ 0.6 → 거래 (프로: "가장 영향력 있는 파라미터")
  - conf < 0.6 → 차단

References:
  - Vadim.blog: ML Features for Crypto Scalping
  - Easley, López de Prado, & O'Hara (2012): VPIN
  - Hurst (1951): R/S analysis
"""

import json
import logging
import math
import time

from src.data.storage import RedisClient
from src.strategy.welford import FeatureNormalizer

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════
#  4개 독립 시그널 모델
# ══════════════════════════════════════════

class OFIModel:
    """Model 1: Order Flow Imbalance — 호가 흐름 기반 모멘텀"""

    def evaluate(self, feat: dict, z_feat: dict) -> int:
        """Returns: +1 (long), -1 (short), 0 (no signal)"""
        ofi_z = z_feat.get("ofi", 0)

        # OFI z-score로 방향 결정 (≥1.0 threshold)
        if ofi_z >= 1.0:
            direction = 1
        elif ofi_z <= -1.0:
            direction = -1
        else:
            return 0

        # 체결 급증 확인 (trade_burst ≥ 1.8)
        if feat.get("trade_burst", 0) < 1.8:
            return 0

        # BS ratio 5s z-score 방향 일치
        z_bs5 = z_feat.get("bs_ratio_5s", 0)
        if direction == 1 and z_bs5 < 0.5:
            return 0
        if direction == -1 and z_bs5 > -0.5:
            return 0

        return direction


class OUModel:
    """Model 2: OU Z-Score 평균회귀"""

    def __init__(self, entry_z: float = 2.0):
        self.entry_z = entry_z

    def evaluate(self, feat: dict, z_feat: dict) -> int:
        """Returns: +1 (long=oversold), -1 (short=overbought), 0 (no signal)"""
        ou_z = feat.get("ou_zscore", 0)

        if abs(ou_z) < self.entry_z:
            return 0

        direction = 1 if ou_z < 0 else -1  # 과매도→long, 과매수→short

        # Book imbalance z-score 확인
        z_imbal = z_feat.get("book_imbalance", 0)
        if direction == 1 and z_imbal < 0.3:
            return 0
        if direction == -1 and z_imbal > -0.3:
            return 0

        # Spread z-score 과도 시 차단
        z_spread = z_feat.get("spread_bps", 0)
        if z_spread > 2.0:
            return 0

        return direction


class CVDModel:
    """Model 3: CVD Divergence — 가격/볼륨 괴리 감지"""

    def evaluate(self, feat: dict, z_feat: dict) -> int:
        """Returns: +1 (long), -1 (short), 0 (no signal)"""
        delta_div = feat.get("delta_div", 0)
        z_cvd = z_feat.get("cvd_5m", 0)

        # 1차: delta_div 기반 divergence (가격↑CVD↓ = short, 가격↓CVD↑ = long)
        if delta_div != 0 and abs(z_cvd) > 0.3:
            return delta_div  # +1 or -1

        # 2차: CVD 모멘텀 (강한 z-score)
        if abs(z_cvd) >= 1.5:
            direction = 1 if z_cvd > 0 else -1
            # BS ratio 30s 확인
            z_bs30 = z_feat.get("bs_ratio_30s", 0)
            if direction == 1 and z_bs30 > 0.3:
                return direction
            if direction == -1 and z_bs30 < -0.3:
                return direction

        return 0


class LSTMModel:
    """Model 4: LSTM (DeepLOB) — Phase 1: tanh fallback, Phase 3: 실제 모델"""

    def __init__(self):
        self.model = None  # Phase 3에서 DeepLOB5 로드

    def evaluate(self, feat: dict, z_feat: dict) -> int:
        """Returns: +1 (long), -1 (short), 0 (no signal)"""
        if self.model is not None:
            # Phase 3: DeepLOB5 추론 (TODO)
            pass

        # Fallback: tanh(mean(z_features)) — 프로 원문
        z_vals = [
            z_feat.get("ofi", 0),
            z_feat.get("book_imbalance", 0),
            z_feat.get("cvd_5m", 0),
            z_feat.get("bs_ratio_5s", 0),
        ]
        mean_z = sum(z_vals) / len(z_vals) if z_vals else 0
        score = math.tanh(mean_z)

        if score > 0.3:
            return 1
        elif score < -0.3:
            return -1
        return 0


# ══════════════════════════════════════════
#  앙상블 신뢰도 계산
# ══════════════════════════════════════════

def ensemble_confidence(directions: list[int]) -> tuple[float, float]:
    """
    프로 앙상블 신뢰도 공식.
    conf = max(0, 1 - σ(active) / |d̄(active)|)

    Returns: (confidence, d_bar)
    """
    active = [d for d in directions if d != 0]
    if not active:
        return 0.0, 0.0

    d_bar = sum(active) / len(active)
    if abs(d_bar) < 1e-9:
        return 0.0, 0.0

    variance = sum((d - d_bar) ** 2 for d in active) / len(active)
    sigma = math.sqrt(variance)
    conf = max(0.0, 1.0 - sigma / abs(d_bar))
    return conf, d_bar


# ══════════════════════════════════════════
#  EnsembleDetector (ScalpDetector 대체)
# ══════════════════════════════════════════

class EnsembleDetector:
    """4모델 앙상블 시그널 감지기 — 프로 원문 일치"""

    def __init__(self, redis: RedisClient, config: dict):
        self.redis = redis
        cfg = config.get("scalp", {})

        # 앙상블 신뢰도 임계값 (프로: 0.6 = "가장 영향력 있는 파라미터")
        self.conf_threshold = cfg.get("ensemble_conf", 0.6)

        # VPIN extreme (사이징용, 시그널 게이팅 아님 — 프로는 0x = 킬스위치)
        self.vpin_extreme = cfg.get("vpin_extreme", 0.85)

        # 프리게이트 (시그널 전 필터)
        self.min_micro_confidence = cfg.get("min_micro_confidence", 0.3)
        self.stale_threshold_ms = cfg.get("stale_threshold_ms", 5000)

        # 반전 시 더 높은 conf 요구 (포지션 보유 중 반전 = 더 확실해야 함)
        self.conf_reversal = cfg.get("ensemble_conf_reversal", 0.9)

        # 시그널 지속성: 같은 방향 N회 연속 투표해야 진입
        self.persistence_required = cfg.get("signal_persistence", 3)

        # 4개 독립 모델
        self.ofi_model = OFIModel()
        self.ou_model = OUModel(entry_z=cfg.get("ou_entry_z", 2.0))
        self.cvd_model = CVDModel()
        self.lstm_model = LSTMModel()

        # Welford normalizer + 워밍업
        self._normalizer = FeatureNormalizer(window=100)
        self._warmup_count = 0
        self._warmup_threshold = 100

        # 시그널 지속성 추적
        self._prev_direction = 0  # 이전 앙상블 방향
        self._direction_streak = 0  # 연속 동일 방향 횟수

        # 디버그 로깅
        self._last_debug_ts = 0
        self._gate_stats = {
            "total": 0, "warmup": 0, "spread": 0, "vpin": 0,
            "micro": 0, "low_conf": 0, "persistence": 0, "passed": 0,
            "ofi": 0, "ou": 0, "cvd": 0, "lstm": 0,
        }

    async def evaluate(self) -> dict | None:
        """
        Redis 피처 → 프리게이트 → 4모델 평가 → 앙상블 신뢰도 → 시그널.
        반환: {type, direction, confidence, votes, price, features, ...} or None
        """
        self._gate_stats["total"] += 1
        self._log_gate_debug()

        # ── 1. 피처 수집 ──
        features = await self._read_features()
        if features is None:
            return None

        price = features.get("price", 0)
        if price <= 0:
            return None

        # ── 2. Staleness ──
        velocity_ts = features.get("velocity_ts", 0)
        now_ms = int(time.time() * 1000)
        if velocity_ts > 0 and (now_ms - velocity_ts) > self.stale_threshold_ms:
            return None

        # ── 3. Welford 워밍업 ──
        self._warmup_count += 1
        if self._warmup_count < self._warmup_threshold:
            self._gate_stats["warmup"] += 1
            return None

        # ── 4. Spread 프리게이트 ──
        spread = features.get("spread", 999)
        spread_bps = spread / price * 10000 if price > 0 else 999
        if spread_bps > 3.0:
            self._gate_stats["spread"] += 1
            return None

        # ── 5. Book Shock ──
        if features.get("depth_shock", False):
            return None

        # ── 6. VPIN extreme → 킬스위치 ──
        vpin = features.get("vpin", 0.3)
        if vpin >= self.vpin_extreme:
            self._gate_stats["vpin"] += 1
            return None

        # ── 7. Micro confidence 프리게이트 ──
        micro_conf = self._calc_micro_confidence(features)
        if micro_conf < self.min_micro_confidence:
            self._gate_stats["micro"] += 1
            return None

        # ── 8. Z-Score 정규화 ──
        price_for_bps = price if price > 0 else 1
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
            "spread_bps": features.get("spread", 0) / price_for_bps * 10000,
            "absorption": features.get("absorption_score", 0),
        }
        z_features = self._normalizer.update_all(raw_features)

        # ── 9. 4모델 독립 평가 ──
        d1 = self.ofi_model.evaluate(features, z_features)
        d2 = self.ou_model.evaluate(features, z_features)
        d3 = self.cvd_model.evaluate(features, z_features)
        d4 = self.lstm_model.evaluate(features, z_features)

        votes = [d1, d2, d3, d4]

        # 모델별 시그널 카운트 (디버그)
        if d1 != 0: self._gate_stats["ofi"] += 1
        if d2 != 0: self._gate_stats["ou"] += 1
        if d3 != 0: self._gate_stats["cvd"] += 1
        if d4 != 0: self._gate_stats["lstm"] += 1

        # ── 10. 앙상블 신뢰도 ──
        conf, d_bar = ensemble_confidence(votes)

        if conf < self.conf_threshold:
            self._gate_stats["low_conf"] += 1
            self._direction_streak = 0
            self._prev_direction = 0
            return None

        direction_int = 1 if d_bar > 0 else -1
        direction = "long" if d_bar > 0 else "short"

        # ── 10.5. 시그널 지속성 (같은 방향 N회 연속 필요) ──
        if direction_int == self._prev_direction:
            self._direction_streak += 1
        else:
            self._direction_streak = 1
            self._prev_direction = direction_int

        if self._direction_streak < self.persistence_required:
            self._gate_stats["persistence"] += 1
            return None

        # ── 11. 사이징 배수 (VPIN + Hurst + micro_conf) ──
        hurst = features.get("hurst", 0.5)
        hurst_available = features.get("hurst_available", False)

        # VPIN 사이징
        if vpin >= 0.6:
            vpin_size_mult = 0.25
        elif vpin >= 0.4:
            vpin_size_mult = 0.5
        else:
            vpin_size_mult = 1.0

        # Hurst 사이징 (시그널 게이팅 아님, 사이즈만 조절)
        hurst_size_mult = 1.0
        if hurst_available:
            if 0.45 <= hurst <= 0.55:
                hurst_size_mult = 0.25  # dead zone
            elif 0.4 <= hurst <= 0.6:
                hurst_size_mult = 0.5   # neutral
            # momentum (>0.6) / mean_revert (<0.4) = 1.0x

        combined_size_mult = round(vpin_size_mult * hurst_size_mult * micro_conf, 4)

        # Regime label (모니터링용)
        if not hurst_available:
            regime = "unknown"
        elif hurst > 0.6:
            regime = "momentum"
        elif hurst < 0.4:
            regime = "mean_revert"
        elif 0.45 <= hurst <= 0.55:
            regime = "dead_zone"
        else:
            regime = "neutral"

        # ── 12. 시그널 빌드 ──
        self._gate_stats["passed"] += 1

        return {
            "type": "ensemble",
            "direction": direction,
            "confidence": round(conf, 4),
            "votes": votes,  # [ofi, ou, cvd, lstm]
            "price": price,
            "regime": regime,
            "hurst": round(hurst, 4),
            "vpin": round(vpin, 4),
            "micro_confidence": round(micro_conf, 2),
            "vpin_size_mult": vpin_size_mult,
            "hurst_size_mult": hurst_size_mult,
            "combined_size_mult": combined_size_mult,
            "features": {
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
            },
        }

    # ══════════════════════════════════════════
    #  유틸리티 (기존 코드 유지)
    # ══════════════════════════════════════════

    def _log_gate_debug(self):
        """30초마다 게이트 + 모델 투표 통계"""
        now = time.time()
        if now - self._last_debug_ts < 30:
            return
        self._last_debug_ts = now
        s = self._gate_stats
        if s["total"] > 0:
            logger.info(
                f"[ENSEMBLE] {s['total']}회 | warmup:{s['warmup']} spread:{s['spread']} "
                f"vpin:{s['vpin']} micro:{s['micro']} low_conf:{s['low_conf']} "
                f"persist:{s['persistence']} | "
                f"votes(ofi:{s['ofi']} ou:{s['ou']} cvd:{s['cvd']} lstm:{s['lstm']}) "
                f"→ passed:{s['passed']}"
            )
        for k in self._gate_stats:
            self._gate_stats[k] = 0

    def _calc_micro_confidence(self, feat: dict) -> float:
        """마이크로스트럭처 레짐 신뢰도 (0.0~1.0)"""
        spread = feat.get("spread", 1.0)
        imbal = abs(feat.get("book_imbalance", 0))
        burst = feat.get("trade_burst", 1.0)

        if spread < 0.5:
            s_score = 1.0
        elif spread < 2.0:
            s_score = 0.7
        else:
            s_score = 0.2

        if imbal < 0.3:
            d_score = 1.0
        elif imbal < 0.7:
            d_score = 0.6
        else:
            d_score = 0.3

        if burst > 1.5:
            a_score = 1.0
        elif burst > 0.7:
            a_score = 0.6
        else:
            a_score = 0.2

        return round(s_score * 0.4 + d_score * 0.3 + a_score * 0.3, 2)

    async def _read_features(self) -> dict | None:
        """Redis에서 모든 피처 읽기"""
        try:
            price_str = await self.redis.get("rt:price:BTC-USDT-SWAP")
            if not price_str:
                return None
            price = float(price_str)

            vel = await self.redis.hgetall("rt:velocity:BTC-USDT-SWAP")
            if not vel:
                return None

            burst_str = await self.redis.get("rt:micro:trade_burst")
            bs5_str = await self.redis.get("rt:micro:bs_ratio_5s")
            bs30_str = await self.redis.get("rt:micro:bs_ratio_30s")
            spread_str = await self.redis.get("rt:micro:spread")
            imbal_str = await self.redis.get("rt:micro:book_imbalance")

            if not all([burst_str, bs5_str, spread_str, imbal_str]):
                return None

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

            vwap_str = await self.redis.get("rt:micro:vwap")
            vwap_dev = 0.0
            if vwap_str:
                vwap_data = json.loads(vwap_str)
                vwap_dev = float(vwap_data.get("deviation_pct", 0))

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
