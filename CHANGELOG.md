# 변경 이력 (CHANGELOG)

대화 기반으로 수정한 모든 사항을 기록합니다.
전체 커밋 raw 인덱스는 [`COMMIT_LOG.md`](COMMIT_LOG.md) 참조 (자동 생성).

---

## 2026-05-07

### Binance CVD + 1분 고속 감지 + 마이크로 비활성

**CVD/Whale Binance 이관** — OKX → Binance Futures WS (fstream.binancefuture.com)
- 거래량 3~5배 → CVD 신뢰도 대폭 상승. OKX는 캔들/가격/호가만 담당.

**1분 고속 모멘텀 감지** — detect_fast(df_1m)
- ATR_1m×1.5 엄격 기준, hold_mode=quick, 5분 감지와 병행
- TP = ATR_1m × 배수 (자동으로 짧은 목표). 지연 5분→1분 단축.

**마이크로스트럭처 비활성** — 15개 지표 2초 계산 중단 (Phase B+까지)

### JSONL 로그 정리 + 텔레그램 개편

**JSONL**: candidate에 h1/h4, entry에 conviction, gate_block 신설, hourly에 lab/adaptive
**텔레그램**: /adaptive, /lab, /shadow 신설. /status ML+Lab. /risk DD%+daily.

### AdaptiveParams 수치 자동 보정 엔진

SPEC §10 신설 + 구현. 거래 결과로부터 TP/SL/방향/사이즈 수치를 이동 통계로 자동 보정.

**모듈 구조** (`src/strategy/adaptive_params.py`):
- EntryQualityScorer: time_to_first_profit → 진입 품질 경고
- DirectionScorer: (1h×4h×direction) EV → 사이즈 배수 or 차단
- TPCalibrator: reach% → ATR 배수 보정 (레짐별)
- SLCalibrator: winning MAE → SL 거리 보정
- HoldOptimizer: 보유시간 vs 승률 → 트레일 강도
- RegimeScorer: 레짐별 EV → 사이즈 배수
- TimeOfDayTracker: 시간대별 EV

**Phase**: 0~30건 수집만 → 30건+ Direction/EntryQuality → 100건+ TP/SL → 300건+ 전체

**Position 추가 필드**: worst_price(MAE), first_profit_ts, entry_atr, entry_h1/h4_trend, params_snapshot

**통합**: _on_trade_closed → adaptive.record_trade() + Redis persist

### 상위 TF 추세 게이트 + Shadow/Paper 개선

- 1h/4h EMA20 역행 차단 (역행 3건 전패 -$60 방지)
- Shadow: ATR barrier + 전 시그널 추적 (entry_executed 필터 제거)
- Paper: 리스크 게이트 제거 + 포지션 무제한 (벤치마크 순수화)

---

## 2026-05-06

### 매매 복기 기반 3중 게이트 + TP1 ATR 전환

5/4~5/5 실거래 7건 복기 (-$25.11): 레짐 역행 진입, TP1 과대, 늦은 진입이 주 손실 원인.

**1. 레짐-방향 게이트** (실거래+페이퍼 차단, shadow 허용)
- `main.py` — `_is_regime_aligned()`: trending_up+short(momentum/cascade) 차단, trending_down+long 차단
- ranging+breakout 차단 (가짜 돌파 방지)
- breakout 역방향은 허용 (전환 시그널)

**2. TP1 ATR 기반 전환** (고정 RR 2.0 → 가변 RR 1.3~2.4)
- `main.py`, `paper_trader.py` — `tp1_dist = price × clamp(ATR×1.5, 0.25%, 0.80%)`
- RR 최소 1.3 보장 (`tp1_dist >= sl_dist × 1.3`)
- 횡보장(ATR 0.3%): 0.67%→0.45%로 축소 → TP1 도달률 대폭 상승

**3. 모멘텀 소진 체크**
- `main.py`, `paper_trader.py` — 최근 3캔들 이동 > TP1의 50% → 진입 차단
- `candidate_detector.py` — `recent_move_pct` 필드 추가

**4. 코드 순서 수정**: `min_sl` 보정을 TP1 RR 계산 전으로 이동

**5. MD 전수조사 갱신**: SPEC_V2(§5.1/5.3/7.1/7.2/8.1), MANUAL(margin_loss_cap), CLAUDE.md

