# Commit Log

> 자동 생성 — `scripts/update_commit_log.sh` (매 커밋 후 실행)
> Updated: 2026-05-14 11:01:32    
> Total commits: 350 (2026-04-03 → 2026-05-14)

Claude 가 질문/변경 작업 시 이 파일을 참고해서 과거 변경 이력 컨텍스트를 확보합니다. 수동 편집 금지 — 다음 커밋 시 덮어써집니다.

## 2026-05-14
- `49b94011` fix: 전수검사 — AdaptiveParams 데이터 손실 + params_snapshot + PaperLab 오염 외 8건
- `30da8aab` fix: profit_stop/profit_protect 완전 제거 — 일일 수익 상한 폐지

## 2026-05-13
- `98483736` fix: ML Phase B 조기전환 방지 (min_samples 100→300) + FAST 로그 dedup

## 2026-05-12
- `647d935b` fix: 전수검사 — 미완성봉 DB저장 제거 + velocity 타입불일치 + docstring
- `8b9f53b1` fix: 전수검사 — AUDIT 9단계 추가 + TTL 누락 + self.symbol 미정의
- `7061eb1d` fix: CRITICAL 캔들 INSERT OR IGNORE → UPSERT (미완성봉 덮어쓰기)
- `3a5a4812` fix: SL algo 검증 주기 1분 + 시간 기반 throttle
- `1eb0e870` fix: 시간청산 제거 + SL algo OKX 소실 감지/재등록
- `6cb5c682` fix: CRITICAL 3건 — SL과대 + 비정상값 + 시간청산 누락

## 2026-05-08
- `cb66fc4e` docs: AUDIT 4경로+SimTrader+drift플래그 반영
- `f8983881` fix: SimTrader 전수검사 7건 수정
- `9e309309` fix: PaperLab에서 drift 진입 제거 — SimTrader가 담당
- `6767ee01` feat: SimTrader — 실거래 동일 로직 가상매매
- `79224e55` fix: PaperLab에 drift 가상 진입 데이터 수집 추가
- `c6b015cc` refactor: drift를 진입 시그널 → 확신도 플래그로 전환
- `f326cb7c` docs: SPEC §3.7 Drift + §3.8 Weak Momentum + §3.9 빈도표 추가
- `9c045e32` fix: PaperLab regime+atr_pct 저장 → AdaptiveParams 정확한 bucket 분류
- `804afc49` fix: AUDIT CRITICAL+MEDIUM — NameError 방지 + shadow hold_mode 매핑
- `70189891` docs: AUDIT에 DB SELECT↔downstream 컬럼 매핑 검사 추가
- `99e0891e` fix: get_pending_shadows에 regime 컬럼 추가
- `060c26e8` feat: Drift Detector 추가 — 점진적 추세 감지 (4번째 후보)

## 2026-05-07
- `e3a3770d` refactor: 리스크 게이트 5개 → 3개 (확신도 사이즈에 위임)
- `6d7514f2` fix: 주간 손실 한도 게이트 제거
- `1e22af35` debug: 리스크 게이트 차단 로그 추가 (eval loop 디버깅)
- `7db88c9c` fix: CRITICAL eval loop pubsub 블로킹 → 단순 1초 폴링으로 교체
- `8f8815b7` fix: 전수검사 HIGH 1 + MED 2 + LOW 2 수정
- `ccbd8b48` cleanup: oi_funding + daily_summary 테이블 완전 제거
- `cf09e6aa` cleanup: dead code 삭제 + DB 스키마 정리
- `287eb0f4` cleanup: 레거시 전면 정리 — paper_trader 삭제 + FlowEngine/FlowML 별칭 제거
- `62142f29` docs: SPEC §2 데이터소스 현행화 + §3.6 1분 고속 감지 추가
- `59a9aac1` fix: fast_momentum shadow barrier에 quick hold_mode 매핑
- `143d0414` feat: 1분 고속 모멘텀 감지 추가 (5분 정규 평가와 병행)
- `890aeb02` perf: 마이크로스트럭처 15개 지표 계산 비활성 (Phase B+까지)
- `77e143f2` feat: CVD/Whale을 Binance Futures WS로 이관 (OKX 대비 3~5배 거래량)
- `66ad63d9` feat: JSONL 로그 전면 정리 — 복기/분석 완전 지원
- `e0266026` fix: AUDIT 전수검사 CRITICAL 1 + HIGH 2 + MED 1 수정
- `d916fd64` docs: AUDIT_PROMPT.md 복구 및 전면 갱신
- `3f892e93` fix: dashboard.py paper:state → lab:stats 참조 수정
- `dd9af8ce` feat: 텔레그램 전면 개편 — 3경로 시스템 대응
- `dbb1a2ad` docs: MD 파일 정리 — 2개 삭제 + 3개 현행화 + CHANGELOG 50% 축소
- `247ba236` docs: SPEC v2 전수검사 갱신 — 아키텍처/게이트/Phase/PaperLab 반영
- `773cec06` feat: 확신도 기반 사이즈 + AdaptiveParams 실거래 연결
- `953f0dd5` feat: PaperTrader → PaperLab 전면 교체 (A/B 파라미터 테스터)
- `deb3b5ad` feat: shadow 연속값 추적 (reach%, MAE, best_move) + 회귀 전환 트리거
- `9acc79ac` fix: TPCalibrator 조기 활성화 + paper 데이터 기여
- `146736b9` fix: paper 벤치마크 완전 순수화 — 게이트 전 위치 이동 + 내부 필터 제거
- `6878af48` fix: 전수검사 — post-only market 폴백 제거 + paper 레거시 게이트 정리
- `145533a4` feat: AdaptiveParams 수치 자동 보정 엔진 (SPEC §10)
- `9601d6b0` feat: 상위 TF 추세 게이트 — 1h/4h EMA20 역행 차단
- `46f0a6f9` fix: shadow ATR barrier + paper 포지션 무제한
- `6a83de9b` fix: paper 리스크 게이트 제거 — 벤치마크 목적 순수화
- `8e54e3be` fix: shadow 라벨 수집 가속 — entry_executed 필터 제거

