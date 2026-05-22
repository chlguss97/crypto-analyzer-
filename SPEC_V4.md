# SPEC v4 — Minimal Grid Bot (Pionex 철학)

> 작성: 2026-05-22  
> 원칙: **단순, 고정, 멈추지 않음**  
> 근거: Pionex/Bitget/Bybit/KuCoin 4대 플랫폼 공통 설계  
> 핵심: 레짐 감지 없음, 적응형 파라미터 없음, 실행 중 변경 없음  
> 이전: SPEC_V3.md (레짐 감지 기반 — 폐기)

---

## 0. 설계 원칙

| # | 원칙 | 근거 |
|---|------|------|
| 1 | **절대 멈추지 않는다** | 가동률 = 수익. "똑똑한 정지"가 15~30% 수익 감소 (Pionex 유저 데이터) |
| 2 | **파라미터는 시작 시 1회 설정, 실행 중 불변** | Pionex: 실행 중 변경 자체를 막음 — 의도적 설계 |
| 3 | **레짐 감지 없음** | 4대 플랫폼 모두 미사용. 오탐 비용 > 방어 이익 |
| 4 | **추세는 그리드 구조가 흡수** | 한쪽 체결 → 반대쪽 대기 → 복귀 시 사이클 완성 |
| 5 | **유일한 방어: 청산 방지** | Neutral 10x (순노출≈0) + BOT_KILL(-20%) |
| 6 | **단순할수록 강건** | 탈레브/챈: 파라미터 ↑ = 오버피팅 ↑ |

### v3 실패 교훈 → v4 설계 근거

| # | v3 문제 | 원인 | v4 대응 |
|---|--------|------|---------|
| 1 | 20분에 4번 정지 | OBI 포화 (항상 ±1) | 레짐 감지 자체 제거 |
| 2 | CUSUM 100% 죽음 → 과민 보정 → 또 조정 | 파라미터 튜닝 무한루프 | 시그널 처리 없음 |
| 3 | 배포 5회/일 → warm-up 취약구간 반복 | 복잡성이 배포를 유발 | 단순 코드 = 수정 불필요 |
| 4 | 아직 1사이클도 미완성 | 엔지니어링 > 운영 | **돌리는 게 최우선** |

---

## 1. 아키텍처

```
[시작 시 1회]
  잔고 조회 → ATR 계산 → 레벨/spacing/레버리지 결정
  → 그리드 주문 전체 배치
       ↓
[무한 루프]
  체결 감지 (WS push + REST fallback)
  → counter-order 즉시 배치
  → counter 체결 → 원래 레벨 재배치
  → 반복 (멈추지 않음)
       ↓
[예외 상황만]
  서킷브레이커 (극단 급변) → 60초 동결 후 자동 재개
  BOT_KILL (-20% DD) → 전체 청산 + 정지
  drift > 90% 소진 (4시간 체크) → 리빌드 1회
```

---

## 2. 그리드 메커닉

### 2.1 초기 설정 (시작 시 1회)

```python
# 1. ATR에서 spacing 계산
atr_pct = ATR(14, 5m) / price × 100
spacing_pct = clamp(atr_pct × 0.6, 0.15%, 0.50%)

# 2. 잔고 기반 레벨 수
leverage = 10  # 고정 (Neutral 그리드: 순노출≈0, XT 가이드 5~10x 권장)
max_notional = balance × leverage
levels = min(10, max(2, floor(max_notional / (price × 0.01))))
levels = levels - (levels % 2)  # 짝수 강제
half = levels // 2

# 3. 기하식 레벨 배치
ratio = 1 + spacing_pct / 100
for i in 1..half:
    buy_price  = center / ratio^i   # 아래
    sell_price = center × ratio^i   # 위
```

### 2.2 주문 규칙

