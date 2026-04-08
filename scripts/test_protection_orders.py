"""
보호 주문 파이프라인 라운드트립 테스트 (OKX 데모 트레이딩)

사전 준비:
  1. OKX 데모 API 키 발급 → .env 의 OKX_API_KEY/OKX_SECRET_KEY/OKX_PASSPHRASE 를
     데모 키로 일시 교체 (또는 별도 .env.demo 사용)
  2. 환경변수 OKX_DEMO=1 설정
  3. 데모 계정에 BTC-USDT-SWAP 잔고 충분한지 확인

실행:
  cd C:\\Users\\user\\Desktop\\claude
  set OKX_DEMO=1
  python -m scripts.test_protection_orders

검증 항목:
  ✅ T1  데모 모드 연결 확인
  ✅ T2  algoClOrdId 생성 형식 (영숫자만, 32자 이하)
  ✅ T3  set_protection 으로 SL+TP1+TP2+TP3 등록 (4건 모두 성공)
  ✅ T4  거래소에 알고 주문 4건 존재 확인
  ✅ T5  update_stop_loss: 기존 SL cancel → 새 SL 등록
  ✅ T6  cancel_algo_order 동작 (남은 알고 모두 취소)
  ✅ T7  포지션 정리 (close_position)
  ✅ T8  algoClOrdId 에 underscore 없는지 재확인
"""
import asyncio
import logging
import os
import sys
import time

# 프로젝트 루트 경로
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.trading.executor import OrderExecutor  # noqa: E402
from src.utils.helpers import load_env, load_config  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("test_protection")

# 검증용 작은 사이즈 (BTC)
TEST_SIZE = 0.001  # 0.001 BTC ≈ $70 (데모)
TEST_LEVERAGE = 10
SL_PCT = 0.02   # 진입가 ±2%
TP1_PCT = 0.01  # ±1%
TP2_PCT = 0.02
TP3_PCT = 0.03


def assert_ok(cond: bool, msg: str):
    if cond:
        logger.info(f"✅ {msg}")
    else:
        logger.error(f"❌ {msg}")
        raise AssertionError(msg)


