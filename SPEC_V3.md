# SPEC v3 — ATR-Adaptive Grid + Leading Regime Detection

> 작성: 2026-05-22  
> 전략: 횡보 수확 + 즉시 회피  
> 원칙: 선행 시그널로 추세 감지 → 그리드 정지 | 횡보에서만 매매  
> 자본: $180 시작, 검증 후 $500~$1,500 확장

---

## 0. 이전 실패 교훈 → v3 설계 근거

| # | 실패 | 원인 | v3 대응 |
|---|------|------|---------|
| 1 | 스캘핑 수수료 적자 | taker 0.05% × 왕복 = 0.10% | **전 주문 maker (0.02%)** |
| 2 | 후행지표로 추세 감지 | ADX/EMA/Hurst = 이미 물린 뒤 | **선행 4시그널 (OBI+CVD+Vol+CUSUM)** |
| 3 | 추세에서 방향 매매 시도 | 70% 페이크아웃, 잘못된 방향 | **추세 = 쉰다 (매매 안 함)** |
| 4 | 과잉 안전장치 | 수익 사이클 강제 중단 | **최소 3개만 (레짐+CB+백스탑)** |
| 5 | 고정 사이즈 | 자본 증가 시 수동 조정 | **잔고 비례 자동 사이징** |

---

## 1. 아키텍처

```
[데이터 수집]
  OKX WS: trades, books5, candles (7 TF)
  Binance WS: aggTrade (CVD, whale)
       ↓
[선행 레짐 감지] ← 핵심
  OBI (호가 불균형)
  CVD 가속도 (Z-score)
  거래량 스파이크
  CUSUM (변화점)
  → Composite Regime Score (CRS)
       ↓
[모드 결정]
  |CRS| < 0.20 → ACTIVE (그리드 가동)
  |CRS| ≥ 0.35 (5초 지속) → PAUSED (주문 취소)
  서킷브레이커 → FROZEN (60초 동결)
       ↓
[그리드 엔진]
  ATR-adaptive spacing
  기하식 레벨 배치
  잔고 비례 사이징
  사이클: 체결 → counter-order(TP) → 완성 → 재배치
       ↓
[실행]
  전 주문 post-only limit (maker 0.02%)
  OKX Hedge Mode (long/short 동시 보유)
```

---

## 2. 선행 레짐 감지 (Leading Regime Detection)

### 2.1 시그널 정의

| # | 시그널 | 데이터 | 계산 | 선행 시간 |
|---|--------|--------|------|-----------|
| 1 | **OBI** | books5 | (ΣBid - ΣAsk) / (ΣBid + ΣAsk), EMA 10초 | 0.5~5초 |
| 2 | **CVD 가속** | trades | CVD_Δ5s / σ(CVD_Δ5s, 60s) | 2~15초 |
| 3 | **거래량 스파이크** | trades | vol_1s / avg(vol_1s, 60s) | 1~10초 |
| 4 | **CUSUM** | price | 누적합 변화점 감지 (threshold=4.0, drift=0.4) | 3~20초 |

### 2.2 Composite Regime Score (CRS)

```python
# 각 시그널 → [-1, +1] 정규화
obi_norm      = clip(obi_ema / 0.5, -1, 1)
cvd_norm      = clip(cvd_z / 3.0, -1, 1)
vol_norm      = clip((vol_ratio - 1) / 4.0, 0, 1) * direction
cusum_norm    = 1.0 if cusum_up else (-1.0 if cusum_down else 0)

# 가중 합산
CRS = obi_norm × 0.25
    + cvd_norm × 0.30
    + vol_norm × 0.20
    + cusum_norm × 0.25
```

### 2.3 모드 전환 규칙

| 현재 → 목표 | 조건 | 동작 |
|-------------|------|------|
| ACTIVE → PAUSED | \|CRS\| ≥ 0.35, **2초 연속** | 미체결 주문 취소, counter-order 유지 |
| PAUSED → ACTIVE | \|CRS\| < 0.15, **15초 연속** + vol_ratio < 2.0 | 그리드 리빌드 |
| ANY → FROZEN | 10초 내 가격 2% 이동 | 전 주문 취소 (counter 포함), 60초 대기 |
| FROZEN → PAUSED | 60초 경과 | CRS 재평가 후 결정 |

**히스테리시스**: 진입 0.35 / 이탈 0.15 → 빈번한 전환 방지  
**쿨다운**: 모드 전환 후 최소 15초간 재전환 불가

---

## 3. 그리드 엔진

### 3.1 기본 메커니즘

```
양방향 그리드: BUY 레벨 (open long) + SELL 레벨 (open short)
체결 → counter-order (TP, spacing 만큼 반대) → 사이클 완성 → 재배치
모든 주문: post-only limit (maker 0.02%)
```

