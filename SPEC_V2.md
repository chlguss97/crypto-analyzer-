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
| Hurst Exponent | `rt:regime:hurst` | R/S 동적 n/8,n/4,n/2,n 스케일 | Hurst (1951) |
| Parkinson/Realized Vol | `rt:micro:parkinson_vol` | Parkinson 50% + Realized 50% 블렌딩 | Parkinson (1980) |
| Welford Z-Score | ScalpDetector 내부 | 100개 윈도우 + 워밍업 100샘플 | Welford (1962) |
| VPIN | `rt:micro:vpin` | 볼륨 버킷 (1/N)Σ|V_buy-V_sell|/V, 4단계 사이징 | Easley et al. (2012) |
| OU Z-Score | `rt:micro:ou_zscore` | OLS 추정 + 0.93 감쇠 + z<-2/z>+2 진입 | Uhlenbeck & Ornstein (1930) |
| Book Resilience | `rt:micro:book_resilience` | EWMA α=0.15 + 30% shock 감지 | Kyle (1985) |

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

### 3.2 Signal B: OU Z-Score Reversion (Hurst < 0.4)

| 조건 | 피처 | 임계값 |
|------|------|--------|
| OU Z-Score | ou_zscore | abs ≥ 2.0σ (z<-2 long, z>+2 short) |
| 호가 지지 | book_imbalance | ≥ +0.10 (long) / ≤ -0.10 (short) |
| 유동성 | spread | ≤ $1.5 |
| 보너스 | delta_div | 방향 일치 시 +0.3 |
| 보너스 | absorption | ≥ 1.0 + 방향 일치 시 +0.3 |

OU 감쇠: 0.93 per update (비활성 시 시그널 약화)

### 3.3 CVD Divergence Override

CVD z-score > 0.3 시 모멘텀 시그널 방향을 반전 (Bouchaud et al. 2004)
- 가격↑ + CVD↓ = 매수 소진 → short으로 오버라이드
- 가격↓ + CVD↑ = 매도 소진 → long으로 오버라이드

### 3.4 앙상블 합의

Burst + OU 2시그널 동시 발생 시:
- 방향 일치 → strength +0.5 (강화)
- 방향 불일치 → 차단 (conf < 0.6 동등)

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

| 유형 | 계산 | 방식 |
|------|------|------|
| **TP** | k(2.0) × Parkinson/Realized Vol (동적) | 서버 limit-on-trigger (maker) |
| **SL** | k(2.0) × Parkinson/Realized Vol (동적) | 서버 market-on-trigger (taker) |
| **시그널 반전** | 반대 방향 시그널 발생 시 | 즉시 market 청산 |
| **시간 정지** | 180초(수익시) / 300초(최대) | market 청산 |

- TP/SL 범위: 0.1%~0.5% (변동성 적응)
- RR 최소 1.0 보장 (TP ≥ SL)
- **러너 없음, 부분청산 없음, 고정 % 없음**
- 프로 레퍼런스: SL은 k×vol, TP도 k×vol 또는 시그널 반전

---

## 6. 리스크 컨트롤

프로 레퍼런스 동일 — 인위적 제한 없이 시장 상태 기반 필터만 사용.

| 항목 | 방식 |
|------|------|
| 레버리지 | 20x 고정 |
| 마진 | balance × 80% × VPIN배수 × Hurst배수 × micro_conf |
| **VPIN 4단계** | Low 1.0x / Med 0.5x / High 0.25x / Extreme 0x (킬스위치) |
| **Hurst Regime** | momentum 1.0x / neutral 0.5x / dead_zone 0.25x |
| **Micro Regime** | spread × depth × activity (floor 0.2) |
| **Book Shock** | 깊이 30% 급감 → 진입 차단 |
| **앙상블 불일치** | Burst + OU 방향 불일치 → 차단 |
| **BOT_KILL** | -20% DD |
| **시그널 반전** | 반대 시그널 → 즉시 청산 |

~~쿨다운, 연패 축소, 시간당 제한, Shadow WR 게이트~~ → 제거 (VPIN/Hurst가 선행 필터)

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
