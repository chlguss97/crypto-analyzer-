# 변경 이력 (CHANGELOG)

대화 기반으로 수정한 모든 사항을 기록합니다.
전체 커밋 raw 인덱스는 [`COMMIT_LOG.md`](COMMIT_LOG.md) 참조 (자동 생성).

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

## 2026-04-13

### 전체 코드 정밀 분석 — CRITICAL 5 + HIGH 15 버그 수정

4개 병렬 에이전트로 전체 코드베이스 정밀 분석 후 발견된 20개 버그 수정.

**CRITICAL:**
- **PnL USDT 계산 오류** — `pos.size`(원본)→`pos.remaining_size` (TP1 50% 후 PnL 2배 계산되던 근본 원인)
- **리스크 한도 미작동** — `record_trade_result()` 미호출 → 일일-10%/주간-20% 한도 완전히 무시됨
- **ML threshold 재시작 리셋** — scalp `_load_v2` cap [0.5,2.5] ↔ `_adjust_weights` [4.0,7.0] 불일치 → 통일
- **레짐 전환 불가능** — 안정화 필터가 stabilized 값을 history에 기록 → 영구 고정 (raw_regime 기록으로 수정)
- **컨플루언스 방향 무시** — 보너스가 역방향 점수에도 가산 → 방향 일치 비율만 반영

**HIGH:** SL 나체 방지, Swing/Scalp 쿨다운 분리, trending boost 실적용, 시간청산 반복 방지, Redis 재연결, dashboard 인증 강화, kill switch 직접 청산, ML net_pnl 사용 등 15건

---

### 매매 차단 게이트 전면 완화 — 폭락장 무매매 사태 대응

주말 BTC 폭락에도 매매 0건 발생 → 안전장치가 동시 발동하여 모든 진입 차단된 구조적 문제 수정.

- **neutral 판정 완화** (`aggregator.py`, `scalp_engine.py`)
  - 양방향 충돌 임계값 0.8 → 0.6 (40% 차이까지 neutral)
  - neutral 시 점수 0 → dominant 방향 50% 감점으로 변경 (진입 가능)
- **grader 최소 점수 6.0 → 5.0** (`grader.py`)
  - C+ 등급 신설 (5.0+, 레버리지 10x, 사이즈 20%)
- **market_structure 횡보 하드리젝 제거** (`grader.py`)
  - ranging일 때 즉시 거부 → 로그만 남기고 통과
- **HH+LL / LH+HL = volatile** (`market_structure.py`)
  - 폭락 시 혼재 패턴을 ranging 대신 volatile로 판정
- **레짐 ranging 전면차단 → 조건부 허용** (`main.py`)
  - Swing: 점수 7.0+ 이면 ranging에서도 진입
  - Scalp: 점수 4.0+ 이면 ranging에서도 진입
  - to_ranging 전환 블록: 무기한 → 10분(Swing)/5분(Scalp) 후 자동 만료
- **SL 쿨다운 축소** (`main.py`)
  - Swing: 60분 → 30분
  - Scalp: 60분 → 15분
- **학습 중 매매 허용** (`main.py`)
  - 학습 락이 실거래 차단하지 않음 (가상매매만 차단 유지)
  - 일일 학습 1~2시간 동안 매매 공백 제거
- **scalp pending 확인 완화** (`main.py`)
  - 5초 후 가격 역행해도 점수 5.0+ 이면 강제 진입

---

## 2026-04-08

### 정밀 분석 후 6개 버그 수정 (Pass 1~5 ★★★)
4개 병렬 에이전트로 ~10,000줄 코드 정밀 분석 → critical 3 + high 3 확정 후 수정.

- **`d0f4425` 안전성** — Redis 끊김 fallback + WS JSON 보호 + heartbeat timeout
  - **BUG #2**: storage.set/get silent fail → `sys:learning` 키 read 실패 시 None == "1" 비교 False → 학습 중 신규 진입 차단 무력화. main.py에 `_learning_local` 메모리 fallback 추가, 5개 체크 사이트에서 OR 조건 사용.
  - **BUG #3**: ws_stream `_connect`의 `json.loads(message)` 가 raw 호출이라 잘못된 메시지 1건이 연결 끊김 → 5~60초 재연결 → 데이터 누락. JSON parse + handle_message 각각 try/except + continue.
  - **M1**: `periodic_heartbeat`의 `get_balance()` 가 멈추면 60초간 heartbeat 정지. `asyncio.wait_for(timeout=5.0)` 추가.
