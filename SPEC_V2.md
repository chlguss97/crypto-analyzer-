# SPEC v2 — BTC 모멘텀 스캘핑 봇 최종 명세서

> 작성: 2026-04-28  
> 목표: 자본 $700~$1,400, 월 +20~30%, 추세장에 크게 먹고 횡보장에 쉬기  
> 원칙: 단순한 후보 감지 + ML이 진입 결정 + Maker 우선 실행  
> 기반: 기존 코드 리팩토링 (신규 파일 0개)

---

## 0. 실패 교훈 (이 설계의 근거)

| # | 실패 | 원인 | 이 설계의 대응 |
|---|------|------|---------------|
| 1 | 복잡한 룰(764줄 7셋업)이 나쁜 진입만 생성 | 조건 많으면 "쉬운 기회"(역추세)만 통과 | 후보 감지 ~50줄, 3종만 |
| 2 | 점수 시스템 차별력 없음 | 6.0~7.5 범위에 몰림 | 점수 폐지, ML Go/NoGo |
| 3 | ML이 장식 (±2점 보정) | 결정권 없이 보조만 | ML이 진입 최종 결정 |
| 4 | 역추세(PB/DIV/LVL) 전패 | 떨어지는데 삼 | 모멘텀 추종만 |
| 5 | RR 0.25 (이기면 작게 지면 크게) | SL -8% vs 실현 TP 낮음 | SL -5%, TP ATR기반(RR 1.3~2.4) |
| 6 | 수수료 > 수익 | taker 0.05% 양쪽 | maker 강제 (완료) |
| 7 | 게이트 13개가 기회 살해 | 과도한 필터 | 게이트 5개로 축소, 나머지는 ML이 학습 |

---

## 1. 아키텍처 개요

```
┌─ Binance (데이터 100%) ──────────────────────────────────────────┐
│  WebSocket: aggTrade, kline(1m~1w), forceOrder, miniTicker       │
│  수집: CVD, 고래, 청산, 캔들, 가격 → Redis + SQLite              │
└──────────────────────────────────────────────────────────────────┘
         ↓ (매 3초)
┌─ CandidateDetector ──────────────────────────────────────────────┐
│  A. Momentum Ignition  (큰 캔들 + 큰 거래량)      일 12~20건     │
│  B. Volatility Breakout (BB 스퀴즈→돌파)          일 3~6건      │
│  C. Liquidation Cascade ($500K+ 청산 폭주)        일 0~4건      │
│  합계: 일 15~30건 후보                                           │
└──────────────────────────────────────────────────────────────────┘
         ↓
┌─ FeatureExtractor (8→25 피처) ───────────────────────────────────┐
│  시장 상태를 숫자로 스냅샷 → signals 테이블 기록 (전수)            │
└──────────────────────────────────────────────────────────────────┘
         ↓
┌─ Risk Gate (5개만) ──────────────────────────────────────────────┐
│  1. 일일 -5%  2. DD -12%  3. 연패5→1h  4. 포지션1개  5. 간격30초 │
│  불통과 → signals에 reject_reason 기록 + shadow 추적 시작         │
└──────────────────────────────────────────────────────────────────┘
         ↓
┌─ ML DecisionEngine ──────────────────────────────────────────────┐
│  Phase A (<200건): 룰 필터 (CVD일치 + vol>avg)                   │
│  Phase B (200건+): GBM P(Win) > 55% → Go                       │
│  NoGo → shadow 추적 (Triple Barrier 라벨링)                      │
└──────────────────────────────────────────────────────────────────┘
         ↓
┌─ OKX Executor ───────────────────────────────────────────────────┐
│  약한(strength<1.5): post-only only (maker 0.02%)               │
│  강한(strength≥1.5): limit 허용 (taker 0.05%)                   │
│  SL/TP 알고 등록 → PositionManager                               │
└──────────────────────────────────────────────────────────────────┘
         ↓
┌─ PositionManager ────────────────────────────────────────────────┐
│  0~90초: Adverse Selection (가격+CVD+거래량 AND → 조기 탈출)      │
│  TP1 50% 청산 → 러너 트레일링                                    │
│  SL / 시간초과 → 전량 청산                                       │
│  결과 → signals 라벨 확정 + ML 버퍼                               │
└──────────────────────────────────────────────────────────────────┘
```

