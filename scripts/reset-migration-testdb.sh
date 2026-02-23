#!/usr/bin/env bash
# =============================================================================
# reset-migration-testdb.sh
# MySQL 8.0→8.4 마이그레이션 테스트용 Docker DB 초기화 스크립트
#
# 사용법:
#   ./scripts/reset-migration-testdb.sh
#   ./scripts/reset-migration-testdb.sh --dump ~/path/to/dump.sql
#   ./scripts/reset-migration-testdb.sh --no-import   (컨테이너만 재생성)
#   ./scripts/reset-migration-testdb.sh --dry-run     (실행 계획만 출력)
#
# 환경 요건:
#   - Docker 실행 중
#   - mysql 클라이언트 설치됨 (PATH에 있어야 함)
#   - 덤프 파일: ~/dataflare_dump.sql (--dump 로 변경 가능)
# =============================================================================

set -euo pipefail

# ─── 설정값 (필요 시 수정) ──────────────────────────────────────────────────
CONTAINER_NAME="migration-test"
MYSQL_PORT=3390
MYSQL_ROOT_PASSWORD="test"
MYSQL_DATABASE="dataflare"
MYSQL_IMAGE="mysql:8.0"
DEFAULT_DUMP_FILE="$HOME/dataflare_dump.sql"
READY_TIMEOUT=60          # MySQL 준비 대기 최대 초
# ───────────────────────────────────────────────────────────────────────────

# ─── 인자 파싱 ──────────────────────────────────────────────────────────────
DUMP_FILE="$DEFAULT_DUMP_FILE"
DO_IMPORT=true
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dump)
      DUMP_FILE="$2"
      shift 2
      ;;
    --no-import)
      DO_IMPORT=false
      shift
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    -h|--help)
      sed -n '2,14p' "$0"
      exit 0
      ;;
    *)
      echo "알 수 없는 옵션: $1 (--help 참고)" >&2
      exit 1
      ;;
  esac
done
# ───────────────────────────────────────────────────────────────────────────

# ─── 색상 출력 헬퍼 ─────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERR]${NC}  $*" >&2; }
# ───────────────────────────────────────────────────────────────────────────

# ─── dry-run 모드: 실행 계획만 출력 ─────────────────────────────────────────
if $DRY_RUN; then
  echo "=== Dry-run 실행 계획 ==="
  echo "  1. docker rm -f $CONTAINER_NAME"
  echo "  2. docker run -d --name $CONTAINER_NAME -p ${MYSQL_PORT}:3306 \\"
  echo "       -e MYSQL_ROOT_PASSWORD=$MYSQL_ROOT_PASSWORD \\"
  echo "       -e MYSQL_DATABASE=$MYSQL_DATABASE \\"
  echo "       $MYSQL_IMAGE (+ innodb 최적화 플래그)"
  if $DO_IMPORT; then
    echo "  3. mysql -h 127.0.0.1 -P $MYSQL_PORT ... $MYSQL_DATABASE < $DUMP_FILE"
  else
    echo "  3. (임포트 스킵 - --no-import)"
  fi
  echo ""
  echo "덤프 파일: $DUMP_FILE"
  [[ -f "$DUMP_FILE" ]] && echo "  → 파일 존재: $(du -h "$DUMP_FILE" | cut -f1)" \
                        || echo "  → 파일 없음!"
  exit 0
fi
# ───────────────────────────────────────────────────────────────────────────

echo ""
echo "════════════════════════════════════════════════════════"
echo "  MySQL 8.0 마이그레이션 테스트 DB 초기화"
echo "  컨테이너: $CONTAINER_NAME  포트: $MYSQL_PORT"
echo "════════════════════════════════════════════════════════"
echo ""

# ─── 사전 검증 ──────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
  error "docker 명령을 찾을 수 없습니다."
  exit 1
fi

if $DO_IMPORT; then
  if [[ ! -f "$DUMP_FILE" ]]; then
    error "덤프 파일을 찾을 수 없습니다: $DUMP_FILE"
    error "--dump <경로> 옵션으로 경로를 지정하세요."
    exit 1
  fi
  DUMP_SIZE=$(du -h "$DUMP_FILE" | cut -f1)
  info "덤프 파일: $DUMP_FILE ($DUMP_SIZE)"