## 2026-05-06
- `52dae59a` fix: DD gate 활성화 + paper_trader TP1 ATR 누락 경로 수정
- `40c23a76` fix: 전수검사 CRITICAL 4건 — market threshold, trail ATR, AS vol_surge, ML default
- `37df5ded` feat: 3중 게이트 + TP1 ATR 전환 — 매매 복기 기반 손실 방지

## 2026-05-04
- `1f35647e` config: ML Phase B 가속 — shadow max_hold 4시간 + min_samples 100
- `583234e2` fix: WS 한쪽 끊김 시 양쪽 재연결 + 끊김 로그 추가
- `1bfae44b` config: margin_pct 0.40→0.80 (잔고 80% 사용)
- `a94b59b4` fix: CRITICAL SL market-on-trigger 복원 + 서버 TP1 realized_pnl 누적

## 2026-04-30
- `6db9951d` refactor: 시간 청산 로직 전면 제거 — SL/TP/트레일링에 위임
- `beaca80a` fix: CRITICAL PnL 수수료 미차감 + post-only 호가 반전 + market 폴백
- `65eac432` fix: Adverse Selection config → __init__로 이동 (hot path load_config 제거)
- `f31ea1d3` docs: AUDIT 12단계 깨진 문자 스캔 추가 + 메모리 저장
- `2d367a85` fix: market 진입 기준 1.5→1.0 + 깨진 한글 수정
- `2be86d84` feat: 강한 시그널(strength>=1.5) market 진입 허용
- `09e3b837` fix: CRITICAL — LeverageCalculator 매번 생성 → load_config FileNotFoundError