### 전수검사 — SPEC vs 코드 4건 CRITICAL 수정

**C1. Market 허용 threshold** (main.py:510)
- `strength >= 1.0` → `>= 1.5` (SPEC §6.1 준수, maker 수수료 절감)

**C2. Trail ATR 배수** (position_manager.py:188)
- `atr * 2` → `atr * trail_atr_mult(1.5)` (SPEC §7.2 준수, 수익 반납 축소)

**C3. Adverse Selection vol_surge 미구현** (position_manager.py:569)
- vol_ratio_1m ≥ 1.5 체크 추가 (SPEC §7.3 AND 조건 3개 완성)

**C4. ML min_samples default** (ml_engine.py:74)
- default 200 → 100 (settings.yaml과 일치)

**SPEC 갱신**: §6.1(SL market-on-trigger, retry 3회), §6.2(margin Phase A/B), §7.2(trail 상세), §8.1(daily loss Phase A/B)

---

## 2026-04-28

### Maker 강제 + Ranging 차단 — 수수료/횡보장 손실 근절

거래 분석 결과: 페이퍼 수수료 $502 > PnL, ranging 6전6패(-$440). 두 근본 원인 동시 제거.

**1. 전 주문 Maker(post-only) 강제** (수수료 60% 절감)
- `executor.py` — 진입: post-only 5회 추격, market 폴백 **완전 제거**. 미체결 시 진입 포기
- `executor.py` — `_limit_order`도 postOnly=True 강제
- `executor.py` — 청산: SL/긴급만 market 허용, TP/시간청산은 post-only only
- `executor.py` — SL 알고 주문도 limit-on-trigger (sl_failsafe가 백업)
- `paper_trader.py` — 수수료 계산 전부 maker 기준 통일
- `main.py` — 수수료 필터 taker→maker 기준
- `backtest/simulator.py` — 백테스트 비용도 maker 기준

**2. RANGING 레짐 진입 차단**
- `main.py` — 레짐 게이트 추가: ranging이면 모든 셋업 진입 차단
- `paper_trader.py` — 동일 게이트 추가

**효과 (페이퍼 11건 기준):**
- 수수료 절감: $305 → $233 (-24%)
- ranging 6건 제거: -$440 손실 제거
- 남은 5건: +$159 순수익
- 총 개선: +$512

---

## 2026-04-27

### 전수검사 — CRITICAL 3 + HIGH 9 + MEDIUM 5건 수정

42개 파일 전체 함수 호출 체인 감사 후 발견된 버그 일괄 수정.

**CRITICAL (3건)**
- `main.py:370` — `price_now` 미정의 NameError → 가격 fetch 후 올바른 `price` 변수 사용으로 이동 **(봇 정지 근본 원인)**
- `position_manager.py:1349` — TP1 부분청산 이익이 최종 PnL에 누락 → `realized_pnl_usdt` 필드 추가, 합산 반영
- `historical_learner.py` 4곳 — `_simulate_trade()`와 `record_trade()` 양쪽에서 수수료 이중 차감 → fee_pct=0 전달

**HIGH (9건)**
- `main.py:290` — 레짐 감지 최소 캔들 20→50 (detect() 요구사항 일치)
- `position_manager.py:1414` — ML callback fee_pct가 remaining_margin 기준 → total_margin 기준으로
- `position_manager.py:688` — TP1 소형 판정 pos.size → pos.remaining_size
- `meta_learner.py:141` — 하이퍼파라미터 튜닝 후 `is_trained=True` 누락 추가
- `meta_learner.py:105` — `len(set(y_test)) < 1` → `< 2` (단일 클래스 가드)
- `binance_stream.py:183` — CVD 윈도우 리셋 시 첫 거래 유실 → 리셋 전 delta 분리
- `ws_stream.py:187` — 동일 CVD 리셋 버그 수정
- `candle_collector.py` — TF_MS에 "1w" 604,800,000 추가
- `fractal.py:104,116` — closes[i-1] IndexError → 범위 제한 `-len(closes)+1`

**나머지 HIGH 3건 추가 수정**
- `main.py` + `risk_manager.py` — streak/daily_pnl 이중 추적 제거 → RiskManager 단일 소스 (getter 4개 추가, main.py 로컬 변수 삭제)
- `flow_engine.py` — SignalTracker 호환: signals_snapshot에 `sig_*` 정규화 키 5개 추가 (기존 키 보존)
- `meta_learner.py` — _select_best_model dead code 삭제 (~37줄), 미사용 import 정리

