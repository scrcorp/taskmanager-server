#!/bin/bash
# worktree-setup.sh — worktree + 격리된 DB + 버킷 디렉토리 생성
#
# 사용법: ./scripts/worktree-setup.sh <branch-name> [base-branch]
# 예시:   ./scripts/worktree-setup.sh fix/s3-paths dev
#
# 수행 내용:
#   1. git worktree 생성 (.claude/worktrees/{branch})
#   2. 로컬 DB 복사 (pg_dump dev → taskmanager_{sanitized_branch})
#   3. 로컬 버킷 디렉토리 생성 (~/.taskmanager/bucket/worktree/{branch}/)
#   4. worktree .env에 DATABASE_URL + LOCAL_BUCKET_DIR + LOCAL_FALLBACK_BUCKET_DIR 오버라이드
set -euo pipefail

BRANCH="${1:?Usage: $0 <branch-name> [base-branch]}"
BASE_BRANCH="${2:-dev}"

# ── 경로 계산 ──────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKTREE_DIR="$SERVER_DIR/.claude/worktrees/$BRANCH"

# DB명에 사용할 sanitized branch명 (슬래시 → 하이픈, 특수문자 제거)
SANITIZED="$(echo "$BRANCH" | tr '/' '-' | tr -cd 'a-zA-Z0-9_-')"
DB_NAME="taskmanager_${SANITIZED}"

# 로컬 버킷 경로
BUCKET_DIR="$HOME/.taskmanager/bucket/worktree/$BRANCH"
FALLBACK_BUCKET_DIR="$HOME/.taskmanager/bucket/dev"

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

echo "OK: .env configured"
echo ""
echo "=== Done ==="
echo "cd $WORKTREE_DIR"
echo "alembic upgrade head  # if you have new migrations"
