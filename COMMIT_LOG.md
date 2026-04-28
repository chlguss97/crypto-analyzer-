# Commit Log

> 자동 생성 — `scripts/update_commit_log.sh` (매 커밋 후 실행)
> Updated: 2026-04-29 08:40:15    
> Total commits: 250 (2026-04-03 → 2026-04-29)

Claude 가 질문/변경 작업 시 이 파일을 참고해서 과거 변경 이력 컨텍스트를 확보합니다. 수동 편집 금지 — 다음 커밋 시 덮어써집니다.

## 2026-04-29
- `796b0c9` fix: 보류 6건 전부 수정 — 페이퍼 실전 동기화 + ML 라벨 + Redis 복구
- `633f347` fix: 전수검사 CRITICAL 1건 + HIGH 4건 수정
- `2b8dc20` fix: 전수검사 CRITICAL 3건 + HIGH 1건 수정

## 2026-04-28
- `2aab4e2` docs: 전수검사 프롬프트 — 프로젝트 맞춤 수정
- `d79e607` fix: signals 테이블 JS 필드명 4건 수정 + dead doc 제거
- `73a25e2` refactor: 레거시 파일명 제거 — flow_engine→candidate_detector, flow_ml→ml_engine
- `65ab33c` fix: CRITICAL — PaperPosition 생성자 필수 필드 누락 (grade/score/entry_time)
- `18052e3` feat: ML Phase 전환 + 마일스톤 텔레그램 즉시 알림
- `fcf4dd9` feat: 마이크로스트럭처 15종 피처 — 프롭 트레이더의 눈
- `6387cb9` feat: 모든 봇 행동 JSONL 로그 — 후보감지+ML결정+shadow결과
- `745f97b` fix: 웹 대시보드 v2 완전 개편
- `e510e1c` fix: 프론트엔드 레거시 완전 제거
- `bbf3369` fix: 레거시 잔재 전수 정리 — dead code/중복 백필/옛 셋업명
- `5c3a699` fix: Adverse Selection load_config 매초 호출 → 1회 캐싱
- `12382cb` fix: 대시보드/텔레그램/페이퍼 v2 동기화
- `9b294f1` refactor: SPEC v2 전면 개편 — CandidateDetector + ML Meta-Label
- `adab444` feat: maker 강제 + ranging 차단 — 수수료/횡보 손실 근절

## 2026-04-27
- `7a33f80` feat: 모멘텀 게이트 — 떨어지는데 롱 치지 마
- `462a806` fix: 오늘 거래 분석 기반 4건 수정
- `155ae57` fix: 서버TP1 후 SL 재등록 + log_push 브랜치 복귀 실패 방지
- `9276773` fix: SL 최소값 0.35%→0.5% — 5m 노이즈 밖으로
- `0d2184a` fix: _append_jsonl import를 파일 상단으로 이동 (런타임 크래시 방지)
- `8cd5365` fix: 로깅 보완 5건 — 주간번호 통일 + 이벤트 이력 보존 + 상태 기록
- `534abf7` fix: log_push에 bot.log/trades.log/signals.log 주간 파일 포함
- `168214d` feat: 주간 로그 영구 보존 — 월요일 기준 파일 로테이션
- `cbfb27c` feat: 로깅 영구 보존 — bot.log 파일 핸들러 + exit JSONL 필드 보강
- `dabf8d3` fix: sl_failsafe 디버그를 JSONL에도 영구 기록 (Docker 로그 유실 대비)
- `2496d3f` fix: sl_failsafe 52% 문제 — SL 검증 대기 강화 + TP1 race condition 제거
- `3938015` fix: 전수검사 20건 버그 수정 — CRITICAL 3 + HIGH 12 + MEDIUM 5

