#!/usr/bin/env bash
#
# 대시보드 + 봇 깨끗한 재배포 — 서버에서 실행
#
# 사용법:  bash scripts/deploy.sh
#
# 주의: `docker compose up -d` 만 하면 포트/이미지 캐시가 꼬여서
#       "TCP 열리는데 HTTP 무응답" 증상 발생. 본 스크립트는 down → build → up
#       으로 깨끗하게 재시작하고 /health 로 실제 응답 확인까지 수행함.

set -euo pipefail

cd "$(dirname "$0")/.."

if docker compose version &>/dev/null; then
    DC="docker compose"
elif command -v docker-compose &>/dev/null; then
    DC="docker-compose"
else
    echo "ERROR: docker compose 없음" >&2
    exit 1
fi

echo "=== [1/5] 최신 코드 pull ==="
git pull --ff-only || true

echo "=== [2/5] 기존 컨테이너 종료 + 포트 해제 ==="
$DC down --remove-orphans || true

echo "=== [3/5] 이미지 재빌드 (no-cache 권장 시: BUILD_NOCACHE=1) ==="
if [ "${BUILD_NOCACHE:-0}" = "1" ]; then
    $DC build --no-cache
else
    $DC build
fi

echo "=== [4/5] 컨테이너 기동 ==="
$DC up -d

echo "=== [5/5] 헬스체크 대기 (최대 60초) ==="
for i in $(seq 1 12); do
    sleep 5
    # /health 는 인증 불필요
    if curl -fsS --max-time 3 http://127.0.0.1:8000/health >/dev/null 2>&1; then
        echo "✅ 대시보드 응답 확인: $(curl -s http://127.0.0.1:8000/health)"
        break
    fi
    echo "대기 중... ($((i*5))s)"
done

echo ""
echo "=== 컨테이너 상태 ==="
$DC ps

echo ""
echo "=== 최근 dashboard 로그 (문제 시 확인) ==="
$DC logs --tail 30 dashboard || true

echo ""
echo "배포 완료. 외부 접속 확인:  curl http://<서버IP>:8000/health"
