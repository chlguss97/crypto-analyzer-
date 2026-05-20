# SPEC v3 — BTC 마이크로스트럭처 스캘핑 엔진

> 작성: 2026-05-20 (v2 → v3 전면 재설계)  
> 목표: 횡보장에서도 수익, 레버리지 변동성 활용, 3%+ 마진 스캘핑  
> 원칙: 4계층 파이프라인 (Raw → Feature → Regime → ML) + 빠른 진출입  
> 참조: Vadim.blog ML Features for Crypto Scalping, Cont et al. (2014) OFI

---

## 0. v2 실패 교훈 → v3 설계 근거

| # | v2 실패 | 원인 | v3 대응 |
|---|---------|------|---------|
| 1 | 횡보에서 SL 반복 | 5분봉 모멘텀 = 횡보에서 노이즈 | 실시간 마이크로스트럭처 (500ms) |
| 2 | 러너 3건이 수익 85% | 큰 트렌드에 의존 | TP 0.20% 빠른 확정 (러너 없음) |
| 3 | ML 필터 무력화 | 피처 품질 낮음 (캔들 기반) | 20종 마이크로스트럭처 피처 |
| 4 | SL algo OKX 소실 -$44 | SL 등록 후 OKX가 자체 취소 | 5초 self-heal + 3회 소실 강제청산 |
| 5 | Shadow WR 1.7%에도 매매 | 시장 상태 무시 | Hurst Regime Gate + Shadow WR 게이트 |
| 6 | 보유 30분~24시간 | 레짐 전환에 노출 | 최대 5분 (시간 정지) |

---

## 1. 아키텍처 — 4계층 파이프라인

```
[1층] Raw Data
  Binance aggTrade (CVD/VPIN/Whale)
  OKX trades (Velocity/마이크로스트럭처)
  OKX books5 (OFI/호가 불균형)
  OKX candles (Hurst/Parkinson)
        ↓
[2층] Feature Engine (Redis, 2초 갱신)
  OFI 멀티레벨, CVD, VPIN, Hurst R/S, Parkinson Vol
  trade_burst, bs_ratio, momentum_quality, VWAP, delta_div
  Welford Z-Score 정규화 (100개 윈도우)
        ↓
[3층] Regime Gate
  Hurst > 0.6 → 모멘텀 스캘핑 (Burst 시그널)
  Hurst < 0.4 → 평균회귀 스캘핑 (VWAP Snap)
  Hurst 0.4~0.6 → 랜덤워크 → 거래 금지
  VPIN > 0.7 → 극단 독성 → 거래 금지
        ↓
[4층] ML Scorer (Phase A: 규칙, Phase B: XGBoost)
  20종 피처 → P(Win) ≥ 0.55 → Go
        ↓
[실행] ScalpManager
  진입: post-only (maker 0.02%)
  TP: +0.20% (서버 limit-on-trigger)
  SL: -0.15% (서버 market-on-trigger)
  시간 정지: 3분(수익시) / 5분(최대)
```

---

## 2. 데이터 소스

### 2.1 Binance Futures

| 데이터 | 채널/REST | Redis 키 | 갱신 |
|--------|----------|----------|------|
| CVD 5m/15m/1h | aggTrade WS | `flow:combined:cvd_*` | 100체결 or 2초 |
| Whale Bias | aggTrade WS | `flow:combined:whale_bias` | 고래 발생 시 |
| 청산 | REST 5초 | `flow:liq:1m_*` | 5초 |
| 펀딩비 | REST 30초 | `rt:funding:BTC-USDT-SWAP` | 30초 |
| OI | REST 30초 | `rt:oi:BTC-USDT-SWAP` | 30초 |
| Vol Ratio | aggTrade | `bn:vol_ratio_1m` | 2초 |

### 2.2 OKX WebSocket

