"""연락처(Contacts) 서비스 — 조직 전화번호부 v1 비즈니스 로직.

계약: `docs/99_inbox/2026-08-14-연락처-API계약.md`
설계: `docs/99_inbox/2026-08-14-연락처(Contacts)-기능-설계.md` (D1~D9)

핵심 규칙
    - org-scope: 모든 쿼리는 organization_id 로 격리. 타 org 는 404(존재를 숨긴다).
    - 가시성(D1): `_visibility_clause` **단일 헬퍼**를 목록/상세/태그/신청이 모두 통과한다.
      IDOR 은 쿼리 레벨에서 막는다 — 파이썬 후처리 필터에 의존하지 않는다.
    - soft delete: 읽기는 항상 `deleted_at IS NULL`.
    - 이력(D9): 상태를 바꾸는 모든 경로가 `contact_audit_service.record()` 를 거친다.
    - 중복 번호(N7): 차단하지 않고 응답에 경고를 실어 보낸다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import Select, ColumnElement, and_, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes.contacts import (
    CONTACT_DELETED,
    CONTACT_NOT_FOUND,
    CONTACT_NOT_YOUR_REQUEST,
    CONTACT_REASON_REQUIRED,
    CONTACT_REQUEST_NOT_FOUND,
    CONTACT_REQUEST_NOT_PENDING,
    CONTACT_STORE_FORBIDDEN,
    CONTACT_VALIDATION_ERROR,
)
from app.core.permissions import is_gm_plus
from app.models.contact import (
    CONTACT_REQUEST_TYPES,
    Contact,
    ContactChangeRequest,
    ContactPhone,
    ContactTag,
    ContactTagLink,
)
from app.models.organization import Store
from app.models.user import User
from app.schemas.contact import (
    MAX_SEARCH_LENGTH,
    ContactApproveResponse,
    ContactChangeRequestCreate,
    ContactChangeRequestResponse,
    ContactDuplicatePhone,
    ContactPayload,
    ContactPhoneInput,
    ContactPhoneResponse,
    ContactRequestApprove,
    ContactResponse,
    ContactTagRef,
    ContactTagResponse,
    ContactUpdate,
)
from app.services.contact_audit_service import (
    contact_audit_service,
    contact_snapshot,
    diff_snapshots,
)
from app.utils.pagination import paginate
from app.utils.phone import normalize_phone

# request_type → 그 신청을 처리(승인/반려)하는 데 필요한 쓰기 권한 (계약 §1).
REQUEST_TYPE_PERMISSION: dict[str, str] = {
    "create": "contacts:create",
    "update": "contacts:update",
    "delete": "contacts:delete",
}

# 정렬 키 (계약 §4.2). tie-breaker id 를 반드시 붙인다 — 동명이인 페이지 중복/누락 방지.
_SORTS = ("name", "name_desc", "created_at", "updated_at")

# 검색어에서 뽑은 숫자로 "정규화 번호 부분일치"를 태울 최소 자릿수.
# 1~2 자리면 메모/이름에 섞인 숫자("after 9pm")가 그 숫자를 포함한 **모든** 번호를 끌고 온다.
# 원본 표기(number) ilike 절은 그대로 살아 있으므로 짧은 숫자도 리터럴로는 계속 검색된다.
_MIN_PHONE_SEARCH_DIGITS = 3


def _escape_like(value: str) -> str:
    """LIKE/ILIKE 와일드카드 이스케이프 — 사용자가 친 `%` 가 와일드카드가 되지 않게."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_uuid(value: str, field: str) -> UUID:
    """문자열 → UUID. 형식이 틀리면 400 (500 으로 새지 않게)."""
    try:
        return UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise CONTACT_VALIDATION_ERROR(
            message=f"{field} is not a valid identifier.", field=field
        )


