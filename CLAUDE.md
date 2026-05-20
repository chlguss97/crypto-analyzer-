# ScalpEngine v3

## 프로젝트 개요
- BTC 무기한 선물(OKX) 자동매매 시스템
- **마이크로스트럭처 스캘핑**: 4계층 파이프라인 (Raw → Feature → Regime → ML)
- 설계서: `SPEC_V2.md` (2026-05-20 v3 전면 재설계)
- 변경 인덱스: `COMMIT_LOG.md` (자동 갱신, 매 커밋 후)
- 사람용 변경 큐레이션: `CHANGELOG.md`
- 운영 매뉴얼: `MANUAL.md`

## 핵심 설계 결정 (v3, 2026-05-20)
- **4계층**: Raw Data → Feature Engine → Regime Gate → ML Scorer → Execute
- **Regime Gate**: Hurst(추세/횡보/랜덤) + VPIN(독성) → 자동 거래허용 판단
- **시그널 2종**: Micro-Momentum Burst (추세장) / VWAP Snap (횡보장)
- **TP/SL**: TP +0.20% (maker) / SL -0.15% (market) / 시간정지 3~5분
- **ML**: 20종 마이크로스트럭처 피처 → XGBoost (Phase A 규칙 → B ML)
- **주문**: post-only maker 강제, 실패→포기 (market 전환 없음)
- **보호**: SL market-on-trigger + 5초 self-heal + 3회 소실 강제청산

## 기술스택
- Python 3.11 / ccxt / scikit-learn / FastAPI
- DB: SQLite(scalp.db: candles + scalp_signals + scalp_trades) + Redis(실시간)
- 알림: Telegram

## 현재 상태 (2026-05-20)
- **Shadow 모드** (실거래 없음, 데이터 수집 중)
- 마이크로스트럭처 15종 + OFI 활성
- Hurst/Parkinson: 5분봉 20개 축적 후 자동 계산
- ML Phase A (labeled 0건, 300건 도달 시 Phase B)
- 잔고: ~$227
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
