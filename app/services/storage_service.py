"""스토리지 서비스 — S3 또는 로컬 파일 저장 + URL 해석.

Storage Service — S3 presigned URL or local file storage.
STORAGE_MODE 환경변수로 모드 결정 ("local" 또는 "s3").
S3 모드에서 access key가 없으면 IAM role을 사용합니다 (EC2 배포 시).
모든 업로드는 temp/ 폴더에 먼저 저장되고, finalize_upload()로 최종 위치로 이동합니다.

DB에는 상대경로(key)만 저장합니다. 예: completions/2026/03/17/{uuid}.jpg
API 응답 시 resolve_url(key)로 환경에 맞는 전체 URL을 생성합니다.

fallback 동작 (STORAGE_FALLBACK_BUCKET 또는 LOCAL_FALLBACK_BUCKET_DIR 설정 시):
  현재 버킷에 파일이 없으면 fallback 버킷에서 자동 복사 후 URL 반환.
  staging은 prod에서, worktree는 dev에서 fallback.

폴더별 경로는 .env에서 설정 가능 (STORAGE_FOLDER_*). 기본값:
  reviews, completions, profiles, notices, issues
"""

import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.utils.image import (
    WEBP_EXT,
    profile_for_folder,
    render_derivatives,
    thumb_key,
    to_webp_key,
)

logger = logging.getLogger(__name__)

# 로컬 버킷 디렉토리 — .env의 LOCAL_BUCKET_DIR 필수
# dev: 프로젝트루트/bucket/dev/, worktree: 프로젝트루트/bucket/worktree/{branch}/
if settings.STORAGE_MODE != "s3" and not settings.LOCAL_BUCKET_DIR:
    raise RuntimeError("LOCAL_BUCKET_DIR must be set in .env for local storage mode")
BUCKET_DIR: Path = Path(settings.LOCAL_BUCKET_DIR) if settings.LOCAL_BUCKET_DIR else Path(".")
FALLBACK_BUCKET_DIR: Path | None = (
    Path(settings.LOCAL_FALLBACK_BUCKET_DIR) if settings.LOCAL_FALLBACK_BUCKET_DIR else None
)

# 폴더별 경로 매핑 — .env에서 오버라이드 가능
FOLDER_MAP: dict[str, str] = {
    "reviews": settings.STORAGE_FOLDER_REVIEWS,
    "completions": settings.STORAGE_FOLDER_COMPLETIONS,
    "profiles": settings.STORAGE_FOLDER_PROFILES,
    "notices": settings.STORAGE_FOLDER_ANNOUNCEMENTS,
    "issues": settings.STORAGE_FOLDER_ISSUES,
    "warnings": settings.STORAGE_FOLDER_WARNINGS,
}


def resolve_folder(folder: str) -> str:
    """폴더 이름을 .env 설정값으로 변환합니다. 미등록 폴더는 그대로 반환."""
    return FOLDER_MAP.get(folder, folder)


# resolve_folder 의 역방향: 실제 경로 세그먼트 → 논리 폴더 이름.
# 키에서 프로파일을 결정할 때 쓴다(.env 오버라이드를 되돌림).
_RESOLVED_TO_LOGICAL: dict[str, str] = {v: k for k, v in FOLDER_MAP.items()}


def logical_folder(resolved: str) -> str:
    """실제 경로 세그먼트 → 논리 폴더 이름. 미등록은 그대로 반환."""
    return _RESOLVED_TO_LOGICAL.get(resolved, resolved)


# ── 중앙 라우팅/검증 테이블 ───────────────────────────────────
# Phase 1 통합: 저장 폴더를 한 곳에서 관리한다. presigned 발급과 서버 직접 업로드
# 모두 이 allowlist를 거쳐야 한다. 클라이언트가 임의 폴더를 지정해 쓰는 것을 차단.
#
# 논리 폴더 이름. resolve_folder()로 .env 오버라이드 값으로 변환되어 실제 경로가 된다.
# NOTE: 클라이언트(app Flutter / console)가 presigned 발급 시 보내는 folder 값을
# 전수 확인해 등록했다(2026-06-22). 빠지면 해당 업로드가 400으로 거부된다.
#   app    default 'completions' / 'tasks' / 'chat' / 'products'
#   console default 'reviews'    / 'tasks'
ALLOWED_FOLDERS: frozenset[str] = frozenset(
    {
        "completions",          # 체크리스트 완료 사진 (app 기본)
        "reviews",              # 리뷰/report 첨부 (console 기본)
        "tasks",                # additional_task 첨부 (app·console)
        "chat",                 # 체크리스트 코멘트/채팅 사진 (app)
        "products",             # 인벤토리 제품 사진 (app)
        "profiles",             # 프로필 사진
        "notices",              # 공지 첨부
        "issues",               # 이슈 사진
        "warnings",             # 경고 서명 PDF (서버 upload_bytes)
        "store_covers",         # 매장 커버 이미지 (서버 upload_bytes)
        "applicant_attachments",  # 지원자 첨부 (서버 upload_bytes)
    }
)

