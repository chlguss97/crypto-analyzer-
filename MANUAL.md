# OKX CryptoAnalyzer 운영 매뉴얼

## 목차
1. [클라우드 서버 운영 (실전)](#1-클라우드-서버-운영)
2. [새 PC에서 로컬 설치 (개발/테스트)](#2-새-pc에서-로컬-설치)
3. [클라우드 서버 새로 구축](#3-클라우드-서버-새로-구축)
4. [자주 쓰는 명령어](#4-자주-쓰는-명령어)
5. [운영 자동화 (헬스체크/로그 디지스트/Commit Log)](#5-운영-자동화)
6. [사이즈/SL 설정 (margin_loss_cap)](#6-사이즈-sl-설정)
7. [수동 SL/TP 변경 (대시보드)](#7-수동-sltp-변경)
8. [문제 해결](#8-문제-해결)

---

## 1. 클라우드 서버 운영

### 서버 정보
- **IP**: `207.148.120.103`
- **위치**: Singapore (Vultr)
- **OS**: Ubuntu 22.04 LTS
- **사양**: 1 vCPU, 1GB RAM, 25GB SSD ($6/월)

### 접속
```bash
ssh root@207.148.120.103
```

### 대시보드 접속
- URL: http://207.148.120.103:8000
- 로그인: `.env`의 `DASHBOARD_USER` / `DASHBOARD_PASS`

### 봇 시작/종료
```bash
cd /root/crypto-bot

docker compose up -d         # 시작
docker compose down          # 종료
docker compose restart bot   # 봇만 재시작
docker compose ps            # 상태 확인
```

### 로그 보기
```bash
docker compose logs -f bot              # 실시간 로그
docker compose logs --tail 100 bot      # 최근 100줄
docker compose logs bot | grep ERROR    # 에러만
```

### 코드 업데이트
```bash
cd /root/crypto-bot
git pull
docker compose up -d --build
```

> ⚠️ 빌드 없이 코드만 반영하면 되는 경우 `docker compose restart bot` 으로 충분.

### 헬스체크 (한 줄 상태)
```bash
cd /root/crypto-bot
./scripts/health_check.sh         # 텍스트 출력
./scripts/health_check.sh json    # JSON
```
출력 항목: 컨테이너 상태 / heartbeat 경과 / 마지막 시그널 평가 / 활성 포지션 / 자동매매 / 잔고 / 학습 상태  
종합 판정: ✅ OK / 🚨 DOWN / ⚠️ STALE (heartbeat 3분 이상 멈춤)

### 로그 디지스트 푸시 (cron)
```bash
crontab -e
# 15분마다 logs 브랜치에 디지스트 푸시:
*/15 * * * * cd /root/crypto-bot && ./scripts/log_push.sh 20 >> /tmp/log_push.log 2>&1
```
- `digests/latest.txt` 가 항상 최신, `digests/{ts}.txt` 에 이력 (7일 자동 삭제)
- 헬스체크 결과가 디지스트 맨 위에 자동 포함 — 봇 다운 즉시 감지 가능
- main 브랜치 오염 없음 (logs 브랜치 분리)

### ML 모델 백업/복원
```bash
# 백업 (서버 → 로컬 PC)
scp root@207.148.120.103:/root/crypto-bot/data/adaptive_v2_*.pkl ./backup/

# 복원 (로컬 PC → 서버)
scp ./backup/adaptive_v2_*.pkl root@207.148.120.103:/root/crypto-bot/data/
docker compose restart bot
```

---

## 2. 새 PC에서 로컬 설치

### 사전 요구사항
- Python 3.11+
- Docker Desktop (또는 Redis 단독 설치)
- Git

### 설치 순서

**1. 코드 다운로드**
```bash
git clone https://github.com/chlguss97/crypto-analyzer-.git
cd crypto-analyzer-

# 필수: post-commit hook 활성화 (COMMIT_LOG.md 자동 갱신)
git config core.hooksPath .githooks
```

**2. Python 의존성 설치**
```bash
pip install -r requirements.txt
```

**3. Redis 실행 (Docker)**
```bash
docker run -d --name redis -p 6379:6379 redis:7-alpine
```

**4. .env 파일 작성**
```bash
cp .env.example .env
```

`.env` 편집:
```
OKX_API_KEY=
OKX_SECRET_KEY=
OKX_PASSPHRASE=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DASHBOARD_USER=admin
DASHBOARD_PASS=
```

> 로컬 테스트만 하면 OKX 키 없이도 동작 (가상매매 + 학습만)
> DASHBOARD_PASS 비우면 인증 없음 (로컬용)

**5. 봇 실행**
```bash
python src/main.py
```

봇 + 대시보드가 함께 시작됩니다.

**6. 대시보드 접속**
```
http://localhost:8000
```

### 종료
- 터미널에서 `Ctrl + C`
- 또는 PowerShell: `taskkill /F /IM python.exe`

---

## 3. 클라우드 서버 새로 구축

### Step 1: SSH 키 생성 (PC에서)
```powershell
ssh-keygen -t ed25519 -C "crypto-bot"
# Enter 3번
type C:\Users\user\.ssh\id_ed25519.pub
```
출력된 공개키 복사

### Step 2: Vultr 서버 생성
1. https://www.vultr.com 가입
2. Deploy New Server
3. 옵션:
   - Type: **Shared CPU**
   - Location: **Singapore**
   - OS: **Ubuntu 22.04 LTS x64**
   - Plan: **vhp-1c-1gb** ($6/월)
   - SSH Keys: 복사한 공개키 등록
   - Hostname: `crypto-bot`
4. Deploy
5. **서버 IP 메모**

### Step 3: 서버 초기 설정
```bash
ssh root@<서버IP>

# 시스템 업데이트
apt update && apt upgrade -y

# 시간대
timedatectl set-timezone Asia/Seoul

# 방화벽
ufw allow 22/tcp
ufw allow 8000/tcp
ufw --force enable

# fail2ban (SSH 방어)
apt install -y fail2ban
systemctl enable fail2ban
systemctl start fail2ban

# Docker 설치
curl -fsSL https://get.docker.com | sh
systemctl enable docker
```

### Step 4: 코드 배포
```bash
cd /root
git clone https://github.com/chlguss97/crypto-analyzer-.git crypto-bot
cd crypto-bot

# 필수: post-commit hook 활성화 (COMMIT_LOG.md 자동 갱신)
git config core.hooksPath .githooks
```

### Step 5: docker-compose.yml 생성
```bash
cat > docker-compose.yml << 'EOF'
services:
  redis:
    image: redis:7-alpine
    restart: always
    volumes:
      - redis_data:/data

  bot:
    build: .
    restart: always
    depends_on:
      - redis
    env_file:
      - .env
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
      - ./config:/app/config
    environment:
      - REDIS_HOST=redis

volumes:
  redis_data:
EOF
```

### Step 6: .env 작성
```bash
nano .env
```
필수 항목:
```
OKX_API_KEY=...
OKX_SECRET_KEY=...
OKX_PASSPHRASE=...
DASHBOARD_USER=admin
DASHBOARD_PASS=강력한비밀번호
```

저장: `Ctrl+X` → `Y` → Enter

### Step 7: ML 모델 업로드 (선택)
기존 학습된 모델이 있으면 PC에서:
```powershell
scp C:\Users\user\Desktop\claude\data\adaptive_v2_*.pkl root@<서버IP>:/root/crypto-bot/data/
```

### Step 8: 빌드 + 실행
```bash
docker compose up -d --build
docker compose logs -f bot
```

`봇 시작` 메시지 확인 후 `Ctrl+C`로 빠져나오기.

### Step 9: 대시보드 접속 확인
```
http://<서버IP>:8000
```

### Step 10: OKX API 키 보안 설정
거래소에서:
1. **Withdraw 권한 OFF** (필수!)
2. **IP 화이트리스트**에 서버 IP 추가
3. Read + Trade 권한만 ON

---

## 4. 자주 쓰는 명령어

### 봇 관리
```bash
docker compose ps                    # 컨테이너 상태
docker compose logs -f bot           # 실시간 로그
docker compose logs --tail 100 bot   # 최근 100줄
docker compose restart bot           # 봇 재시작
docker compose down                  # 전체 종료
docker compose up -d                 # 전체 시작
docker compose up -d --build         # 재빌드 + 시작
```

### 서버 관리
```bash
df -h                # 디스크 사용량
free -h              # 메모리 사용량
htop                 # CPU/프로세스 (q로 종료)
ufw status           # 방화벽 상태
systemctl status docker  # Docker 상태
```

### 코드 업데이트
```bash
cd /root/crypto-bot
git pull
docker compose up -d --build
```

### 데이터 백업
```bash
# 서버에서 ML 모델 백업
scp /root/crypto-bot/data/adaptive_v2_*.pkl backup-server:~/

# 또는 PC로 다운로드 (PC에서)
scp root@<서버IP>:/root/crypto-bot/data/*.pkl ./backup/
```

### 컨테이너 내부 접근
```bash
docker compose exec bot bash         # 봇 컨테이너 진입
docker compose exec redis redis-cli  # Redis CLI
```

---

## 5. 운영 자동화

### 헬스체크 스크립트
- `scripts/health_check.sh` — 봇 상태 한 줄 요약 (text/json)
- `digests/latest.txt` 의 첫 블록에 자동 포함됨
- 텔레그램/모니터링 통합 시 json 모드 활용

### 로그 디지스트 (logs 브랜치 자동 푸시)
- `scripts/log_digest.sh N` — 최근 N분 로그를 카테고리별 요약 (트레이딩 이벤트, 알고 주문, ERROR/WARNING, 진입 거부 통계)
- `scripts/log_push.sh N` — 위 결과를 GitHub `logs` 브랜치 `digests/` 에 자동 push
- cron 권장: `*/15 * * * * cd /root/crypto-bot && ./scripts/log_push.sh 20`
- Claude 가 다른 PC에서 작업할 때 `git show origin/logs:digests/latest.txt` 로 즉시 최신 상태 확인

### COMMIT_LOG.md (자동 갱신)
- `scripts/update_commit_log.sh` — `git log` 에서 전체 커밋을 날짜별로 정리해 `COMMIT_LOG.md` 생성
- `.githooks/post-commit` 가 매 커밋 후 자동 실행 (PC 당 1회 `git config core.hooksPath .githooks` 필요)
- **lag-by-1 허용** — 자기 커밋은 자기 안에 못 들어감, 다음 커밋이 따라잡음
- 수동 편집 금지 — `git log` 의 단일 미러
- 새 PC/서버 셋업 직후 한 번 수동 실행해서 초기 생성: `bash scripts/update_commit_log.sh`

### 학습 스케줄 (현재)
| 시간 (UTC) | 시간 (KST) | 종류 |
|---|---|---|
| 22:00 | 07:00 (다음날) | 일일 대량 학습 + 백테스트 (+ 일요일 메타 학습) |
| 04:00 | 13:00 | 세션 경량 학습 (Asia 점심) |
| 11:00 | 20:00 | 세션 경량 학습 (EU 피크 직후) |

**학습 중 보호**: `redis sys:learning=1` 동안 신규 진입 차단, 활성 포지션은 5초 폴링으로 가속화 (`cb1758a`)

---

## 6. 사이즈/SL 설정

### sizing_mode (config/config.yaml)
두 가지 모드 중 하나 선택:

#### `margin_loss_cap` (기본값, 작은 계좌 권장)
```yaml
sizing_mode: margin_loss_cap
margin_pct: 0.95              # 잔고 대비 마진 비율 (작은 계좌 95%)
max_margin_loss_pct: 10.0     # SL 손실 한도 (마진의 %)
use_indicator_sl: true        # 매물대 SL 우선 사용
tp1_margin_gain_pct: 15.0     # TP1 마진 +15% 익절
trail_margin_pct: 5.0         # 러너 트레일 마진 5% 거리
trail_min_price_pct: 0.2      # 트레일 최소 가격 거리 (노이즈 방어)
min_indicator_sl_price_pct: 0.05  # 매물대 SL 최소 가격 거리 (즉시청산 방지)
```

작동:
- SL 가격 거리 = `max_margin_loss_pct / leverage` (예: 25x + 10% → 가격 0.4%)
- 매물대 SL 이 더 가까우면 매물대 사용 (보수적), 더 멀면 마진 한도 사용
- TP/트레일도 모두 마진 % 기준 → 가격 거리 = `% / leverage`

예시 — 잔고 $30, 25x 레버리지:
- 마진 = $30 × 0.95 = $28.5
- 사이즈 = floor($28.5 × 25 / $70k / 0.01) × 0.01 = 0.01 BTC
- SL 가격 거리 = 0.4% (마진 -10% = -$2.85)
- TP1 가격 거리 = 0.6% (마진 +15% = +$4.27)

#### `risk_per_trade` (옛 모드, 큰 계좌 권장)
```yaml
sizing_mode: risk_per_trade
risk_per_trade: 0.005   # 잔고의 0.5%
```

### 활성 모델 (Swing/Scalp)
```bash
# 현재 설정 확인
docker compose exec redis redis-cli get sys:active_model

# 변경
docker compose exec redis redis-cli set sys:active_model scalp   # scalp 만
docker compose exec redis redis-cli set sys:active_model swing   # swing 만
docker compose exec redis redis-cli set sys:active_model both    # 둘 다
```
**기본값: `scalp`** (스캘핑 중점, `fbb6985` 변경)

### 자동매매 ON/OFF
```bash
docker compose exec redis redis-cli set sys:autotrading on
docker compose exec redis redis-cli set sys:autotrading off
```
대시보드 토글로도 가능. 봇 시작 시 기본 OFF.

---

## 7. 수동 SL/TP 변경

대시보드(또는 API)에서 활성 포지션의 SL/TP 가격을 직접 수정 가능. 봇의 트레일/self-heal 이 사용자 수정을 존중함 (덮어쓰지 않음).

### API
```bash
# SL 변경
curl -u admin:PASS -X POST http://207.148.120.103:8000/api/position/sl \
  -H "Content-Type: application/json" \
  -d '{"symbol": "BTC/USDT:USDT", "price": 71200}'

# TP 변경
curl -u admin:PASS -X POST http://207.148.120.103:8000/api/position/tp \
  -H "Content-Type: application/json" \
  -d '{"symbol": "BTC/USDT:USDT", "price": 73500}'
```

### Sanity Check (자동)
- SL/TP 방향이 포지션 방향과 일치하는지 (롱이면 SL<entry<TP)
- 마진 손실이 50% 이하인지
- 위반 시 거부 + 사유 반환

### Override 동작
- `manual_sl_override` 플래그 — 트레일/self-heal 이 이 SL 을 덮어쓰지 않음
- TP1 hit 후 본전 이동(반익본절)은 봇이 자동으로 override 리셋

---

## 8. 문제 해결

### 대시보드 접속 안 됨
```bash
docker compose ps                    # 봇 실행 중인지
docker compose logs --tail 50 bot    # 에러 확인
ufw status                           # 방화벽 8000 열려있는지
netstat -tlnp | grep 8000            # 포트 LISTEN 확인
```

### 봇이 계속 재시작됨
```bash
docker compose logs --tail 100 bot   # 에러 메시지 확인
```
주요 원인:
- `.env` 파일 형식 오류
- API 키 만료/잘못됨
- Redis 연결 실패

### 메모리 부족
```bash
free -h
docker stats                         # 컨테이너별 사용량
```
1GB로 부족하면 Vultr에서 2GB로 업그레이드 (재부팅 필요)

### 디스크 부족
```bash
df -h
docker system prune -a               # 안 쓰는 이미지 정리
```

### Git pull 충돌
```bash
git checkout .                       # 로컬 변경 버리기
git pull
```

### Redis 연결 실패
```bash
docker compose ps                    # Redis 실행 중?
docker compose logs redis            # Redis 로그
docker compose restart redis         # 재시작
```

### ML 모델 손상
```bash
# 백업에서 복원
scp ./backup/adaptive_v2_*.pkl root@<서버IP>:/root/crypto-bot/data/
docker compose restart bot
```

---

## 보안 체크리스트

서버 배포 시 반드시 확인:

- [ ] OKX API 키: **Withdraw 권한 OFF**
- [ ] OKX API 키: **IP 화이트리스트**에 서버 IP만 등록
- [ ] `.env`의 `DASHBOARD_PASS` 설정 (16자 이상 강력한 비밀번호)
- [ ] SSH 키 인증 사용 (비밀번호 로그인 차단)
- [ ] 방화벽 활성화 (22, 8000만 열기)
- [ ] fail2ban 설치
- [ ] `.env` 파일이 git에 안 올라감 확인

---

## 일일 모니터링 항목

대시보드에서 매일 체크:

1. **봇 상태**: RUNNING / 잔고
2. **ML 정확도**: OOS 70% 이상 유지되는지
3. **레짐 모델 성능**: 각 레짐별 승률
4. **실거래 P&L**: 일일/주간 손실 한도 안 넘었는지
5. **Paper Trading 승률**: 가상매매 결과 추세
6. **에러 로그**: `docker compose logs bot | grep ERROR`

---

## 비용

- **Vultr 서버**: $6/월 + Backups $1.2/월 = **$7.2/월**
- **OKX 거래 수수료**: 매매 시 발생 (편도 0.05%)
- **OKX 펀딩비**: 8시간마다 발생 (가변)

---

## 연락처/링크

- GitHub: https://github.com/chlguss97/crypto-analyzer-
- Vultr: https://my.vultr.com
- OKX: https://www.okx.com
- Vultr 서버 콘솔: https://my.vultr.com/subs/?id=<서버ID>
