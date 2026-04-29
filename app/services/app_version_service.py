"""App version service — 릴리스 카탈로그 조회/등록 + pre-signed URL 발급."""

from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.app_version import AppVersion
from app.services.storage_service import storage_service


def _semver_tuple(v: str) -> tuple:
    """semver 문자열 → 비교용 튜플. 비표준이면 raw 문자열 반환."""
    parts = v.split(".")
    out = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            return (v,)
    return tuple(out)


class AppVersionService:
    def attendance_channel(self) -> str:
        """현재 서버 환경에 대응하는 attendance 앱 채널명."""
        return f"attendance_{settings.APP_ENV}"

    async def get_for_channel(
        self, db: AsyncSession, channel: str
    ) -> tuple[Optional[AppVersion], Optional[str]]:
        """채널의 최신 릴리스 + 강제 최소 버전 반환.

        Returns:
            (latest_row, min_version_string) — 없으면 둘 다 None.
        """
        latest_q = select(AppVersion).where(
            AppVersion.channel == channel,
            AppVersion.is_latest.is_(True),
        )
        latest = (await db.execute(latest_q)).scalar_one_or_none()

        min_q = select(AppVersion.version).where(
            AppVersion.channel == channel,
            AppVersion.is_min_required.is_(True),
        )
        min_versions = (await db.execute(min_q)).scalars().all()
        min_version = (
            max(min_versions, key=_semver_tuple) if min_versions else None
        )
        return latest, min_version

    def presigned_download_url(self, key: str) -> str:
        return storage_service.generate_presigned_download_url(key, expires=600)

    async def create(
        self,
        db: AsyncSession,
        *,
        channel: str,
        version: str,
        s3_key: str,
        is_latest: bool,
        is_min_required: bool,
        release_notes: Optional[str],
    ) -> AppVersion:
        """새 릴리스 등록. is_latest=True 면 같은 채널의 기존 latest를 false로 내림."""
        if is_latest:
            await db.execute(
                update(AppVersion)
                .where(
                    AppVersion.channel == channel,
                    AppVersion.is_latest.is_(True),
                )
                .values(is_latest=False)
            )
        row = AppVersion(
            channel=channel,
            version=version,
            s3_key=s3_key,
            is_latest=is_latest,
            is_min_required=is_min_required,
            release_notes=release_notes,
        )
        db.add(row)
        await db.flush()
        return row


app_version_service = AppVersionService()
