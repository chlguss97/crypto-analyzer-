# BTC 자동매매 시스템 전수검사 프롬프트

> CryptoAnalyzer v2 — 4경로 시스템 (Shadow + PaperLab + SimTrader + 실거래)
> 4경로 / AdaptiveParams / ML / Drift플래그 / 확신도6점 전체 정합성 검증
> 마지막 갱신: 2026-05-08

---

## 실행 모드

- **자동 실행**. 매 단계 승인 요청하지 말 것.
- 단, 아래 **"자동 중단 조건"** 에 해당하면 즉시 멈추고 사용자에게 보고.
- 그 외에는 **분석 → 수정 → 문서화 → 커밋**까지 끝까지 진행.

---

## 0단계: 시스템 맵

```
src/
  main.py                        — 오케스트레이터 (4경로 분기 + 확신도 6점 + drift 플래그)
  strategy/
    candidate_detector.py         — 후보 감지 (fast_momentum/momentum/breakout/cascade + drift 플래그)
    ml_engine.py                  — ML Go/NoGo (Phase A/B/B+)
    adaptive_params.py            — 수치 자동 보정 (7모듈, Phase TP_SL=10/DIRECTION=30/FULL=300)
    paper_lab.py                  — PaperLab A/B 3-Variant + drift 비교
    sim_trader.py                 — SimTrader 실전 시뮬 (확신도+게이트+h1/h4)
    setup_tracker.py              — 셋업 성과 추적
    signal_tracker.py             — 시그널 기여도 추적
  trading/
    executor.py                   — OKX 주문 (post-only/market, SL market-on-trigger)
    position_manager.py           — 포지션 SL/TP/트레일/Adverse Selection + AdaptiveParams 추적 필드
    risk_manager.py               — 리스크 게이트 (is_trading_allowed: DD/daily/cooldown)
    leverage.py                   — 레버리지 계산 (ATR/grade 기반)
  data/
    ws_stream.py                  — OKX WS (전체 데이터)
    binance_stream.py             — Binance REST (청산/펀딩비/OI)
    candle_collector.py           — OKX REST (캔들 백필)
    storage.py                    — SQLite + Redis (signals 테이블: reach_pct/mae_pct/best_move_pct)
  engine/
    regime_detector.py            — 레짐 판별 (4종)
    base.py                       — to_dataframe
    fast/ (atr, bollinger, ema, rsi)
  monitoring/
    dashboard.py                  — FastAPI 대시보드 (lab:stats 참조)
    telegram_bot.py               — 텔레그램 (13 commands: /status /adaptive /lab /shadow 등)
    trade_logger.py               — JSONL 영구 로그
  utils/
    helpers.py                    — load_config, get_env
config/
  settings.yaml                   — 전체 설정 (Phase A 운영값)
```

### 3경로 구조

```
시그널 발생
  ├─→ Shadow (시장 관찰): 모든 시그널 추적, reach%/mae%/best_move% + label → ML + AdaptiveParams
  ├─→ PaperLab (실험): 3 Variant(tight/base/wide) 동시 시뮬 → AdaptiveParams
  └─→ 실거래 (검증): is_trading_allowed → ML Go → 확신도 점수(0~5) → 사이즈 비율 진입
                       AdaptiveParams 보정값(tp_mult, sl_pct, size_mult) 사용
```

### 데이터 흐름

```
OKX WS → Redis → CandidateDetector → ML → {Shadow, PaperLab, 실거래}
  Shadow → DB signals (label + reach%/mae%) → ML 학습 + AdaptiveParams
  PaperLab → AdaptiveParams (reach%/mae%) + drift 비교 데이터
  SimTrader → AdaptiveParams (확신도별 성과, h1/h4 EV) ← autotrading 무관
  실거래 → AdaptiveParams (검증) + position_manager (worst_price, first_profit_ts)
  AdaptiveParams → Redis adaptive:state (persist) + 실거래 파라미터 override
```

---

## 1단계: SPEC 기반 검증

