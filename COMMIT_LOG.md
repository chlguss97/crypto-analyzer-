# Commit Log

> 자동 생성 — `scripts/update_commit_log.sh` (매 커밋 후 실행)
> Updated: 2026-04-17 10:02:56    
> Total commits: 168 (2026-04-03 → 2026-04-17)

Claude 가 질문/변경 작업 시 이 파일을 참고해서 과거 변경 이력 컨텍스트를 확보합니다. 수동 편집 금지 — 다음 커밋 시 덮어써집니다.

## 2026-04-17
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

