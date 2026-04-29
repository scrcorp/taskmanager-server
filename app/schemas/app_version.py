"""App version schemas — sideload APK 배포용 클라이언트/관리자 응답."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class AppVersionResponse(BaseModel):
    """클라이언트(앱) 가 받는 버전 체크 응답.

    min_version: 이 미만은 강제 차단 (None 이면 enforcement 없음)
    latest_version: 최신 버전 (None 이면 등록 릴리스 없음)
    download_url: latest 버전 APK pre-signed URL (S3) 또는 로컬 URL (None 이면 다운로드 불가)
    release_notes: 최신 릴리스 노트
    """

    model_config = ConfigDict(populate_by_name=True)

    min_version: Optional[str] = Field(default=None, alias="min_version")
    latest_version: Optional[str] = Field(default=None, alias="latest_version")
    download_url: Optional[str] = Field(default=None, alias="download_url")
    release_notes: Optional[str] = Field(default=None, alias="release_notes")


class AppVersionCreateRequest(BaseModel):
    """관리자가 새 릴리스 등록할 때 보내는 페이로드 (CI 또는 admin UI)."""

    channel: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=32)
    s3_key: str = Field(min_length=1, max_length=512)
    is_latest: bool = True
    is_min_required: bool = False
    release_notes: Optional[str] = None


class AppVersionRow(BaseModel):
    """관리자/CI 응답용."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    channel: str
    version: str
    s3_key: str
    is_latest: bool
    is_min_required: bool
    release_notes: Optional[str] = None
    released_at: datetime
