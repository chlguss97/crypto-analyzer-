# SPEC v5 — ScalpBot (Jay 단타법)

> 작성: 2026-05-29 (최종 갱신)
> 원칙: **볼밴 타점 + 후행 확인 + 먹고 나감**
> 근거: Jay 단타법 + 학술 백테스트 (BB+StochRSI+MACD 승률 76%+)
> 이전: SPEC_V4.md (Minimal DGT Grid — 폐기)

---

## 0. 설계 원칙

| # | 원칙 | 근거 |
|---|------|------|
| 1 | **볼밴이 타점** | Jay: "타점은 항상 볼밴. 상단에서 숏, 하단에서 롱" |
| 2 | **20이평 매매 금지** | Jay: "20이평만 피해서 매매하면 돈을 번다" |
| 3 | **후행 확인 후 진입** | StochRSI+MACD 크로스 = 신호 확인 |
| 4 | **먹고 나간다** | StochRSI 도달 = 즉시 청산 |
| 5 | **볼밴 터지면 나간다** | SL = BB 밴드 이탈 |
| 6 | **단순할수록 강건** | 지표 3개만 (BB + StochRSI + MACD) |

### 3층 구조

```
1층 (타점):  볼린저밴드 상하단 = 진입 허용 구간
2층 (신호):  StochRSI + MACD 크로스 = 방향 확인
3층 (금지):  BB 중간(20이평) = 매매 금지 구간
```

---

## 1. 아키텍처

```
[시작 시 1회]
  잔고 조회 → 크래시 복구 → 레버리지 설정
  → Redis pub/sub 구독 (ch:kline:ready)
       ↓
[캔들 닫힘 이벤트 (30m)]
  → DB에서 최근 100개 캔들 로드
  → BB + StochRSI + MACD 계산
  → BB 위치 판단:
    ├── BB 중간(35~65%) → 매매 금지, 스킵
    ├── BB 하단(<35%) + 롱 신호 → 시장가 진입
    ├── BB 상단(>65%) + 숏 신호 → 시장가 진입
    └── 청산 조건 (StochRSI 도달) → 시장가 청산
       ↓
[0.5초 루프]
  → BB SL 체크 (롱→BB하단 이탈, 숏→BB상단 이탈)
  → BOT_KILL 체크 (30초마다)
  → 서킷브레이커 체크

[주기적 태스크]
  → 포지션 체크 (30초) → Redis pos:active: 동기화
  → 캔들 정리 (24시간) → 1m 3일, 5m 7일, 15m 30일 보존
  → 잔고 heartbeat (60초) → Redis sys:balance
  → 일일 리포트 (UTC 00:00) → 텔레그램
```

---

## 2. 매매 로직

### 2.1 지표

| 지표 | 설정 | 용도 |
|------|------|------|
| **Bollinger Bands** | period=20, std=2.0 | 타점 (상하단) + SL |
| **Stochastic RSI** | RSI(14), Stoch(14), K(3), D(3) | 과매수/과매도 크로스 |
| **MACD** | fast=8, slow=26, signal=9 | 모멘텀 크로스 확인 |
| **ATR** | period=14 | SL fallback |
| **4h MACD** | fast=8, slow=26, signal=9 | 큰 방향 필터 (0선 기준) |

### 2.2 롱 진입 (4조건 동시 충족)

```
조건 1: 가격이 BB 하단 구간 (BB position < 40%)
조건 2: StochRSI K < 20 (바닥권) AND K가 D를 상향 돌파 (골든크로스)
조건 3: MACD 라인이 Signal 라인을 상향 돌파 (골든크로스)
조건 4: 4시간 MACD > 0 (큰 방향이 상승일 때만)
필터:  K > 70 이면 스킵 (소진)
```

### 2.3 숏 진입 (4조건 동시 충족)

```
조건 1: 가격이 BB 상단 구간 (BB position > 60%)
조건 2: StochRSI K > 80 (상단권) AND K가 D를 하향 돌파 (데드크로스)
조건 3: MACD 라인이 Signal 라인을 하향 돌파 (데드크로스)
조건 4: 4시간 MACD < 0 (큰 방향이 하락일 때만)
필터:  K < 30 이면 스킵 (소진)
```

### 2.4 BB 중간 매매 금지

```
BB position 35% ~ 65% → 진입 불가
Jay: "20이평은 쓰레기 평단. 뭐가 나올지 아무것도 모르는 구간"
pending_signal 등록은 유지, 타임아웃만 진행
```

### 2.5 청산

```
롱 청산: StochRSI K > 80 도달 → 즉시 시장가 청산
숏 청산: StochRSI K < 20 도달 → 즉시 시장가 청산
```

