# OKX CryptoAnalyzer v2.0

## 프로젝트 개요
- BTC 무기한 선물(OKX) 자동매매 시스템
- **모멘텀 스캘핑**: CandidateDetector 3종(Momentum/Breakout/Cascade) + ML Meta-Label Go/NoGo
- 설계서: `SPEC_V2.md` (2026-04-28 전면 재설계)
- 변경 인덱스: `COMMIT_LOG.md` (자동 갱신, 매 커밋 후)
- 사람용 변경 큐레이션: `CHANGELOG.md`
- 운영 매뉴얼: `MANUAL.md`

## 핵심 설계 결정 (SPEC v2, 2026-05-07 현행)
- **3경로**: Shadow(시장관찰) + PaperLab(A/B실험) + 실거래(검증)
- 시그널: 3종 후보(Momentum/Breakout/Cascade) → ML Go/NoGo → 확신도 점수
- **확신도 사이즈**: 0~5점 → 0%~100% (이진 차단 대신 비율 사이즈)
- **AdaptiveParams**: TP/SL/방향 자동 보정 (10건+ 활성, 매 거래 갱신)
- **SL/TP**: SL 마진 -5% (보정 가능) / TP1 ATR기반 (보정 가능, RR≥1.3)
- **주문**: Maker 강제 (post-only), strength≥1.5만 taker, 실패→포기
- **보호 주문**: SL market-on-trigger + TP limit-on-trigger + sl_failsafe

## 기술스택
- Python 3.11 / ccxt / scikit-learn / FastAPI
- DB: SQLite(캔들) + Redis(실시간)
- 알림: Telegram

## 현재 상태 (2026-05-07)
- 실거래 운영 중 (잔고 ~$315, Phase A 데이터 수집)
- Shadow: 전 시그널 추적 + reach%/mae% 연속값 수집
- PaperLab: 3 Variant A/B 테스트 (tight/base/wide)
- AdaptiveParams: TP/SL 보정 10건+ 활성, 매 거래 자동 갱신
- ML: Phase A (labeled ~28건), Phase B 100건 도달 시 전환
- 서버: Vultr Singapore, Docker Compose

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