class ContactService:
    """연락처 CRUD + 검색 + 태그 + 변경 신청."""

    # ====================================================================
    # 가시성 (D1) — 모든 읽기 경로가 통과하는 단일 헬퍼
    # ====================================================================

    def _visibility_clause(
        self, user: User, accessible: list[UUID] | None
    ) -> ColumnElement[bool] | None:
        """store 가시 조건. None = 조건 없음(전 매장).

        Owner/Super Owner: accessible is None → 전부.
        **GM: 전 매장 예외 (D1 표)** — `get_accessible_store_ids` 는 GM 에게 "관리 매장"만
        주지만, 연락처 가시성에서 GM 은 전 매장이므로 store 조건 자체를 붙이지 않는다.
        warnings 와 다른 지점이니 복붙하지 말 것.
        SV/기타 read 보유자: store_id IS NULL(전체 공유) 또는 배정 매장.
        """
        if accessible is None or is_gm_plus(user):
            return None
        if not accessible:
            # 배정 매장이 없으면 전체 공유 연락처만 보인다.
            return Contact.store_id.is_(None)
        return or_(Contact.store_id.is_(None), Contact.store_id.in_(accessible))

    def _base_select(
        self, user: User, accessible: list[UUID] | None
    ) -> Select[tuple[Contact]]:
        """org + soft-delete + 가시성이 걸린 기본 SELECT."""
        stmt = select(Contact).where(
            Contact.organization_id == user.organization_id,
            Contact.deleted_at.is_(None),
        )
        clause = self._visibility_clause(user, accessible)
        if clause is not None:
            stmt = stmt.where(clause)
        return stmt

    def _visible_contact_ids_subquery(self, user: User, accessible: list[UUID] | None):
        """가시 연락처 id 서브쿼리 — 태그 usage_count / 신청 가시성에서 재사용."""
        stmt = select(Contact.id).where(
            Contact.organization_id == user.organization_id,
            Contact.deleted_at.is_(None),
        )
        clause = self._visibility_clause(user, accessible)
        if clause is not None:
            stmt = stmt.where(clause)
        return stmt.scalar_subquery()

    async def _guard_store(self, db: AsyncSession, user: User, store_id: UUID) -> None:
        """쓰기 대상 store 접근 검증 — 타 org 는 404, 접근 불가는 403(도메인 코드)."""
        from app.api.deps import check_store_access

        try:
            await check_store_access(db, user, store_id)
        except HTTPException as exc:
            if exc.status_code == 403:
                raise CONTACT_STORE_FORBIDDEN(store_id=str(store_id))
            raise

    # ====================================================================
    # 응답 조립 (bulk 로딩 — relationship 을 선언하지 않았으므로 명시 쿼리)
    # ====================================================================

    async def _build_responses(
        self,
        db: AsyncSession,
        contacts: Sequence[Contact],
        *,
        include_pending: bool = False,
    ) -> list[ContactResponse]:
        """Contact 행들 → ContactResponse 목록. 자식/조인은 전부 in-bulk 로 한 번씩."""
        if not contacts:
            return []
        ids = [c.id for c in contacts]

        # 번호 — sort_order 순
        phone_rows = (
            await db.execute(
                select(ContactPhone)
                .where(ContactPhone.contact_id.in_(ids))
                .order_by(ContactPhone.contact_id, ContactPhone.sort_order)
            )
        ).scalars().all()
        phones_by_contact: dict[UUID, list[ContactPhoneResponse]] = {}
        for p in phone_rows:
            phones_by_contact.setdefault(p.contact_id, []).append(
                ContactPhoneResponse(
                    id=str(p.id),
                    label=p.label,
                    number=p.number,
                    number_normalized=p.number_normalized,
                    is_primary=p.is_primary,
                    sort_order=p.sort_order,
                )
            )

        # 태그 — 표시명 기준 정렬
        tag_rows = (
            await db.execute(
                select(ContactTagLink.contact_id, ContactTag)
                .join(ContactTag, ContactTag.id == ContactTagLink.tag_id)
                .where(ContactTagLink.contact_id.in_(ids))
                .order_by(ContactTag.key)
            )
        ).all()
        tags_by_contact: dict[UUID, list[ContactTagRef]] = {}
        for contact_id, tag in tag_rows:
            tags_by_contact.setdefault(contact_id, []).append(
                ContactTagRef(id=str(tag.id), name=tag.name, key=tag.key)
            )

        # 매장명 / 작성자명 스냅샷 조인
        store_ids = {c.store_id for c in contacts if c.store_id is not None}
        store_names: dict[UUID, str] = {}
        if store_ids:
            rows = (
                await db.execute(
                    select(Store.id, Store.name).where(Store.id.in_(store_ids))
                )
            ).all()
            store_names = {sid: name for sid, name in rows}

        user_ids = {c.created_by for c in contacts if c.created_by is not None}
        user_names: dict[UUID, str] = {}
        if user_ids:
            rows = (
                await db.execute(
                    select(User.id, User.full_name).where(User.id.in_(user_ids))
                )
            ).all()
            user_names = {uid: name for uid, name in rows}

        # 대기 중 신청 수 — 상세에서만 (목록은 N+1/무의미한 집계 회피로 0 고정)
        pending_counts: dict[UUID, int] = {}
        if include_pending:
            rows = (
                await db.execute(
                    select(
                        ContactChangeRequest.contact_id, func.count(ContactChangeRequest.id)
                    )
                    .where(
                        ContactChangeRequest.contact_id.in_(ids),
                        ContactChangeRequest.status == "pending",
                    )
                    .group_by(ContactChangeRequest.contact_id)
                )
            ).all()
            pending_counts = {cid: cnt for cid, cnt in rows}

        return [
            ContactResponse(
                id=str(c.id),
                name=c.name,
                company=c.company,
                email=c.email,
                memo=c.memo,
                store_id=str(c.store_id) if c.store_id else None,
                store_name=store_names.get(c.store_id) if c.store_id else None,
                phones=phones_by_contact.get(c.id, []),
                tags=tags_by_contact.get(c.id, []),
                created_by=str(c.created_by) if c.created_by else None,
                created_by_name=user_names.get(c.created_by) if c.created_by else None,
                created_at=c.created_at,
                updated_at=c.updated_at,
                pending_request_count=pending_counts.get(c.id, 0),
            )
            for c in contacts
        ]

    async def _build_response(
        self, db: AsyncSession, contact: Contact, *, include_pending: bool = False
    ) -> ContactResponse:
        items = await self._build_responses(db, [contact], include_pending=include_pending)
        return items[0]

    def _snapshot(self, resp: ContactResponse) -> dict[str, Any]:
        """ContactResponse → 이력 스냅샷 (계약 §7.2)."""
        return contact_snapshot(
            name=resp.name,
            company=resp.company,
            email=resp.email,
            memo=resp.memo,
            store_id=resp.store_id,
            store_name=resp.store_name,
            phones=[
                {"label": p.label, "number": p.number, "is_primary": p.is_primary}
                for p in resp.phones
            ],
            tags=[t.name for t in resp.tags],
        )

    # ====================================================================
    # 목록 / 검색
    # ====================================================================

    async def list_contacts(
        self,
        db: AsyncSession,
        user: User,
        accessible: list[UUID] | None,
        *,
        q: str | None = None,
        tag: str | None = None,
        store_id: str | None = None,
        sort: str = "name",
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[list[ContactResponse], int]:
        """통합 검색 목록 (계약 §4.2).

        q 는 name/company/email/memo/tag.name/phone(원본·정규화) 를 OR 부분일치한다.
        store_id 는 가시 조건을 **좁히기만** 한다: UUID | 'none'(전체 공유만) | 미지정.
        """
        page = max(1, page)
        per_page = max(1, min(per_page, 100))
        stmt = self._base_select(user, accessible)

        # --- store 필터 ---
        if store_id:
            if store_id == "none":
                stmt = stmt.where(Contact.store_id.is_(None))
            else:
                store_uuid = _parse_uuid(store_id, "store_id")
                # 타 org → 404, 접근 불가 → 403 (드롭다운에는 접근 가능한 매장만 담긴다)
                await self._guard_store(db, user, store_uuid)
                stmt = stmt.where(Contact.store_id == store_uuid)

        # --- 태그 필터 (정규화 키 비교) ---
        if tag and tag.strip():
            tag_key = tag.strip().lower()
            stmt = stmt.where(
                exists(
                    select(ContactTagLink.id)
                    .join(ContactTag, ContactTag.id == ContactTagLink.tag_id)
                    .where(
                        ContactTagLink.contact_id == Contact.id,
                        ContactTag.key == tag_key,
                    )
                )
            )

        # --- 통합 검색어 ---
        if q and q.strip():
            term = q.strip()[:MAX_SEARCH_LENGTH]
            pattern = f"%{_escape_like(term)}%"
            phone_conditions: list[ColumnElement[bool]] = [
                ContactPhone.number.ilike(pattern, escape="\\")
            ]
            digits = normalize_phone(term)
            if digits and len(digits) >= _MIN_PHONE_SEARCH_DIGITS:
                # 숫자가 섞였으면 정규화 매칭도 함께 태운다 ('213-555' → '213555')
                phone_conditions.append(
                    ContactPhone.number_normalized.like(
                        f"%{_escape_like(digits)}%", escape="\\"
                    )
                )
            stmt = stmt.where(
                or_(
                    Contact.name.ilike(pattern, escape="\\"),
                    Contact.company.ilike(pattern, escape="\\"),
                    Contact.email.ilike(pattern, escape="\\"),
                    Contact.memo.ilike(pattern, escape="\\"),
                    exists(
                        select(ContactTagLink.id)
                        .join(ContactTag, ContactTag.id == ContactTagLink.tag_id)
                        .where(
                            ContactTagLink.contact_id == Contact.id,
                            ContactTag.name.ilike(pattern, escape="\\"),
                        )
                    ),
                    exists(
                        select(ContactPhone.id).where(
                            ContactPhone.contact_id == Contact.id,
                            or_(*phone_conditions),
                        )
                    ),
                )
            )

        # --- 정렬 (기본 이름순, tie-breaker id) ---
        if sort not in _SORTS:
            sort = "name"
        if sort == "name":
            stmt = stmt.order_by(func.lower(Contact.name).asc(), Contact.id.asc())
        elif sort == "name_desc":
            stmt = stmt.order_by(func.lower(Contact.name).desc(), Contact.id.asc())
        elif sort == "created_at":
            stmt = stmt.order_by(Contact.created_at.desc(), Contact.id.asc())
        else:
            stmt = stmt.order_by(Contact.updated_at.desc(), Contact.id.asc())

        rows, total = await paginate(db, stmt, page=page, per_page=per_page)
        return await self._build_responses(db, rows), total

    # ====================================================================
    # 단건 조회
    # ====================================================================

    async def _load_visible(
        self,
        db: AsyncSession,
        user: User,
        accessible: list[UUID] | None,
        contact_id: UUID,
        *,
        include_deleted: bool = False,
    ) -> Contact:
        """가시성 절을 통과하는 연락처 1건. 부재/타org/삭제/불가시는 전부 404.

        `include_deleted=True` 면 soft-delete 된 행도 돌려준다 (승인 경로가
        "삭제됨(409)"과 "안 보임(404)"을 구분해야 하기 때문). 가시성 절은 그대로 걸린다.
        """
        if include_deleted:
            stmt = select(Contact).where(
                Contact.organization_id == user.organization_id,
                Contact.id == contact_id,
            )
            clause = self._visibility_clause(user, accessible)
            if clause is not None:
                stmt = stmt.where(clause)
        else:
            stmt = self._base_select(user, accessible).where(Contact.id == contact_id)
        contact = (await db.execute(stmt)).scalar_one_or_none()
        if contact is None:
            raise CONTACT_NOT_FOUND()
        return contact

    async def get_contact(
        self, db: AsyncSession, user: User, accessible: list[UUID] | None, contact_id: UUID
    ) -> ContactResponse:
        contact = await self._load_visible(db, user, accessible, contact_id)
        return await self._build_response(db, contact, include_pending=True)

    # ====================================================================
    # 태그
    # ====================================================================

    async def _resolve_tags(
        self, db: AsyncSession, user: User, names: list[str]
    ) -> list[ContactTag]:
        """태그 문자열 → 태그 행. (org, key) upsert 로 표기 흔들림을 흡수한다 (D7).

        표시명은 **최초 등록 표기 유지** — 이미 있는 키는 name 을 갱신하지 않는다.
        """
        if not names:
            return []
        by_key = {n.strip().lower(): n.strip() for n in names if n and n.strip()}
        if not by_key:
            return []

        # 없는 것만 삽입 (동시 요청 경합은 UNIQUE + DO NOTHING 으로 흡수)
        await db.execute(
            pg_insert(ContactTag)
            .values(
                [
                    {
                        "organization_id": user.organization_id,
                        "name": name,
                        "key": key,
                        "created_by": user.id,
                    }
                    for key, name in by_key.items()
                ]
            )
            .on_conflict_do_nothing(constraint="uq_contact_tags_org_key")
        )
        await db.flush()

        rows = (
            await db.execute(
                select(ContactTag).where(
                    ContactTag.organization_id == user.organization_id,
                    ContactTag.key.in_(list(by_key.keys())),
                )
            )
        ).scalars().all()
        found = {t.key: t for t in rows}
        # 입력 순서를 보존해 돌려준다 (링크 생성 순서 = 사용자가 친 순서)
        return [found[k] for k in by_key if k in found]

    async def list_tags(
        self,
        db: AsyncSession,
        user: User,
        accessible: list[UUID] | None,
        *,
        q: str | None = None,
        limit: int = 20,
    ) -> list[ContactTagResponse]:
        """태그 자동완성 (계약 §4.3).

        usage_count 는 **caller 가 볼 수 있는 연락처 기준**이다 — 안 보이는 연락처 수를
        노출하지 않기 위해 링크 조인에 가시 연락처 서브쿼리를 건다.
        """
        limit = max(1, min(limit, 50))
        visible = self._visible_contact_ids_subquery(user, accessible)
        usage = func.count(ContactTagLink.id)
        stmt = (
            select(ContactTag.id, ContactTag.name, ContactTag.key, usage.label("usage_count"))
            .select_from(ContactTag)
            .outerjoin(
                ContactTagLink,
                and_(
                    ContactTagLink.tag_id == ContactTag.id,
                    ContactTagLink.contact_id.in_(visible),
                ),
            )
            .where(ContactTag.organization_id == user.organization_id)
            .group_by(ContactTag.id, ContactTag.name, ContactTag.key)
            .order_by(usage.desc(), ContactTag.key.asc())
            .limit(limit)
        )
        if q and q.strip():
            prefix = _escape_like(q.strip().lower())
            stmt = stmt.where(ContactTag.key.like(f"{prefix}%", escape="\\"))

        rows = (await db.execute(stmt)).all()
        return [
            ContactTagResponse(id=str(tid), name=name, key=key, usage_count=count)
            for tid, name, key, count in rows
        ]

    # ====================================================================
    # 내용 반영 (생성/수정/승인 공용)
    # ====================================================================

    async def _write_phones(
        self, db: AsyncSession, contact: Contact, phones: list[ContactPhoneInput]
    ) -> None:
        """번호 전량 교체 — delete-all + re-insert (행 id 는 외부에 의미가 없다).

        대표번호가 하나도 없으면 첫 번째를 자동 승격한다(응답에 반영되므로 조용한 실패 아님).
        """
        existing = (
            await db.execute(
                select(ContactPhone).where(ContactPhone.contact_id == contact.id)
            )
        ).scalars().all()
        for row in existing:
            await db.delete(row)
        await db.flush()

        has_primary = any(p.is_primary for p in phones)
        for index, p in enumerate(phones):
            db.add(
                ContactPhone(
                    contact_id=contact.id,
                    label=p.label,
                    number=p.number,
                    number_normalized=normalize_phone(p.number),
                    is_primary=p.is_primary or (index == 0 and not has_primary),
                    sort_order=index,
                )
            )
        await db.flush()

    async def _write_tags(
        self, db: AsyncSession, user: User, contact: Contact, names: list[str]
    ) -> None:
        """태그 링크 전량 교체."""
        existing = (
            await db.execute(
                select(ContactTagLink).where(ContactTagLink.contact_id == contact.id)
            )
        ).scalars().all()
        for row in existing:
            await db.delete(row)
        await db.flush()

        for tag in await self._resolve_tags(db, user, names):
            db.add(ContactTagLink(contact_id=contact.id, tag_id=tag.id))
        await db.flush()

    async def _apply_content(
        self,
        db: AsyncSession,
        user: User,
        contact: Contact,
        fields: dict[str, Any],
    ) -> None:
        """검증된 필드 dict 를 연락처에 반영한다.

        키가 없으면 변경 없음 (PUT 의 exclude_unset 규약). phones/tags 는 값이 있으면
        전량 교체, `[]` 이면 전삭제.
        """
        if "name" in fields:
            contact.name = fields["name"]
        for key in ("company", "email", "memo"):
            if key in fields:
                setattr(contact, key, fields[key])
        if "store_id" in fields:
            raw = fields["store_id"]
            if raw is None:
                contact.store_id = None
            else:
                store_uuid = _parse_uuid(str(raw), "store_id")
                await self._guard_store(db, user, store_uuid)
                contact.store_id = store_uuid
        if fields.get("phones") is not None:
            await self._write_phones(db, contact, fields["phones"])
        if fields.get("tags") is not None:
            await self._write_tags(db, user, contact, fields["tags"])

    async def _duplicate_warnings(
        self,
        db: AsyncSession,
        user: User,
        accessible: list[UUID] | None,
        contact: Contact,
    ) -> list[ContactDuplicatePhone]:
        """같은 정규화 번호를 가진 **다른** 연락처 경고 (N7 — 차단 안 함).

        caller 가 볼 수 있는 연락처만 알려준다(안 보이는 연락처의 존재를 노출하지 않기 위해).
        """
        mine = (
            await db.execute(
                select(ContactPhone.number, ContactPhone.number_normalized).where(
                    ContactPhone.contact_id == contact.id,
                    ContactPhone.number_normalized.is_not(None),
                )
            )
        ).all()
        if not mine:
            return []
        by_normalized = {n: raw for raw, n in mine}

        visible = self._visible_contact_ids_subquery(user, accessible)
        rows = (
            await db.execute(
                select(ContactPhone.number_normalized, Contact.id, Contact.name)
                .join(Contact, Contact.id == ContactPhone.contact_id)
                .where(
                    ContactPhone.number_normalized.in_(list(by_normalized.keys())),
                    Contact.id != contact.id,
                    Contact.id.in_(visible),
                )
                .order_by(Contact.name)
            )
        ).all()

        seen: set[tuple[str, UUID]] = set()
        warnings: list[ContactDuplicatePhone] = []
        for normalized, other_id, other_name in rows:
            if (normalized, other_id) in seen:
                continue
            seen.add((normalized, other_id))
            warnings.append(
                ContactDuplicatePhone(
                    number=by_normalized[normalized],
                    number_normalized=normalized,
                    contact_id=str(other_id),
                    contact_name=other_name,
                )
            )
        return warnings

    # ====================================================================
    # 생성 / 수정 / 삭제
    # ====================================================================

    async def create_contact(
        self,
        db: AsyncSession,
        user: User,
        accessible: list[UUID] | None,
        payload: ContactPayload,
        *,
        reason: str | None = None,
        created_by: UUID | None = None,
        change_request_id: UUID | None = None,
    ) -> ContactResponse:
        """연락처 생성. 신청 승인 경로도 이 메서드를 쓴다(created_by = 신청자).

        commit 은 하지 않는다 — 호출부(라우터 진입 서비스 메서드)가 커밋한다.
        """
        contact = Contact(
            organization_id=user.organization_id,
            name=payload.name,
            company=payload.company,
            email=payload.email,
            memo=payload.memo,
            created_by=created_by or user.id,
            updated_by=user.id,
        )
        db.add(contact)
        await db.flush()

        fields = payload.model_dump(exclude_unset=False)
        fields["phones"] = payload.phones or []
        fields["tags"] = payload.tags or []
        await self._apply_content(db, user, contact, fields)
        await db.flush()

        resp = await self._build_response(db, contact)
        await contact_audit_service.record(
            db,
            organization_id=user.organization_id,
            action="create",
            actor=user,
            contact_id=contact.id,
            contact_name=contact.name,
            change_request_id=change_request_id,
            reason=reason,
            before=None,
            after=self._snapshot(resp),
        )
        resp.duplicate_phone_warnings = await self._duplicate_warnings(
            db, user, accessible, contact
        )
        return resp

    async def create_contact_api(
        self,
        db: AsyncSession,
        user: User,
        accessible: list[UUID] | None,
        data: Any,
    ) -> ContactResponse:
        """POST /contacts/ 진입점 — 생성 후 커밋."""
        payload = ContactPayload(
            name=data.name,
            company=data.company,
            email=data.email,
            memo=data.memo,
            store_id=data.store_id,
            phones=data.phones,
            tags=data.tags,
        )
        resp = await self.create_contact(
            db, user, accessible, payload, reason=data.reason
        )
        await db.commit()
        return resp

    async def update_contact(
        self,
        db: AsyncSession,
        user: User,
        accessible: list[UUID] | None,
        contact: Contact,
        fields: dict[str, Any],
        *,
        reason: str,
        change_request_id: UUID | None = None,
    ) -> ContactResponse:
        """연락처 수정 (전체 치환). no-op 이면 이력을 남기지 않는다 (계약 §4.6).

        commit 은 하지 않는다.
        """
        before_resp = await self._build_response(db, contact)
        before_snapshot = self._snapshot(before_resp)

        await self._apply_content(db, user, contact, fields)
        contact.updated_by = user.id
        await db.flush()

        after_resp = await self._build_response(db, contact)
        after_snapshot = self._snapshot(after_resp)

        changed_before, changed_after = diff_snapshots(before_snapshot, after_snapshot)
        if changed_after:
            await contact_audit_service.record(
                db,
                organization_id=user.organization_id,
                action="update",
                actor=user,
                contact_id=contact.id,
                contact_name=contact.name,
                change_request_id=change_request_id,
                reason=reason,
                before=changed_before,
                after=changed_after,
            )
        after_resp.duplicate_phone_warnings = await self._duplicate_warnings(
            db, user, accessible, contact
        )
        return after_resp

    async def update_contact_api(
        self,
        db: AsyncSession,
        user: User,
        accessible: list[UUID] | None,
        contact_id: UUID,
        data: ContactUpdate,
    ) -> ContactResponse:
        """PUT /contacts/{id} 진입점 — 보낸 키만 반영하고 커밋."""
        contact = await self._load_visible(db, user, accessible, contact_id)
        fields = data.model_dump(exclude_unset=True)
        reason = fields.pop("reason", None)
        if not reason:
            raise CONTACT_REASON_REQUIRED(message="Enter a reason for this change.")
        # phones 는 Pydantic 모델 그대로 써야 하므로 model_dump 결과를 원본으로 되돌린다.
        if "phones" in fields:
            fields["phones"] = data.phones if data.phones is not None else []
        if "tags" in fields:
            fields["tags"] = data.tags if data.tags is not None else []
        resp = await self.update_contact(
            db, user, accessible, contact, fields, reason=reason
        )
        await db.commit()
        return resp

    async def delete_contact(
        self,
        db: AsyncSession,
        user: User,
        accessible: list[UUID] | None,
        contact_id: UUID,
        reason: str,
        *,
        change_request_id: UUID | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        """soft delete (사유 필수) + 대기 중 신청 superseded 전환 (계약 §4.7).

        자식(번호/태그 링크)은 지우지 않는다 — 모든 읽기가 부모의 deleted_at 을 타므로
        노출되지 않고, 복구 시 그대로 살아난다.
        """
        if not reason or not reason.strip():
            raise CONTACT_REASON_REQUIRED(
                message="Enter a reason for deleting this contact."
            )
        contact = await self._load_visible(db, user, accessible, contact_id)
        before_resp = await self._build_response(db, contact)

        contact.deleted_at = datetime.now(timezone.utc)
        contact.deleted_by = user.id
        contact.delete_reason = reason.strip()
        contact.updated_by = user.id
        await db.flush()

        await contact_audit_service.record(
            db,
            organization_id=user.organization_id,
            action="delete",
            actor=user,
            contact_id=contact.id,
            contact_name=contact.name,
            change_request_id=change_request_id,
            reason=reason.strip(),
            before=self._snapshot(before_resp),
            after=None,
        )

        superseded = await self._supersede_pending(
            db, user, contact, exclude_request_id=change_request_id
        )
        if commit:
            await db.commit()
        return {
            "message": "Contact deleted",
            "superseded_request_count": superseded,
        }

    async def _supersede_pending(
        self,
        db: AsyncSession,
        user: User,
        contact: Contact,
        *,
        exclude_request_id: UUID | None = None,
    ) -> int:
        """대상 연락처의 pending 신청을 superseded 로 전환하고 각각 이력을 남긴다."""
        stmt = select(ContactChangeRequest).where(
            ContactChangeRequest.contact_id == contact.id,
            ContactChangeRequest.status == "pending",
        )
        if exclude_request_id is not None:
            stmt = stmt.where(ContactChangeRequest.id != exclude_request_id)
        rows = (await db.execute(stmt)).scalars().all()
        now = datetime.now(timezone.utc)
        for req in rows:
            req.status = "superseded"
            req.resolved_at = now
            req.resolved_by = user.id
            req.resolved_by_name = user.full_name
            await contact_audit_service.record(
                db,
                organization_id=user.organization_id,
                action="request_superseded",
                actor=user,
                contact_id=contact.id,
                contact_name=contact.name,
                change_request_id=req.id,
                before={"status": "pending"},
                after={"status": "superseded"},
            )
        await db.flush()
        return len(rows)

    # ====================================================================
    # 변경 신청 (D4)
    # ====================================================================

    def _build_request_response(
        self,
        req: ContactChangeRequest,
        *,
        current: ContactResponse | None = None,
        contact_updated_at: datetime | None = None,
    ) -> ContactChangeRequestResponse:
        """신청 응답 조립. is_stale 는 경고일 뿐 승인을 막지 않는다 (N5)."""
        is_stale = False
        if (
            req.request_type != "create"
            and req.base_updated_at is not None
            and contact_updated_at is not None
        ):
            is_stale = contact_updated_at > req.base_updated_at
        return ContactChangeRequestResponse(
            id=str(req.id),
            request_type=req.request_type,
            status=req.status,
            contact_id=str(req.contact_id) if req.contact_id else None,
            contact_name=req.contact_name_snapshot,
            payload=req.payload,
            applied_payload=req.applied_payload,
            reason=req.reason,
            requested_by=str(req.requested_by) if req.requested_by else None,
            requested_by_name=req.requested_by_name,
            requested_at=req.requested_at,
            resolved_by=str(req.resolved_by) if req.resolved_by else None,
            resolved_by_name=req.resolved_by_name,
            resolved_at=req.resolved_at,
            resolution_note=req.resolution_note,
            base_updated_at=req.base_updated_at,
            is_stale=is_stale,
            current_contact=current,
        )

    async def _build_request_responses(
        self,
        db: AsyncSession,
        user: User,
        accessible: list[UUID] | None,
        requests: Sequence[ContactChangeRequest],
        *,
        include_current: bool,
    ) -> list[ContactChangeRequestResponse]:
        """신청 목록 응답 — 대상 연락처를 in-bulk 로 붙인다."""
        if not requests:
            return []
        contact_ids = {r.contact_id for r in requests if r.contact_id is not None}
        contacts: dict[UUID, Contact] = {}
        current_map: dict[UUID, ContactResponse] = {}
        if contact_ids:
            rows = (
                await db.execute(
                    self._base_select(user, accessible).where(Contact.id.in_(contact_ids))
                )
            ).scalars().all()
            contacts = {c.id: c for c in rows}
            if include_current and rows:
                built = await self._build_responses(db, rows)
                current_map = {UUID(item.id): item for item in built}

        return [
            self._build_request_response(
                r,
                current=current_map.get(r.contact_id) if include_current else None,
                contact_updated_at=(
                    contacts[r.contact_id].updated_at
                    if r.contact_id in contacts
                    else None
                ),
            )
            for r in requests
        ]

    @staticmethod
    def _validate_request_shape(data: ContactChangeRequestCreate) -> None:
        """신청 종류별 shape/사유 검증 (계약 §5.2).

        스키마가 아니라 여기서 검증하는 이유: Pydantic 에서 걸면 FastAPI 기본 422 가
        나가는데, 계약 §6 은 이 도메인의 검증 실패를 400 + CONTACT_* 코드로 통일한다.
        직접 수정 경로(update/delete)의 사유 필수와도 응답 형태가 같아진다.
        """
        if data.request_type == "create":
            if data.contact_id is not None:
                raise CONTACT_VALIDATION_ERROR(
                    message="A new-contact request cannot target an existing contact."
                )
            if data.payload is None:
                raise CONTACT_VALIDATION_ERROR(
                    message="Enter the contact details to request."
                )
            return

        if not data.contact_id:
            raise CONTACT_VALIDATION_ERROR(message="Select the contact to change.")

        if data.request_type == "update":
            if data.payload is None:
                raise CONTACT_VALIDATION_ERROR(
                    message="Enter the contact details to request."
                )
            if not data.reason or not data.reason.strip():
                raise CONTACT_REASON_REQUIRED(
                    message="Enter a reason for the change you are requesting."
                )
            return

        # delete
        if data.payload is not None:
            raise CONTACT_VALIDATION_ERROR(
                message="A delete request cannot carry contact details."
            )
        if not data.reason or not data.reason.strip():
            raise CONTACT_REASON_REQUIRED(
                message="Enter a reason for the deletion you are requesting."
            )

    async def create_request(
        self,
        db: AsyncSession,
        user: User,
        accessible: list[UUID] | None,
        data: ContactChangeRequestCreate,
        *,
        writable_types: set[str],
    ) -> ContactChangeRequestResponse:
        """변경 신청 생성 (계약 §5.2).

        **쓰기 권한 보유자는 신청이 아니라 즉시 반영된다** — 같은 신청 행을 만들되
        자기 승인으로 바로 approved 처리한다(기록은 남기고 대기열은 오염시키지 않는다).
        """
        self._validate_request_shape(data)

        contact: Contact | None = None
        if data.contact_id:
            contact_uuid = _parse_uuid(data.contact_id, "contact_id")
            contact = await self._load_visible(db, user, accessible, contact_uuid)

        if data.payload is not None and data.payload.store_id:
            store_uuid = _parse_uuid(data.payload.store_id, "store_id")
            await self._guard_store(db, user, store_uuid)

        req = ContactChangeRequest(
            organization_id=user.organization_id,
            request_type=data.request_type,
            contact_id=contact.id if contact else None,
            contact_name_snapshot=(
                contact.name if contact else (data.payload.name if data.payload else None)
            ),
            payload=data.payload.model_dump() if data.payload else None,
            reason=data.reason,
            status="pending",
            base_updated_at=contact.updated_at if contact else None,
            requested_by=user.id,
            requested_by_name=user.full_name,
        )
        db.add(req)
        await db.flush()

        await contact_audit_service.record(
            db,
            organization_id=user.organization_id,
            action="request_create",
            actor=user,
            contact_id=contact.id if contact else None,
            contact_name=req.contact_name_snapshot,
            change_request_id=req.id,
            reason=data.reason,
            before=None,
            after=req.payload,
        )

        if data.request_type in writable_types:
            # 쓰기 권한이 있으면 대기시키지 않고 그 자리에서 반영한다.
            await self._apply_request(db, user, accessible, req, payload=None, note=None)
            await db.commit()
            await db.refresh(req)
            return (
                await self._build_request_responses(
                    db, user, accessible, [req], include_current=True
                )
            )[0]

        await db.commit()
        await db.refresh(req)
        return self._build_request_response(
            req,
            current=None,
            contact_updated_at=contact.updated_at if contact else None,
        )

    async def list_requests(
        self,
        db: AsyncSession,
        user: User,
        accessible: list[UUID] | None,
        *,
        writable_types: set[str],
        status: str = "pending",
        request_type: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[list[ContactChangeRequestResponse], int]:
        """처리 대기 목록 (계약 §5.4) — 종류 게이트 + 가시성 절.

        쓰기 권한이 하나도 없으면 **빈 페이지**(403 아님 — 정보가 새지 않게).
        """
        page = max(1, page)
        per_page = max(1, min(per_page, 100))
        if not writable_types:
            return [], 0

        types = sorted(writable_types)
        if request_type:
            if request_type not in types:
                return [], 0
            types = [request_type]

        stmt = select(ContactChangeRequest).where(
            ContactChangeRequest.organization_id == user.organization_id,
            ContactChangeRequest.request_type.in_(types),
        )
        if status and status != "all":
            stmt = stmt.where(ContactChangeRequest.status == status)

        clause = self._visibility_clause(user, accessible)
        if clause is not None:
            visible = self._visible_contact_ids_subquery(user, accessible)
            store_values = [str(s) for s in (accessible or [])]
            payload_store = ContactChangeRequest.payload["store_id"].astext
            create_visible: ColumnElement[bool] = payload_store.is_(None)
            if store_values:
                create_visible = or_(create_visible, payload_store.in_(store_values))
            stmt = stmt.where(
                or_(
                    and_(
                        ContactChangeRequest.request_type == "create",
                        create_visible,
                    ),
                    ContactChangeRequest.contact_id.in_(visible),
                )
            )

        # 오래된 신청 먼저 — 방치 방지 (계약 §5.4)
        stmt = stmt.order_by(
            ContactChangeRequest.requested_at.asc(), ContactChangeRequest.id.asc()
        )
        rows, total = await paginate(db, stmt, page=page, per_page=per_page)
        items = await self._build_request_responses(
            db, user, accessible, rows, include_current=True
        )
        return items, total

    async def list_my_requests(
        self,
        db: AsyncSession,
        user: User,
        accessible: list[UUID] | None,
        *,
        status: str = "all",
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[list[ContactChangeRequestResponse], int]:
        """내 신청 목록 (계약 §5.5) — 가시성 절 **미적용** (N4).

        내가 낸 신청은 그 뒤 매장 배정이 바뀌어도 상태와 반려 사유를 볼 수 있어야 한다.
        """
        page = max(1, page)
        per_page = max(1, min(per_page, 100))
        stmt = select(ContactChangeRequest).where(
            ContactChangeRequest.organization_id == user.organization_id,
            ContactChangeRequest.requested_by == user.id,
        )
        if status and status != "all":
            stmt = stmt.where(ContactChangeRequest.status == status)
        stmt = stmt.order_by(
            ContactChangeRequest.requested_at.desc(), ContactChangeRequest.id.asc()
        )
        rows, total = await paginate(db, stmt, page=page, per_page=per_page)
        # current_contact 는 null (계약 §5.5). is_stale 계산도 대상 로드가 필요 없다.
        items = [self._build_request_response(r) for r in rows]
        return items, total

    async def _load_request(
        self, db: AsyncSession, user: User, request_id: UUID
    ) -> ContactChangeRequest:
        req = (
            await db.execute(
                select(ContactChangeRequest).where(
                    ContactChangeRequest.id == request_id,
                    ContactChangeRequest.organization_id == user.organization_id,
                )
            )
        ).scalar_one_or_none()
        if req is None:
            raise CONTACT_REQUEST_NOT_FOUND()
        return req

    async def cancel_request(
        self,
        db: AsyncSession,
        user: User,
        accessible: list[UUID] | None,
        request_id: UUID,
    ) -> ContactChangeRequestResponse:
        """신청 취소 — 본인 + pending 만 (계약 §5.6)."""
        req = await self._load_request(db, user, request_id)
        if req.requested_by != user.id:
            raise CONTACT_NOT_YOUR_REQUEST()
        if req.status != "pending":
            raise CONTACT_REQUEST_NOT_PENDING(status=req.status)

        req.status = "cancelled"
        req.resolved_at = datetime.now(timezone.utc)
        req.resolved_by = user.id
        req.resolved_by_name = user.full_name
        await contact_audit_service.record(
            db,
            organization_id=user.organization_id,
            action="request_cancel",
            actor=user,
            contact_id=req.contact_id,
            contact_name=req.contact_name_snapshot,
            change_request_id=req.id,
            before={"status": "pending"},
            after={"status": "cancelled"},
        )
        await db.commit()
        await db.refresh(req)
        return self._build_request_response(req)

    async def _apply_request(
        self,
        db: AsyncSession,
        user: User,
        accessible: list[UUID] | None,
        req: ContactChangeRequest,
        *,
        payload: ContactPayload | None,
        note: str | None,
    ) -> ContactResponse | None:
        """신청을 실제로 반영하고 approved 로 마감한다 (커밋은 호출부).

        원문 payload 는 덮어쓰지 않는다 — 승인자가 고쳐서 반영하면 applied_payload 에
        따로 남긴다 (D4: 신청 원문 영구 보존).
        """
        effective = payload
        if effective is None and req.payload is not None:
            effective = ContactPayload(**req.payload)

        contact_resp: ContactResponse | None = None
        target: Contact | None = None
        if req.contact_id is not None:
            # 대상 연락처도 **처리자의 가시성 절**을 통과해야 한다.
            # (신청 목록은 가시성으로 걸러지지만 request_id 를 직접 아는 호출은 그렇지 않다.
            #  걸러 두지 않으면 안 보이는 매장의 연락처를 승인 경로로 읽고 고칠 수 있다.)
            # deleted_at 필터는 여기서 붙이지 않는다 — 삭제된 대상은 404 가 아니라
            # 409 contact_deleted 로 구분해서 알려야 한다(계약 §5.7).
            target = await self._load_visible(
                db, user, accessible, req.contact_id, include_deleted=True
            )
            if target.deleted_at is not None:
                raise CONTACT_DELETED()

        if req.request_type == "create":
            assert effective is not None
            contact_resp = await self.create_contact(
                db,
                user,
                accessible,
                effective,
                reason=req.reason,
                # 신청한 사람이 소유자 (계약 §5.7)
                created_by=req.requested_by,
                change_request_id=req.id,
            )
            req.contact_id = UUID(contact_resp.id)
            req.contact_name_snapshot = contact_resp.name
        elif req.request_type == "update":
            assert target is not None and effective is not None
            fields = effective.model_dump(exclude_unset=False)
            fields["phones"] = effective.phones or []
            fields["tags"] = effective.tags or []
            contact_resp = await self.update_contact(
                db,
                user,
                accessible,
                target,
                fields,
                reason=req.reason or "Approved change request",
                change_request_id=req.id,
            )
            req.contact_name_snapshot = contact_resp.name
        else:  # delete
            assert target is not None
            before_resp = await self._build_response(db, target)
            await self.delete_contact(
                db,
                user,
                accessible,
                target.id,
                req.reason or "Approved delete request",
                change_request_id=req.id,
                commit=False,
            )
            contact_resp = before_resp

        req.status = "approved"
        req.resolved_at = datetime.now(timezone.utc)
        req.resolved_by = user.id
        req.resolved_by_name = user.full_name
        if note:
            req.resolution_note = note
        if payload is not None:
            req.applied_payload = payload.model_dump()

        await contact_audit_service.record(
            db,
            organization_id=user.organization_id,
            action="request_approve",
            actor=user,
            contact_id=req.contact_id,
            contact_name=req.contact_name_snapshot,
            change_request_id=req.id,
            reason=note,
            before=req.payload,
            after=req.applied_payload or req.payload,
        )
        await db.flush()
        return contact_resp

    async def approve_request(
        self,
        db: AsyncSession,
        user: User,
        accessible: list[UUID] | None,
        request_id: UUID,
        body: ContactRequestApprove,
    ) -> ContactApproveResponse:
        """신청 승인 + 반영 (계약 §5.7). 이력 2행 (request_approve + 실제 변경)."""
        req = await self._load_request(db, user, request_id)
        if req.status != "pending":
            raise CONTACT_REQUEST_NOT_PENDING(status=req.status)

        if body.payload is not None and body.payload.store_id:
            store_uuid = _parse_uuid(body.payload.store_id, "store_id")
            await self._guard_store(db, user, store_uuid)

        contact_resp = await self._apply_request(
            db, user, accessible, req, payload=body.payload, note=body.note
        )
        await db.commit()
        await db.refresh(req)
        assert contact_resp is not None
        return ContactApproveResponse(
            request=self._build_request_response(req),
            contact=contact_resp,
        )

    async def reject_request(
        self,
        db: AsyncSession,
        user: User,
        accessible: list[UUID] | None,
        request_id: UUID,
        reason: str,
    ) -> ContactChangeRequestResponse:
        """신청 반려 — 사유 필수 (계약 §5.8).

        승인과 동일하게 **대상 연락처가 처리자에게 보여야** 한다. 삭제된 대상은
        반려가 가능해야 하므로(계약 §5.7 안내 문구) 삭제 여부는 막지 않는다.
        """
        req = await self._load_request(db, user, request_id)
        if req.contact_id is not None:
            await self._load_visible(
                db, user, accessible, req.contact_id, include_deleted=True
            )
        if req.status != "pending":
            raise CONTACT_REQUEST_NOT_PENDING(status=req.status)
        if not reason or not reason.strip():
            raise CONTACT_REASON_REQUIRED(
                message="Enter a reason for rejecting this request."
            )

        req.status = "rejected"
        req.resolution_note = reason.strip()
        req.resolved_at = datetime.now(timezone.utc)
        req.resolved_by = user.id
        req.resolved_by_name = user.full_name
        await contact_audit_service.record(
            db,
            organization_id=user.organization_id,
            action="request_reject",
            actor=user,
            contact_id=req.contact_id,
            contact_name=req.contact_name_snapshot,
            change_request_id=req.id,
            reason=reason.strip(),
            before={"status": "pending"},
            after={"status": "rejected"},
        )
        await db.commit()
        await db.refresh(req)
        return self._build_request_response(req)

    # ====================================================================
    # 권한 헬퍼
    # ====================================================================

    async def get_request_type(
        self, db: AsyncSession, user: User, request_id: UUID
    ) -> str:
        """신청 종류만 조회 — 라우터가 종류별 쓰기 권한을 검사하기 위해 쓴다.

        존재하지 않거나 타 org 이면 404 (권한 검사 전에 존재를 숨긴다).
        """
        req = await self._load_request(db, user, request_id)
        return req.request_type

    async def writable_request_types(self, db: AsyncSession, user: User) -> set[str]:
        """caller 가 처리할 수 있는 신청 종류 집합 (Owner bypass 포함)."""
        from app.api.deps import user_has_permissions

        allowed: set[str] = set()
        for request_type in CONTACT_REQUEST_TYPES:
            if await user_has_permissions(db, user, REQUEST_TYPE_PERMISSION[request_type]):
                allowed.add(request_type)
        return allowed


contact_service = ContactService()
