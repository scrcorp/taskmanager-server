"""스토리지 서비스 — S3 또는 로컬 파일 저장.

Storage Service — S3 presigned URL or local file storage.
AWS 키가 비어있으면 자동으로 로컬 모드로 전환됩니다.
모든 업로드는 temp/ 폴더에 먼저 저장되고, finalize_upload()로 최종 위치로 이동합니다.
"""

import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

# 로컬 업로드 디렉토리 — .env의 LOCAL_UPLOADS_DIR 또는 server/uploads/
_SERVER_ROOT: Path = Path(__file__).resolve().parent.parent.parent
UPLOADS_DIR: Path = Path(settings.LOCAL_UPLOADS_DIR) if settings.LOCAL_UPLOADS_DIR else _SERVER_ROOT / "uploads"


class StorageService:
    """파일 업로드 서비스 — S3 또는 로컬 모드 자동 선택."""

    def __init__(self) -> None:
        self._client = None

    @property
    def is_local(self) -> bool:
        return not settings.AWS_ACCESS_KEY_ID or not settings.AWS_S3_BUCKET

    @property
    def client(self):
        if self.is_local:
            return None
        if self._client is None:
            import boto3
            from botocore.config import Config as BotoConfig

            self._client = boto3.client(
                "s3",
                region_name=settings.AWS_S3_REGION,
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                config=BotoConfig(signature_version="s3v4"),
            )
        return self._client

    def _generate_key(self, filename: str, folder: str) -> str:
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
        date_prefix = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        return f"temp/{folder}/{date_prefix}/{uuid.uuid4().hex}.{ext}"

    def generate_presigned_upload_url(
        self,
        filename: str,
        content_type: str,
        folder: str = "reviews",
        expires: int = 3600,
    ) -> dict[str, str]:
        """presigned PUT URL과 temp file URL을 반환합니다.

        모든 업로드는 temp/ 하위에 저장됩니다.
        finalize_upload()로 최종 위치로 이동해야 합니다.
        """
        key = self._generate_key(filename, folder)

        if self.is_local:
            base = f"http://localhost:8000/api/v1/admin/storage/upload/{key}"
            file_url = f"http://localhost:8000/uploads/{key}"
            return {"upload_url": base, "file_url": file_url, "key": key}

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
            prefix = "http://localhost:8000/uploads/"
            if file_url.startswith(prefix):
                return file_url[len(prefix):]
        else:
            prefix = f"https://{settings.AWS_S3_BUCKET}.s3.{settings.AWS_S3_REGION}.amazonaws.com/"
            if file_url.startswith(prefix):
                return file_url[len(prefix):]
        return None

    def finalize_upload(self, file_url: str) -> str:
        """temp 파일을 최종 위치로 이동합니다. 최종 file_url을 반환합니다.

        temp/ 경로가 아닌 파일은 그대로 반환합니다.
        """
        key = self._extract_key(file_url)
        if not key or not key.startswith("temp/"):
            return file_url

        final_key = key[len("temp/"):]

        if self.is_local:
            src = UPLOADS_DIR / key
            dst = UPLOADS_DIR / final_key
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return f"http://localhost:8000/uploads/{final_key}"

        self.client.copy_object(
            Bucket=settings.AWS_S3_BUCKET,
            Key=final_key,
            CopySource={"Bucket": settings.AWS_S3_BUCKET, "Key": key},
        )
        self.client.delete_object(Bucket=settings.AWS_S3_BUCKET, Key=key)
        return f"https://{settings.AWS_S3_BUCKET}.s3.{settings.AWS_S3_REGION}.amazonaws.com/{final_key}"


storage_service: StorageService = StorageService()