| 채널 | Redis 키 | 용도 |
|------|----------|------|
| tickers | `rt:price:*`, `rt:ticker:*` | 현재가, bid/ask |
| trades | `rt:velocity:*` (10s/30s/60s) | 가격 변속도 |
| trades | `rt:micro:*` (15종) | 마이크로스트럭처 피처 |
| books5 | `rt:micro:book_imbalance`, `rt:micro:ofi` | OFI, 호가 불균형 |
| candle 7TF | DB + `ch:kline:ready` | Hurst/Parkinson 계산 |

### 2.3 신규 피처 (v3)

| 피처 | Redis 키 | 계산 | 참조 |
|------|----------|------|------|
| OFI 멀티레벨 | `rt:micro:ofi` | books5 ΔBid - ΔAsk (5레벨) | Cont et al. (2014) |
| Hurst Exponent | `rt:regime:hurst` | R/S 분석, 5분봉 20~50개 | Hurst (1951) |
| Parkinson Vol | `rt:micro:parkinson_vol` | √[Σ(ln H/L)² / 4n·ln2] | Parkinson (1980) |
| Welford Z-Score | ScalpDetector 내부 | 100개 윈도우 온라인 정규화 | Welford (1962) |
| VPIN | `rt:micro:vpin` (미구현) | 볼륨 버킷 |V_buy-V_sell|/V | Easley et al. (2012) |

---

## 3. ScalpDetector — 시그널 감지

### 구현: `src/strategy/scalp_detector.py`

Redis에서만 읽음. DB/캔들 접근 없음. 500ms 폴링.

### 3.1 Signal A: Micro-Momentum Burst (Hurst > 0.6)

| 조건 | 피처 | 임계값 |
|------|------|--------|
| 가격 가속 | move_10s | ≥ price × 0.06% |
| 지속 이동 | move_30s | 같은 방향, ≥ price × 0.10% |
| 신선도 | move_10s/move_30s | ≥ 0.4 |
| 호가 지지 | ofi_zscore | ≥ ±1.0 |
| 체결 급증 | trade_burst | ≥ 1.8 |
| 플로우 | bs_ratio_5s | ≥ 0.60 / ≤ 0.40 |
| 유동성 | spread | ≤ $2.0 |
| 소진 필터 | move_60s | < price × 0.15% |

### 3.2 Signal B: VWAP Snap (Hurst < 0.4)

| 조건 | 피처 | 임계값 |
|------|------|--------|
| VWAP 이탈 | vwap_deviation | ≥ 0.12% |
| 다이버전스 | delta_div | 1 (long) / -1 (short) |
| 흡수 | absorption_score | ≥ 1.0 |
| 호가 지지 | book_imbalance | ≥ ±0.15 |
| 유동성 | spread | ≤ $1.5 |

---

## 4. ML DecisionEngine

### 구현: `src/strategy/ml_engine.py`

**Phase A** (< 300건): 무조건 Go (데이터 수집)  
**Phase B** (300건+): XGBoost, P(Win) ≥ 0.55 → Go

### 피처 (20종)

```
[z-score 정규화]
z_ofi, z_book_imbalance, z_trade_burst, z_bs_ratio_5s, z_bs_ratio_30s,
z_momentum_quality, z_delta_accel, z_cvd_5m

[원시]
spread, vwap_deviation, delta_div, absorption_score, whale_bias, price_impact

[레짐]
hurst, vpin, parkinson_vol, micro_confidence

[플로우]
cvd_5m_raw, funding_rate
```

### 학습 사이클
- 300건 도달 → Walk-Forward 80/20 학습
- OOS accuracy ≥ 52% → Phase B 활성화
- 100건마다 재학습

---

## 5. 청산 로직

| 유형 | 가격 | 넷 마진 (20x) | 방식 |
|------|------|--------------|------|
| TP | +0.20% | **+3.2%** | 서버 limit-on-trigger (maker) |
| SL | -0.15% | **-4.4%** | 서버 market-on-trigger (taker) |

**시간 정지**: 180초(수익/본전→즉시), 300초(무조건)

**러너 없음, 부분청산 없음.** 100% TP or 100% SL or Time.

**손익분기 승률: 57.9%**

---

## 6. 리스크 컨트롤