## 2026-04-24
- `e17736e` feat: Signal Score — 마지막 시그널 30초 유지 표시
- `a5ae220` fix: 대시보드 전수검사 — structure→vol_band + API fallback + 중복호출 제거
- `d0078b8` feat: 일일 리포트 통합 + ML 마일스톤 알림
- `c5740e0` fix: 페이퍼 잔고 초기화 문제 — Redis TTL 제거 + heartbeat 갱신
- `16dbbf4` feat: 동적 트레일 SL — 이익비례 + 시간축소 + 정체감지 + R-lock
- `f66a6c6` fix: is_tp → is_limit_trigger 변수명 수정 (NameError)
- `136232d` feat: 러너 SL을 limit-on-trigger로 변경 (maker 수수료 0.02%)
- `76ceac4` fix: 지정가/시장가 로직 3건 수정
- `fa553fa` fix: CRITICAL — position_manager filled_size 단위 오류 (contracts→BTC)
- `65dcb41` fix: 텔레그램 셋업 감지 알림 제거 — 진입 시에만 알림
- `af8b4b8` feat: 실거래 ON — 페이퍼 병행 모드
- `c2d441f` fix: 전수검사 최종 — streak 쿨다운 KeyError 방지 + HTML 레거시 정리
- `2b9acf8` fix: 전수 검사 WRONG_DATA 6건 수정 (call-chain 추적)
- `86da013` fix: 뿌리 뽑기 — CRITICAL 4 + HIGH 7 + MEDIUM 4 수정
- `469890b` fix: 전체 버그 수정 12건 (CRITICAL 2 + HIGH 10)
- `df9e83c` fix: 대시보드 페이퍼 탭 데이터 매핑 수정
- `9599bd0` fix: PaperTrader 잔고 DB 복원 — 재시작 시 리셋 방지
- `232e295` feat: FlowEngine v2 — 6셋업 다중 진입 (15건/일 목표)

## 2026-04-23
- `dad13bc` fix: Flow x Regime 히트맵 A/B/C → FLOW 동적 + by_regime 추적 추가
- `11621c9` refactor: SetupTracker A/B/C → FLOW 단일 셋업으로 전환
- `8fda090` refactor: 레거시 코드 전면 제거 + 대시보드 버그 수정 + 페이퍼 탭 추가
- `ed7e555` refactor: PaperTrader v2 — 독립 가상 계좌 아키텍처 (실거래 OFF)

## 2026-04-17
- `f2c860b` chore: COMMIT_LOG 갱신
- `63cf350` fix: 서버 크래시 3건 수정 — OKX API + None 참조 + 1d 데이터 부족
- `89ea763` fix: OKX 알고 조회 ordType 파라미터 누락 — 51000 에러 수정
- `e27a41e` chore: COMMIT_LOG 갱신
- `ab1f4ce` refactor: 텔레그램 + 대시보드 FlowEngine 전면 패치
- `237babd` docs: CHANGELOG + 메모리 — FlowEngine v1 전체 기록
- `4393b31` chore: COMMIT_LOG 갱신
- `466abe1` fix: FlowEngine 검증 — CVD 노이즈 필터 + 1d 레벨 추가
- `c7cbe84` chore: COMMIT_LOG 갱신
- `a120ee7` refactor: 레거시 코드 정리 — FlowEngine 전용 체계 확립
- `d7ea699` chore: COMMIT_LOG 갱신
- `b5d24c8` feat: FlowML — FlowEngine 전용 경량 ML 접목
- `60980f1` chore: COMMIT_LOG 갱신
- `a312589` feat: FlowEngine v1 — 단순 오더플로우 엔진 (ABC 모델 대체)
- `1decdbc` chore: COMMIT_LOG 갱신
- `7ec5e08` fix: 점수 정합성 — 최소점수 체크 + 등급 동적 매핑
- `900148b` chore: COMMIT_LOG 갱신
- `25c3d11` fix: HTF 하드차단 → 편향 가감 전환 + 추세 돌파 감지
- `984a42e` chore: COMMIT_LOG 갱신
- `5e44d87` feat: HTF 큰 추세 필터 — 4h/1d/1w 캔들 + 역방향 차단
- `e068c5d` chore: COMMIT_LOG 갱신
- `a70c1b5` fix: Binance 통합 전수 검증 보완 3건
- `4297b36` chore: COMMIT_LOG 갱신
- `ab0e074` perf: 지연 0ms — 이벤트 드리븐 평가 + 청산 폭발 감지
- `db7818c` chore: COMMIT_LOG 갱신
- `2401673` perf: Binance WS 캔들 스트림 — REST 폴링 제거, 0ms 지연
- `87d7526` chore: COMMIT_LOG 갱신
- `22198a0` perf: 캔들/평가 주기 가속 — Binance rate limit 활용
- `e73cbce` chore: COMMIT_LOG 갱신
- `013607a` feat: 차트 분석 Binance 선물 기준 전환 — OKX는 실행만
- `262f7f0` chore: COMMIT_LOG 갱신
- `36559c4` feat: Binance WS 실시간 스트림 — 시장 전체 오더플로우
- `b3f9b05` docs: CHANGELOG 4/16~17 업데이트 (대시보드 개편, 알고 잔존 수정, 오더플로우)
- `71c659d` chore: COMMIT_LOG 갱신
- `a082a66` feat: 오더플로우 방향 확인 + 봇 스냅샷 + SL 안전장치
- `c21daa3` chore: COMMIT_LOG 갱신
- `444b2c7` fix: 4/16 거래 복기 문제점 3건 수정
- `06106a0` chore: COMMIT_LOG 갱신
- `ed74812` feat: 대시보드 전면 개편 — Engine 탭 + 레거시 ML 10개 엔드포인트 삭제

