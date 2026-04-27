#!/bin/bash
# worktree-setup.sh — worktree + 격리된 DB + 버킷 디렉토리 생성
#
# 사용법: ./scripts/worktree-setup.sh <branch-name> [base-branch]
# 예시:   ./scripts/worktree-setup.sh fix/s3-paths dev
#
# 수행 내용:
#   1. git worktree 생성 (.claude/worktrees/{branch})
#   2. 로컬 DB 복사 (pg_dump dev → taskmanager_{sanitized_branch})
#   3. 로컬 버킷 디렉토리 생성 (프로젝트루트/bucket/worktree/{branch}/)
#   4. worktree .env에 DATABASE_URL + LOCAL_BUCKET_DIR + LOCAL_FALLBACK_BUCKET_DIR 오버라이드
set -euo pipefail

BRANCH="${1:?Usage: $0 <branch-name> [base-branch]}"
BASE_BRANCH="${2:-dev}"

# ── 경로 계산 ──────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# sanitized branch명 (슬래시 → 하이픈, 특수문자 제거) — 디렉토리/DB명에 공통 사용
SANITIZED="$(echo "$BRANCH" | tr '/' '-' | tr -cd 'a-zA-Z0-9_-')"
WORKTREE_DIR="$SERVER_DIR/.claude/worktrees/$SANITIZED"
DB_NAME="taskmanager_${SANITIZED}"

# 로컬 버킷 경로 — 프로젝트 루트(server/../) 아래에 통일
PROJECT_ROOT="$(cd "$SERVER_DIR/.." && pwd)"

# dev .env에서 LOCAL_BUCKET_DIR 읽기 (dev 버킷 = fallback)
DEV_BUCKET_DIR="$(grep '^LOCAL_BUCKET_DIR=' "$SERVER_DIR/.env" 2>/dev/null | cut -d= -f2-)"
if [ -z "$DEV_BUCKET_DIR" ]; then
    DEV_BUCKET_DIR="$PROJECT_ROOT/bucket/dev"
fi

BUCKET_DIR="$PROJECT_ROOT/bucket/worktree/$SANITIZED"
FALLBACK_BUCKET_DIR="$DEV_BUCKET_DIR"

# ── .env에서 원본 DB 정보 추출 ─────────────────────────────
ENV_FILE="$SERVER_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found"
    exit 1
fi

# DATABASE_URL 파싱: postgresql+asyncpg://user:pass@host:port/dbname
ORIG_URL="$(grep '^DATABASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
# asyncpg → psycopg2 호환 URL로 변환하여 host:port 추출
DB_USER="$(echo "$ORIG_URL" | sed -n 's|.*://\([^:]*\):.*|\1|p')"
DB_PASS="$(echo "$ORIG_URL" | sed -n 's|.*://[^:]*:\([^@]*\)@.*|\1|p')"
DB_HOST="$(echo "$ORIG_URL" | sed -n 's|.*@\([^:]*\):.*|\1|p')"
DB_PORT="$(echo "$ORIG_URL" | sed -n 's|.*:\([0-9]*\)/[^/]*$|\1|p')"
SOURCE_DB="$(echo "$ORIG_URL" | sed -n 's|.*/\([^/]*\)$|\1|p')"

export PGPASSWORD="$DB_PASS"

echo "=== Worktree Setup ==="
echo "Branch:      $BRANCH (from $BASE_BRANCH)"
echo "Worktree:    $WORKTREE_DIR"
echo "DB:          $DB_NAME (from $SOURCE_DB)"
echo "Bucket:      $BUCKET_DIR"
echo ""

# ── 1. Git worktree ────────────────────────────────────────
if [ -d "$WORKTREE_DIR" ]; then
    echo "SKIP: worktree already exists at $WORKTREE_DIR"
else
    echo "Creating worktree..."
    cd "$SERVER_DIR"
    git worktree add "$WORKTREE_DIR" -b "$BRANCH" "$BASE_BRANCH" 2>/dev/null || \
    git worktree add "$WORKTREE_DIR" "$BRANCH"
    echo "OK: worktree created"
