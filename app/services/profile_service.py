"""프로필 서비스 — 현재 사용자 프로필 조회/수정 비즈니스 로직.

Profile Service — Business logic for current user's profile read/update.
Handles self-service profile management via the app-facing API.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.alert_categories import CATEGORIES, normalize_preferences
from app.models.alert_preference_audit import AlertPreferenceAudit
from app.models.user import Role, User
from app.repositories.user_repository import user_repository
from app.schemas.user import (
    AlertCategoryChannel,
    AlertCategoryMeta,
    AlertPreferencesResponse,
    AlertPreferencesUpdate,
    ProfileResponse,
    ProfileUpdate,
)
from app.utils.exceptions import BadRequestError, DuplicateError


class ProfileService:
    """프로필 관련 비즈니스 로직을 처리하는 서비스.

    Service handling profile business logic.
    Provides read and update operations for the current user's own profile.
    """

    async def _resolve_role_name(self, db: AsyncSession, role_id: UUID) -> str:
        """역할 ID로 역할 이름을 조회합니다.

        Resolve role name from a role ID.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            role_id: 역할 UUID (Role UUID)

        Returns:
            str: 역할 이름 또는 "Unknown" (Role name or "Unknown")
        """
        result = await db.execute(
            select(Role.name).where(Role.id == role_id)
        )
        role_name: str | None = result.scalar()
        return role_name or "Unknown"

    def _to_response(self, user: User, role_name: str) -> ProfileResponse:
        """사용자 모델을 프로필 응답 스키마로 변환합니다.

        Convert a User model instance to a ProfileResponse schema.

        Args:
            user: 사용자 모델 인스턴스 (User model instance)
            role_name: 조회된 역할 이름 (Resolved role name)

        Returns:
            ProfileResponse: 프로필 응답 (Profile response)
        """
        return ProfileResponse(
            id=str(user.id),
            username=user.username,
            full_name=user.full_name,
            email=user.email,
            role_name=role_name,
            organization_id=str(user.organization_id),
            preferred_language=user.preferred_language,
        )

    async def get_profile(
        self,
        db: AsyncSession,
        current_user: User,
    ) -> ProfileResponse:
        """현재 사용자의 프로필을 조회합니다.

        Retrieve the current user's profile.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            current_user: 인증된 사용자 모델 (Authenticated user model)

        Returns:
            ProfileResponse: 프로필 응답 (Profile response)
        """
        role_name: str = await self._resolve_role_name(db, current_user.role_id)
        return self._to_response(current_user, role_name)

    async def update_profile(
        self,
        db: AsyncSession,
        current_user: User,
        data: ProfileUpdate,
    ) -> ProfileResponse:
        """현재 사용자의 프로필을 업데이트합니다.

        Update the current user's profile with provided fields.
        Only non-None fields from the update data are applied.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            current_user: 인증된 사용자 모델 (Authenticated user model)
            data: 업데이트 데이터 (Profile update data)

        Returns:
            ProfileResponse: 업데이트된 프로필 응답 (Updated profile response)
        """
        # None이 아닌 필드만 업데이트 — Only update non-None (provided) fields
        update_data: dict = data.model_dump(exclude_unset=True)

        # username 변경 시 조직 내 중복 검사
        if "username" in update_data and update_data["username"] is not None:
            new_username: str = update_data["username"].strip()
            if not new_username:
                raise BadRequestError("Username cannot be empty")
            update_data["username"] = new_username
            if new_username != current_user.username:
                exists: bool = await user_repository.exists(
                    db, {
                        "organization_id": current_user.organization_id,
                        "username": new_username,
                    }
                )
                if exists:
                    raise DuplicateError("Username already exists in this organization")

        # 이메일 변경 시 인증 상태 리셋
        if "email" in update_data and update_data["email"] != current_user.email:
            update_data["email_verified"] = False

        # full_name 변경(레거시 경로) — 구조화 이름(first/middle/last)과의 desync 방지.
        # 이름이 실제로 바뀌면 구조화 파트를 비운다 (display_name 은 full_name 폴백).
        # 조합 규칙 단일화: app/utils/names.py 참조.
        if "full_name" in update_data and update_data["full_name"] is not None:
            new_full: str = update_data["full_name"].strip()
            if not new_full:
                raise BadRequestError("Name cannot be empty")
            update_data["full_name"] = new_full
            if new_full != current_user.full_name:
                update_data["first_name"] = None
                update_data["middle_name"] = None
                update_data["last_name"] = None

        try:
            for field, value in update_data.items():
                if hasattr(current_user, field):
                    setattr(current_user, field, value)

            await db.flush()
            await db.refresh(current_user)
            await db.commit()
        except Exception:
            await db.rollback()
            raise

        role_name: str = await self._resolve_role_name(db, current_user.role_id)
        return self._to_response(current_user, role_name)


    # --- 알림 선호 (Alert preferences) ---

    def _build_preferences_response(self, user: User) -> AlertPreferencesResponse:
        """카테고리 메타 + 사용자 현재 prefs 를 응답 형태로."""
        prefs_raw = user.alert_preferences or {}
        prefs: dict[str, AlertCategoryChannel] = {}
        for code, val in prefs_raw.items():
            if not isinstance(val, dict):
                continue
            prefs[code] = AlertCategoryChannel(
                in_app=val.get("in_app"),
                email=val.get("email"),
                push=val.get("push"),
            )
        categories = [
            AlertCategoryMeta(
                code=c["code"],
                label=c["label"],
                description=c["description"],
                email_available=c["email_available"],
                push_available=True,
            )
            for c in CATEGORIES
        ]
        return AlertPreferencesResponse(categories=categories, preferences=prefs)

    async def get_alert_preferences(
        self,
        current_user: User,
    ) -> AlertPreferencesResponse:
        """현재 사용자의 알림 선호 + 카테고리 메타 조회."""
        return self._build_preferences_response(current_user)


    # 이력에 남기는 채널 목록. 여기 없는 키는 기록하지 않는다.
    _AUDITED_CHANNELS = ("in_app", "email", "push")

    def _record_preference_changes(
        self,
        db: AsyncSession,
        *,
        user: User,
        changed_by: User,
        before: dict,
        after: dict,
        user_agent: str | None,
    ) -> None:
        """before → after 를 (카테고리, 채널) 단위로 비교해 변경분만 이력 행으로 추가.

        값은 3-상태(True/False/None)다. None 은 "미설정 = 기본값 따름" 으로
        False 와 의미가 다르므로 그대로 보존한다. 값이 안 바뀐 항목은 기록하지 않는다.
        """
        codes = set(before) | set(after)
        for code in codes:
            old_ch = before.get(code) or {}
            new_ch = after.get(code) or {}
            if not isinstance(old_ch, dict):
                old_ch = {}
            if not isinstance(new_ch, dict):
                new_ch = {}
            for channel in self._AUDITED_CHANNELS:
                old_val = old_ch.get(channel)
                new_val = new_ch.get(channel)
                if old_val == new_val:
                    continue
                db.add(
                    AlertPreferenceAudit(
                        organization_id=user.organization_id,
                        user_id=user.id,
                        changed_by_user_id=changed_by.id,
                        category_code=code,
                        channel=channel,
                        old_value=old_val,
                        new_value=new_val,
                        user_agent=user_agent,
                    )
                )

    async def update_alert_preferences(
        self,
        db: AsyncSession,
        current_user: User,
        data: AlertPreferencesUpdate,
        user_agent: str | None = None,
    ) -> AlertPreferencesResponse:
        """알림 선호 부분 업데이트 — 받은 카테고리/채널만 머지.

        삭제 의도(default 로 복귀)는 클라가 명시적으로 in_app/email 에 None 보내면
        해당 키 제거. 보내지 않은 카테고리는 기존 값 유지.
        """
        existing: dict = dict(current_user.alert_preferences or {})

        # 클라 입력 정규화 — 알 수 없는 카테고리/필드 제거
        raw_input: dict = {
            code: val.model_dump(exclude_none=True)
            for code, val in data.preferences.items()
        }
        cleaned = normalize_preferences(raw_input)

        # 명시적 None 처리 — 클라가 "기본값으로" 라고 명시 → 키 삭제
        # exclude_none 으로 이미 None 은 빠진 상태. 별도 처리 불필요.
        for code, channels in cleaned.items():
            merged = dict(existing.get(code, {}))
            merged.update(channels)
            existing[code] = merged

        # 모두 None/default 가 된 카테고리는 삭제 (저장소 깔끔하게)
        existing = {k: v for k, v in existing.items() if v}

        # 변경 이력 — "언제 껐는지" 를 증명할 수 있어야 하므로 저장 전에 diff 를 남긴다.
        # 같은 트랜잭션에 넣어, 설정 저장이 실패하면 이력도 남지 않게 한다.
        self._record_preference_changes(
            db,
            user=current_user,
            changed_by=current_user,
            before=dict(current_user.alert_preferences or {}),
            after=existing,
            user_agent=user_agent,
        )

        try:
            current_user.alert_preferences = existing
            await db.flush()
            await db.refresh(current_user)
            await db.commit()
        except Exception:
            await db.rollback()
            raise

        return self._build_preferences_response(current_user)


# 싱글턴 인스턴스 — Singleton instance
profile_service: ProfileService = ProfileService()