| 규칙 | 상세 |
|------|------|
| 전 주문 post-only limit | OKX `orderType: post_only` — maker 0.02% 보장 |
| post-only 거부 시 | 1틱 조정 재시도 1회, 실패 시 포기 (market fallback 절대 없음) |
| Hedge Mode | OKX `posSide: long/short` — 양방향 동시 보유 |
| Margin Mode | `isolated` — 그리드 자본만 격리 |
| 사이즈 | 0.01 BTC (1계약) / 레벨 — OKX 최소단위 |
| 레버리지 | **10x 고정** (Neutral 모드, 시작 시 1회 설정) |

### 2.3 상태 머신 (레벨별)

```
[EMPTY]
  → place limit order (buy or sell)
  → 성공: [PLACED], 실패: 재시도 후 [EMPTY] 유지

[PLACED]
  → WS push "filled" → [FILLED]
  → WS push "canceled" → [EMPTY] (재배치)
  → REST fallback 감지 → [FILLED] or [EMPTY]

[FILLED]
  → counter-order 즉시 배치
  → 성공: [COUNTER_PLACED]
  → 실패: 재시도 (최대 3회, 간격 2초)

[COUNTER_PLACED]
  → WS push "filled" → [CYCLE_COMPLETE]
  → WS push "canceled" → counter 재배치

[CYCLE_COMPLETE]
  → PnL 계산 + DB 기록 + 텔레그램 알림
  → risk_manager 갱신
  → 원래 레벨 재배치 → [EMPTY]
  → 무한 반복
```

### 2.4 부분 체결 (Partial Fill)

```
OKX BTC-USDT-SWAP에서 0.01 BTC(1계약)은 거의 항상 원자적 체결.
partially_filled 이벤트는 무시 — filled만 처리.
(Bitget/Bybit도 소량 그리드에서 동일 정책)
```

### 2.4 수익 공식

```
건당 gross = 0.01 BTC × spacing_abs
건당 수수료 = 0.01 × price × maker_fee × 2 (양방향)
건당 net = gross - 수수료

예시 (BTC=$78,000, spacing=0.15%):
  gross = 0.01 × $117 = $1.17
  수수료 = 0.01 × $78,000 × 0.0004 = $0.31
  net = $0.86
```

---

## 3. 체결 감지

### 3.1 Primary: OKX Private WebSocket (10~50ms)

```
WS 채널: orders (instType: SWAP)
이벤트: 주문 상태 변경 시 즉시 push
처리: filled → counter-order 배치, canceled → 상태 업데이트
```

### 3.2 Fallback: REST 폴링 (30초마다)

```
fetch_open_orders → 현재 OKX 주문과 로컬 상태 대조
WS 누락 감지 → 보정
```

### 3.3 Counter-Order 배치

```
buy 체결 @ fill_price → sell limit @ fill_price × ratio (reduce_only)
sell 체결 @ fill_price → buy limit @ fill_price / ratio (reduce_only)
```

---

## 4. 안전장치 (2개만)

### 4.1 서킷브레이커

```
조건: 10초 내 가격 2% 이동
동작: 전 주문 취소 + 60초 동결 → 자동 리빌드
빈도: 극히 드묾 (정상 운영 방해 없음)
```

### 4.2 BOT_KILL

```
조건: 계좌 DD -20% (peak 대비)
동작: 전 포지션 시장가 청산 + 봇 정지
복구: 수동 재시작 필요
```

**제거된 것:**
- ~~RegimeDetector~~ (레짐 감지 없음)
- ~~적응형 레버리지~~ (10x 고정)
- ~~적응형 spacing~~ (시작 시 1회 계산)
- ~~주기적 리밸런스~~ → DGT 경계 돌파 시 즉시 리빌드
- ~~CRS/OBI/CVD/CUSUM~~ (시그널 처리 없음)

---

## 5. DGT 리빌드 (Dynamic Grid Trading — Chen et al. 2025)

```
정적 그리드 기대수익 = 0 (수학 증명, arXiv:2506.11921)
DGT: 가격이 경계 돌파 시 즉시 center 재설정 → 양의 기대수익

트리거: 현재가 > 그리드 상단 OR 현재가 < 그리드 하단
체크: 메인 루프 매 틱 (1초)
동작:
  1. 미체결 주문 전부 취소 (counter-order는 유지)
  2. 현재가를 새 center로 설정
  3. ATR 재계산 (최신 spacing 반영)
  4. 새 그리드 레벨 배치
  5. 기존 사이클 카운트/PnL 유지

상단 돌파 시: 이전 사이클 이익 확정됨 → 즉시 재배치
하단 돌파 시: 미체결 counter 유지 → 새 레벨 추가 배치
```

