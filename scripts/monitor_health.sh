#!/usr/bin/env bash
#
# 외부 봇 모니터링 — 봇과 무관한 별도 프로세스
#
# 봇이 죽거나 STALE 상태일 때 텔레그램으로 직접 alert.
# 봇 자체가 죽어도 cron 이 살아있으면 동작.
#
# cron 예시 (5분마다):
#   */5 * * * * cd /root/crypto-bot && ./scripts/monitor_health.sh >> /tmp/bot_monitor.log 2>&1
#
# 동작:
#   1. health_check.sh json 실행
#   2. 상태 != OK 면 텔레그램 발송
#   3. 1시간(3600s) 이내 같은 알람 중복 발송 방지 (스팸 방지)
#

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

# .env 에서 텔레그램 토큰 로드 (봇과 동일 토큰 사용)
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

TOKEN="${TELEGRAM_BOT_TOKEN:-}"
CHAT_ID="${TELEGRAM_CHAT_ID:-}"

if [ -z "$TOKEN" ] || [ -z "$CHAT_ID" ]; then
    echo "$(date '+%H:%M:%S') 텔레그램 토큰/채팅ID 미설정 → 모니터링 비활성"
    exit 0
fi

# 헬스체크 실행
HEALTH=$("$SCRIPT_DIR/health_check.sh" json 2>/dev/null || echo '{"status":"ERROR","container":"unknown"}')
STATUS=$(echo "$HEALTH" | grep -oE '"status": "[^"]*"' | cut -d'"' -f4)
CONTAINER=$(echo "$HEALTH" | grep -oE '"container": "[^"]*"' | cut -d'"' -f4)
HB_AGO=$(echo "$HEALTH" | grep -oE '"heartbeat_ago_sec": -?[0-9]+' | grep -oE '\-?[0-9]+')
BALANCE=$(echo "$HEALTH" | grep -oE '"balance": "[^"]*"' | cut -d'"' -f4)

if [ -z "$STATUS" ]; then STATUS="UNKNOWN"; fi

# 마지막 alert 시각 (반복 alert 방지 — 1시간에 1번만)
LAST_ALERT_FILE="/tmp/bot_monitor_last_alert"
NOW=$(date +%s)
LAST_ALERT=0
if [ -f "$LAST_ALERT_FILE" ]; then
    LAST_ALERT=$(cat "$LAST_ALERT_FILE" 2>/dev/null || echo 0)
fi
DIFF=$((NOW - LAST_ALERT))

# OK 가 아니면 alert (STALE / DOWN / UNKNOWN / ERROR 등)
if [ "$STATUS" != "OK" ]; then
    # 1시간 이내 중복 발송 방지
    if [ "$DIFF" -gt 3600 ]; then
        MSG="🚨 [외부 모니터] 봇 비정상%0A%0A상태: ${STATUS}%0A컨테이너: ${CONTAINER}%0AHeartbeat: ${HB_AGO}초 전%0A잔고: ${BALANCE}%0A%0A즉시 확인 필요"
        curl -sS --max-time 10 \
            "https://api.telegram.org/bot${TOKEN}/sendMessage" \
            -d "chat_id=${CHAT_ID}" \
            -d "text=${MSG}" > /dev/null 2>&1
        echo "$NOW" > "$LAST_ALERT_FILE"
        echo "$(date '+%H:%M:%S') ALERT 발송: ${STATUS} (HB ${HB_AGO}s)"
    else
        echo "$(date '+%H:%M:%S') ${STATUS} 감지 — 중복 alert 스킵 (${DIFF}s 전 발송)"
    fi
else
    echo "$(date '+%H:%M:%S') ${STATUS} (HB ${HB_AGO}s, 잔고 ${BALANCE})"
    # OK 상태로 복귀 시 마지막 알람 리셋 (다음 사고 즉시 알람)
    rm -f "$LAST_ALERT_FILE"
fi