fi

# ── 2. DB 복사 ─────────────────────────────────────────────
if psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -lqt | cut -d \| -f 1 | grep -qw "$DB_NAME"; then
    echo "SKIP: database $DB_NAME already exists"
else
    echo "Creating database $DB_NAME..."
    createdb -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$DB_NAME"
    echo "Copying data from $SOURCE_DB..."
    pg_dump -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$SOURCE_DB" | \
        psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -q "$DB_NAME"
    echo "OK: database copied"
fi

# ── 3. 버킷 디렉토리 ──────────────────────────────────────
mkdir -p "$BUCKET_DIR"
mkdir -p "$FALLBACK_BUCKET_DIR"
echo "OK: bucket dirs created"

# ── 4. worktree .env 생성 ─────────────────────────────────
WT_ENV="$WORKTREE_DIR/.env"
# 원본 .env 복사 후 오버라이드
cp "$ENV_FILE" "$WT_ENV"

# 기존 DATABASE_URL 교체
NEW_URL="postgresql+asyncpg://${DB_USER}:${DB_PASS}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
sed -i '' "s|^DATABASE_URL=.*|DATABASE_URL=${NEW_URL}|" "$WT_ENV"

# 버킷 설정 추가 (이미 있으면 교체, 없으면 추가)
grep -q '^LOCAL_BUCKET_DIR=' "$WT_ENV" && \
    sed -i '' "s|^LOCAL_BUCKET_DIR=.*|LOCAL_BUCKET_DIR=${BUCKET_DIR}|" "$WT_ENV" || \
    echo "LOCAL_BUCKET_DIR=${BUCKET_DIR}" >> "$WT_ENV"

grep -q '^LOCAL_FALLBACK_BUCKET_DIR=' "$WT_ENV" && \
    sed -i '' "s|^LOCAL_FALLBACK_BUCKET_DIR=.*|LOCAL_FALLBACK_BUCKET_DIR=${FALLBACK_BUCKET_DIR}|" "$WT_ENV" || \
    echo "LOCAL_FALLBACK_BUCKET_DIR=${FALLBACK_BUCKET_DIR}" >> "$WT_ENV"

# ── 포트 자동 스캔 — dev 와 다른 워크트리 와 모두 겹치지 않도록 ─
# dev 는 58000/53000/58080 고정. 워크트리는 58100~58999 / 53100~53999 / 58180~58999 에서 첫 빈 자리.
port_free() {
    # lsof 는 빠르지만 없을 수도 있어 nc 로 폴백
    if command -v lsof >/dev/null 2>&1; then
        ! lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
    else
        ! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
    fi
}

find_free_port() {
    local start="$1" end="$2"
    for ((p=start; p<=end; p++)); do
        if port_free "$p"; then echo "$p"; return 0; fi
    done
    echo "ERROR: no free port in [$start-$end]" >&2
    return 1
}

# 이미 포트가 기록돼 있으면 유지, 없을 때만 스캔
EXISTING_SERVER_PORT="$(grep -E '^DEV_SERVER_PORT=' "$WT_ENV" | tail -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
EXISTING_ADMIN_PORT="$(grep -E '^DEV_ADMIN_PORT='  "$WT_ENV" | tail -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
EXISTING_APP_PORT="$(grep -E '^DEV_APP_PORT='     "$WT_ENV" | tail -1 | cut -d= -f2- | tr -d '[:space:]' || true)"

if [ -z "$EXISTING_SERVER_PORT" ]; then
    DEV_SERVER_PORT=$(find_free_port 58100 58999)
    echo "DEV_SERVER_PORT=${DEV_SERVER_PORT}" >> "$WT_ENV"
else
    DEV_SERVER_PORT="$EXISTING_SERVER_PORT"
fi

if [ -z "$EXISTING_ADMIN_PORT" ]; then
    DEV_ADMIN_PORT=$(find_free_port 53100 53999)
    echo "DEV_ADMIN_PORT=${DEV_ADMIN_PORT}" >> "$WT_ENV"
