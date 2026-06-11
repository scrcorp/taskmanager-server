"""경고 사유 카테고리 서비스 — Warning category 비즈니스 로직 (v1.1).

org별 카테고리 추가/이름변경/숨김/삭제 + 시드 + 검증. 핵심 규칙:
    - 시드: org 생성 시 기본 12종(app.core.warning.DEFAULT_WARNING_CATEGORIES).
      refusal_overtime=hidden, other=system. idempotent (이미 있으면 skip).
    - 삭제 = soft(deleted_at). **같은 code 재추가 → 새 row 가 아니라 revive**
      (deleted_at=NULL, is_hidden=False, label 갱신).
    - system(other) 은 숨김/삭제 불가.
    - 검증: 경고 categories[] 코드는 org 비삭제 카테고리여야 함. 수정 시엔 그 경고가
      이미 가진 코드(legacy)도 허용 (삭제된 카테고리 보존 — '(removed)' 잠금 표시).
    - 관리 권한(Owner only)은 라우터에서 강제. 서비스는 org-scope 로직만.
"""

import re
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.warning import DEFAULT_WARNING_CATEGORIES, SYSTEM_CATEGORY_SORT
from app.models.warning_category import WarningCategory
from app.repositories.warning_category_repository import warning_category_repository
from app.utils.exceptions import BadRequestError, NotFoundError


def slugify_code(label: str) -> str:
    """label → code 슬러그 (소문자, 영숫자만, 언더스코어 구분, 최대 40)."""
    code = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return code[:40]


class WarningCategoryService:
    """경고 카테고리 서비스 — 시드 + CRUD(revive) + 검증."""

    async def seed_defaults(self, db: AsyncSession, organization_id: UUID) -> None:
        """org 기본 카테고리 시드 (idempotent — 이미 있으면 skip).

        org 생성 트랜잭션 안에서 호출 → 여기선 flush 만(commit 은 호출자).
        마이그레이션 backfill 에서도 같은 목록을 사용.
        """
        existing = await warning_category_repository.count_for_org(db, organization_id)
        if existing > 0:
            return
        for i, (code, label, is_hidden, is_system) in enumerate(DEFAULT_WARNING_CATEGORIES):
            sort = SYSTEM_CATEGORY_SORT if is_system else (i + 1) * 10
            db.add(
                WarningCategory(
                    organization_id=organization_id,
                    code=code,
                    label=label,
                    sort_order=sort,
                    is_hidden=is_hidden,
                    is_system=is_system,
                )
            )
        await db.flush()

    async def list_categories(
        self,
        db: AsyncSession,
        organization_id: UUID,
        *,
        include_hidden: bool = True,
    ) -> list[WarningCategory]:
        """org 카테고리 목록 (비삭제). 관리=include_hidden True, picker=False."""
        return await warning_category_repository.list_for_org(
            db, organization_id, include_hidden=include_hidden, include_deleted=False
        )

    async def create_category(
        self, db: AsyncSession, organization_id: UUID, label: str
    ) -> WarningCategory:
        """카테고리 추가. 같은 code 가 이미 있으면(삭제됐든 아니든) 분기.

        - 살아있는 동일 code → 400 (중복).
        - 삭제된 동일 code → **revive** (deleted_at 해제, hidden 해제, label 갱신).
        - 없으면 새 row (sort_order = 비시스템 max + 10).
        """
        code = slugify_code(label)
        if not code:
            raise BadRequestError("Category name must contain letters or numbers")

        existing = await warning_category_repository.get_by_code(db, organization_id, code)
        if existing is not None:
            if existing.deleted_at is None:
                raise BadRequestError("A category with this name already exists")
            # revive — 좀비 row 안 만들고 살림
            existing.deleted_at = None
            existing.is_hidden = False
            existing.label = label
            existing.updated_at = datetime.now(timezone.utc)
            await db.flush()
            await db.commit()
            await db.refresh(existing)
            return existing

        max_sort = await warning_category_repository.max_sort_order(db, organization_id)
        category = WarningCategory(
            organization_id=organization_id,
            code=code,
            label=label,
            sort_order=max_sort + 10,
            is_hidden=False,
            is_system=False,
        )
        db.add(category)
        await db.flush()
        await db.commit()
        await db.refresh(category)
        return category

    async def update_category(
        self,
        db: AsyncSession,
        organization_id: UUID,
        category_id: UUID,
        *,
        label: str | None = None,
        is_hidden: bool | None = None,
    ) -> WarningCategory:
        """이름 변경 / 숨김 토글. system(other) 은 숨김 불가."""
        category = await warning_category_repository.get_by_id(
            db, organization_id, category_id
        )
        if category is None:
            raise NotFoundError("Category not found")

        if is_hidden is not None:
            if category.is_system and is_hidden:
                raise BadRequestError("The system category cannot be hidden")
            category.is_hidden = is_hidden
        if label is not None:
            category.label = label
        category.updated_at = datetime.now(timezone.utc)
        await db.flush()
        await db.commit()
        await db.refresh(category)
        return category

    async def delete_category(
        self, db: AsyncSession, organization_id: UUID, category_id: UUID
    ) -> None:
        """soft delete. system(other) 은 삭제 불가. (같은 code 재추가 시 revive)"""
        category = await warning_category_repository.get_by_id(
            db, organization_id, category_id
        )
        if category is None:
            raise NotFoundError("Category not found")
        if category.is_system:
            raise BadRequestError("The system category cannot be deleted")
        category.deleted_at = datetime.now(timezone.utc)
        await db.flush()
        await db.commit()

    async def validate_codes(
        self,
        db: AsyncSession,
        organization_id: UUID,
        codes: list[str],
        *,
        existing_codes: list[str] | None = None,
    ) -> None:
        """경고 categories[] 코드 검증. 비삭제 카테고리 ∪ (수정 시) 기존 경고 코드.

        수정 시 그 경고가 이미 가진 코드(삭제된 카테고리 = legacy)는 보존 허용.
        """
        valid = await warning_category_repository.non_deleted_codes(db, organization_id)
        if existing_codes:
            valid = valid | set(existing_codes)
        unknown = [c for c in codes if c not in valid]
        if unknown:
            raise BadRequestError(
                f"Unknown or unavailable reason category code(s): {', '.join(sorted(set(unknown)))}"
            )

    def to_response(self, category: WarningCategory) -> dict:
        """WarningCategory → WarningCategoryResponse dict."""
        return {
            "id": str(category.id),
            "code": category.code,
            "label": category.label,
            "sort_order": category.sort_order,
            "is_hidden": category.is_hidden,
            "is_system": category.is_system,
        }


warning_category_service = WarningCategoryService()
