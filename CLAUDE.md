# OKX CryptoAnalyzer v1.0

## 프로젝트 개요
- BTC 무기한 선물(OKX) 자동매매 시스템
- 14개 매매 기법 기반 시그널 합산 → 등급별 자동 진입/청산
- 설계서: `명세서.md` 참고

## 기술스택
- Python 3.11 / ccxt / scikit-learn / FastAPI
- DB: SQLite(캔들) + Redis(실시간)
- 알림: Telegram

## 개발 로드맵
- Phase 1: 데이터 수집 + 기법 엔진
- Phase 2: ML + 시그널 합산
- Phase 3: 매매 엔진 + 리스크
- Phase 4: 백테스트
- Phase 5: 모니터링 + 실전

## 현재 진행 상황
- 명세서 작성 완료 (2026-04-03)
- Phase 1 시작 전