**MEDIUM (5건)**
- `fvg.py:27` — 마지막 캔들 FVG off-by-one → `len(candles)` 포함
- `volume_pattern.py` — 캔들 4개 미만 가드 추가
- `index.html` — 수동매매 응답 fill_price/size_btc 미존재 → data.msg 사용

---

## 2026-04-17 (후반)

### FlowEngine v1 — 오더플로우 기반 매매 엔진 전환

4번째이자 가장 단순한 전략. 14지표 → ScalpEngine → Setup ABC → **FlowEngine**.

**설계 원칙**: 3가지 조건 전부 YES면 진입, 하나라도 NO면 패스.
1. 큰 추세 (1d/4h EMA20/50) — 방향 결정
2. 주요 레벨 (1d/4h/1h 스윙 고저점, 멀티TF 병합) — 진입 자리
3. CVD 합산 (OKX+Binance, threshold 0.5 BTC) — 오더플로우 확인

**FlowML**: 경량 GBM (16피처), 50건마다 자동 재학습, 콜드 스타트 시 보정 0
**HTF 편향**: 차단 아님 가감만 (하락장 롱 가능 — 감점으로 확신 있을 때만)
**추세 돌파**: 1d/4h 20봉 고저점 돌파 감지 → 최고 우선 시그널

### Binance 선물 데이터 전면 전환

- WS 10스트림: aggTrade, miniTicker, kline(1m~1w), forceOrder
- 캔들: Binance WS → DB 직접 저장 (0ms), REST는 30초 백업
- CVD 합산: OKX + Binance = 시장 75% 커버
- 대형 체결 $50k+: 5분 윈도우 방향 편향
- 청산 폭발: $500k+/분 → 변동성 시그널
- 이벤트 드리븐: kline 확정 → pub/sub → 즉시 평가 (~0ms)

### 레거시 코드 정리

- main.py: 14개 지표 import/인스턴스 전부 주석처리
- 활성 import 23개 → 19개
- PaperTrader, HistoricalLearner, AutoBacktest, MetaLearner = None
- AdaptiveML swing/scalp = 주석
- SetupTracker: "A/B/C" → "FLOW"
- 봇 메시지: "FlowEngine v1"

### 점수/등급 정합성 수정

- 최소 점수 5.5 체크 추가 (htf_bias 적용 후)
- 등급 동적 매핑 (9+ A+, 8+ A, 7+ B+, 5.5+ B)
- "B+" 하드코딩 제거

---

## 2026-04-16~17

### 대시보드 전면 개편

- 크래시 루프 근절: `/health` 엔드포인트(인증 불필요), `_ensure_initialized` 락+타임아웃, shutdown None 가드
- 별도 Docker 컨테이너 안정화: Redis 명령 큐(`cmd:bot`)로 bot↔dashboard 통신
- **Engine 탭 신설** (구 "AI/Models" 대체): TradeEngine 동적 상태바, Setup Performance, Setup×Regime 히트맵, Real vs Paper 비교
- 레거시 ML 엔드포인트 10개 삭제 (`/api/ml/*`, `/api/meta*`, `/api/backtest*`), 라우트 41→30개
- `/api/engine/state`, `/api/engine/overview` 신설
- `SetupTracker.get_summary()` 데드락 수정 (`Lock` → `RLock`)

### 미체결 알고 주문 잔존 근본 수정

4번 수정에도 재발하던 **TP 잔존 버그** 6개 근본 경로 차단:
- 러너 모드 활성 시 TP2/TP3 즉시 정리 (`_cancel_unused_tps`)
- 부분 청산 후 SL/TP1 사이즈 재등록 (`_resize_protection`)
- `cancel_all_algos` 쿼리 실패 시 3회 재시도 (silent skip 제거)
- `_full_close` 예외 발생 시에도 알고 정리 보장
- `_finalize_position` 후 잔존 알고 검증 + 재정리
- `periodic_orphan_algo_sweeper` 백스톱 (120초 주기)

### 수수료 60% 절감 — post-only limit 전환

- 진입/청산: post-only limit 우선 (maker 0.02%), 실패 시 market 폴백
- TP 알고: `orderPx=triggerPx` (limit-on-trigger)
- SL 알고: market 유지 (안전 우선)
- 긴급 청산 (sl_failsafe, kill_switch): market 유지