이 외에 **실행 중 파라미터 변경 없음**.

---

## 6. 추세장 대응 (그리드 자체 흡수)

### 6.1 양방향 그리드의 추세 흡수 메커니즘

```
시나리오 A: BTC $78,000 → $79,000 상승 (+1.3%)

  Lv1 sell @ $78,117 체결 → short 0.01 BTC 보유
  counter buy @ $78,000 대기
  
  Lv-1 buy @ $77,883 → 안 닿음 (그대로 대기)
  
  가격 $79,000에서 횡보/하락 시작
  → $78,000 복귀 → counter buy 체결 → 사이클 완성 ($0.86)
  → Lv1 sell 재배치
  
  결과: 추세 중에도 sell 사이클이 완성됨. buy는 대기 중.

시나리오 B: BTC $78,000 → $76,000 하락 (-2.6%)

  Lv-1 buy @ $77,883 체결 → long 0.01 BTC 보유
  counter sell @ $78,000 대기
  
  Lv-2 buy @ $77,766 체결 → long 0.02 BTC 보유
  counter sell @ $77,883 대기
  
  가격 $76,000에서 반등
  → $77,883 복귀 → Lv-2 counter 체결 → 1사이클 완성
  → $78,000 복귀 → Lv-1 counter 체결 → 2사이클 완성
  
  결과: 추세 끝나면 역순으로 사이클 전부 완성.
```

### 6.2 최악 시나리오 분석

```
BTC 10% 일방향 하락 ($78,000 → $70,200):

  2레벨 그리드 ($180 자본, 5x):
  - Lv-1 buy 체결, Lv-2 buy 체결
  - long 0.02 BTC, 평균 진입 ~$77,825
  - 미실현 손실: 0.02 × ($77,825 - $70,200) = -$152.5
  - $180 자본 대비 -84.7% → BOT_KILL(-20%) 발동 → 청산
  
  문제: 5x 레버리지에서 10% 하락은 치명적.
  
  대응: 이건 그리드의 구조적 한계.
  - 레버리지 5x가 이 시나리오의 유일한 방어
  - BOT_KILL -20%가 최종 안전망
  - 10% 무조정 하락은 BTC에서 월 1~2회 발생
  - 이때 BOT_KILL로 $36 손실 → 나머지 $144 보전
  - 사이클 수익으로 복구 필요 (~42사이클 = 약 2주)
```

### 6.3 추세 발생 시 봇 행동 요약

| 시나리오 | 봇 행동 | 결과 |
|----------|---------|------|
| ±0.5% 횡보 | 사이클 반복 | **수익** |
| ±2% 스윙 | 한쪽 체결 → 복귀 시 완성 | **수익** (느림) |
| ±5% 추세 후 복귀 | 일시 미실현 손실 → 복귀 시 사이클 완성 | **수익** (지연) |
| ±5% 추세 지속 | drift 리빌드 (4h 체크) | **새 center로 재시작** |
| ±10% 급변 | 서킷브레이커 60초 동결 | **동결 후 자동 재개** |
| ±20% 폭락 | BOT_KILL 발동 → 전체 청산 | **$36 손실, 재시작 필요** |

---

## 7. 데이터 계층

### 7.1 수집

| 소스 | 채널 | 용도 |
|------|------|------|
| OKX WS Public | tickers | 가격 |
| OKX WS Public | candles (5m) | ATR 계산 (시작 시) |
| OKX WS Private | orders | 체결 감지 (primary) |
| OKX REST | fetch_open_orders | 체결 감지 (fallback 30초) |

**제거된 것:**
- ~~OKX trades 스트림~~ (볼륨/CVD 분석 불필요)
- ~~OKX books5~~ (OBI 분석 불필요)
- ~~Binance 스트림~~ (전부 불필요)