### 2.6 대기 신호 (비동시 크로스)

```
StochRSI와 MACD가 동시에 크로스하지 않는 경우:
  → pending_signal 등록 (BB 위치 무관, 어디서든 감지)
  → 5캔들(2.5시간) 내 두 번째 조건 충족 + BB 구간 OK → 진입
  → 5캔들 초과 시 폐기
  
신호 등록은 BB 중간에서도 가능 (진입만 BB 상하단에서)
```

### 2.7 거짓 신호 필터

```
MACD 골든크로스인데 StochRSI K > 70 → 롱 스킵 (소진)
MACD 데드크로스인데 StochRSI K < 30 → 숏 스킵 (소진)
```

---

## 3. Stop Loss — BB 밴드 이탈

### 3.1 Primary: BB 이탈

```
롱 SL: 가격 < BB 하단 → 즉시 청산 ("bb_lower_breach")
숏 SL: 가격 > BB 상단 → 즉시 청산 ("bb_upper_breach")

Jay: "볼밴이 터지면 틀린거. 내 손절은 하단이니까"
```

### 3.2 Fallback: ATR (BB 값 없을 때)

```python
atr_pct = ATR(14, TF) / price × 100
sl_pct = clamp(atr_pct × 1.0, 0.5%, 2.0%)
```

체크 주기: **0.5초** (실시간 Redis 가격)

---

## 4. 포지션 관리

| 규칙 | 상세 |
|------|------|
| 동시 포지션 | **1개만** (롱 또는 숏, 동시 없음) |
| 주문 타입 | 시장가 (taker 0.05%) |
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

## 6. 타임프레임

| TF | 설정 |
|----|------|
| **30m** (현재) | `scalp.timeframe: "30m"` |
| 1h (대안) | `scalp.timeframe: "1h"` |

Jay: "30분이나 한시간을 주로 보는거같아요"
현재 30m 사용 (매매 빈도 2배). settings.yaml에서 전환 가능.

---

## 7. 데이터 계층

### 7.1 수집

| 소스 | 채널 | 용도 |
|------|------|------|
| OKX WS Public | tickers | 실시간 가격 (SL 체크) |
| OKX WS Business | candles 8종 (1m~1w, 30m 포함) | 지표 계산 |
| OKX WS Private | orders | 체결 감지 |
| OKX REST | candles | 백필 + fallback (120초) |

### 7.2 Redis 키

```
rt:price:BTC-USDT-SWAP          → float (실시간 가격)
rt:ticker:BTC-USDT-SWAP         → hash (bid/ask/vol/high/low)
scalp:state:BTC/USDT:USDT       → JSON (ScalpState, TTL 24h)
pos:active:BTC/USDT:USDT        → hash (direction/size/entry/pnl, TTL 60s, 30초 갱신)
sys:balance                     → float (TTL 300s, 60초 갱신)
sys:bot_status                  → "running" | "stopped"
sys:autotrading                 → "on" | "off"
sys:last_heartbeat              → unix timestamp (TTL 120s)
risk:streak                     → int
risk:daily_pnl                  → float
```

### 7.3 캔들 보존 정책

| TF | 보존 기간 | 정리 주기 |
|----|-----------|----------|
| 1m | 3일 | 24시간 |
| 5m | 7일 | 24시간 |
| 15m | 30일 | 24시간 |
| 30m~1w | 무기한 | 정리 안 함 |

### 7.4 SQLite (scalp.db)

```sql
CREATE TABLE candles (
    symbol TEXT, timeframe TEXT, timestamp INTEGER,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    UNIQUE(symbol, timeframe, timestamp)
);

CREATE TABLE scalp_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    size_btc REAL NOT NULL,
    pnl_usdt REAL NOT NULL,
    fee_total REAL NOT NULL,
    entry_time INTEGER NOT NULL,
    exit_time INTEGER NOT NULL,
    exit_reason TEXT NOT NULL,
    timeframe TEXT NOT NULL
);
```

---

## 8. 파일 구조

```
src/
├── main.py                    # 엔트리포인트 (ScalpBot)
├── data/
│   ├── storage.py             # SQLite + Redis
│   ├── ws_stream.py           # OKX Public WS (tickers + candles 8종)
│   ├── order_stream.py        # OKX Private WS (체결 감지)
│   └── candle_collector.py    # 캔들 백필
├── strategy/
│   ├── scalp_engine.py        # 스캘프 코어 (BB + StochRSI + MACD)
│   ├── scalp_state.py         # ScalpState + Redis 직렬화
│   └── indicators.py          # BB, StochRSI, MACD, ATR (numpy)
├── trading/
│   ├── executor.py            # OKX 주문 실행
│   └── risk_manager.py        # BOT_KILL + 모니터링
├── monitoring/
│   ├── dashboard.py           # FastAPI 대시보드
│   ├── telegram_bot.py        # 알림 + 명령어
│   ├── trade_logger.py        # JSONL 기록
│   └── static/index.html      # 대시보드 UI
└── utils/
    └── helpers.py             # config/env
```

