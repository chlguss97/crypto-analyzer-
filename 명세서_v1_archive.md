# OKX CryptoAnalyzer v1.0 — 설계서

> Python + ccxt / OKX 선물 (Perpetual Swap)  
> 작성일: 2026-04-03 / 최종 갱신: 2026-04-08  
> 매매 스타일: **스캘핑 중점** + Swing 보조 (보유 수분~수시간, 러너 모드 시 최대 8시간)

---

## ⚡ 현재 상태 요약 (2026-04-08)

> 이 박스는 본문 Part 1~16 의 원본 설계와 실제 운영 상태의 차이를 빠르게 보여줌.  
> 본문은 2026-04-03 시점 설계서이며, 그 이후 변경은 **Part 17~22** 와 [`CHANGELOG.md`](CHANGELOG.md) 에 정리.

| 항목 | 원본 설계 (2026-04-03) | 현재 (2026-04-08) | 참조 |
|---|---|---|---|
| 기법 수 | 14개 (단일 모델) | Swing 14 + **Scalp 18종** 듀얼, 스캘핑 중점 | Part 19 |
| ML | RF + Walk-Forward | **AdaptiveML v2** — 레짐별 GBM+RF+LR 앙상블, 60+ 피처, MetaLearner | Part 17 |
| 레짐 판별 | (없음) | 4레짐 (trending_up/down/ranging/volatile) + 레짐별 전략 | Part 18 |
| 학습 시스템 | 백테스트만 | **HistoricalLearner + PaperTrader + AutoBacktest + 스케줄러 (UTC 22/04/11)** | Part 20 |
| 보호 주문 | ATR×1.2 SL 단일 | **OKX 서버사이드 SL+TP1/TP2/TP3 등록 + 반익본절 + 러너 트레일링** | Part 22 |
| 사이즈/SL 모드 | risk_per_trade 0.5% 고정 | **`margin_loss_cap`** (마진 손익% 한도) 기본 + risk_per_trade 보존 | Part 22 |
| 매매-학습 격리 | (없음) | `sys:learning=1` 동안 신규 진입 차단, 활성 포지션 5초 폴링 | Part 20 |
| 사용자 수동 SL/TP | (없음) | 대시보드/API 로 수정, 트레일/self-heal 이 존중 | Part 22 |
| 추가 모듈 | — | FractalIndicator, NewsFilter, SignalTracker | CHANGELOG |
| 배포 | 로컬 | **Vultr Singapore + Docker Compose + 헬스체크 + 디지스트 자동 푸시** | Part 21 |
| 대시보드 | 단일 페이지 | 4탭 거래소 스타일 + EN/KR + 수동 SL/TP | CHANGELOG |
| 변경 인덱스 | — | `COMMIT_LOG.md` 자동 갱신 (post-commit hook) | MANUAL Section 5 |

> 본문 중 ⚠️ 가 붙은 섹션은 갱신본이 Part 17+ 에 있음을 의미.

---

## Part 1. 기능 프레임워크 (큰 틀)

```
┌─────────────────────────────────────────────────────────────┐
│            CryptoAnalyzer v1.0 (Short-Term Trading)         │
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────┐  │
│  │ 데이터    │→│ 기법엔진  │→│ 시그널    │→│ 자동매매    │  │
│  │ 수집/저장 │  │ (14개)   │  │ 합산기   │  │ 엔진       │  │
│  └──────────┘  └──────────┘  └──────────┘  └────────────┘  │
│        ↕              ↕             ↕             ↕         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────┐  │
│  │ 백테스트  │  │ ML엔진   │  │ 리스크   │  │ 모니터링   │  │
│  │          │  │ (RF+WF)  │  │ 관리     │  │ + 알림     │  │
│  └──────────┘  └──────────┘  └──────────┘  └────────────┘  │
│                                                             │
│  기술스택: Python 3.11 / ccxt / scikit-learn / FastAPI      │
│  DB: SQLite(캔들) + Redis(실시간) / 알림: Telegram          │
└─────────────────────────────────────────────────────────────┘
```

### 타임프레임 전략

```
┌─────────────────────────────────────────────────┐
│           멀티 타임프레임 계층 구조               │
│                                                 │
│  [필터 TF]  4H   → 큰 추세 방향 + OB 영역 확인   │
│       ↓                                         │
│  [확인 TF]  1H   → 추세 구조 (BOS/CHoCH) 확인    │
│       ↓                                         │
│  [실행 TF]  15m  → 진입 타이밍 + 청산 관리        │
│                                                 │
│  ※ 1D는 당일 바이어스 참고용                      │
└─────────────────────────────────────────────────┘
```

### 모듈 요약

| 모듈 | 역할 |
|------|------|
| **데이터 수집** | REST+WS로 캔들/틱/오더북/OI/펀딩비 수집, SQLite 저장 |
| **기법 엔진** | 14개 기법 — Fast Path(실시간) + Slow Path(주기적) 2단계 |
| **시그널 합산기** | 기법별 시그널 가중 합산 → 진입등급(A/B/C/D) |
| **ML 엔진** | Random Forest + Walk-Forward |
| **자동매매** | 주문실행(시장가/지정가), 포지션관리, 미체결추적 |
| **리스크 관리** | ATR기반 SL/TP, 트레일링, 시간청산, 수수료 필터 |
| **백테스트** | 과거데이터 시뮬레이션, 수수료/슬리피지/펀딩비 반영 |
| **모니터링** | 웹 대시보드(FastAPI), 텔레그램 알림, P&L 추적 |

### 시그널 처리 — 2단계 구조

```
┌─ Fast Path (실시간, 15m봉 갱신 시 즉시 계산) ────┐
│  - EMA 정배열        - RSI                       │
│  - Bollinger Bands   - VWAP                      │
│  - Market Structure (BOS/CHoCH)                  │
│  → 즉각적 진입/청산 판단에 사용                     │
└──────────────────────────────────────────────────┘
        ↓ (합류)
┌─ Slow Path (1~5분 주기) ────────────────────────┐
│  - Order Block       - FVG                       │
│  - OI / Funding Rate - 롱숏비율                   │
│  - CVD               - 거래량 패턴                │
│  - Liquidation Level                             │
│  → 배경 컨텍스트, 시그널 강도 보정                  │
└──────────────────────────────────────────────────┘
```

### 거래 대상

```yaml
# BTC 무기한 선물 (주력)
primary: "BTC/USDT:USDT"    # USDT-M 무기한
leverage: 10~30x (등급 + ATR 연동 동적 조절)
margin: isolated             # 격리 마진

# 확장 가능 (Phase 2)
secondary:
  - "ETH/USDT:USDT"
  - "SOL/USDT:USDT"
```

---

## Part 2. BTC 선물 매매 기법 명세 (14개)

> 각 기법은 독립적으로 시그널을 산출하고,  
> 시그널 합산기에서 가중치를 적용해 최종 매매 결정  
> **모든 파라미터는 15m/1H 기준으로 최적화**

---

### 기법 1. Order Block (오더블록) ★ 핵심 — Slow Path

**개념:** 기관이 대량 주문을 실행한 가격 영역. 가격이 해당 영역에 돌아오면 반응할 확률이 높다.

```
Bullish OB: 강한 상승 임펄스 직전의 마지막 음봉(하락 캔들)
 ┌──────┐
 │▓▓▓▓▓▓│ ← 이 음봉 = Bullish OB
 └──────┘
    ↑↑↑↑↑↑↑  (강한 상승 임펄스)

Bearish OB: 강한 하락 임펄스 직전의 마지막 양봉(상승 캔들)
    ↓↓↓↓↓↓↓  (강한 하락 임펄스)
 ┌──────┐
 │░░░░░░│ ← 이 양봉 = Bearish OB
 └──────┘
```

**탐지 로직:**
```
1. 임펄스 감지: N봉 내 ATR × 배수 이상 한 방향 이동
2. 임펄스 시작 직전 반대방향 캔들 식별 = OB 후보
3. OB 영역 = 해당 캔들의 [Low ~ High]
4. 강도 계산:
   - 임펄스 크기 (ATR 배수) → 클수록 강함
   - 거래량 비율 (OB 캔들 vol / 20봉 평균 vol)
   - 상위 TF OB와 겹침 여부 → 겹치면 강도 ×1.5
5. 소진(mitigation) 추적: 가격이 OB를 완전히 관통하면 무효화
```

**파라미터:**

| 파라미터 | 15m | 1H | 4H (참고) |
|---------|------|------|----------|
| 임펄스 기준 | ATR × 1.5 | ATR × 2.0 | ATR × 2.5 |
| 최대 유효기간 | 24시간 | 3일 | 7일 |
| 최대 리테스트 | 2회 | 3회 | 3회 |
| 최소 거래량 비율 | 1.3× | 1.5× | 1.5× |

**시그널 출력:**
```python
{
    'type': 'order_block',
    'direction': 'long',        # 'long' | 'short'
    'strength': 0.82,           # 0~1 (강도)
    'ob_zone': [64200, 64800],  # OB 가격 범위
    'distance_pct': 0.3,        # 현재가 대비 거리(%)
    'htf_aligned': True,        # 상위TF 방향 일치 여부
    'retest_count': 1,          # 리테스트 횟수
    'age_hours': 3.5,           # OB 생성 후 경과 시간
}
```

**진입가 산정 — OTE (Optimal Trade Entry):**
```
OB 영역의 50% 지점 (피보나치 0.5 리트레이스먼트)
= (OB_high + OB_low) / 2

예: OB = [64,200 ~ 64,800]
    OTE = 64,500 에 지정가 진입
    Grade A 시: OB 터치 즉시 시장가 진입도 허용
```

---

### 기법 2. Fair Value Gap (FVG) — Slow Path

**개념:** 3봉 구조에서 발생하는 가격 갭. 시장이 "공정가"로 되돌아오려는 성질을 이용.

```
Bullish FVG:
  봉1 High ─────┐
                 │ ← 이 갭 = Bullish FVG
  봉3 Low  ─────┘
  (봉1 고가 < 봉3 저가)

Bearish FVG:
  봉3 High ─────┐
                 │ ← 이 갭 = Bearish FVG  
  봉1 Low  ─────┘
  (봉1 저가 > 봉3 고가)
```

**적용:**
```
- 최소 갭 크기: 0.12% (15m 기준)
- 1H FVG: 유효기간 최대 48시간, 리테스트 확률 ~65%
- 15m FVG: 유효기간 최대 12시간
- FVG + OB 겹침 = "Golden Zone" → 최고 등급 진입 구간
- FVG 50% 채워지면 "부분 소진", 100% 관통하면 무효화
```

**시그널 출력:**
```python
{
    'type': 'fvg',
    'direction': 'long',
    'gap_zone': [63800, 64100],
    'gap_size_pct': 0.47,
    'filled_pct': 0.0,          # 0~1 (채워진 비율)
    'overlaps_ob': True,        # OB와 겹침 여부 ← 핵심
    'age_hours': 2.5,           # 생성 후 경과 시간
}
```