## 2026-04-16
- `8a43dd4` chore: COMMIT_LOG 갱신
- `3a397e5` fix: 전 주문 post-only limit 우선 — maker 수수료 0.02% (taker 대비 60% 절감)
- `9fdc77e` chore: COMMIT_LOG 갱신
- `d6e092b` fix: 미체결 알고 주문 잔존 근본 원인 6건 제거
- `a175cca` chore: COMMIT_LOG 갱신
- `25866d5` refactor: Signal Performance Ranking 섹션 제거
- `89a0fa8` chore: COMMIT_LOG 자동 갱신
- `4d580b6` fix: 대시보드 크래시 루프 근절 + /health 엔드포인트
- `1ca1095` fix: 대시보드 별도 Docker 컨테이너로 완전 분리
- `5dcceee` fix: 대시보드 모듈 로드 시 DB/Redis 인스턴스 생성 제거
- `429f330` feat: Setup A 전면 전환 — 모멘텀 추격 → 풀백 진입
- `3e548fe` fix: uvicorn 로그 활성화 (dashboard.log)
- `3165022` fix: 대시보드 lazy init — startup hang 근절
- `7c3234b` fix: 대시보드 startup 경량화 + 불필요 섹션 제거
- `77948fb` fix: 포지션 복원 시 알고 중복 등록 근절
- `ba8194c` fix: 포지션 종료 시 OKX 잔존 알고 전체 정리
- `db0be17` fix: 레버리지 설정 시 알고 충돌(59668) 자동 정리 후 재시도
- `3ef6c22` fix: 대시보드 별도 프로세스로 분리 (asyncio 루프 충돌 근절)
- `241ca11` fix: PAPER_UNIFIED → PAPER_SETUP_A/B/C 명칭 변경
- `67381bb` fix: Dockerfile config/ 명시적 COPY (BuildKit 캐시 누락 방지)
- `43466a1` fix: 셋업 ABC 보완 — 과완화 리스크 방지
- `22f96e2` feat: 셋업 ABC 조건 완화 — 하루 3건+ 매매 목표
- `31b497d` feat: 셋업 reject 이유 로깅 — 왜 진입 안 하는지 진단
- `0cf3680` config: 레버리지 15~20x (SL 0.4~0.53% + 사이즈 확보)
- `a19dea5` fix: margin_pct 0.30→0.50 (OKX 최소주문 0.01 BTC 미달 해결)

## 2026-04-15
- `311627c` fix: 로깅 빈 곳 4건 수정
- `f5a71b8` fix: 주문/포지션 CRITICAL 6건 + HIGH 6건 수정
- `98cc313` fix: 봇 재시작 시 포지션 없어도 stale 알고 주문 전체 정리
- `7463525` fix: /api/setup-tracker 에러 핸들링 추가
- `5077d70` fix: uvicorn 대시보드 스레드에 명시적 이벤트 루프 생성
- `5ee5290` fix: 대시보드 Redis/DB 이벤트 루프 충돌 수정
- `0c00cc6` fix: 대시보드 API fetch에 credentials 추가 (401 문제 해결)
- `142db0b` fix: Self-Improving AI → SetupTracker 교체 + 구 meta/backtest API 정리
- `9d935c7` feat: SetupTracker 자기개선 + 전수 버그 수정
- `6474c7e` fix: 대시보드 Swing/Scalp/ML 잔존 섹션 → TradeEngine으로 교체
- `e677d86` fix: ctx UnboundLocalError — 변수 스코프 수정
- `986bd15` feat: 텔레그램 명령어 확장 + 셋업 알림 + 리밋 오더
- `c616fea` feat: 대시보드 TradeEngine 전환 + 실거래 ON
- `3875eaf` fix: UnifiedEngine → TradeEngine 명칭 변경 + 충돌/데드코드 수정
- `2aa04e2` fix: settings.yaml timeframes 레거시 호환 복구 (candle_collector KeyError)
- `5be7de8` fix: settings.yaml 레거시 호환 필드 복구 (leverage.py KeyError)
- `605dee3` feat: 전면 개편 — Unified Engine v1 (셋업 ABC 통합 모델)
- `e131986` perf: 캔들 갱신 3초 + 스캘핑 평가 3초 + 포지션 체크 1초
- `9ba3f3b` feat: 오더블록 v3 — MSB+ChoCH+유동성+임펄스품질 전문가급 구현
- `e41ed33` feat: 시그널 구조 전면 개편 — 추세추종 중심 + 멀티TF 오더블록
- `e03fdcd` config: 레버리지 10~25x
- `0df9e05` config: 레버리지 5~25x + 포지션 체크 1초
- `7dddd8d` fix: 구조적 손실 근절 — SL/TP/레버리지 전면 개편
- `7bcff60` fix: 숏 편향 수정 + 급등락 $500-1000 감지 강화