### 3.2 파라미터

| 항목 | 공식 | 범위 |
|------|------|------|
| **spacing** | ATR(14, 5m) / price × 0.6 | clamp [0.15%, 0.50%] |
| **그리드 타입** | 기하식 (%) | level_n = center × (1 + spacing%)^n |
| **레벨 수** | floor(잔고 × target_lev / (BTC가격 × 0.01)) | 최소 2, 최대 10 |
| **사이즈** | 0.01 BTC (1계약) × 레벨별 동일 | OKX 최소단위 |
| **목표 레버리지** | 8x (평시), 10x (저변동), 6x (고변동) | ATR 기반 자동 조절 |
| **리밸런스** | 1시간 ATR 재계산, drift > 50% → 리빌드 | 열린 counter 있으면 스킵 |

### 3.3 레벨 배분

ACTIVE 모드에서 항상 **대칭**: buy N + sell N

```
$180  (8x = $1,440): 레벨 2 (1 buy + 1 sell)
$500  (8x = $4,000): 레벨 4 (2 buy + 2 sell)
$1,000 (8x = $8,000): 레벨 6 (3 buy + 3 sell)
$2,000 (8x = $16,000): 레벨 10 (5 buy + 5 sell)
```

### 3.4 사이징 공식

```python
def calc_grid_params(balance: float, btc_price: float, atr_pct: float):
    # ATR 기반 레버리지 조절
    if atr_pct < 0.10:
        target_lev = 10  # 저변동 → 공격적
    elif atr_pct > 0.30:
        target_lev = 6   # 고변동 → 보수적
    else:
        target_lev = 8   # 평시

    max_notional = balance * target_lev
    contract_value = btc_price * 0.01  # 1계약 = 0.01 BTC
    total_levels = min(10, max(2, int(max_notional / contract_value)))
    
    # 짝수 강제 (buy/sell 대칭)
    if total_levels % 2 != 0:
        total_levels -= 1
    
    half_levels = total_levels // 2
    return total_levels, half_levels, target_lev
```

### 3.5 사이클 수익 공식

```
건당 gross = 0.01 BTC × spacing_abs ($)
건당 수수료 = 0.01 × BTC가격 × 0.0002 × 2 (진입+청산, 양쪽 maker)
건당 net = gross - 수수료

예시 (BTC=$78,000, spacing=0.15%):
  gross = 0.01 × $117 = $1.17
  수수료 = 0.01 × $78,000 × 0.0004 = $0.31
  net = $0.86
```

---

## 4. 안전장치 (3개)

### 4.1 선행 레짐 정지 (전략 핵심)

- CRS ≥ 0.35 (2초 지속) → 미체결 주문 취소
- Counter-order는 유지 (TP 기다림)
- PAUSED 중에도 CRS 계속 계산 → 복귀 판단 (15초 + vol_ratio < 2.0)

### 4.2 서킷브레이커

```python
# 10초 내 2% 이동 감지
price_10s_ago = price_buffer[-10]  # 1초 간격 버퍼
change_pct = abs(current_price - price_10s_ago) / price_10s_ago * 100

if change_pct >= 2.0:
    → FROZEN 모드
    → 전 주문 즉시 취소 (counter 포함)
    → 60초 대기 후 재평가
```

### 4.3 OKX 자체 청산 (거래소 담당)

- OKX 강제 청산이 최종 안전장치 역할
- 8x 레버리지 기준 ~12% 역행 시 자동 청산
- 별도 conditional order 미구현 (레짐 감지 + 서킷브레이커가 그 전에 방어)
- 봇 내부 DD 스탑 없음 (수익 방해 방지)

---

## 5. 데이터 계층

### 5.1 수집 (기존 유지)

| 소스 | 채널 | 용도 |
|------|------|------|
| OKX WS Public | trades | CVD, 거래량, whale, 가격 |
| OKX WS Public | books5 | OBI, depth 분석 |
| OKX WS Business | candle (7 TF) | ATR, Hurst (보조) |
| OKX WS Public | tickers | 실시간 가격 |
| Binance WS | aggTrade | CVD 보조, whale 감지 |
| Binance REST | funding, OI | 펀딩 모니터링 |

### 5.2 Redis 키 구조

```
# 실시간 가격
rt:price:BTC-USDT-SWAP          → float

# 레짐 감지
regime:crs                      → float (-1~+1)
regime:mode                     → "ACTIVE" | "PAUSED" | "FROZEN"
regime:signals                  → hash {obi, cvd, vol, cusum, crs, mode}

# 그리드 상태
grid:state:BTC/USDT:USDT        → JSON (GridState)

# 시스템
sys:balance                     → float
sys:bot_status                  → "running" | "stopped"

# 리스크 (모니터링용)
risk:streak                     → int
risk:daily_pnl                  → float
risk:weekly_pnl                 → float
```