---

### 기법 3. Market Structure (BOS / CHoCH) — Fast Path

**개념:** 가격의 스윙 고점/저점 패턴으로 추세 방향과 전환점을 판단.

```
상승 추세 (Bullish):
  HH ──── Higher High
  │
  HL ──── Higher Low (각 저점이 이전보다 높음)

하락 추세 (Bearish):
  LH ──── Lower High
  │
  LL ──── Lower Low (각 고점이 이전보다 낮음)

BOS (Break of Structure): 추세 방향 유지 확인
  상승 중 HH 갱신 = Bullish BOS ✓

CHoCH (Change of Character): 추세 전환 시그널
  상승 중 HL 이탈 = Bearish CHoCH ⚠️ (추세 전환!)
```

**적용:**
```
- 스윙포인트 감도:
  · 15m: strength=5 (좌/우 5봉) → 진입 타이밍
  · 1H:  strength=5 (좌/우 5봉) → 추세 확인
- 멀티TF 구조 정렬:
  · 1H BOS Bullish + 15m BOS Bullish → 강한 롱 시그널
  · 1H Bullish + 15m CHoCH Bearish → 진입 보류
- 15m BOS 후 첫 번째 풀백이 최적 진입 타이밍
- 1H CHoCH는 기존 포지션 전량 청산 시그널
```

**시그널 출력:**
```python
{
    'type': 'market_structure',
    'trend': 'bullish',            # 'bullish' | 'bearish' | 'ranging'
    'last_event': 'BOS_bullish',   # 마지막 구조 이벤트
    'last_event_bars_ago': 3,      # 이벤트 이후 봉 수
    'swing_high': 65400,
    'swing_low': 63200,
    'htf_trend': 'bullish',        # 1H 추세
    'aligned': True,               # 15m-1H 정렬 여부
}
```

---

### 기법 4. Bollinger Bands (볼린저 밴드) — Fast Path

**개념:** 이동평균 ± 2σ 밴드. 스퀴즈 돌파 + 밴드 반전 + 밴드워크 3가지 패턴.

**3대 패턴:**

```
[패턴 A] 스퀴즈 → 돌파 (Squeeze Breakout) ★ 핵심 패턴
  밴드 폭이 최근 50봉 최소 → 에너지 축적
  → 돌파 + 거래량 스파이크 동반 시 진입
  → 1H 추세 방향과 일치하는 돌파만 유효

[패턴 B] 밴드 터치 반전 (Mean Reversion)
  가격이 하단 터치 + RSI < 30 → 롱 반전 기대
  가격이 상단 터치 + RSI > 70 → 숏 반전 기대
  ※ 1H 추세와 반대 방향이면 무시 (역추세 금지)

[패턴 C] 밴드워크 (Band Walk)
  가격이 상단 밴드를 따라 이동 = 강한 상승 모멘텀
  → 추세 추종, 중간선 이탈 시 청산
```

**파라미터:**
```
- period: 20 (표준)
- std_dev: 2.0 (표준)
- 실행 TF: 15m
- bb_position: (현재가 - 하단) / (상단 - 하단)
  · < 0.15: 과매도 영역 → 롱 기회 (추세 방향 일치 시)
  · > 0.85: 과매수 영역 → 숏 기회 (추세 방향 일치 시)
- bb_width: (상단 - 하단) / 중간선
  · < 50봉 최소값의 1.1배: 스퀴즈 상태
- squeeze_duration: 스퀴즈 지속 봉 수 (8봉 이상이면 돌파 강도 ↑)
```

**시그널 출력:**
```python
{
    'type': 'bollinger',
    'bb_position': 0.12,           # 0~1
    'bb_width': 0.023,             # 밴드폭 비율
    'pattern': 'squeeze',          # 'squeeze' | 'mean_reversion' | 'band_walk'
    'squeeze_bars': 12,            # 스퀴즈 지속 봉수
    'direction': 'long',
    'strength': 0.7,
}
```

---

### 기법 5. EMA 다중 정배열/역배열 — Fast Path

**개념:** 여러 기간 EMA의 정렬 상태로 추세 강도와 방향을 판단.

```
정배열 (Bullish Alignment):
  가격 > EMA9 > EMA21 > EMA50 > EMA200
  → 강한 상승 추세 확인

역배열 (Bearish Alignment):
  가격 < EMA9 < EMA21 < EMA50 < EMA200
  → 강한 하락 추세 확인

크로스 시그널:
  EMA9 × EMA21 골든크로스 (15m) → 추세 전환 신호
  EMA50 × EMA200 골든크로스 (1H) → 장기 추세 전환 (신뢰도 높음)
```

**적용:**
```
- EMA 기간: 9 / 21 / 50 / 200
- 실행 TF: 15m (EMA 9/21/50) + 1H (EMA200 참고)
- 정배열 점수: 
  · 4개 완전 정배열 = 1.0
  · 3개 정렬 = 0.75
  · 2개 이하 = 0.0 (추세 불명)
- EMA50(15m) 기울기: 양수면 롱 바이어스, 음수면 숏 바이어스
- 가격이 EMA50 위 → 롱 우선 / 아래 → 숏 우선
- EMA200(1H) 대비 거리 > 5%: 과도한 확장 → 평균회귀 경고
```

**시그널 출력:**
```python
{
    'type': 'ema',
    'alignment': 'bullish',      # 'bullish' | 'bearish' | 'mixed'
    'alignment_score': 1.0,      # 0~1
    'ema9': 64520,
    'ema21': 64100,
    'ema50': 63200,
    'ema200': 61800,
    'ema50_slope': 0.15,         # EMA50 기울기
    'recent_cross': 'golden_9_21',
    'cross_bars_ago': 4,
    'price_vs_ema50': 'above',
}
```

---

### 기법 6. RSI + 다이버전스 — Fast Path

**개념:** RSI 과매수/과매도 + 가격-RSI 다이버전스로 추세 전환/지속 판단.

```
[일반 다이버전스] — 추세 전환 시그널
  Bullish: 가격 LL + RSI HL → 하락 모멘텀 약화 → 반등 예상
  Bearish: 가격 HH + RSI LH → 상승 모멘텀 약화 → 하락 예상

[히든 다이버전스] — 추세 지속 시그널  
  Bullish: 가격 HL + RSI LL → 상승 추세 중 건전한 조정
  Bearish: 가격 LH + RSI HH → 하락 추세 중 약한 반등
```

**적용:**
```
- RSI 기간: 14 (기본) + 7 (단기 보조)
- 실행 TF: 15m (RSI 14 + 7) / 1H (RSI 14 확인)
- 과매수/과매도 기준:
  · RSI(14) > 70: 과매수 (숏 경고)
  · RSI(14) < 30: 과매도 (롱 기회)
  · RSI(14) > 80: 극단 과매수 → 롱 포지션 축소
  · RSI(14) < 20: 극단 과매도 → 숏 포지션 축소
- 핵심 조합:
  · bb_position < 0.15 + RSI(14) < 30 = 강한 롱 시그널
  · bb_position > 0.85 + RSI(14) > 70 = 강한 숏 시그널
- 다이버전스: 최소 5봉 간격 (15m 기준)
- RSI 50 크로스: 추세 방향 전환 보조 확인
```

**시그널 출력:**
```python
{
    'type': 'rsi',
    'rsi_14': 28.5,
    'rsi_7': 22.1,
    'zone': 'oversold',          # 'overbought' | 'neutral' | 'oversold'
    'divergence': 'bullish',     # 'bullish' | 'bearish' | 'hidden_bullish' | 'hidden_bearish' | None
    'divergence_strength': 0.8,
    'bb_rsi_combo': True,        # BB+RSI 동시 극단 여부
    'direction': 'long',
}
```

---

### 기법 7. 거래량 패턴 분석 — Slow Path

**개념:** 거래량의 급증/감소 패턴으로 돌파 진위, 추세 강도, 고점/저점 판단.

```
[패턴 A] 볼륨 스파이크 (Volume Spike) ★ 돌파 확인 핵심
  현재 거래량 / 20봉 평균 거래량 = spike_ratio
  · spike > 2.0: 주목 (15m 기준)
  · spike > 3.0: 강한 관심 → 돌파 확인
  · spike > 5.0: 극단적 이벤트 → 캐스케이드 가능

[패턴 B] 볼륨 드라이업 (Volume Dry-up)
  거래량 3봉 연속 감소 + 가격 횡보 = 에너지 축적
  → BB 스퀴즈와 동시 출현하면 돌파 임박

[패턴 C] 볼륨 다이버전스
  가격 신고가 + 거래량 감소 = 약한 돌파 → 페이크아웃 경고
  가격 신저가 + 거래량 감소 = 매도 소진 → 바닥 가능

[패턴 D] 클라이맥스 볼륨 (Climax Volume) — 청산 시그널
  극단적 거래량 + 긴 꼬리 캔들 = 추세 종료
  → 기존 포지션 부분/전량 청산
```

**Taker Buy/Sell Ratio:**
```
- taker_buy_volume / total_volume = taker_ratio
  · > 0.55: 공격적 매수 (시장가 매수 우위)
  · < 0.45: 공격적 매도 (시장가 매도 우위)
- OKX API에서 직접 제공
```

**시그널 출력:**
```python
{
    'type': 'volume',
    'spike_ratio': 4.2,
    'pattern': 'spike',           # 'spike' | 'dryup' | 'divergence' | 'climax'
    'taker_buy_ratio': 0.58,
    'trend_confirm': True,
    'direction': 'long',
    'strength': 0.75,
}
```

---

### 기법 8. Funding Rate (펀딩비) — Slow Path, 선물 전용 ★

**개념:** 무기한 선물의 가격을 현물에 수렴시키기 위한 주기적 수수료. 시장 과열/과냉 지표.

```
펀딩비 > 0 (양수): 롱이 숏에게 지불 → 롱 과열
펀딩비 < 0 (음수): 숏이 롱에게 지불 → 숏 과열

[핵심 전략: 극단 펀딩비 역발상]
  펀딩비 > +0.05%: 롱 과밀 → 롱 청산 위험 ↑
  펀딩비 > +0.1%:  극단 과열 → 역방향(숏) 기회
  펀딩비 < -0.05%: 숏 과밀 → 숏 스퀴즈 가능 → 롱 기회
  펀딩비 < -0.1%:  극단 공포 → 역방향(롱) 기회
```

**적용:**
```
- OKX 펀딩비 주기: 8시간 (00:00, 08:00, 16:00 UTC)
- 역할: 방향 바이어스 + 비용 관리
  · 펀딩비 양수 → 롱 보유 비용 증가 → 숏 선호
  · 펀딩비 음수 → 숏 보유 비용 증가 → 롱 선호
- 극단 펀딩비(|rate| > 0.05%) → 역발상 시그널 가중치 ×2
- ★ 펀딩비 정산 전후:
  → 정산 15분 전 신규 진입 금지 (변동성 위험)
  → 정산 직후 역방향 움직임 포착 기회
- 보유 중 펀딩비 누적 비용 추적 (1~4시간 보유 시 정산 걸릴 수 있음)
```

