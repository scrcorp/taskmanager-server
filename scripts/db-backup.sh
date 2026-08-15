#!/bin/bash
# db-backup.sh — DB 논리 백업(pg_dump -Fc)을 떠서 S3 에 올린다.
#
# 왜 필요한가:
#   RDS 자동 백업(7일 PITR)은 인스턴스 통째 복구만 된다. 테이블 하나/행 몇 개를
#   되돌리려면 새 인스턴스를 띄워야 하고, 7일이 지난 손상은 아예 못 되돌린다.
#   특히 배포는 컨테이너 기동 시 `alembic upgrade head` 를 무인 실행하므로
#   (start.sh) 마이그레이션 직전 상태를 남겨두는 것이 유일한 실질 롤백 수단이다.
#
# 사용법:
#   ./scripts/db-backup.sh [label] [flags]
#
#   label   백업 목적을 파일명에 남기는 꼬리표. 기본 manual.
#           예) predeploy-a1b2c3d, daily, before-payroll-fix
#
#   --no-upload      덤프만 뜨고 S3 업로드는 건너뛴다(로컬 검증용, 파일 보존)
#   --keep-local     업로드 후에도 로컬 덤프 파일을 지우지 않는다
#   --env-file PATH  .env 경로 지정 (기본: repo 루트의 .env)
#   -h, --help       이 도움말
#
# 설정 (프로세스 환경변수 > .env 파일 순으로 읽는다):
#   DATABASE_URL           필수. postgresql+asyncpg:// 형태도 그대로 인식한다.
#   DB_BACKUP_S3_BUCKET    필수. 백업 전용 버킷 (파일 업로드 버킷과 분리할 것).
#   DB_BACKUP_S3_PREFIX    키 prefix. 기본 db
#   DB_BACKUP_S3_SSE       서버측 암호화. 기본 AES256. KMS 쓰면 aws:kms
#   DB_BACKUP_KMS_KEY_ID   SSE 가 aws:kms 일 때 키 ID
#   AWS_S3_REGION          aws CLI 리전 (없으면 CLI 기본 설정)
#   APP_ENV                파일명/키에 들어갈 환경 라벨. 기본 unknown
#   PG_CLIENT_IMAGE        host 에 pg_dump 가 없을 때 쓸 이미지. 기본 postgres:17-alpine
#   DB_BACKUP_MIN_BYTES    이보다 작은 덤프는 실패로 본다. 기본 65536
#   DB_BACKUP_TMP_DIR      덤프 임시 디렉토리. 기본 <repo>/.backup-tmp
#   DB_BACKUP_MIN_FREE_MB  이보다 여유공간이 적으면 덤프를 시도하지 않는다. 기본 1024
#   SKIP_DB_BACKUP=1       백업을 건너뛰고 성공으로 종료 (배포 긴급 탈출구)
#   DB_BACKUP_RETENTION_DAYS  설정하면 이 일수보다 오래된 백업을 S3 에서 지운다.
#                             비워두면 아무것도 지우지 않는다(기본).
#   DB_BACKUP_KEEP_MIN     나이와 무관하게 남길 최소 개수. 기본 7
#
# 오래된 백업 정리:
#   이번 백업이 업로드·검증까지 성공한 뒤에만 돈다(백업 실패한 날 옛것부터
#   지우는 사고 방지). 정리 실패는 배포를 막지 않는다.
#   IAM 에 s3:DeleteObject / s3:ListBucket 이 필요하다 — 권한을 주기 싫으면
#   이 값을 비워두고 S3 Lifecycle 규칙으로 만료시키는 쪽이 안전하다:
#
#     aws s3api put-bucket-lifecycle-configuration --bucket <백업버킷> \
#       --lifecycle-configuration file://scripts/db-backup-lifecycle.json
#     aws s3api get-bucket-lifecycle-configuration --bucket <백업버킷>   # 확인
#
#   (put 은 기존 규칙 전체를 덮어쓴다. 다른 규칙이 있으면 get 으로 받아 합칠 것.
#    S3 만료는 비동기라 삭제가 하루쯤 늦을 수 있다.)
#
# 종료 코드: 0 성공(또는 SKIP) / 그 외 실패. 배포 스크립트에서 fail-closed 로 쓴다.
#
# 정기 실행(EC2 crontab 예시 — 매일 11:10 UTC):
#   10 11 * * * cd ~/taskmanager-server && ./scripts/db-backup.sh daily >> ~/db-backup.log 2>&1

