#!/bin/bash
# worktree-cleanup.sh — worktree + DB + 버킷 디렉토리 삭제
#
# 사용법: ./scripts/worktree-cleanup.sh <branch-name>
# 예시:   ./scripts/worktree-cleanup.sh fix/s3-paths
#
# 수행 내용:
#   1. git worktree 삭제
#   2. 로컬 DB 삭제 (dropdb taskmanager_{sanitized_branch})
#   3. 로컬 버킷 디렉토리 삭제 (~/.taskmanager/bucket/worktree/{branch}/)
#   4. git branch 삭제 (선택)
set -euo pipefail

BRANCH="${1:?Usage: $0 <branch-name>}"

# ── 경로 계산 ──────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKTREE_DIR="$SERVER_DIR/.claude/worktrees/$BRANCH"

SANITIZED="$(echo "$BRANCH" | tr '/' '-' | tr -cd 'a-zA-Z0-9_-')"
DB_NAME="taskmanager_${SANITIZED}"

BUCKET_DIR="$HOME/.taskmanager/bucket/worktree/$BRANCH"

# ── .env에서 DB 접속 정보 추출 ─────────────────────────────
ENV_FILE="$SERVER_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    ORIG_URL="$(grep '^DATABASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
    DB_USER="$(echo "$ORIG_URL" | sed -n 's|.*://\([^:]*\):.*|\1|p')"
    DB_PASS="$(echo "$ORIG_URL" | sed -n 's|.*://[^:]*:\([^@]*\)@.*|\1|p')"
    DB_HOST="$(echo "$ORIG_URL" | sed -n 's|.*@\([^:]*\):.*|\1|p')"
    DB_PORT="$(echo "$ORIG_URL" | sed -n 's|.*:\([0-9]*\)/[^/]*$|\1|p')"
    export PGPASSWORD="$DB_PASS"
fi

echo "=== Worktree Cleanup ==="
echo "Branch:    $BRANCH"
echo "Worktree:  $WORKTREE_DIR"
echo "DB:        $DB_NAME"
echo "Bucket:    $BUCKET_DIR"
echo ""

# ── 1. Git worktree 삭제 ──────────────────────────────────
if [ -d "$WORKTREE_DIR" ]; then
    echo "Removing worktree..."
    cd "$SERVER_DIR"
    git worktree remove "$WORKTREE_DIR" --force 2>/dev/null || true
    echo "OK: worktree removed"
else
    echo "SKIP: worktree not found"
fi

# ── 2. DB 삭제 ─────────────────────────────────────────────
if [ -n "${DB_HOST:-}" ]; then
    if psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -lqt | cut -d \| -f 1 | grep -qw "$DB_NAME"; then
        echo "Dropping database $DB_NAME..."
        dropdb -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$DB_NAME"
        echo "OK: database dropped"
    else
        echo "SKIP: database $DB_NAME not found"
    fi
fi

# ── 3. 버킷 디렉토리 삭제 ─────────────────────────────────
if [ -d "$BUCKET_DIR" ]; then
    echo "Removing bucket dir..."
    rm -rf "$BUCKET_DIR"
    echo "OK: bucket dir removed"
else
    echo "SKIP: bucket dir not found"
fi

# ── 4. Git branch 삭제 (선택) ─────────────────────────────
echo ""
read -p "Delete branch '$BRANCH'? (y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    cd "$SERVER_DIR"
    git branch -d "$BRANCH" 2>/dev/null || git branch -D "$BRANCH"
    echo "OK: branch deleted"
fi

echo ""
echo "=== Cleanup Complete ==="