SPEC_V2.md 섹션별 코드 대조:

| SPEC 섹션 | 검증 대상 |
|-----------|----------|
| §1 아키텍처 | 4경로 분기 (Shadow+PaperLab+SimTrader+실거래) |
| §3 CandidateDetector | 3종 조건 (momentum/breakout/cascade), Phase A 완화값 |
| §4 FeatureExtractor | 8→25 피처, signals 테이블 스키마 (reach_pct/mae_pct/best_move_pct) |
| §5 ML | Phase A(<100) 무조건Go, Phase B(100+) go>0.55, Phase B+(300) 회귀 트리거 |
| §5.3 Shadow | 전 시그널 추적, ATR barrier(no min_sl), best/worst 추적, 연속값 DB 저장 |
| §6 Executor | post-only 3회→포기(strength<1.5), market(>=1.5), SL market-on-trigger |
| §7 SL/TP | ATR TP1(AdaptiveParams 보정), trail_atr_mult=1.5, giveback, R-lock |
| §7.3 Adverse Selection | 3 AND 조건 (margin+CVD+vol_surge>=1.5) |
| §8 리스크 | is_trading_allowed 5게이트 + 확신도 0~5점 사이즈 |
| §10 AdaptiveParams | 7모듈, Phase 10/30/300, Redis persist, paper+shadow 피딩 |
| §11 settings.yaml | Phase A/B 값 구분, hold_modes, ml config |

---

## 2단계: 변경 키워드 역추적 ★SPEC에 없는 것도 잡는 핵심 단계

SPEC 기반만으로는 텔레그램/대시보드 같은 부수 파일을 놓침.
**변경된 인터페이스의 키워드를 전체 코드에서 grep**하여 영향 파일 발견.

### 검색 대상 키워드

```
구 → 신 전환:
  paper_trader → paper_lab
  PaperTrader → PaperLab
  paper:state → lab:stats
  paper_balance → (제거 또는 lab 기반)
  _is_regime_aligned → _calc_conviction (확신도로 대체)
  _check_htf_trend → _calc_conviction (통합)

신규 추가:
  conviction, CONVICTION_MULT
  adaptive, AdaptiveParams, adaptive:state
  PaperLab, paper_lab, lab:stats
  reach_pct, mae_pct, best_move_pct
  shadow_tracking, worst_price, first_profit_ts
  entry_atr, entry_h1_trend, entry_h4_trend, params_snapshot
```

### 실행 방법

```bash
for keyword in paper_trader PaperTrader "paper:state" paper_balance conviction adaptive lab:stats reach_pct shadow_tracking; do
  echo "=== $keyword ==="
  grep -rn "$keyword" src/ config/ scripts/ --include="*.py" --include="*.html" --include="*.js" --include="*.yaml" --include="*.sh"
done
```

**모든 참조가 현행 코드와 일치하는지 확인. 구 키워드가 남아있으면 수정.**

---

## 3단계: 호출 체인 검증

### 핵심 체인 A: 시그널 → 3경로 분기

```
CandidateDetector.detect() → candidate dict
  → ml_engine.decide(features) → (go, prob)
  → db.insert_signal(record) → sig_id
  
  → paper_lab.on_candidate(candidate, regime)      ← 게이트 전 (전부 진입)
  
  → drift 플래그 체크: check_drift_flag(df_5m) → _drift_dir 캐시
  → drift 활성 시 PaperLab에 drift 가상 후보도 전달
  
  → _calc_conviction(6점: 1h+4h+regime+str+cvd+drift) → (score, detail)
  → score == 0 → gate_block JSONL → return
  → sim_trader.try_entry(같은 conviction/SL/TP) ← autotrading 무관
  → _execute(candidate, balance, regime, daily_pnl, conviction=score)
    → adaptive.get_tp_mult(regime) → ATR 배수
    → adaptive.get_sl_margin_pct() → SL 마진%
    → adaptive.get_entry_size_mult(...) → 사이즈 배수
    → CONVICTION_MULT[conviction] → conviction_mult
    → position_manager.open_position(trade_req) → pos
    → pos.entry_atr, pos.entry_h1_trend, pos.params_snapshot 세팅
```

