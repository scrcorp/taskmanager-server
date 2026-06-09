"""경고 서비스 — Warning v1 비즈니스 로직.

Warning Service — create/update/resolve/delete, 방향 검증, subject-store 검증,
seq 발급, 소유권(Owner 전체 / GM 본인) 수정·삭제, 직원별 카운트, 응답 빌드.

핵심 규칙:
    - org-scope: 모든 조회는 organization_id 로 격리.
    - 방향 검증: app.core.permissions.can_warn (발행자보다 엄격히 낮은 권한만 대상).
    - subject-store: 경고의 store 는 대상 직원이 배정된 매장(user_stores) 이어야 한다.
    - 사람 ID: seq = org당 max+1. UNIQUE(org, seq) 충돌 시 재시도.
    - 수정/삭제 소유권: Owner 는 조직 전체, 그 외(GM)는 본인 발행건만.
    - soft delete: deleted_at. 읽기는 항상 deleted_at IS NULL.
"""

from datetime import datetime, timezone
from typing import Sequence
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.permissions import can_warn, is_owner, role_priority
from app.models.organization import Organization, Store
from app.models.user import User
from app.models.user_store import UserStore
from app.models.warning import Warning
from app.repositories.warning_repository import warning_repository
from app.utils.exceptions import BadRequestError, NotFoundError


def _ref_no(seq: int) -> str:
    """seq → 사람용 ID "W-00046"."""
    return f"W-{seq:05d}"