**시그널 출력:**
```python
{
    'type': 'funding_rate',
    'current_rate': 0.067,
    'avg_rate_24h': 0.045,
    'trend': 'increasing',
    'extreme': True,
    'contrarian_direction': 'short',
    'next_settlement_min': 45,    # 다음 정산까지 남은 시간(분)
    'strength': 0.8,
}
```

---

### 기법 9. Open Interest (미결제약정) — Slow Path, 선물 전용 ★

**개념:** 열려있는 선물 포지션의 총 규모. 시장 참여도와 레버리지 수준을 나타냄.

```
[OI + 가격 조합 해석]

  가격 ↑ + OI ↑ = 새로운 롱 유입 → 추세 강화 확인 ✓
  가격 ↑ + OI ↓ = 숏 청산(커버링) → 약한 상승 (곧 소진) ⚠️
  가격 ↓ + OI ↑ = 새로운 숏 유입 → 하락 추세 강화 확인 ✓
  가격 ↓ + OI ↓ = 롱 청산(손절) → 약한 하락 (바닥 근접) ⚠️

[OI 다이버전스]
  가격 신고가 + OI 감소 = 신규 매수 없음 → 가짜 돌파 경고
  가격 신저가 + OI 감소 = 투매 마무리 → 반등 가능

[OI 급증 (Spike)]
  OI 1시간 변화 > +5%: 대규모 레버리지 유입 → 변동성 폭발 예고
  → BB 스퀴즈 + OI 급증 = 최고 셋업
```

**파라미터:**
```
- OI 변화율 기준 (1시간 단위):
  · ±3%: 정상
  · ±5%: 주의 → 변동성 대비
  · ±10%: 경고 → 캐스케이드 가능, 포지션 축소
- OI 데이터 소스: OKX API + CoinGlass 보조
```

**시그널 출력:**
```python
{
    'type': 'open_interest',
    'oi_current': 12500000000,
    'oi_change_1h_pct': 3.2,
    'oi_change_24h_pct': 8.5,
    'oi_price_combo': 'new_longs',
    'divergence': False,
    'direction': 'long',
    'strength': 0.6,
}
```

---

### 기법 10. Liquidation Level (청산 구간) — Slow Path, 선물 전용 ★

**개념:** 레버리지 포지션이 강제 청산되는 가격대. 청산 캐스케이드가 가격을 급격히 밀어냄.

```
[청산 캐스케이드 메커니즘]
  가격 하락 → 롱 청산(강제 매도) → 추가 하락 → 더 많은 롱 청산 → 폭락
  가격 상승 → 숏 청산(강제 매수) → 추가 상승 → 더 많은 숏 청산 → 폭등

[Liquidation Heatmap 활용]
  청산 밀집 구간 = "자석 가격" → 가격이 끌려감
  · 위쪽 숏 청산 밀집 → 숏 스퀴즈 (가격 상승)
  · 아래쪽 롱 청산 밀집 → 롱 스퀴즈 (가격 하락)

[전략: 캐스케이드 활용]
  청산 밀집대 방향으로 모멘텀 진입 → 캐스케이드 타고 익절
  청산 밀집대 터치 후 반전 시 → 반대 방향 진입 (압력 소진 후 반등)
```

**적용:**
```
- 추정 청산가 계산: entry_price × (1 ± 1/leverage)
  · 10x 롱: 진입가 -10% 에서 청산
  · 25x 롱: 진입가 -4% 에서 청산
  · 50x 롱: 진입가 -2% 에서 청산
- 현재가 ±3~5% 이내의 청산 밀집대에 주목
- 외부 API: CoinGlass Liquidation Heatmap (보조)
```

**시그널 출력:**
```python
{
    'type': 'liquidation',
    'nearest_long_liq_zone': 62500,
    'nearest_short_liq_zone': 67000,
    'long_liq_density': 0.7,
    'short_liq_density': 0.4,
    'cascade_risk': 'moderate',
    'magnet_direction': 'up',
    'distance_to_nearest_pct': 2.5,
}
```

---

### 기법 11. Long/Short Ratio (롱숏 비율) — Slow Path, 선물 전용

**개념:** 거래소 전체 롱 vs 숏 포지션 비율. 군중 심리의 극단을 감지.

```
[역발상 원칙]
  롱/숏 비율 > 2.0: 롱 과밀 → 하락 반전 경고
  롱/숏 비율 < 0.5: 숏 과밀 → 상승 반전 기회
  롱/숏 비율 0.8~1.2: 균형 → 방향 판단 보류

[두 가지 지표]
  - 계좌 기준 (by account): 개인 트레이더 심리
  - 포지션 기준 (by position): 대형 트레이더(고래) 심리
  → 불일치 시: 고래 방향 추종
```

**적용:**
```
- 5분 단위 갱신
- 급격한 비율 변화(1시간 내 ±20%)가 시그널
- 단독 진입 근거 안 됨, 다른 기법과 합류 시에만 가중치 부여
```

**시그널 출력:**
```python
{
    'type': 'long_short_ratio',
    'ratio_account': 1.85,
    'ratio_position': 0.92,
    'divergence': True,
    'whale_direction': 'short',
    'change_1h_pct': 15.2,
    'contrarian_signal': 'short',
    'strength': 0.65,
}
```

---

### 기법 12. CVD (Cumulative Volume Delta) — Slow Path

**개념:** 시장가 매수량 - 시장가 매도량의 누적. 공격적 참여자의 실제 방향.

```
[CVD 다이버전스] — 가장 강력한 시그널

  가격 ↑ + CVD ↓ = 매수 주도 아님 → 약한 상승 (곧 꺾임)
  가격 ↓ + CVD ↑ = 매도 주도 아님 → 약한 하락 (바닥 근접)

[CVD + OB 결합] ★ 최고 셋업 중 하나
  OB 리테스트 + CVD 반전 상승 = 기관 매수 확인 → 롱
  OB 리테스트 + CVD 계속 하락 = 지지 실패 → OB 무효화
```

**적용:**
```
- CVD 데이터: OKX 체결(trades) WebSocket에서 실시간 계산
- aggressor 판단: 체결가 >= ask면 taker buy, <= bid면 taker sell
- CVD 기간: 15m / 1H 별도 트래킹
- CVD + OB/FVG 영역 겹침 → 신뢰도 ×2
```

**시그널 출력:**
```python
{
    'type': 'cvd',
    'cvd_trend': 'rising',
    'cvd_slope': 0.8,
    'price_cvd_divergence': True,
    'divergence_type': 'bullish',
    'direction': 'long',
    'strength': 0.7,
}
```

---

### 기법 13. ATR 기반 동적 SL/TP + 레버리지