### 핵심 체인 B: 포지션 관리

```
position_manager._process_position(symbol, pos, price)
  → worst_price / first_profit_ts 업데이트
  → Adverse Selection (3 AND: margin + CVD + vol_surge)
  → SL failsafe
  → TP1 50% close → runner trailing
  → _full_close() → on_trade_closed callback
    → risk_manager.record_trade_result()
    → ml_engine.record_decision_result()
    → adaptive.record_trade({reach_pct, mae_pct, ...})
```

### 핵심 체인 C: Shadow 추적

```
periodic_shadow_check()
  → db.get_pending_shadows() → WHERE label = -1 (전 시그널, entry_executed 무관)
  → shadow_tracking[sig_id] = {best, worst} (메모리 추적)
  → 매 폴링: best/worst 업데이트
  → barrier 도달 시:
    → reach_pct, mae_pct, best_move_pct 계산
    → db.update_signal_label(sig_id, label, barrier, pnl, ts, reach_pct, mae_pct, best_move_pct)
    → ml_engine.record_decision_result(False, label)
    → adaptive.record_trade({reach_pct, mae_pct, ...})
    → JSONL shadow_result (reach_pct, mae_pct 포함)
    → shadow_tracking.pop(sig_id)
```

### 핵심 체인 D: PaperLab

```
paper_lab.on_candidate(candidate, regime)
  → 3 Variant 각각 독립 진입 (TP/SL 계산, variant별 파라미터)
  → JSONL lab_entry

paper_lab.check_positions(price)
  → best/worst 업데이트
  → SL/TP1/runner/time 체크
  → _close_position(variant, pos, price, reason)
    → reach_pct, mae_pct 계산
    → adaptive.record_trade({...})
    → JSONL lab_exit
```

### 핵심 체인 E: AdaptiveParams

```
adaptive.record_trade(result)
  → 7모듈 각각 update()
  → _save_state() → Redis adaptive:state (TTL 30일)
  → JSONL adaptive_update

adaptive.get_tp_mult(regime) → Phase 2+(10건) 시 보정값, 아니면 기본 1.5
adaptive.get_sl_margin_pct() → Phase 2+(10건) 시 보정값, 아니면 기본 5.0
adaptive.get_entry_size_mult(dir, h1, h4, regime) → Phase 1+(30건) 시 EV 기반
```

---

## 4단계: 상태 저장소 대조

### Redis 키 매핑

| 키 | 쓰는 곳 | 읽는 곳 | 비고 |
|----|---------|---------|------|
| `sys:autotrading` | telegram /on /off | main.py _evaluate | |
| `sys:regime` | main.py | telegram, dashboard | |
| `sys:balance` | main.py | telegram, dashboard | |
| `sys:trade_state` | main.py | telegram /market /risk | ml_phase, ml_labeled, streak |
| `adaptive:state` | adaptive_params._save_state | telegram /adaptive, adaptive.load_state | JSON, TTL 30일 |
| `lab:stats` | main.py (paper_lab.get_stats) | telegram /lab, dashboard | JSON, TTL 300s |
| `rt:price:BTC-USDT-SWAP` | ws_stream | main.py, shadow, position_mgr | |
| `flow:combined:cvd_*` | ws_stream/binance | candidate_detector, position_mgr(AS) | |
| `bn:vol_ratio_1m` | binance_stream | position_mgr (AS vol_surge) | |

**구 키 확인**: `paper:state`, `paper:positions` → 더 이상 쓰지 않아야 함

### DB (SQLite) signals 테이블

```sql
signals (
  id, ts, candidate_type, direction, strength, price, features,
  ml_go, ml_prob, entry_executed, reject_reason,
  label, barrier_hit, pnl_pct, resolve_ts, regime,
  reach_pct, mae_pct, best_move_pct,    ← 2026-05-07 추가
  created_at
)
```