### 4/16 거래 복기 → 실전 개선 3건

7건 2W 3L, WR 29%, sl_failsafe 57%, 0분 즉사 2건 분석:
- **SL 거리 검증**: 체결가-SL < 0.15% 이면 진입 즉시 취소 (0분 즉사 방지)
- **SL 등록 OKX 검증**: set_protection 후 pending 조회 → 미발견 시 재등록 3회
- **같은 방향 연속 4회 차단**: LONG 편향 86% 방지

### 방향성 근본 개선 — 오더플로우 통합

WR 33% (랜덤 이하) 근본 원인: EMA(후행)만으로 추세 판단.
- `_market_context`에 CVD/펀딩율/L·S비율 실시간 오더플로우 통합
- `flow_bias` 스코어 (-1~+1): CVD(±0.4) + 펀딩(±0.2) + L/S(±0.15)
- **EMA↑ but flow_bias < -0.2 → trend=neutral 격하** → 역추세 진입 차단

### 봇 스냅샷 (Claude 실시간 분석용)

- 매 60초 `data/logs/bot_snapshot.json` 생성 (포지션/레짐/잔고/미체결알고)
- `log_push.sh`가 logs 브랜치에 포함 → `git fetch`로 현재 상태 즉시 확인

---

## 2026-04-15

### 전면 개편: Unified Engine v1 (Scalp/Swing 통합)

**Dual Model → Unified Engine 전환:**
- ScalpEngine(22시그널) + SwingEngine(14시그널) → UnifiedEngine(셋업 ABC)
- 점수 합산 방식 → 셋업 조건 전부 충족 시에만 진입
- ML 콜드스타트: 기존 64k 학습 데이터 폐기 (21% 정확도 오염), raw 시그널로 매매
- 페이퍼 트레이딩으로 검증 후 실거래 전환

**셋업 A (추세 모멘텀):** 15m 추세 + 5m BOS + 1m 모멘텀 + 거래량 1.3x+
**셋업 B (OB 리테스트):** MSB OB + 프레시 + 1m ChoCH + 유동성 클리어
**셋업 C (브레이크아웃):** 5m 레인지 돌파 + 거래량 2x+ + 리테스트 홀드

**리스크 개편:**
- margin_pct 0.90 → 0.30 (생존 우선)
- max_positions 3 → 1 (동시 1포지션)
- 연패 사이즈 축소: 3연패 70%, 5연패 50%, 10연패 25%
- 조건부 쿨다운: 손절 후 3분, 익절 후 1분, 방향전환 5분
- hold_modes: quick(셋업A)/standard(셋업B,C)/swing

---

### 시그널 구조 전면 개편: 추세추종 중심 전환

매매 308건 분석: 방향 정확도 21% (랜덤 50%보다 나쁨), 시그널 뒤집으면 79% 승률.
평균회귀 시그널이 추세추종보다 1.4배 가중치 높아 항상 역추세 진입하던 구조적 문제.

**추세추종 시그널 대폭 강화:**
- EMA크로스 2.5→5.0, 모멘텀 1.5→4.0, BOS 3.5→5.0, 레인지브레이크 3.5→5.0
- rapid_momentum 2.5→4.0, 실시간스파이크 5.0 유지

**평균회귀 시그널 비활성화:**
- RSI2 Extreme 4.0→0, VWAP MR 3.5→0, Cascade Fade 2.5→0 (73% 역방향 주범)
- RSI반전 2.0→1.0(횡보 시만), BB돌파 3.0→1.0(횡보 시만)
- VWAP levels 평균회귀 로직 제거, 돌파만 유지

**오더블록 멀티TF 개편 (참고.txt 기반):**
- 5m 단일TF → 15m/5m 멀티TF OB 중첩 감지
- MSB(구조 돌파) 확인 필수
- 프레시(한 번도 터치 안 된) OB 우선 (+0.3 보너스)
- FVG 동반 시 +0.2 보너스
- 멀티TF 중첩 시 +0.3 보너스, 가중치 6.0x (최고)
- 추세 방향 일치 필수 (역추세 OB 차단)

---

### 구조적 손실 근절 — SL/TP/레버리지 전면 개편

