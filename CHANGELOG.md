# 변경 이력 (CHANGELOG)

대화 기반으로 수정한 모든 사항을 기록합니다.

---

## 2026-04-07

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
