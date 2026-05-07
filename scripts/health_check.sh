#!/usr/bin/env bash
#
# 봇 헬스체크 — 한 줄 출력으로 살아있는지 즉시 확인
#
# 사용법:
#   ./scripts/health_check.sh         # stdout 출력
#   ./scripts/health_check.sh json    # JSON 형식 (digest 에 포함하기 좋음)
#

set -uo pipefail

FORMAT="${1:-text}"

# docker compose 자동 감지 — 두 단어 명령 확인은 직접 실행 (command -v 는 단일 단어만)
if docker compose version &>/dev/null; then
    DC="docker compose"
elif command -v docker-compose &>/dev/null; then
    DC="docker-compose"
else
    echo "ERROR: docker compose 없음" >&2
    exit 1
fi

# 1. 컨테이너 상태 — --format 으로 정확한 STATUS 컬럼 추출 (PORTS 와 혼동 방지)
BOT_STATUS=$($DC ps --format '{{.State}}' bot 2>/dev/null | head -1)
if [ -z "$BOT_STATUS" ]; then
    # fallback: 옛 docker compose 는 --format 미지원
    BOT_STATUS=$($DC ps bot 2>/dev/null | tail -n +2 | awk '{
        # STATUS 컬럼 = "Up X minutes" 또는 "Exited" 등 — 4번째 컬럼부터 시작
        for (i=4; i<=NF; i++) { if ($i ~ /^(Up|Exited|Restarting|Created|Paused|Dead)/) { print $i; exit } }
    }' | head -1)
fi
[ -z "$BOT_STATUS" ] && BOT_STATUS="not_found"

# 2. 마지막 로그 시각 (어떤 로그든)
LAST_LOG=$($DC logs --tail 1 bot 2>&1 | grep -oE '202[0-9]-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' | head -1)
[ -z "$LAST_LOG" ] && LAST_LOG="없음"

# 3. 마지막 heartbeat (Redis 에 저장됨)
HEARTBEAT=$($DC exec -T redis redis-cli get sys:last_heartbeat 2>/dev/null | tr -d '\r\n')
HB_AGO=-1
if [ -n "$HEARTBEAT" ] && [ "$HEARTBEAT" != "(nil)" ]; then
    # 정수인지 확인 (set -u 안전 + 음수 방어)
    if [[ "$HEARTBEAT" =~ ^[0-9]+$ ]]; then
        NOW=$(date +%s)
        HB_AGO=$((NOW - HEARTBEAT))
    fi
fi

# 4. 마지막 시그널 평가 시각 (CandidateDetector 로그 패턴)
LAST_EVAL=$($DC logs --since 5m bot 2>&1 | grep -E "\[TRADE\] setup=|\[TRADE\] 게이트:" | tail -1 | grep -oE '[0-9]{2}:[0-9]{2}:[0-9]{2}' | head -1)
[ -z "$LAST_EVAL" ] && LAST_EVAL="없음(5분내)"

# 5. 활성 포지션 수
ACTIVE_POS=$($DC exec -T redis redis-cli --scan --pattern "pos:active:*" 2>/dev/null | wc -l)

# 6. 봇 자동매매 ON/OFF
AUTOTRADING=$($DC exec -T redis redis-cli get sys:autotrading 2>/dev/null | tr -d '\r\n')
[ -z "$AUTOTRADING" ] && AUTOTRADING="unknown"

# 7. 잔고 (Redis 캐시 — main.py periodic_heartbeat 가 60초마다 갱신)
BALANCE_RAW=$($DC exec -T redis redis-cli get sys:balance 2>/dev/null | tr -d '\r\n')
if [ -n "$BALANCE_RAW" ] && [ "$BALANCE_RAW" != "(nil)" ]; then
    BALANCE="\$$BALANCE_RAW"
else
    BALANCE="?"
fi

# 8. 학습 중 여부 (지난 5분 로그에서 [HIST] 또는 [SCHED])
LEARNING=$($DC logs --since 5m bot 2>&1 | grep -E "\[HIST\]|\[SCHED\]" | tail -1)
LEARN_STATUS="idle"
[ -n "$LEARNING" ] && LEARN_STATUS="active"

# 종합 판정 — running/Up 으로 시작하면 정상
STATUS="OK"
case "$BOT_STATUS" in
    running|Up*) ;;  # 정상
    *) STATUS="DOWN" ;;
esac

if [ "$STATUS" = "OK" ] && { [ "$HB_AGO" -lt 0 ] || [ "$HB_AGO" -gt 180 ]; }; then
    STATUS="STALE"  # 3분 이상 heartbeat 없음
fi

if [ "$FORMAT" = "json" ]; then
    cat <<EOF
{
  "status": "$STATUS",
  "container": "$BOT_STATUS",
  "last_log": "$LAST_LOG",
  "heartbeat_ago_sec": $HB_AGO,
  "last_signal_eval": "$LAST_EVAL",
  "active_positions": $ACTIVE_POS,
  "autotrading": "$AUTOTRADING",
  "balance": "$BALANCE",
  "learning": "$LEARN_STATUS",
  "checked_at": "$(date '+%Y-%m-%d %H:%M:%S %Z')"
}
EOF
else
    echo "═══ 봇 헬스체크 ($(date '+%H:%M:%S')) ═══"
    case "$STATUS" in
        OK) echo "상태       : ✅ $STATUS" ;;
        DOWN) echo "상태       : 🚨 $STATUS (컨테이너 다운)" ;;
        STALE) echo "상태       : ⚠️  $STATUS (heartbeat 멈춤)" ;;
    esac
    echo "컨테이너   : $BOT_STATUS"
    echo "마지막 로그: $LAST_LOG"
    if [ "$HB_AGO" -ge 0 ]; then
        echo "Heartbeat  : ${HB_AGO}초 전"
    else
        echo "Heartbeat  : 없음"
    fi
    echo "시그널 평가: $LAST_EVAL"
    echo "활성 포지션: ${ACTIVE_POS}개"
    echo "자동매매   : $AUTOTRADING"
    echo "잔고       : $BALANCE"
    echo "학습 상태  : $LEARN_STATUS"
fi