# 서버가 고정 키(deterministic key)로 직접 쓰는 경로의 허용 접두사.
# 서명 PNG, 4070 PDF 등 — 랜덤 키가 아니라 엔터티 ID 기반 안정 경로를 쓴다.
ALLOWED_KEY_PREFIXES: tuple[str, ...] = (
    "temp/",          # presigned 업로드 수신 (finalize 전)
    "signatures/",    # tip 서명 PNG
    "forms/",         # 4070 PDF
)


class InvalidStorageFolder(ValueError):
    """allowlist에 없는 저장 폴더가 요청된 경우."""


class UnsafeStorageKey(ValueError):
    """허용되지 않은/위험한(path traversal 등) 저장 키가 요청된 경우."""


def validate_folder(folder: str) -> str:
    """저장 폴더가 allowlist에 있는지 검증하고 그대로 반환. 없으면 예외."""
    if folder not in ALLOWED_FOLDERS:
        raise InvalidStorageFolder(folder)
    return folder


def validate_upload_key(key: str) -> str:
    """직접 저장(raw PUT/고정키) 키가 안전한지 검증.

    - path traversal(``..``)·절대경로·백슬래시 차단
    - 허용 접두사(ALLOWED_KEY_PREFIXES)로 시작해야 함
    """
    if not key or ".." in key or key.startswith("/") or "\\" in key:
        raise UnsafeStorageKey(key)
    if not key.startswith(ALLOWED_KEY_PREFIXES):
        raise UnsafeStorageKey(key)
    return key