## 2026-04-13
- `2889f5c` fix: LongShortRatioIndicator NoneType 비교 에러 수정
- `41a18b7` feat: 스캘핑 기법 강화 + ML 스캘핑 엔진 업그레이드
- `de273c0` feat: 해외 유명 스캘핑 기법 3개 추가 + scalp threshold 현실화
- `0a28a4c` docs: CHANGELOG 정밀 분석 결과 추가
- `862ff64` fix: 전체 코드 정밀 분석 — CRITICAL 5개 + HIGH 15개 버그 수정
- `9ea9c75` fix: 러너 트레일링 best_price 초기값 + trail_distance cap 개선
- `98f87a0` fix: 매매 차단 게이트 전면 완화 — 폭락장 무매매 사태 대응

## 2026-04-10
- `ad9036b` fix: 스캘핑 쿨다운 미작동 + 연속 SL 재진입 방지
- `1b2efed` fix: 포지션 종료 후 잔존 트리거 주문 취소 누락 버그 수정
- `a57cdbb` fix: ML/시그널/SL 전면 개편 — 승률 55% 목표
- `0897418` feat: 봇 시작 시 자동매매 ON + 모드 + 잔고 텔레그램 알림
- `bea2da3` config: active_model scalp -> both (swing + scalp 동시)
- `c68df1c` config: autotrading default OFF -> ON
- `600c149` style: /help 명령어 이모지 알림과 일치시킴
- `0bfc052` feat: /며니 히든 이스터에그
- `7c35ea5` style: 텔레그램 알림 이모지 + 텍스트 세련되게 정리
- `21b2fb8` style: /help 이모지 + 대문자 정리
- `86647bb` revert: 자동매매 ON/OFF 이모지 원복 (🟢🔴)
- `46938b7` fix: /help 한글 깨짐 — 영문으로 변경 (유니코드 인코딩 문제 회피)
- `666ee92` fix: 텔레그램 이모지 호환성 — 🟢🔴 → ✅❌ (4자리 유니코드)
- `3a5663b` feat(telegram): /clear 명령 — 좀비 포지션 강제 정리
- `bbe29e6` feat(telegram): 양방향 명령어 — /on /off /status /balance /close /help
- `9662c7f` fix(critical): close_attempts 10회 cap이 무한루프 못 막던 버그
- `5390ad6` fix(critical): ML train() numpy inhomogeneous 에러 — position_check 죽이는 root cause
- `5ba7b6f` fix(critical): 포지션 없는데 CLOSE 무한루프 + REGIMES import 에러

## 2026-04-09
- `0d62fe7` feat(ops): 페이퍼 매매도 trades.jsonl 에 기록 — Claude 직접 분석 가능
- `38d0f87` feat(strategy): 옵션 B + Explosive Quick Mode — 박스권에서도 변동성 폭발 시 진입
- `0da80e8` fix(signal): anti_chop 필터 OR 조건으로 강화 — 박스권 미감지 fix
- `4f45009` fix(critical): exit_price OKX fetch — trades.jsonl 부정확 데이터 root cause
- `4d4c449` fix(strategy+ops): ranging 차단 + 매물대 SL 강화 + 거래 이력 logs 브랜치 자동 push
- `125668e` feat(notify): 텔레그램 알림 5개 추가 — TP1/본절/러너 + 손실임박 + 연패 + 레짐 + 일일리포트
- `88b9598` fix(sync): 거래소에 없는 옛 Redis pos:active:* 자동 정리
- `84f25a7` feat(safety): 추가 권장 3개 — 외부 모니터링 + 영구 거래로그 + 알고 정리 visibility
- `809fd56` fix(critical): 어제 14h 봇 죽음 사고 — DB 자동복구 + 알고 정리 + graceful shutdown
- `4f9e72f` fix(critical): SQLite WAL 모드 롤백 — docker volume 손상 원인

