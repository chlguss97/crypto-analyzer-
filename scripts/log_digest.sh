#!/usr/bin/env bash
#
# 로그 다이제스트 — 최근 N 분 로그에서 트레이딩 핵심 이벤트만 추려내기
#
# 사용법:
#   ./scripts/log_digest.sh                  # 최근 30분
#   ./scripts/log_digest.sh 60               # 최근 60분
#   ./scripts/log_digest.sh 30 > digest.txt  # 파일로
#
# cron 예시 (10분마다 → 파일):
#   */10 * * * * cd /home/ubuntu/crypto-analyzer- && ./scripts/log_digest.sh 15 > /tmp/bot_digest.log 2>&1
#
# 핵심 이벤트:
#   - 진입/청산 (포지션 오픈, 부분 청산, 포지션 종료)
#   - TP/SL 알고 등록 + 갱신
#   - 러너 트레일링
#   - 모든 ERROR / WARNING
#   - 예외/스택트레이스
#   - OKX API 에러 (sCode, algoClOrdId)
#

set -euo pipefail

MINUTES="${1:-30}"
SERVICE="${BOT_SERVICE:-bot}"

# docker compose logs --since 인자 사용
echo "═══════════════════════════════════════════════════"
echo "📊 로그 디지스트 — 최근 ${MINUTES}분 ($(date '+%Y-%m-%d %H:%M:%S %Z'))"
echo "═══════════════════════════════════════════════════"
echo

# ── 🩺 헬스체크 (가장 먼저, 한 눈에 보이게) ──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -x "$SCRIPT_DIR/health_check.sh" ]; then
    "$SCRIPT_DIR/health_check.sh"
elif [ -f "$SCRIPT_DIR/health_check.sh" ]; then
    bash "$SCRIPT_DIR/health_check.sh"
fi
echo

# docker compose 또는 docker-compose 자동 감지
if docker compose version &>/dev/null; then
    DC="docker compose"
elif command -v docker-compose &>/dev/null; then
    DC="docker-compose"
else
    echo "ERROR: docker compose / docker-compose 없음" >&2
    exit 1
fi

# 1. 진입/청산/TP 이벤트
echo "── 🎯 트레이딩 이벤트 ──"
$DC logs --since "${MINUTES}m" "$SERVICE" 2>&1 | \
    grep -E "포지션 오픈|포지션 종료|부분 청산|TP1|러너|🏃|✅|🛑|sl_failsafe|algo:|reduce" \
    | tail -100 || echo "(없음)"
echo

# 2. 알고 주문 등록/취소/실패
echo "── 🔧 알고 주문 (SL/TP) ──"
$DC logs --since "${MINUTES}m" "$SERVICE" 2>&1 | \
    grep -E "알고 주문|set_protection|algoClOrdId|update_stop_loss|cancel_algo|sCode" \
    | tail -50 || echo "(없음)"
echo

# 3. ERROR / 🚨
echo "── ❌ ERROR / 🚨 ──"
$DC logs --since "${MINUTES}m" "$SERVICE" 2>&1 | \
    grep -E "ERROR|🚨|Exception|Traceback|진입 실패|청산 실패" \
    | tail -50 || echo "(없음)"
echo

# 4. WARNING
echo "── ⚠️ WARNING ──"
$DC logs --since "${MINUTES}m" "$SERVICE" 2>&1 | \
    grep -E "WARNING|⚠️" \
    | grep -vE "DeprecationWarning" \
    | tail -30 || echo "(없음)"
echo

# 5. 시그널 평가 + 거부 사유 통계 (FlowEngine 패턴)
echo "── 📉 시그널 평가 통계 ──"
$DC logs --since "${MINUTES}m" "$SERVICE" 2>&1 | \
    grep -E "\[TRADE\] setup=" | \
    sed -E 's/.*reason=([^ ]+).*/\1/' | \
    sort | uniq -c | sort -rn | head -10 || echo "(없음)"
echo
echo "── 🚧 게이트 차단 통계 ──"
$DC logs --since "${MINUTES}m" "$SERVICE" 2>&1 | \
    grep -E "\[TRADE\] 게이트:" | \
    sed -E 's/.*게이트: ([^→]+).*/\1/' | \
    sort | uniq -c | sort -rn | head -10 || echo "(없음)"
echo

# 6. 잔고/포지션 상태 (마지막 1건)
echo "── 💰 마지막 잔고/포지션 ──"
$DC logs --since "${MINUTES}m" "$SERVICE" 2>&1 | \
    grep -E "잔고|balance|포지션 사이즈" | tail -5 || echo "(없음)"
echo

echo "═══════════════════════════════════════════════════"
echo "끝 — line count: $($DC logs --since ${MINUTES}m $SERVICE 2>&1 | wc -l)"