---

## 2. 데이터 — 전부 Binance, 매매만 OKX

### 2.1 Binance WebSocket (기존 binance_stream.py)

| 스트림 | Redis 키 | TTL | 용도 |
|--------|----------|-----|------|
| aggTrade | `flow:combined:cvd_5m/15m/1h` | 400/1200/4800s | CVD (피처 9,10) |
| aggTrade | `flow:combined:whale_bias` | 600s | 고래 편향 (피처 12) |
| miniTicker | `bn:price:BTCUSDT` | 30s | 기준 가격 |
| kline_1m~1w | DB + `bn:kline:*` | - | 캔들 (후보 감지 + 피처) |
| forceOrder | `flow:liq:surge` | 120s | 청산 캐스케이드 (후보 C) |
| forceOrder | `flow:liq:1m_total/long/short` | 120s | 청산 수치 (피처 13) |

### 2.2 추가 필요 (Phase 3)

| 스트림 | Redis 키 | 용도 |
|--------|----------|------|
| depth20@100ms | `bn:depth:BTCUSDT` | 호가 불균형 (피처 확장용) |

### 2.3 OKX (executor.py만 사용)

- `create_order` — 진입/청산 주문
- `fetch_positions` — 포지션 확인
- `fetch_balance` — 잔고 조회
- `private_post_trade_cancel_algos` — 알고 취소

> 차트 분석, 시그널 감지는 OKX API를 일절 사용하지 않음.

---

## 3. CandidateDetector — 3종 후보 감지

### 3.1 구현 위치: `src/strategy/flow_engine.py` (전면 교체)

```python
class CandidateDetector:
    """
    후보 감지기 — "시장이 움직이고 있는가?" 만 판단.
    복잡한 셋업/점수 없음. ML이 진입 결정.
    """
```

### 3.2 후보 A: Momentum Ignition

**근거:** Jegadeesh & Titman (1993) 모멘텀 프리미엄. Liu & Tsyvinski (2021) BTC 모멘텀.

```
감지 조건 (AND):
  1. 직전 완성 5m 캔들 body > ATR(14) × 0.8
  2. 해당 캔들 거래량 > 20봉 평균 × 1.3
  3. body / (high - low) > 0.6 (몸통 비율 — 꼬리 작음)

방향: 양봉 → long, 음봉 → short
strength: body / ATR (1.0 = 평균적, 2.0 = 매우 강함)

출력:
  {
    "type": "momentum",
    "direction": "long" | "short",
    "strength": float,     # body/ATR
    "price": float,        # 현재 가격
    "atr": float,
    "vol_ratio": float,    # 거래량/평균
  }
```

### 3.3 후보 B: Volatility Breakout

**근거:** Bollinger (2001), Turtle Trading (Dennis, 1983). Kang et al. (2021) BTC BB.

```
감지 조건 (AND):
  1. BB width percentile < 25% (최근 100봉 중 하위 25%)
  2. 현재 가격 > upper BB (long) or < lower BB (short)
  3. 돌파 캔들 거래량 > 20봉 평균 × 1.2

방향: upper 돌파 → long, lower 돌파 → short
strength: (price - BB_mid) / (BB_upper - BB_mid) (>1.0 = 돌파)

출력:
  {
    "type": "breakout",
    "direction": "long" | "short",
    "strength": float,     # BB 돌파 정도
    "price": float,
    "bb_width_pctl": float, # 스퀴즈 정도 (0~100)
    "vol_ratio": float,
  }
```

### 3.4 후보 C: Liquidation Cascade

**근거:** Schär (2021), Aramonte et al. (BIS, 2022) 강제청산 가격 영향.

```
감지 조건 (AND):
  1. flow:liq:1m_total > $500,000 (1분간 청산 총액)
  2. 한쪽 편중 > 80% (long_liq/total > 0.8 or short_liq/total > 0.8)
  3. 5m 가격 변동 > 0.2% (이미 움직이는 중)

방향: 롱 청산 폭주 → short, 숏 청산 폭주 → long
strength: total_liq / 1,000,000 (1.0 = $1M, 2.0 = $2M)

출력:
  {
    "type": "cascade",
    "direction": "long" | "short",
    "strength": float,     # 청산 규모/$1M
    "price": float,
    "liq_total": float,
    "liq_bias": float,     # 편중도
  }
```