매매 내역 30건 정밀 분석: 이상치 제외 28건 Net -153.82 USDT, RR 0.46, 승률 37%.
SL 너무 좁음(0.12%) + 레버리지 과다(25~40x) + TP < SL(RR<1)이 근본 원인.

**SL 거리 확대:**
- `max_margin_loss_pct` 12% → 8% (15x에서 SL 0.53% = ~$400)
- `min_indicator_sl_price_pct` 0.40% → 0.50% (최소 SL $375+)
- scalp_engine `min_sl_dist` 0.15% → 0.35%, `sl_atr_mult` 1.5 → 2.0

**TP/RR 개선 (RR 0.46 → 1.5+):**
- `tp1_margin_gain_pct` 20% → 15% (SL 8% 대비 RR 1.87)
- EXPLOSIVE: SL 3%/TP 5%(RR<1) → SL 5%/TP 10%(RR 2.0)
- scalp_engine `tp_rr` 2.5 → 3.0

**레버리지 전면 하향:**
- `leverage_range` [10,30] → [5,15]
- 등급별 상한: A+ 30→15, A 25→12, B+ 20→10, B 15→8
- 스캘핑: 고정 25x → `leverage_calc` 동적 계산 (연패 감소 적용)
- 연패 감소 강화: 1연패부터 ×0.8, 4연패 ×0.3

**sl_failsafe 과다 대응:**
- 포지션 체크 폴링: 활성 시 15초→3초, 비활성 15초→10초

**수수료 필터:**
- TP1 마진수익 ≤ 수수료 시 진입 차단 (수수료가 수익 먹는 거래 방지)

---

### 숏 편향 수정 + 급등락 감지 강화

매매 내역 분석 결과: 상승장(+1.26%)에서 숏 5건 전패(-9.25 USDT), 롱 2건 전승(+2.38 USDT).
평균회귀 시그널 3개가 추세 필터 없이 역추세 숏을 반복 생성하던 구조적 문제 수정.

**숏 편향 수정:**
- **평균회귀 시그널 추세 필터** (`scalp_engine.py`) — RSI2 Extreme(4.0x), VWAP MR(3.5x), Cascade Fade에 5m EMA50/200 매크로 추세 필터 추가. 상승장 숏 / 하락장 롱 가중치 0으로 차단
- **Cascade Fade 가중치 하향** — 4.5x → 2.5x (상방 캐스케이드 후 음봉 하나에 숏 치던 과잉 반응 억제)
- **VWAP MR 레인징 판별 교체** — `was_inside`(사실상 항상 True) → ADX < 25 기반 비추세 판별로 교체

**급등락 감지 강화 ($500-1000 움직임):**
- **WebSocket 가격 변속도 추적** (`ws_stream.py`) — 체결 데이터에서 10초/30초/60초 윈도우별 가격 이동량·범위 실시간 계산, Redis `rt:velocity:*`에 저장
- **실시간 스파이크 시그널 추가** (`scalp_engine.py` #22) — 10초 $150+ / 30초 $300+ / 60초 $500+ 방향성 이동 감지, 가중치 5.0x (추세 추종)
- **rapid_momentum 개선** — close-to-close만 보던 문제 수정, high/low 기반 실변동폭(bar_spike, range_pct) 감지 추가

---

## 2026-04-06 ~ 2026-04-15 (아카이브 요약)

> 상세 내역은 COMMIT_LOG.md 참조

- **04-06**: Phase 1~5 완료 (데이터/14기법/ML/매매엔진/백테스트/모니터링)
- **04-07**: ML v2 (레짐별 앙상블), 가상매매, 프랙탈, 학습 스케줄러
- **04-08**: 실거래 -90% 사고 → 보호 주문 전면 재구성, leverage 마진 공식 수정, margin_loss_cap
- **04-13**: CRITICAL 5 + HIGH 15 버그 수정 (PnL계산, 리스크한도, 레짐고정, ML)


- **04-15**: TradeEngine v1 통합 (Scalp/Swing→통합), Setup ABC, 주문 CRITICAL 수정
- **04-16~17**: 대시보드 별도 컨테이너, post-only limit, FlowEngine v1
- **04-23**: 레거시 정리 (main.py 1916→901줄)
- **04-27**: 42파일 전수검사 (CRITICAL 3+HIGH 9)
- **04-28**: SPEC v2 전면 개편 (FlowEngine→CandidateDetector+ML Meta-Label)