fi
# ───────────────────────────────────────────────────────────────────────────

# ─── Step 1: 기존 컨테이너 제거 ─────────────────────────────────────────────
info "Step 1/4: 기존 컨테이너 제거..."
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
  docker rm -f "$CONTAINER_NAME" > /dev/null
  success "컨테이너 '$CONTAINER_NAME' 제거됨"
else
  info "컨테이너 '$CONTAINER_NAME' 없음 (스킵)"
fi
# ───────────────────────────────────────────────────────────────────────────

# ─── Step 2: MySQL 8.0 컨테이너 생성 ────────────────────────────────────────
info "Step 2/4: MySQL $MYSQL_IMAGE 컨테이너 생성..."
docker run -d \
  --name "$CONTAINER_NAME" \
  -p "${MYSQL_PORT}:3306" \
  -e MYSQL_ROOT_PASSWORD="$MYSQL_ROOT_PASSWORD" \
  -e MYSQL_DATABASE="$MYSQL_DATABASE" \
  "$MYSQL_IMAGE" \
  --innodb-flush-log-at-trx-commit=2 \
  --innodb-doublewrite=0 \
  --innodb-buffer-pool-size=2G \
  --skip-log-bin \
  --max-allowed-packet=512M \
  > /dev/null

success "컨테이너 생성됨 (포트 $MYSQL_PORT)"
# ───────────────────────────────────────────────────────────────────────────

# ─── Step 3: MySQL 준비 대기 ────────────────────────────────────────────────
info "Step 3/4: MySQL 준비 대기 (최대 ${READY_TIMEOUT}초)..."
ELAPSED=0
while true; do
  if docker exec "$CONTAINER_NAME" \
      mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "SELECT 1" 2>/dev/null | grep -q 1; then
    success "MySQL 준비 완료 (${ELAPSED}초)"
    break
  fi
  if (( ELAPSED >= READY_TIMEOUT )); then
    error "MySQL이 ${READY_TIMEOUT}초 내에 준비되지 않았습니다."
    docker logs --tail=20 "$CONTAINER_NAME" >&2
    exit 1
  fi
  sleep 1
  (( ELAPSED++ ))
  printf "."
done
# ───────────────────────────────────────────────────────────────────────────

# ─── Step 4: 덤프 임포트 ────────────────────────────────────────────────────
if $DO_IMPORT; then
  info "Step 4/4: 덤프 임포트 중... (시간이 걸릴 수 있습니다)"
  IMPORT_START=$(date +%s)

  mysql -h 127.0.0.1 -P "$MYSQL_PORT" \
    -uroot -p"$MYSQL_ROOT_PASSWORD" \
    --init-command="SET FOREIGN_KEY_CHECKS=0; SET UNIQUE_CHECKS=0; SET AUTOCOMMIT=0; SET sql_mode='';" \
    "$MYSQL_DATABASE" \
    < "$DUMP_FILE"

  IMPORT_END=$(date +%s)
  IMPORT_SEC=$(( IMPORT_END - IMPORT_START ))
  success "임포트 완료 (${IMPORT_SEC}초)"

  # 결과 확인
  TABLE_COUNT=$(mysql -h 127.0.0.1 -P "$MYSQL_PORT" -uroot -p"$MYSQL_ROOT_PASSWORD" \
    -sNe "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='$MYSQL_DATABASE'" 2>/dev/null)
  success "테이블 수: ${TABLE_COUNT}개"
else
  info "Step 4/4: 임포트 스킵 (--no-import)"
fi
# ───────────────────────────────────────────────────────────────────────────

echo ""
echo "════════════════════════════════════════════════════════"
echo -e "${GREEN}  초기화 완료!${NC}"
echo "  접속 정보:"
echo "    Host:     127.0.0.1"
echo "    Port:     $MYSQL_PORT"
echo "    User:     root"
echo "    Password: $MYSQL_ROOT_PASSWORD"
echo "    DB:       $MYSQL_DATABASE"
echo "════════════════════════════════════════════════════════"
echo ""