### 5.3 SQLite 테이블

```sql
-- 캔들 (기존 유지)
CREATE TABLE candles (
    symbol TEXT, timeframe TEXT, timestamp INTEGER,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, timeframe, timestamp)
);

-- 그리드 사이클 기록
CREATE TABLE grid_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grid_id TEXT,
    level_id INTEGER,
    cycle_num INTEGER,
    side TEXT,          -- "buy" | "sell"
    entry_price REAL,
    exit_price REAL,
    size_btc REAL,
    pnl_usdt REAL,
    fee_total REAL,
    entry_time INTEGER,
    exit_time INTEGER,
    spacing_pct REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_grid_trades_time ON grid_trades(exit_time DESC);
```

---

## 6. 실행 계층

### 6.1 주문 규칙

| 규칙 | 상세 |
|------|------|
| **전 주문 post-only** | OKX `orderType: post_only` — 체결 불가 시 취소됨 (taker 방지) |
| **post-only 실패 시** | 포기 (market fallback 없음, 재시도 없음) |
| **Hedge Mode** | OKX `tdMode: isolated`, `posSide: long/short` 분리 |
| **Counter-order** | 체결가 ± spacing으로 post-only limit (reduce_only) |

### 6.2 주문 흐름

```
[그리드 주문 배치]
  place_limit_order("buy", 0.01, price, "long")
  place_limit_order("sell", 0.01, price, "short")
       ↓
[체결 감지] (OKX Private WS orders 채널, 10~50ms push)
  주문 상태 변경 즉시 콜백 → grid_engine.on_order_update()
  REST fallback: 10초마다 fetch_open_orders (WS 누락 대비)
       ↓
[Counter-order 배치]
  buy 체결 → sell TP (entry + spacing) reduce_only
  sell 체결 → buy TP (entry - spacing) reduce_only
       ↓
[사이클 완성]
  counter 체결 → PnL 기록 → 원래 레벨 재배치
```

---

## 7. 모니터링

### 7.1 텔레그램 알림

| 이벤트 | 메시지 |
|--------|--------|
| 봇 시작 | 레벨 수, spacing, 잔고 |
| 모드 전환 | ACTIVE↔PAUSED↔FROZEN + CRS 값 |
| 사이클 완성 | 레벨, 진입가→청산가, PnL, 누적 |
| 서킷브레이커 | 트리거 가격, 변동폭 |
| 일일 리포트 | 사이클 수, 총 PnL, 가동률 |

### 7.2 대시보드

- 현재 모드 + CRS 실시간
- 그리드 레벨 시각화 (가격 + 상태)
- 사이클 히스토리
- 잔고 / PnL 추이

### 7.3 로그

```
[GRID] 모니터 | $77,650 | center=$77,600 | Lv-1:placed Lv1:placed | cycles=5 pnl=$+4.30
[REGIME] CRS=0.42 (OBI:0.38 CVD:0.55 Vol:0.31 CUSUM:up) → PAUSED
[CB] 서킷브레이커! $77,650→$76,100 (-2.0%) in 8s → FROZEN
```

---

## 8. 파일 구조 (목표)

```
src/
├── main.py                    # GridBot 엔트리포인트
├── data/
│   ├── storage.py             # SQLite + Redis
│   ├── ws_stream.py           # OKX Public WS (trades/books/candles)
│   ├── order_stream.py        # ★ OKX Private WS (체결 10~50ms push)
│   ├── binance_stream.py      # Binance funding/OI
│   └── candle_collector.py    # 캔들 백필
├── strategy/
│   ├── grid_engine.py         # 그리드 코어 (모드 전환 포함)
│   └── regime_detector.py     # ★ 신규: CRS 계산 + 모드 판정
├── trading/
│   ├── executor.py            # OKX 주문 실행
│   ├── grid_state.py          # 그리드 상태 구조체
│   └── risk_manager.py        # 모니터링 (차단 없음, 기록만)
├── monitoring/
│   ├── dashboard.py           # FastAPI
│   ├── telegram_bot.py        # 알림 + 명령
│   ├── trade_logger.py        # JSONL 기록
│   └── static/index.html      # 대시보드 UI
└── utils/
    └── helpers.py             # config/env 로딩

config/
└── settings.yaml              # 전체 설정

data/
├── scalp.db                   # SQLite
├── risk_state.json            # 리스크 백업
└── logs/                      # JSONL + 텍스트 로그
```

---

## 9. 설정 구조 (settings.yaml)

