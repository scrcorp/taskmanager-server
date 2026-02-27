"""AWS S3 스토리지 서비스 — presigned URL 생성.

S3 Storage Service — Generates presigned URLs for direct browser uploads.
"""

import uuid
from datetime import datetime, timezone

import boto3
from botocore.config import Config as BotoConfig

from app.config import settings


class StorageService:
    """S3 presigned URL 생성 서비스."""

    def __init__(self) -> None:
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client(
                "s3",
                region_name=settings.AWS_S3_REGION,
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                config=BotoConfig(signature_version="s3v4"),
            )
        return self._client

    def generate_presigned_upload_url(
        self,
        filename: str,
        content_type: str,
        folder: str = "reviews",
        expires: int = 3600,
    ) -> dict[str, str]:
        """presigned PUT URL과 최종 file URL을 반환합니다.

        Args:
            filename: 원본 파일명 (확장자 추출용)
            content_type: MIME type (e.g. "image/jpeg")
            folder: S3 키 prefix
            expires: URL 유효 시간(초)

        Returns:
            {"upload_url": presigned PUT URL, "file_url": public GET URL, "key": S3 key}
        """
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
        date_prefix = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        key = f"{folder}/{date_prefix}/{uuid.uuid4().hex}.{ext}"

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


storage_service: StorageService = StorageService()
