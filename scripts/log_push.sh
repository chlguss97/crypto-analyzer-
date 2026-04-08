#!/usr/bin/env bash
#
# 로그 디지스트를 GitHub 'logs' 브랜치에 자동 푸시
# → Claude 가 git fetch + checkout 으로 분석 가능
#
# 사용법:
#   ./scripts/log_push.sh           # 최근 30분 로그 푸시
#   ./scripts/log_push.sh 60        # 최근 60분
#
# cron 예시 (15분마다):
#   */15 * * * * cd /home/ubuntu/crypto-analyzer- && ./scripts/log_push.sh 20 >> /tmp/log_push.log 2>&1
#
# 사전 준비:
#   1. git config --global user.email "bot@cloud.local"
#   2. git config --global user.name "cloud-bot"
#   3. logs 브랜치 한 번 만들어두기:
#        git checkout --orphan logs && git rm -rf . && \
#        echo "# Bot Logs" > README.md && git add README.md && \
#        git commit -m "init logs" && git push -u origin logs && \
#        git checkout main
#

set -euo pipefail

MINUTES="${1:-30}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DIGEST_FILE="/tmp/bot_digest_$$.txt"
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')

cd "$REPO_DIR"

# 1. 디지스트 생성
"$SCRIPT_DIR/log_digest.sh" "$MINUTES" > "$DIGEST_FILE"

# 2. 빈 로그면 푸시 안 함 (트래픽 절약)
if ! grep -qE "포지션|TP|ERROR|WARNING" "$DIGEST_FILE"; then
    echo "$(date '+%H:%M:%S') 푸시 스킵: 의미있는 이벤트 없음"
    rm -f "$DIGEST_FILE"
    exit 0
fi

# 3. main 브랜치 백업 (작업 안전)
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
git stash --include-untracked --quiet || true

# 4. logs 브랜치로 전환 (없으면 fetch)
git fetch origin logs --quiet 2>/dev/null || true
if ! git rev-parse --verify logs &>/dev/null; then
    git checkout -b logs origin/logs 2>/dev/null || {
        echo "ERROR: logs 브랜치 없음. 먼저 수동 생성 필요" >&2
        echo "  git checkout --orphan logs && git rm -rf . && echo '# Logs' > README.md && git add README.md && git commit -m init && git push -u origin logs && git checkout main" >&2
        git checkout "$CURRENT_BRANCH" --quiet
        git stash pop --quiet 2>/dev/null || true
        exit 2
    }
else
    git checkout logs --quiet
fi
git pull origin logs --quiet --rebase 2>/dev/null || true

# 5. 디지스트 파일 복사
mkdir -p digests
cp "$DIGEST_FILE" "digests/${TIMESTAMP}.txt"

# 6. latest.txt 항상 갱신 (최신 항상 같은 경로에서 보기)
cp "$DIGEST_FILE" "digests/latest.txt"

# 7. 오래된 디지스트 정리 (7일 초과)
find digests/ -name "20*.txt" -mtime +7 -delete 2>/dev/null || true

# 8. 커밋 + 푸시
git add digests/
if git diff --cached --quiet; then
    echo "$(date '+%H:%M:%S') 변경 없음"
else
    git commit -m "log digest ${TIMESTAMP}" --quiet
    git push origin logs --quiet
    echo "$(date '+%H:%M:%S') 푸시 완료: digests/${TIMESTAMP}.txt"
fi

# 9. 원래 브랜치 복귀
git checkout "$CURRENT_BRANCH" --quiet
git stash pop --quiet 2>/dev/null || true

rm -f "$DIGEST_FILE"