### 7.2 Redis 키

```
# 가격
rt:price:BTC-USDT-SWAP          → float

# 그리드 상태
grid:state:BTC/USDT:USDT        → JSON (GridState)

# 시스템
sys:balance                     → float
sys:bot_status                  → "running" | "stopped"

# 리스크 (모니터링용)
risk:streak                     → int
risk:daily_pnl                  → float
```

### 7.3 SQLite

```sql
CREATE TABLE candles (
    symbol TEXT, timeframe TEXT, timestamp INTEGER,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, timeframe, timestamp)
);

CREATE TABLE grid_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grid_id TEXT, level_id INTEGER, cycle_num INTEGER,
    side TEXT, entry_price REAL, exit_price REAL,
    size_btc REAL, pnl_usdt REAL, fee_total REAL,
    entry_time INTEGER, exit_time INTEGER, spacing_pct REAL
);
```

---

## 8. 파일 구조

```
src/
├── main.py                    # 엔트리포인트
├── data/
│   ├── storage.py             # SQLite + Redis
│   ├── ws_stream.py           # OKX Public WS (tickers + candles만)
│   ├── order_stream.py        # OKX Private WS (체결 감지)
│   └── candle_collector.py    # 캔들 백필
├── strategy/
│   └── grid_engine.py         # 그리드 코어 (전체 로직)
├── trading/
│   ├── executor.py            # OKX 주문 실행
│   ├── grid_state.py          # 그리드 상태 구조체
│   └── risk_manager.py        # BOT_KILL + 모니터링
├── monitoring/
│   ├── dashboard.py           # FastAPI
│   ├── telegram_bot.py        # 알림
│   ├── trade_logger.py        # JSONL 기록
│   └── static/index.html      # 대시보드 UI
└── utils/
    └── helpers.py             # config/env

삭제:
  ❌ regime_detector.py (445줄 전부 삭제)
```

---

## 9. 설정 (settings.yaml)

```yaml
strategy: "grid"

exchange:
  name: "okx"
  symbol: "BTC/USDT:USDT"
  margin_mode: "isolated"

grid:
  enabled: true
  leverage: 10                    # Neutral 고정 (순노출≈0)
  size_btc: 0.01                  # 레벨당 1계약
  atr_mult: 0.6                   # spacing = ATR% × 0.6
  spacing_min_pct: 0.15           # 최소 spacing
  spacing_max_pct: 0.50           # 최대 spacing
  spacing_type: "geometric"       # 기하식
  atr_period: 14
  atr_timeframe: "5m"

safety:
  circuit_breaker_pct: 2.0        # 10초 내 2% → 60초 동결
  circuit_breaker_window_sec: 10
  circuit_breaker_freeze_sec: 60
  bot_kill_drawdown_pct: 20       # DD -20% → 전체 청산

fees:
  maker: 0.0002
  taker: 0.0005

telegram:
  enabled: true

redis:
  host: "localhost"
  port: 6379
  db: 0
```

---

## 10. 크래시 복구

### 10.1 복구 흐름

```
봇 시작 시:
  1. Redis에서 grid state 로드 (grid:state:BTC/USDT:USDT)
     → state 있음: 기존 그리드 복구
     → state 없음: 신규 그리드 생성 (섹션 2.1)

  2. OKX에서 실제 상태 조회
     → fetch_positions(): 현재 보유 포지션
     → fetch_open_orders(): 현재 미체결 주문

  3. 레벨별 대조 (state vs OKX):
     a) status=placed인데 OKX에 주문 없음:
        → fetch_order(id)로 체결 여부 확인
        → closed: filled 처리 + counter 배치
        → canceled: 레벨 재배치
     
     b) counter_status=placed인데 OKX에 주문 없음:
        → fetch_order(id)로 체결 여부 확인
        → closed: 사이클 완성 처리
        → canceled: counter 재배치
     
     c) state 없는데 포지션 있음 (나체 포지션):
        → 시장가 청산 (reduce_only)
        → 미체결 주문 전부 취소

  4. cancelled 상태 레벨 재배치

  5. state 저장 + 정상 루프 진입
```