## 2026-04-29
- `177b5631` debug: DB 테이블 확인 스크립트
- `47a05071` fix: 대시보드/텔레그램 레거시 정리
- `e2bca74b` config: 로그 시간대 KST (Asia/Seoul) — 내부 로직은 UTC 유지
- `8a0c44af` debug: 캔들 volume 확인 스크립트
- `cdc11c5b` docs: 전수검사 7단계 추가 — 변수 스코프 검증
- `131e4e91` fix: CRITICAL — _build_raw_features df_1m 미정의 NameError
- `9b9b65bc` config: 후보 감지 조건 완화 (Phase A 데이터 수집)
- `4b5aa1f2` config: 일일 손실 한도 -5%→-10% (Phase A 데이터 수집)
- `bc4fe366` feat: Phase A 무조건 Go — 데이터 수집 가속 (마진 40% 유지)
- `c7386945` feat: 약한 후보(weak_momentum) shadow 전용 — ML 데이터 2~3배 가속
- `73744305` chore: 디버그 로그 제거 + 운영 로그 유지
- `8891c6f0` debug: _evaluate 조기리턴 로깅 추가
- `90aa805f` debug: OKX 캔들 WS 디버그 로깅 추가 (임시)
- `a8c9a4d1` fix: OKX 캔들 WS → /business 엔드포인트 분리
- `85806ad7` refactor: OKX WS 단일 데이터 소스 전환 — Binance 의존 제거
- `4d10ccef` fix: Futures REST now 변수 스코프 에러
- `45ca0c04` feat: Futures REST 하이브리드 — 청산/펀딩비/OI 5초 폴링
- `5fc1d2ce` fix: Binance spot WS 확정 — futures WS 모든 엔드포인트 차단
- `a2e543b0` fix: Binance WS 새 엔드포인트 wss://ws-fapi.binance.com 시도
- `1ee615a9` fix: Binance WS 구독 메시지 방식으로 전환 (공식 라이브러리 동일)
- `54cc1dad` docs: 전수검사 프롬프트 보완 — 원본 대비 누락 3건 추가
- `423894ad` docs: 전수검사 프롬프트에 7단계(외부 API 스펙 검증) 추가
- `bbc55b64` fix: CRITICAL — Binance combined stream URL 형식 수정
- `8b5f2103` fix: Binance futures WS 복원 + 디버그 로깅 강화
- `19b9c87e` fix: Binance futures 403 차단 → spot 엔드포인트 전환
- `7ff360b7` fix: ml_engine asyncio.get_event_loop→get_running_loop (Python 3.12+ 호환)
- `fcd8924a` chore: 모든 패키지 최신 버전으로 (버전 제한 제거)
- `1b2430fc` chore: requirements.txt 정리 — 미사용 제거 + 최신 호환
- `f299558b` fix: CRITICAL — websockets v16 호환 (async with → await + recv 루프)
- `084c3220` debug: Binance WS 첫 메시지 로깅 (임시)
- `6ab9898c` perf: post-only 대기 3초→2초 (최대 6초 블록)
- `160a9bd6` fix: post-only 블록 40초→9초 (3초×3회) — 스캘핑 속도 확보
- `3e2003b0` fix: 전수검사 2차 — HIGH 8건 수정
- `34250610` fix: CRITICAL — Binance WS combined stream 파싱 누락
- `796b0c94` fix: 보류 6건 전부 수정 — 페이퍼 실전 동기화 + ML 라벨 + Redis 복구
- `633f347d` fix: 전수검사 CRITICAL 1건 + HIGH 4건 수정
- `2b8dc206` fix: 전수검사 CRITICAL 3건 + HIGH 1건 수정

## 2026-04-28
- `2aab4e2d` docs: 전수검사 프롬프트 — 프로젝트 맞춤 수정
- `d79e6071` fix: signals 테이블 JS 필드명 4건 수정 + dead doc 제거
- `73a25e2e` refactor: 레거시 파일명 제거 — flow_engine→candidate_detector, flow_ml→ml_engine
- `65ab33c5` fix: CRITICAL — PaperPosition 생성자 필수 필드 누락 (grade/score/entry_time)
- `18052e39` feat: ML Phase 전환 + 마일스톤 텔레그램 즉시 알림
- `fcf4dd96` feat: 마이크로스트럭처 15종 피처 — 프롭 트레이더의 눈
- `6387cb9b` feat: 모든 봇 행동 JSONL 로그 — 후보감지+ML결정+shadow결과
- `745f97bc` fix: 웹 대시보드 v2 완전 개편
- `e510e1cb` fix: 프론트엔드 레거시 완전 제거
- `bbf33698` fix: 레거시 잔재 전수 정리 — dead code/중복 백필/옛 셋업명
- `5c3a6992` fix: Adverse Selection load_config 매초 호출 → 1회 캐싱
- `12382cb6` fix: 대시보드/텔레그램/페이퍼 v2 동기화
- `9b294f1a` refactor: SPEC v2 전면 개편 — CandidateDetector + ML Meta-Label
- `adab4448` feat: maker 강제 + ranging 차단 — 수수료/횡보 손실 근절

## 2026-04-27
- `7a33f804` feat: 모멘텀 게이트 — 떨어지는데 롱 치지 마
- `462a806e` fix: 오늘 거래 분석 기반 4건 수정
- `155ae570` fix: 서버TP1 후 SL 재등록 + log_push 브랜치 복귀 실패 방지
- `92767735` fix: SL 최소값 0.35%→0.5% — 5m 노이즈 밖으로
- `0d2184a1` fix: _append_jsonl import를 파일 상단으로 이동 (런타임 크래시 방지)
- `8cd5365e` fix: 로깅 보완 5건 — 주간번호 통일 + 이벤트 이력 보존 + 상태 기록
- `534abf7a` fix: log_push에 bot.log/trades.log/signals.log 주간 파일 포함
- `168214d6` feat: 주간 로그 영구 보존 — 월요일 기준 파일 로테이션
- `cbfb27c6` feat: 로깅 영구 보존 — bot.log 파일 핸들러 + exit JSONL 필드 보강
- `dabf8d38` fix: sl_failsafe 디버그를 JSONL에도 영구 기록 (Docker 로그 유실 대비)
- `2496d3fb` fix: sl_failsafe 52% 문제 — SL 검증 대기 강화 + TP1 race condition 제거
- `39380159` fix: 전수검사 20건 버그 수정 — CRITICAL 3 + HIGH 12 + MEDIUM 5

