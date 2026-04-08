#!/usr/bin/env bash
#
# COMMIT_LOG.md 자동 갱신 — git log 에서 전체 커밋을 날짜별로 그룹핑
#
# 사용법:
#   ./scripts/update_commit_log.sh
#
# 규칙:
#   - 매 커밋 후 실행 (Claude 가 자동 호출)
#   - COMMIT_LOG.md 는 항상 git log 의 단일 원본을 반영 (수동 편집 금지)
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT="$REPO_DIR/COMMIT_LOG.md"

cd "$REPO_DIR"

TOTAL=$(git rev-list --count HEAD)
NOW=$(date '+%Y-%m-%d %H:%M:%S %Z')
FIRST=$(git log --reverse --pretty=format:'%ad' --date=short | head -1)
LAST=$(git log -1 --pretty=format:'%ad' --date=short)

{
  echo "# Commit Log"
  echo ""
  echo "> 자동 생성 — \`scripts/update_commit_log.sh\` (매 커밋 후 실행)"
  echo "> Updated: ${NOW}"
  echo "> Total commits: ${TOTAL} (${FIRST} → ${LAST})"
  echo ""
  echo "Claude 가 질문/변경 작업 시 이 파일을 참고해서 과거 변경 이력 컨텍스트를 확보합니다. 수동 편집 금지 — 다음 커밋 시 덮어써집니다."
  echo ""

  git log --pretty=format:'%ad|%h|%s' --date=short | awk -F'|' '
    BEGIN { prev = "" }
    {
      if ($1 != prev) {
        if (prev != "") print ""
        print "## " $1
        prev = $1
      }
      # 나머지 필드는 subject 에 | 가 들어있을 수 있으므로 재조립
      subj = $3
      for (i = 4; i <= NF; i++) subj = subj "|" $i
      print "- `" $2 "` " subj
    }
  '
  echo ""
} > "$OUT"

echo "Updated: $OUT (${TOTAL} commits, ${FIRST} → ${LAST})"
