"""App Version 모델 — 모바일/태블릿 앱 (attendance, staff 등) 릴리스 카탈로그.

Sideload APK 배포 환경에서 클라이언트가 자체적으로 업데이트 강제/유도할 수 있게
서버가 채널별 최신/최소 버전을 노출한다.

흐름:
    1. CI가 APK 빌드 → S3 업로드 → POST /api/v1/admin/app-versions 호출
    2. 앱이 부팅 시 GET /api/v1/attendance/app-version 호출
    3. current < min_required → 강제 update blocker
       current < latest      → 권장 update banner
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AppVersion(Base):
    """앱 릴리스 1건.

    Attributes:
        id: 레코드 PK
        channel: 배포 채널 식별자 (예: "attendance_production", "attendance_staging")
        version: semver 문자열 (예: "1.0.5")
        s3_key: S3 객체 키 (예: "app-releases/attendance/v1.0.5/tma.apk").
                서버가 환경별 버킷 + 이 key 로 pre-signed URL 발급.
        is_latest: 채널의 최신 릴리스 표시 (채널당 1개). 새 릴리스 등록 시 이전 row 자동 false.
        is_min_required: 이 버전 미만 클라이언트는 차단(blocker). 채널 내 여러 이력 허용,
                         그 중 가장 높은 semver 가 floor.
        release_notes: 릴리스 노트 (markdown)
        released_at: 릴리스 시각
    """

    __tablename__ = "app_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    s3_key: Mapped[str] = mapped_column(String(512), nullable=False)
    is_latest: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_min_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    release_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    released_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_app_versions_channel", "channel"),
        Index("ix_app_versions_channel_latest", "channel", "is_latest"),
    )
