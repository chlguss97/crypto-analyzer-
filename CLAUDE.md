# GridBot v4 — Minimal Grid (Pionex 철학)

## 프로젝트 개요
- BTC 무기한 선물(OKX) 자동매매 시스템
- **현재 전략: Minimal Grid — 멈추지 않는 봇** (2026-05-22~)
- 설계서: `SPEC_V4.md`
- 변경 인덱스: `COMMIT_LOG.md`
- 운영 매뉴얼: `MANUAL.md`

## 설계 원칙
1. **절대 멈추지 않는다** (가동률 = 수익)
2. **파라미터 시작 시 1회 설정, 실행 중 불변** (Pionex 철학)
3. **레짐 감지 없음** (오탐 비용 > 방어 이익)
4. **추세는 그리드 구조가 자체 흡수**
5. **DGT 리빌드**: 경계 돌파 시 즉시 center 재설정 (Chen et al. 2025)

## 현재 전략
- **레버리지**: 10x 고정 (Neutral 그리드, 순노출≈0)
- **spacing**: ATR(14,5m) × 0.6, clamp [0.15%~0.50%], 기하식
- **레벨**: 잔고 비례 자동 ($500→4, $1000→8)
- **주문**: 전부 post-only limit (maker 0.02%)
- **체결 감지**: OKX Private WS push (10~50ms) + REST 30초 fallback
- **안전장치**: 서킷브레이커(2%/10초) + BOT_KILL(-20%)
- **리빌드**: DGT — 가격이 그리드 경계 돌파 시 즉시

## 기술스택
- Python 3.11 / ccxt / FastAPI
- DB: SQLite(scalp.db: candles + grid_trades) + Redis
- 알림: Telegram
- 서버: Vultr Singapore, Docker Compose

## 메모리 자동 저장
- 새 파일 생성 또는 기존 파일 대규모 수정
- 아키텍처/설계 결정 변경
- 사용자 피드백 (선호/비선호)
- 버그 원인과 해결 방법

## Git
- 원격: https://github.com/chlguss97/crypto-analyzer-.git (private)
- 동기화: git add . && git commit -m "메시지" && git push / git pull