### 10.2 핵심 원칙

```
OKX가 진실의 원천 (Single Source of Truth).
봇이 죽어도 OKX에 걸린 주문은 살아있음.
복구 = OKX 실제 상태와 로컬 상태의 동기화.
```

### 10.3 WS 연결 끊김 시

```
Private WS (orders) 끊김:
  → 자동 재연결 (지수 백오프: 5s, 10s, 20s... max 60s)
  → 재연결 후: REST로 한번 전체 동기화
  → 끊긴 사이 체결된 주문 감지 + 처리

Public WS (tickers/candles) 끊김:
  → 자동 재연결
  → 가격 정보 일시 중단 (서킷브레이커 감지 불가)
  → 그리드 주문은 OKX에서 계속 동작 (영향 없음)
```

---

## 11. 모니터링

### 11.1 텔레그램 알림

| 이벤트 | 메시지 | 빈도 |
|--------|--------|------|
| 봇 시작 | 잔고, 레벨 수, spacing, center | 시작 시 |
| 사이클 완성 | 레벨, 진입→청산, PnL, 누적 | 체결 시 |
| 서킷브레이커 | 트리거 가격, 변동폭 | 극히 드묾 |
| BOT_KILL | 청산 완료, 잔여 잔고 | 극히 드묾 |
| drift 리빌드 | 새 center, spacing | 4시간 체크 시 |
| 일일 리포트 | 사이클 수, PnL, 잔고, 가동시간 | 매일 00:00 UTC |

### 11.2 텔레그램 명령어

| 명령 | 동작 |
|------|------|
| /status | 모드, 잔고, 사이클 수, PnL |
| /balance | 잔고 + 미실현 PnL |
| /grid | 레벨별 상태 (가격, placed/filled/counter) |
| /close | 전 포지션 청산 + 봇 정지 |
| /help | 명령어 목록 |

### 11.3 대시보드

```
카드 1: 헤더 (잔고, 가동시간, 상태)
카드 2: BTC 현재가 + 24h 범위
카드 3: 그리드 레벨 시각화 (가격, 상태)
카드 4: 사이클 히스토리 (최근 20건)
카드 5: 리스크 (DD, streak, daily PnL)
```

### 11.4 로그

```
[GRID] 사이클 완성 Lv-1: $77,438→$77,554 PnL=$+0.86 (총 5사이클 $+4.30)
[GRID] drift 리빌드: center $77,554→$78,200 spacing=0.18%
[CB] 서킷브레이커! $78,000→$76,440 (-2.0%) → 60초 동결
[KILL] BOT_KILL DD -20% → 전체 청산, 잔고 $144
```

---

## 12. 수익 모델

### 자본별 예상 (10x, DGT)

| 자본 | 레벨 | 사이클/일 | 월 예상 |
|------|------|----------|---------|
| $180 | 2 (1+1) | 4~8 | $50~90 |
| $500 | 4 (2+2) | 8~15 | $130~220 |
| $1,000 | 8 (4+4) | 15~25 | $250~400 |
| $2,000 | 10 (5+5) | 20~35 | $350~550 |

### 핵심 전제
- **가동률 90%+** (멈추지 않으니까)
- spacing 0.15%~0.50% (ATR 기반)
- maker 0.02% 양방향
- BTC 일일 변동 1~3%

---

## 12. 이전 대비 삭제 목록

| 삭제 대상 | 줄 수 | 이유 |
|-----------|-------|------|
| regime_detector.py | 445줄 | 레짐 감지 철학 제거 |
| grid_engine 레짐 콜백 | ~100줄 | 모드 전환 불필요 |
| grid_engine 적응형 레버리지 | ~30줄 | 5x 고정 |
| ws_stream trades/books5 구독 | ~80줄 | 시그널 분석 불필요 |
| binance_stream.py | 전체 | 불필요 |
| main.py RegimeDetector 관련 | ~20줄 | |
| dashboard 레짐 엔드포인트 | ~40줄 | |
| **총 삭제** | **~715줄** | |
