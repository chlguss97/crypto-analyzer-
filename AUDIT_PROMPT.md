# BTC 자동매매 시스템 전수검사 프롬프트

> CryptoAnalyzer v2 (CandidateDetector + ML Meta-Label)
> 실전매매 / 페이퍼매매 / ML 세 경로의 정합성을 호출 체인 단위로 검증하고,
> 빠진 분기·미연결·불일치를 모두 찾아내는 자동 실행 프롬프트.

---

## 실행 모드

- **자동 실행**. 매 단계 승인 요청하지 말 것.
- 단, 아래 **"자동 중단 조건"** 에 해당하면 즉시 멈추고 사용자에게 보고.
- 그 외에는 **분석 → 수정 → 문서화 → 커밋**까지 끝까지 진행.

---

## 0단계: 시스템 맵

코드 만지기 전에 정리:

- 파일 트리 (실전 / 페이퍼 / ML / 공통)

```
src/
  main.py                        — 오케스트레이터 (후보→ML→실행 루프)
  strategy/
    candidate_detector.py         — 3종 후보 감지 (Momentum/Breakout/Cascade)
    ml_engine.py                  — ML Go/NoGo 이진분류 (Phase A/B)
    paper_trader.py               — 가상매매 (독립 가상 계좌)
    setup_tracker.py              — 셋업 성과 추적
    signal_tracker.py             — 시그널 기여도 추적
  trading/
    executor.py                   — OKX 주문 실행 (maker 강제)
    position_manager.py           — 포지션 SL/TP/트레일/Adverse Selection
    risk_manager.py               — 리스크 게이트 (일일/주간/DD/연패)
    leverage.py                   — 레버리지 계산
  data/
    ws_stream.py                  — OKX WS (전체 데이터: CVD/캔들/마이크로/고래/가격)
    binance_stream.py             — Binance REST (청산/펀딩비/OI만)
    candle_collector.py           — OKX REST (캔들 백필 + 30초 백업)
    storage.py                    — SQLite + Redis 래퍼
  engine/
    regime_detector.py            — 레짐 판별 (trending/ranging/volatile)
    base.py                       — to_dataframe 유틸
    fast/ (atr, bollinger, ema, rsi)
  monitoring/
    dashboard.py                  — FastAPI 대시보드
    telegram_bot.py               — 텔레그램 알림/명령
    trade_logger.py               — JSONL 영구 로그
  utils/
    helpers.py                    — load_config, get_env
config/
  settings.yaml                   — 전체 설정
```

- 진입점: `main.py:main()` (단일)
- 데이터 흐름: OKX WS → Redis → CandidateDetector → ML → OKX Executor → PositionManager → DB
  (청산/펀딩/OI만 Binance REST 폴링)
- 실전·페이퍼 분기: `main.py:_evaluate()` 에서 paper_trader.try_candidate_entry() + _execute() 병렬
- ML 학습: signals DB → ml_engine.check_and_train() (5분마다)
- ML 추론: candidate features → ml_engine.decide() → Go/NoGo

---

## 1단계: 호출 체인 검증

함수 A → B 호출 시:

- A의 return 필드 ↔ B의 파라미터: **이름·타입·순서**
- `None` / 빈 dict / `NaN` 가능성
- 단위 일치
  - 가격: USDT (BTC/USDT:USDT)
  - 수량: BTC (OKX는 contracts 단위 → _btc_to_contracts 변환)
  - 시간: epoch seconds (signals.ts) vs epoch ms (trades.entry_time)
- float 정밀도 (OKX tick size 0.1, 최소 주문 0.01 BTC)

핵심 체인:
```
CandidateDetector.detect() → dict
  → ml_engine.decide(features_raw) → (bool, float)
  → db.insert_signal(record) → int
  → paper_trader.try_candidate_entry(candidate, regime)
  → _execute(candidate, balance, regime, daily_pnl) → bool
    → position_manager.open_position(trade_req)
    → executor.open_position(direction, size, grade, entry_price, sl_price, leverage)

position_manager._process_position()
  → Adverse Selection 체크 (load_config 캐싱)
  → SL failsafe
  → TP progression / runner trailing
  → _full_close() → on_trade_closed callback → main._on_trade_closed()

periodic_shadow_check()
  → db.get_pending_shadows() → list[dict]
  → Triple Barrier 가격 비교
  → db.update_signal_label()
  → ml_engine.record_decision_result()
```