- **`687bfda` 시그널 정확도** — CVD 1봉 lag 제거 + BOS strength 보정 + scalp 지연 단축
  - **BUG #1**: ws_stream이 15m 경계에서 `_cvd_15m`을 redis에 set하고 0으로 리셋 → main.py가 redis에서 읽으면 항상 직전 윈도우 합계 → 시그널 엔진이 1봉(15분) lag된 CVD로 평가. `cvd:15m:current` 키 신설 (매 체결마다 갱신), main.py가 우선 읽기.
  - **BUG #5**: `scalp_engine._break_of_structure`의 `strength = min(1.0, overshoot*100+0.5)` → 0.01% 돌파만으로도 strength=1.0 (노이즈 만점). `min(1.0, max(0.3, overshoot*50+0.3))`로 변경 (0.5%→0.55, 1%→0.8, 2%→1.0).
  - **BUG #6**: `periodic_scalp_eval` 시작 시 `sleep(20)` → 봇 재시작 직후 4 사이클 손실. `sleep(2)`로 단축.
- **`ec4b5fd` 시그널 점수 인플레이션 보정**
  - **BUG #4**: WEIGHTS 합 26.0 + CONFLUENCE_BONUSES 합 9.5 → 한 방향 분산 ~13 + 보너스 5 = raw 18 → 정규화 18/12*10 = 15 → clip 10. 보너스 2~3개만 동시 발동해도 점수 천장 도달, A+(≥9.0) 트리거 너무 쉬움. `REALISTIC_MAX_SCORE: 12.0 → 18.0`, `MAX_CONFLUENCE_BONUS = 5.0` cap 신설.
- **`98daa0a` 정리** — SQLite WAL + WS unknown channel 로깅 + dead code 삭제
  - SQLite `PRAGMA journal_mode=WAL + synchronous=NORMAL` (다중 코루틴 동시성).
  - WS `_handle_message`에 unknown channel 로깅 (OKX 신규 채널 감지).
  - **dead code 삭제**: `src/signal_engine/ml_model.py` (옛 v1 RF 모델, 현재 `adaptive_ml.py` 사용. grep 0건).

#### 검증 후 false positive로 기각된 항목
- regime_history race (Pass 1) — asyncio 단일 이벤트 루프, await 없는 두 줄 사이 코루틴 전환 불가
- scalp_engine OB look-ahead (Pass 2) — `end = n - 5` 로 미래 봉 미사용
- slow OrderBlock/FVG look-ahead (Pass 1 의심) — `lookback = min(100, len-5)`, `range(2, len-1)` 로 안전

### 보호 주문 파이프라인 전면 재구성 (실거래 -90% 사고 후) ★★★
- **`4832098`** 진입 시 SL+TP1/TP2/TP3 OKX 서버사이드 일괄 등록
  - `algoClOrdId` underscore 제거 (OKX 영숫자만 허용) — SL/TP 알고 등록 정상화
  - `check_positions` SL failsafe — 가격이 SL 넘으면 강제 청산
  - `pnl_pct` 를 leveraged 계좌 PnL로 변경, raw는 `price_pnl_pct` 분리
  - `_update_trailing` 가격% 트리거 → 가격 기반 TP 도달 감지
  - 진입 직후 50%/30%/20% TP1/2/3 등록, TP1 → 본전+0.1% 이동(반익본절), TP2 → SL TP1 가격 잠금, TP3 → 잔량 종료
  - SL 등록 실패 시 진입 즉시 되돌림 (보호 없는 포지션 금지)
  - `cancel_algo_order` 다중 fallback, `OKX_DEMO=1` 데모 트레이딩 지원
  - `scripts/test_protection_orders.py` 검증 스크립트 추가
