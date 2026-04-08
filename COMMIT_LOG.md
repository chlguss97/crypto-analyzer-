# Commit Log

> 자동 생성 — `scripts/update_commit_log.sh` (매 커밋 후 실행)
> Updated: 2026-04-08 13:56:05    
> Total commits: 60 (2026-04-03 → 2026-04-08)

Claude 가 질문/변경 작업 시 이 파일을 참고해서 과거 변경 이력 컨텍스트를 확보합니다. 수동 편집 금지 — 다음 커밋 시 덮어써집니다.

## 2026-04-08
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

