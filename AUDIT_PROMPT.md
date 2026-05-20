# ScalpEngine v3 전수검사 프롬프트

> ScalpEngine v3 — 4계층 마이크로스트럭처 스캘핑
> 마지막 갱신: 2026-05-20

---

## 시스템 맵

```
src/
  main.py                        — ScalpEngine 오케스트레이션 (12 태스크)
  strategy/
    scalp_detector.py             — 실시간 시그널 감지 (Redis only, 500ms)
    scalp_manager.py              — 스캘핑 포지션 관리 (TP/SL/TimeStop)
    ml_engine.py                  — XGBoost Go/NoGo (Phase A→B, 20종 피처)
    adaptive_params.py            — TP/SL 자동 보정
    welford.py                    — Welford 온라인 z-score 정규화
  data/
    ws_stream.py                  — OKX WS (trades/tickers/books/candles + 마이크로15종 + OFI + Hurst + Parkinson)
    binance_stream.py             — Binance aggTrade/REST (CVD/Whale/Liq/Funding)
    candle_collector.py           — OKX REST 캔들 백필
    storage.py                    — SQLite (scalp.db) + Redis 래퍼
  trading/
    executor.py                   — OKX CCXT 주문 실행
    risk_manager.py               — BOT_KILL + 쿨다운 + 연패 관리
  monitoring/
    telegram_bot.py               — Telegram 알림/명령
    trade_logger.py               — JSONL 로깅 (_append_jsonl)
    dashboard.py                  — FastAPI 대시보드
  utils/helpers.py                — 설정 로딩
config/settings.yaml              — 모든 설정 (scalp 섹션 포함)
```

## DB 테이블 (scalp.db)
- `candles`: OHLCV (symbol, timeframe, timestamp)
- `scalp_signals`: Shadow + ML 라벨링 (signal_type, direction, features, regime, hurst, vpin, label, barrier_hit)
- `scalp_trades`: 실거래 (signal_id, direction, entry/exit, pnl, hold_sec, regime)

## Redis 키
- `rt:price:*`, `rt:ticker:*`, `rt:velocity:*` — 가격/속도
- `rt:micro:*` (15종) — 마이크로스트럭처
- `rt:micro:ofi` — OFI 멀티레벨
- `rt:regime:hurst` — Hurst 지수
- `rt:micro:parkinson_vol` — Parkinson 변동성
- `flow:combined:*` — CVD/Whale
- `flow:liq:*` — 청산
- `rt:funding:*`, `rt:oi:*` — 펀딩/OI

## 검사 체크리스트

1. **Import 정합성**: 삭제된 모듈 참조 없는지 (engine/, candidate_detector, paper_lab, sim_trader, signal_tracker, setup_tracker, position_manager, leverage)
2. **DB 테이블명**: `scalp_signals`, `scalp_trades` 사용 (old: signals, trades)
3. **DB 메서드명**: `insert_scalp_signal`, `insert_scalp_trade`, `update_scalp_trade_exit` 등
4. **Redis 키**: 피처 키가 실제 ws_stream/binance_stream에서 생성되는지
5. **ScalpDetector**: 모든 조건 임계값이 config/settings.yaml과 일치하는지
6. **ScalpManager**: 동적 SL/TP (k × vol) + self-heal + failsafe + 시그널 반전 청산
7. **ML 피처**: 20종 피처명이 scalp_detector → ml_engine 간 일치하는지
8. **Telegram**: scalp_manager 주입, /close /clear 명령 동작
9. **Dashboard**: 모든 SQL 쿼리가 scalp_signals/scalp_trades 참조
10. **JSONL**: 이벤트 타입 (candidate, shadow_result, scalp_entry, scalp_exit, hourly_snapshot)
11. **프로 레퍼런스 12항목**: VPIN 4단계, OFI 5레벨, Hurst 동적스케일, OU 감쇠0.93, Vol블렌딩50/50, 앙상블합의, 마이크로3축, Welford워밍업, BookResilience, 동적SL/TP, CVD오버라이드, 사이징캐스케이드
12. **제거 확인**: 쿨다운/연패축소/시간당제한/진입간격/Shadow WR 게이트가 코드에 없는지