- **`95192d3`** TP2/TP3 → 러너 트레일링 (옵션 A) 전환
  - TP1만 50% 고정 익절, 잔여 50%는 트레일링 SL로 추세 끝까지
  - `Position.runner_mode/best_price/trail_distance` 필드 추가
  - `_update_runner_trail` — 가격이 새 고/저 갱신 시 SL을 `best_price ± trail_distance`로 추격
  - 러너 모드는 시간 청산 8시간 hard limit만 적용
- **`349ac21`** 러너 모드 정밀 검수 — 7개 critical
  - `get_position_size` 단위 불일치 (contracts → BTC 정규화)
  - 진입 직후 `fill_price=0` 가능성 → `fetch_positions` 보정, 그래도 실패 시 즉시 청산
  - TP1 이중 처리 race (서버 자동 + 봇 폴링) → 사이즈 동기화로 즉시 마킹
  - 알고 주문 `ordType="trigger"` 명시
  - float 비교 → epsilon (`< 1e-8`)
  - 러너 trail 최소 거리 `max(tp1_dist*0.5, entry*0.003)` — 노이즈 방어
  - `_reconcile_external_close` reason 분류 갱신

### 매매 안정성 정밀 분석 ★★★
- **`baae27b`** 20-pass 정밀 분석 — 8 critical
  - **leverage 마진 공식 버그** (사용자 -90% 사고 근본 원인): `margin = risk / sl_pct` → `margin = risk / (leverage × sl_pct/100)`
  - `_execute_scalp` 동일 마진 공식 수정
  - `RiskManager.current_balance` OKX 30초 throttle 자동 동기화 (`executor` 주입)
  - 좀비 포지션 방지 강화 — fill/SL 실패 + 청산 실패 → 자동매매 정지 + 텔레그램 emergency
  - DB `insert_trade` 실패 시 음수 임시 ID로 진행
  - `_partial_close` fractional contract 거부 방어 (`math.floor`)
  - **PositionManager `_symbol_locks`** — 동일 symbol 동시 처리 방지
  - **`_self_heal_algos`** — SL/TP 알고가 None이면 매 폴링마다 자동 재등록
  - **`sync_positions` 실제 구현** — 거래소 포지션 발견 시 Redis에서 복원, 없으면 긴급 SL + 텔레그램
- **`6ca4b4e`** 5+회 정밀 분석 — 8 critical/high
  - `_full_close` 가 close 실패 시 좀비 → SL 긴급 재등록 + 메모리 유지 + 다음 폴링 재시도
  - `_finalize_position()` 신설 — DB/Redis/메모리/텔레그램/ML 일관 처리 (small position TP1 100% 케이스 정상 정리)
  - `notify_exit` 누락 → `PositionManager.telegram` 주입
- **`4b64866`** 8개 추가 정밀 검수
  - `Position.to_dict()` Redis hset 호환 (`algo_ids` json.dumps + bool→int)
  - `order.fee=None` AttributeError 방어 (3곳)
  - `_execute_scalp` 마진 캡 (`min(margin, balance*0.3)`)
  - 학습 중 `paper_trader.try_entry` + `check_positions` 스킵 (CPU 경합 방지)
  - `_check_time_exit` 부분 청산 후 SL 알고 사이즈 명시적 갱신
  - `_on_tp_hit` partial_close 실패 시 race 보정 (서버 사이즈 재확인)
  - **소형 포지션 (1 contract) TP1 100% 분기** — `filled_size < MIN_ORDER_SIZE × 2` 면 TP1 100% 청산, 러너 모드 비활성 (계좌 $28 케이스 직접 해결)
  - `_reconcile_external_close` reason 분류 (runner_trail/breakeven/tp1_full_server)

### 사이즈/SL 정책 — 마진 손익% 기반 통일
- **`361604f`** **margin_loss_cap 모드 신설**
  - `sizing_mode: margin_loss_cap` (vs 옛 `risk_per_trade`)
  - SL 가격 거리 = `max_margin_loss_pct / leverage` (10x+10% → 1.0%, 25x+10% → 0.4%)
  - 매물대 SL이 더 가까우면 매물대 사용 (보수적), 멀면 마진 한도 사용
  - `margin_pct` (잔고 대비 마진 비율, 기본 95%)
  - 작은 계좌 ($30) 진입 가능 — 잔고 $28.5 마진 → 0.01 BTC + SL 0.4% (마진 -10%) + TP1 1.5R (마진 +15%)