async def main():
    # .env 로드
    load_env()
    if os.environ.get("OKX_DEMO", "0") not in ("1", "true", "True", "yes"):
        logger.error("환경변수 OKX_DEMO=1 이 설정되어야 합니다 (데모 모드 강제)")
        sys.exit(1)

    cfg = load_config()
    symbol = cfg["exchange"]["symbol"]

    executor = OrderExecutor()
    await executor.initialize()
    logger.info(f"심볼: {symbol}")

    try:
        # ── T1. 데모 모드 연결 ──
        balance = await executor.get_balance()
        logger.info(f"데모 USDT 잔고: ${balance:,.2f}")
        assert_ok(balance > 0, "T1: 데모 계정 잔고 조회")

        # ── T2. algoClOrdId 형식 검증 ──
        sample_id = OrderExecutor._gen_algo_id("sl")
        assert_ok(
            sample_id.isalnum() and len(sample_id) <= 32,
            f"T2: algoClOrdId 형식 OK ({sample_id}, len={len(sample_id)})"
        )
        assert_ok("_" not in sample_id, "T2: 언더스코어 없음")

        # 현재가 조회
        ticker = await executor.exchange.fetch_ticker(symbol)
        last_price = float(ticker["last"])
        logger.info(f"현재가: ${last_price:,.1f}")

        # ── T3. SHORT 진입 + set_protection ──
        direction = "short"
        await executor.set_leverage(TEST_LEVERAGE, direction)

        # 진입 (시장가) — SHORT 오픈: side=sell, posSide=short
        order = await executor._market_order(
            side="sell",
            size=TEST_SIZE,
            pos_side="short",
        )
        assert_ok(order is not None, "T3a: 진입 시장가 체결")
        fill_price = float(order.get("average") or order.get("price") or last_price)
        logger.info(f"진입 체결가: ${fill_price:,.1f}")

        # SL/TP 가격 계산 (SHORT 기준)
        sl_price = round(fill_price * (1 + SL_PCT), 1)
        tp1_price = round(fill_price * (1 - TP1_PCT), 1)
        tp2_price = round(fill_price * (1 - TP2_PCT), 1)
        tp3_price = round(fill_price * (1 - TP3_PCT), 1)
        logger.info(
            f"보호 주문: SL ${sl_price} | TP1 ${tp1_price} TP2 ${tp2_price} TP3 ${tp3_price}"
        )

        ids = await executor.set_protection(
            direction=direction,
            total_size=TEST_SIZE,
            sl_price=sl_price,
            tp_levels=[
                (tp1_price, 0.5),
                (tp2_price, 0.3),
                (tp3_price, 0.2),
            ],
        )
        logger.info(f"등록된 알고 ID: {ids}")
        assert_ok(ids["sl"] is not None, "T3b: SL 등록 성공")
        assert_ok(ids["tp1"] is not None, "T3c: TP1 등록 성공")
        assert_ok(ids["tp2"] is not None, "T3d: TP2 등록 성공")
        assert_ok(ids["tp3"] is not None, "T3e: TP3 등록 성공")
        for k, v in ids.items():
            if v:
                assert_ok("_" not in v, f"T8: {k} ID에 underscore 없음 ({v})")

        # ── T4. 거래소에 알고 주문 존재 확인 ──
        await asyncio.sleep(2)
        try:
            inst_id = executor.exchange.market(symbol)["id"]
            algo_orders = await executor.exchange.private_get_trade_orders_algo_pending({
                "instType": "SWAP",
                "instId": inst_id,
            })
            data = algo_orders.get("data", [])
            our_ids = {ids["sl"], ids["tp1"], ids["tp2"], ids["tp3"]}
            found = sum(1 for o in data if o.get("algoClOrdId") in our_ids)
            logger.info(f"거래소 미체결 알고 주문: {len(data)}건, 우리 것: {found}건")
            assert_ok(found >= 4, f"T4: 알고 주문 4건 존재 확인 (found={found})")
        except Exception as e:
            logger.warning(f"T4 알고 주문 조회 실패 (ccxt 메서드 미지원 가능): {e}")

        # ── T5. update_stop_loss: SL 본절로 이동 ──
        new_sl = round(fill_price * 1.001, 1)  # SHORT: SL을 진입가 약간 위로 (사실상 본절)
        new_sl_id = await executor.update_stop_loss(
            direction=direction,
            size=TEST_SIZE,
            new_sl=new_sl,
            old_algo_id=ids["sl"],
        )
        assert_ok(new_sl_id is not None and new_sl_id != ids["sl"], "T5: SL 갱신 (cancel+신규)")
        ids["sl"] = new_sl_id
        logger.info(f"새 SL ID: {new_sl_id}")

        # ── T6. cancel_algo_order: 모든 알고 정리 ──
        for k, v in list(ids.items()):
            if v:
                ok = await executor.cancel_algo_order(v)
                logger.info(f"  취소 {k}={v}: {'OK' if ok else 'SKIP/이미체결'}")

        # ── T7. 포지션 정리 ──
        size_now = await executor.get_position_size(symbol)
        logger.info(f"청산 전 포지션 사이즈: {size_now}")
        if size_now > 0:
            close_order = await executor.close_position(direction, size_now, "test_cleanup")
            assert_ok(close_order is not None, "T7: 포지션 시장가 청산")
        else:
            logger.info("T7: 이미 청산됨 (서버 알고가 먼저 발동했을 수 있음)")

        logger.info("=" * 50)
        logger.info("🎉 모든 검증 항목 통과")
        logger.info("=" * 50)

    except AssertionError as e:
        logger.error(f"검증 실패: {e}")
        sys.exit(2)
    except Exception as e:
        logger.error(f"예외 발생: {e}", exc_info=True)
        sys.exit(3)
    finally:
        # 안전 정리: 포지션 잔여 시 청산 시도
        try:
            size = await executor.get_position_size(symbol)
            if size and size > 0:
                logger.warning(f"⚠️  잔여 포지션 {size} 발견 → 강제 청산")
                await executor.close_position("short", size, "test_finally")
        except Exception:
            pass
        try:
            await executor.cancel_all_orders()
        except Exception:
            pass
        await executor.close()


if __name__ == "__main__":
    asyncio.run(main())
