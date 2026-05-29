"""App version service — 릴리스 카탈로그 조회/등록 + pre-signed URL 발급."""

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.app_version import AppVersion
from app.services.storage_service import storage_service


# attendance APK 파일명 패턴: app-releases/attendance/v{X.Y.Z}/htma_{X.Y.Z+N}.apk
# 우선 파일명에서 +build 까지 포함된 풀버전 추출, 없으면 경로의 v{version} 사용.
_FULL_VERSION_RE = re.compile(r"_(\d+\.\d+\.\d+(?:\+\d+)?)\.apk$", re.IGNORECASE)
_PATH_VERSION_RE = re.compile(r"v(\d+\.\d+\.\d+)/", re.IGNORECASE)


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


def _version_sort_key(version: str) -> tuple:
    """'1.0.9+27' → (1, 0, 9, 27) 비교용. semver + build number 동시 비교."""
    semver, _, build = version.partition("+")
    parts: list[int] = []
    for p in semver.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            return (0, 0, 0, 0)
    while len(parts) < 3:
        parts.append(0)
    build_num = int(build) if build.isdigit() else 0
    return tuple(parts[:3]) + (build_num,)


def _extract_version_from_key(key: str) -> Optional[str]:
    """S3 key 또는 로컬 path 에서 버전 추출.

    파일명 우선 (`htma_1.0.9+27.apk` → `1.0.9+27`), 없으면 경로 (`v1.0.9/` → `1.0.9`).
    """
    m = _FULL_VERSION_RE.search(key)
    if m:
        return m.group(1)
    m = _PATH_VERSION_RE.search(key)
    if m:
        return m.group(1)
    return None


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

    def get_latest_attendance_from_storage(self) -> Optional[dict]:
        """현재 환경 bucket 의 attendance APK 들 중 버전 가장 높은 것 반환.

        S3 list (또는 local bucket dir list) → 파일명/path 에서 버전 파싱 → 최신 선택.
        DB 의 `is_latest` 플래그에 의존하지 않으므로 등록 자동화 누락에 안전.

        버킷이 환경별로 분리돼 있으므로 파일명 필터 없이 버전만 비교.

        Returns:
            { version, key, url, uploaded_at } 또는 None (release 없음).
        """
        prefix = "app-releases/attendance/"
        candidates: list[tuple[str, str, datetime]] = []

        if storage_service.is_local:
            base = Path(settings.LOCAL_BUCKET_DIR) / "app-releases" / "attendance"
            if not base.exists():
                return None
            for apk in base.rglob("*.apk"):
                rel_key = str(apk.relative_to(Path(settings.LOCAL_BUCKET_DIR)))
                version = _extract_version_from_key(rel_key)
                if version is None:
                    continue
                mtime = datetime.fromtimestamp(apk.stat().st_mtime)
                candidates.append((version, rel_key, mtime))
        else:
            client = storage_service.client
            if client is None:
                return None
            bucket = settings.AWS_S3_BUCKET
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if not key.endswith(".apk"):
                        continue
                    version = _extract_version_from_key(key)
                    if version is None:
                        continue
                    candidates.append((version, key, obj["LastModified"]))

        if not candidates:
            return None

        candidates.sort(key=lambda c: _version_sort_key(c[0]), reverse=True)
        version, key, uploaded_at = candidates[0]
        return {
            "version": version,
            "key": key,
            "url": self.presigned_download_url(key),
            "uploaded_at": uploaded_at,
        }

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