### 3.5 빈도 예상

| 후보 | 추세장 | 보통 | 횡보장 |
|------|--------|------|--------|
| A. Momentum | 15~20 | 10~15 | 5~8 |
| B. Breakout | 4~6 | 2~4 | 1~2 |
| C. Cascade | 2~4 | 0~2 | 0 |
| **합계** | **21~30** | **12~21** | **6~10** |

---

## 4. FeatureExtractor — 피처 스냅샷

### 4.1 핵심 8 피처 (Phase B 초기, 200건)

| # | 이름 | 계산 | 범위 | 의미 |
|---|------|------|------|------|
| 1 | `price_momentum` | (close - close[5봉전]) / close × 100 | -3~+3% | 5분 모멘텀 |
| 2 | `trend_strength` | (EMA8 - EMA21) / ATR | -3~+3 | 추세 방향+강도 |
| 3 | `cvd_norm` | CVD_5m / 5분_총거래량 | -1~+1 | 매수/매도 압력 |
| 4 | `cvd_matches` | direction과 CVD 부호 일치 | 0 or 1 | 흐름 확인 |
| 5 | `vol_ratio` | 5m_vol / 20봉_avg_vol | 0.2~5.0 | 거래량 수준 |
| 6 | `adx` | ADX(14) | 0~80 | 추세 강도 |
| 7 | `bb_position` | (price-BB_low)/(BB_up-BB_low) | -0.5~1.5 | 밴드 내 위치 |
| 8 | `hour_sin` | sin(2π × hour_utc / 24) | -1~+1 | 시간대 (순환) |

### 4.2 확장 피처 (500건 이후 추가)

| # | 이름 | 의미 |
|---|------|------|
| 9 | `price_change_15m` | 15분 모멘텀 |
| 10 | `price_change_1h` | 1시간 모멘텀 |
| 11 | `cvd_15m_norm` | 15분 CVD |
| 12 | `whale_bias` | 고래 편향 |
| 13 | `liq_pressure` | 청산 압력 |
| 14 | `atr_pct` | ATR / 가격 |
| 15 | `bb_width_pctl` | BB 폭 백분위 |
| 16 | `vol_trend` | 3봉 거래량 추세 |
| 17 | `regime_score` | 레짐 수치 |
| 18 | `di_spread` | |+DI - -DI| |
| 19 | `loss_streak` | 연패 수 |
| 20 | `daily_pnl_pct` | 당일 손익 |
| 21 | `hour_cos` | cos(시간) |
| 22 | `candle_body_ratio` | 몸통 비율 |
| 23 | `price_vs_ema50` | EMA50 거리 |
| 24 | `vol_ratio_1m` | 1분 거래량 |
| 25 | `minutes_since_last` | 마지막 거래 후 시간 |

### 4.3 signals 테이블 (전수 기록)

```sql
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    candidate_type TEXT NOT NULL,        -- momentum/breakout/cascade
    direction TEXT NOT NULL,             -- long/short
    strength REAL NOT NULL,
    price REAL NOT NULL,
    features TEXT NOT NULL,              -- JSON {피처명: 값}
    ml_go INTEGER DEFAULT -1,           -- 1=Go, 0=NoGo, -1=ML비활성
    ml_prob REAL DEFAULT 0,             -- P(Win)
    entry_executed INTEGER DEFAULT 0,   -- 실제 진입 여부
    reject_reason TEXT,                 -- 미진입 사유
    -- Triple Barrier 결과 (shadow + 실거래 공통)
    label INTEGER DEFAULT -1,           -- 1=Win, 0=Loss, -1=미확정
    barrier_hit TEXT,                   -- tp/sl/time/adverse
    pnl_pct REAL DEFAULT 0,
    resolve_ts INTEGER DEFAULT 0,       -- 라벨 확정 시각
    regime TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_label ON signals(label);
```

---

## 5. ML DecisionEngine — Meta-Labeling

### 5.1 Phase A: 룰 기반 (0~100건)

