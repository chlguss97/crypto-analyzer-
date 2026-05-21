# ScalpEngine v3 + Grid Trading

## 프로젝트 개요
- BTC 무기한 선물(OKX) 자동매매 시스템
- **현재 전략: ATR-Adaptive Grid Trading** (2026-05-21~)
- 이전 전략: 마이크로스트럭처 스캘핑 (비활성화)
- 설계서: `SPEC_V2.md`
- 변경 인덱스: `COMMIT_LOG.md` (자동 갱신, 매 커밋 후)
- 사람용 변경 큐레이션: `CHANGELOG.md`
- 운영 매뉴얼: `MANUAL.md`

## 현재 활성 전략: Grid Trading (2026-05-21)
- **ATR-Adaptive Grid**: 4레벨 (2 buy + 2 sell), 각 0.01 BTC
- **Spacing**: ATR% × 0.6, clamp(0.10%~0.50%)
- **주문**: 전부 limit post-only (maker 0.02%)
- **사이클**: 체결 → counter-order(TP) → 완성 → 재배치
- **리밸런스**: 1시간마다 ATR 재계산, drift > 50% → 재구성
- **안전장치**: Hurst > 0.7 → 일시정지, BOT_KILL -20% DD
- **수익**: 건당 ~$0.47 (spacing $78 - 수수료 $0.31)

## 비활성 전략: 4모델 앙상블 스캘핑 (archived)
- scalp.enabled: false
- OFI + OU + CVD + LSTM(tanh fallback) 4모델 앙상블
- conf = max(0, 1 - σ/|d̄|), threshold 0.8
- 스캘핑 수수료 문제로 그리드로 전환 (2026-05-21)

## 기술스택
- Python 3.11 / ccxt / PyTorch / FastAPI
- DB: SQLite(scalp.db: candles + scalp_signals + scalp_trades + grid_trades) + Redis
- 알림: Telegram

## 현재 상태 (2026-05-21)
- **Grid LIVE**: 4레벨 배치 중, spacing ~0.10%
- 스캘핑: 비활성화
- LSTM: 미학습 (LOB 데이터 수집 중, Phase 3 대기)
- 잔고: ~$170
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