> ⚠️ **사이즈/SL 산정 정책은 2026-04-08 부터 `margin_loss_cap` 모드가 기본입니다. 갱신본은 [Part 22](#part-22-보호-주문--사이즈-정책-2026-04-08-) 참조. 본문은 옛 risk_per_trade 0.5% 모드 기준.**

**개념:** 변동성에 비례하는 손절/목표가 설정 + 레버리지 자동 조절.

```
[SL 산정]
  롱 SL = 진입가 - ATR(14, 15m) × 1.2
  숏 SL = 진입가 + ATR(14, 15m) × 1.2
  · 최소 SL: 0.3%
  · 최대 SL: 1.5%

[TP 산정]
  TP1 = 1.5R (50% 물량 청산)
  TP2 = 2.5R (30% 물량 청산)
  TP3 = 트레일링 (20% 물량)

[포지션 사이즈]
  사이즈 = (계좌 × 리스크비율) / (SL거리% × 레버리지)
  · 리스크비율 = 0.5% (고정 — 레버리지 무관)

[동적 레버리지 — ATR 연동] ★
  leverage = min(등급별_최대배율, 0.5% / ATR_pct)
  
  변동성 낮을 때 (ATR < 0.2%): 고배율 가능 (25~30x)
  변동성 보통   (ATR 0.2~0.4%): 중배율 (15~20x)
  변동성 높을 때 (ATR > 0.4%): 저배율 강제 (10~15x)

  → SL은 항상 ATR × 1.2 이상 유지 (노이즈 위)
  → 레버리지가 자동으로 조절되어 1회 손실은 항상 0.5%
```

**등급별 레버리지 상한:**
```
┌────────┬────────┬──────────────────────────────┐
│ 등급    │ 점수   │ 최대 레버리지 (ATR 제한 적용)  │
├────────┼────────┼──────────────────────────────┤
│ A+     │ 9.0+   │ 30x                          │
│ A      │ 8.0+   │ 25x                          │
│ B+     │ 7.5+   │ 20x                          │
│ B      │ 6.5+   │ 15x                          │
│ B-     │ 6.0+   │ 10x                          │
└────────┴────────┴──────────────────────────────┘

실제 배율 = min(등급 상한, ATR 제한, 연패 제한)
```

**연패 시 레버리지 감소:**
```
  연속 2연패 → 최대배율 × 0.7
  연속 3연패 → 최대배율 × 0.5
  연속 5연패 → 매매 중단 (2시간 쿨다운)
```

**BTC 15m ATR 참고치:**
```
  15m ATR: ~$150~400 (시장 상황에 따라)
  1H ATR:  ~$300~800
  4H ATR:  ~$600~1,500
```

---

### 기법 14. VWAP (거래량가중평균가) — Fast Path

**개념:** 거래량을 고려한 평균 가격. 기관이 참고하는 "공정 가격" 기준선.

```
[VWAP 활용]
  가격 > VWAP: 매수 세력 우위 → 롱 유리
  가격 < VWAP: 매도 세력 우위 → 숏 유리
  VWAP 터치 후 반등/거부: 진입 타이밍

[적용]
  - 세션 VWAP: 24시간 단위 리셋 (00:00 UTC 기준)
  - 주간 VWAP: 월요일 00:00 UTC 리셋
  - VWAP을 방향 필터로 사용 (EMA50 보조)
  
[VWAP + OB 결합]
  OB 영역 + VWAP 겹침 = 강력한 지지/저항
  → 이 구간에서의 진입은 매우 높은 승률
```

**시그널 출력:**
```python
{
    'type': 'vwap',
    'session_vwap': 64350,
    'weekly_vwap': 63800,
    'price_vs_vwap': 'above',
    'dist_pct': 0.45,
    'touch_recent': True,         # 최근 3봉 내 VWAP 터치 여부
    'direction': 'long',
    'strength': 0.5,
}
```

---

## Part 3. 시그널 합산 & 매매 결정

### 시그널 가중치 테이블

```
┌─────────────────────────┬────────┬──────────┬────────────────────────┐
│ 기법                     │ 가중치  │ 경로     │ 역할                   │
├─────────────────────────┼────────┼──────────┼────────────────────────┤
│ ★ Order Block           │  3.0   │ Slow     │ 핵심 진입 구간          │
│ ★ Market Structure      │  2.5   │ Fast     │ 추세 방향 필터          │
│ ★ Bollinger Bands       │  2.0   │ Fast     │ 스퀴즈 돌파 핵심        │
│ ★ Funding Rate          │  2.0   │ Slow     │ 선물 과열/과냉 감지      │
│ ★ Open Interest         │  2.0   │ Slow     │ 레버리지/참여도 판단     │
│   RSI + 다이버전스        │  1.5   │ Fast     │ 과매수/과매도 필터       │
│   거래량 패턴             │  1.5   │ Slow     │ 돌파 확인 (스파이크)     │
│   FVG                   │  1.5   │ Slow     │ OB 보조 (겹침 시 보너스) │
│   CVD                   │  1.5   │ Slow     │ 공격적 참여 방향 확인    │
│   Liquidation Level     │  1.5   │ Slow     │ 자석 가격/캐스케이드     │
│   EMA 정배열             │  1.0   │ Fast     │ 추세 확인 + 방향 필터    │
│   Long/Short Ratio      │  1.0   │ Slow     │ 역발상 보조             │
│   VWAP                  │  1.0   │ Fast     │ 공정가 기준 + 방향 필터  │
│   ATR                   │  ─     │ Fast     │ SL/TP + 레버리지 산정   │
│   ML 예측               │  2.5   │ Slow     │ 종합 확률 판단          │
├─────────────────────────┼────────┼──────────┼────────────────────────┤
│ 합계 가능 최대           │ ~25.5  │          │                        │
└─────────────────────────┴────────┴──────────┴────────────────────────┘
```

### 컨플루언스 보너스 점수

```
[기법 겹침 시 추가 점수]

  OB + FVG 겹침 ("Golden Zone")     → +2.0
  OB + VWAP 겹침                    → +1.5
  BB 스퀴즈 + 거래량 스파이크        → +1.5
  RSI 극단 + BB 극단 동시           → +1.0
  OI 급증 + BB 스퀴즈               → +1.0
```

### 진입 등급 기준

```
정규화 점수 = (가중합 + 보너스) / 가능 최대점수 × 10

Grade A+ (9.0+): 최고 확신 — 풀 사이즈 (100%) — 시장가 — 최대 30x
  필수조건: 콤보셋업 + 다수 보조 시그널 합류

Grade A (8.0+):  높은 확신 — 풀 사이즈 (100%) — 시장가 — 최대 25x
  필수조건: OB 또는 BB돌파 + 구조 일치 + 최소 3개 보조 시그널

Grade B+ (7.5+): 중상 확신 — 75% 사이즈 — 지정가 — 최대 20x

Grade B (6.5+):  보통 확신 — 50% 사이즈 — 지정가 — 최대 15x
  필수조건: OB 또는 구조 + 최소 2개 보조 시그널

Grade B- (6.0+): 최소 확신 — 30% 사이즈 — 지정가 — 최대 10x

Grade C/D (<6.0): 진입 금지
```

### 매매 결정 플로우

```
1. [필수 필터] — 하나라도 실패하면 매매 불가
   □ 일일 손실 한도 미초과
   □ 최대 드로다운 미초과  
   □ 최대 동시 포지션 미초과 (3개)
   □ 연패 쿨다운 미적용 중
   □ 펀딩비 정산 15분 전이 아닐 것
   □ 같은 심볼 중복 포지션 없음
   □ 기대수익 > 0.15% (수수료 필터)

2. [방향 결정]
   1H Market Structure 추세 방향 = 기본 방향
   15m + 1H 구조 일치 시만 진입
   EMA50(15m) 위 → 롱만 / 아래 → 숏만
   VWAP 위 → 롱 가산 / 아래 → 숏 가산

3. [진입 구간 확인]
   활성 Order Block 내 가격 위치
   FVG 겹침 여부 → Golden Zone 보너스
   BB 스퀴즈 + 거래량 스파이크 → 돌파 진입

4. [확인 시그널 수집]
   Fast Path: 실시간 계산 (EMA, RSI, BB, VWAP, 구조)
   Slow Path: 주기적 갱신 (OB, FVG, OI, FR, CVD 등)
   가중 합산 + 보너스 → 정규화 점수 → 등급

5. [레버리지 결정]
   등급별 최대 배율 → ATR 제한 → 연패 제한 → 최종 배율
   포지션 크기 = (계좌 × 0.5%) / (SL% × 레버리지)

6. [실행]
   Grade A+/A → 시장가 즉시 진입
   Grade B+/B/B- → 지정가 (미체결 2분 → 시장가 전환 or 취소)
   ATR 기반 SL/TP 즉시 설정
```

---

## Part 4. 트레일링 & 청산 전략

### 트레일링 스톱

```
Tier 0: 진입 직후
  SL = ATR(15m) × 1.2 (초기 손절)

Tier 1: 수익 +0.8% 도달
  SL → 진입가 + 수수료 (본전 확보)
  
Tier 2: 수익 +1.5% 도달 (= TP1)
  50% 물량 청산 (확정 수익)
  나머지 SL → +0.5%

Tier 3: 수익 +2.5% 도달 (= TP2)
  30% 물량 추가 청산 (잔여 20%)
  SL → +1.5%

Tier 4: 수익 +3.5% 이상
  ATR 기반 동적 트레일링
  SL = 현재가 - ATR(15m) × 0.8 (따라감)
```

### 시간 기반 청산 ★

```
[시간 경과 = 시나리오 약화]

  진입 후 1시간:  수익 < +0.3% → 50% 청산 (모멘텀 약함)
  진입 후 2시간:  TP1 미달 → 75% 청산
  진입 후 4시간:  미청산 물량 전량 청산
  진입 후 6시간:  무조건 전량 청산 (최대 보유 시간)
  
  ※ 수익 중이라도 6시간 초과 시 청산
  ※ 펀딩비 정산 걸리면 비용 추적 후 판단
```

### 청산 시그널

```
[즉시 전량 청산]
- SL 도달
- 1H CHoCH 발생 (큰 구조 전환)
- 반대 방향 Grade A 시그널
- 시간 청산 트리거 (6시간)

[부분 청산]
- TP1/TP2 도달
- RSI(14) 극단 (>80 or <20)
- 거래량 클라이맥스 감지
- 15m CHoCH 발생 (소구조 전환)
- 시간 청산 트리거 (1시간/2시간)

[신규 진입 금지 (쿨다운)]
- 연속 3연패 → 30분 쿨다운 + 배율 50%로 제한
- 연속 5연패 → 2시간 쿨다운
- 일일 손실 -5% → 당일 매매 중단
```

---

## Part 5. 리스크 관리 요약

```yaml
# 포지션 수준
max_risk_per_trade: 0.5%         # 1회 매매 최대 손실 (레버리지 무관 고정)
leverage_range: 10x~30x          # 등급 + ATR + 연패 연동
sl_min_pct: 0.3%                 # 최소 SL 거리
sl_max_pct: 1.5%                 # 최대 SL 거리
sl_atr_multiplier: 1.2           # SL = ATR × 1.2 (노이즈 위)
min_expected_profit: 0.15%       # 최소 기대수익 (수수료 필터)

# 계좌 수준
max_positions: 3                 # 최대 동시 포지션
max_same_direction: 2            # 같은 방향 최대
max_daily_loss: 5%               # 일일 최대 손실 → 당일 매매 중단
max_drawdown: 15%                # 최대 드로다운 → 전체 중단
cooldown_3_loss: 30min           # 연속 3연패 → 30분 쿨다운
cooldown_5_loss: 2h              # 연속 5연패 → 2시간 쿨다운
max_hold_time: 6h                # 최대 보유 시간

# 체결 방식
grade_a_execution: "market"      # Grade A+/A → 시장가
grade_b_execution: "limit"       # Grade B+/B/B- → 지정가
limit_timeout_sec: 120           # 지정가 미체결 2분 → 전환/취소

# 수수료
taker_fee: 0.05%                 # OKX Taker
maker_fee: 0.02%                 # OKX Maker
round_trip_cost: 0.10%           # 왕복 수수료 (Taker 기준)

# 선물 전용
funding_cost_monitor: true
funding_blackout_min: 15         # 정산 전 진입 금지
auto_reduce_high_funding: true   # 펀딩비 >0.1% 시 포지션 축소
margin_alert_threshold: 50%

# 동적 레버리지 공식
leverage_formula: "min(grade_max, 0.5% / ATR_pct, streak_limit)"
```

---

## Part 6. 콤보 셋업 (Quick Reference)

> 가장 승률 높은 진입 패턴 정리

```
[셋업 A] OB 리테스트 + CVD 반전 ★★★
  조건: 15m/1H OB 터치 + CVD 기울기 반전 + 1H 추세 일치
  진입: OB 50% (OTE) 지정가 or 시장가
  SL: OB 반대편
  TP: 1.5R → 2.5R
  예상 보유: 1~3시간

[셋업 B] BB 스퀴즈 돌파 + 거래량 스파이크 ★★★
  조건: 15m BB 스퀴즈 8봉+ + 돌파 + spike_ratio > 2.0
  진입: 돌파 방향 시장가
  SL: BB 중간선
  TP: 밴드 반대편 or 2.0R
  예상 보유: 30분~2시간

[셋업 C] Golden Zone 반등 ★★★
  조건: OB + FVG 겹침 구간 터치 + RSI 극단 + 구조 일치
  진입: Golden Zone 내 시장가
  SL: Golden Zone 이탈
  TP: 1.5R → 2.5R
  예상 보유: 1~4시간

[셋업 D] EMA 크로스 + RSI 확인 ★★
  조건: EMA9/21 크로스(15m) + RSI 방향 일치 + 거래량 증가
  진입: 크로스 확인 후 지정가
  SL: EMA50
  TP: 1.5R
  예상 보유: 1~2시간

[셋업 E] 캐스케이드 서핑 ★★
  조건: OI 급증 + 청산 밀집대 방향 모멘텀
  진입: 캐스케이드 시작 확인 후 시장가
  SL: ATR × 1.2
  TP: 청산 밀집대 도달 → 즉시 익절
  예상 보유: 30분~1시간
```

---

## Part 7. 개발 로드맵

```
Phase 1 (1~2주): 데이터 + 기법 엔진
  └─ OKX ccxt 연동, 캔들 수집, WebSocket 실시간 데이터
  └─ 14개 기법 구현 (Fast Path / Slow Path 분리)

Phase 2 (1~2주): ML + 시그널 합산
  └─ RF Walk-Forward, 피처 통합
  └─ 컨플루언스 스코어링 + 보너스 점수

Phase 3 (1주): 매매 엔진 + 리스크
  └─ 주문 실행 (시장가/지정가 하이브리드)
  └─ 동적 레버리지, 시간 청산, 트레일링, 수수료 필터

Phase 4 (1주): 백테스트
  └─ 과거 데이터 시뮬레이션
  └─ 콤보 셋업별 승률/수익률 검증
  └─ 레버리지 구간별 성과 비교

Phase 5 (1주): 모니터링 + 실전
  └─ 대시보드, 텔레그램
  └─ 테스트넷 → 실전(소액)
```

---

## Part 8. 프로젝트 구조

```
crypto-analyzer/
├── config/
│   ├── settings.yaml          # 전략 파라미터 (SL배수, 레버리지 범위, 등급 기준 등)
│   └── .env                   # API 키, 텔레그램 토큰 (git 제외)
│
├── src/
│   ├── __init__.py
│   ├── main.py                # 엔트리포인트, 스케줄러 초기화
│   │
│   ├── data/                  # 데이터 수집 모듈
│   │   ├── __init__.py
│   │   ├── candle_collector.py    # REST 캔들 수집 + 백필
│   │   ├── ws_stream.py           # WebSocket 실시간 (틱, 오더북, 체결)
│   │   ├── oi_funding.py          # OI, 펀딩비, 롱숏비율 수집
│   │   └── storage.py             # SQLite + Redis 읽기/쓰기 래퍼
│   │
│   ├── engine/                # 기법 엔진
│   │   ├── __init__.py
│   │   ├── base.py                # BaseIndicator 추상 클래스
│   │   ├── fast/                  # Fast Path 기법들
│   │   │   ├── ema.py
│   │   │   ├── rsi.py
│   │   │   ├── bollinger.py
│   │   │   ├── vwap.py
│   │   │   └── market_structure.py
│   │   └── slow/                  # Slow Path 기법들
│   │       ├── order_block.py
│   │       ├── fvg.py
│   │       ├── volume_pattern.py
│   │       ├── funding_rate.py
│   │       ├── open_interest.py
│   │       ├── liquidation.py
│   │       ├── long_short_ratio.py
│   │       └── cvd.py
│   │
│   ├── signal/                # 시그널 합산 + ML
│   │   ├── __init__.py
│   │   ├── aggregator.py          # 가중 합산 + 컨플루언스 보너스
│   │   ├── grader.py              # 등급 판정 (A+/A/B+/B/B-/C/D)
│   │   └── ml_model.py           # RF Walk-Forward 학습/예측
│   │
│   ├── trading/               # 매매 엔진
│   │   ├── __init__.py
│   │   ├── executor.py            # 주문 실행 (시장가/지정가)
│   │   ├── position_manager.py    # 포지션 추적, 트레일링, 시간청산
│   │   ├── leverage.py            # 동적 레버리지 계산
│   │   └── risk_manager.py        # 리스크 필터 (일일한도, 연패, 드로다운)
│   │
│   ├── monitoring/            # 모니터링
│   │   ├── __init__.py
│   │   ├── dashboard.py           # FastAPI 대시보드
│   │   ├── telegram_bot.py        # 텔레그램 알림
│   │   └── trade_logger.py        # 트레이드 로그 기록
│   │
│   └── utils/                 # 유틸리티
│       ├── __init__.py
│       └── helpers.py             # 공통 함수 (ATR 계산, 시간 변환 등)
│
├── backtest/
│   ├── __init__.py
│   ├── simulator.py               # 백테스트 시뮬레이터
│   └── report.py                  # 성과 리포트 생성
│
├── data/
│   └── candles.db                 # SQLite DB 파일
│
├── tests/                         # 테스트
│   ├── test_engine/
│   ├── test_signal/
│   └── test_trading/
│
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

### 설정 파일 구조

```yaml
# config/settings.yaml
exchange:
  name: "okx"
  symbol: "BTC/USDT:USDT"
  margin_mode: "isolated"

timeframes:
  execution: "15m"
  confirmation: "1h"
  filter: "4h"

risk:
  risk_per_trade: 0.005        # 0.5%
  leverage_range: [10, 30]
  sl_atr_multiplier: 1.2
  sl_min_pct: 0.003
  sl_max_pct: 0.015
  min_expected_profit: 0.0015  # 0.15%
  max_positions: 3
  max_same_direction: 2
  max_daily_loss: 0.05         # 5%
  max_drawdown: 0.15           # 15%
  max_hold_hours: 6

trailing:
  breakeven_trigger: 0.008     # +0.8%
  tp1_trigger: 0.015           # +1.5%
  tp1_close_pct: 0.5           # 50%
  tp2_trigger: 0.025           # +2.5%
  tp2_close_pct: 0.3           # 30%
  tp3_atr_multiplier: 0.8

cooldown:
  streak_3_min: 30
  streak_5_min: 120

fees:
  taker: 0.0005
  maker: 0.0002

telegram:
  enabled: true

redis:
  host: "localhost"
  port: 6379
  db: 0
```

```
# .env.example
OKX_API_KEY=
OKX_SECRET_KEY=
OKX_PASSPHRASE=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

---

## Part 9. DB 스키마 + Redis 키 구조

### SQLite 테이블

```sql
-- 캔들 데이터
CREATE TABLE candles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,           -- '15m', '1h', '4h'
    timestamp INTEGER NOT NULL,        -- Unix ms
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    UNIQUE(symbol, timeframe, timestamp)
);
CREATE INDEX idx_candles_lookup ON candles(symbol, timeframe, timestamp DESC);

-- 트레이드 로그
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,           -- 'long' | 'short'
    grade TEXT NOT NULL,               -- 'A+', 'A', 'B+', 'B', 'B-'
    score REAL NOT NULL,               -- 정규화 점수
    entry_price REAL NOT NULL,
    entry_time INTEGER NOT NULL,       -- Unix ms
    exit_price REAL,
    exit_time INTEGER,
    exit_reason TEXT,                  -- 'tp1','tp2','trailing','sl','time','signal','manual'
    leverage INTEGER NOT NULL,
    position_size REAL NOT NULL,       -- USDT
    pnl_usdt REAL,                    -- 실현 손익
    pnl_pct REAL,                     -- 수익률 %
    fee_total REAL,                   -- 수수료 합계
    funding_cost REAL DEFAULT 0,      -- 펀딩비 비용
    signals_snapshot TEXT,            -- 진입 시 시그널 JSON
    notes TEXT
);

-- OI / 펀딩비 히스토리
CREATE TABLE oi_funding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    open_interest REAL,
    funding_rate REAL,
    long_short_ratio_account REAL,
    long_short_ratio_position REAL,
    UNIQUE(symbol, timestamp)
);
CREATE INDEX idx_oi_funding_lookup ON oi_funding(symbol, timestamp DESC);

-- 일일 성과 요약
CREATE TABLE daily_summary (
    date TEXT PRIMARY KEY,             -- 'YYYY-MM-DD'
    total_trades INTEGER DEFAULT 0,
    win_count INTEGER DEFAULT 0,
    loss_count INTEGER DEFAULT 0,
    total_pnl_usdt REAL DEFAULT 0,
    total_pnl_pct REAL DEFAULT 0,
    max_drawdown_pct REAL DEFAULT 0,
    total_fees REAL DEFAULT 0,
    total_funding REAL DEFAULT 0
);
```

### Redis 키 구조

```
# 실시간 데이터 (TTL: 자동 갱신)
rt:price:{symbol}                  → 현재가 (string)
rt:orderbook:{symbol}              → 오더북 top 10 (hash)
rt:ticker:{symbol}                 → 24h 티커 (hash)

# 시그널 캐시 (TTL: 각 경로 갱신주기 × 2)
sig:fast:{symbol}                  → Fast Path 시그널 묶음 (JSON string)
sig:slow:{symbol}                  → Slow Path 시그널 묶음 (JSON string)
sig:aggregated:{symbol}            → 합산 결과 + 등급 (JSON string)

# 포지션 상태 (TTL 없음, 명시적 삭제)
pos:active:{symbol}                → 활성 포지션 정보 (hash)
pos:trailing:{symbol}              → 트레일링 상태 (hash)

# 리스크 카운터 (TTL: 자정 리셋)
risk:daily_pnl                     → 당일 누적 손익 (string)
risk:streak                        → 현재 연패 수 (string)
risk:cooldown_until                → 쿨다운 종료 시각 (string, Unix)
risk:trade_count_today              → 당일 매매 횟수 (string)

# CVD 실시간 누적 (TTL: 자동 갱신)
cvd:15m:{symbol}                   → 15m CVD 누적값 (string)
cvd:1h:{symbol}                    → 1H CVD 누적값 (string)

# 시스템 상태
sys:bot_status                     → 'running' | 'paused' | 'stopped'
sys:last_heartbeat                 → 마지막 헬스체크 시각
```

---

## Part 10. 모듈 간 인터페이스

### 데이터 흐름 (비동기 구조)

```
┌─────────────────────────────────────────────────────────────┐
│                    asyncio Event Loop                        │
│                                                             │
│  ┌─────────────┐    Redis Pub/Sub     ┌─────────────────┐  │
│  │ ws_stream   │ ──────────────────→  │ Fast Path 엔진   │  │
│  │ (WebSocket) │   'ch:tick:{sym}'    │ (15m봉 완성 시)  │  │
│  └─────────────┘                      └────────┬────────┘  │
│                                                │            │
│  ┌─────────────┐    APScheduler       ┌────────▼────────┐  │
│  │ candle_     │ ──(1~5분 주기)────→  │ Slow Path 엔진   │  │
│  │ collector   │                      └────────┬────────┘  │
│  └─────────────┘                               │            │
│                                       ┌────────▼────────┐  │
│                                       │ Aggregator      │  │
│                                       │ (합산 + 등급)    │  │
│                                       └────────┬────────┘  │
│                                                │            │
│                                       ┌────────▼────────┐  │
│                                       │ Trading Engine  │  │
│                                       │ (실행 + 관리)    │  │
│                                       └─────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### 핵심 인터페이스 정의

```python
# 1. BaseIndicator → 모든 기법 엔진의 부모
class BaseIndicator(ABC):
    @abstractmethod
    async def calculate(self, candles: pd.DataFrame, 
                        context: dict) -> dict:
        """시그널 dict 반환 (type, direction, strength 필수)"""
        pass
    
    @property
    @abstractmethod
    def path(self) -> str:
        """'fast' | 'slow'"""
        pass

# 2. Aggregator 입력/출력
AggregatorInput = {
    'fast_signals': [dict, ...],      # Fast Path 시그널 리스트
    'slow_signals': [dict, ...],      # Slow Path 시그널 리스트
    'ml_prediction': dict | None,     # ML 예측 결과
}

AggregatorOutput = {
    'score': float,                   # 정규화 점수 (0~10)
    'grade': str,                     # 'A+', 'A', 'B+', 'B', 'B-', 'C', 'D'
    'direction': str,                 # 'long' | 'short' | 'neutral'
    'confluence_bonus': float,        # 컨플루언스 보너스 점수
    'signals_detail': dict,           # 기법별 상세 시그널
    'timestamp': int,
}

# 3. Trading Engine 입력
TradeRequest = {
    'symbol': str,
    'direction': str,
    'grade': str,
    'score': float,
    'leverage': int,
    'position_size_usdt': float,
    'entry_type': str,                # 'market' | 'limit'
    'entry_price': float | None,      # limit일 때만
    'sl_price': float,
    'tp_prices': [float, float],      # TP1, TP2
    'signals_snapshot': dict,
}
```

### 이벤트 트리거 정리

```
[15m 봉 완성]
  → Fast Path 전체 재계산
  → Aggregator 실행
  → 등급 B- 이상이면 Trading Engine 호출

[1~5분 주기 (APScheduler)]
  → Slow Path 재계산
  → Redis sig:slow 갱신

[WebSocket 틱 수신]
  → Redis rt:price 갱신
  → 활성 포지션 SL/TP 체크 (position_manager)
  → 트레일링 업데이트

[1분 주기]
  → 시간 청산 체크
  → 쿨다운 상태 체크
  → 헬스체크 (sys:last_heartbeat)
```

---

## Part 11. ML 엔진 상세

> ⚠️ **이 섹션은 v1 (RF + Walk-Forward) 기준 원본 설계입니다. 현재 운영은 AdaptiveML v2 — 갱신본은 [Part 17](#part-17-adaptiveml-v2-2026-04-07-) 참조.**

### 피처 목록

```python
FEATURES = [
    # Fast Path (실시간)
    'ema_alignment_score',       # EMA 정배열 점수 (0~1)
    'ema50_slope',               # EMA50 기울기
    'rsi_14',                    # RSI(14) 값
    'rsi_7',                     # RSI(7) 값
    'rsi_divergence',            # 다이버전스 유무 (0/1)
    'bb_position',               # BB 내 위치 (0~1)
    'bb_width',                  # BB 폭
    'bb_squeeze_bars',           # 스퀴즈 지속 봉수
    'vwap_distance_pct',         # VWAP 대비 거리 %
    'market_structure_trend',    # 추세 (1=bull, -1=bear, 0=range)
    'ms_aligned',                # 15m-1H 정렬 (0/1)
    'bos_bars_ago',              # 마지막 BOS 이후 봉수
    
    # Slow Path (주기적)
    'ob_distance_pct',           # OB까지 거리 %
    'ob_strength',               # OB 강도 (0~1)
    'ob_htf_aligned',            # 상위TF OB 겹침 (0/1)
    'fvg_distance_pct',          # FVG까지 거리 %
    'fvg_overlaps_ob',           # OB-FVG 겹침 (0/1)
    'volume_spike_ratio',        # 거래량 스파이크 비율
    'taker_buy_ratio',           # Taker Buy 비율
    'funding_rate',              # 현재 펀딩비
    'funding_extreme',           # 극단 펀딩비 (0/1)
    'oi_change_1h_pct',          # OI 1시간 변화율 %
    'oi_price_combo',            # OI+가격 조합 (인코딩)
    'ls_ratio_account',          # 롱숏비율 (계좌)
    'ls_ratio_position',         # 롱숏비율 (포지션)
    'cvd_slope',                 # CVD 기울기
    'cvd_divergence',            # CVD 다이버전스 (0/1)
    'liq_nearest_distance_pct',  # 가장 가까운 청산구간 거리 %
    'liq_magnet_direction',      # 청산 자석 방향 (1/-1)
    
    # 파생 피처
    'atr_14_pct',                # ATR(14) 변동성 %
    'hour_of_day',               # 시간대 (0~23)
    'day_of_week',               # 요일 (0~6)
    'streak_count',              # 현재 연승/연패 수
]
# 총 피처 수: ~32개
```

### 라벨 정의

```
라벨 = 진입 후 결과 (수수료 차감 후 기준)

  1 (Win):  TP1(+1.5R) 이상 도달 후 청산
  0 (Loss): SL 도달 또는 시간 청산 시 손실

  ※ 시간 청산 시 본전 부근(±0.1%)이면 라벨에서 제외 (노이즈 방지)
  ※ 수수료(왕복 0.1%) + 슬리피지(0.05%) 차감 후 판정
```

### Walk-Forward 설정

```
학습 윈도우:  30일 (최소, 15m봉 기준 ~2,880개)
검증 윈도우:  7일
재학습 주기:  7일마다 (매주 일요일 00:00 UTC)
최소 샘플 수: 200거래 이상이어야 ML 활성화
모델:         RandomForestClassifier
              - n_estimators: 200
              - max_depth: 8
              - min_samples_leaf: 10
              - class_weight: 'balanced'

※ ML 데이터 부족 시: ML 가중치 0으로 처리 (나머지 기법만으로 운영)
※ ML 가중치 2.5 근거: OB(3.0), 구조(2.5)와 동급 → 종합 판단 역할
   단, 초기에는 0으로 시작하여 충분한 데이터 축적 후 활성화
```

---

## Part 12. 주문 실행 엣지 케이스

### 슬리피지 관리

```
시장가 주문:
  - 최대 허용 슬리피지: 0.1% (초과 시 주문 취소 후 재시도)
  - 백테스트 반영: 0.05% 고정 슬리피지 가정
  - OKX 시장가는 최적 호가 기준 → 유동성 충분한 BTC에서는 실질 슬리피지 미미

지정가 주문:
  - 타임아웃: 120초 (2분)
  - 부분 체결 처리:
    · 70% 이상 체결 → 나머지 취소, 체결분으로 진행
    · 70% 미만 체결 → 전량 취소 (시장가 전환 안 함, 기회 포기)
  - 가격 미끄러짐 시 1회 재제출 허용 (가격 0.05% 조정)
```

### SL/TP 실행 방식

```
SL: OKX 서버 사이드 (algo order - trigger)
  → 봇 다운되어도 거래소에서 실행
  → 설정: triggerPx = SL가격, ordPx = -1 (시장가)

TP1/TP2: 봇에서 관리 (클라이언트 사이드)
  → 15초 간격 가격 체크 → 도달 시 시장가 청산
  → 봇 다운 시 SL만 남음 (TP 미실행은 수익 기회 상실이지 손실 아님)

트레일링: 봇에서 관리
  → 틱 수신마다 트레일링 SL 갱신
  → 갱신된 SL은 OKX 서버사이드로 재설정
```

### 동시 청산 우선순위

```
여러 청산 조건이 동시에 트리거될 경우:

1순위: SL 도달          → 즉시 전량 청산 (서버사이드, 가장 빠름)
2순위: 반대 Grade A 시그널 → 전량 청산 후 반대 포지션 진입
3순위: 1H CHoCH         → 전량 청산
4순위: 시간 청산         → 비율에 따라 부분/전량 청산
5순위: TP/트레일링       → 정상 수익 실현

※ 실제로는 SL이 서버사이드라 봇보다 먼저 실행됨
※ 봇에서는 청산 전 항상 포지션 조회 후 잔여 물량만 청산
```

### API 장애 대응

```
OKX API 무응답/에러 시:
  - 주문 실행: 3회 재시도 (간격 2초) → 실패 시 해당 시그널 포기
  - 포지션 조회: 5회 재시도 → 실패 시 텔레그램 긴급 알림
  - SL은 서버사이드이므로 API 장애와 무관하게 동작
  - 신규 진입은 중단, 기존 포지션은 SL에 위임
```

---

## Part 13. 수수료/비용 상세

### OKX 수수료 체계

```
기본 (Tier 1):
  Maker: 0.02%   Taker: 0.05%

30일 거래량 증가 시 자동 할인:
  Tier 2 (>10M USDT):  Maker 0.018% / Taker 0.045%
  Tier 3 (>20M USDT):  Maker 0.015% / Taker 0.040%

→ settings.yaml에서 현재 Tier에 맞게 수수료율 설정
→ 등급 변경 시 수동 업데이트 (자동 감지 불필요)
```

### 비용 계산 로직

```python
def calculate_trade_cost(entry_price, exit_price, size, leverage, 
                         entry_type, exit_type, funding_cost=0):
    """
    entry_type/exit_type: 'maker' | 'taker'
    """
    entry_fee = size * fee_rate[entry_type]
    exit_fee = size * fee_rate[exit_type]
    total_cost = entry_fee + exit_fee + funding_cost
    
    # 수수료 필터: 기대수익이 총 비용의 1.5배 이상이어야 진입
    # 즉, 최소 기대수익 = 왕복수수료 × 1.5 ≈ 0.15%
    return total_cost
```

### 펀딩비 비용 추적

```
- 보유 중 펀딩비 정산 시각 통과 시 비용/수익 기록
- 펀딩비 = 포지션 가치 × 펀딩비율
  · 롱 보유 + 양수 펀딩비 → 비용 (지불)
  · 롱 보유 + 음수 펀딩비 → 수익 (수취)
- 정산 시각: 00:00, 08:00, 16:00 UTC
- trades 테이블의 funding_cost 컬럼에 누적 기록
```

### 백테스트 비용 반영

```
왕복 수수료: Taker 기준 0.10% (보수적)
슬리피지:    0.05% (편도)
펀딩비:      실제 과거 펀딩비 데이터 사용 (OKX API 제공)
```

---

## Part 14. 로깅 & 모니터링

### 트레이드 로그

```
모든 매매는 trades 테이블에 자동 기록 + 텍스트 로그 파일 병행

로그 레벨:
  INFO:  진입/청산/등급 판정
  DEBUG: 시그널 상세, 파라미터 값
  WARN:  슬리피지 초과, API 재시도, 쿨다운 진입
  ERROR: API 장애, 주문 실패, 예외
```

### 텔레그램 알림

```
[즉시 발송]
  🟢 진입: 방향, 등급, 레버리지, 진입가, SL/TP
  🔴 청산: 청산사유, 수익률, 실현손익
  ⚠️ 경고: 연패 쿨다운, 일일 한도 근접, API 에러
  🛑 긴급: 봇 정지, 최대 드로다운 도달, 네트워크 장애

[일일 리포트 (매일 00:00 UTC)]
  📊 당일 요약: 매매 횟수, 승률, 총 손익, 잔고

[선택 알림 (설정 가능)]
  📈 Grade A+ 시그널 감지 (진입 전)
  💰 TP1/TP2 도달
```

### FastAPI 대시보드 엔드포인트

```
GET  /api/status              → 봇 상태 (running/paused/stopped)
GET  /api/position            → 현재 활성 포지션
GET  /api/signals             → 최신 시그널 합산 결과
GET  /api/trades?days=7       → 최근 매매 내역
GET  /api/daily-summary       → 일일 성과 요약
GET  /api/equity-curve        → 자산 곡선 데이터
POST /api/pause               → 봇 일시정지
POST /api/resume              → 봇 재개
POST /api/close-all           → 전 포지션 청산 (킬 스위치)
```

---

## Part 15. 안전장치

> 매매 흐름을 방해하지 않는 선에서 핵심 보호만 적용

### 킬 스위치

```
POST /api/close-all 또는 텔레그램 /kill 명령
  → 전 포지션 시장가 즉시 청산
  → 미체결 주문 전량 취소
  → 봇 상태 'stopped'으로 변경
  → 수동 /resume 전까지 재시작 불가
  
※ 자동 트리거: 최대 드로다운(15%) 도달 시에만
※ 나머지는 수동 판단에 맡김
```

### 네트워크/봇 장애

```
WebSocket 끊김:
  → 5초 후 자동 재연결 (최대 3회)
  → 3회 실패 → REST 폴링 모드 전환 (30초 간격)
  → REST도 실패 → 신규 진입 중단, 기존 포지션은 서버사이드 SL에 위임
  → 텔레그램 알림 발송

봇 재시작 시:
  → OKX에서 현재 포지션 조회 → Redis/메모리 동기화
  → 서버사이드 SL 확인 → 없으면 재설정
  → 미체결 주문 확인 → 2분 초과된 것은 취소
  → 정상 루프 재개
```

### 마진 비율 경고

```
마진 비율 > 50%:
  → 텔레그램 경고 알림 (1회)
  → 신규 진입 사이즈 50%로 제한

마진 비율 > 70%:
  → 신규 진입 금지
  → 텔레그램 긴급 알림

※ 강제 청산은 하지 않음 → 기존 포지션 SL/TP에 위임
※ 과도한 자동 청산은 오히려 불리한 가격에 빠지므로 지양
```

### 데이터 무결성

```
캔들 수집 누락 감지:
  → 이전 봉 timestamp + interval ≠ 현재 봉 timestamp → 갭 발생
  → REST API로 누락 구간 백필 (최대 500봉)
  → 백필 실패 시 해당 구간 시그널은 무효 처리

Redis 장애:
  → 메모리 내 dict로 폴백 (재시작 시 유실 가능)
  → 시그널 캐시 용도이므로 치명적이지 않음
  → 포지션 정보는 항상 OKX API에서 재조회 가능
```

---

## Part 16. 개발/배포 환경

### 필요 소프트웨어

```
1. Docker Desktop     → 컨테이너 실행 환경 (Redis, 봇, 대시보드)
2. Git                → 코드 버전 관리 + 두 PC 동기화
3. Python 3.11        → 개발 시 로컬 실행/디버깅용
4. VS Code (권장)     → 코드 편집기
5. Redis / SQLite     → Docker가 처리 (직접 설치 불필요)
```

### Docker Desktop 설치 (Windows)

```
Step 1. 다운로드
  → https://www.docker.com/products/docker-desktop/
  → "Download for Windows" 클릭

Step 2. 설치
  → Docker Desktop Installer.exe 실행
  → "Use WSL 2 instead of Hyper-V" 체크 (권장)
  → Install 클릭 → 완료 후 재부팅

Step 3. WSL 2 설치 (Docker가 요구함)
  → 재부팅 후 Docker에서 WSL 2 업데이트 요청 시:
  → PowerShell(관리자) 열고:
    wsl --install
  → 다시 재부팅

Step 4. 확인
  → Docker Desktop 실행 (시작 메뉴에서 검색)
  → 트레이에 고래 🐳 아이콘 → "Docker is running" 확인
  → 터미널에서 확인:
    docker --version         # Docker version 2x.x.x
    docker-compose --version # Docker Compose version v2.x.x

Step 5. 테스트
  → 터미널에서:
    docker run hello-world
  → "Hello from Docker!" 메시지 나오면 성공
```

### Docker 주의사항 (회사 PC)

```
- 회사 네트워크에서 Docker Hub 접근이 막혀있을 수 있음
  → IT팀에 확인 또는 VPN 사용
- Docker Desktop은 무료 (개인/소규모 기업)
  → 직원 250명 이상 기업은 유료 ($5/월)
  → 해당 시 Rancher Desktop (무료 대안) 사용 가능
- Hyper-V / WSL 2 활성화 필요
  → BIOS에서 가상화(VT-x) 활성화 되어있어야 함
  → 안 되면: 설정 → Windows 기능 → "Linux용 Windows 하위 시스템" 체크
```

### 프로젝트 Docker 설정

```dockerfile
# Dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "src/main.py"]
```

```yaml
# docker-compose.yml
services:
  redis:
    image: redis:7-alpine
    restart: always
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data

  bot:
    build: .
    restart: always
    depends_on:
      - redis
    env_file:
      - .env
    volumes:
      - ./data:/app/data
      - ./config:/app/config

  dashboard:
    build: .
    command: uvicorn src.monitoring.dashboard:app --host 0.0.0.0 --port 8000
    restart: always
    depends_on:
      - redis
      - bot
    ports:
      - "8000:8000"
    env_file:
      - .env

volumes:
  redis_data:
```

### 두 PC 작업 흐름

```
회사 PC                              노트북
────────                            ────────
코드 작성/테스트                      
git add . && git commit && git push  
                                    git pull
                                    이어서 작업
                                    git add . && git commit && git push
git pull
이어서 작업

※ 양쪽 다 Docker Desktop 설치되어 있으면
  docker-compose up 으로 동일한 환경 실행 가능
```

### 단계별 환경 사용

```
[Phase 1~3: 개발]
  → 노트북/회사 PC에서 Python 직접 실행
  → Redis만 Docker로: docker run -d -p 6379:6379 redis:7-alpine
  → 코드 수정 → 바로 실행 → 빠른 반복

[Phase 4: 백테스트]
  → 로컬 Python 직접 실행 (CPU 많이 쓰므로)

[Phase 5: 테스트넷]
  → docker-compose up 으로 전체 통합 테스트
  → 문제 없으면 그대로 VPS 배포

[실전 배포: VPS]
  → Oracle Cloud 무료 VPS (Ubuntu)
  → Docker 설치 → git clone → docker-compose up -d
  → 24/7 안정 운영
```

---

# Part 17~22 — 2026-04-07 이후 추가 (갱신본)

> 본문 Part 1~16 은 2026-04-03 시점 원본 설계서이며, 그 이후의 큰 변경은 이하 섹션에 가산 형태로 정리합니다.  
> 작은 변경 (버그 픽스, UI 조정 등) 은 [`CHANGELOG.md`](CHANGELOG.md) 와 [`COMMIT_LOG.md`](COMMIT_LOG.md) 에 있습니다.

---

## Part 17. AdaptiveML v2 (2026-04-07~)

### 변화 요약
- 단일 RandomForest → **레짐별 멀티모델 + 앙상블**
- 글로벌 GBM (전체 데이터) + 레짐별 GBM/RF/LR (각 레짐 데이터) — 최대 14개 모델
- Walk-forward 검증 (Train acc / OOS acc 분리 추적)
- 60+ 강화 피처 (세션, 요일, 시그널 변화율, 레짐 원핫, 크로스 피처)
- v1 → v2 자동 마이그레이션
- 시그널 이름 마이그레이션 자동 처리 (예: `scalp_ob` → `order_block`)

### 모델 구조
```
글로벌 모델: GBM (전체 8000+ 데이터, OOS ~0.85)
   ↓ baseline 예측
레짐별 모델 (각 레짐 데이터 충분 시):
   - trending_up:    GBM, RF, LR  → soft voting
   - trending_down:  GBM, RF, LR  → soft voting
   - ranging:        GBM, RF, LR  → soft voting
   - volatile:       GBM, RF      → soft voting
   ↓
최종 예측 = 현재 레짐 앙상블 (없으면 글로벌 fallback)
```

### 주요 파라미터
- 글로벌 버퍼: 50,000 건 (~1주일치)
- 레짐별 버퍼: 15,000 건 each
- 동적 재학습 주기: 50/100/200 (MetaLearner 자동 결정)
- 학습 트리거: HistoricalLearner / 학습 스케줄러 / 메타 학습

### MetaLearner (매주 일요일)
- 하이퍼파라미터 Grid Search (5종)
- 피처 중요도 분석 → 약한 시그널 가중치 자동 감소
- 모델 종류 자동 선택 (GBM vs RF vs LR)
- 자가 진단 + 자동 복구

### 저장
- `data/adaptive_v2_swing.pkl`, `data/adaptive_v2_scalp.pkl`
- `tempfile + shutil.move` 원자적 저장 (pkl 손상 방지)
- `asyncio.to_thread()` 로 래핑 (sklearn pickle dump가 이벤트 루프 막던 문제 해결, `883abff`)

---

## Part 18. MarketRegime — 4레짐 자동 판별 (2026-04-07~)

### 4레짐 정의
```
trending_up   : ADX > 25, +DI > -DI, EMA 상승 정배열
trending_down : ADX > 25, -DI > +DI, EMA 하락 역배열
ranging       : ADX < 20, BB Width 좁음, 가격 BB 중앙 근처
volatile      : ATR% > 평균×1.5, BB Width 급팽창
```

### 판별 알고리즘
- 입력: 1H 캔들의 ADX(14), +DI/-DI, BB Width %, ATR %, EMA20/50/200 배열
- 출력: 레짐 라벨 + 신뢰 점수
- **안정화**: 2회 연속 같은 레짐이어야 전환 (jitter 방지)
- 매 1H 봉 마감 시 재계산

### 레짐별 전략
| 레짐 | 레버리지 배율 | 사이즈 배율 | 비고 |
|---|---|---|---|
| trending_up | 1.0× | 1.0× | 풀 사이즈, 추세 추종 |
| trending_down | 1.0× | 1.0× | 동일 |
| ranging | 0.7× | 0.7× | 박스 안에서 보수적 |
| volatile | 0.5× | 0.5× | 변동성 ↑ → 사이즈 ↓ |

### 활용
- ML 앙상블의 레짐별 모델 선택 키
- 사이즈 계산기에 배율 적용
- Aggregator 가 레짐 가중치로 시그널 보정

---

## Part 19. ScalpEngine v3/v4 — 스캘핑 전용 엔진 (2026-04-07~)

### 위치
`src/scalp_engine.py` — Swing 14기법(Part 2)과 별개의 독립 엔진

### 시그널 18종

**기본 5**
- `ema_cross` — EMA9/21 골든/데드크로스
- `rsi_reversal` — RSI 30/70 반전
- `bb_break` — 볼린저 밴드 돌파
- `volume_spike` — 거래량 스파이크 (5m 기준)
- `momentum` — 단기 모멘텀

**급변동 4**
- `volatility_explosion` — ATR 폭발 + 5봉 누적 방향
- `range_breakout` — 박스 돌파
- `candle_pattern` — 핀바/장대봉 (핀바 우선)
- `rapid_momentum` — 급속 모멘텀

**SMC 3**
- `order_block` — 1m/5m 오더블록 (옛 이름 `scalp_ob`)
- `liquidity_sweep` — 유동성 스윕 (ATR 정규화)
- `fvg` — 1m FVG (옛 이름 `scalp_fvg`)

**강화 3**
- `vwap_intraday` — VWAP 일중 레벨
- `pivot_points` — 100봉 피봇
- `bos` — Break of Structure (스윙 양옆 1봉)

**필터 2**
- `session_filter` — 세션별 가중치
- `anti_chop` — ADX<18 + 방향전환≥6 동시 만족 시 진입 거부

**관리 1**
- `trailing_stop` — 트레일링 SL

### 3가지 진입 모드
| 모드 | SL | TP | 비고 |
|---|---|---|---|
| **SMC** | 0.5× ATR | 2.5R | 안티첩 무시, 사이즈 120% |
| **급변동** | 0.6× ATR | 1.5R + 트레일링 | 안티첩 무시, 사이즈 100% |
| **일반** | 0.8× ATR | 2.0R | 안티첩 적용, 사이즈 80% |

> 위 SL/TP 는 v3 ATR 기준이며, 2026-04-08 부터 `margin_loss_cap` 모드 적용 시 마진 % 기준으로 변환됨 — Part 22 참조.

### 세션별 배율
| 세션 | 배율 |
|---|---|
| US/EU 오버랩 | 1.2× |
| US 단독 | 1.1× |
| EU 단독 | 1.0× |
| 아시아 | 0.8× |
| 주말 | 0.6× |

### v4 주요 픽스 (efcaf54)
- OB 인덱싱 음수→양수 (정확한 탐지)
- FVG 방향 확인 (추세 일치 시만)
- ADX 계산 Wilder's smoothing 정통 방식
- BOS 스윙 조건 완화 (양옆 2봉→1봉)
- 필터 다중 폭락 해결 (곱셈→가산 페널티, 최대 50%만 감점)

---

## Part 20. 학습 자동화 (2026-04-07~)

### 구성요소

**HistoricalLearner** (`src/strategy/historical_learner.py`)
- 90일 캔들 자동 수집 → 시그널 재현 → 대량 학습
- SL 4종 다양화 (0.8 / 1.0 / 1.2 / 1.5x ATR) 로 라벨 다양성 확보
- 레짐별 집중 학습, 급변동 구간 집중 학습

**PaperTrader** (`src/strategy/paper_trader.py`)
- 점수 2.0+ 모든 시그널 가상 진입 (전수 학습)
- 미진입 시그널 30분 추적 → "놓친 기회" 학습 (Shadow 모드)
- 누적 거래 통계 + 승률 추적
- **학습 중에는 try_entry/check_positions 모두 스킵** (CPU 경합 방지, `4b64866`)

**AutoBacktest** (`src/strategy/auto_backtest.py`)
- 매일 자동 백테스트 (최근 30일)

**SignalTracker** (`src/strategy/signal_tracker.py`)
- 각 거래의 활성 시그널(강도≥0.3) 추출 → 강도 비례 P&L 분배
- 시그널별 누적: 거래수, 승률, 평균 P&L, 기여도 점수
- 자동 식별: 약한 시그널(평균 -0.1% 이하) / 강한 시그널(+0.2% 이상)
- 기여도 공식: `avg_pnl × min(1, sqrt(trades)/10) × (0.5 + wr×0.5)`
- 10건마다 자동 저장

### 학습 스케줄 (cb1758a — 조용한 시간대로 변경)
| UTC | KST | 종류 | 비고 |
|---|---|---|---|
| 22:00 | 07:00 (다음날) | 일일 대량 학습 + 백테스트 | 일요일은 메타학습 추가, 사용자 기상 시간 |
| 04:00 | 13:00 | 세션 경량 학습 | Asia 점심, EU 미오픈 |
| 11:00 | 20:00 | 세션 경량 학습 | EU 피크 직후, NY 개장 직전 |

### 학습-매매 격리 (`883abff`, `cb1758a`)
- 스케줄러가 `_guarded_study()` 헬퍼로 `redis sys:learning=1` set/unset
- `_execute_swing/_execute_scalp` 가 시작 시 `sys:learning` 체크 → 진입 스킵
- 활성 포지션이 있으면 `periodic_position_check` 가 5초 폴링으로 가속 (TP1 본절 이동 지연 30~45초 → 5초)
- `ml.save()` 를 `asyncio.to_thread()` 로 래핑 → 이벤트 루프 블로킹 해소

---

## Part 21. 클라우드 배포 + 운영 자동화 (2026-04-07~)

### 인프라
- **Vultr Singapore** $6/월 + Backup $1.2/월
- Ubuntu 22.04 LTS, 1 vCPU / 1GB RAM / 25GB SSD
- IP `207.148.120.103`, 경로 `/root/crypto-bot`
- Docker Compose: `bot` (Python + 대시보드) + `redis` (실시간 캐시)
- 방화벽 22, 8000 만 + fail2ban
- OKX API: Withdraw OFF + IP 화이트리스트 (서버 IP)
- 대시보드: HTTP Basic Auth (`DASHBOARD_USER`/`DASHBOARD_PASS`)
- 로그 회전: bot 50MB×5, redis 10MB×3
- sklearn 1.8.0 버전 고정

### 운영 자동화 (`097143f`, `883abff`)
- **`scripts/health_check.sh`** — 봇 상태 한 줄 (text/json), OK/DOWN/STALE 판정
- **`scripts/log_digest.sh N`** — 최근 N분 핵심 이벤트 요약 (헬스체크 자동 포함)
- **`scripts/log_push.sh N`** — `logs` 브랜치에 디지스트 자동 push (`*/15 * * * *` cron 권장)
- **`scripts/update_commit_log.sh`** — `git log` → `COMMIT_LOG.md` 재생성
- **`.githooks/post-commit`** — 매 커밋 후 자동 갱신 (PC 당 1회 `git config core.hooksPath .githooks`)

### 변경 인덱스 계층
```
[git commit]                ← 단일 진실 (SSOT)
     ↓ post-commit hook
[COMMIT_LOG.md]             ← 자동 미러 (lag-by-1)
     ↓ 큰 변경 시 사람 큐레이션
[CHANGELOG.md]              ← 카테고리별 큐레이션
[MANUAL.md]                 ← 운영 명령
[명세서.md]                 ← 설계 의도/아키텍처
[메모리]                    ← Claude 행동 규칙 + 비코드 컨텍스트
```

---

## Part 22. 보호 주문 + 사이즈 정책 (2026-04-08~)

### 배경
- 2026-04-08 실거래 -90% 강제청산 사고
- 근본 원인: `leverage_calc.calculate_position_size` 마진 공식이 leverage 미반영 → 0.5% risk 가 실제 12.5% (25x)
- 이후 보호 주문 파이프라인 전면 재구성

### 보호 주문 파이프라인 (`4832098`, `95192d3`)
```
[진입]
  ↓
[OKX set_protection]
  - SL  (ATR 또는 마진 한도, set_indicator vs margin_loss_cap)
  - TP1 (50% 청산, 1.5R 또는 마진 +15%)
  - TP2 (30% 청산, 2.5R) — 옵션 A 에서는 미등록
  - TP3 (20% 청산, 4R) — 옵션 A 에서는 미등록
  ↓ 등록 실패 시 진입 즉시 되돌림 (보호 없는 포지션 금지)
[옵션 A: 러너 트레일링]
  - TP1 hit → 50% 익절 + SL 본전+0.1% 이동 (반익본절)
  - 잔여 50% → 트레일링 SL (best_price ± trail_distance)
  - trail_distance = max(tp1_dist × 0.5, 가격 0.3%)
  - 8시간 hard limit
```

### algoClOrdId 형식
- OKX 는 영숫자만 허용 — `_` 등 특수기호 금지
- 봇 내부 형식: `slXXXXXXXXXX` (timestamp 기반)
- 옛 `sl_XXX` 형식이 등록 실패의 원인이었음

### Self-Heal (`baae27b`)
- 매 폴링마다 SL/TP 알고가 None 이면 자동 재등록
- 네트워크 일시 끊김 후 자동 복구
- 사용자 수동 SL/TP override 는 존중 (덮어쓰지 않음)

### sync_positions
- 봇 재시작 후 거래소에 포지션 발견 시:
  1. Redis 에서 옛 Position 상태 복원 시도
  2. 없으면 긴급 SL 등록 + 텔레그램 알림
- 무방비 포지션 방지

### Race Condition Lock
- `PositionManager._symbol_locks` — 동일 symbol 의 check_positions / signal_exit / close_all 동시 처리 방지
- `_process_position` 메서드 분리 (lock 안에서 호출)

### 사이즈/SL 모드 — `margin_loss_cap` (기본, `361604f` `fbb6985`)

#### config
```yaml
sizing_mode: margin_loss_cap
margin_pct: 0.95              # 잔고 대비 마진 비율
max_margin_loss_pct: 10.0     # SL 마진 손실 한도 %
use_indicator_sl: true        # 매물대 SL 우선 사용
tp1_margin_gain_pct: 15.0     # TP1 마진 +15% 익절
trail_margin_pct: 5.0         # 러너 트레일 마진 5% 거리
trail_min_price_pct: 0.2      # 트레일 최소 가격 거리 (노이즈 방어)
min_indicator_sl_price_pct: 0.05  # 매물대 SL 최소 가격 거리 (즉시청산 방지)
```

#### 작동
1. 마진 = `잔고 × margin_pct`
2. 사이즈 = `floor(마진 × leverage / 가격 / MIN_LOT) × MIN_LOT`
3. SL 가격 거리 = `max_margin_loss_pct / leverage` (예: 25x + 10% → 0.4%)
4. 매물대 기반 SL 이 위 거리보다:
   - 가까우면 → 매물대 SL 사용 (보수적)
   - 멀면 → 마진 한도 SL 사용 (손실 보호)
   - 단, `min_indicator_sl_price_pct` 미만이면 즉시청산 방지로 마진 한도 사용
5. TP/트레일 거리도 모두 `% / leverage` 로 환산

#### 옛 모드 (`risk_per_trade`) 보존
- 큰 계좌 (수백~수천 USDT) 권장
- `margin = risk / (leverage × sl_pct/100)` (`baae27b` 에서 leverage 반영 수정)

### 사용자 수동 SL/TP (`fbb6985`)
- `PositionManager.manual_update_sl(symbol, price)` / `manual_update_tp(...)`
- API: `POST /api/position/sl|tp`
- sanity check: 방향 일치, 마진 손실 50% 이하
- `manual_sl_override` / `manual_tp_override` 플래그 — 트레일/self-heal 이 존중
- TP1 hit 후 본절 이동은 봇이 자동 (override 리셋)

### 소형 포지션 (1 contract) 처리 (`4b64866`)
- `filled_size < MIN_ORDER_SIZE × 2` → TP1 100% 청산, 러너 모드 비활성
- 잔고 ~$28 케이스 (1 contract 만 가능) 직접 해결

### 좀비 방지 (`baae27b`, `6ca4b4e`)
- fill 확인 실패 + 강제 청산 실패 → 자동매매 정지 + 텔레그램 emergency
- `_full_close` 가 close 실패 시 SL 긴급 재등록 + 메모리 유지 + 다음 폴링 재시도
- 3회 재시도 + 실패 시 `sys:autotrading=off`

---