- **`fbb6985`** TP/트레일도 마진 % 기준으로 통일
  - `tp1_margin_gain_pct` (기본 +15%) — 가격 거리 = `% / leverage`
  - `trail_margin_pct` (기본 5%) + `trail_min_price_pct` (기본 0.2%) 노이즈 방어
  - `min_indicator_sl_price_pct` (기본 0.05%) — 매물대 SL이 너무 가까우면 마진 한도 사용
  - **사용자 수동 SL/TP 수정** — `PositionManager.manual_update_sl/tp(symbol, price)` + 대시보드 `POST /api/position/sl|tp`
  - sanity check (방향 일치, 마진 손실 50% 이하), `manual_sl_override`/`manual_tp_override` 플래그 — 트레일/self_heal이 사용자 수정 존중
  - **`sys:active_model` 기본값 `both` → `scalp`** (스캘핑 중점)

### 운영 인프라
- **`097143f`** 클라우드 로그 자동 디지스트 + GitHub logs 브랜치 푸시
  - `scripts/log_digest.sh` — 트레이딩 핵심 이벤트만 추출 (진입/청산/TP/러너/ERROR/WARNING/거부 통계)
  - `scripts/log_push.sh` — `digests/{ts}.txt` + `latest.txt` 갱신, 7일 초과 자동 삭제
  - cron: `*/15 * * * * cd ~/crypto-bot && ./scripts/log_push.sh 20`
  - `logs` 브랜치에 분리 저장 → main 브랜치 오염 없음
- **`883abff`** 헬스체크 + 학습-매매 격리
  - `scripts/health_check.sh` — 컨테이너/heartbeat/시그널/포지션/잔고/학습 상태 한 줄 (text/json)
  - 종합 판정 OK/DOWN/STALE
  - log_digest에 헬스체크 결과 자동 포함
  - **학습-매매 격리** — `_guarded_study()` 헬퍼로 `redis sys:learning=1` set/unset, 학습 중 신규 진입 차단
  - **IO 블로킹 해결** — `ml.save()` 를 `asyncio.to_thread()` 로 래핑 (sklearn pickle dump가 이벤트 루프 막던 문제)
- **`cb1758a`** 학습 중 폴링 5초 + 스케줄 조용한 시간대 이동
  - `periodic_position_check` 가 학습 중 + 활성 포지션 있으면 5초 폴링 (TP1 본절 이동 지연 30~45초 → 5초)
  - 학습 스케줄: `UTC 02/10/18` → **`UTC 22 (KST 07:00) / UTC 04 (KST 13:00) / UTC 11 (KST 20:00)`** — EU 피크/한국 출근 회피, 글로벌 최저 활동 시간

### 데이터 수집
- **`af1b0f6`** 캔들 조회 1회 재시도 + 에러 본문 명확
  - ccxt `fetch_ohlcv` 일시 오류 시 1초 후 재시도
  - 에러 type + repr 300자까지 로깅 (URL만 잘리던 문제 해결)

---

## 2026-04-07

### 1주일 운영 최종 안정성 강화 (★★★ 5건)
- **adaptive_ml.py save()**: tempfile + shutil.move 원자적 저장 → pkl 손상 방지
- **signal_tracker.py save()**: 동일 원자적 저장 → JSON 손상 방지
- **main.py periodic_daily_reset**: day 비교 → date() 객체 비교 (월 변경 안전), 60→30초 주기
- **main.py cleanup**: Graceful Shutdown 강화 (ML/Tracker 저장 우선, 미청산 포지션 로그)
- **ws_stream.py**: CVD 오버플로우 방어 (±1e9 클램프)