---

## 2단계: 상태 저장소 1:1 대조

### Redis
- `SET` / `HSET` 키 ↔ `GET` / `HGET` 키 (오타·접두사·TTL 만료 후 읽기)
- **마이크로스트럭처 키 (rt:micro:*)**: binance_stream 쓰기 ↔ candidate_detector 읽기
- **플로우 키 (flow:*)**: binance_stream 쓰기 ↔ candidate_detector 읽기
- **시스템 키 (sys:*)**: main.py 쓰기 ↔ dashboard/telegram 읽기
- **리스크 키 (risk:*)**: risk_manager 쓰기 ↔ risk_manager/dashboard 읽기

### DB (SQLite)
- **signals** 테이블: insert_signal() ↔ get_pending_shadows() ↔ update_signal_label() ↔ get_labeled_signals()
- **trades** 테이블: insert_trade() ↔ update_trade_exit() (position_manager + paper_trader)
- **candles** 테이블: insert_candles() ↔ get_candles()

### 메모리 vs Redis vs DB 동기화
- risk_manager._state (메모리) ↔ risk:* (Redis) — initialize()에서 로드, record_trade_result()에서 동기화
- position_manager.positions (메모리) ↔ pos:active:* (Redis) — sync_positions()에서 복원
- paper_trader 잔고 (메모리) ↔ paper:state (Redis) — restore_from_db()에서 DB 기반 복원

---

## 3단계: 실전 ↔ 페이퍼 정합성

단계별 비교:

| 단계 | 실전 (executor + position_manager) | 페이퍼 (paper_trader) |
|------|-------------------------------------|----------------------|
| 후보 감지 | CandidateDetector.detect() (공통) | 동일 |
| ML Go/NoGo | ml_engine.decide() (공통) | paper_trader.flow_ml.decide() |
| 주문 생성 | executor.open_position() → OKX API | paper_trader._record_entry() → DB |
| SL/TP 등록 | executor.set_protection() → OKX 알고 | paper_trader 내부 가격 비교 |
| 체결 처리 | OKX fill → position_manager | 즉시 체결 가정 |
| 수수료 | 실제 OKX fee | FEE_MAKER * 2 * leverage |
| Adverse Selection | position_manager._process_position() | 없음 (페이퍼는 미구현) |
| 포지션 갱신 | position_manager.check_positions() | paper_trader.check_positions() |
| 손익 계산 | position_manager._close_position() | paper_trader._close_position() |
| 재시작 복구 | sync_positions() (OKX fetch) | restore_from_db() (DB 기반) |

> 의도된 차이: 페이퍼는 슬리피지/부분체결/미체결 없음 (즉시 체결 가정)
> 확인 필요: Adverse Selection이 페이퍼에 없음 → 실전/페이퍼 성과 차이 원인

---

## 4단계: ML 경로 정합성

- **학습 피처 ↔ 추론 피처**: ml_engine.CORE_FEATURES / EXTENDED_FEATURES 목록이 candidate_detector._build_raw_features() 출력 키와 정확히 일치하는지
- **피처 순서**: _extract_feature_vector()가 feature_names 순서대로 추출하는지
- **정규화**: StandardScaler가 학습/추론 동일하게 적용되는지 (self.scaler)
- **라벨 정의**: Triple Barrier (TP hit=1, SL hit=0, Time=0) ↔ periodic_shadow_check 동일
- **look-ahead bias**: shadow 추적이 미래 가격을 쓰지 않는지 (Redis 실시간 가격만 사용)
- **학습 윈도우**: window_size 500 ↔ 실시간 features 동일 구조

---

## 5단계: HTML / API

