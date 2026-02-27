"""스토리지 서비스 — S3 또는 로컬 파일 저장.

Storage Service — S3 presigned URL or local file storage.
AWS 키가 비어있으면 자동으로 로컬 모드로 전환됩니다.
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

# 로컬 업로드 디렉토리 — server/uploads/
UPLOADS_DIR: Path = Path(__file__).resolve().parent.parent / "uploads"


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
        return f"{folder}/{date_prefix}/{uuid.uuid4().hex}.{ext}"

    def generate_presigned_upload_url(
        self,
        filename: str,
        content_type: str,
        folder: str = "reviews",
        expires: int = 3600,
    ) -> dict[str, str]:
        """presigned PUT URL과 최종 file URL을 반환합니다.

        로컬 모드: 서버 자체 PUT 엔드포인트 URL 반환
        S3 모드: AWS presigned PUT URL 반환
        """
        key = self._generate_key(filename, folder)

        if self.is_local:
            # 로컬 모드 — 서버 PUT 엔드포인트를 upload_url로 사용
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


storage_service: StorageService = StorageService()
