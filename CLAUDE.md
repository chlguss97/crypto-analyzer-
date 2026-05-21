# ScalpEngine v3

## 프로젝트 개요
- BTC 무기한 선물(OKX) 자동매매 시스템
- **마이크로스트럭처 스캘핑**: 4계층 파이프라인 (Raw → Feature → Regime → ML)
- 설계서: `SPEC_V2.md` (2026-05-20 v3 전면 재설계)
- 변경 인덱스: `COMMIT_LOG.md` (자동 갱신, 매 커밋 후)
- 사람용 변경 큐레이션: `CHANGELOG.md`
- 운영 매뉴얼: `MANUAL.md`

## 핵심 설계 결정 (v3, 2026-05-20 — 프로 레퍼런스 12/12 EXACT)
- **4계층**: Raw Data → Feature Engine → Regime Gate → ML Scorer → Execute
- **Regime Gate**: Hurst(동적 n/8~n 스케일) + VPIN(4단계) + Book Shock
- **시그널 2종**: Micro-Momentum Burst + OU Z-Score Reversion (0.93 감쇠)
- **앙상블**: Burst + OU 방향 합의, CVD divergence >0.3 오버라이드
- **TP/SL**: 동적 k(2.0) × Parkinson/Realized Vol 블렌딩 (고정% 없음)
- **청산**: 시그널 반전 post-only 청산 (시간정지 없음 — 프로 원문에 없음)
- **사이징**: VPIN배수 × Hurst배수 × micro_conf 캐스케이드
- **주문**: post-only maker 강제, 실패→포기
- **보호**: SL market-on-trigger + 5초 self-heal + 3회 소실 강제청산
- **제거**: 쿨다운/연패축소/시간당제한/시간정지/진입간격/Shadow WR 게이트 (프로 원문에 없음)

## 기술스택
- Python 3.11 / ccxt / scikit-learn / FastAPI
- DB: SQLite(scalp.db: candles + scalp_signals + scalp_trades) + Redis(실시간)
- 알림: Telegram

## 현재 상태 (2026-05-20)
- **LIVE 모드** (실거래 활성)
- 마이크로스트럭처 15종 + OFI + VPIN + OU Z-Score + Book Resilience 활성
- Hurst/Parkinson: 봇 시작 시 DB 백필로 즉시 계산
- 임계값: 전부 z-score/상대값 (절대 달러 임계값 0건)
- Shadow: **240건 시그널, 102건 라벨 (WR 84.3%)** — ou_reversion 92%, burst 18%
- ML Phase A (labeled 102건, 300건 도달 시 Phase B)
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