- `getElementById` 하는 모든 ID가 DOM에 실제 존재
- `fetch` URL ↔ 백엔드 라우트 (메소드·경로·쿼리)
- API 응답 필드 ↔ JS에서 접근하는 필드명 (candidate_type, ml_go, price, label 등)
- signals 테이블: DB 컬럼명 ↔ JS 필드명 ↔ API 응답 키

---

## 6단계: 시간·경계·예외

- 캔들 윈도우 리셋 (CVD 5m/15m/1h) 시 delta 보존
- 재연결 후 누락된 캔들 백필 (candle_collector REST 30초 백업)
- 타임존: 전부 UTC (epoch seconds/ms)
- 동시성: asyncio 단일 루프 (GIL 보호), position_manager Lock
- 예외 발생 시 포지션 일관성:
  - 주문 나갔는데 DB 미반영 → sync_positions()에서 복원
  - SL 등록 실패 → sl_fail_close (즉시 청산)
  - Redis 다운 → fallback 기본값 (0, None → 안전한 방향)

---

## 7단계: 변수 스코프 검증 ★이전 검사에서 놓친 치명적 버그 유형

모든 함수에서 **사용하는 변수가 어디서 오는지** 확인:
- 파라미터로 받는가?
- self 속성인가?
- 함수 내에서 정의하는가?
- import한 모듈인가?
- **위 4가지 아니면 → NameError 버그**

특히 위험한 패턴:
- 함수 A에서 쓰던 변수를 함수 B에서도 같은 이름으로 쓰지만 B에는 전달 안 됨
- 리팩토링으로 함수를 분리했는데 변수 전달을 빠뜨림
- async 함수에서 외부 스코프 변수 참조 (closure 아닌 경우)

---

## 8단계: 외부 API 스펙 검증 ★코드만 읽지 말고 공식 문서 대조

코드 대 코드가 아니라 **코드 대 외부 API 공식 스펙** 대조.
이전 검사에서 놓친 치명적 버그들이 여기서 발견됨.

### WebSocket URL 형식
- Binance combined stream: `/stream?streams=s1/s2/s3` (NOT `/ws/s1/s2/s3`)
- Binance single stream: `/ws/<streamName>`
- OKX public WS: `wss://ws.okx.com:8443/ws/v5/public`
- 각 URL 형식이 공식 문서와 정확히 일치하는지

### WebSocket 메시지 형식
- Binance combined: `{"stream":"...", "data":{...}}` — unwrap 필요
- Binance single: 직접 `{"e":"aggTrade", ...}`
- OKX: `{"arg":{...}, "data":[...]}`
- 각 이벤트 필드명이 공식 문서와 일치하는지 (e, p, q, m, T 등)

### 라이브러리 버전 호환
- `pip list`로 실제 설치 버전 확인
- websockets: v14+ API 변경 (async with 패턴 → await connect + recv 루프)
- ccxt: unified API 필드명 변경 여부
- python-telegram-bot: v20+ async 전환
- asyncio: Python 3.12+ get_event_loop() → get_running_loop()

### 네트워크 접근성
- 서버에서 각 외부 엔드포인트 접근 가능한지 (curl 테스트)
- 방화벽/IP 차단 여부
- REST는 되는데 WS만 안 되는 케이스

### REST API 파라미터
- OKX: ordType, instId, tdMode, posSide 등 필수 파라미터
- Binance: fetch_ohlcv 심볼 형식 (spot: BTC/USDT, futures: BTC/USDT:USDT)

> **코드가 "문법적으로" 맞아도 "스펙에" 안 맞으면 조용히 실패.**
> 반드시 공식 문서 URL 참조해서 대조할 것.

---

## 발견 항목 출력 형식

| # | 심각도 | 카테고리 | 위치 | 증상 | 증거 | 권장 수정 |
|---|--------|----------|------|------|------|-----------|

### 심각도 정의

