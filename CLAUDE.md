# OKX CryptoAnalyzer v2.0

## 프로젝트 개요
- BTC 무기한 선물(OKX) 자동매매 시스템
- **모멘텀 스캘핑**: CandidateDetector 3종(Momentum/Breakout/Cascade) + ML Meta-Label Go/NoGo
- 설계서: `SPEC_V2.md` (2026-04-28 전면 재설계)
- 변경 인덱스: `COMMIT_LOG.md` (자동 갱신, 매 커밋 후)
- 사람용 변경 큐레이션: `CHANGELOG.md`
- 운영 매뉴얼: `MANUAL.md`

## 핵심 설계 결정 (SPEC v2, 2026-04-28)
- 매매 스타일: 모멘텀 스캘핑 (역추세 폐기, 추세 추종만)
- 시그널: 3종 후보 감지 → ML Go/NoGo → 실행
- 데이터: 100% Binance (차트/CVD/청산), 매매만 OKX
- 타임프레임: 5m(실행) / 15m(확인) / 1h(필터)
- 레버리지: 15~20배 동적
- **SL/TP**: 마진 -5% / +10% (RR 2.0)
- **주문**: Maker 강제 (post-only), 강한 시그널만 taker 허용
  - 옛 모드 `risk_per_trade` (큰 계좌용) 보존
- 시그널 처리: Fast Path(실시간) + Slow Path(주기) 2단계
- 시간 청산: 단계별 + 러너 모드는 8시간 hard limit
- 수수료 필터: 최소 기대수익 0.15% 이상만 진입
- **보호 주문**: 진입 시 OKX에 SL+TP1/TP2/TP3 일괄 등록 + 반익본절 + 러너 트레일링 (옵션 A)
- **학습-매매 격리**: 학습 중(`sys:learning=1`) 신규 진입 차단, 활성 포지션은 5초 폴링 가속

## 기술스택
- Python 3.11 / ccxt / scikit-learn / FastAPI
- DB: SQLite(캔들) + Redis(실시간)
- 알림: Telegram

## 개발 로드맵
- Phase 1: 데이터 수집 + 기법 엔진 (14개, Fast/Slow 분리)
- Phase 2: ML + 시그널 합산 + 컨플루언스 보너스
- Phase 3: 매매 엔진 + 동적 레버리지 + 리스크 관리
- Phase 4: 백테스트 (콤보 셋업별 검증)
- Phase 5: 모니터링 + 테스트넷 → 실전

## 현재 진행 상황
- 명세서 작성 완료 (2026-04-03)
- GitHub 연동 완료: https://github.com/chlguss97/crypto-analyzer-.git
- Phase 1~5 완료 (2026-04-06): 데이터/14기법/ML/매매엔진/백테스트/모니터링
- ML v2 업그레이드 (2026-04-07): 레짐별 앙상블, 프랙탈, 가상매매, 역사백필, 학습 스케줄러
- 클라우드 배포 (2026-04-07): Vultr Singapore, Docker Compose, 헬스체크/디지스트 자동화
- **실거래 운영 + 사고 대응 (2026-04-08)**:
  - 실거래 -90% 강제청산 사고 → 보호 주문 파이프라인 전면 재구성
  - SL+TP1/TP2/TP3 OKX 서버사이드 등록 + 반익본절 + 러너 트레일링 (옵션 A)
  - leverage 마진 공식 critical 버그 수정 (사고 근본 원인)
  - `margin_loss_cap` 사이즈 모드 신설 → 작은 계좌($30) 진입 가능
  - 사용자 수동 SL/TP 변경 (대시보드/API)
  - 학습-매매 격리, 좀비 방지 self_heal/sync_positions
  - 스캘핑 중점 모드 default
- **운영 인프라 정착 (2026-04-08)**:
  - `COMMIT_LOG.md` 자동 갱신 (post-commit hook)
  - CHANGELOG/MANUAL/명세서 드리프트 정리
- 다음 단계: 실거래 안정성 검증 → 점진적 운용 규모 확대

## 메모리 자동 저장
- 대화 중 아래 항목이 발생하면 반드시 메모리에 자동 저장할 것:
  - 새 파일 생성 또는 기존 파일 대규모 수정
  - 아키텍처/설계 결정 변경
  - ML 모델 구조 변경
  - 사용자 피드백 (선호/비선호)
  - 버그 원인과 해결 방법
- 대화 종료 전에 변경사항이 있으면 메모리 업데이트 확인

## Git
- 원격: https://github.com/chlguss97/crypto-analyzer-.git (private)
- 다른 PC에서 작업 시: git clone 후 이어서 진행
- 동기화: git add . && git commit -m "메시지" && git push / git pull