**확인**: `get_pending_shadows()`가 `WHERE label = -1` (entry_executed 조건 없음)

### DB SELECT ↔ downstream 컬럼 매핑 ★데이터 흐름 추적

**원칙: SQL SELECT의 컬럼이 downstream에서 실제로 사용되는지 1:1 대조.**
이전 검사에서 `regime` 컬럼 누락으로 TPCalibrator 보정 불가 사고 발생 (2026-05-08).

```
get_pending_shadows() SELECT:
  id        → update_signal_label(sig_id)
  ts        → elapsed 계산
  candidate_type → hold_mode 매핑 (shadow barrier)
  direction → TP/SL 방향
  price     → barrier 계산
  features  → atr_pct 추출 (ATR TP1 계산)
  regime    → AdaptiveParams bucket 분류 (trending/ranging/other)
  
  누락 시: regime 없으면 → bucket="other" → trending/ranging 보정 불가
```

```
get_labeled_signals() SELECT:
  *         → ml_engine._train()에서 features, label, entry_executed 사용
  
  확인: features JSON 키 ↔ ml_engine.CORE_FEATURES/EXTENDED_FEATURES 일치
  확인: entry_executed → 가중치 2.0배 적용
```

```
record_trade() 호출 경로별 데이터 완전성:
  Shadow → {regime: DB에서, h1/h4: "unknown", reach_pct: 계산}
  PaperLab → {regime: "unknown", h1/h4: "unknown", reach_pct: 계산}
  실거래 → {regime: pos.params_snapshot, h1/h4: pos.entry_h1/h4, reach_pct: 계산}
  
  주의: "unknown"으로 들어가는 필드는 해당 모듈에서 보정 데이터로 못 씀
```

**검사 방법:**
1. 모든 `db.execute(SELECT ...)` 쿼리의 컬럼 목록 추출
2. 리턴된 dict/Row가 downstream에서 `.get("컬럼명")` 으로 접근하는 곳 전부 추적
3. SELECT에 없는 컬럼을 `.get()`하면 → None → 기본값 → **잘못된 데이터 흐름**

### Position 추적 필드

```python
worst_price, first_profit_ts, entry_atr, entry_h1_trend, entry_h4_trend, params_snapshot
```

**확인**: `_process_position()`에서 worst_price/first_profit 업데이트, `on_trade_closed`에서 pos_data로 전달

---

## 5단계: 3경로 정합성

| 항목 | Shadow | PaperLab | 실거래 |
|------|--------|----------|--------|
| 진입 조건 | 없음 (전부) | 없음 (전부) | 확신도 0~5점 |
| TP1 | ATR barrier (no min_sl) | Variant별 ATR mult | AdaptiveParams 보정 ATR |
| SL | margin_pct/15 (고정) | Variant별 sl_pct | AdaptiveParams 보정 |
| 포지션 관리 | 없음 (barrier만) | 간단 SL/TP1/runner | 풀 관리 (AS, trail, R-lock) |
| AdaptiveParams 피딩 | O (reach%/mae%) | O (reach%/mae%) | O (reach%/mae%+검증) |
| ML 피딩 | O (label 0/1) | X | O (record_decision_result) |
| JSONL | shadow_result | lab_entry/lab_exit | entry/exit |

**확인 포인트**:
- PaperLab이 게이트 **전에** 호출되는지 (main.py 흐름)
- Shadow가 **entry_executed 무관**하게 추적하는지
- 3경로 모두 **AdaptiveParams에 동일 형식**으로 피딩하는지

---

## 6단계: ML 경로 정합성

- 학습 피처 ↔ 추론 피처: CORE_FEATURES/EXTENDED_FEATURES ↔ _build_raw_features()
- 피처 순서: _extract_feature_vector() 순서 일치
- 정규화: StandardScaler 학습/추론 동일
- 라벨: Shadow barrier 결과 (reach% 기반이 아닌 label 0/1)
- Phase 전환: 100건 → Phase B, 300건 → Phase B+ 알림
- default min_samples: 100 (코드 + config 일치)