class StorageService:
    """파일 업로드 + URL 해석 서비스 — S3 또는 로컬 모드 자동 선택."""

    def __init__(self) -> None:
        self._client = None
        self._resolved_cache: set[str] = set()

    @property
    def is_local(self) -> bool:
        return settings.STORAGE_MODE != "s3"

    @property
    def has_fallback(self) -> bool:
        if self.is_local:
            return FALLBACK_BUCKET_DIR is not None
        return bool(settings.STORAGE_FALLBACK_BUCKET)

    @property
    def client(self):
        if self.is_local:
            return None
        if self._client is None:
            import boto3
            from botocore.config import Config as BotoConfig

            kwargs: dict = {
                "region_name": settings.AWS_S3_REGION,
                "config": BotoConfig(signature_version="s3v4"),
            }
            # access key가 있으면 명시적 사용, 없으면 IAM role (boto3 기본 credential chain)
            if settings.AWS_ACCESS_KEY_ID:
                kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
                kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
            self._client = boto3.client("s3", **kwargs)
        return self._client

    # ── key 생성 ──────────────────────────────────────────────

    def _generate_key(self, filename: str, folder: str) -> str:
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
        date_prefix = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        resolved = resolve_folder(folder)
        return f"temp/{resolved}/{date_prefix}/{uuid.uuid4().hex}.{ext}"

    # ── presigned URL ─────────────────────────────────────────

    def generate_presigned_upload_url(
        self,
        filename: str,
        content_type: str,
        folder: str = "reviews",
        expires: int = 3600,
        *,
        base_url: str = "http://localhost:8000",
        upload_path_prefix: str = "/api/v1/app/storage",
    ) -> dict[str, str]:
        """presigned PUT URL과 temp key를 반환합니다.

        모든 업로드는 temp/ 하위에 저장됩니다.
        finalize_upload()로 최종 위치로 이동해야 합니다.

        Returns:
            upload_url: 클라이언트가 PUT할 URL
            file_url: S3 모드에서 클라이언트가 참조용으로 쓸 전체 URL (temp)
            key: 스토리지 key (temp/...)
        """
        key = self._generate_key(filename, folder)

        if self.is_local:
            upload_url = f"{base_url}{upload_path_prefix}/upload/{key}"
            file_url = f"{base_url}/bucket/{key}"
            return {"upload_url": upload_url, "file_url": file_url, "key": key}

        upload_url = self.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": settings.AWS_S3_BUCKET,
                "Key": key,
                "ContentType": content_type,
            },
            ExpiresIn=expires,
        )
        file_url = f"https://{settings.AWS_S3_BUCKET}.s3.{settings.AWS_S3_REGION}.amazonaws.com/{key}"
        return {"upload_url": upload_url, "file_url": file_url, "key": key}

    def generate_presigned_download_url(
        self,
        key: str,
        expires: int = 600,
    ) -> str:
        """presigned GET URL — 임시 다운로드 링크.

        S3 모드: get_object presigned URL (default 10분 만료).
        로컬 모드: 그냥 정적 파일 URL (만료 개념 없음).
        """
        if self.is_local:
            return self._build_url(key)
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.AWS_S3_BUCKET, "Key": key},
            ExpiresIn=expires,
        )

    # ── 로컬 파일 저장 ────────────────────────────────────────

    def save_local(self, key: str, data: bytes) -> str:
        """로컬 파일 저장. 경로를 반환합니다."""
        path = BUCKET_DIR / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def _put_object(self, key: str, data: bytes, content_type: str | None = None) -> None:
        """모드 무관 단일 쓰기 — local 이면 디스크, s3 면 put_object."""
        if self.is_local:
            self.save_local(key, data)
            return
        kwargs: dict = {"Bucket": settings.AWS_S3_BUCKET, "Key": key, "Body": data}
        if content_type:
            kwargs["ContentType"] = content_type
        self.client.put_object(**kwargs)

    def _final_key(self, filename: str, folder: str, *, ext: str | None = None) -> str:
        """temp 를 거치지 않는 최종 key 생성. ext 지정 시 확장자 강제(webp 등)."""
        if ext is None:
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
        date_prefix = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        resolved = resolve_folder(folder)
        return f"{resolved}/{date_prefix}/{uuid.uuid4().hex}.{ext}"

    def _store_derivatives(self, base_key: str, derivatives: dict[str, bytes]) -> str:
        """인코딩된 파생본을 저장하고 **DB에 저장할 base key**를 반환.

        full 이 있으면 base_key(=...webp)에 full 저장 + thumb 별도 저장.
        full 이 없으면(PRODUCT_SQUARE/AVATAR) 단일 파생본을 base_key 에 저장.
        """
        primary = derivatives.get("full") or derivatives.get("thumb")
        assert primary is not None  # render_derivatives 가 빈 dict 면 호출 안 됨
        self._put_object(base_key, primary, "image/webp")
        if "full" in derivatives and "thumb" in derivatives:
            self._put_object(thumb_key(base_key), derivatives["thumb"], "image/webp")
        return base_key

    # ── 직접 업로드 (multipart 요청에서 서버가 직접 S3/로컬에 저장) ─

    def upload_bytes(
        self,
        data: bytes,
        filename: str,
        folder: str,
        content_type: str | None = None,
    ) -> str:
        """multipart 요청 등에서 받은 바이트를 직접 저장하고 key를 반환합니다.

        presigned 패턴 대신 서버가 직접 업로드하는 단순 케이스용.
        temp/ 폴더를 거치지 않고 곧바로 최종 위치에 저장합니다.

        Args:
            data: 파일 바이트.
            filename: 원본 파일명 (확장자 추출용).
            folder: 폴더 키 (FOLDER_MAP 또는 임의 값).
            content_type: S3 업로드 시 ContentType. None이면 자동 추론하지 않음.

        Returns:
            상대경로(key). 예: store_covers/2026/04/28/{uuid}.jpg
        """
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
        date_prefix = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        resolved = resolve_folder(folder)
        key = f"{resolved}/{date_prefix}/{uuid.uuid4().hex}.{ext}"

        if self.is_local:
            self.save_local(key, data)
            return key

        kwargs: dict = {
            "Bucket": settings.AWS_S3_BUCKET,
            "Key": key,
            "Body": data,
        }
        if content_type:
            kwargs["ContentType"] = content_type
        self.client.put_object(**kwargs)
        return key

    # ── key 추출 (기존 절대 URL + 새 상대경로 모두 지원) ──────

    def extract_key(self, file_url_or_key: str) -> str | None:
        """file URL 또는 상대경로에서 storage key를 추출합니다.

        지원 형식:
          - 상대경로: completions/2026/03/17/abc.jpg (그대로 반환)
          - S3 URL: https://{bucket}.s3.{region}.amazonaws.com/{key}
          - 로컬 URL: http://*/uploads/{key} 또는 http://*/bucket/{key}
        """
        if not file_url_or_key:
            return None

        # 이미 상대경로 (http로 시작하지 않으면)
        if not file_url_or_key.startswith("http"):
            return file_url_or_key

        # S3 URL — .amazonaws.com/ 이후가 key
        marker = ".amazonaws.com/"
        idx = file_url_or_key.find(marker)
        if idx != -1:
            return file_url_or_key[idx + len(marker):]

        # 로컬 URL — /uploads/ 또는 /bucket/ 이후가 key
        for local_marker in ("/bucket/", "/uploads/"):
            idx = file_url_or_key.find(local_marker)
            if idx != -1:
                return file_url_or_key[idx + len(local_marker):]

        return None

    # ── finalize (temp → 최종 위치, 상대경로 반환) ────────────

    def finalize_upload(self, file_url_or_key: str) -> str:
        """temp 파일을 최종 위치로 이동합니다. **상대경로(key)**를 반환합니다.

        temp/ 경로가 아닌 파일은 key를 그대로 반환합니다.
        멱등(idempotent): 이미 finalize된 파일은 최종 key를 반환합니다.
        """
        key = self.extract_key(file_url_or_key)
        if not key:
            return file_url_or_key

        if not key.startswith("temp/"):
            return key

        final_key = key[len("temp/"):]

        if self.is_local:
            src = BUCKET_DIR / key
            dst = BUCKET_DIR / final_key
            if not src.exists():
                return final_key
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return final_key

        # S3: temp → final copy + delete
        try:
            self.client.copy_object(
                Bucket=settings.AWS_S3_BUCKET,
                Key=final_key,
                CopySource={"Bucket": settings.AWS_S3_BUCKET, "Key": key},
            )
            self.client.delete_object(Bucket=settings.AWS_S3_BUCKET, Key=key)
        except self.client.exceptions.NoSuchKey:
            pass
        return final_key

    # ── resolve_url (상대경로 → 전체 URL, fallback 자동 처리) ─

    def resolve_url(self, key: str | None, *, variant: str = "full") -> str | None:
        """상대경로(key) → 접근 가능한 전체 URL.

        variant="thumb": base 가 ``...webp`` 면 썸네일 key 를 시도하고, 썸네일이
        실제로 존재할 때만 그 URL 을 반환한다(없으면 base=full 로 폴백). 레거시
        비-webp key, 단일파생 프로파일(thumb 미생성)은 자연히 base 로 떨어진다.

        fallback 설정 시: 현재 버킷에 없으면 fallback에서 복사 후 URL 반환.
        fallback 미설정 시 (prod/dev): 존재 확인 없이 바로 URL 반환.
        어디에도 없으면 None 반환.
        """
        if not key:
            return None

        if variant == "thumb" and key.endswith(WEBP_EXT):
            tk = thumb_key(key)
            if tk != key:
                thumb_url = self._resolve_if_present(tk)
                if thumb_url:
                    return thumb_url
            # 썸네일 부재 → base(full)로 폴백

        if not self.has_fallback:
            return self._build_url(key)

        # fallback 모드: 현재 버킷 확인 → 없으면 fallback에서 복사
        if key in self._resolved_cache:
            return self._build_url(key)

        if self._exists(key):
            self._resolved_cache.add(key)
            return self._build_url(key)

        if self._copy_from_fallback(key):
            self._resolved_cache.add(key)
            return self._build_url(key)

        # 어디에도 없음
        return None

    def _resolve_if_present(self, key: str) -> str | None:
        """key 가 실제로 존재할 때만 URL 반환(없으면 None). 썸네일 폴백 판정용.

        캐시로 반복 head/stat 를 줄인다. fallback 모드면 fallback 복사도 시도.
        NOTE(prod 핫패스): non-fallback S3 모드에선 미캐시 썸네일마다 head_object
        1회가 든다. 캐시가 흡수하지만, 콘솔 표시 단계(Phase 2)에서 도메인이
        썸네일 보유를 아는 경우 head 생략 최적화 여지 있음.
        """
        if key in self._resolved_cache:
            return self._build_url(key)
        if self._exists(key):
            self._resolved_cache.add(key)
            return self._build_url(key)
        if self.has_fallback and self._copy_from_fallback(key):
            self._resolved_cache.add(key)
            return self._build_url(key)
        return None

    def _build_url(self, key: str) -> str:
        """key → 현재 환경의 전체 URL."""
        if self.is_local:
            return f"{self._local_base_url}/bucket/{key}"
        return f"https://{settings.AWS_S3_BUCKET}.s3.{settings.AWS_S3_REGION}.amazonaws.com/{key}"

    @property
    def _local_base_url(self) -> str:
        """로컬 서버 base URL. SERVER_BASE_URL 설정 우선, 없으면 localhost."""
        if settings.SERVER_BASE_URL:
            return settings.SERVER_BASE_URL.rstrip("/")
        port = settings.SERVER_PORT if hasattr(settings, "SERVER_PORT") else 8000
        return f"http://localhost:{port}"

    def _exists(self, key: str) -> bool:
        """현재 버킷에 파일이 존재하는지 확인."""
        if self.is_local:
            return (BUCKET_DIR / key).exists()
        try:
            self.client.head_object(Bucket=settings.AWS_S3_BUCKET, Key=key)
            return True
        except Exception:
            return False

    def _copy_from_fallback(self, key: str) -> bool:
        """fallback 버킷에서 현재 버킷으로 파일 복사. 성공 여부 반환."""
        if self.is_local:
            if not FALLBACK_BUCKET_DIR:
                return False
            src = FALLBACK_BUCKET_DIR / key
            if not src.exists():
                return False
            dst = BUCKET_DIR / key
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            logger.info("fallback 복사: %s → %s", src, dst)
            return True

        # S3: fallback 버킷 → 현재 버킷 복사
        fallback = settings.STORAGE_FALLBACK_BUCKET
        if not fallback:
            return False
        try:
            self.client.copy_object(
                Bucket=settings.AWS_S3_BUCKET,
                Key=key,
                CopySource={"Bucket": fallback, "Key": key},
            )
            logger.info("S3 fallback 복사: %s/%s → %s/%s", fallback, key, settings.AWS_S3_BUCKET, key)
            return True
        except Exception:
            return False

    def read_bytes(self, key: str | None) -> bytes | None:
        """key 의 파일 바이트를 읽어 반환 (인증된 서버 다운로드 서빙용).

        현재 버킷에 없으면 fallback 버킷에서 복사 시도(resolve_url 과 동일 정책).
        끝내 없으면 None.
        """
        if not key:
            return None
        if not self._exists(key) and not self._copy_from_fallback(key):
            return None
        if self.is_local:
            try:
                return (BUCKET_DIR / key).read_bytes()
            except Exception:
                return None
        try:
            obj = self.client.get_object(Bucket=settings.AWS_S3_BUCKET, Key=key)
            return obj["Body"].read()
        except Exception:
            return None

    # ── 파일 삭제 ─────────────────────────────────────────────

    def delete_file(self, file_url_or_key: str) -> bool:
        """S3 또는 로컬에서 파일을 삭제합니다. 성공 여부를 반환합니다.

        상대경로(key) 또는 기존 절대 URL 모두 지원.
        """
        if not file_url_or_key:
            return False

        key = self.extract_key(file_url_or_key)
        if not key:
            return False

        self._resolved_cache.discard(key)

        if self.is_local:
            path = BUCKET_DIR / key
            if path.exists():
                path.unlink()
                return True
            return False

        try:
            self.client.delete_object(Bucket=settings.AWS_S3_BUCKET, Key=key)
            return True
        except Exception:
            return False

    # ── 고수준 단일 진입점 (Phase 1 통합) ─────────────────────
    # 도메인 코드는 아래 4개만 쓴다. 저수준 메서드(save_local/upload_bytes/
    # finalize_upload/generate_presigned_upload_url)는 내부 구현으로 둔다.

    def presign_upload(
        self,
        folder: str,
        filename: str,
        content_type: str,
        expires: int = 3600,
        *,
        base_url: str = "http://localhost:8000",
        upload_path_prefix: str = "/api/v1/app/storage",
    ) -> dict[str, str]:
        """[저장-1] presigned 업로드 URL 발급. 폴더 allowlist 검증 포함.

        클라이언트가 temp/로 직접 올린 뒤 put_finalized()로 마무리한다.
        """
        validate_folder(folder)
        return self.generate_presigned_upload_url(
            filename=filename,
            content_type=content_type,
            folder=folder,
            expires=expires,
            base_url=base_url,
            upload_path_prefix=upload_path_prefix,
        )

    def receive_upload(self, key: str, data: bytes) -> str:
        """[저장-2a] presigned(local) PUT 수신. 키 안전성 검증 후 저장.

        ``temp/`` 등 허용 접두사 + path traversal 차단. raw PUT 엔드포인트 전용.
        """
        validate_upload_key(key)
        return self.save_local(key, data)

    def put_finalized(self, file_url_or_key: str) -> str:
        """[저장-2b] temp 업로드를 최종 위치로 확정. 상대경로(key) 반환.

        Phase 2: temp 바이트가 이미지면 폴더 프로파일로 WebP(+썸네일) 인코딩 후
        ``...webp`` key 를 반환한다(DB엔 이 key 저장). 비이미지(PDF·동영상)거나
        디코딩 실패면 기존 finalize_upload 동작(단순 이동, 원본 보존)으로 폴백.
        """
        key = self.extract_key(file_url_or_key)
        if not key or not key.startswith("temp/"):
            # temp 가 아니면 인코딩 대상 아님(이미 finalize된 key 등)
            return self.finalize_upload(file_url_or_key)

        final_key = key[len("temp/"):]
        profile = profile_for_folder(logical_folder(final_key.split("/", 1)[0]))

        data = self.read_bytes(key) if profile.encode else None
        if data is not None:
            derivatives = render_derivatives(data, profile)
            if derivatives:
                webp_key = to_webp_key(final_key)
                self._store_derivatives(webp_key, derivatives)
                # 원본 temp 정리(인코딩본만 영구 보존 = 원본폐기 c)
                self.delete_file(key)
                return webp_key

        # 비이미지/디코딩 실패 → 기존 동작(원본 그대로 이동)
        return self.finalize_upload(file_url_or_key)

    def put_bytes(
        self,
        data: bytes,
        folder: str = "",
        filename: str = "",
        *,
        content_type: str | None = None,
        key: str | None = None,
    ) -> str:
        """[저장-3] 서버가 보유한 바이트를 직접 저장하고 key 반환.

        - ``key`` 미지정: ``folder`` allowlist 검증 후 랜덤 키 생성 (upload_bytes).
        - ``key`` 지정: 고정 키(엔터티 ID 기반)로 저장. 키 안전성 검증 (save_local).
          서명 PNG·4070 PDF처럼 재생성 시 같은 경로로 덮어써야 하는 경우.

        NOTE(Phase 2 후보): 고정키 저장은 현재 save_local 고정 — STORAGE_MODE=s3
        에서도 로컬 디스크에 쓴다(기존 서명/4070 코드 동작 그대로 보존). S3 모드에서
        고정키를 S3에 올리도록 하는 건 동작 변경이라 통합 단계에선 손대지 않음.

        Phase 2: folder 경로(랜덤 키)이고 바이트가 이미지면 폴더 프로파일로
        WebP(+썸네일) 인코딩 후 ``...webp`` key 반환. 비이미지면 기존 upload_bytes
        (원본 그대로 저장)로 폴백. 고정키(key=) 경로는 인코딩하지 않음(서명/PDF).
        """
        if key is not None:
            validate_upload_key(key)
            self.save_local(key, data)
            return key
        validate_folder(folder)

        profile = profile_for_folder(folder)
        if profile.encode:
            derivatives = render_derivatives(data, profile)
            if derivatives:
                webp_key = self._final_key(filename, folder, ext="webp")
                return self._store_derivatives(webp_key, derivatives)

        # 비이미지/디코딩 실패 → 기존 동작(원본 그대로 저장)
        return self.upload_bytes(data, filename, folder, content_type=content_type)


storage_service: StorageService = StorageService()