class WarningService:
    """경고 서비스 — 대상 picker + 경고 CRUD + 카운트 + 응답 빌드."""

    # ====================================================================
    # 경고 대상 직원 (picker)
    # ====================================================================

    async def list_warnable_users(
        self,
        db: AsyncSession,
        current_user: User,
        *,
        store_id: UUID | None = None,
        q: str | None = None,
        page: int = 1,
        limit: int = 30,
    ) -> dict:
        """방향 필터된 경고 대상 직원 목록 (paginated envelope) + stores[].

        roles.priority > current_user priority (엄격히 더 낮은 권한), org-scope,
        활성, 자기 제외. 매장 접근 검증은 라우터에서 선행한다.
        N+1 제거: repository 가 role + user_stores→store 를 eager load.
        """
        page = max(1, page)
        limit = max(1, min(limit, 100))
        q_clean = q.strip() if q else None

        users, total = await warning_repository.list_warnable_users(
            db,
            current_user.organization_id,
            min_priority_exclusive=role_priority(current_user),
            exclude_user_id=current_user.id,
            store_id=store_id,
            q=q_clean,
            limit=limit,
            offset=(page - 1) * limit,
        )

        org_id = current_user.organization_id
        items: list[dict] = []
        for u in users:
            assigned = [
                us
                for us in u.user_stores
                if us.store is not None and us.store.organization_id == org_id
            ]
            assigned.sort(key=lambda us: us.created_at)
            stores = [{"id": str(us.store_id), "name": us.store.name} for us in assigned]
            primary = assigned[0] if assigned else None

            items.append(
                {
                    "id": str(u.id),
                    "full_name": u.full_name,
                    "employee_no": u.employee_no,
                    "role_name": u.role.name if u.role else "",
                    "role_priority": role_priority(u),
                    "store_id": str(primary.store_id) if primary else None,
                    "store_name": primary.store.name if primary else None,
                    "stores": stores,
                }
            )

        return {
            "items": items,
            "total": total,
            "page": page,
            "limit": limit,
            "has_more": (page * limit) < total,
        }

    # ====================================================================
    # 경고 조회 / 카운트
    # ====================================================================

    async def list_warnings(
        self,
        db: AsyncSession,
        organization_id: UUID,
        *,
        store_ids: list[UUID] | None = None,
        status: str | None = None,
        category: str | None = None,
        subject_user_id: UUID | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Warning], int]:
        return await warning_repository.list_active(
            db,
            organization_id,
            store_ids=store_ids,
            status=status,
            category=category,
            subject_user_id=subject_user_id,
            page=page,
            per_page=per_page,
        )

    async def get_warning(
        self, db: AsyncSession, warning_id: UUID, organization_id: UUID
    ) -> Warning:
        """경고 단건 조회 (org-scope + soft-delete 제외). 부재 시 404."""
        warning = await warning_repository.get_active(db, warning_id, organization_id)
        if warning is None:
            raise NotFoundError("Warning not found")
        return warning

    async def get_counts(
        self,
        db: AsyncSession,
        organization_id: UUID,
        *,
        store_ids: list[UUID] | None = None,
    ) -> list[dict]:
        """직원별 (total, active) 경고 갯수 — Staff 목록 칼럼용."""
        counts = await warning_repository.counts_by_subject(
            db, organization_id, store_ids=store_ids
        )
        return [
            {"user_id": str(uid), "total": total, "active": active}
            for uid, (total, active) in counts.items()
        ]

    # ====================================================================
    # 생성 / 수정 / 삭제
    # ====================================================================

    async def _load_subject(
        self, db: AsyncSession, subject_id: UUID, organization_id: UUID
    ) -> User:
        """org-scope 로 대상 직원 로드 (role eager). 부재 시 404."""
        result = await db.execute(
            select(User)
            .options(selectinload(User.role))
            .where(User.id == subject_id, User.organization_id == organization_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise NotFoundError("Subject employee not found")
        return user

    async def _validate_subject_store(
        self,
        db: AsyncSession,
        subject_id: UUID,
        store_id: UUID,
        organization_id: UUID,
    ) -> None:
        """store 가 caller org 에 속하고, 대상 직원이 그 매장에 배정됐는지 검증.

        org 불일치/부재 → 404 (cross-org 누설 방지). 배정 안 됨 → 400.
        Owner 는 check_store_access 가 no-op 이므로 org 격리는 여기서 강제.
        """
        store = await db.get(Store, store_id)
        if store is None or store.organization_id != organization_id:
            raise NotFoundError("Store not found")
        result = await db.execute(
            select(UserStore.user_id).where(
                UserStore.user_id == subject_id, UserStore.store_id == store_id
            )
        )
        if result.scalar_one_or_none() is None:
            raise BadRequestError("Store is not assigned to this employee")

    def _assert_can_modify(self, current_user: User, warning: Warning) -> None:
        """수정/철회 권한 — Owner 는 전체, 그 외(GM)는 본인 발행건만. 아니면 403.

        (삭제는 별도 — Owner 전용. delete_warning 참조.)
        """
        if not is_owner(current_user) and warning.issued_by_id != current_user.id:
            raise HTTPException(
                status_code=403,
                detail="You can only modify warnings you issued",
            )

    async def create_warning(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        issuer: User,
        data,
    ) -> Warning:
        """새 경고 발행. 방향 검증(상위→하위) + subject-store 검증 + seq 발급.

        매장 접근 검증(GM 관리매장)은 라우터에서 선행. 여기서는 org 격리 +
        subject 가 그 매장 배정인지 + 방향 검증. seq 충돌 시 재시도.
        """
        subject_id = UUID(data.subject_user_id)
        store_id = UUID(data.store_id)

        # 방향 검증 — 발행자보다 엄격히 낮은 권한만.
        subject = await self._load_subject(db, subject_id, organization_id)
        if not can_warn(issuer, subject):
            raise HTTPException(
                status_code=403,
                detail="You can only warn users with lower authority",
            )

        # store org 격리 + subject 배정 매장 검증.
        await self._validate_subject_store(db, subject_id, store_id, organization_id)

        # seq 발급 + insert (UNIQUE(org, seq) 충돌 시 재시도).
        last_exc: Exception | None = None
        for _ in range(5):
            seq = await warning_repository.next_seq(db, organization_id)
            warning = Warning(
                organization_id=organization_id,
                seq=seq,
                issued_by_id=issuer.id,
                subject_user_id=subject_id,
                store_id=store_id,
                title=data.title,
                categories=list(data.categories),
                details=data.details,
                corrective_action=data.corrective_action,
                status="active",
                warning_date=data.warning_date,
            )
            db.add(warning)
            try:
                await db.flush()
                await db.refresh(warning)
                await db.commit()
                return warning
            except IntegrityError as exc:
                await db.rollback()
                last_exc = exc
        # 5회 모두 실패 (희박) — 원래 예외 재발생.
        assert last_exc is not None
        raise last_exc

    async def update_warning(
        self,
        db: AsyncSession,
        *,
        warning_id: UUID,
        organization_id: UUID,
        current_user: User,
        data,
        check_store_access,
    ) -> Warning:
        """경고 수정 (partial). 소유권 검증 후 제공된 필드만 반영.

        store 변경 시 org 격리 + 접근(check_store_access) + subject 배정 재검증.
        status 'resolved'↔'active' 토글 시 resolved_at stamp/clear.
        subject(대상 직원)는 변경 불가.
        check_store_access: 라우터가 주입하는 async (store_id) → None | raise 403.
        """
        warning = await self.get_warning(db, warning_id, organization_id)
        self._assert_can_modify(current_user, warning)
        fields = data.model_dump(exclude_unset=True)

        try:
            if "store_id" in fields and fields["store_id"] is not None:
                new_store_id = UUID(fields["store_id"])
                await check_store_access(new_store_id)
                # 대상 직원이 새 매장에 배정됐는지 + org 격리.
                if warning.subject_user_id is not None:
                    await self._validate_subject_store(
                        db, warning.subject_user_id, new_store_id, organization_id
                    )
                warning.store_id = new_store_id

            if "title" in fields and fields["title"] is not None:
                warning.title = fields["title"]
            if "categories" in fields and fields["categories"] is not None:
                warning.categories = list(fields["categories"])
            if "details" in fields:
                warning.details = fields["details"]
            if "corrective_action" in fields:
                warning.corrective_action = fields["corrective_action"]
            if "warning_date" in fields and fields["warning_date"] is not None:
                warning.warning_date = fields["warning_date"]

            if "status" in fields and fields["status"] is not None:
                new_status = fields["status"]
                if new_status == "withdrawn" and warning.status != "withdrawn":
                    warning.withdrawn_at = datetime.now(timezone.utc)
                elif new_status == "active":
                    warning.withdrawn_at = None
                warning.status = new_status

            warning.updated_at = datetime.now(timezone.utc)
            await db.flush()
            await db.refresh(warning)
            await db.commit()
            return warning
        except Exception:
            await db.rollback()
            raise

    async def delete_warning(
        self,
        db: AsyncSession,
        *,
        warning_id: UUID,
        organization_id: UUID,
        current_user: User,
    ) -> None:
        """소프트 삭제 — Owner 전용. 이미 삭제/부재면 404 (idempotent-safe).

        삭제는 기록을 감사에서까지 지우므로 Owner 만 가능하다. GM 은 잘못 발행한
        경고를 '철회(withdrawn)'만 할 수 있고(기록은 남음), 삭제는 못 한다.
        """
        warning = await self.get_warning(db, warning_id, organization_id)
        if not is_owner(current_user):
            raise HTTPException(
                status_code=403,
                detail="Only an Owner can delete warnings",
            )
        try:
            warning.deleted_at = datetime.now(timezone.utc)
            await db.flush()
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    # ====================================================================
    # 응답 빌드
    # ====================================================================

    async def build_warning_response(
        self, db: AsyncSession, warning: Warning, *, include_ordinal: bool = False
    ) -> dict:
        """Warning → WarningResponse dict (joined names + ref_no).

        include_ordinal=True 면 그 직원의 경고 순번(First/Second/Other 표시용)을
        계산해 담는다. 목록에선 N+1 을 피하려 생략(None), 상세에서만 True.
        """
        subject_name: str | None = None
        employee_no: str | None = None
        if warning.subject_user_id:
            subject = await db.get(User, warning.subject_user_id)
            if subject:
                subject_name = subject.full_name
                employee_no = subject.employee_no

        ordinal: int | None = None
        if include_ordinal and warning.subject_user_id:
            ordinal = await warning_repository.subject_warning_ordinal(
                db, warning.organization_id, warning.subject_user_id, warning.created_at
            )

        issued_by_name: str | None = None
        if warning.issued_by_id:
            issuer = await db.get(User, warning.issued_by_id)
            if issuer:
                issued_by_name = issuer.full_name

        store_name: str | None = None
        if warning.store_id:
            store = await db.get(Store, warning.store_id)
            if store:
                store_name = store.name

        return {
            "id": str(warning.id),
            "ref_no": _ref_no(warning.seq),
            "status": warning.status,
            "subject_user_id": str(warning.subject_user_id) if warning.subject_user_id else None,
            "subject_name": subject_name,
            "employee_no": employee_no,
            "issued_by_id": str(warning.issued_by_id) if warning.issued_by_id else None,
            "issued_by_name": issued_by_name,
            "store_id": str(warning.store_id) if warning.store_id else None,
            "store_name": store_name,
            "title": warning.title,
            "categories": list(warning.categories or []),
            "details": warning.details,
            "corrective_action": warning.corrective_action,
            "warning_date": warning.warning_date,
            "ordinal": ordinal,
            "withdrawn_at": warning.withdrawn_at,
            "created_at": warning.created_at,
            "updated_at": warning.updated_at,
        }


    # ====================================================================
    # PDF (EMPLOYEE WARNING NOTICE FORM)
    # ====================================================================

    async def build_pdf(
        self, db: AsyncSession, warning: Warning, organization_id: UUID
    ) -> tuple[bytes, str]:
        """경고 → 종이 양식 PDF bytes + 파일명. 이름/회사/순번 resolve.

        First/Second/Other 는 그 직원의 경고 순번으로 서버가 자동 결정한다.
        Deadline/Follow-up/서명은 양식에 빈 줄로만 둔다(우리가 안 받는 칸).
        """
        from app.utils.warning_pdf import build_warning_notice_pdf

        org = await db.get(Organization, organization_id)
        subject = (
            await db.get(User, warning.subject_user_id)
            if warning.subject_user_id
            else None
        )
        manager = (
            await db.get(User, warning.issued_by_id) if warning.issued_by_id else None
        )

        ordinal = 1
        if warning.subject_user_id:
            ordinal = await warning_repository.subject_warning_ordinal(
                db, organization_id, warning.subject_user_id, warning.created_at
            )

        pdf_bytes = build_warning_notice_pdf(
            company_name=org.name if org else "",
            ref_no=_ref_no(warning.seq),
            employee_name=subject.full_name if subject else "",
            manager_name=manager.full_name if manager else "",
            warning_date=warning.warning_date.isoformat(),
            ordinal=ordinal,
            categories=list(warning.categories or []),
            details=warning.details or "",
            corrective_action=warning.corrective_action or "",
        )
        filename = f"Warning_{_ref_no(warning.seq)}.pdf"
        return pdf_bytes, filename


# 싱글턴 인스턴스
warning_service: WarningService = WarningService()
