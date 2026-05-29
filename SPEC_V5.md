# SPEC v5 — ScalpBot (Jay 단타법)

> 작성: 2026-05-29
> 원칙: **후행 확인, 먹고 나감, 단순**
> 근거: Jay 단타법 + 학술 백테스트 (StochRSI+MACD 승률 52~73%)
> 이전: SPEC_V4.md (Minimal DGT Grid — 폐기)

---

## 0. 설계 원칙

| # | 원칙 | 근거 |
|---|------|------|
| 1 | **후행 확인 후 진입** | 예측(스윙) vs 확인(단타). 단타는 신호 확인 후 진입 |
| 2 | **먹고 나간다** | StochRSI 도달 = 즉시 청산. 욕심 없음 |
| 3 | **지표 2개만** | StochRSI + MACD. 복잡할수록 매매 안 함 |
| 4 | **파라미터 고정** | 시작 시 1회 설정, 실행 중 불변 |
| 5 | **거짓 신호 필터** | MACD 크로스인데 StochRSI 소진 → 스킵 |
| 6 | **단순할수록 강건** | 필터 추가 = 매매 빈도 감소 = 수익 감소 |

### v4 → v5 전환 이유

| # | v4 문제 | v5 대응 |
|---|--------|---------|
| 1 | 그리드 정적 수익 = 0 (수학 증명) | 모멘텀 크로스 = 양의 기대수익 |
| 2 | DGT 리빌드 빈번 → 수수료 소모 | 신호 기반 진입, 불필요 매매 없음 |
| 3 | 양방향 노출 → 추세장 손실 | 단방향 1포지션, 추세 따라감 |
| 4 | 사이클 완성까지 자본 묶임 | 시장가 즉시 진입/청산 |

---

## 1. 아키텍처

```
[시작 시 1회]
  잔고 조회 → 레버리지 설정 → 크래시 복구
  → Redis pub/sub 구독 (ch:kline:ready)
       ↓
[캔들 닫힘 이벤트 (30m 또는 1h)]
  → DB에서 최근 100개 캔들 로드
  → StochRSI + MACD 계산
  → 신호 평가:
    ├── 청산 조건 → 시장가 청산
    ├── 진입 조건 → 시장가 진입
    └── 대기 조건 → pending_signal 등록 (3캔들 타임아웃)
       ↓
[0.5초 루프]
  → SL 체크 (실시간 가격 vs ATR SL)
  → BOT_KILL 체크 (30초마다)
  → 서킷브레이커 체크
```

---

## 2. 매매 로직 (Jay 단타법)

### 2.1 지표

| 지표 | 설정 | 용도 |
|------|------|------|
| **Stochastic RSI** | RSI(14), Stoch(14), K(3), D(3) | 과매수/과매도 + 크로스 |
| **MACD** | fast=8, slow=26, signal=9 | 모멘텀 크로스 확인 |
| **ATR** | period=14, 동일 TF | SL 거리 계산 |

MACD fast=8은 Jay 비표준 설정. 1h 단타에서 표준(12)보다 반응 빠름.

### 2.2 롱 진입 (2조건 동시 충족)

```
조건 1: StochRSI K < 20 (바닥권) AND K가 D를 상향 돌파 (골든크로스)
조건 2: MACD 라인이 Signal 라인을 상향 돌파 (골든크로스)
필터:  K > 70 이면 스킵 (소진 — 이미 많이 올라옴)
```

### 2.3 숏 진입 (2조건 동시 충족)

```
조건 1: StochRSI K > 80 (상단권) AND K가 D를 하향 돌파 (데드크로스)
조건 2: MACD 라인이 Signal 라인을 하향 돌파 (데드크로스)
필터:  K < 30 이면 스킵 (소진 — 이미 많이 내려옴)
```

### 2.4 청산

```
롱 청산: StochRSI K > 80 도달 → 즉시 시장가 청산
숏 청산: StochRSI K < 20 도달 → 즉시 시장가 청산
SL 청산: 가격이 ATR SL 돌파 → 즉시 시장가 청산
```

### 2.5 대기 신호 (비동시 크로스)

```
StochRSI와 MACD가 동시에 크로스하지 않는 경우:
  → pending_signal 등록 ("long_wait_macd", "short_wait_srsi" 등)
  → 3캔들 내 두 번째 조건 충족 시 진입
  → 3캔들 초과 시 폐기
```

### 2.6 거짓 신호 필터 (Jay 핵심 노하우)

```
MACD 골든크로스인데 StochRSI K가 이미 70+ → 롱 스킵
MACD 데드크로스인데 StochRSI K가 이미 30- → 숏 스킵

이유: StochRSI가 이미 반대편에 도달 = 에너지 소진
Jay: "이렇게 구라핑을 피해가시면됩니다"
```

