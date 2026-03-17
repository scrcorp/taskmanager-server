-- convert-urls-to-relative.sql
-- DB에 저장된 절대 URL을 상대경로(key)로 변환하는 1회성 스크립트
--
-- 대상 테이블 (refactor/db-server-cleanup 이후):
--   - cl_item_files.file_url
--   - cl_item_messages.content (file URL이 남아있는 경우)
--   - task_evidences.file_url
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

SELECT 'cl_item_messages.content' AS target,
       count(*) FILTER (WHERE content LIKE 'https://%.amazonaws.com/%') AS s3_urls,
       count(*) FILTER (WHERE content ~ '^https?://[^/]+/(uploads|bucket)/') AS local_urls,
       count(*) FILTER (WHERE content IS NOT NULL AND content NOT LIKE 'http%') AS already_relative,
       count(*) AS total
FROM cl_item_messages WHERE content IS NOT NULL;

SELECT 'task_evidences.file_url' AS target,
       count(*) FILTER (WHERE file_url LIKE 'https://%.amazonaws.com/%') AS s3_urls,
       count(*) FILTER (WHERE file_url ~ '^https?://[^/]+/(uploads|bucket)/') AS local_urls,
       count(*) FILTER (WHERE file_url IS NOT NULL AND file_url NOT LIKE 'http%') AS already_relative,
       count(*) AS total
FROM task_evidences WHERE file_url IS NOT NULL;

\echo ''
\echo '=== URL prefix 제거 실행 ==='

-- cl_item_files.file_url
UPDATE cl_item_files
SET file_url = REGEXP_REPLACE(file_url, '^https://[^/]+\.s3\.[^/]+\.amazonaws\.com/', '')
WHERE file_url LIKE 'https://%.s3.%.amazonaws.com/%';

UPDATE cl_item_files
SET file_url = REGEXP_REPLACE(file_url, '^https?://[^/]+/(uploads|bucket)/', '')
WHERE file_url ~ '^https?://[^/]+/(uploads|bucket)/';

-- cl_item_messages.content (file URL이 남아있는 경우)
UPDATE cl_item_messages
SET content = REGEXP_REPLACE(content, '^https://[^/]+\.s3\.[^/]+\.amazonaws\.com/', '')
WHERE content LIKE 'https://%.s3.%.amazonaws.com/%';

UPDATE cl_item_messages
SET content = REGEXP_REPLACE(content, '^https?://[^/]+/(uploads|bucket)/', '')
WHERE content ~ '^https?://[^/]+/(uploads|bucket)/';

-- task_evidences.file_url
UPDATE task_evidences
SET file_url = REGEXP_REPLACE(file_url, '^https://[^/]+\.s3\.[^/]+\.amazonaws\.com/', '')
WHERE file_url LIKE 'https://%.s3.%.amazonaws.com/%';

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

SELECT 'cl_item_messages.content' AS target,
       count(*) FILTER (WHERE content LIKE 'http%') AS still_absolute,
       count(*) FILTER (WHERE content NOT LIKE 'http%') AS relative,
       count(*) AS total
FROM cl_item_messages WHERE content IS NOT NULL;

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