### 안정성 보강 6건
- **adaptive_ml.py _load_v2**: 시그널 이름 마이그레이션 자동 처리 (scalp_ob → order_block)
- **ws_stream.py**: WebSocket 무한 재연결 (3회 → 무제한, 최대 60초 대기)
- **main.py**: asyncio.gather return_exceptions=True (한 태스크 죽어도 봇 안 죽음)
- **storage.py**: Redis set/get/hset/hgetall/delete/publish 모두 try/except (silent fail)
- **main.py**: 30일 이상 paper 거래 자동 삭제 (DB 무한 증가 방지)
- **docker-compose.yml**: 로그 회전 (bot 50MB×5, redis 10MB×3)

### 대시보드 UI 재구성 + 도움말 툴팁
- **3탭 → 4탭 거래소 스타일**:
  - Dashboard (핵심 운영)
  - Market & Signals (시장 분석)
  - AI / Models (ML 전체)
  - Risk & System (리스크/뉴스/시스템) — 신규
- **빠진 UI 4개 추가**: Real Trading Risk, News Filter, Auto Backtest, Meta Learner
- **System Status 카드**: 봇 상태/하트비트/자동매매/ML 활성화
- **명칭 직관화 16개**: Recent Trades → Real Trade History 등
- **23개 카드에 도움말 툴팁(?) 추가**: 마우스 오버 시 한국어 설명
- **시그널 이름 변경**: scalp_ob → order_block, scalp_fvg → fvg

### SignalTracker — 시그널 기여도 추적 시스템
- **신규 모듈**: `src/strategy/signal_tracker.py`
- 각 거래의 활성 시그널(강도≥0.3) 추출 → 강도 비례 P&L 분배
- 시그널별 누적 통계: 거래수, 승률, 평균 P&L, 기여도 점수, 모드별/레짐별
- **자동 식별**: 약한 시그널(평균 -0.1% 이하) / 강한 시그널(+0.2% 이상)
- **기여도 점수 공식**: `avg_pnl × min(1, sqrt(trades)/10) × (0.5 + wr×0.5)`
- 100건마다 자동 저장 (data/signal_tracker.json)
- **대시보드 표시**: ML 탭에 강한/약한 시그널 + 전체 랭킹 테이블
- API: `GET /api/signal-tracker`, `POST /api/signal-tracker/reset`

### 전체 코드베이스 버그 점검 ★ 항목 (10건)
- **bollinger.py**: NaN 비교 방어 (`pd.isna` 체크) + 데이터 부족 시 안전 반환
- **executor.py**: API 키 로그 노출 방지 (예외 메시지 마스킹)
- **candle_collector.py**: 무한 루프 방어 (max_iterations + new_current 체크)
- **adaptive_ml.py**: 조용한 실패 → debug 로그 추가 (GBM/RF/LR 학습 실패 추적 가능)
- **paper_trader.py**: shadows 동시 수정 안전 (인덱스 → 객체 기반 제거)
- **base.py**: `to_dataframe` 입력 검증 + NaN 행 제거 + 빈 캔들 방어
- **meta_learner.py**: deque import 함수 밖으로 이동 (성능)
- **main.py**: 스케줄러 예외 `exc_info=True` 추가 (디버깅 용이)
- **grader.py**: 입력 검증 강화 (이미 .get() 안전)
- **rsi.py**: inf 처리 (이미 수정됨)

### 전체 코드베이스 버그 점검 및 수정 (★★★ + ★★ 11건)
- **main.py:504**: 스캘핑 TP2가 TP1과 동일했던 치명적 버그 → TP1×1.6 거리로 수정
- **position_manager.py**: entry_price 0 나누기 방어 + None 체크 추가
- **historical_learner.py**: exit_price None + 빈 future 캔들 방어
- **meta_learner.py**: max_imp 0 나누기 방어
- **storage.py**: get_candles 정렬 일관성 (항상 ASC 반환)
- **grader.py:110**: A+ 하향 시 GRADES[2] 중복 → A+→A, A→B+ 정상화
- **paper_trader.py**: _regime_history IndexError 방어
- **auto_backtest.py**: i+2 슬라이싱 범위 오버플로 방어
- **aggregator.py**: ob_zone NaN 비교 처리 (math.isnan)
- **rsi.py**: avg_loss=0 inf 처리 + clip(0, 100)
- **position_manager.py**: 동시성 안전 (positions dict 체크)