---

## 3. Stop Loss — ATR 기반

```python
atr_pct = ATR(14, TF) / price × 100
sl_pct = clamp(atr_pct × 1.0, 0.5%, 2.0%)
```

| 항목 | 값 | 이유 |
|------|-----|------|
| ATR 배수 | 1.0 | 스캘핑 표준 (연구 근거) |
| 하한 | 0.5% | 정상 노이즈에 안 걸리게 |
| 상한 | 2.0% | 10x 기준 계좌 20% = BOT_KILL 근접 방지 |

체크 주기: **0.5초** (실시간 Redis 가격)

---

## 4. 포지션 관리

| 규칙 | 상세 |
|------|------|
| 동시 포지션 | **1개만** (롱 또는 숏, 동시 없음) |
| 주문 타입 | 시장가 (모멘텀 전략, limit 부적합) |
| 사이즈 | 0.01 BTC (설정 가능) |
| 레버리지 | 10x 고정, isolated margin |
| Hedge Mode | OKX `posSide: long/short` |
| 쿨다운 | 설정 가능 (기본 0초) |

---

## 5. 안전장치 (2개)

### 5.1 서킷브레이커

```
조건: 10초 내 가격 2% 이동
동작: 60초 동결 (신규 진입 불가, SL은 계속 동작)
```

### 5.2 BOT_KILL

```
조건: 계좌 DD -20% (peak 대비)
동작: 포지션 시장가 청산 + 봇 정지
복구: 수동 재시작 필요
```

---

## 6. 수익 모델

### 레버리지 10x 기준

| 시나리오 | 원물 움직임 | 계좌 수익 |
|----------|-----------|----------|
| 좋은 날 | ~4% | ~40% |
| 평균 | ~1.5% | ~15% |
| SL | ~0.8% | ~8% (손실) |

### 손익비

```
평균 수익 : 평균 손실 ≈ 15% : 8% ≈ 1.9 : 1
승률 52~73% (연구 기준) → 기대값 양수
```

### 월 예상 (보수적)

| 자본 | 일 매매 | 승률 55% | 월 예상 |
|------|---------|----------|---------|
| $500 | 1~2건 | 55% | $50~150 |
| $1,000 | 1~2건 | 55% | $100~300 |
| $2,000 | 1~2건 | 55% | $200~600 |

---

## 7. 타임프레임

| TF | 특징 | 설정 |
|----|------|------|
| **1h** (기본) | 신호 안정적, 거짓 신호 적음 | `scalp.timeframe: "1h"` |
| **30m** (대안) | 매매 빈도 2배, 노이즈 약간 증가 | `scalp.timeframe: "30m"` |

Jay: "30분이나 한시간을 주로 보는거같아요"

settings.yaml에서 `timeframe` 값만 바꾸면 전환 가능.
WS 수집: 30m + 1h 모두 구독 중.

---

## 8. 데이터 계층

### 8.1 수집

| 소스 | 채널 | 용도 |
|------|------|------|
| OKX WS Public | tickers | 실시간 가격 (SL 체크) |
| OKX WS Business | candles 8종 (1m~1w, 30m 포함) | 지표 계산 |
| OKX WS Private | orders | 체결 감지 |
| OKX REST | fetch_open_orders | fallback |

### 8.2 Redis 키

```
# 가격
rt:price:BTC-USDT-SWAP          → float

# 스캘프 상태
scalp:state:BTC/USDT:USDT       → JSON (ScalpState)

# 시스템
sys:balance                     → float
sys:bot_status                  → "running" | "stopped"

# 리스크
risk:streak                     → int
risk:daily_pnl                  → float
```

### 8.3 SQLite

```sql
CREATE TABLE candles (
    symbol TEXT, timeframe TEXT, timestamp INTEGER,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    UNIQUE(symbol, timeframe, timestamp)
);

CREATE TABLE scalp_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT,        -- "long" / "short"
    entry_price REAL, exit_price REAL,
    size_btc REAL, pnl_usdt REAL, fee_total REAL,
    entry_time INTEGER, exit_time INTEGER,
    exit_reason TEXT,      -- "stoch_rsi_top", "stop_loss_1.5%", etc.
    timeframe TEXT         -- "1h" / "30m"
);
```

---

## 9. 파일 구조