## 2026-04-24
- `e17736e9` feat: Signal Score — 마지막 시그널 30초 유지 표시
- `a5ae2207` fix: 대시보드 전수검사 — structure→vol_band + API fallback + 중복호출 제거
- `d0078b8d` feat: 일일 리포트 통합 + ML 마일스톤 알림
- `c5740e04` fix: 페이퍼 잔고 초기화 문제 — Redis TTL 제거 + heartbeat 갱신
- `16dbbf4d` feat: 동적 트레일 SL — 이익비례 + 시간축소 + 정체감지 + R-lock
- `f66a6c6f` fix: is_tp → is_limit_trigger 변수명 수정 (NameError)
- `136232d0` feat: 러너 SL을 limit-on-trigger로 변경 (maker 수수료 0.02%)
- `76ceac49` fix: 지정가/시장가 로직 3건 수정
- `fa553faf` fix: CRITICAL — position_manager filled_size 단위 오류 (contracts→BTC)
- `65dcb41b` fix: 텔레그램 셋업 감지 알림 제거 — 진입 시에만 알림
- `af8b4b85` feat: 실거래 ON — 페이퍼 병행 모드
- `c2d441fa` fix: 전수검사 최종 — streak 쿨다운 KeyError 방지 + HTML 레거시 정리
- `2b9acf81` fix: 전수 검사 WRONG_DATA 6건 수정 (call-chain 추적)
- `86da0133` fix: 뿌리 뽑기 — CRITICAL 4 + HIGH 7 + MEDIUM 4 수정
- `469890b1` fix: 전체 버그 수정 12건 (CRITICAL 2 + HIGH 10)
- `df9e83cd` fix: 대시보드 페이퍼 탭 데이터 매핑 수정
- `9599bd0e` fix: PaperTrader 잔고 DB 복원 — 재시작 시 리셋 방지
- `232e2951` feat: FlowEngine v2 — 6셋업 다중 진입 (15건/일 목표)

## 2026-04-23
- `dad13bc4` fix: Flow x Regime 히트맵 A/B/C → FLOW 동적 + by_regime 추적 추가
- `11621c95` refactor: SetupTracker A/B/C → FLOW 단일 셋업으로 전환
- `8fda090e` refactor: 레거시 코드 전면 제거 + 대시보드 버그 수정 + 페이퍼 탭 추가
- `ed7e555a` refactor: PaperTrader v2 — 독립 가상 계좌 아키텍처 (실거래 OFF)

## 2026-04-17
- `f2c860b6` chore: COMMIT_LOG 갱신
- `63cf350c` fix: 서버 크래시 3건 수정 — OKX API + None 참조 + 1d 데이터 부족
- `89ea7632` fix: OKX 알고 조회 ordType 파라미터 누락 — 51000 에러 수정
- `e27a41e3` chore: COMMIT_LOG 갱신
- `ab1f4ceb` refactor: 텔레그램 + 대시보드 FlowEngine 전면 패치
- `237babdc` docs: CHANGELOG + 메모리 — FlowEngine v1 전체 기록
- `4393b318` chore: COMMIT_LOG 갱신
- `466abe1d` fix: FlowEngine 검증 — CVD 노이즈 필터 + 1d 레벨 추가
- `c7cbe840` chore: COMMIT_LOG 갱신
- `a120ee77` refactor: 레거시 코드 정리 — FlowEngine 전용 체계 확립
- `d7ea6997` chore: COMMIT_LOG 갱신
- `b5d24c86` feat: FlowML — FlowEngine 전용 경량 ML 접목
- `60980f16` chore: COMMIT_LOG 갱신
- `a312589f` feat: FlowEngine v1 — 단순 오더플로우 엔진 (ABC 모델 대체)
- `1decdbcf` chore: COMMIT_LOG 갱신
- `7ec5e08e` fix: 점수 정합성 — 최소점수 체크 + 등급 동적 매핑
- `900148b0` chore: COMMIT_LOG 갱신
- `25c3d11d` fix: HTF 하드차단 → 편향 가감 전환 + 추세 돌파 감지
- `984a42e7` chore: COMMIT_LOG 갱신
- `5e44d87d` feat: HTF 큰 추세 필터 — 4h/1d/1w 캔들 + 역방향 차단
- `e068c5d9` chore: COMMIT_LOG 갱신
- `a70c1b5c` fix: Binance 통합 전수 검증 보완 3건
- `4297b36a` chore: COMMIT_LOG 갱신
- `ab0e0747` perf: 지연 0ms — 이벤트 드리븐 평가 + 청산 폭발 감지
- `db7818c8` chore: COMMIT_LOG 갱신
- `2401673f` perf: Binance WS 캔들 스트림 — REST 폴링 제거, 0ms 지연
- `87d75266` chore: COMMIT_LOG 갱신
- `22198a0a` perf: 캔들/평가 주기 가속 — Binance rate limit 활용
- `e73cbce4` chore: COMMIT_LOG 갱신
- `013607ac` feat: 차트 분석 Binance 선물 기준 전환 — OKX는 실행만
- `262f7f08` chore: COMMIT_LOG 갱신
- `36559c4b` feat: Binance WS 실시간 스트림 — 시장 전체 오더플로우
- `b3f9b053` docs: CHANGELOG 4/16~17 업데이트 (대시보드 개편, 알고 잔존 수정, 오더플로우)
- `71c659d5` chore: COMMIT_LOG 갱신
- `a082a66f` feat: 오더플로우 방향 확인 + 봇 스냅샷 + SL 안전장치
- `c21daa3d` chore: COMMIT_LOG 갱신
- `444b2c7c` fix: 4/16 거래 복기 문제점 3건 수정
- `06106a0e` chore: COMMIT_LOG 갱신
- `ed748122` feat: 대시보드 전면 개편 — Engine 탭 + 레거시 ML 10개 엔드포인트 삭제