```
Go 조건 (2개 AND):
  1. cvd_matches == 1 (CVD가 방향 지지)
  2. vol_ratio > 1.0 (거래량 평균 이상)

→ 후보의 ~60% 통과 = 일 9~18건
→ 쿨다운/포지션 제한 후 일 6~12건

목적: ML 학습 데이터 수집. "돈 버는 것"보다 "데이터 모으기".
자본: $100~200 micro.
```

### 5.2 Phase B: ML Go/NoGo (100건+)

```python
model = GradientBoostingClassifier(
    n_estimators=100,
    max_depth=3,
    learning_rate=0.1,
    min_samples_leaf=20,
    subsample=0.8,
    max_features=0.7,
)
```

**학습 데이터:**
- signals 테이블에서 label != -1 인 행 전부
- 진입한 거래 + shadow 추적 완료된 미진입 후보
- 가중치: 진입한 거래 × 2.0, shadow × 1.0 (실거래가 더 신뢰)

**결정:**
```
P(Win) > 55% → Go (진입)
P(Win) ≤ 55% → NoGo (shadow 추적)
```

**ML이 NoGo 한 후보도 shadow 추적 → 라벨 확정 → 재학습 데이터**

### 5.3 Triple Barrier 라벨링 (Shadow 전용)

```
후보 시점 가격 P, 고정 레버리지 15x, 후보 유형별 barrier:

  momentum:  TP = P × (1 + 0.0067),  SL = P × (1 - 0.0033),  Time = 240분
  breakout:  TP = P × (1 + 0.0067),  SL = P × (1 - 0.0033),  Time = 240분
  cascade:   TP = P × (1 + 0.0053),  SL = P × (1 - 0.0027),  Time = 120분

  (short은 TP/SL 반대)

이후 가격 관찰 (5초 간격, Redis bn:price):
  TP 먼저 → label = 1
  SL 먼저 → label = 0
  시간 초과 → label = 0

※ Shadow는 고정 barrier로 일관된 라벨 수집.
※ 실거래/페이퍼 TP1은 ATR 기반 (§7.2 참조) — Phase B 전환 시 shadow도 ATR 전환 검토.
```

### 5.4 Walk-Forward 재학습

```
윈도우: 최근 500건 (라벨 확정된 것)
트리거: 100건 새 데이터마다 재학습
검증: 윈도우 마지막 20% (100건) OOS

OOS 정확도:
  > 55%: 정상, 모델 교체
  52~55%: 경고, 모델 교체
  < 52%: ML 비활성 → Phase A (룰) 복귀
  < 52% 2연속: 텔레그램 알림 "ML 퇴화"
```

### 5.5 콜드스타트 타임라인

```
Day 1~3:   후보 감지 + 룰 필터 진입 + shadow (50~90건 축적)
Day 4~7:   계속 축적 (100~200건)
Day 8~10:  200건 도달 → 첫 ML 학습 → OOS 확인
           > 55%: Phase B 전환
           < 55%: 계속 룰 필터 + 축적
Day 11~14: ML 활성 운영 + 100건 추가 → 재학습
```

---

## 6. 주문 실행

### 6.1 진입 (executor.py — 구현 완료 + 수정)

```
strength < 1.5 (약한 시그널):
  post-only limit 3회 추격 (maker 0.02%)
  미체결 → 진입 포기 (taker 없음)

strength ≥ 1.5 (강한 시그널):
  post-only 3회 추격 (maker 시도)
  미체결 → market 주문 (taker 0.05% 허용)
  이유: 강한 모멘텀은 체결이 더 중요

SL/TP 알고:
  SL: market-on-trigger + sl_failsafe 백업 (체결 보장, 사고 방지)
  TP: limit-on-trigger (maker)
```

### 6.2 수수료 시뮬레이션