## 2026-04-08
- `985fa6d` fix(trail): trail_min_price_pct 0.2 → 0.5 (옵션 A) + 진화 backlog 주석
- `2c71f65` fix(critical): ccxt OKX amount BTC→contracts 단위 변환 (실거래 -90% 사고 후 진짜 root cause)
- `6f748f8` feat(notify): 자동매매 ON/OFF 토글 시 텔레그램 알림 추가
- `51b4036` fix(safety): 20-pass 분석 — 4개 critical/high 버그
- `88f5c20` fix(scalp): max_possible 41.5 → 15.0 — 정규화 분모 현실화
- `55293ce` fix(scalp): entry_threshold cap 강화 — 옛 pkl 3.45 → 2.5
- `f1e2550` fix(scalp): entry_threshold 점수 분포에 맞춰 조정 + 디버깅 로그 강화
- `d368181` fix(scalp): scalp 평가 결과를 INFO 로그로 노출 (디버깅 가능하게)
- `abdc2ea` fix(signal): BUG #4 회귀 — REALISTIC_MAX_SCORE 18→12 복원
- `5d106be` docs: CHANGELOG 에 04-08 정밀 분석 + 6개 버그 수정 내역 추가
- `98daa0a` chore: SQLite WAL + WS unknown channel 로깅 + ml_model.py 삭제
- `ec4b5fd` fix(signal): aggregator 점수 인플레이션 보정 (BUG #4)
- `687bfda` fix(signal): CVD 1봉 lag 제거 + BOS overshoot 보정 + scalp 초기 지연 단축
- `d0f4425` fix(safety): Redis fallback + WS JSON 보호 + heartbeat timeout
- `8edbe5a` chore: 셸 스크립트에 실행권한(+x) 부여
- `6daef85` docs(ops): COMMIT_LOG 자동 갱신 인프라 + 04-08 드리프트 정리
- `32f495d` fix(monitoring): 잔고를 Redis sys:balance 캐시로 이전
- `fbb6985` feat: SL/TP 둘 다 마진 손익% 기준 + 사용자 수동 수정 + 스캘핑 중점
- `361604f` feat(sizing): margin_loss_cap 모드 — 마진 손실 % 한도 기반 SL + 사이즈
- `af1b0f6` fix(data): 캔들 조회 1회 재시도 + 에러 본문 명확히 로깅
- `baae27b` fix: 20-pass 정밀 분석 — 8개 critical 버그 + self-heal + sync_positions 구현
- `6ca4b4e` fix: 5+회 정밀 분석 — 8개 critical/high 버그 + 통합 정리
- `4b64866` fix: 정밀 검수 — 8개 추가 버그 수정 + 소형 포지션 케이스 처리
- `cb1758a` feat(safety+sched): 학습 중 폴링 5초 단축 + 스케줄 조용한 시간대로 이동
- `883abff` feat(ops+safety): 헬스체크 + 학습-매매 격리
- `097143f` feat(ops): 클라우드 로그 자동 디지스트 + GitHub logs 브랜치 푸시
- `349ac21` fix(trading): 러너 모드 정밀 검수 — 7개 critical 버그 수정
- `95192d3` feat(trading): TP2/TP3 → 러너 트레일링 (옵션 A) 로 전환
- `4832098` fix(trading): 진입 시 SL+TP1/TP2/TP3 서버사이드 등록 + 반익본절 SL 자동 이동

## 2026-04-07
- `72e68f1` ML 버퍼 크기 확장: 10000 → 50000 (~1주일치 데이터)
- `e0666d7` ui: ML 모델 카드 메인을 OOS 정확도(신뢰도)로 변경, 학습건수도 함께 표시
- `f59dba8` ui: ML 모델 카드에 trade_count → buffer_size 표시 (실제 학습 데이터)
- `a8129cd` 스캘핑 반응속도 향상
- `541e068` fix: SignalTracker 저장 빈도 100→10건, 대시보드 10초 캐시
- `2fdc94d` fix: ML scaler 피처 차원 실제 검증 (키 비교만으로 부족)
- `fa59099` fix: ML 피처 개수 불일치 (37 → 67) 처리
- `4bb54a3` docs: CHANGELOG에 최근 작업 누락분 추가
- `11d7558` 1주일 운영 최종 안정성 강화 (★★★ 5건)
- `8afff55` 1주일 운영 안정성 보강 — 6개 항목
- `4a7723d` ui: System Status 카드 데이터 채우는 JS 추가 (5초 갱신)
- `88f6bee` ui: 툴팁 위→아래로 변경, 카드 헤더 overflow visible 보장
- `b5e250a` ui: 물음표 아이콘 항상 제목 바로 옆에 붙도록 flex div로 감쌈
- `506adc2` 대시보드 도움말 툴팁 추가 (각 카드에 ? 아이콘)
- `7318b18` 대시보드 UI 재구성 + 직관적 명칭 변경
- `62e173f` 대시보드 누락 UI 4개 추가 + 시그널 이름 정리
- `ada3a1a` SignalTracker: 시그널별 기여도 추적 시스템
- `e7b9d4e` fix: 전체 코드베이스 ★ 우선순위 10개 정리
- `0b538ad` remove: 111.png 스크린샷 제거 + .gitignore 추가
- `db74f77` fix: 전체 코드베이스 버그 점검 후 11개 치명/주요 버그 수정
- `5e0da27` docs: ScalpEngine v4 + 클라우드 배포 변경 이력 추가
- `efcaf54` ScalpEngine v4: 전체 점검 후 14개 버그/로직 수정
- `16cd849` fix: pkl 손상 방지 + sklearn 버전 고정
- `a0923d3` docs: 문서화 항목 추가
- `dd19bb8` 변경 이력 추가 (CHANGELOG.md)
- `a4f5b34` 운영 매뉴얼 추가 (MANUAL.md)
- `affcc4d` fix: dashboard.py 함수 순서 + Redis 호스트 환경변수 지원
- `fe1fedf` 대시보드 HTTP Basic Auth 추가 (서버 배포 보안)
- `24f9fdb` MetaLearner: ML 자가 업그레이드 시스템
- `fe9f57e` 실거래 리스크 + 뉴스 필터 + 백테스트 검증 + 스캘핑 강화
- `ccf0c29` fix: 최근거래 실매매만 표시 + ML 재학습 간격 안정화
- `0a53487` fix: 대시보드 무한 로딩 해결 — 별도 스레드 + 중복 루프 제거 + I/O 최적화
- `d18b836` 대시보드 한/영 언어 전환 기능 추가
- `e5972f3` 스캘핑 전용 루프(15초) + 리스크 관리 + 대시보드
- `ff6d1ad` ScalpEngine v3: SMC(OB/유동성스윕/FVG) + 세션필터 + 트레일링 + 안티첩
- `f3c8d50` ScalpEngine v2: 급변동 스캘핑 모드 추가
- `f43509e` fix: Scalp 임계값 상한 5.0/하한 2.5로 조정 (하루 5건+ 거래 보장)
- `7e5bd2e` fix: Scalp ML 임계값 상한 제한 (7.95→6.0 상한)
- `acd9145` ML v2 대규모 업그레이드: 레짐별 앙상블 + 가상매매 + 프랙탈 + 학습 스케줄러

## 2026-04-06
- `ff476df` main.py v2: 듀얼 모델 + AdaptiveML 실전 통합 + 전체 개선
- `cbd7a02` 듀얼 모델(Swing/Scalp) + AdaptiveML + 웹 대시보드 대규모 업데이트
- `3016fa3` Phase 5 완료: 텔레그램 알림 + 대시보드 + 트레이드 로거
- `70ea223` Phase 4 완료: 백테스트 시뮬레이터 + 성과 리포트
- `8734458` Phase 3 완료: 매매 엔진 + 동적 레버리지 + 리스크 관리
- `0b95bed` Phase 2 완료: 시그널 합산기 + 등급 판정 + ML 엔진
- `2339be5` Phase 1 완료: 데이터 수집 + 14개 기법 엔진 구현

## 2026-04-03
- `cf424c3` 초기 프로젝트 세팅: 명세서 + CLAUDE.md