## 2026-04-16
- `8a43dd4f` chore: COMMIT_LOG 갱신
- `3a397e54` fix: 전 주문 post-only limit 우선 — maker 수수료 0.02% (taker 대비 60% 절감)
- `9fdc77e5` chore: COMMIT_LOG 갱신
- `d6e092bb` fix: 미체결 알고 주문 잔존 근본 원인 6건 제거
- `a175cca9` chore: COMMIT_LOG 갱신
- `25866d5c` refactor: Signal Performance Ranking 섹션 제거
- `89a0fa82` chore: COMMIT_LOG 자동 갱신
- `4d580b64` fix: 대시보드 크래시 루프 근절 + /health 엔드포인트
- `1ca10954` fix: 대시보드 별도 Docker 컨테이너로 완전 분리
- `5dcceeed` fix: 대시보드 모듈 로드 시 DB/Redis 인스턴스 생성 제거
- `429f3309` feat: Setup A 전면 전환 — 모멘텀 추격 → 풀백 진입
- `3e548fe5` fix: uvicorn 로그 활성화 (dashboard.log)
- `31650229` fix: 대시보드 lazy init — startup hang 근절
- `7c3234bf` fix: 대시보드 startup 경량화 + 불필요 섹션 제거
- `77948fbe` fix: 포지션 복원 시 알고 중복 등록 근절
- `ba8194c6` fix: 포지션 종료 시 OKX 잔존 알고 전체 정리
- `db0be171` fix: 레버리지 설정 시 알고 충돌(59668) 자동 정리 후 재시도
- `3ef6c224` fix: 대시보드 별도 프로세스로 분리 (asyncio 루프 충돌 근절)
- `241ca118` fix: PAPER_UNIFIED → PAPER_SETUP_A/B/C 명칭 변경
- `67381bb4` fix: Dockerfile config/ 명시적 COPY (BuildKit 캐시 누락 방지)
- `43466a11` fix: 셋업 ABC 보완 — 과완화 리스크 방지
- `22f96e25` feat: 셋업 ABC 조건 완화 — 하루 3건+ 매매 목표
- `31b497d4` feat: 셋업 reject 이유 로깅 — 왜 진입 안 하는지 진단
- `0cf36809` config: 레버리지 15~20x (SL 0.4~0.53% + 사이즈 확보)
- `a19dea52` fix: margin_pct 0.30→0.50 (OKX 최소주문 0.01 BTC 미달 해결)

