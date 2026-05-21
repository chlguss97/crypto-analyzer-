# GridBot 전수검사 프롬프트

> GridBot — ATR-Adaptive Grid Trading
> 마지막 갱신: 2026-05-21

---

## 시스템 맵

```
src/
  main.py                        — GridBot 오케스트레이션
  strategy/
    grid_engine.py               — ATR-Adaptive Grid (build/monitor/rebalance/recover)
  data/
    ws_stream.py                 — OKX WS (trades/tickers/books/candles + Hurst)
    candle_collector.py          — OKX REST 캔들 백필 (ATR용)
    storage.py                   — SQLite (scalp.db) + Redis 래퍼
  trading/
    executor.py                  — OKX CCXT 주문 실행 + 그리드용 limit order
    grid_state.py                — GridLevel/GridState 데이터클래스 + Redis 직렬화
    risk_manager.py              — BOT_KILL -20% DD
  monitoring/
    telegram_bot.py              — Telegram 알림/명령
    trade_logger.py              — JSONL 로깅
    dashboard.py                 — FastAPI 대시보드
  utils/helpers.py               — 설정 로딩
config/settings.yaml             — Grid + risk + exchange 설정
```

## DB 테이블 (scalp.db)
- `candles`: OHLCV (symbol, timeframe, timestamp)
- `grid_trades`: 그리드 사이클 기록 (grid_id, level_id, entry/exit, pnl, spacing)

## Redis 키 (Grid 관련)
- `rt:price:BTC-USDT-SWAP` — 현재가
- `rt:regime:hurst` — Hurst 지수 (그리드 일시정지 게이트)
- `grid:state:BTC/USDT:USDT` — 그리드 상태 (JSON, 크래시 복구용)
- `sys:bot_status` — running/stopped
- `sys:balance` — 잔고
- `sys:last_heartbeat` — 헬스체크

## 검사 체크리스트

### 1. 레거시 잔해 확인 (0건이어야 함)

**삭제된 파일 참조:**
- `scalp_detector` / `ScalpDetector` / `EnsembleDetector`
- `scalp_manager` / `ScalpManager`
- `ml_engine` / `MLDecisionEngine` / `ModelManager`
- `adaptive_params` / `AdaptiveParams`
- `welford` / `FeatureNormalizer`

**삭제된 DB 테이블 참조:**
- `scalp_signals` (코드에서 참조 0건)
- `scalp_trades` (코드에서 참조 0건)

**삭제된 설정 참조:**
- `scalp:` 섹션 (settings.yaml에 없어야 함)
- `cooldown:` 섹션
- `ml:` 섹션
- `shadow_mode`
- `time_stop_sec` / `time_stop_max_sec`

**삭제된 Redis 키 참조:**
- `pos:active:*` (스캘핑 포지션)
- `risk:cooldown_until`
- `rt:micro:vpin` / `rt:micro:ofi` / `rt:micro:ou_zscore` (코드에서 set 안 함은 OK, 참조만 확인)

### 2. 레거시 데이터 삭제 (서버에서 실행)

```bash
# SQLite 레거시 테이블 삭제
ssh root@207.148.120.103 "docker exec crypto-bot-bot-1 python3 -c \"
import sqlite3
db = sqlite3.connect('/app/data/scalp.db')
db.execute('DROP TABLE IF EXISTS scalp_signals')
db.execute('DROP TABLE IF EXISTS scalp_trades')
db.execute('VACUUM')
db.close()
print('scalp_signals + scalp_trades 삭제 완료')
\""

# Redis 레거시 키 삭제
ssh root@207.148.120.103 "docker exec crypto-bot-redis-1 redis-cli KEYS 'rt:micro:*' | xargs -r docker exec -i crypto-bot-redis-1 redis-cli DEL"
ssh root@207.148.120.103 "docker exec crypto-bot-redis-1 redis-cli KEYS 'flow:*' | xargs -r docker exec -i crypto-bot-redis-1 redis-cli DEL"
ssh root@207.148.120.103 "docker exec crypto-bot-redis-1 redis-cli KEYS 'risk:*' | xargs -r docker exec -i crypto-bot-redis-1 redis-cli DEL"
ssh root@207.148.120.103 "docker exec crypto-bot-redis-1 redis-cli DEL pos:active:BTC/USDT:USDT"

# LOB 스냅샷 파일 삭제
ssh root@207.148.120.103 "rm -f /root/crypto-bot/data/lob_snapshots.bin"

# ML 모델 파일 삭제
ssh root@207.148.120.103 "rm -f /root/crypto-bot/data/ml_scalp_model.pkl /root/crypto-bot/data/deeplob5.pt"

# 리스크 백업 삭제
ssh root@207.148.120.103 "rm -f /root/crypto-bot/data/risk_state.json"
```

### 3. Grid 정합성 확인

- `grid_engine.py`: build_grid → 4레벨 주문 배치 확인
- `grid_engine.py`: monitor_tick → 체결 감지 + counter-order 배치
- `grid_engine.py`: rebalance → drift/ATR 기반 재구성
- `grid_engine.py`: regime gate → Hurst > 0.7 일시정지
- `grid_state.py`: Redis 저장/복원 정합성
- `executor.py`: place_limit_order / cancel_order_by_id 존재
- `storage.py`: grid_trades 테이블 + insert/query 메서드
- `settings.yaml`: grid 섹션 완전성 (enabled, levels, spacing, atr, hurst_pause)
- `risk_manager.py`: BOT_KILL -20% DD만 체크 (다른 게이트 없음)

### 4. 문서 정합성

- `CLAUDE.md`: Grid Trading 현재 활성, 스캘핑 비활성 표기
- `SPEC_V2.md`: §8 Grid Trading Engine 존재
- `CHANGELOG.md`: 2026-05-21 Grid 전환 기록
- `MANUAL.md`: Grid 운영 가이드 추가 필요 (TODO)