```
src/
├── main.py                    # 엔트리포인트 (ScalpBot)
├── data/
│   ├── storage.py             # SQLite + Redis
│   ├── ws_stream.py           # OKX Public WS (tickers + candles 8종)
│   ├── order_stream.py        # OKX Private WS (체결 감지)
│   └── candle_collector.py    # 캔들 백필
├── strategy/
│   ├── scalp_engine.py        # 스캘프 코어 (Jay 단타법)
│   ├── scalp_state.py         # 상태 구조체 + Redis 직렬화
│   └── indicators.py          # StochRSI, MACD, ATR, BB (numpy)
├── trading/
│   ├── executor.py            # OKX 주문 실행
│   └── risk_manager.py        # BOT_KILL + 모니터링
├── monitoring/
│   ├── dashboard.py           # FastAPI
│   ├── telegram_bot.py        # 알림 + 명령어
│   ├── trade_logger.py        # JSONL 기록
│   └── static/index.html      # 대시보드 UI
└── utils/
    └── helpers.py             # config/env
```

---

## 10. 설정 (settings.yaml)

```yaml
strategy: "scalp"

exchange:
  name: "okx"
  symbol: "BTC/USDT:USDT"
  margin_mode: "isolated"

scalp:
  enabled: true
  leverage: 10
  timeframe: "1h"               # "30m" 또는 "1h"
  size_btc: 0.01
  signal_timeout_candles: 3
  cooldown_sec: 0
  bollinger_filter: false

  stoch_rsi_period: 14
  stoch_rsi_k: 3
  stoch_rsi_d: 3
  stoch_rsi_ob: 80
  stoch_rsi_os: 20

  macd_fast: 8
  macd_slow: 26
  macd_signal: 9

  sl_atr_mult: 1.0
  sl_min_pct: 0.5
  sl_max_pct: 2.0

safety:
  circuit_breaker_pct: 2.0
  circuit_breaker_window_sec: 10
  circuit_breaker_freeze_sec: 60
  bot_kill_drawdown_pct: 20

fees:
  maker: 0.0002
  taker: 0.0005
```

---

## 11. 크래시 복구

```
봇 시작 시:
  1. Redis에서 ScalpState 로드
  2. OKX에서 실제 포지션 조회
  3. 대조:
     a) state=long인데 포지션 없음 → flat 리셋
     b) state=short인데 포지션 없음 → flat 리셋
     c) state 없는데 포지션 있음 → 고아 포지션 시장가 청산
  4. 레버리지 설정 (복구 후)
  5. 정상 루프 진입
```

---

## 12. 모니터링

### 12.1 텔레그램 알림

| 이벤트 | 메시지 |
|--------|--------|
| 봇 시작 | 잔고, TF, 레버리지, MACD 설정 |
| 진입 | 방향, 가격, SL% |
| 청산 | 방향, 진입→청산, PnL, 이유, WR |
| 서킷브레이커 | 변동폭, 동결 시간 |
| BOT_KILL | 청산 완료, 잔여 잔고 |
| 일일 리포트 | 매매 수, PnL, 잔고 |

### 12.2 텔레그램 명령어

| 명령 | 동작 |
|------|------|
| /status | 포지션, WR, PnL, pending 신호 |
| /balance | 잔고 |
| /market | BTC 가격 |
| /stats | 오늘 매매 통계 |
| /trades | 최근 10건 |
| /risk | DD, WR, PnL |
| /close | 포지션 청산 + 엔진 정지 |
| /clear | 전 주문 취소 |

### 12.3 로그

```
[SCALP] 지표 | K=18.3 D=22.1 MACD=-5.2 Sig=-3.8 | pos=flat pending=None
[SCALP] StochRSI 롱 크로스 감지 → MACD 대기
[SCALP] ENTRY LONG @ $107,500.0 | SL=1.20%
[SCALP] EXIT LONG @ $108,200.0 | PnL $+5.82 (+8.3%) | reason=stoch_rsi_top | 총 3건 WR=67%
[SCALP] STOP LOSS 1.20% >= 1.20%
```

---

## 13. 학술 근거

| 출처 | 결과 |
|------|------|
| StochRSI + MACD 백테스트 (opofinance) | 승률 52~73%, 평균 0.88%/건 (원물) |
| MACD + RSI 조합 논문 (CSI 300, 2015~2025) | MACD 단독 40% → RSI 결합 77% |
| arXiv MACD 비교연구 (미국 주식) | 모멘텀 지표 결합 시 profit factor 유의미 개선 |
| Jay 실전 | "어쩔때는 40프로(10x)도 먹을수있고" |

---

## 14. v4 대비 삭제 목록

| 삭제 대상 | 줄 수 | 이유 |
|-----------|-------|------|
| grid_engine.py | 708줄 | 그리드 전략 폐기 |
| grid_state.py | 105줄 | 그리드 상태 불필요 |
| grid_trades 테이블 | - | scalp_trades로 교체 |
| grid: config 섹션 | - | scalp: 으로 교체 |
| **총 삭제** | **~813줄** | |