## 2026-04-15
- `311627ce` fix: 로깅 빈 곳 4건 수정
- `f5a71b8c` fix: 주문/포지션 CRITICAL 6건 + HIGH 6건 수정
- `98cc3136` fix: 봇 재시작 시 포지션 없어도 stale 알고 주문 전체 정리
- `74635250` fix: /api/setup-tracker 에러 핸들링 추가
- `5077d706` fix: uvicorn 대시보드 스레드에 명시적 이벤트 루프 생성
- `5ee52906` fix: 대시보드 Redis/DB 이벤트 루프 충돌 수정
- `0c00cc61` fix: 대시보드 API fetch에 credentials 추가 (401 문제 해결)
- `142db0b7` fix: Self-Improving AI → SetupTracker 교체 + 구 meta/backtest API 정리
- `9d935c74` feat: SetupTracker 자기개선 + 전수 버그 수정
- `6474c7e9` fix: 대시보드 Swing/Scalp/ML 잔존 섹션 → TradeEngine으로 교체
- `e677d865` fix: ctx UnboundLocalError — 변수 스코프 수정
- `986bd152` feat: 텔레그램 명령어 확장 + 셋업 알림 + 리밋 오더
- `c616fea3` feat: 대시보드 TradeEngine 전환 + 실거래 ON
- `3875eafe` fix: UnifiedEngine → TradeEngine 명칭 변경 + 충돌/데드코드 수정
- `2aa04e28` fix: settings.yaml timeframes 레거시 호환 복구 (candle_collector KeyError)
- `5be7de8d` fix: settings.yaml 레거시 호환 필드 복구 (leverage.py KeyError)
- `605dee30` feat: 전면 개편 — Unified Engine v1 (셋업 ABC 통합 모델)
- `e1319866` perf: 캔들 갱신 3초 + 스캘핑 평가 3초 + 포지션 체크 1초
- `9ba3f3b7` feat: 오더블록 v3 — MSB+ChoCH+유동성+임펄스품질 전문가급 구현
- `e41ed335` feat: 시그널 구조 전면 개편 — 추세추종 중심 + 멀티TF 오더블록
- `e03fdcd3` config: 레버리지 10~25x
- `0df9e057` config: 레버리지 5~25x + 포지션 체크 1초
- `7dddd8d8` fix: 구조적 손실 근절 — SL/TP/레버리지 전면 개편
- `7bcff602` fix: 숏 편향 수정 + 급등락 $500-1000 감지 강화

## 2026-04-13
- `2889f5c3` fix: LongShortRatioIndicator NoneType 비교 에러 수정
- `41a18b79` feat: 스캘핑 기법 강화 + ML 스캘핑 엔진 업그레이드
- `de273c0b` feat: 해외 유명 스캘핑 기법 3개 추가 + scalp threshold 현실화
- `0a28a4c2` docs: CHANGELOG 정밀 분석 결과 추가
- `862ff64b` fix: 전체 코드 정밀 분석 — CRITICAL 5개 + HIGH 15개 버그 수정
- `9ea9c752` fix: 러너 트레일링 best_price 초기값 + trail_distance cap 개선
- `98f87a05` fix: 매매 차단 게이트 전면 완화 — 폭락장 무매매 사태 대응

## 2026-04-10
- `ad9036bb` fix: 스캘핑 쿨다운 미작동 + 연속 SL 재진입 방지
- `1b2efeda` fix: 포지션 종료 후 잔존 트리거 주문 취소 누락 버그 수정
- `a57cdbbd` fix: ML/시그널/SL 전면 개편 — 승률 55% 목표
- `08974183` feat: 봇 시작 시 자동매매 ON + 모드 + 잔고 텔레그램 알림
- `bea2da34` config: active_model scalp -> both (swing + scalp 동시)
- `c68df1c8` config: autotrading default OFF -> ON
- `600c149b` style: /help 명령어 이모지 알림과 일치시킴
- `0bfc0521` feat: /며니 히든 이스터에그
- `7c35ea5b` style: 텔레그램 알림 이모지 + 텍스트 세련되게 정리
- `21b2fb8d` style: /help 이모지 + 대문자 정리
- `86647bbc` revert: 자동매매 ON/OFF 이모지 원복 (🟢🔴)
- `46938b7e` fix: /help 한글 깨짐 — 영문으로 변경 (유니코드 인코딩 문제 회피)
- `666ee927` fix: 텔레그램 이모지 호환성 — 🟢🔴 → ✅❌ (4자리 유니코드)
- `3a5663b6` feat(telegram): /clear 명령 — 좀비 포지션 강제 정리
- `bbe29e65` feat(telegram): 양방향 명령어 — /on /off /status /balance /close /help
- `9662c7fd` fix(critical): close_attempts 10회 cap이 무한루프 못 막던 버그
- `5390ad61` fix(critical): ML train() numpy inhomogeneous 에러 — position_check 죽이는 root cause
- `5ba7b6f7` fix(critical): 포지션 없는데 CLOSE 무한루프 + REGIMES import 에러