| 등급 | 설명 |
|------|------|
| **CRITICAL** | 자금 손실·중복 주문·포지션 꼬임·PnL 계산 오류 |
| **HIGH** | 실전/페이퍼 분기 누락, ML 피처 불일치, 거래 기록 누락 |
| **MEDIUM** | 로깅 누락, 에러 처리 미흡, 대시보드 표시 오류 |
| **LOW** | 네이밍·주석·dead code |

> 위치는 반드시 `파일명:줄번호`. 모르면 `파일명:?`. **추측 금지**.

---

## 9단계: 운영 상태 확인 (수정 전 필수)

코드 수정 전 자동 점검:

| 점검 항목 | 발견 시 동작 |
|-----------|--------------|
| 현재 포지션 보유 여부 | 보유 시 → **자동 중단**, 사용자 보고 |
| 진행 중·미체결 주문 | 있으면 → **자동 중단** |
| 봇 프로세스 실행 중 여부 | 실행 중이면 executor.py/position_manager.py 수정 시 → **경고** |

체크 방법:
- 포지션: Redis `pos:active:*` 키 / OKX fetch_positions
- 봇 상태: Redis `sys:bot_status`
- Docker: `docker compose ps`

---

## 10단계: 자동 수정 정책

### ✅ 자동 수정 OK

- LOW / MEDIUM 전체
- HIGH 중 명백한 오타·키 불일치·타입 불일치
- 페이퍼매매 코드의 모든 수정
- ML 학습 코드 (ml_engine.py)
- 대시보드 / 텔레그램 코드
- 문서·주석

### ⚠️ 자동 수정 + 강조 보고

- HIGH 중 로직 변경
- 실전매매 코드 (executor.py, position_manager.py)의 CRITICAL / HIGH

### 🛑 자동 중단 — 사용자 보고 후 대기

- 거래소 API 호출 파라미터 변경
- 주문 수량·가격 계산식 변경
- DB 스키마 변경 (마이그레이션 필요)
- .env, API 키, 인증 관련 코드
- 발견 항목이 **50개 초과**

---

## 11단계: 변경 이력 문서화

### 1) CHANGELOG.md 갱신

```markdown
## [날짜]

### Fixed
- (CRITICAL) ... (파일:줄)
- (HIGH) ... (파일:줄)

### Changed
- ...
```

### 2) 최종 요약 출력

- 검사 범위 (파일·함수 개수)
- 발견 항목 전체 표
- 자동 수정한 항목 / 보류한 항목
- 다음 검사 우선순위

---

## 12단계: Git 커밋

### .gitignore 누락 체크 (커밋 전)

다음 패턴이 stage에 들어갔으면 **자동 중단**:
- `.env`, `.env.*`, `*.key`, `*.pem`
- `*.db`, `*.sqlite`, `*.dump`, `*.bak`
- `*.pkl`, `*.h5`, `*.pt` (ML 모델 바이너리)
- `config/secret*`, `credentials*`
- 100MB 초과 파일

### 비밀값 패턴 grep (커밋 직전)

```regex
api_key\s*=\s*["'][^"']+["']
secret\s*=\s*["'][^"']+["']
password\s*=\s*["'][^"']+["']
```

발견 시 → **자동 중단**, 해당 라인 보고.

### 커밋 메시지 형식

```
<type>: <한 줄 요약>

- 변경 내용 (파일:줄)

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
```

> main 브랜치 직접 커밋 (우리 프로젝트는 main 직접 운영).
> .env, *.pkl, *.db 커밋 금지. 비밀값 grep 스캔 후 커밋.

---

## 🚫 절대 금지

- `git push --force`
- `.env`, API 키 커밋
- 운영 봇 포지션 보유 중 executor.py/position_manager.py 변경 후 즉시 배포
- 코드 안 보고 추측

---

## 진행 규칙

1. **CRITICAL 부터** 처리.
2. 코드 안 보고 추측 금지. 모르면 "확인 필요".
3. 진행률 주기적으로 보고.
4. 의도된 차이인지 버그인지 모호하면 **자동 수정 금지, 보류 처리**.
5. 종료 시 최종 요약.

---

> **시간 제한 없음. 누락이 가장 큰 죄. 천천히, 빠짐없이.**