---

## 7단계: 텔레그램 + 대시보드 + 스크립트

**SPEC 기반 검사에서 빠지는 영역. 반드시 별도 검증.**

### 텔레그램 (telegram_bot.py)
- 13개 명령어 모두 작동하는지
- /status: ML Phase + Lab 요약 표시
- /adaptive: tp_mult, sl_pct, Phase 표시
- /lab: 3 Variant 비교 표시
- /shadow: label 분포, reach% 평균
- /risk: DD%, daily PnL, margin% (구 하드코딩 streak 없음)
- notify_entry: 확신도 점수 + 사이즈 비율
- 구 PaperTrader 참조 없음

### 대시보드 (dashboard.py + index.html)
- paper:state → lab:stats 참조 확인
- API 응답 필드 ↔ JS 필드명 일치
- 구 Paper Trading 탭 → PaperLab 정보로 전환 필요

### 스크립트 (scripts/)
- health_check.sh: 현행 Redis 키 참조
- log_push.sh: JSONL 형식 (shadow_result에 reach_pct 포함)

---

## 8단계: 외부 API 스펙 검증

- OKX: ordType, instId, tdMode, posSide 필수 파라미터
- Binance: REST 폴링 엔드포인트
- WebSocket URL/메시지 형식
- ccxt/websockets/telegram 라이브러리 버전 호환

---

## 9단계: 시간·경계·예외

- 단위: 가격 USDT, 수량 BTC (OKX contracts 변환), 시간 epoch seconds/ms
- 동시성: asyncio Lock (position_manager)
- 예외: 주문 실패 → sl_fail_close, Redis 다운 → fallback
- 재시작: adaptive.load_state(), position sync, ML state 복원

---

## 10단계: 변수 스코프 + 런타임 패턴

- 모든 함수에서 변수 출처 확인 (파라미터/self/local/import → 아니면 NameError)
- hot path에서 파일 I/O 금지 (load_config 캐싱)
- 매 호출 객체 재생성 금지

---

## 11단계: 운영 상태 확인 (수정 전 필수)

| 점검 항목 | 발견 시 |
|-----------|---------|
| 포지션 보유 | **자동 중단** |
| 미체결 주문 | **자동 중단** |
| 봇 실행 중 + executor 수정 | **경고** |

---

## 12단계: 자동 수정 정책

| 대상 | 정책 |
|------|------|
| LOW/MEDIUM | 자동 수정 |
| HIGH (오타/키 불일치) | 자동 수정 + 보고 |
| HIGH (로직 변경) | 자동 수정 + 강조 보고 |
| CRITICAL (거래소 API/주문 계산) | **자동 중단** |
| .env/API키/인증 | **자동 중단** |
| 50개 초과 발견 | **자동 중단** |

---

## 발견 항목 형식

| # | 심각도 | 카테고리 | 위치 | 증상 | 권장 수정 |
|---|--------|----------|------|------|-----------|

심각도: CRITICAL(자금) > HIGH(분기/피처) > MEDIUM(로깅/표시) > LOW(네이밍)

---

## 13단계: Git 커밋

- .env, *.pkl, *.db 커밋 금지 (grep 스캔)
- main 직접 커밋
- `Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>`

---

## 🚫 절대 금지

- `git push --force`
- `.env`, API 키 커밋
- 포지션 보유 중 executor/position_manager 즉시 배포
- 코드 안 보고 추측

---

## 진행 규칙

1. CRITICAL 부터 처리
2. 코드 안 보고 추측 금지
3. **SPEC 검사 + 키워드 역추적 둘 다 실행** (어느 하나만 하면 누락)
4. 의도된 차이 vs 버그 모호하면 보류
5. 종료 시 최종 요약

> **시간 제한 없음. 누락이 가장 큰 죄. 천천히, 빠짐없이.**