set -euo pipefail

# ── 오래된 백업 선별 (순수 함수) ───────────────────────────
# stdin: "YYYY-MM-DD<TAB>키" 줄들 / stdout: 지울 키들
# 삭제 로직이 이 스크립트에서 제일 위험한 부분이라 S3 없이 테스트할 수 있게
# 분리해 두고 --prune-select 로 직접 호출할 수 있게 했다(내부/테스트용).
#
# 두 가지 안전장치:
#   1) cutoff 이전 것만 지운다
#   2) keep_min 개는 나이와 무관하게 남긴다 — 날짜 계산이 틀리거나 목록이
#      이상하게 와도 전부 날아가지 않게 하는 바닥.
prune_select() {
    local cutoff="$1" keep_min="$2" lines total allowed
    lines="$(sort)"   # ISO 날짜라 사전순 = 오래된 것부터
    [ -n "$lines" ] || return 0
    total="$(printf '%s\n' "$lines" | grep -c . || true)"
    allowed=$(( total - keep_min ))
    [ "$allowed" -gt 0 ] || return 0
    printf '%s\n' "$lines" | awk -F'\t' -v cutoff="$cutoff" -v allowed="$allowed" \
        'NF >= 2 && $1 < cutoff && n < allowed { print $2; n++ }'
}

if [ "${1:-}" = "--prune-select" ]; then
    prune_select "${2:?cutoff}" "${3:?keep_min}"
    exit 0
fi

# ── 인자 파싱 ──────────────────────────────────────────────
LABEL=""
NO_UPLOAD=0
KEEP_LOCAL=0
ENV_FILE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --no-upload)  NO_UPLOAD=1 ;;
        --keep-local) KEEP_LOCAL=1 ;;
        --env-file)   ENV_FILE="${2:?--env-file needs a path}"; shift ;;
        -h|--help)    sed -n '2,45p' "$0"; exit 0 ;;
        -*)           echo "Unknown flag: $1" >&2; exit 2 ;;
        *)
            if [ -z "$LABEL" ]; then LABEL="$1"
            else echo "Unexpected arg: $1" >&2; exit 2; fi
            ;;
    esac
    shift
done
LABEL="${LABEL:-manual}"
# 파일명에 들어가므로 안전한 문자만 남긴다
LABEL="$(printf '%s' "$LABEL" | tr -c 'A-Za-z0-9._-' '-')"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"