```
$1,000 자본, 마진 30%, 레버리지 15x
포지션: $4,500 notional, 하루 8건

최적 (전부 maker):     $4,500 × 0.02% × 2 × 8 = $14.4/일
현실적 (30% taker SL): maker 5건 $9.0 + 혼합 3건 $9.45 = $18.45/일
수익 대비:             목표 $25/일 중 수수료 $18.45 = 74%...

→ 수수료 비중이 너무 높음. 해결:
  1. 마진을 더 키움 (30→40%) → 포지션 $6,000 → 수익↑ 수수료비중↓
  2. 거래 수를 줄임 (8→6건) → 더 확실한 것만
  
보정: margin_pct 0.40 (Phase B 목표) / 0.80 (Phase A 데이터 수집), 일 6건
  포지션 $6,000, 6건: maker 4건 $9.6 + 혼합 2건 $8.4 = $18.0
  목표 수익: $6,000 × 10% × 50% × 55% × 6 = ~$30/일
  수수료 비중: $18/$30 = 60%... 여전히 높음

→ 근본 해결: TP를 키우거나, 레버리지를 낮추거나, 자본을 키움
  자본 $1,400 + 마진 40% + 15x: 포지션 $8,400
  수수료 비중 $18/$42 = 43% — 수용 가능 수준
```

> 결론: 자본 $1,000 이상 + margin_pct 0.40 + 일 6건 이하가 수수료 구조상 최적.

---

## 7. SL/TP/보유 전략

### 7.1 후보 유형별 설정

```yaml
hold_modes:
  momentum:       # 후보 A
    max_hold_min: 240
    sl_margin_pct: 5.0          # 마진 -5%
    tp1_margin_pct: 10.0        # shadow 라벨용 (실거래는 ATR 기반)
    tp2_mult: 2.5               # sl_dist × 2.5
    tp3_mult: 4.0

  breakout:       # 후보 B
    max_hold_min: 240
    sl_margin_pct: 5.0
    tp1_margin_pct: 10.0
    tp2_mult: 3.0               # sl_dist × 3.0
    tp3_mult: 5.0

  cascade:        # 후보 C
    max_hold_min: 120
    sl_margin_pct: 4.0          # 타이트
    tp1_margin_pct: 8.0
    tp2_mult: 2.0               # sl_dist × 2.0
    tp3_mult: 3.0
```

### 7.2 TP 구조

```
TP1 거리 계산 (실거래 + 페이퍼):
  tp1_dist = price × clamp(ATR_5m × 1.5, 0.25%, 0.80%)
  RR 최소 1.3 보장: tp1_dist = max(tp1_dist, sl_dist × 1.3)

  횡보장 (ATR 0.3%): TP1 ≈ 0.45%, RR ≈ 1.35
  추세장 (ATR 0.6%): TP1 ≈ 0.80%, RR ≈ 2.4

TP1 도달 시:
  50% 포지션 청산 (확정 수익)
  SL → 진입가 + 수수료 (본절)
  나머지 50% → 러너 트레일링

러너:
  trail_distance = max(ATR × 1.5, 수익 × giveback)
  giveback: <15분 35%, 15~30분 25%, 30분+ 15%
  cap: 최대 0.8%, 최소 0.1%
  R-lock: 2R 수익 → 1R 잠금, 3R → 2R 잠금
```

### 7.3 Adverse Selection (진입 후 90초)

```
조건 (AND — 3개 전부 충족해야 탈출):
  1. 마진 -2.5% 이상 역행
  2. CVD가 진입 반대로 전환 (5m CVD 부호 반전)
  3. 역행 방향 거래량 급증 (vol_ratio_1m > 1.5)

탈출: post-only 청산 시도 → 실패 시 market
손실: 마진 ~-2.5% (SL -5%의 절반)
```

---

## 8. 리스크 관리

### 8.1 게이트 7개

| # | 게이트 | 임계값 | 복구 |
|---|--------|--------|------|
| 1 | 일일 손실 | -10% (Phase A) / -5% (Phase B) | 다음날 자동 리셋 |
| 2 | 최대 DD | -12% | 수동 리셋 (텔레그램 알림) |
| 3 | 봇 정지 DD | -15% | 봇 완전 정지 (수동 재시작만) |
| 4 | 연패 쿨다운 | 5연패 → 1시간 | 시간 경과 |
| 5 | 포지션/간격 | 1개 + 30초 | 청산/경과 |
| 6 | 레짐-방향 | trending역방향(momentum/cascade) 차단, ranging+breakout 차단 | 레짐 전환 |
| 7 | 모멘텀 소진 | 최근 3캔들 이동 > TP1의 50% | 다음 후보 |

