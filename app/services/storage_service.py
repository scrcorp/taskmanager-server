"""스토리지 서비스 — S3 또는 로컬 파일 저장.

Storage Service — S3 presigned URL or local file storage.
STORAGE_MODE 환경변수로 모드 결정 ("local" 또는 "s3").
S3 모드에서 access key가 없으면 IAM role을 사용합니다 (EC2 배포 시).
모든 업로드는 temp/ 폴더에 먼저 저장되고, finalize_upload()로 최종 위치로 이동합니다.

폴더별 경로는 .env에서 설정 가능 (STORAGE_FOLDER_*). 기본값:
  reviews, completions, profiles, announcements, issues
"""

import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

# 로컬 업로드 디렉토리 — .env의 LOCAL_UPLOADS_DIR 또는 server/uploads/
_SERVER_ROOT: Path = Path(__file__).resolve().parent.parent.parent
UPLOADS_DIR: Path = Path(settings.LOCAL_UPLOADS_DIR) if settings.LOCAL_UPLOADS_DIR else _SERVER_ROOT / "uploads"

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
    """파일 업로드 서비스 — S3 또는 로컬 모드 자동 선택."""

    def __init__(self) -> None:
        self._client = None

    @property
    def is_local(self) -> bool:
        return settings.STORAGE_MODE != "s3"

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

    def _generate_key(self, filename: str, folder: str) -> str:
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
        date_prefix = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        resolved = resolve_folder(folder)
        return f"temp/{resolved}/{date_prefix}/{uuid.uuid4().hex}.{ext}"

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
        """presigned PUT URL과 temp file URL을 반환합니다.

        모든 업로드는 temp/ 하위에 저장됩니다.
        finalize_upload()로 최종 위치로 이동해야 합니다.

        Args:
            base_url: 요청의 base URL (request.base_url에서 추출, 로컬 모드 전용)
            upload_path_prefix: 업로드 엔드포인트 prefix (admin/app 구분)
        """
        key = self._generate_key(filename, folder)

        if self.is_local:
            upload_url = f"{base_url}{upload_path_prefix}/upload/{key}"
            file_url = f"{base_url}/uploads/{key}"
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

    def save_local(self, key: str, data: bytes) -> str:
        """로컬 파일 저장. 경로를 반환합니다."""
        path = UPLOADS_DIR / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def _extract_key(self, file_url: str) -> str | None:
        """file URL에서 storage key를 추출합니다."""
        if self.is_local:
            # /uploads/ 마커로 key 추출 — 호스트에 무관하게 동작
            marker = "/uploads/"
            idx = file_url.find(marker)
            if idx != -1:
                return file_url[idx + len(marker):]
        else:
            prefix = f"https://{settings.AWS_S3_BUCKET}.s3.{settings.AWS_S3_REGION}.amazonaws.com/"
            if file_url.startswith(prefix):
                return file_url[len(prefix):]
        return None

    def finalize_upload(self, file_url: str) -> str:
        """temp 파일을 최종 위치로 이동합니다. 최종 file_url을 반환합니다.

        temp/ 경로가 아닌 파일은 그대로 반환합니다.
        멱등(idempotent): 이미 finalize된 파일은 최종 URL을 반환합니다.
        """
        key = self._extract_key(file_url)
        if not key or not key.startswith("temp/"):
            return file_url

        final_key = key[len("temp/"):]

        if self.is_local:
            src = UPLOADS_DIR / key
            dst = UPLOADS_DIR / final_key
            # file_url에서 base URL 추출 — 하드코딩 방지
            marker = "/uploads/"
            idx = file_url.find(marker)
            base = file_url[:idx] if idx != -1 else "http://localhost:8000"
            final_url = f"{base}/uploads/{final_key}"
            if not src.exists():
                # 이미 finalize됨 또는 파일 없음 — 최종 URL 반환
                return final_url
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return final_url

        # S3: temp → final copy + delete (이미 없으면 무시)
        try:
            self.client.copy_object(
                Bucket=settings.AWS_S3_BUCKET,
                Key=final_key,
                CopySource={"Bucket": settings.AWS_S3_BUCKET, "Key": key},
            )
            self.client.delete_object(Bucket=settings.AWS_S3_BUCKET, Key=key)
        except self.client.exceptions.NoSuchKey:
            pass
        return f"https://{settings.AWS_S3_BUCKET}.s3.{settings.AWS_S3_REGION}.amazonaws.com/{final_key}"


    def delete_file(self, file_url: str) -> bool:
        """S3 또는 로컬에서 파일을 삭제합니다. 성공 여부를 반환합니다.

        file_url이 빈 문자열이거나 None이면 무시합니다.
        """
        if not file_url:
            return False

        key = self._extract_key(file_url)
        if not key:
            return False

        if self.is_local:
            path = UPLOADS_DIR / key
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