log()  { printf '[db-backup %s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
die()  { printf '[db-backup] ERROR: %s\n' "$*" >&2; exit 1; }

if [ "${SKIP_DB_BACKUP:-0}" = "1" ]; then
    log "SKIP_DB_BACKUP=1 — 백업을 건너뛴다 (배포는 계속 진행됨)"
    exit 0
fi

# ── 설정 읽기: 프로세스 환경변수가 우선, 없으면 .env ────────
# .env 를 source 하지 않는다 — 값에 공백/JSON 이 섞여 있어 깨진다.
env_get() {
    local key="$1"
    local from_proc="${!key:-}"
    if [ -n "$from_proc" ]; then printf '%s' "$from_proc"; return; fi
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^[[:space:]]*${key}=//p" "$ENV_FILE" | tail -1 \
        | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

DATABASE_URL="$(env_get DATABASE_URL)"
BUCKET="$(env_get DB_BACKUP_S3_BUCKET)"
PREFIX="$(env_get DB_BACKUP_S3_PREFIX)";     PREFIX="${PREFIX:-db}"
SSE="$(env_get DB_BACKUP_S3_SSE)";           SSE="${SSE:-AES256}"
KMS_KEY="$(env_get DB_BACKUP_KMS_KEY_ID)"
REGION="$(env_get AWS_S3_REGION)"
APP_ENV="$(env_get APP_ENV)";                APP_ENV="${APP_ENV:-unknown}"
PG_IMAGE="$(env_get PG_CLIENT_IMAGE)";       PG_IMAGE="${PG_IMAGE:-postgres:17-alpine}"
MIN_BYTES="$(env_get DB_BACKUP_MIN_BYTES)";  MIN_BYTES="${MIN_BYTES:-65536}"
TMP_DIR="$(env_get DB_BACKUP_TMP_DIR)";      TMP_DIR="${TMP_DIR:-$ROOT/.backup-tmp}"
MIN_FREE_MB="$(env_get DB_BACKUP_MIN_FREE_MB)"; MIN_FREE_MB="${MIN_FREE_MB:-1024}"
RETENTION_DAYS="$(env_get DB_BACKUP_RETENTION_DAYS)"
KEEP_MIN="$(env_get DB_BACKUP_KEEP_MIN)";    KEEP_MIN="${KEEP_MIN:-7}"

[ -n "$DATABASE_URL" ] || die "DATABASE_URL 이 없다 (env 또는 $ENV_FILE)"
if [ "$NO_UPLOAD" = "0" ] && [ -z "$BUCKET" ]; then
    die "DB_BACKUP_S3_BUCKET 이 설정되지 않았다. 백업 전용 버킷을 지정할 것 (업로드용 AWS_S3_BUCKET 과 분리)."
fi

# SQLAlchemy 드라이버 접미사(+asyncpg 등)는 libpq 가 모르므로 떼어낸다
PGURL="$(printf '%s' "$DATABASE_URL" | sed -E 's|^postgresql\+[a-z0-9_]+://|postgresql://|; s|^postgres\+[a-z0-9_]+://|postgresql://|')"
case "$PGURL" in
    postgresql://*|postgres://*) ;;
    *) die "DATABASE_URL 형식을 인식할 수 없다: ${DATABASE_URL%%://*}://..." ;;
esac

# ── PG 클라이언트 실행기 결정 ──────────────────────────────
# host 에 pg_dump 가 있으면 그걸 쓰고, 없으면 컨테이너로 돌린다.
# (운영 api 이미지에는 postgresql-client 가 없어서 컨테이너 경로가 기본이다)
mkdir -p "$TMP_DIR"
# 실패로 남은 임시 덤프 정리. 정상 경로에선 업로드 후 지우지만, 중간에 죽으면
# 남아서 EC2 디스크를 먹는다. 덤프를 새로 뜨기 전에 치워야 공간이 확보된다.
find "$TMP_DIR" -maxdepth 1 -name '*.dump' -type f -mtime +2 -delete 2>/dev/null || true

TS="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP_NAME="htm-${APP_ENV}-${TS}-${LABEL}.dump"
DUMP_PATH="$TMP_DIR/$DUMP_NAME"

if command -v pg_dump >/dev/null 2>&1; then
    PG_MODE="host"
    IN_DUMP_PATH="$DUMP_PATH"
    pg_run() { PGURL="$PGURL" "$@"; }
    log "PG 클라이언트 모드: host ($(command -v pg_dump))"
elif command -v docker >/dev/null 2>&1; then
    PG_MODE="docker"
    IN_DUMP_PATH="/backup/$DUMP_NAME"
    pg_run() {
        docker run --rm \
            --user "$(id -u):$(id -g)" \
            -e "PGURL=$PGURL" \
            -v "$TMP_DIR:/backup" \
            "$PG_IMAGE" "$@"
    }
    log "PG 클라이언트 모드: docker ($PG_IMAGE)"
else
    die "pg_dump 도 docker 도 없다. PG 클라이언트를 설치하거나 docker 를 쓸 수 있게 할 것."
fi

# ── 프리플라이트: 접속 + 버전 호환 확인 ────────────────────
# pg_dump 는 서버보다 낮은 버전이면 덤프를 거부한다. 배포 직전에 알아채면
# 늦으므로 여기서 먼저 확인하고 조치 방법을 알려준다.
# 버전 문자열에는 배포판 꼬리표가 붙는다("17.10 (Debian ...)", "17.6 (Homebrew)").
# 끝 토큰이 아니라 첫 번째 숫자 덩어리를 뽑아야 한다.
ver_major() { printf '%s' "$1" | grep -oE '[0-9]+' | head -1; }

SERVER_VER="$(pg_run sh -c 'psql -d "$PGURL" -Atqc "show server_version"' 2>/dev/null | head -1 || true)"
[ -n "$SERVER_VER" ] || die "DB 접속 실패. DATABASE_URL / 보안그룹 / 네트워크를 확인할 것."
CLIENT_VER="$(pg_run sh -c 'pg_dump --version' | head -1)"
SERVER_MAJOR="$(ver_major "$SERVER_VER")"
CLIENT_MAJOR="$(ver_major "${CLIENT_VER#pg_dump (PostgreSQL)}")"
log "server PostgreSQL $SERVER_VER / client $CLIENT_VER"
[ -n "$SERVER_MAJOR" ] && [ -n "$CLIENT_MAJOR" ] || die "PG 버전을 읽지 못했다 (server='$SERVER_VER' client='$CLIENT_VER')"
if [ "$CLIENT_MAJOR" -lt "$SERVER_MAJOR" ]; then
    die "pg_dump($CLIENT_MAJOR) 가 서버($SERVER_MAJOR) 보다 낮다. PG_CLIENT_IMAGE=postgres:${SERVER_MAJOR}-alpine 로 올릴 것."
fi

# ── 덤프 ───────────────────────────────────────────────────
cleanup() {
    if [ "$KEEP_LOCAL" = "0" ] && [ "$NO_UPLOAD" = "0" ]; then
        rm -f "$DUMP_PATH"
    fi
}
trap cleanup EXIT

# 공간이 모자라면 pg_dump 는 반쯤 쓰다 ENOSPC 로 죽는다. 그 상태의 로그보다
# 여기서 "얼마나 남았고 뭘 지우면 되는지"를 말해주는 편이 훨씬 빨리 복구된다.
FREE_KB="$(df -Pk "$TMP_DIR" | awk 'NR==2 {print $4}')"
FREE_MB=$(( FREE_KB / 1024 ))
DB_SIZE="$(pg_run sh -c 'psql -d "$PGURL" -Atqc "select pg_size_pretty(pg_database_size(current_database()))"' 2>/dev/null | head -1 || true)"
log "디스크 여유 ${FREE_MB}MB / DB 크기 ${DB_SIZE:-unknown} (덤프는 압축되므로 DB 크기보다 훨씬 작다)"
if [ "$FREE_MB" -lt "$MIN_FREE_MB" ]; then
    die "디스크 여유공간 부족: ${FREE_MB}MB < ${MIN_FREE_MB}MB. 옛 도커 이미지/빌드 캐시를 회수할 것 — docker image prune -f && docker builder prune -f"
fi

log "덤프 시작 → $DUMP_NAME"
# -Fc: custom 포맷. 압축되어 있고 pg_restore 로 테이블 단위 선택 복구가 된다.
# --no-owner/--no-acl: 다른 계정/환경으로 복구할 때 소유자 오류로 막히지 않게.
pg_run sh -c 'pg_dump -d "$PGURL" -Fc --no-owner --no-acl --file "'"$IN_DUMP_PATH"'"'

[ -f "$DUMP_PATH" ] || die "덤프 파일이 생성되지 않았다: $DUMP_PATH"
SIZE="$(wc -c < "$DUMP_PATH" | tr -d ' ')"
if [ "$SIZE" -lt "$MIN_BYTES" ]; then
    die "덤프가 너무 작다(${SIZE}B < ${MIN_BYTES}B). 빈 DB 를 떴을 가능성이 있어 업로드하지 않는다."
fi
# 무결성 확인 — 목록을 읽을 수 있으면 pg_restore 가 해석 가능한 파일이다
pg_run sh -c 'pg_restore --list "'"$IN_DUMP_PATH"'" > /dev/null'
log "덤프 완료: ${SIZE} bytes (pg_restore 판독 OK)"

if [ "$NO_UPLOAD" = "1" ]; then
    log "--no-upload — S3 업로드를 건너뛴다. 파일: $DUMP_PATH"
    exit 0
fi

# ── 업로드 ─────────────────────────────────────────────────
# host 의 aws CLI 를 우선 쓴다. 컨테이너 fallback 은 IMDS hop limit 때문에
# EC2 IAM role 을 못 읽을 수 있으니(기본 hop=1) host 설치를 권장한다.
if command -v aws >/dev/null 2>&1; then
    AWS_DUMP_PATH="$DUMP_PATH"
    aws_run() { aws "$@"; }
elif command -v docker >/dev/null 2>&1; then
    log "host 에 aws CLI 가 없어 컨테이너로 업로드한다 (IAM role 을 못 읽으면 host 설치 필요)"
    AWS_DUMP_PATH="/backup/$DUMP_NAME"
    aws_run() { docker run --rm -v "$TMP_DIR:/backup" amazon/aws-cli "$@"; }
else
    die "aws CLI 도 docker 도 없어 업로드할 수 없다."
fi

KEY="${PREFIX}/${APP_ENV}/$(date -u +%Y/%m)/${DUMP_NAME}"
S3_URI="s3://${BUCKET}/${KEY}"

CP_ARGS=(s3 cp "$AWS_DUMP_PATH" "$S3_URI" --only-show-errors)
if [ "$SSE" = "aws:kms" ]; then
    CP_ARGS+=(--sse aws:kms)
    [ -n "$KMS_KEY" ] && CP_ARGS+=(--sse-kms-key-id "$KMS_KEY")
else
    CP_ARGS+=(--sse "$SSE")
fi
[ -n "$REGION" ] && CP_ARGS+=(--region "$REGION")

log "업로드 → $S3_URI"
aws_run "${CP_ARGS[@]}"

# 업로드가 실제로 그 크기로 올라갔는지 확인한다 (조용한 실패 방지)
HEAD_ARGS=(s3api head-object --bucket "$BUCKET" --key "$KEY" --query ContentLength --output text)
[ -n "$REGION" ] && HEAD_ARGS+=(--region "$REGION")
REMOTE_SIZE="$(aws_run "${HEAD_ARGS[@]}" | tr -d '\r')"
[ "$REMOTE_SIZE" = "$SIZE" ] || die "업로드 크기 불일치 (local=${SIZE} remote=${REMOTE_SIZE})"

log "완료: $S3_URI (${SIZE} bytes)"

# ── 오래된 백업 정리 ───────────────────────────────────────
# 반드시 "이번 백업이 성공적으로 올라간 뒤"에만 돈다. 백업이 실패한 날에
# 옛것부터 지워버리는 사고를 구조적으로 막기 위한 순서다.
# prune 자체가 실패해도 배포는 막지 않는다 — 중요한 건 백업이지 청소가 아니다.
if [ -z "$RETENTION_DAYS" ]; then
    exit 0
fi

prune_old_backups() {
    local cutoff listing candidates count=0 key
    # GNU(date -d) / BSD(date -v) 양쪽 지원
    cutoff="$(date -u -d "-${RETENTION_DAYS} days" +%Y-%m-%d 2>/dev/null \
              || date -u -v-"${RETENTION_DAYS}"d +%Y-%m-%d)"

    local list_args=(s3api list-objects-v2 --bucket "$BUCKET"
                     --prefix "${PREFIX}/${APP_ENV}/"
                     --query 'Contents[].[LastModified,Key]' --output text)
    [ -n "$REGION" ] && list_args+=(--region "$REGION")
    listing="$(aws_run "${list_args[@]}")"

    # "2026-08-14T03:09:49+00:00\t키" → "2026-08-14\t키"
    candidates="$(printf '%s\n' "$listing" \
        | awk -F'\t' 'NF >= 2 && $1 ~ /^[0-9]{4}-[0-9]{2}-[0-9]{2}T/ { split($1, a, "T"); print a[1] "\t" $2 }' \
        | prune_select "$cutoff" "$KEEP_MIN")"

    if [ -z "$candidates" ]; then
        log "정리 대상 없음 (${RETENTION_DAYS}일 초과, 최소 ${KEEP_MIN}개 보존)"
        return 0
    fi

    while IFS= read -r key; do
        [ -n "$key" ] || continue
        local rm_args=(s3api delete-object --bucket "$BUCKET" --key "$key")
        [ -n "$REGION" ] && rm_args+=(--region "$REGION")
        aws_run "${rm_args[@]}" > /dev/null
        log "삭제: $key"
        count=$((count + 1))
    done <<< "$candidates"
    log "정리 완료: ${count}개 삭제 (${RETENTION_DAYS}일 초과, 최소 ${KEEP_MIN}개 보존)"
}

if ! prune_old_backups; then
    log "WARN: 오래된 백업 정리에 실패했다. 백업 자체는 성공했으므로 계속 진행한다."
fi
