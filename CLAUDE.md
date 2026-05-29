# ScalpBot v5 — Jay 단타법 (StochRSI + MACD)

## 프로젝트 개요
- BTC 무기한 선물(OKX) 자동매매 시스템
- **현재 전략: Jay 단타법 — 후행 확인 매매** (2026-05-29~)
- 설계서: `SPEC_V5.md`
- 변경 인덱스: `COMMIT_LOG.md`
- 운영 매뉴얼: `MANUAL.md`

## 설계 원칙
1. **후행 확인 후 진입** (예측 아닌 확인)
2. **파라미터 시작 시 1회 설정, 실행 중 불변**
3. **단순할수록 강건** (지표 2개만: StochRSI + MACD)
4. **먹고 나간다** (StochRSI 도달 = 즉시 청산)
5. **거짓 신호 필터** (소진 구간 스킵)

## 현재 전략
- **타임프레임**: 30m (Jay 권장 30m~1h)
- **타점**: 볼린저밴드(20,2) 상단(숏) / 하단(롱) 에서만 진입
- **금지**: BB 중간(35~65%) 매매 금지 (Jay: "20이평은 쓰레기 평단")
- **롱 진입**: BB 하단 + StochRSI(14) K<20 골든크로스 + MACD(8,26,9) 골든크로스
- **숏 진입**: BB 상단 + StochRSI K>80 데드크로스 + MACD 데드크로스
- **청산**: StochRSI 반대편 도달 (롱→K>80, 숏→K<20)
- **SL**: BB 밴드 이탈 (롱→하단, 숏→상단) + ATR fallback
- **레버리지**: 10x 고정, isolated margin
- **주문**: 시장가 (모멘텀 전략)
- **안전장치**: 서킷브레이커(2%/10초) + BOT_KILL(-20%)

## 기술스택
- Python 3.11 / ccxt / FastAPI
- DB: SQLite(scalp.db: candles + scalp_trades) + Redis
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