| 항목 | 값 |
|------|-----|
| 레버리지 | 20x 고정 |
| 마진 | balance × 80% |
| 연패 축소 | 2:80%, 3:60%, 5:40%, 7:25% |
| 쿨다운 | 승 30초, 패 90초, 3연패 10분, 5연패 60분 |
| 시간당 최대 | 8건 |
| 진입 간격 | 60초 |
| BOT_KILL | -20% DD |
| Shadow WR | 4시간 < 20% → 매매 정지 |

---

## 7. DB 스키마 (scalp.db)

```sql
-- candles (변경 없음)
CREATE TABLE candles (...);

-- scalp_signals (Shadow + ML 라벨링)
CREATE TABLE scalp_signals (
    id, ts, signal_type, direction, price, features,
    regime, hurst, vpin, ml_prob, ml_go,
    entry_executed, reject_reason,
    label, barrier_hit, pnl_pct, resolve_ts, reach_pct, mae_pct
);

-- scalp_trades (실거래)
CREATE TABLE scalp_trades (
    id, signal_id, direction, entry_price, exit_price,
    entry_time, exit_time, exit_reason,
    size_btc, leverage, pnl_usdt, pnl_pct, fee_total,
    hold_sec, regime, hurst, features_snapshot
);
```

---

## 8. 파일 구조

```
src/
  data/
    binance_stream.py    — Binance aggTrade/REST (CVD/Whale/Liq/Funding)
    ws_stream.py         — OKX WS (trades/tickers/books/candles) + 마이크로스트럭처
    candle_collector.py  — OKX REST 캔들 백필
    storage.py           — SQLite (scalp.db) + Redis 래퍼
  strategy/
    scalp_detector.py    — 실시간 시그널 감지 (Redis only, 500ms)
    scalp_manager.py     — 스캘핑 포지션 관리 (TP/SL/TimeStop)
    ml_engine.py         — XGBoost Go/NoGo (Phase A→B)
    adaptive_params.py   — TP/SL 자동 보정
    welford.py           — Welford 온라인 z-score 정규화
  trading/
    executor.py          — OKX CCXT 주문 실행
    risk_manager.py      — BOT_KILL + 쿨다운
  monitoring/
    telegram_bot.py      — Telegram 알림/명령
    trade_logger.py      — JSONL 로깅
    dashboard.py         — FastAPI 대시보드
  utils/
    helpers.py           — 설정 로딩
  main.py               — ScalpEngine 오케스트레이션
```

---

## 9. JSONL 이벤트 타입

| 타입 | 설명 |
|------|------|
| `candidate` | 시그널 감지 (Shadow 포함) |
| `shadow_result` | Shadow 라벨 확정 (TP/SL/Time) |
| `scalp_entry` | 실거래 진입 |
| `scalp_exit` | 실거래 청산 |
| `gate_block` | 게이트 차단 (shadow_wr_low 등) |
| `hourly_snapshot` | 시간별 스냅샷 (잔고/레짐/ML) |
| `adaptive_update` | AdaptiveParams 갱신 |

---

## 10. 비동기 태스크 (12개)

| 태스크 | 주기 | 역할 |
|--------|------|------|
| ws_stream.start() | 상시 | OKX WS 데이터 수집 |
| binance_stream.start() | 상시 | Binance 데이터 수집 |
| periodic_candle_update | 30초 | REST 캔들 백업 |
| periodic_scalp_eval | 500ms | 시그널 감지 + ML + 실행 |
| periodic_scalp_position | 500ms | 포지션 관리 (시간정지/failsafe) |
| periodic_shadow_check | 5초 | Triple Barrier 라벨링 |
| periodic_ml_retrain | 300초 | ML 재학습 |
| periodic_daily_reset | 60초 | 일일 리셋 |
| periodic_heartbeat | 60초 | 헬스체크 + 스냅샷 |
| periodic_orphan_algo_sweeper | 120초 | 고아 알고 정리 |
| periodic_dashboard_commands | 5초 | Redis 명령 큐 |
| telegram.poll_commands | 상시 | 텔레그램 명령 |