**게이트 6 상세 (레짐-방향):**
- `trending_up` + short momentum/cascade → 차단
- `trending_down` + long momentum/cascade → 차단
- `trending` + breakout 역방향 → 허용 (전환 시그널)
- `ranging` + breakout → 차단 (가짜 돌파)
- shadow: 게이트 무시 (ML 라벨 수집 유지)
- paper + 실거래: 차단

### 8.2 연패 사이즈 축소

```yaml
streak_sizing:
  2: 0.80
  3: 0.60
  5: 0.40
  7: 0.25
```

### 8.3 수익 보호

```
일일 +3% → 이후 마진 50%로 축소
일일 +5% → 추가 진입 차단 + 텔레그램 "좋은 날" 알림
```

---

## 9. 기존 코드 처리

### 9.1 파일별 처리 계획

**전면 교체 (내용 완전히 새로 씀):**

| 파일 | 현재 | 새로 |
|------|------|------|
| `src/strategy/flow_engine.py` | 764줄 FlowEngine 7셋업 | ~200줄 CandidateDetector 3종 |
| `src/strategy/flow_ml.py` | 329줄 점수보정 | ~400줄 ML Go/NoGo + Walk-Forward |

**대폭 수정:**

| 파일 | 변경 |
|------|------|
| `src/main.py` | _evaluate_unified → 새 흐름, 게이트 13→5, signals 기록 |
| `src/strategy/paper_trader.py` | 새 CandidateDetector + ML 연동 |
| `src/trading/risk_manager.py` | 주간/DD 한도, 수익보호 추가 |
| `src/trading/position_manager.py` | Adverse Selection 추가 |
| `src/strategy/setup_tracker.py` | 셋업명 → 후보 유형명 |
| `src/data/storage.py` | signals 테이블 추가 |
| `config/settings.yaml` | 수치 전면 변경 |

**유지 (변경 없음):**

| 파일 | 이유 |
|------|------|
| `src/trading/executor.py` | maker 강제 이미 완료 + taker 허용 조건 추가만 |
| `src/data/binance_stream.py` | 데이터 수집 잘 작동 중 |
| `src/data/ws_stream.py` | OKX 가격 수집 |
| `src/data/candle_collector.py` | 캔들 수집 |
| `src/engine/regime_detector.py` | 레짐 판별 정상 작동 |
| `src/trading/leverage.py` | 레버리지 계산 |
| `src/utils/helpers.py` | 유틸리티 |

**삭제 (레거시):**

| 파일/디렉토리 | 이유 |
|--------------|------|
| `src/signal_engine/` (aggregator, grader) | 옛 14기법 합산기, 미사용 |
| `src/strategy/adaptive_ml.py` | 옛 ML v2, 미사용 |
| `src/strategy/historical_learner.py` | 옛 학습기, 미사용 |
| `src/strategy/scalp_engine.py` | 옛 스캘핑 엔진, 미사용 |
| `src/strategy/unified_engine.py` | 옛 통합 엔진, 미사용 |
| `src/strategy/meta_learner.py` | 옛 메타러너, 미사용 |
| `src/strategy/auto_backtest.py` | 옛 백테스트, 미사용 |
| `src/engine/fast/fractal.py` | 프랙탈, 미사용 |
| `src/engine/slow/` 전체 | 옛 Slow Path 기법들, 미사용 |
| `src/engine/fast/` 일부 | market_structure, vwap — CandidateDetector에서 미사용 |
| `src/data/oi_funding.py` | OI/펀딩 수집, 미사용 |
| `src/trading/news_filter.py` | 뉴스 필터, 미사용 |
| `backtest/` 전체 | 새 구조에 맞지 않음, 나중에 재작성 |
| `tmp_analyze.py` | 임시 파일 |

### 9.2 대시보드 변경

```
기존 표시:  셋업 (LVL/MOM/PB/BRK/DIV/SES/LIQ) + ML 점수 + 레짐
새 표시:    후보 (Momentum/Breakout/Cascade) + ML P(Win) + 레짐

제거: 옛 셋업 API, Signal Performance, ML 모델 카드 (옛 형식)
추가: ML Go/NoGo 현황, signals 테이블 최근 20건, shadow 결과
```