else
    DEV_ADMIN_PORT="$EXISTING_ADMIN_PORT"
fi

if [ -z "$EXISTING_APP_PORT" ]; then
    DEV_APP_PORT=$(find_free_port 58180 58999)
    echo "DEV_APP_PORT=${DEV_APP_PORT}" >> "$WT_ENV"
else
    DEV_APP_PORT="$EXISTING_APP_PORT"
fi

echo "OK: .env configured (ports: server=$DEV_SERVER_PORT, admin=$DEV_ADMIN_PORT, app=$DEV_APP_PORT)"

# ── 4b. admin worktree + .env.local ───────────────────────
# admin/도 같은 브랜치로 worktree를 만들고 .env.local에 server 포트와
# 회사코드를 박아준다. (메인 admin/.env.local에서 NEXT_PUBLIC_COMPANY_CODE 복사.)
ADMIN_REPO="$PROJECT_ROOT/admin"
if [ -d "$ADMIN_REPO/.git" ] || [ -f "$ADMIN_REPO/.git" ]; then
    ADMIN_WT="$ADMIN_REPO/.claude/worktrees/$SANITIZED"
    if [ -d "$ADMIN_WT" ]; then
        echo "SKIP: admin worktree already exists at $ADMIN_WT"
    else
        echo "Creating admin worktree..."
        (cd "$ADMIN_REPO" && \
            git worktree add "$ADMIN_WT" -b "$BRANCH" "$BASE_BRANCH" 2>/dev/null || \
            git worktree add "$ADMIN_WT" "$BRANCH")
        echo "OK: admin worktree created"
    fi

    ADMIN_ENV_LOCAL="$ADMIN_WT/.env.local"
    if [ -f "$ADMIN_ENV_LOCAL" ]; then
        echo "SKIP: $ADMIN_ENV_LOCAL already exists"
    else
        # 메인 admin/.env.local에서 회사코드 가져오기 (없으면 빈 값)
        ADMIN_COMPANY_CODE=""
        if [ -f "$ADMIN_REPO/.env.local" ]; then
            ADMIN_COMPANY_CODE="$(grep -E '^NEXT_PUBLIC_COMPANY_CODE=' "$ADMIN_REPO/.env.local" | tail -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
        fi
        cat > "$ADMIN_ENV_LOCAL" <<EOF
NEXT_PUBLIC_API_URL=http://localhost:${DEV_SERVER_PORT}/api/v1
NEXT_PUBLIC_COMPANY_CODE=${ADMIN_COMPANY_CODE}
EOF
        if [ -z "$ADMIN_COMPANY_CODE" ]; then
            echo "OK: admin .env.local created (NEXT_PUBLIC_COMPANY_CODE empty — main admin/.env.local has no value)"
        else
            echo "OK: admin .env.local created (company code: $ADMIN_COMPANY_CODE)"
        fi
    fi
else
    echo "SKIP: admin/ not found at $ADMIN_REPO — skipping admin worktree"
fi

# ── 5. venv 생성 + 패키지 설치 ──────────────────────────────
if [ -d "$WORKTREE_DIR/.venv" ]; then
    echo "SKIP: .venv already exists"
else
    echo "Creating .venv..."
    python3 -m venv "$WORKTREE_DIR/.venv"
    echo "Installing requirements..."
    "$WORKTREE_DIR/.venv/bin/pip" install -q -r "$WORKTREE_DIR/requirements.txt"
    echo "OK: .venv created + packages installed"
fi

echo ""
echo "=== Done ==="
echo ""
echo "Ports: server=$DEV_SERVER_PORT  admin=$DEV_ADMIN_PORT  app=$DEV_APP_PORT"
echo ""
echo "로컬 개발 서버 띄우기:"
echo "  ./scripts/dev-up.sh -w $BRANCH"
echo ""
echo "또는 수동:"
echo "  cd $WORKTREE_DIR"
echo "  source .venv/bin/activate"
echo "  alembic upgrade head  # if you have new migrations"
