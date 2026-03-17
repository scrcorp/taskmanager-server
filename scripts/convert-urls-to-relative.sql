-- convert-urls-to-relative.sql
-- DB에 저장된 절대 URL을 상대경로(key)로 변환하는 1회성 스크립트
--
-- 사용법:
--   1. 먼저 DRY RUN (변환 결과만 확인):
--      psql -h <host> -U <user> -d <db> -f scripts/convert-urls-to-relative.sql
--      → BEGIN ~ ROLLBACK 안에서 실행되므로 실제 변경 없음
--
--   2. 실제 적용 시: 맨 아래 ROLLBACK → COMMIT 으로 변경 후 실행
--
-- 대상 테이블/컬럼:
--   - cl_completions.photo_url
--   - cl_completion_history.photo_url
--   - task_evidences.file_url
--   - cl_review_contents.content (type='photo' 또는 'video')

BEGIN;

-- ── 변환 전 현황 ─────────────────────────────────────────
\echo '=== 변환 전 현황 ==='

SELECT 'cl_completions.photo_url' AS target,
       count(*) FILTER (WHERE photo_url LIKE 'https://%.amazonaws.com/%') AS s3_urls,
       count(*) FILTER (WHERE photo_url ~ '^https?://[^/]+/(uploads|bucket)/') AS local_urls,
       count(*) FILTER (WHERE photo_url IS NOT NULL AND photo_url NOT LIKE 'http%') AS already_relative,
       count(*) AS total
FROM cl_completions WHERE photo_url IS NOT NULL;

SELECT 'cl_completion_history.photo_url' AS target,
       count(*) FILTER (WHERE photo_url LIKE 'https://%.amazonaws.com/%') AS s3_urls,
       count(*) FILTER (WHERE photo_url ~ '^https?://[^/]+/(uploads|bucket)/') AS local_urls,
       count(*) FILTER (WHERE photo_url IS NOT NULL AND photo_url NOT LIKE 'http%') AS already_relative,
       count(*) AS total
FROM cl_completion_history WHERE photo_url IS NOT NULL;

SELECT 'task_evidences.file_url' AS target,
       count(*) FILTER (WHERE file_url LIKE 'https://%.amazonaws.com/%') AS s3_urls,
       count(*) FILTER (WHERE file_url ~ '^https?://[^/]+/(uploads|bucket)/') AS local_urls,
       count(*) FILTER (WHERE file_url IS NOT NULL AND file_url NOT LIKE 'http%') AS already_relative,
       count(*) AS total
FROM task_evidences WHERE file_url IS NOT NULL;

SELECT 'cl_review_contents(media)' AS target,
       count(*) FILTER (WHERE content LIKE 'https://%.amazonaws.com/%') AS s3_urls,
       count(*) FILTER (WHERE content ~ '^https?://[^/]+/(uploads|bucket)/') AS local_urls,
       count(*) FILTER (WHERE content IS NOT NULL AND content NOT LIKE 'http%') AS already_relative,
       count(*) AS total
FROM cl_review_contents WHERE type IN ('photo', 'video');

-- ── URL prefix 제거 ──────────────────────────────────────
\echo ''
\echo '=== URL prefix 제거 실행 ==='

-- cl_completions.photo_url
UPDATE cl_completions
SET photo_url = REGEXP_REPLACE(photo_url, '^https://[^/]+\.s3\.[^/]+\.amazonaws\.com/', '')
WHERE photo_url LIKE 'https://%.s3.%.amazonaws.com/%';

UPDATE cl_completions
SET photo_url = REGEXP_REPLACE(photo_url, '^https?://[^/]+/(uploads|bucket)/', '')
WHERE photo_url ~ '^https?://[^/]+/(uploads|bucket)/';

-- cl_completion_history.photo_url
UPDATE cl_completion_history
SET photo_url = REGEXP_REPLACE(photo_url, '^https://[^/]+\.s3\.[^/]+\.amazonaws\.com/', '')
WHERE photo_url LIKE 'https://%.s3.%.amazonaws.com/%';

UPDATE cl_completion_history
SET photo_url = REGEXP_REPLACE(photo_url, '^https?://[^/]+/(uploads|bucket)/', '')
WHERE photo_url ~ '^https?://[^/]+/(uploads|bucket)/';

-- task_evidences.file_url
UPDATE task_evidences
SET file_url = REGEXP_REPLACE(file_url, '^https://[^/]+\.s3\.[^/]+\.amazonaws\.com/', '')
WHERE file_url LIKE 'https://%.s3.%.amazonaws.com/%';

UPDATE task_evidences
SET file_url = REGEXP_REPLACE(file_url, '^https?://[^/]+/(uploads|bucket)/', '')
WHERE file_url ~ '^https?://[^/]+/(uploads|bucket)/';

-- cl_review_contents.content (photo/video만)
UPDATE cl_review_contents
SET content = REGEXP_REPLACE(content, '^https://[^/]+\.s3\.[^/]+\.amazonaws\.com/', '')
WHERE type IN ('photo', 'video')
  AND content LIKE 'https://%.s3.%.amazonaws.com/%';

UPDATE cl_review_contents
SET content = REGEXP_REPLACE(content, '^https?://[^/]+/(uploads|bucket)/', '')
WHERE type IN ('photo', 'video')
  AND content ~ '^https?://[^/]+/(uploads|bucket)/';

-- ── 변환 후 현황 ─────────────────────────────────────────
\echo ''
\echo '=== 변환 후 현황 ==='

SELECT 'cl_completions.photo_url' AS target,
       count(*) FILTER (WHERE photo_url LIKE 'http%') AS still_absolute,
       count(*) FILTER (WHERE photo_url NOT LIKE 'http%') AS relative,
       count(*) AS total
FROM cl_completions WHERE photo_url IS NOT NULL;

SELECT 'cl_completion_history.photo_url' AS target,
       count(*) FILTER (WHERE photo_url LIKE 'http%') AS still_absolute,
       count(*) FILTER (WHERE photo_url NOT LIKE 'http%') AS relative,
       count(*) AS total
FROM cl_completion_history WHERE photo_url IS NOT NULL;

SELECT 'task_evidences.file_url' AS target,
       count(*) FILTER (WHERE file_url LIKE 'http%') AS still_absolute,
       count(*) FILTER (WHERE file_url NOT LIKE 'http%') AS relative,
       count(*) AS total
FROM task_evidences WHERE file_url IS NOT NULL;

SELECT 'cl_review_contents(media)' AS target,
       count(*) FILTER (WHERE content LIKE 'http%') AS still_absolute,
       count(*) FILTER (WHERE content NOT LIKE 'http%') AS relative,
       count(*) AS total
FROM cl_review_contents WHERE type IN ('photo', 'video');

-- ── 샘플 확인 ────────────────────────────────────────────
\echo ''
\echo '=== 변환 후 샘플 (최대 3건) ==='
SELECT photo_url FROM cl_completions WHERE photo_url IS NOT NULL LIMIT 3;

-- ⚠️ DRY RUN: 실제 적용 시 ROLLBACK → COMMIT 으로 변경
ROLLBACK;
\echo ''
\echo '⚠️  ROLLBACK — 실제 변경 없음. COMMIT으로 바꿔서 다시 실행하세요.'