---

## 9. 설정 (settings.yaml)

```yaml
strategy: "scalp"

exchange:
  name: "okx"
  symbol: "BTC/USDT:USDT"
  margin_mode: "isolated"

scalp:
  enabled: true
  leverage: 10
  timeframe: "30m"
  size_btc: 0.01
  signal_timeout_candles: 3
  cooldown_sec: 0

  # StochRSI
  stoch_rsi_period: 14
  stoch_rsi_k: 3
  stoch_rsi_d: 3
  stoch_rsi_ob: 80
  stoch_rsi_os: 20

  # MACD (Jay: fast=8)
  macd_fast: 8
  macd_slow: 26
  macd_signal: 9

  # 볼린저밴드
  bb_period: 20
  bb_std: 2.0
  bb_mid_avoid_pct: 30       # BB 중간 35~65% 매매 금지

  # SL
  sl_atr_mult: 1.0           # ATR fallback
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

## 10. 크래시 복구

```
봇 시작 시:
  1. Redis에서 ScalpState 로드
  2. OKX에서 실제 포지션 조회
  3. 대조:
     a) state=long인데 포지션 없음 → flat 리셋
     b) state=short인데 포지션 없음 → flat 리셋
     c) state 없는데 포지션 있음 → 고아 포지션 시장가 청산
  4. 레버리지 설정 (복구 후 — 포지션 있으면 실패하므로)
  5. 정상 루프 진입
```

---

## 11. 모니터링

### 11.1 텔레그램 알림

| 이벤트 | 메시지 |
|--------|--------|
| 봇 시작 | 잔고, TF, 레버리지, MACD 설정 |
| 진입 | 방향, 가격, BB SL 가격 |
| 청산 | 방향, 진입→청산, PnL, 이유, WR |
| 서킷브레이커 | 변동폭, 동결 시간 |
| BOT_KILL | 청산 완료, 잔여 잔고 |
| 일일 리포트 | 매매 수, PnL, 잔고 |

### 11.2 텔레그램 명령어

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

### 11.3 JSONL 이벤트 (trades_*.jsonl)

| 이벤트 | 주기 | 내용 |
|--------|------|------|
| `scalp_signal` | 30분 (캔들 닫힐 때) | K, D, MACD, Sig, BB%, 가격, 크로스 여부 |
| `scalp_entry` | 진입 시 | 방향, 가격, BB 상하단, SL |
| `scalp_exit` | 청산 시 | 방향, 진입→청산, PnL, 이유, WR |
| `circuit_breaker` | 발동 시 | 변동%, 가격, 동결 시간 |
| `bot_kill` | 발동 시 | DD%, peak, balance |
| `hourly_snapshot` | 1시간 | 잔고, 포지션, PnL, WR |

### 11.4 로그 저장

| 파일 | 레벨 | 보존 |
|------|------|------|
| `bot.log` | INFO+ | 주간 로테이션 (520주) |
| `trades_*.jsonl` | 이벤트별 | 주간 파일 (영구) |
| Docker 콘솔 | INFO+ | 재시작 시 유실 |

### 11.5 로그 포맷

```
[SCALP] 지표 | K=18.3 D=22.1 MACD=-5.2 Sig=-3.8 | BB=15% ($106,500-$108,200) | pos=flat
[SCALP] ENTRY LONG @ $106,800.0 | SL=BB 하단 $106,500
[SCALP] EXIT LONG @ $107,900.0 | PnL $+8.12 (+11.6%) | reason=stoch_rsi_top
[SCALP] BB SL: price $106,400 < BB하단 $106,500
```

---

## 12. 학술 근거

| 출처 | 결과 |
|------|------|
| BB + RSI 조합 (ResearchGate) | 승률 87.5% |
| BB + MACD + StochRSI (FMZ) | 승률 76%+ |
| StochRSI + MACD 단독 (opofinance) | 승률 52~73% |
| BB mean reversion (QuantifiedStrategies) | 승률 58~65% |
| Jay 실전 | "볼밴이 붕괴 안 하면 맞는다, 기다리면 먹여준다" |
