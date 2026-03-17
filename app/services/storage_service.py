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
  reviews, completions, profiles, announcements, issues
"""

import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

# 로컬 버킷 디렉토리 — .env의 LOCAL_BUCKET_DIR 또는 ~/.taskmanager/bucket/dev/
_DEFAULT_BUCKET_DIR: Path = Path.home() / ".taskmanager" / "bucket" / "dev"
BUCKET_DIR: Path = Path(settings.LOCAL_BUCKET_DIR) if settings.LOCAL_BUCKET_DIR else _DEFAULT_BUCKET_DIR
FALLBACK_BUCKET_DIR: Path | None = (
    Path(settings.LOCAL_FALLBACK_BUCKET_DIR) if settings.LOCAL_FALLBACK_BUCKET_DIR else None
)

# 폴더별 경로 매핑 — .env에서 오버라이드 가능
FOLDER_MAP: dict[str, str] = {
    "reviews": settings.STORAGE_FOLDER_REVIEWS,
    "completions": settings.STORAGE_FOLDER_COMPLETIONS,
    "profiles": settings.STORAGE_FOLDER_PROFILES,
    "announcements": settings.STORAGE_FOLDER_ANNOUNCEMENTS,
    "issues": settings.STORAGE_FOLDER_ISSUES,
}


def resolve_folder(folder: str) -> str:
    """폴더 이름을 .env 설정값으로 변환합니다. 미등록 폴더는 그대로 반환."""
    return FOLDER_MAP.get(folder, folder)


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

    # ── 로컬 파일 저장 ────────────────────────────────────────

    def save_local(self, key: str, data: bytes) -> str:
        """로컬 파일 저장. 경로를 반환합니다."""
        path = BUCKET_DIR / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

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

    def resolve_url(self, key: str | None) -> str | None:
        """상대경로(key) → 접근 가능한 전체 URL.

        fallback 설정 시: 현재 버킷에 없으면 fallback에서 복사 후 URL 반환.
        fallback 미설정 시 (prod/dev): 존재 확인 없이 바로 URL 반환.
        어디에도 없으면 None 반환.
        """
        if not key:
            return None

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

    def _build_url(self, key: str) -> str:
        """key → 현재 환경의 전체 URL."""
        if self.is_local:
            return f"/bucket/{key}"
        return f"https://{settings.AWS_S3_BUCKET}.s3.{settings.AWS_S3_REGION}.amazonaws.com/{key}"

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


storage_service: StorageService = StorageService()