### 9.3 텔레그램 변경

```
진입 알림: "📊 MOMENTUM LONG @ $78,200 | ML 62% | vol 2.1x | 15x"
청산 알림: "✅ TP1 hit +$15.2 (+10.1%) | 12min"
일일 리포트: 거래수/승률/PnL + ML 정확도 + shadow 정확도
경고: DD -12%, ML 퇴화, 연패 5
```

---

## 10. settings.yaml 전체

```yaml
exchange:
  name: "okx"
  symbol: "BTC/USDT:USDT"
  margin_mode: "isolated"

# 분석 전부 Binance, 실행만 OKX
data_source: "binance"

timeframes:
  primary: "5m"
  confirmation: "15m"
  filter: "1h"
  candles: ["1m", "5m", "15m", "1h", "4h", "1d"]

risk:
  sizing_mode: "margin_loss_cap"
  margin_pct: 0.40
  leverage_range: [15, 20]
  max_positions: 1
  max_daily_loss: 0.05
  max_drawdown: 0.12
  bot_kill_drawdown: 0.15
  profit_protect_pct: 0.03
  profit_stop_pct: 0.05
  max_margin_loss_pct: 5.0
  tp1_margin_gain_pct: 10.0
  min_entry_interval_sec: 30

  streak_sizing:
    2: 0.80
    3: 0.60
    5: 0.40
    7: 0.25

hold_modes:
  momentum:
    max_hold_min: 45
    sl_margin_pct: 5.0
    tp1_margin_pct: 10.0
    tp2_mult: 2.5
    tp3_mult: 4.0
  breakout:
    max_hold_min: 90
    sl_margin_pct: 5.0
    tp1_margin_pct: 10.0
    tp2_mult: 3.0
    tp3_mult: 5.0
  cascade:
    max_hold_min: 20
    sl_margin_pct: 4.0
    tp1_margin_pct: 8.0
    tp2_mult: 2.0
    tp3_mult: 3.0

trailing:
  tp1_close_pct: 0.5
  tp2_close_pct: 0.3
  tp3_close_pct: 0.2
  trail_atr_mult: 1.5
  trail_profit_giveback: 0.25
  trail_min_price_pct: 0.15

adverse_selection:
  enabled: true
  window_sec: 90
  margin_threshold_pct: 2.5
  require_cvd_reversal: true
  require_vol_surge: true

candidate:
  momentum:
    min_body_atr_ratio: 0.8
    min_vol_ratio: 1.3
    min_body_ratio: 0.6
  breakout:
    bb_squeeze_pctl: 25
    min_vol_ratio: 1.2
  cascade:
    min_liq_usd: 500000
    min_bias_pct: 0.80
    min_price_change_pct: 0.2

ml:
  phase_a_min_samples: 200
  retrain_interval: 100
  window_size: 500
  min_oos_accuracy: 0.52
  go_threshold: 0.55
  initial_features: 8
  expanded_features_at: 500

cooldown:
  after_loss_sec: 60
  after_win_sec: 20
  streak_5_min: 60

polling:
  candle_fast_sec: 2
  candle_slow_sec: 6
  signal_eval_sec: 3
  position_check_sec: 1
  shadow_check_sec: 5

fees:
  taker: 0.0005
  maker: 0.0002

telegram:
  enabled: true

redis:
  host: "localhost"
  port: 6379
  db: 0

data:
  candle_backfill_days: 30
```

---

## 11. 성공/실패 기준

| 기준 | 성공 | 부분 성공 | 실패 |
|------|------|----------|------|
| 2주 일평균 | +1.5%+ | +0.5~1.5% | < +0.5% |
| 최대 DD | < 8% | < 12% | > 12% |
| 승률 | > 50% | > 45% | < 45% |
| RR | > 1.5:1 | > 1.0:1 | < 1.0:1 |
| ML OOS | > 55% | > 52% | < 52% |

실패 시: signals 테이블 분석 → 피처/임계값 조정 → 재시도.

---

## 부록: 기존 명세서 처리

```
명세서.md → 명세서_v1_archive.md (리네임, 보존)
SPEC_V2.md → 이 파일이 현행 명세서
CLAUDE.md → 설계서 참조를 SPEC_V2.md로 변경
```
