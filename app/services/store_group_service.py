"""매장 그룹 서비스 — store_groups CRUD 비즈니스 로직.

Store Group Service — Business logic for store group CRUD.
그룹은 empid 채번 정책의 단위: numbering_mode="group" 이면 그룹 내 매장들이
시퀀스를 공유한다. 모드/편성 변경 시 기존 empid 중복은 경고만 하고 막지 않는다
(정책 A — 자동 재번호 금지, 해소는 EMPID 임포트에서).
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import NUMBERING_MODE_GROUP, Store, StoreGroup
from app.repositories.store_group_repository import store_group_repository
from app.schemas.organization import (
    NumberingRecalculateRequest,
    NumberingRecalculateResponse,
    NumberingUpdateRequest,
    NumberingUpdateResponse,
    AssignPreviewConflict,
    AssignPreviewHolder,
    AssignPreviewMember,
    AssignPreviewPersonSplit,
    AssignPreviewSplitStore,
    GroupAssignPreviewResponse,
    StoreGroupCreate,
    StoreGroupResponse,
    StoreGroupUpdate,
)
from app.services import empid_cursor_service
from app.services.org_numbering import duplicate_empids_in_scope
from app.core.error_codes.common import GROUP_NOT_FOUND, STORE_NOT_FOUND
from app.utils.exceptions import DuplicateError, NotFoundError


class StoreGroupService:
    """매장 그룹 비즈니스 로직을 처리하는 서비스."""

    @staticmethod
    def _to_response(
        group: StoreGroup,
        store_count: int = 0,
        duplicate_empids: list[dict[str, int]] | None = None,
    ) -> StoreGroupResponse:
        """StoreGroup 모델 → 응답 스키마 변환."""
        return StoreGroupResponse(
            id=str(group.id),
            organization_id=str(group.organization_id),
            name=group.name,
            code=group.code,
            sort_order=group.sort_order,
            numbering_mode=group.numbering_mode,
            number_range_start=group.number_range_start,
            payroll_corp_name=group.payroll_corp_name,
            store_count=store_count,
            duplicate_empids=duplicate_empids or [],
            created_at=group.created_at,
        )

    async def _group_store_ids(self, db: AsyncSession, group_id: UUID) -> list[UUID]:
        """그룹 소속 전체 매장 id (폐점 포함 — 번호 점유 유지)."""
        return list(
            (await db.execute(select(Store.id).where(Store.group_id == group_id)))
            .scalars()
            .all()
        )

    async def list_groups(
        self,
        db: AsyncSession,
        organization_id: UUID,
    ) -> list[StoreGroupResponse]:
        """조직의 그룹 목록 — sort_order 순, 소속 매장 수 + 공유 스코프 중복 경고 포함.

        duplicate_empids 를 목록에서도 계산해야 Manage Groups 재오픈 시
        기존 중복 경고 배너가 유지된다 (Save 응답에만 의존하면 재오픈 시 사라짐).
        """
        groups = await store_group_repository.get_by_org(db, organization_id)
        counts = await store_group_repository.store_counts(db, organization_id)
        out: list[StoreGroupResponse] = []
        for g in groups:
            duplicates: list[dict[str, int]] = []
            if g.numbering_mode == NUMBERING_MODE_GROUP:
                scope = await self._group_store_ids(db, g.id)
                duplicates = await duplicate_empids_in_scope(db, scope)
            out.append(self._to_response(g, counts.get(g.id, 0), duplicates))
        # 채번 커서 현황 (§3-1) — Groups 패널이 "다음 발급 번호"와 재계산 버튼을 그린다.
        numbering = await empid_cursor_service.numbering_for_groups(
            db, [g.id for g in groups]
        )
        for g, response in zip(groups, out):
            response.numbering = numbering.get(g.id)
        return out

    async def create_group(
        self,
        db: AsyncSession,
        organization_id: UUID,
        data: StoreGroupCreate,
    ) -> StoreGroupResponse:
        """새 그룹을 생성합니다. org 내 이름 중복은 거절."""
        exists = await store_group_repository.exists(
            db, {"organization_id": organization_id, "name": data.name}
        )
        if exists:
            raise DuplicateError("A store group with this name already exists")
        next_sort = await store_group_repository.get_max_sort_order(db, organization_id) + 1
        try:
            group = await store_group_repository.create(
                db,
                {
                    "organization_id": organization_id,
                    "name": data.name,
                    "code": (data.code.strip() or None) if data.code else None,
                    "numbering_mode": data.numbering_mode,
                    "number_range_start": data.number_range_start,
                    "payroll_corp_name": data.payroll_corp_name,
                    "sort_order": next_sort,
                    # 채번 커서 초기화 — 신규 그룹도 NULL 로 두지 않는다(O1: NULL 폴백을
                    # 남기면 MAX 경로가 코드에 되살아난다).
                    "next_empid": empid_cursor_service.initial_cursor(
                        data.number_range_start
                    ),
                },
            )
            await db.commit()
            response = self._to_response(group)
            response.numbering = await empid_cursor_service.numbering_for_group(
                db, group.id
            )
            return response
        except Exception:
            await db.rollback()
            raise

    async def update_group(
        self,
        db: AsyncSession,
        group_id: UUID,
        organization_id: UUID,
        data: StoreGroupUpdate,
    ) -> StoreGroupResponse:
        """그룹을 수정합니다. 공유 모드 전환 시 스코프 내 기존 empid 중복을 경고로 반환."""
        fields = data.model_dump(exclude_unset=True)
        # NOT NULL 컬럼(name/numbering_mode)에 명시적 null 이 오면 no-op 처리 (500 방지).
        # number_range_start 는 nullable — 명시적 null 로 번호대 해제 허용.
        # nullable 컬럼은 명시적 null 로 해제할 수 있어야 한다 (번호대 해제, 급여 표시명 제거).
        nullable = {"number_range_start", "code", "payroll_corp_name"}
        fields = {k: v for k, v in fields.items() if v is not None or k in nullable}
        if isinstance(fields.get("code"), str):
            fields["code"] = fields["code"].strip() or None
        if "name" in fields and fields["name"] is not None:
            current = await store_group_repository.get_by_id(db, group_id, organization_id)
            if current is None:
                raise NotFoundError("Store group not found")
            if current.name != fields["name"]:
                exists = await store_group_repository.exists(
                    db, {"organization_id": organization_id, "name": fields["name"]}
                )
                if exists:
                    raise DuplicateError("A store group with this name already exists")
        try:
            group = await store_group_repository.update(
                db, group_id, fields, organization_id
            )
            if group is None:
                raise NotFoundError("Store group not found")
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        duplicates: list[dict[str, int]] = []
        if group.numbering_mode == NUMBERING_MODE_GROUP:
            scope = await self._group_store_ids(db, group.id)
            duplicates = await duplicate_empids_in_scope(db, scope)
        counts = await store_group_repository.store_counts(db, organization_id)
        response = self._to_response(group, counts.get(group.id, 0), duplicates)
        response.numbering = await empid_cursor_service.numbering_for_group(
            db, group.id
        )
        return response

    async def delete_group(
        self,
        db: AsyncSession,
        group_id: UUID,
        organization_id: UUID,
    ) -> None:
        """그룹을 삭제합니다. 소속 매장은 FK SET NULL 로 미그룹 상태가 된다 (번호 불변)."""
        try:
            deleted = await store_group_repository.delete(db, group_id, organization_id)
            if not deleted:
                raise NotFoundError("Store group not found")
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def _assert_group(
        self, db: AsyncSession, group_id: UUID, organization_id: UUID
    ) -> None:
        """org 소속 그룹인지 검증 — 타 org 그룹은 404 (존재 누설 방지)."""
        group = await store_group_repository.get_by_id(db, group_id, organization_id)
        if group is None:
            raise GROUP_NOT_FOUND()

    async def update_numbering(
        self,
        db: AsyncSession,
        group_id: UUID,
        organization_id: UUID,
        data: NumberingUpdateRequest,
        actor_id: UUID | None,
    ) -> NumberingUpdateResponse:
        """그룹 커서 수동 조정 (§3-2). 사유 필수, 낮추는 것도 허용(lowered=true)."""
        await self._assert_group(db, group_id, organization_id)
        info, previous, lowered = await empid_cursor_service.set_cursor(
            db,
            scope=empid_cursor_service.SCOPE_GROUP,
            scope_id=group_id,
            next_empid=data.next_empid,
            reason=data.reason,
            actor_id=actor_id,
        )
        return NumberingUpdateResponse(
            **info.model_dump(), previous=previous, lowered=lowered
        )

    async def recalculate_numbering(
        self,
        db: AsyncSession,
        group_id: UUID,
        organization_id: UUID,
        data: NumberingRecalculateRequest,
        actor_id: UUID | None,
    ) -> NumberingRecalculateResponse:
        """그룹 커서 재계산 (§3-3). apply=false 면 미리보기, true 면 사유 필수.

        재계산은 `empid_kind='sequence'` 만 본다 — 예외 번호(본사 이관 등)는 제외되고
        그 건수가 exception_count 로 함께 나간다 (RULE-C).
        """
        await self._assert_group(db, group_id, organization_id)
        info, applied, previous = await empid_cursor_service.recalculate_cursor(
            db,
            scope=empid_cursor_service.SCOPE_GROUP,
            scope_id=group_id,
            apply=data.apply,
            reason=data.reason,
            actor_id=actor_id,
        )
        return NumberingRecalculateResponse(
            **info.model_dump(), applied=applied, previous=previous
        )

    async def reorder_groups(
        self,
        db: AsyncSession,
        organization_id: UUID,
        ordered_ids: list[UUID],
    ) -> int:
        """그룹 표시 순서를 일괄 변경합니다."""
        try:
            updated = await store_group_repository.reorder(db, organization_id, ordered_ids)
            await db.commit()
            return updated
        except Exception:
            await db.rollback()
            raise

    async def assign_preview(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID,
        group_id: UUID | None,
    ) -> GroupAssignPreviewResponse:
        """편입 미리보기 — 매장을 group_id 에 넣으면 생길 EMPID 충돌을 조회만 한다.

        읽기 전용: 아무것도 변경/커밋하지 않는다. 편입 자체는 empid 를 절대 건드리지
        않으므로(정책 A) 여기서 미리 경고만 하고, 해소는 EMPID Bulk Edit 에서 한다.

        - group_id null(이탈) 또는 mode="store"(독립 채번) → 충돌 개념 없음, 빈 배열.
        - mode="group" → 그룹 내 다른 매장들과 비교. 휴면(is_work_assignment=false)·
          폐점 매장 행도 포함 — 번호 점유 유지 정책(duplicate_empids_in_scope)과 동일 기준.
        - conflicts: 편입 멤버의 번호를 그룹 내 **다른 사람**이 이미 사용.
          같은 사람이 같은 번호를 갖는 건 정상이라 제외.
        - person_splits: 같은 사람이 편입 매장과 그룹 내 다른 매장에서 **다른 번호**.
        """
        from app.models.org_member import OrgMember, OrgMemberStore
        from app.models.user import User

        # store 의 org 소속 검증 — 타 org 매장은 404 (존재 누설 방지, _validate_group_org 미러)
        store_org = await db.scalar(
            select(Store.organization_id).where(Store.id == store_id)
        )
        if store_org is None or store_org != organization_id:
            raise STORE_NOT_FOUND()

        numbering_mode: str | None = None
        if group_id is not None:
            row = (
                await db.execute(
                    select(StoreGroup.organization_id, StoreGroup.numbering_mode).where(
                        StoreGroup.id == group_id
                    )
                )
            ).first()
            if row is None or row.organization_id != organization_id:
                raise GROUP_NOT_FOUND()
            numbering_mode = row.numbering_mode

        # 편입 매장의 empid 보유 멤버 — 휴면 포함 (번호 점유 유지 정책 미러)
        incoming_rows = (
            await db.execute(
                select(OrgMemberStore.empid, OrgMember.user_id, User.full_name)
                .join(OrgMember, OrgMember.id == OrgMemberStore.org_member_id)
                .join(User, User.id == OrgMember.user_id)
                .where(
                    OrgMemberStore.store_id == store_id,
                    OrgMemberStore.empid.isnot(None),
                )
                .order_by(OrgMemberStore.empid)
            )
        ).all()

        response = GroupAssignPreviewResponse(
            numbering_mode=numbering_mode,
            incoming_with_empid=len(incoming_rows),
        )
        # 이탈(null) / 독립 채번(store) — 스코프 공유가 없으므로 충돌 개념 자체가 없다
        if group_id is None or numbering_mode != NUMBERING_MODE_GROUP:
            return response

        # 그룹 내 다른 매장 (편입 매장 제외, 폐점 포함 — 번호 점유 유지)
        other_store_ids = list(
            (
                await db.execute(
                    select(Store.id).where(
                        Store.group_id == group_id, Store.id != store_id
                    )
                )
            )
            .scalars()
            .all()
        )
        if not other_store_ids:
            return response

        # 그룹 내 다른 매장들의 empid 보유 행 — 휴면 포함
        holder_rows = (
            await db.execute(
                select(
                    OrgMemberStore.empid,
                    OrgMemberStore.store_id,
                    OrgMember.user_id,
                    User.full_name,
                    Store.name.label("store_name"),
                )
                .join(OrgMember, OrgMember.id == OrgMemberStore.org_member_id)
                .join(User, User.id == OrgMember.user_id)
                .join(Store, Store.id == OrgMemberStore.store_id)
                .where(
                    OrgMemberStore.store_id.in_(other_store_ids),
                    OrgMemberStore.empid.isnot(None),
                )
                .order_by(OrgMemberStore.empid, Store.name)
            )
        ).all()

        for inc in incoming_rows:
            # 충돌: 같은 번호를 그룹 내 **다른 사람**이 보유 (같은 사람 같은 번호 = 정상)
            holders = [
                AssignPreviewHolder(
                    user_id=str(h.user_id),
                    name=h.full_name,
                    store_id=str(h.store_id),
                    store_name=h.store_name,
                )
                for h in holder_rows
                if h.empid == inc.empid and h.user_id != inc.user_id
            ]
            if holders:
                response.conflicts.append(
                    AssignPreviewConflict(
                        empid=inc.empid,
                        incoming=AssignPreviewMember(
                            user_id=str(inc.user_id), name=inc.full_name
                        ),
                        holders=holders,
                    )
                )
            # 인물 분열: 같은 사람이 다른 매장에서 **다른 번호** (같은 번호는 제외)
            elsewhere = [
                AssignPreviewSplitStore(
                    store_id=str(h.store_id),
                    store_name=h.store_name,
                    empid=h.empid,
                )
                for h in holder_rows
                if h.user_id == inc.user_id and h.empid != inc.empid
            ]
            if elsewhere:
                response.person_splits.append(
                    AssignPreviewPersonSplit(
                        user_id=str(inc.user_id),
                        name=inc.full_name,
                        incoming_empid=inc.empid,
                        elsewhere=elsewhere,
                    )
                )
        return response


# 싱글턴 인스턴스 — Singleton instance
store_group_service: StoreGroupService = StoreGroupService()