## 2026-04-09
- `0d62fe78` feat(ops): 페이퍼 매매도 trades.jsonl 에 기록 — Claude 직접 분석 가능
- `38d0f87b` feat(strategy): 옵션 B + Explosive Quick Mode — 박스권에서도 변동성 폭발 시 진입
- `0da80e8b` fix(signal): anti_chop 필터 OR 조건으로 강화 — 박스권 미감지 fix
- `4f450095` fix(critical): exit_price OKX fetch — trades.jsonl 부정확 데이터 root cause
- `4d4c449c` fix(strategy+ops): ranging 차단 + 매물대 SL 강화 + 거래 이력 logs 브랜치 자동 push
- `125668e8` feat(notify): 텔레그램 알림 5개 추가 — TP1/본절/러너 + 손실임박 + 연패 + 레짐 + 일일리포트
- `88b95980` fix(sync): 거래소에 없는 옛 Redis pos:active:* 자동 정리
- `84f25a74` feat(safety): 추가 권장 3개 — 외부 모니터링 + 영구 거래로그 + 알고 정리 visibility
- `809fd569` fix(critical): 어제 14h 봇 죽음 사고 — DB 자동복구 + 알고 정리 + graceful shutdown
- `4f9e72fa` fix(critical): SQLite WAL 모드 롤백 — docker volume 손상 원인

