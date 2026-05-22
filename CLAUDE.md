# GridBot v3 — Leading Regime Detection

## 프로젝트 개요
- BTC 무기한 선물(OKX) 자동매매 시스템
- **현재 전략: ATR-Adaptive Grid + 선행 레짐 감지** (2026-05-22~)
- 설계서: `SPEC_V3.md`
- 전수검사: `AUDIT_V3.md`
- 변경 인덱스: `COMMIT_LOG.md`
- 사람용 변경 큐레이션: `CHANGELOG.md`
- 운영 매뉴얼: `MANUAL.md`

## 현재 활성 전략: Grid + Leading Regime (2026-05-22)
- **2모드**: ACTIVE (그리드 가동) / PAUSED (추세 감지 → 정지)
- **선행 감지**: OBI + CVD가속 + Volume스파이크 + CUSUM → CRS
- **그리드**: ATR-adaptive, 기하식 spacing, 잔고 비례 사이징
- **레벨**: 자본에 따라 자동 (2~10개), 목표 레버리지 8x
- **spacing**: ATR% × 0.6, clamp [0.15%~0.50%]
- **주문**: 전부 post-only limit (maker 0.02%)
- **안전장치**: 선행레짐정지 + 서킷브레이커(2%/10초) + 거래소백스탑(-20%)

## 기술스택
- Python 3.11 / ccxt / FastAPI
- DB: SQLite(scalp.db: candles + grid_trades) + Redis
- 알림: Telegram

## 현재 상태 (2026-05-22)
- **Phase 0**: 레거시 삭제 진행 중
- **Phase 1**: regime_detector.py 작성 예정
- **Phase 2**: grid_engine.py 리팩터 예정
- 잔고: ~$180
- 서버: Vultr Singapore, Docker Compose

## 메모리 자동 저장
- 대화 중 아래 항목이 발생하면 반드시 메모리에 자동 저장할 것:
  - 새 파일 생성 또는 기존 파일 대규모 수정
  - 아키텍처/설계 결정 변경
  - 사용자 피드백 (선호/비선호)
  - 버그 원인과 해결 방법
- 대화 종료 전에 변경사항이 있으면 메모리 업데이트 확인

## Git
- 원격: https://github.com/chlguss97/crypto-analyzer-.git (private)
- 다른 PC에서 작업 시: git clone 후 이어서 진행
- 동기화: git add . && git commit -m "메시지" && git push / git pull