```yaml
strategy: "grid"

exchange:
  name: okx
  symbol: "BTC/USDT:USDT"
  margin_mode: isolated

grid:
  enabled: true
  target_leverage: 8          # 평시 목표 (ATR 따라 6~10 자동)
  size_btc: 0.01              # 레벨당 (OKX 최소 1계약)
  atr_mult: 0.6               # spacing = ATR% × mult
  spacing_min_pct: 0.15        # 최소 spacing (펀딩비 커버)
  spacing_max_pct: 0.50        # 최대 spacing
  spacing_type: geometric      # arithmetic | geometric
  rebalance_sec: 3600          # ATR 재계산 주기
  drift_rebalance_pct: 50      # drift > 50% → 리빌드
  monitor_sec: 1               # 체결 확인 주기
  atr_period: 14
  atr_timeframe: "5m"

regime:
  # CRS 임계값
  pause_threshold: 0.35        # ACTIVE → PAUSED
  resume_threshold: 0.15       # PAUSED → ACTIVE
  pause_confirm_sec: 2         # 정지 확인 (2초 — 빠르게 멈춤)
  resume_confirm_sec: 15       # 복귀 확인 (15초 — 안정 확인)
  mode_switch_cooldown_sec: 15 # 모드 전환 최소 간격
  # 서킷브레이커
  circuit_breaker_pct: 2.0     # X% 이동
  circuit_breaker_window_sec: 10  # Y초 내
  circuit_breaker_freeze_sec: 60  # 동결 시간
  # 시그널 가중치
  weight_obi: 0.25
  weight_cvd: 0.30
  weight_volume: 0.20
  weight_cusum: 0.25
  # CUSUM 파라미터
  cusum_threshold: 4.0
  cusum_drift: 0.4

risk:
  # 거래소 백스탑 (미구현 — OKX 자체 청산이 담당)
  backstop_drawdown_pct: 20    # 참조용

fees:
  maker: 0.0002
  taker: 0.0005

telegram:
  enabled: true

redis:
  host: redis
  port: 6379
  db: 0
```

---

## 10. 수익 모델

### 10.1 건당 수익

```
spacing 0.15% ($117 at $78k):
  gross = $1.17
  fee   = $0.31
  net   = $0.86/cycle

spacing 0.30% ($234 at $78k):
  gross = $2.34
  fee   = $0.31
  net   = $2.03/cycle
```

### 10.2 월간 예상 ($500, 4레벨)

| 시장 | 가동률 | 사이클/활성일 | 월 수익 |
|------|--------|-------------|---------|
| 횡보 (좋은 달) | 70% | 6 | $108 |
| 보통 | 55% | 4 | $56 |
| 추세 잦음 | 40% | 2~3 | $26~39 |

**월평균 기대: $55~90 (11~18%)**  
**복리 6개월 후: $1,200+ → $150/월 도달**

### 10.3 자본별 확장

| 자본 | 레벨 | 레버리지 | 월 예상 | 월 수익률 |
|------|------|----------|---------|-----------|
| $180 | 2 | 8.7x | $20~35 | 11~19% |
| $500 | 4 | 6.2x | $55~90 | 11~18% |
| $1,000 | 6 | 4.7x | $100~160 | 10~16% |
| $2,000 | 10 | 3.9x | $200~320 | 10~16% |

---

## 11. 삭제 대상 (레거시)

| 대상 | 이유 |
|------|------|
| `/crypto-scalper/` 디렉토리 전체 | Rust 스캘퍼, 미사용 |
| `log.txt` | 구 로그, 712KB |
| `tmp_check.py` | 임시 파일 |
| `사진.jpg` | 무관 파일 |
| DB: `scalp_signals` 테이블 | 스캘핑 시그널, 미사용 |
| DB: `scalp_trades` 테이블 | 스캘핑 거래, 미사용 |
| Redis: `rt:regime:hurst` | 후행 → CRS로 대체 |
| Redis: `rt:micro:*` | 스캘핑 피처, 그리드에서 미사용 |
| ws_stream.py 마이크로스트럭처 15종 | OBI/CVD/Vol만 필요, 나머지 삭제 |
| risk_manager.py 차단 로직 | 모니터링만 남김 |
| SPEC_V2.md | 본 문서로 대체 |
| dashboard 스캘핑 관련 UI | 그리드 전용으로 교체 |

---

## 12. 구현 우선순위

| Phase | 내용 | 의존성 |
|-------|------|--------|
| **P0** | 레거시 삭제 + DB 정리 | 없음 |
| **P1** | `regime_detector.py` 신규 작성 (CRS 4시그널) | ws_stream 데이터 |
| **P2** | `grid_engine.py` 리팩터 (모드 전환 + 자동 사이징 + 기하식) | P1 |
| **P3** | 서킷브레이커 + 거래소 백스탑 | P2 |
| **P4** | settings.yaml 재구성 + 배포 테스트 | P0~P3 |
| **P5** | 대시보드 + 텔레그램 업데이트 | P4 |