## 2026-04-08
- `985fa6df` fix(trail): trail_min_price_pct 0.2 → 0.5 (옵션 A) + 진화 backlog 주석
- `2c71f651` fix(critical): ccxt OKX amount BTC→contracts 단위 변환 (실거래 -90% 사고 후 진짜 root cause)
- `6f748f86` feat(notify): 자동매매 ON/OFF 토글 시 텔레그램 알림 추가
- `51b40361` fix(safety): 20-pass 분석 — 4개 critical/high 버그
- `88f5c200` fix(scalp): max_possible 41.5 → 15.0 — 정규화 분모 현실화
- `55293ce5` fix(scalp): entry_threshold cap 강화 — 옛 pkl 3.45 → 2.5
- `f1e25507` fix(scalp): entry_threshold 점수 분포에 맞춰 조정 + 디버깅 로그 강화
- `d3681812` fix(scalp): scalp 평가 결과를 INFO 로그로 노출 (디버깅 가능하게)
- `abdc2eaa` fix(signal): BUG #4 회귀 — REALISTIC_MAX_SCORE 18→12 복원
- `5d106be9` docs: CHANGELOG 에 04-08 정밀 분석 + 6개 버그 수정 내역 추가
- `98daa0ad` chore: SQLite WAL + WS unknown channel 로깅 + ml_model.py 삭제
- `ec4b5fd5` fix(signal): aggregator 점수 인플레이션 보정 (BUG #4)
- `687bfdaf` fix(signal): CVD 1봉 lag 제거 + BOS overshoot 보정 + scalp 초기 지연 단축
- `d0f44257` fix(safety): Redis fallback + WS JSON 보호 + heartbeat timeout
- `8edbe5aa` chore: 셸 스크립트에 실행권한(+x) 부여
- `6daef856` docs(ops): COMMIT_LOG 자동 갱신 인프라 + 04-08 드리프트 정리
- `32f495d7` fix(monitoring): 잔고를 Redis sys:balance 캐시로 이전
- `fbb69851` feat: SL/TP 둘 다 마진 손익% 기준 + 사용자 수동 수정 + 스캘핑 중점
- `361604ff` feat(sizing): margin_loss_cap 모드 — 마진 손실 % 한도 기반 SL + 사이즈
- `af1b0f69` fix(data): 캔들 조회 1회 재시도 + 에러 본문 명확히 로깅
- `baae27b2` fix: 20-pass 정밀 분석 — 8개 critical 버그 + self-heal + sync_positions 구현
- `6ca4b4ee` fix: 5+회 정밀 분석 — 8개 critical/high 버그 + 통합 정리
- `4b64866f` fix: 정밀 검수 — 8개 추가 버그 수정 + 소형 포지션 케이스 처리
- `cb1758a0` feat(safety+sched): 학습 중 폴링 5초 단축 + 스케줄 조용한 시간대로 이동
- `883abff0` feat(ops+safety): 헬스체크 + 학습-매매 격리
- `097143fc` feat(ops): 클라우드 로그 자동 디지스트 + GitHub logs 브랜치 푸시
- `349ac218` fix(trading): 러너 모드 정밀 검수 — 7개 critical 버그 수정
- `95192d3e` feat(trading): TP2/TP3 → 러너 트레일링 (옵션 A) 로 전환
- `48320984` fix(trading): 진입 시 SL+TP1/TP2/TP3 서버사이드 등록 + 반익본절 SL 자동 이동

## 2026-04-07
- `72e68f13` ML 버퍼 크기 확장: 10000 → 50000 (~1주일치 데이터)
- `e0666d7f` ui: ML 모델 카드 메인을 OOS 정확도(신뢰도)로 변경, 학습건수도 함께 표시
- `f59dba84` ui: ML 모델 카드에 trade_count → buffer_size 표시 (실제 학습 데이터)
- `a8129cd3` 스캘핑 반응속도 향상
- `541e068d` fix: SignalTracker 저장 빈도 100→10건, 대시보드 10초 캐시
- `2fdc94d0` fix: ML scaler 피처 차원 실제 검증 (키 비교만으로 부족)
- `fa59099f` fix: ML 피처 개수 불일치 (37 → 67) 처리
- `4bb54a35` docs: CHANGELOG에 최근 작업 누락분 추가
- `11d7558b` 1주일 운영 최종 안정성 강화 (★★★ 5건)
- `8afff553` 1주일 운영 안정성 보강 — 6개 항목
- `4a7723de` ui: System Status 카드 데이터 채우는 JS 추가 (5초 갱신)
- `88f6bee4` ui: 툴팁 위→아래로 변경, 카드 헤더 overflow visible 보장
- `b5e250a0` ui: 물음표 아이콘 항상 제목 바로 옆에 붙도록 flex div로 감쌈
- `506adc21` 대시보드 도움말 툴팁 추가 (각 카드에 ? 아이콘)
- `7318b18e` 대시보드 UI 재구성 + 직관적 명칭 변경
- `62e173ff` 대시보드 누락 UI 4개 추가 + 시그널 이름 정리
- `ada3a1a0` SignalTracker: 시그널별 기여도 추적 시스템
- `e7b9d4e5` fix: 전체 코드베이스 ★ 우선순위 10개 정리
- `0b538ad5` remove: 111.png 스크린샷 제거 + .gitignore 추가
- `db74f771` fix: 전체 코드베이스 버그 점검 후 11개 치명/주요 버그 수정
- `5e0da273` docs: ScalpEngine v4 + 클라우드 배포 변경 이력 추가
- `efcaf545` ScalpEngine v4: 전체 점검 후 14개 버그/로직 수정
- `16cd8496` fix: pkl 손상 방지 + sklearn 버전 고정
- `a0923d36` docs: 문서화 항목 추가
- `dd19bb86` 변경 이력 추가 (CHANGELOG.md)
- `a4f5b344` 운영 매뉴얼 추가 (MANUAL.md)
- `affcc4d3` fix: dashboard.py 함수 순서 + Redis 호스트 환경변수 지원
- `fe1fedf6` 대시보드 HTTP Basic Auth 추가 (서버 배포 보안)
- `24f9fdb8` MetaLearner: ML 자가 업그레이드 시스템
- `fe9f57ed` 실거래 리스크 + 뉴스 필터 + 백테스트 검증 + 스캘핑 강화
- `ccf0c29f` fix: 최근거래 실매매만 표시 + ML 재학습 간격 안정화
- `0a534873` fix: 대시보드 무한 로딩 해결 — 별도 스레드 + 중복 루프 제거 + I/O 최적화
- `d18b8366` 대시보드 한/영 언어 전환 기능 추가
- `e5972f37` 스캘핑 전용 루프(15초) + 리스크 관리 + 대시보드
- `ff6d1ad9` ScalpEngine v3: SMC(OB/유동성스윕/FVG) + 세션필터 + 트레일링 + 안티첩
- `f3c8d507` ScalpEngine v2: 급변동 스캘핑 모드 추가
- `f43509eb` fix: Scalp 임계값 상한 5.0/하한 2.5로 조정 (하루 5건+ 거래 보장)
- `7e5bd2e4` fix: Scalp ML 임계값 상한 제한 (7.95→6.0 상한)
- `acd91457` ML v2 대규모 업그레이드: 레짐별 앙상블 + 가상매매 + 프랙탈 + 학습 스케줄러

## 2026-04-06
- `ff476dfe` main.py v2: 듀얼 모델 + AdaptiveML 실전 통합 + 전체 개선
- `cbd7a02c` 듀얼 모델(Swing/Scalp) + AdaptiveML + 웹 대시보드 대규모 업데이트
- `3016fa34` Phase 5 완료: 텔레그램 알림 + 대시보드 + 트레이드 로거
- `70ea2239` Phase 4 완료: 백테스트 시뮬레이터 + 성과 리포트
- `87344587` Phase 3 완료: 매매 엔진 + 동적 레버리지 + 리스크 관리
- `0b95bed2` Phase 2 완료: 시그널 합산기 + 등급 판정 + ML 엔진
- `2339be5b` Phase 1 완료: 데이터 수집 + 14개 기법 엔진 구현

## 2026-04-03
- `cf424c3e` 초기 프로젝트 세팅: 명세서 + CLAUDE.md