### ScalpEngine v4 — 전체 점검 후 14개 버그/로직 수정
- **버그 픽스**:
  - OB 인덱싱 (음수→양수, 정확한 탐지)
  - FVG 방향 확인 추가 (추세 일치 시만)
  - 거래량 스파이크 1m→5m
  - ADX 계산 Wilder's smoothing 정통 방식
  - BOS 스윙 조건 완화 (양옆 2봉→1봉)
  - 핀바 우선순위 (장대봉보다 먼저 체크)
- **로직 개선**:
  - 필터 다중 폭락 해결 (곱셈→가산 페널티, 최대 50%만 감점)
  - SMC entry 방향 일치 확인
  - RSI 25/75→30/70 완화
  - 변동성 폭발 방향 5봉 누적으로 안정화
  - Pivot Points 100봉으로 완화
  - Liquidity Sweep ATR 정규화
  - VWAP 거래량 0 방어
  - 안티첩 ADX<18 + 방향전환≥6 동시 만족
- **기타**: 아시아 세션 0.7→0.8, SMC/급변동은 안티첩 무시, penalty 필드 추가

### 클라우드 배포
- **Vultr Singapore** $6/월 ($1.2 백업) 운영 시작
- IP: `207.148.120.103`
- Docker Compose 통합 운영
- ML 모델 13,820건 / 700건 클라우드 이전
- sklearn 1.8.0 버전 고정
- pkl 저장 안전장치 (빈 버퍼 덮어쓰기 방지)
- Redis 환경변수 지원 (`REDIS_HOST`/`REDIS_PORT`)

### 문서화
- **MANUAL.md**: 운영 매뉴얼 작성 (서버 운영, 설치, 명령어, 문제 해결)
- **CHANGELOG.md**: 변경 이력 작성
- **자동 업데이트 규칙**: 변경사항 발생 시 두 파일 자동 갱신

### 인프라 / 배포
- **클라우드 배포 완료**: Vultr Singapore ($6/월)
  - IP: `207.148.120.103`
  - Ubuntu 22.04 LTS, 1 vCPU, 1GB RAM
  - Docker Compose로 봇 + Redis 통합 운영
- **운영 매뉴얼 작성** (`MANUAL.md`)
- **변경 이력 작성** (`CHANGELOG.md`)
- **대시보드 보안**: HTTP Basic Auth 추가
  - `DASHBOARD_USER` / `DASHBOARD_PASS` 환경변수
- **Redis 환경변수 지원**: `REDIS_HOST`/`REDIS_PORT` (Docker용)

### ML 시스템 대규모 업그레이드 (v2)
- **AdaptiveML v2**: 레짐별 멀티모델 + 앙상블
  - 글로벌 GBM + 레짐별 GBM/RF/LR (최대 14개 모델)
  - 60+ 강화 피처 (세션, 요일, 시그널 변화율, 레짐 원핫, 크로스 피처)
  - Walk-forward 검증 (Train/OOS 추적)
  - v1 → v2 자동 마이그레이션
- **MarketRegimeDetector**: 4레짐 자동 판별
  - trending_up / trending_down / ranging / volatile
  - ADX, +DI/-DI, BB Width, ATR%, EMA 배열 종합
  - 안정화 (2회 연속 같아야 전환)
- **레짐별 전략**: 추세장 1.0x, 횡보 0.7x, 고변동 0.5x 레버리지
- **MetaLearner**: 자가 업그레이드 시스템 (매주 일요일)
  - 하이퍼파라미터 Grid Search (5종)
  - 피처 중요도 분석 → 약한 시그널 가중치 자동 감소
  - 모델 종류 자동 선택 (GBM vs RF vs LR)
  - 동적 재학습 주기 (50/100/200)
  - 자가 진단 + 자동 복구

### 학습 시스템
- **HistoricalLearner**: 과거 캔들로 시그널 재현 → 대량 학습
  - SL 4종 다양화 (0.8/1.0/1.2/1.5x)
  - 90일 캔들 자동 수집
  - 레짐별 집중 학습
  - 급변동 구간 집중 학습
