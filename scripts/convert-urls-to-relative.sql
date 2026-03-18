-- convert-urls-to-relative.sql
-- DB에 저장된 절대 URL을 상대경로(key)로 변환하는 1회성 스크립트
--
-- 실행 조건: alembic migration 완료 후 (새 테이블 구조가 적용된 상태)
--
-- 대상 테이블/컬럼:
--   - cl_item_files.file_url       (체크리스트 항목 첨부파일)
--   - task_evidences.file_url      (업무 증빙 파일)
--
-- 사용법:
--   1. DRY RUN (변환 결과만 확인, 실제 변경 없음):
--      psql -h <host> -U <user> -d <db> -f scripts/convert-urls-to-relative.sql
--
--   2. 실제 적용: 맨 아래 ROLLBACK → COMMIT 으로 변경 후 실행

BEGIN;

\echo '=== 변환 전 현황 ==='

SELECT 'cl_item_files.file_url' AS target,
       count(*) FILTER (WHERE file_url LIKE 'https://%.amazonaws.com/%') AS s3_urls,
       count(*) FILTER (WHERE file_url ~ '^https?://[^/]+/(uploads|bucket)/') AS local_urls,
       count(*) FILTER (WHERE file_url IS NOT NULL AND file_url NOT LIKE 'http%') AS already_relative,
       count(*) AS total
FROM cl_item_files WHERE file_url IS NOT NULL;

SELECT 'task_evidences.file_url' AS target,
       count(*) FILTER (WHERE file_url LIKE 'https://%.amazonaws.com/%') AS s3_urls,
       count(*) FILTER (WHERE file_url ~ '^https?://[^/]+/(uploads|bucket)/') AS local_urls,
       count(*) FILTER (WHERE file_url IS NOT NULL AND file_url NOT LIKE 'http%') AS already_relative,
       count(*) AS total
FROM task_evidences WHERE file_url IS NOT NULL;

\echo ''
\echo '=== URL prefix 제거 실행 ==='

-- cl_item_files.file_url — S3
UPDATE cl_item_files
SET file_url = REGEXP_REPLACE(file_url, '^https://[^/]+\.s3\.[^/]+\.amazonaws\.com/', '')
WHERE file_url LIKE 'https://%.s3.%.amazonaws.com/%';

-- cl_item_files.file_url — local
UPDATE cl_item_files
SET file_url = REGEXP_REPLACE(file_url, '^https?://[^/]+/(uploads|bucket)/', '')
WHERE file_url ~ '^https?://[^/]+/(uploads|bucket)/';

-- task_evidences.file_url — S3
UPDATE task_evidences
SET file_url = REGEXP_REPLACE(file_url, '^https://[^/]+\.s3\.[^/]+\.amazonaws\.com/', '')
WHERE file_url LIKE 'https://%.s3.%.amazonaws.com/%';

-- task_evidences.file_url — local
UPDATE task_evidences
SET file_url = REGEXP_REPLACE(file_url, '^https?://[^/]+/(uploads|bucket)/', '')
WHERE file_url ~ '^https?://[^/]+/(uploads|bucket)/';

\echo ''
\echo '=== 변환 후 현황 ==='

SELECT 'cl_item_files.file_url' AS target,
       count(*) FILTER (WHERE file_url LIKE 'http%') AS still_absolute,
       count(*) FILTER (WHERE file_url NOT LIKE 'http%') AS relative,
       count(*) AS total
FROM cl_item_files WHERE file_url IS NOT NULL;

SELECT 'task_evidences.file_url' AS target,
       count(*) FILTER (WHERE file_url LIKE 'http%') AS still_absolute,
       count(*) FILTER (WHERE file_url NOT LIKE 'http%') AS relative,
       count(*) AS total
FROM task_evidences WHERE file_url IS NOT NULL;

\echo ''
\echo '=== 샘플 (최대 3건) ==='
SELECT file_url FROM cl_item_files WHERE file_url IS NOT NULL LIMIT 3;

-- ⚠️ DRY RUN: 실제 적용 시 ROLLBACK → COMMIT 으로 변경
ROLLBACK;
\echo ''
\echo '⚠️  ROLLBACK — 실제 변경 없음. COMMIT으로 바꿔서 다시 실행하세요.'