- **PaperTrader**: 가상매매 + Shadow 추적
  - 점수 2.0+ 모든 시그널 진입 (전수 학습)
  - 미진입 시그널 30분 추적 → 놓친 기회 학습
- **AutoBacktest**: 매일 자동 백테스트 (최근 30일)
- **학습 스케줄러**: 하루 3회
  - UTC 02:00 (한국 11:00): 대량 학습 + 백테스트 (+ 일요일 메타 학습)
  - UTC 10:00 (한국 19:00): 세션 경량 학습
  - UTC 18:00 (한국 03:00): 세션 경량 학습

### ScalpEngine v3 (스캘핑 전문)
- **시그널 18종** 구성:
  - 기본 5: EMA크로스, RSI반전, BB돌파, 거래량스파이크, 모멘텀
  - 급변동 4: 변동성폭발, 레인지브레이크아웃, 캔들패턴, 급속모멘텀
  - SMC 3: 1m/5m 오더블록, 유동성스윕, 1m FVG
  - 강화 3: VWAP 일중 레벨, 피봇 포인트, BOS (Break of Structure)
  - 필터 2: 세션 필터, 안티첩 필터
  - 관리 1: 트레일링 스탑
- **3가지 진입 모드**:
  - SMC: SL 0.5x ATR, TP 2.5R
  - 급변동: SL 0.6x ATR, TP 1.5R + 트레일링
  - 일반: SL 0.8x ATR, TP 2.0R
- **세션별 배율**: US/EU 1.2x, US 1.1x, EU 1.0x, 아시아 0.7x, 주말 0.6x
- **포지션 크기 차등**: SMC 120%, 급변동 100%, 일반 80%

### 매매 엔진
- **스캘핑 전용 평가 루프**: 15초 (Swing 60초)
- **진입 확인 대기**: 시그널 발생 → 15초 후 방향 확인 → 진입
- **연패 쿨다운**: 3연패 5분, 5연패 30분
- **스캘핑 일일 손실 한도**: -10% 자동 중단
- **임계값 자동 조정 상한**: Swing 8.0/4.0, Scalp 5.0/2.5

### 리스크 관리
- **실거래 일일 -10% / 주간 -20% 자동 차단**
- **주차 자동 감지** + 리셋
- **NewsFilter**: FOMC, CPI, NFP, PPI 등 ±30~60분 매매 차단

### 프랙탈 지표
- **FractalIndicator**: Williams Fractal 멀티스케일 (2/3/5)
- 돌파 감지, 클러스터 존, 지지/저항 자동 식별
- aggregator + ML 가중치 통합

### 대시보드
- **Market Regime 카드**: 실시간 레짐 + 점수 + 지표
- **Swing/Scalp 레짐 모델 분리** 표시
- **Paper Trading 통계** (5건/10건 갱신)
- **Scalping Mode 패널**: 점수, 방향, 일일PnL, 연패, 급변동, SMC, 세션
- **한/영 언어 전환** (EN/KR 토글, localStorage 저장)
- **별도 스레드 구조**: ML 학습 중에도 응답 보장

### 성능 최적화
- ML save/log 빈도 축소 (50건/100건마다)
- historical_learner CPU 양보 빈도 증가 (10건마다)
- dashboard.py 중복 캔들/시그널 루프 제거
- 시작 시 역사 백필 → 스케줄러로 이동

### 버그 픽스
- v1→v2 마이그레이션 시 피처 구조 변경 처리
- DB 심볼 불일치 (BTC-USDT-SWAP → BTC/USDT:USDT)
- 대시보드 무한 로딩 (uvicorn 별도 스레드 분리)
- Scalp 임계값 무한 상승 → 상한 5.0 제한
- 최근 거래에 paper 표시 → real만 필터
- ML record_trade 콜백에 signals 전달 (실거래 학습)
- dashboard.py 함수 정의 순서 (verify_auth)
- Redis 호스트 환경변수 미적용

---

## 2026-04-06 이전
- Phase 1~5 완료 (데이터 수집, 14개 기법, ML, 매매 엔진, 백테스트, 모니터링)
- 듀얼 모델 (Swing/Scalp) 초기 구축
- 웹 대시보드 초기 버전
