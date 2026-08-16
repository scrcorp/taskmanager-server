"""Service for unified multi-type Report.

타입별 로직(검증, 본문 생성 등)은 모두 이 service에 모음.
새 타입 추가 시 type 분기를 늘리는 방식. 분기가 많아지면 strategy 패턴으로
type별 클래스 분리 고려.
"""
import logging
import uuid
from html import escape
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from sqlalchemy import and_, or_

from app.core.error_codes.reports import (
    ISSUE_RECIPIENT_IDS_INVALID,
    ISSUE_RECIPIENT_NOT_IN_ORG,
    ISSUE_VISIBILITY_SCOPE_INVALID,
    REPORT_NOT_VISIBLE,
)
from app.config import settings
from app.core.permissions import (
    GM_PRIORITY,
    STAFF_PRIORITY,
    SV_PRIORITY,
    is_gm_plus,
    is_owner,
)
from app.models.organization import Store
from app.models.report import (
    Report,
    ReportAcknowledgement,
    ReportComment,
    ReportTemplate,
    ReportType,
)
from app.models.user import Role, User
from app.models.user_store import UserStore
from app.repositories.report_repository import (
    report_repository,
    report_template_repository,
    report_type_repository,
)
from app.core.issue_fields import (
    build_fields_snapshot,
    resolve_issue_fields,
    validate_and_normalize_values,
)
from app.schemas.report import (
    DEFAULT_ISSUE_CATEGORIES,
    DEFAULT_ISSUE_CATEGORY_DESCRIPTION_TEMPLATES,
    DEFAULT_ISSUE_CATEGORY_FIELDS,
    DEFAULT_REPORT_TYPE_DEFS,
    ISSUE_VISIBILITY_SCOPES,
    LEGACY_ISSUE_CATEGORY_DESCRIPTION_TEMPLATES,
    ReportCommentCreate,
    ReportCreate,
    ReportTemplateCreate,
    ReportTemplateUpdate,
    ReportTypeCreate,
    ReportTypeUpdate,
    ReportUpdate,
)
from app.utils.exceptions import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.utils.timezone import get_store_timezone

logger = logging.getLogger(__name__)


# ── 타입별 검증/본문 빌더 ────────────────────────────────────


async def _validate_issue_links(
    db: AsyncSession,
    organization_id: UUID,
    store_id: UUID,
    links: dict[str, Any] | None,
) -> None:
    """payload.links에 들어 있는 ID들이 해당 매장/조직에 속하는지 검증.

    검증 통과 조건:
    - schedule_ids: schedules.store_id == store_id
    - checklist_instance_ids: checklist_instances.store_id == store_id
    - position_ids: positions.store_id == store_id
    - work_role_ids: store_work_roles.store_id == store_id
    - related_user_ids: users.organization_id == organization_id (매장 소속까진 강제 안 함)
    """
    if not links:
        return

    from app.models.schedule import Schedule, StoreWorkRole
    from app.models.checklist import ChecklistInstance
    from app.models.work import Position

    def _parse_uuids(values: Any, field: str) -> list[UUID]:
        if not values:
            return []
        try:
            return [UUID(v) for v in values]
        except (TypeError, ValueError):
            raise BadRequestError(f"links.{field} contains invalid UUID")

    schedule_ids = _parse_uuids(links.get("schedule_ids"), "schedule_ids")
    if schedule_ids:
        rows = await db.execute(
            select(Schedule.id).where(
                Schedule.id.in_(schedule_ids),
                Schedule.store_id == store_id,
            )
        )
        found = {r[0] for r in rows.all()}
        missing = [str(x) for x in schedule_ids if x not in found]
        if missing:
            raise BadRequestError(
                f"links.schedule_ids contain ids not in this store: {missing}"
            )

    cl_ids = _parse_uuids(links.get("checklist_instance_ids"), "checklist_instance_ids")
    if cl_ids:
        rows = await db.execute(
            select(ChecklistInstance.id).where(
                ChecklistInstance.id.in_(cl_ids),
                ChecklistInstance.store_id == store_id,
            )
        )
        found = {r[0] for r in rows.all()}
        missing = [str(x) for x in cl_ids if x not in found]
        if missing:
            raise BadRequestError(
                f"links.checklist_instance_ids contain ids not in this store: {missing}"
            )

    pos_ids = _parse_uuids(links.get("position_ids"), "position_ids")
    if pos_ids:
        rows = await db.execute(
            select(Position.id).where(
                Position.id.in_(pos_ids),
                Position.store_id == store_id,
            )
        )
        found = {r[0] for r in rows.all()}
        missing = [str(x) for x in pos_ids if x not in found]
        if missing:
            raise BadRequestError(
                f"links.position_ids contain ids not in this store: {missing}"
            )

    role_ids = _parse_uuids(links.get("work_role_ids"), "work_role_ids")
    if role_ids:
        rows = await db.execute(
            select(StoreWorkRole.id).where(
                StoreWorkRole.id.in_(role_ids),
                StoreWorkRole.store_id == store_id,
            )
        )
        found = {r[0] for r in rows.all()}
        missing = [str(x) for x in role_ids if x not in found]
        if missing:
            raise BadRequestError(
                f"links.work_role_ids contain ids not in this store: {missing}"
            )

    user_ids = _parse_uuids(links.get("related_user_ids"), "related_user_ids")
    if user_ids:
        rows = await db.execute(
            select(User.id).where(
                User.id.in_(user_ids),
                User.organization_id == organization_id,
            )
        )
        found = {r[0] for r in rows.all()}
        missing = [str(x) for x in user_ids if x not in found]
        if missing:
            raise BadRequestError(
                f"links.related_user_ids contain ids not in this organization: {missing}"
            )

    # role 약어 검증 — staff / sv / gm / owner / all (system role).
    related_roles = links.get("related_roles") or []
    if related_roles:
        valid = {"staff", "sv", "gm", "owner", "all"}
        invalid = [r for r in related_roles if r not in valid]
        if invalid:
            raise BadRequestError(
                f"links.related_roles must be one of {sorted(valid)}; got {invalid}"
            )


async def ensure_system_issue_template(db: AsyncSession) -> bool:
    """system default issue 템플릿(org_id=NULL, store_id=NULL) 보장 + 멱등 보정.

    - row 가 없으면 기본 카테고리 + description 프리셋으로 생성.
    - row 가 있으면 (a) 빠진 기본 카테고리를 sort_order=max+1 로 append,
      (b) 프리셋이 정의된 카테고리인데 description_template 키가 **아예 없는** 항목을 채우고,
      (c) 값이 **지난 버전 프리셋 원문 그대로**인 항목을 현재 원문으로 갱신한다
      (LEGACY_ISSUE_CATEGORY_DESCRIPTION_TEMPLATES — 프리셋 문구를 고쳐도 이미 시드된
      환경이 옛 문구에 고정되는 걸 막는다).
      명시적으로 null 로 비웠거나 운영자가 직접 고친 문구는 덮어쓰지 않는다
      (운영자가 지운 프리셋을 startup 이 되살리면 안 된다).

    반환: 변경이 있었으면 True.
    """
    existing = (
        await db.execute(
            select(ReportTemplate).where(
                ReportTemplate.type == "issue",
                ReportTemplate.organization_id.is_(None),
                ReportTemplate.store_id.is_(None),
                ReportTemplate.is_default.is_(True),
            )
        )
    ).scalars().first()

    if existing is None:
        categories = [
            {
                "code": code,
                "label": code.replace("_", " ").title(),
                "color": None,
                "sort_order": idx + 1,
                "is_active": True,
                "description_template": DEFAULT_ISSUE_CATEGORY_DESCRIPTION_TEMPLATES.get(code),
                # 프리셋 텍스트 대신 진짜 입력칸으로 묻는다(2026-08-15).
                "fields": [dict(f) for f in DEFAULT_ISSUE_CATEGORY_FIELDS.get(code, [])],
            }
            for idx, code in enumerate(DEFAULT_ISSUE_CATEGORIES)
        ]
        db.add(
            ReportTemplate(
                type="issue",
                organization_id=None,
                store_id=None,
                name="Default Issue Form",
                is_default=True,
                is_active=True,
                payload={"categories": categories, "custom_fields": []},
            )
        )
        await db.commit()
        logger.info("Created system default issue template")
        return True

    payload = dict(existing.payload or {})
    categories = [dict(c) for c in (payload.get("categories") or [])]
    present = {c.get("code") for c in categories}
    max_sort = max((c.get("sort_order") or 0) for c in categories) if categories else 0
    changed = False

    for code in DEFAULT_ISSUE_CATEGORIES:
        if code in present:
            continue
        max_sort += 1
        categories.append({
            "code": code,
            "label": code.replace("_", " ").title(),
            "color": None,
            "sort_order": max_sort,
            "is_active": True,
            "description_template": DEFAULT_ISSUE_CATEGORY_DESCRIPTION_TEMPLATES.get(code),
            "fields": [dict(f) for f in DEFAULT_ISSUE_CATEGORY_FIELDS.get(code, [])],
        })
        changed = True

    for cat in categories:
        code = cat.get("code")

        # (a) 기본 필드 백필 — 아직 필드를 안 가진 카테고리에만. 운영자가 만든 필드는
        #     건드리지 않는다(비어 있을 때만 채운다).
        seed_fields = DEFAULT_ISSUE_CATEGORY_FIELDS.get(code)
        if seed_fields and not (cat.get("fields") or []):
            cat["fields"] = [dict(f) for f in seed_fields]
            changed = True

        # (b) 프리셋 문구
        current = DEFAULT_ISSUE_CATEGORY_DESCRIPTION_TEMPLATES.get(code)
        legacy = LEGACY_ISSUE_CATEGORY_DESCRIPTION_TEMPLATES.get(code, [])
        if current is not None and "description_template" not in cat:
            cat["description_template"] = current
            changed = True
            continue
        # 옛 프리셋 원문 그대로면 → 현재 원문으로 갱신, 현재 원문이 없으면(필드로 승격)
        # **비운다**. 안 비우면 같은 항목을 필드와 프리셋이 두 번 묻는다.
        # null(운영자가 비움)이나 손댄 문구는 그대로 둔다.
        if cat.get("description_template") in legacy:
            cat["description_template"] = current
            changed = True

    if not changed:
        return False

    payload["categories"] = categories
    payload.setdefault("custom_fields", [])
    existing.payload = payload
    flag_modified(existing, "payload")
    await db.commit()
    logger.info("Backfilled system default issue template categories")
    return True


async def _validate_issue_sharing(
    db: AsyncSession,
    organization_id: UUID,
    payload: dict[str, Any],
    *,
    existing_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """issue payload 의 공유/알림 키 검증 + 정규화. 문제가 있으면 400.

    - visibility_scope: ISSUE_VISIBILITY_SCOPES 중 하나.
      모르는 값을 조용히 default 로 떨어뜨리면 작성자는 공유했다고 믿는데 공유가 안 된다.
      **미지정이면 "default" 를 박지 않고 `_issue_visibility_scope` 로 승계한다** —
      visibility_scope 를 모르는 구버전 클라(구 콘솔 탭 등)가 legacy
      share_with_store_all=true 인 payload 를 그대로 PUT 하는 순간 "매장 전원 공개" 가
      조용히 default 로 좁아지기 때문이다(하위호환 파손).
    - extra_viewers.user_ids: 같은 조직의 활성 사용자여야 한다.
      (조회권을 여는 키라 타 조직 id 를 넣으면 cross-tenant 열람이 열린다)
      단, **이미 저장돼 있던 id 는 다시 검증하지 않는다**(existing_payload). 지목했던
      사람이 나중에 퇴사·비활성화되면 그 리포트의 모든 수정이 400 으로 막혀 리포트가
      벽돌이 되기 때문이다. 새로 추가한 id 만 크게 실패한다.
    - notify_excluded_user_ids: **더 이상 아무 효과가 없다** (자동 수신자 = 그 매장 GM 이상은
      해제 불가). 구버전 클라가 계속 보내므로 400 을 내지 않고 그대로 받아 무시한다.
      효과가 없으니 값 검증도 하지 않는다 — 검증은 "이 키가 뭔가 한다"는 잘못된 신호다.
    """
    scope = payload.get("visibility_scope")
    if scope is None:
        payload["visibility_scope"] = _issue_visibility_scope(payload)
    elif scope not in ISSUE_VISIBILITY_SCOPES:
        raise ISSUE_VISIBILITY_SCOPE_INVALID(allowed=list(ISSUE_VISIBILITY_SCOPES))
    # visibility_scope 가 정본이 된 이상 legacy 키는 남겨두면 안 된다. 두 키가 공존하면
    # SQL clause(legacy OR) 와 파이썬 판정(scope 우선) 이 갈려 "목록엔 보이는데 열면 403"
    # 이 된다. 위에서 승계까지 끝났으므로 여기서 지워도 의미 손실은 없다.
    payload.pop("share_with_store_all", None)

    def _parse(values: Any, field: str) -> list[UUID]:
        if not values:
            return []
        if not isinstance(values, list):
            raise ISSUE_RECIPIENT_IDS_INVALID(field=f"payload.{field}")
        out: list[UUID] = []
        for v in values:
            try:
                out.append(UUID(str(v)))
            except (TypeError, ValueError):
                raise ISSUE_RECIPIENT_IDS_INVALID(field=f"payload.{field}")
        return out

    extra_viewers = payload.get("extra_viewers") or {}
    if extra_viewers and not isinstance(extra_viewers, dict):
        raise ISSUE_RECIPIENT_IDS_INVALID(field="payload.extra_viewers")
    to_check: list[tuple[str, list[UUID]]] = [
        ("extra_viewers.user_ids", _parse(extra_viewers.get("user_ids"), "extra_viewers.user_ids")),
    ]
    # 이미 저장돼 있던 지목 인원은 재검증 대상이 아니다 (위 docstring 참조).
    already = _payload_user_ids(
        ((existing_payload or {}).get("extra_viewers") or {}).get("user_ids")
    )
    for field, ids in to_check:
        new_ids = [x for x in ids if x not in already]
        if not new_ids:
            continue
        rows = await db.execute(
            select(User.id).where(
                User.id.in_(new_ids),
                User.organization_id == organization_id,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        found = {r[0] for r in rows.all()}
        missing = [str(x) for x in new_ids if x not in found]
        if missing:
            raise ISSUE_RECIPIENT_NOT_IN_ORG(field=f"payload.{field}", user_ids=missing)
    return payload


def _build_daily_payload_from_template(template: ReportTemplate, period: str) -> dict[str, Any]:
    """daily 리포트 생성 시 템플릿 sections를 본문 sections로 변환."""
    tpl_sections = (template.payload or {}).get("sections", []) or []
    sections = []
    for ts in sorted(tpl_sections, key=lambda s: s.get("sort_order", 0)):
        sections.append({
            "id": str(uuid.uuid4()),
            "title": ts.get("title", ""),
            "content": None,
            "sort_order": ts.get("sort_order", 0),
            "template_section_id": ts.get("id"),
        })
    return {"period": period, "sections": sections}


def _author_priority_subq():
    """Report.author_id 의 role priority 를 뽑는 correlated scalar subquery."""
    return (
        select(Role.priority)
        .select_from(User)
        .join(Role, Role.id == User.role_id)
        .where(User.id == Report.author_id)
        .correlate(Report)
        .scalar_subquery()
    )


def _manages_report_store_exists(user: User):
    """이 리포트의 매장에서 내가 manager 인가 (user_stores.is_manager) — correlated EXISTS.

    매장 배정(user_stores)만으로는 남의 리포트를 볼 근거가 안 된다. 같은 매장에서
    같이 일한다는 뜻일 뿐이기 때문이다. **관리 책임(is_manager)이 있는 매장에서만**
    아래 직급의 리포트가 열린다.
    """
    return (
        select(UserStore.id)
        .where(
            UserStore.user_id == user.id,
            UserStore.store_id == Report.store_id,
            UserStore.is_manager.is_(True),
        )
        .correlate(Report)
        .exists()
    )


def _assigned_to_report_store_exists(user: User):
    """이 리포트의 매장에 내가 배정돼 있는가 (is_manager 무관) — correlated EXISTS.

    visibility_scope="store_all"(작성자가 매장 전원에게 공개한 경우) 전용 근거.
    """
    return (
        select(UserStore.id)
        .where(
            UserStore.user_id == user.id,
            UserStore.store_id == Report.store_id,
        )
        .correlate(Report)
        .exists()
    )


def _issue_visibility_scope(payload: dict[str, Any] | None) -> str:
    """issue payload 의 조회 범위 정규화 — 해석은 이 함수 한 곳에서만 한다.

    - visibility_scope 가 있으면 그 값 (알 수 없는 값은 default 로 — 쓰기 시점에 400 으로
      이미 막았으므로, 읽기에서는 가장 좁은 쪽으로 떨어뜨린다)
    - 없으면 legacy share_with_store_all=true → "store_all"
    - 그 외 "default"
    """
    payload = payload or {}
    scope = payload.get("visibility_scope")
    if isinstance(scope, str) and scope in ISSUE_VISIBILITY_SCOPES:
        return scope
    # SQL 쪽은 `payload->>'share_with_store_all' = 'true'` 이므로 여기서도 정확히
    # 그 값만 인정한다. bool(...) 로 두면 문자열 "false" 같은 쓰레기 값에서 두 판정이 갈린다.
    if payload.get("share_with_store_all") in (True, "true"):
        return "store_all"
    return "default"


# ── `_issue_visibility_scope` 의 SQL 판(版) ──────────────────────────
# 파이썬 판정과 SQL 필터가 **같은 우선순위**를 써야 한다. 어긋나면 목록/단건이 갈린다.


def _scope_text():
    """payload->>'visibility_scope'. 키 없음/JSON null 이면 SQL NULL."""
    return Report.payload["visibility_scope"].astext


def _scope_is_unset():
    """visibility_scope 가 없거나 우리가 모르는 값 — 이때만 legacy 키를 본다."""
    scope = _scope_text()
    return or_(scope.is_(None), scope.notin_(ISSUE_VISIBILITY_SCOPES))


def _legacy_store_all():
    """legacy share_with_store_all=true (JSON boolean 도, 문자열 "true" 도 인정)."""
    return Report.payload["share_with_store_all"].astext == "true"


def _report_visibility_clause(user: User):
    """리포트 조회 가시성 조건 (모든 타입 공통).

    두 축이 **동시에** 만족해야 남의 리포트가 열린다.

    1. 직급 — 작성자가 나보다 **아래 직급**이어야 한다. 동급·상급은 manager 여도 못 본다.
    2. 매장 — 그 리포트의 매장에서 내가 **manager(user_stores.is_manager)** 여야 한다.
       배정만 된 매장(같이 일하는 매장)은 근거가 안 된다.

    - 내가 쓴 것은 매장·직급과 무관하게 항상 보인다.
    - Owner+ (priority <= OWNER_PRIORITY): 제한 없음 (None)
    - issue 타입은 작성자가 명시적으로 공유한 경우(extra_viewers.user_ids /
      share_with_store_all) 추가로 열어준다 — 명시 공유는 위 두 축보다 우선.
    - **issue 타입 한정**: 그 매장에 배정된 GM 이상은 누가 썼든(동급·상급 포함) 볼 수 있다.
      issue 알림은 그 매장 GM 이상 전원에게 무조건 가므로, 여기서 안 열어주면
      "알림은 왔는데 누르면 403" 이 된다. daily 등 다른 타입은 건드리지 않는다.

    매장 범위 제한(accessible_store_ids)은 호출부에서 별도로 한 번 더 적용된다.
    """
    if is_owner(user):
        return None
    priority = user.role.priority if user.role else 999
    user_str = str(user.id)
    conds = [
        Report.author_id == user.id,
        # 아래 직급 + 그 매장의 manager. author 없는 리포트는 priority NULL → 제외.
        and_(
            _author_priority_subq() > priority,
            _manages_report_store_exists(user),
        ),
        # 명시 공유 (issue 전용 payload 키)
        Report.payload["extra_viewers"]["user_ids"].op("?")(user_str),
        # visibility_scope="managers" — 그 매장 manager 전원 (직급 무관)
        and_(
            _scope_text() == "managers",
            _manages_report_store_exists(user),
        ),
        # visibility_scope="store_all" (+ legacy share_with_store_all) — 그 매장 배정 인원 전원.
        # legacy 키도 같은 EXISTS 로 좁힌다: "매장 전원 공개" 가 타 매장 사람에게까지 열리던
        # 현행은 유출 버그이지 의도된 범위가 아니다.
        #
        # legacy 는 **visibility_scope 가 없/모를 때만** 본다 — `_issue_visibility_scope`
        # 와 같은 우선순위여야 한다. 그냥 OR 로 두면 두 키가 공존하는 row
        # (예: scope="default" + share_with_store_all=true) 에서 목록엔 뜨는데
        # 단건 조회는 403 이 되는, 필터가 아닌 상태가 된다.
        and_(
            or_(
                _scope_text() == "store_all",
                and_(_scope_is_unset(), _legacy_store_all()),
            ),
            _assigned_to_report_store_exists(user),
        ),
    ]
    if is_gm_plus(user):
        # issue 전용 — 알림 대상(그 매장 GM 이상 전원)과 조회권을 일치시킨다.
        conds.append(
            and_(
                Report.type == "issue",
                _assigned_to_report_store_exists(user),
            )
        )
    return or_(*conds)


def can_view_report(
    user: User,
    report: Report,
    author_priority: int | None,
    manages_store: bool,
    assigned_to_store: bool = False,
) -> bool:
    """단건 조회 가시성 — _report_visibility_clause 의 파이썬 판정 버전.

    manages_store: 이 리포트의 매장에서 viewer 가 manager 인가.
    assigned_to_store: 이 리포트의 매장에 viewer 가 배정돼 있는가 (is_manager 무관).
    """
    if is_owner(user):
        return True
    if report.author_id == user.id:
        return True
    priority = user.role.priority if user.role else 999
    if author_priority is not None and author_priority > priority and manages_store:
        return True
    # issue 전용 — 그 매장에 배정된 GM 이상은 작성자 직급과 무관하게 열 수 있다
    # (알림이 무조건 가는 사람들이라 여기서 막으면 '알림 눌렀는데 403' 이 된다).
    if report.type == "issue" and is_gm_plus(user) and assigned_to_store:
        return True
    payload = report.payload or {}
    extra = (payload.get("extra_viewers") or {}).get("user_ids") or []
    if str(user.id) in [str(u) for u in extra]:
        return True
    scope = _issue_visibility_scope(payload)
    if scope == "managers" and manages_store:
        return True
    if scope == "store_all" and assigned_to_store:
        return True
    return False


async def _author_priority_of(db: AsyncSession, report: Report) -> int:
    """리포트 작성자의 role priority. 작성자가 없으면 STAFF 취급(가장 하위).

    알림 수신자를 가시성 규칙과 맞추는 데 쓴다 — 열 수 없는 사람에게 알림을 보내면
    받는 쪽에는 403 로 끝나는 알림만 남는다.
    """
    if not report.author_id:
        return STAFF_PRIORITY
    res = await db.execute(
        select(Role.priority)
        .select_from(User)
        .join(Role, Role.id == User.role_id)
        .where(User.id == report.author_id)
    )
    return res.scalar() or STAFF_PRIORITY


def _payload_user_ids(values: Any) -> set[UUID]:
    """payload 안의 user id 문자열 리스트 → UUID 집합 (파싱 실패는 무시)."""
    out: set[UUID] = set()
    for uid in values or []:
        try:
            out.add(UUID(str(uid)))
        except (ValueError, TypeError):
            continue
    return out


async def _resolve_issue_auto_recipients(
    db: AsyncSession,
    *,
    store_id: UUID | None,
) -> list[dict[str, Any]]:
    """자동 수신자 — 그 매장에 배정된 **GM 이상 전원** (활성 사용자).

    작성자 직급과 비교하지 않는다. 작성자 본인이 GM 이상이면 본인도 포함된다
    (내가 올린 이슈도 내 매장 이슈이므로 GM 목록에서 빠질 이유가 없다).
    is_manager 도 보지 않는다 — 배정(user_stores)만 있으면 된다.

    이 집합은 **해제 불가**다. payload.notify_excluded_user_ids 는 더 이상 반영하지 않는다.

    반환 항목: {user_id, full_name, role_label, role_priority}. 정렬은 호출부에서.
    """
    if not store_id:
        return []
    q = (
        select(User.id, User.full_name, Role.name, Role.priority)
        .join(Role, Role.id == User.role_id)
        .join(UserStore, UserStore.user_id == User.id)
        .where(
            UserStore.store_id == store_id,
            Role.priority <= GM_PRIORITY,
            User.is_active.is_(True),
            User.deleted_at.is_(None),
        )
    )
    res = await db.execute(q)
    seen: set[UUID] = set()
    items: list[dict[str, Any]] = []
    for uid, full_name, role_name, priority in res.all():
        if uid in seen:
            continue
        seen.add(uid)
        items.append({
            "user_id": uid,
            "full_name": full_name or "",
            "role_label": role_name or "",
            "role_priority": priority if priority is not None else STAFF_PRIORITY,
        })
    return items


async def _resolve_issue_viewers(
    db: AsyncSession, report: Report
) -> set[UUID]:
    """이슈 리포트의 **조회권자** user_id 집합.

    알림 대상이 아니다. 알림은 _resolve_issue_notify_recipients 하나만 쓴다 —
    볼 수 있다는 것과 통지받는다는 것은 별개다. (이걸 alert fan-out 에 쓰면
    범위를 store_all 로 넓히는 순간 매장 전원에게 알림이 쏟아진다)

    - 작성자
    - 그 매장에 배정된 GM 이상 전원 (직급 비교 없음 — 알림 대상과 같은 집합)
    - 그 매장의 manager(user_stores.is_manager) 중 작성자보다 상위 직급인 사람
    - payload.extra_viewers.user_ids
    - visibility_scope="managers" 면 그 매장 manager 전원 (직급 무관)
    - visibility_scope="store_all"(legacy share_with_store_all 포함) 이면 그 매장 배정 인원 전원
    - payload.extra_viewers.position_ids 는 향후 (position-user 매핑 도입 후)
    """
    viewers: set[UUID] = set()
    if report.author_id:
        viewers.add(report.author_id)

    payload = report.payload or {}
    scope = _issue_visibility_scope(payload)

    if report.store_id:
        # (1) 그 매장 GM 이상 전원 — _report_visibility_clause / can_view_report 의
        #     issue 전용 조건과 같은 집합이어야 한다. 세 곳이 어긋나면
        #     "목록엔 있는데 열면 403" 또는 "알림은 갔는데 못 여는" 상태가 된다.
        auto = await _resolve_issue_auto_recipients(db, store_id=report.store_id)
        viewers.update(item["user_id"] for item in auto)

        # (2) 그 매장 manager 중 작성자보다 상위 직급 (기존 default 규칙 — 유지)
        author_priority = await _author_priority_of(db, report)
        res = await db.execute(
            select(User.id)
            .join(Role, Role.id == User.role_id)
            .join(UserStore, UserStore.user_id == User.id)
            .where(
                UserStore.store_id == report.store_id,
                UserStore.is_manager.is_(True),
                Role.priority < author_priority,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        viewers.update(row[0] for row in res)

    viewers.update(_payload_user_ids((payload.get("extra_viewers") or {}).get("user_ids")))

    if report.store_id and scope in ("managers", "store_all"):
        q = (
            select(User.id)
            .join(UserStore, UserStore.user_id == User.id)
            .where(
                UserStore.store_id == report.store_id,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        if scope == "managers":
            q = q.where(UserStore.is_manager.is_(True))
        res = await db.execute(q)
        viewers.update(row[0] for row in res)
    return viewers


async def _resolve_issue_notify_recipients(
    db: AsyncSession, report: Report, *, event: str | None = None
) -> set[UUID]:
    """이 리포트의 실제 알림 수신자 = (그 매장 GM 이상 전원) ∪ (콕 집어 추가한 사람) ∪ (작성자).

    - 자동 수신자(GM+)는 **해제 불가**. payload.notify_excluded_user_ids 는 하위호환으로
      받기만 하고 무시한다 (구버전 클라가 보낸 제외 목록이 GM 알림을 끄면 안 된다).
    - 작성자 본인도 GM 이상이면 포함된다.
    - **작성자는 GM 미만이어도 자기 리포트에 달린 후속(댓글/상태 변경) 알림을 받는다.**
      GM+ 집합만 쓰면 이슈를 올린 staff 가 "닫혔다/답이 달렸다"를 영영 못 듣는다
      (dev 에서는 조회권자 알림으로 받고 있던 동작이라, 안 넣으면 회귀다).
      event="created" 는 작성자 = 액터이므로 제외한다 — 자기가 방금 쓴 걸 통지받을 이유가 없다.
      작성자는 항상 조회권이 있으므로 "알림은 왔는데 못 여는" 불일치는 생기지 않는다.
    - 조회 범위(visibility_scope)는 이 집합에 영향을 주지 않는다 — 범위 확대는
      "열어볼 수 있게" 이지 "통지" 가 아니다.
    - **비활성/삭제된 사용자에게는 보내지 않는다.** extra_viewers 는 payload 에 박힌
      과거 id 라, 필터 없이 쓰면 퇴사자에게 이슈 메일이 계속 나간다
      (dev 에서 메일 대상이던 _resolve_issue_managers 는 active 를 걸고 있었다).

    스냅샷을 저장하지 않고 매번 재계산한다 — 나중에 부임한 GM 도 자동으로 받는다.
    """
    payload = report.payload or {}
    auto = await _resolve_issue_auto_recipients(db, store_id=report.store_id)
    recipients = {item["user_id"] for item in auto}

    # payload/컬럼에서 온 id 는 활성 여부를 보장하지 않으므로 한 번 걸러서 합친다.
    unverified = _payload_user_ids((payload.get("extra_viewers") or {}).get("user_ids"))
    if event != "created" and report.author_id:
        unverified.add(report.author_id)
    unverified -= recipients  # auto 는 이미 active 필터를 통과했다
    if unverified:
        rows = await db.execute(
            select(User.id).where(
                User.id.in_(unverified),
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        recipients |= {row[0] for row in rows.all()}
    return recipients


async def _resolve_issue_managers(
    db: AsyncSession, report: Report
) -> set[UUID]:
    """이슈 리포트의 자동 수신자 user_id 집합 (추가 지목 인원 반영 전).

    그 매장에 배정된 GM 이상 전원.
    """
    if not report.store_id:
        return set()
    auto = await _resolve_issue_auto_recipients(db, store_id=report.store_id)
    return {item["user_id"] for item in auto}


_EMAIL_EXCERPT_LIMIT = 600


def _email_excerpt(text: str | None) -> str | None:
    """알림 메일 인용 블록에 넣을 본문. 너무 길면 자르고 말줄임을 붙인다.

    예전 160자는 '어떤 이슈인지 알 수 없다'는 문제의 일부였다 — 템플릿 프리셋
    (Platform:/Rating:/... 같은 여러 줄)은 앞부분이 라벨뿐이라 내용이 안 보인다.
    """
    if not text or not text.strip():
        return None
    t = text.strip()
    return t if len(t) <= _EMAIL_EXCERPT_LIMIT else t[:_EMAIL_EXCERPT_LIMIT].rstrip() + "…"


def _apply_section_updates(report: Report, updates: list) -> None:
    """report.payload.sections의 content를 sort_order 매핑으로 업데이트.

    JSONB는 in-place mutation을 SQLAlchemy가 자동 감지하지 못하므로
    flag_modified 호출 필요.
    """
    if not isinstance(report.payload, dict):
        return
    sections = list(report.payload.get("sections") or [])
    by_sort = {u.sort_order: u.content for u in updates}
    for s in sections:
        so = s.get("sort_order")
        if so in by_sort:
            s["content"] = by_sort[so]
    report.payload = {**report.payload, "sections": sections}
    flag_modified(report, "payload")


class ReportService:

    # ── Template CRUD ──────────────────────────────────────

    async def list_templates(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        type: str | None = None,
        store_id: UUID | None = None,
        is_active: bool | None = None,
    ) -> list[ReportTemplate]:
        return await report_template_repository.list_for_org(
            db, type=type, organization_id=organization_id,
            store_id=store_id, is_active=is_active,
        )

    async def get_template_detail(
        self, db: AsyncSession, template_id: UUID, organization_id: UUID
    ) -> ReportTemplate:
        t = await report_template_repository.get_by_id(db, template_id)
        if not t or (t.organization_id and t.organization_id != organization_id):
            raise NotFoundError("Template not found")
        return t

    async def create_template(
        self, db: AsyncSession, organization_id: UUID, data: ReportTemplateCreate
    ) -> ReportTemplate:
        try:
            t = ReportTemplate(
                type=data.type,
                organization_id=organization_id,
                store_id=UUID(data.store_id) if data.store_id else None,
                name=data.name,
                is_default=data.is_default,
                is_active=True,
                applicable_types=data.applicable_types,
                payload=data.payload or {},
            )
            db.add(t)
            await db.flush()
            await db.refresh(t)
            await db.commit()
            return t
        except Exception:
            await db.rollback()
            raise

    async def update_template(
        self,
        db: AsyncSession,
        template_id: UUID,
        organization_id: UUID,
        data: ReportTemplateUpdate,
    ) -> ReportTemplate:
        t = await self.get_template_detail(db, template_id, organization_id)
        try:
            if data.name is not None:
                t.name = data.name
            if data.is_default is not None:
                t.is_default = data.is_default
            if data.is_active is not None:
                t.is_active = data.is_active
            if data.applicable_types is not None:
                # [] 도 의미 있음(전체 적용). 명시 전달 시 그대로 저장.
                t.applicable_types = data.applicable_types
            if data.payload is not None:
                t.payload = data.payload
            await db.flush()
            await db.refresh(t)
            await db.commit()
            return t
        except Exception:
            await db.rollback()
            raise

    async def delete_template(
        self, db: AsyncSession, template_id: UUID, organization_id: UUID
    ) -> None:
        t = await self.get_template_detail(db, template_id, organization_id)
        try:
            await db.delete(t)
            await db.flush()
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def get_template_for_use(
        self,
        db: AsyncSession,
        *,
        type: str,
        organization_id: UUID,
        store_id: UUID | None = None,
    ) -> ReportTemplate:
        t = await report_template_repository.get_template_for_store(
            db, type=type, organization_id=organization_id, store_id=store_id,
        )
        if not t:
            raise NotFoundError(f"No available {type} report template")
        return t

    # ── Report Types (daily period 구성) ───────────────────

    async def resolve_effective_types(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        store_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        """한 매장에 실제 적용되는 report_type 목록(resolved).

        규칙(결정-1/7/9):
          - org-default(store_id IS NULL) 행이 base.
          - store 행이 같은 code 의 org 행을 override(label/active/deadline/sort).
          - store 전용 code 는 추가.
          - org 에 행이 하나도 없으면 내장 기본값(DEFAULT_REPORT_TYPE_DEFS) 사용.
        반환: sort_order 정렬된 dict 목록 (모든 타입; is_active 포함).
        """
        org_rows = await report_type_repository.list_org_defaults(db, organization_id)

        merged: dict[str, dict[str, Any]] = {}
        if org_rows:
            for row in org_rows:
                merged[row.code] = self._type_row_to_effective(row, scope="org")
        else:
            for d in DEFAULT_REPORT_TYPE_DEFS:
                merged[d["code"]] = {
                    "code": d["code"],
                    "label": d["label"],
                    "sort_order": d["sort_order"],
                    "is_active": d["is_active"],
                    "default_deadline_local_time": None,
                    "deadline_day_offset": 0,
                    "scope": "org",
                    "id": None,
                    "org_type_id": None,
                }

        if store_id is not None:
            store_rows = await report_type_repository.list_store_rows(
                db, organization_id, store_id
            )
            for row in store_rows:
                base = merged.get(row.code)
                eff = self._type_row_to_effective(row, scope="store")
                # org row 의 id 를 org_type_id 로 보존 (override 관계 추적용)
                if base and base.get("scope") == "org":
                    eff["org_type_id"] = base.get("id")
                merged[row.code] = eff

        return sorted(merged.values(), key=lambda e: (e["sort_order"], e["label"]))

    @staticmethod
    def _type_row_to_effective(row: ReportType, scope: str) -> dict[str, Any]:
        return {
            "code": row.code,
            "label": row.label,
            "sort_order": row.sort_order,
            "is_active": row.is_active,
            "default_deadline_local_time": row.default_deadline_local_time,
            "deadline_day_offset": row.deadline_day_offset,
            "scope": scope,
            "id": str(row.id),
            "org_type_id": None,
        }

    @staticmethod
    def _compute_deadline_at(
        *,
        db_tz: str,
        report_date: date,
        report_type: dict[str, Any],
    ) -> datetime | None:
        """report_type 의 deadline 규칙으로 마감 UTC datetime 계산 (store-tz 철칙).

        default_deadline_local_time 가 없으면 None(마감 없음).
        base = report_date + deadline_day_offset 일, local HH:MM (store tz) → UTC.
        """
        hhmm = report_type.get("default_deadline_local_time")
        if not hhmm:
            return None
        try:
            h, m = hhmm.split(":")
            local_time = time(int(h), int(m))
        except (ValueError, AttributeError):
            return None
        offset = report_type.get("deadline_day_offset") or 0
        base_date = report_date + timedelta(days=offset)
        tz = ZoneInfo(db_tz)
        local_dt = datetime.combine(base_date, local_time, tzinfo=tz)
        return local_dt.astimezone(timezone.utc)

    async def list_report_types(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        store_id: UUID | None = None,
        effective: bool = False,
    ) -> list[dict[str, Any]]:
        """report_types 목록.

        effective=True → store 에 실제 적용되는 resolved 목록(EffectiveReportType).
        effective=False → 해당 scope 의 raw 관리 목록(ReportType).
        """
        if effective:
            return await self.resolve_effective_types(
                db, organization_id=organization_id, store_id=store_id
            )
        rows = await report_type_repository.list_for_scope(
            db, organization_id, store_id
        )
        return [self.build_report_type_response(r) for r in rows]

    async def create_report_type(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        data: ReportTypeCreate,
    ) -> ReportType:
        store_id = UUID(data.store_id) if data.store_id else None
        if store_id is not None:
            await self._assert_store_in_org(db, organization_id, store_id)
        # 살아있는 row 와 code 충돌 방지(부분 unique index 와 일치).
        dup = await report_type_repository.find_live_by_code(
            db, organization_id, store_id, data.code
        )
        if dup:
            raise ConflictError(
                f"A report type with code '{data.code}' already exists in this scope. "
                "Use a different code or edit the existing one.",
                existing_id=str(dup.id),
            )
        try:
            rt = ReportType(
                organization_id=organization_id,
                store_id=store_id,
                code=data.code,
                label=data.label,
                sort_order=data.sort_order,
                is_active=data.is_active,
                default_deadline_local_time=data.default_deadline_local_time,
                deadline_day_offset=data.deadline_day_offset,
            )
            db.add(rt)
            await db.flush()
            await db.refresh(rt)
            await db.commit()
            return rt
        except Exception:
            await db.rollback()
            raise

    async def update_report_type(
        self,
        db: AsyncSession,
        *,
        type_id: UUID,
        organization_id: UUID,
        data: ReportTypeUpdate,
    ) -> ReportType:
        rt = await report_type_repository.get_by_id(db, type_id, organization_id)
        if not rt:
            raise NotFoundError("Report type not found")
        try:
            if data.label is not None:
                rt.label = data.label
            if data.sort_order is not None:
                rt.sort_order = data.sort_order
            if data.is_active is not None:
                rt.is_active = data.is_active
            if data.default_deadline_local_time is not None:
                rt.default_deadline_local_time = data.default_deadline_local_time or None
            if data.deadline_day_offset is not None:
                rt.deadline_day_offset = data.deadline_day_offset
            await db.flush()
            await db.refresh(rt)
            await db.commit()
            return rt
        except Exception:
            await db.rollback()
            raise

    async def delete_report_type(
        self,
        db: AsyncSession,
        *,
        type_id: UUID,
        organization_id: UUID,
    ) -> None:
        rt = await report_type_repository.get_by_id(db, type_id, organization_id)
        if not rt:
            raise NotFoundError("Report type not found")
        try:
            rt.is_deleted = True
            rt.deleted_at = datetime.now(timezone.utc)
            await db.flush()
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def reorder_report_types(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        items: list[tuple[UUID, int]],
    ) -> None:
        try:
            for type_id, sort_order in items:
                rt = await report_type_repository.get_by_id(db, type_id, organization_id)
                if rt:
                    rt.sort_order = sort_order
            await db.flush()
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def _assert_store_in_org(
        self, db: AsyncSession, organization_id: UUID, store_id: UUID
    ) -> None:
        res = await db.execute(
            select(Store.id).where(
                Store.id == store_id, Store.organization_id == organization_id
            )
        )
        if res.scalar_one_or_none() is None:
            raise NotFoundError("Store not found in this organization")

    def build_report_type_response(self, rt: ReportType) -> dict:
        return {
            "id": str(rt.id),
            "organization_id": str(rt.organization_id),
            "store_id": str(rt.store_id) if rt.store_id else None,
            "code": rt.code,
            "label": rt.label,
            "sort_order": rt.sort_order,
            "is_active": rt.is_active,
            "default_deadline_local_time": rt.default_deadline_local_time,
            "deadline_day_offset": rt.deadline_day_offset,
            "created_at": rt.created_at,
            "updated_at": rt.updated_at,
        }

    # ── Report CRUD ────────────────────────────────────────

    async def list_reports(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        type: str | None = None,
        store_id: UUID | None = None,
        author_id: UUID | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        period: str | None = None,
        status: str | None = None,
        exclude_draft: bool = True,
        page: int = 1,
        per_page: int = 20,
        accessible_store_ids: list[UUID] | None = None,
        viewer: User | None = None,
        show_all: bool = False,  # deprecated — 무시. 라우터 하위호환용으로만 남아 있다.
    ):
        payload_filters: dict | None = None
        if period:
            payload_filters = {"period": period}
        exclude_status = "draft" if (status is None and exclude_draft) else None

        # 가시성: 모든 타입에 직급 기반 필터 적용 (자기 것 + 하위 직급 작성분).
        # show_all 은 더 이상 이 필터를 우회하지 못한다 — 우회 가능하면 필터가 아니다.
        extra_clause = None
        if viewer is not None:
            extra_clause = _report_visibility_clause(viewer)

        return await report_repository.get_by_org(
            db, organization_id,
            type=type, store_id=store_id, author_id=author_id,
            date_from=date_from, date_to=date_to,
            status=status, exclude_status=exclude_status,
            payload_filters=payload_filters,
            extra_clause=extra_clause,
            page=page, per_page=per_page,
            accessible_store_ids=accessible_store_ids,
        )

    async def get_report(
        self, db: AsyncSession, report_id: UUID, organization_id: UUID
    ) -> Report:
        r = await report_repository.get_with_details(db, report_id, organization_id)
        if not r:
            raise NotFoundError("Report not found")
        return r

    async def assert_can_view(
        self, db: AsyncSession, viewer: User, report: Report
    ) -> None:
        """단건 조회 가시성 검사 — list 필터와 같은 규칙.

        (자기 것) 또는 (아래 직급 작성 + 그 매장의 manager) 또는 (issue 명시 공유).
        list 에서 안 보이는 리포트를 id 직접 호출로 열 수 있으면 필터가 아니므로,
        상세/리뷰/확인/댓글 등 '내용을 보는' 경로는 모두 이 검사를 거친다.
        """
        author_priority: int | None = None
        if report.author_id:
            res = await db.execute(
                select(Role.priority)
                .select_from(User)
                .join(Role, Role.id == User.role_id)
                .where(User.id == report.author_id)
            )
            author_priority = res.scalar()
        manages_store = False
        assigned_to_store = False
        if report.store_id:
            mres = await db.execute(
                select(UserStore.is_manager).where(
                    UserStore.user_id == viewer.id,
                    UserStore.store_id == report.store_id,
                )
            )
            rows = mres.all()
            assigned_to_store = bool(rows)
            manages_store = any(bool(row[0]) for row in rows)
        if not can_view_report(
            viewer, report, author_priority, manages_store, assigned_to_store
        ):
            raise REPORT_NOT_VISIBLE()

    async def list_issue_recipients(
        self,
        db: AsyncSession,
        *,
        viewer: User,
        store_id: UUID,
        report: Report | None = None,
    ) -> list[dict[str, Any]]:
        """이슈 알림 수신자 목록. 작성 화면과 상세 화면이 같이 쓴다.

        - 자동 수신자(source="auto") = 그 매장 GM 이상 전원. **항상 수신, 해제 불가**
          (is_recipient=True, can_remove=False).
        - 추가 지목(source="added") = payload.extra_viewers.user_ids. 제거 가능.

        스냅샷이 아니라 매번 재계산이므로, 부임/퇴사가 즉시 반영된다.
        """
        payload = (report.payload or {}) if report is not None else {}

        auto = await _resolve_issue_auto_recipients(db, store_id=store_id)
        added = _payload_user_ids((payload.get("extra_viewers") or {}).get("user_ids"))

        items: list[dict[str, Any]] = []
        auto_ids = set()
        for item in auto:
            auto_ids.add(item["user_id"])
            items.append({
                "user_id": str(item["user_id"]),
                "full_name": item["full_name"],
                "role_label": item["role_label"],
                "role_priority": item["role_priority"],
                "source": "auto",
                "is_recipient": True,
                "can_remove": False,
            })

        added_only = [uid for uid in added if uid not in auto_ids]
        if added_only:
            rows = await db.execute(
                select(User.id, User.full_name, Role.name, Role.priority, User.is_active)
                .join(Role, Role.id == User.role_id, isouter=True)
                .where(
                    User.id.in_(added_only),
                    User.organization_id == viewer.organization_id,
                    User.deleted_at.is_(None),
                )
            )
            for uid, full_name, role_name, priority, is_active in rows.all():
                items.append({
                    "user_id": str(uid),
                    "full_name": full_name or "",
                    "role_label": role_name or "",
                    "role_priority": priority if priority is not None else STAFF_PRIORITY,
                    "source": "added",
                    # 비활성 사용자에게는 실제로 발송하지 않는다
                    # (_resolve_issue_notify_recipients 와 같은 판정). 목록에서 숨기지는
                    # 않는다 — 지목이 payload 에 남아 있는 걸 작성자가 보고 지울 수 있어야 한다.
                    "is_recipient": bool(is_active),
                    "can_remove": True,
                })

        items.sort(key=lambda i: (i["role_priority"], i["full_name"]))
        return items

    async def list_issue_expected_viewers(
        self,
        db: AsyncSession,
        *,
        viewer: User,
        store_id: UUID,
        scope: str,
        report: Report | None = None,
        extra_user_ids: list[UUID] | None = None,
    ) -> dict[str, Any]:
        """선택한 조회 범위에서 **실제로 누가 보게 되는지** 미리보기.

        작성 화면에서 범위를 고를 때 쓴다. 범위 문구만으로는 누가 보는지 알 수 없어
        작성자가 "이 사람도 보나?" 를 못 판단한다.

        - store_all 은 인원이 많아 목록을 만들지 않는다 (mode="summary", 개수만).
        - default / managers 는 사람 목록 (mode="list").
        - extra_user_ids 를 주면 아직 저장 전인 추가 지목까지 반영해서 계산한다.
          (안 주면 report.payload.extra_viewers.user_ids 를 쓴다)

        판정은 _report_visibility_clause / can_view_report 와 같은 규칙이어야 한다.
        """
        if scope not in ISSUE_VISIBILITY_SCOPES:
            raise ISSUE_VISIBILITY_SCOPE_INVALID(allowed=list(ISSUE_VISIBILITY_SCOPES))

        payload = (report.payload or {}) if report is not None else {}
        if extra_user_ids is None:
            extras = _payload_user_ids((payload.get("extra_viewers") or {}).get("user_ids"))
        else:
            extras = set(extra_user_ids)

        author_id = report.author_id if report is not None else viewer.id
        if report is not None:
            author_priority = await _author_priority_of(db, report)
        else:
            author_priority = (
                viewer.role.priority
                if viewer.role and viewer.role.priority is not None
                else STAFF_PRIORITY
            )

        # 매장 배정 인원 전원 (GM+ / manager 판정을 여기서 한 번에 한다)
        rows = (
            await db.execute(
                select(
                    User.id, User.full_name, Role.name, Role.priority, UserStore.is_manager
                )
                .join(Role, Role.id == User.role_id, isouter=True)
                .join(UserStore, UserStore.user_id == User.id)
                .where(
                    UserStore.store_id == store_id,
                    User.is_active.is_(True),
                    User.deleted_at.is_(None),
                )
            )
        ).all()

        assigned: dict[UUID, dict[str, Any]] = {}
        for uid, full_name, role_name, priority, is_manager in rows:
            cur = assigned.setdefault(uid, {
                "user_id": str(uid),
                "full_name": full_name or "",
                "role_label": role_name or "",
                "role_priority": priority if priority is not None else STAFF_PRIORITY,
                "is_manager": False,
            })
            cur["is_manager"] = cur["is_manager"] or bool(is_manager)

        # 알림 대상 = _resolve_issue_notify_recipients 와 같은 집합이어야 미리보기가
        # 거짓말을 하지 않는다: 그 매장 GM+ ∪ 지목 ∪ 작성자(후속 알림).
        notified = {
            uid for uid, info in assigned.items()
            if info["role_priority"] <= GM_PRIORITY
        } | extras
        if author_id:
            notified.add(author_id)

        if scope == "store_all":
            count = len(set(assigned) | extras | ({author_id} if author_id else set()))
            return {
                "store_id": str(store_id),
                "report_id": str(report.id) if report is not None else None,
                "scope": scope,
                "mode": "summary",
                "summary": {
                    "label": "Everyone assigned to this store",
                    "count": count,
                },
                "items": [],
            }

        items: list[dict[str, Any]] = []
        seen: set[UUID] = set()

        def _add(uid: UUID, info: dict[str, Any], reason: str, reason_label: str) -> None:
            if uid in seen:
                return
            seen.add(uid)
            items.append({
                "user_id": str(uid),
                "full_name": info["full_name"],
                "role_label": info["role_label"],
                "role_priority": info["role_priority"],
                "reason": reason,
                "reason_label": reason_label,
                "is_notified": uid in notified,
            })

        for uid, info in assigned.items():
            if uid == author_id:
                _add(uid, info, "author", "Author")
            elif info["role_priority"] <= GM_PRIORITY:
                _add(uid, info, "gm_or_above", "GM or above at this store")
            elif info["is_manager"] and (
                scope == "managers" or info["role_priority"] < author_priority
            ):
                _add(uid, info, "store_manager", "Manager of this store")

        # 매장에 배정돼 있지 않은 작성자(권한 이관/타 매장 작성 등)도 자기 글은 본다
        missing = [uid for uid in ([author_id] if author_id else []) if uid not in seen]
        missing += [uid for uid in extras if uid not in seen]
        if missing:
            extra_rows = (
                await db.execute(
                    select(User.id, User.full_name, Role.name, Role.priority)
                    .join(Role, Role.id == User.role_id, isouter=True)
                    .where(
                        User.id.in_(missing),
                        User.organization_id == viewer.organization_id,
                        User.deleted_at.is_(None),
                    )
                )
            ).all()
            for uid, full_name, role_name, priority in extra_rows:
                info = {
                    "full_name": full_name or "",
                    "role_label": role_name or "",
                    "role_priority": priority if priority is not None else STAFF_PRIORITY,
                }
                if uid == author_id:
                    _add(uid, info, "author", "Author")
                else:
                    _add(uid, info, "added", "Added by the author")

        items.sort(key=lambda i: (i["role_priority"], i["full_name"]))
        label = (
            "Author, managers above the author, and GM or above at this store"
            if scope == "default"
            else "All managers at this store, plus GM or above"
        )
        return {
            "store_id": str(store_id),
            "report_id": str(report.id) if report is not None else None,
            "scope": scope,
            "mode": "list",
            "summary": {"label": label, "count": len(items)},
            "items": items,
        }

    async def create_report(
        self,
        db: AsyncSession,
        organization_id: UUID,
        author_id: UUID,
        data: ReportCreate,
    ) -> Report:
        store_id = UUID(data.store_id)

        # type별 분기
        deadline_at: datetime | None = None
        if data.type == "daily":
            if not data.report_date:
                raise BadRequestError("report_date is required for daily reports")
            report_date = date.fromisoformat(data.report_date)
            period = (data.payload or {}).get("period")
            if not period:
                raise BadRequestError(
                    "payload.period is required — pick an enabled report type for this store"
                )

            # period 는 매장에 enabled 된 report_type code 중 하나여야 한다(결정-1/7/9).
            effective = await self.resolve_effective_types(
                db, organization_id=organization_id, store_id=store_id
            )
            enabled = {e["code"]: e for e in effective if e["is_active"]}
            if period not in enabled:
                allowed = sorted(enabled.keys())
                raise BadRequestError(
                    f"payload.period '{period}' is not an enabled report type for this store. "
                    f"Allowed: {allowed}. Enable it in report type settings first."
                )

            # per-person 중복 체크 (결정-8): 같은 작성자의 같은 slot 만 차단.
            existing = await report_repository.find_daily_duplicate(
                db, store_id, report_date, period, author_id=author_id
            )
            if existing:
                raise HTTPException(status_code=409, detail={
                    "message": "You already have a report for this store/date/period",
                    "existing_report_id": str(existing.id),
                    "status": existing.status,
                })

            # 템플릿 결정 (applicable_types 가 이 period 를 포함하는 템플릿 우선; 결정-9)
            template_id = UUID(data.template_id) if data.template_id else None
            if template_id:
                template = await report_template_repository.get_by_id(db, template_id)
                if not template or template.type != "daily":
                    raise NotFoundError("Template not found")
            else:
                template = await report_template_repository.get_template_for_store(
                    db, type="daily", organization_id=organization_id,
                    store_id=store_id, type_code=period,
                )
            if not template:
                raise NotFoundError("No available daily report template")

            payload = _build_daily_payload_from_template(template, period)
            title = None
            # 마감 일시(P2): report_type 규칙으로부터 계산 (store-tz 기준).
            deadline_at = self._compute_deadline_at(
                db_tz=await get_store_timezone(db, store_id),
                report_date=report_date,
                report_type=enabled[period],
            )
        elif data.type == "issue":
            # 이슈 리포트: store template에서 카테고리/커스텀 필드 동적 로딩.
            from app.schemas.report import (
                DEFAULT_ISSUE_CATEGORIES,
                ISSUE_SEVERITIES,
            )
            from app.services.storage_service import storage_service
            raw_payload = dict(data.payload or {})
            category = raw_payload.get("category")
            severity = raw_payload.get("severity")
            if severity not in ISSUE_SEVERITIES:
                raise BadRequestError(
                    f"payload.severity must be one of {ISSUE_SEVERITIES}"
                )
            if not data.title:
                raise BadRequestError("title is required for issue reports")

            # 매장 issue template lookup (store → org → system default)
            template = await report_template_repository.get_template_for_store(
                db, type="issue", organization_id=organization_id, store_id=store_id,
            )
            if template:
                tpl = template.payload or {}
                tpl_categories = tpl.get("categories") or []
                allowed_codes = {
                    c.get("code") for c in tpl_categories if c.get("is_active", True)
                }
                # 카테고리 정의가 비어있으면 시스템 기본 6개로 fallback
                if not allowed_codes:
                    allowed_codes = set(DEFAULT_ISSUE_CATEGORIES)
            else:
                tpl = {}
                allowed_codes = set(DEFAULT_ISSUE_CATEGORIES)

            if category not in allowed_codes:
                raise BadRequestError(
                    f"payload.category must be one of {sorted(allowed_codes)}"
                )

            # 표시 대상 = 전역 custom_fields + 이 카테고리의 fields (field_order 순)
            active_fields = resolve_issue_fields(tpl, category)
            # 미응답은 null 로 명시 기록된다 — "안 물어봄"(키 없음)과 구분하기 위해.
            cfv = validate_and_normalize_values(
                active_fields, raw_payload.get("custom_field_values")
            )
            # 그때 물어본 정의를 리포트에 박아둔다. 템플릿이 바뀌어도 과거 리포트가
            # 해석 가능해야 한다. 클라가 보낸 snapshot 은 신뢰하지 않고 서버가 만든다.
            fields_snapshot = build_fields_snapshot(active_fields)

            # attachments key 정규화 (temp → 최종). 멱등.
            attachments = raw_payload.get("attachments") or []
            finalized: list[dict] = []
            for a in attachments:
                if not isinstance(a, dict):
                    continue
                key_or_url = a.get("key") or a.get("url")
                if not key_or_url:
                    continue
                try:
                    final_key = storage_service.put_finalized(key_or_url)
                except Exception:
                    final_key = key_or_url
                finalized.append({**a, "key": final_key})
            raw_payload["attachments"] = finalized

            # links 검증: 모든 ID들이 매장/조직에 속해야 함
            await _validate_issue_links(
                db, organization_id, store_id, raw_payload.get("links")
            )
            # 공유 범위 + 수신자 지정 검증 (조회권을 여는 키라 조직 검증 필수)
            raw_payload = await _validate_issue_sharing(db, organization_id, raw_payload)

            # issue 는 report_date 가 명시 안 됐으면 today 로 자동 set
            # (date range 필터에서 매칭되도록).
            report_date = (
                date.fromisoformat(data.report_date)
                if data.report_date
                else date.today()
            )
            raw_payload["custom_field_values"] = cfv
            raw_payload["fields_snapshot"] = fields_snapshot
            payload = raw_payload
            title = data.title
        else:
            template = None
            if data.template_id:
                template = await report_template_repository.get_by_id(db, UUID(data.template_id))
            report_date = date.fromisoformat(data.report_date) if data.report_date else None
            payload = data.payload or {}
            title = data.title

        # 타입별 초기 status
        initial_status = "open" if data.type == "issue" else "draft"

        try:
            r = Report(
                type=data.type,
                organization_id=organization_id,
                store_id=store_id,
                template_id=template.id if template else None,
                author_id=author_id,
                title=title,
                status=initial_status,
                report_date=report_date,
                deadline_at=deadline_at,
                payload=payload,
            )
            db.add(r)
            await db.flush()
            await db.refresh(r)
            await db.commit()
            # 이슈는 생성 즉시 조회권자 전원에게 알림
            if r.type == "issue":
                await self._notify_issue_event(db, report=r, event="created", actor_id=author_id)
            return r
        except Exception:
            await db.rollback()
            raise

    async def update_report(
        self,
        db: AsyncSession,
        report_id: UUID,
        organization_id: UUID,
        author_id: UUID,
        data: ReportUpdate,
        is_manager: bool = False,
    ) -> Report:
        """리포트 본문 수정.

        - daily: 작성자만, draft 상태에서만.
        - issue: 작성자 OR 매니저(GM+) 가능, closed 상태는 거부.
        """
        r = await self.get_report(db, report_id, organization_id)
        if r.type == "daily":
            if r.author_id != author_id:
                raise ForbiddenError("Only the author can update this report")
            if r.status != "draft":
                raise BadRequestError("Only draft daily reports can be updated")
        elif r.type == "issue":
            if r.author_id != author_id and not is_manager:
                raise ForbiddenError("Only the author or a manager can update this report")
            if r.status == "closed":
                raise BadRequestError("Closed issue reports cannot be updated")
        else:
            if r.author_id != author_id:
                raise ForbiddenError("Only the author can update this report")
        try:
            if data.title is not None:
                r.title = data.title
            if data.payload is not None:
                # issue 타입은 links 검증
                if r.type == "issue":
                    await _validate_issue_links(
                        db,
                        organization_id,
                        r.store_id,
                        data.payload.get("links") if isinstance(data.payload, dict) else None,
                    )
                    if isinstance(data.payload, dict):
                        data.payload = await _validate_issue_sharing(
                            db,
                            organization_id,
                            dict(data.payload),
                            existing_payload=r.payload,
                        )
                        # 커스텀 필드도 생성과 **같은 규칙**으로 재검증한다.
                        # 예전엔 수정 경로가 payload 를 통째 교체만 해서, 작성 때 막히던
                        # 값이 수정으로는 그냥 들어갔다.
                        # 스냅샷은 지금 폼 기준으로 다시 만든다 — 사용자가 보고 고친 폼이
                        # 곧 저장 형태여야 한다(카테고리를 바꿨을 수도 있다).
                        tpl_u = await report_template_repository.get_template_for_store(
                            db, type="issue",
                            organization_id=organization_id, store_id=r.store_id,
                        )
                        fields_u = resolve_issue_fields(
                            (tpl_u.payload if tpl_u else {}) or {},
                            data.payload.get("category"),
                        )
                        data.payload["custom_field_values"] = (
                            validate_and_normalize_values(
                                fields_u, data.payload.get("custom_field_values")
                            )
                        )
                        data.payload["fields_snapshot"] = build_fields_snapshot(fields_u)
                r.payload = data.payload
                flag_modified(r, "payload")
            if data.sections is not None:
                _apply_section_updates(r, data.sections)
            await db.flush()
            await db.refresh(r)
            await db.commit()
            return r
        except Exception:
            await db.rollback()
            raise

    async def transition_issue_status(
        self,
        db: AsyncSession,
        report_id: UUID,
        organization_id: UUID,
        actor_id: UUID,
        new_status: str,
    ) -> Report:
        """이슈 상태 전이 (open → in_progress → closed). 관리자(SV+) 권한."""
        from app.schemas.report import ISSUE_STATUSES
        if new_status not in ISSUE_STATUSES:
            raise BadRequestError(f"Invalid status. Allowed: {ISSUE_STATUSES}")
        r = await self.get_report(db, report_id, organization_id)
        if r.type != "issue":
            raise BadRequestError("Only issue reports support status transition")
        if r.status == new_status:
            return r
        try:
            r.status = new_status
            await db.flush()
            await db.refresh(r)
            await db.commit()
            await self._notify_issue_event(
                db, report=r, event=f"status:{new_status}", actor_id=actor_id
            )
            return r
        except Exception:
            await db.rollback()
            raise

    async def submit_report(
        self,
        db: AsyncSession,
        report_id: UUID,
        organization_id: UUID,
        author_id: UUID,
    ) -> Report:
        r = await self.get_report(db, report_id, organization_id)
        if r.author_id != author_id:
            raise ForbiddenError("Only the author can submit this report")
        if r.status != "draft":
            raise BadRequestError("Only draft reports can be submitted")
        try:
            r.status = "submitted"
            r.submitted_at = datetime.now(timezone.utc)
            await db.flush()
            await db.refresh(r)
            await db.commit()
            # 제출 시 매장 리뷰어(SV+)에게 알림 (daily 한정, 최소 동작).
            if r.type == "daily":
                await self._notify_daily_submitted(db, report=r, actor_id=author_id)
            return r
        except Exception:
            await db.rollback()
            raise

    async def review_report(
        self,
        db: AsyncSession,
        report_id: UUID,
        organization_id: UUID,
        reviewer_id: UUID,
        feedback: str | None = None,
    ) -> Report:
        """리포트 검토 완료 처리 (P3, reports:review).

        submitted → reviewed. reviewed_by/at 기록. feedback 있으면 코멘트로 남기고
        작성자에게 알림. reviewed 상태에서 재호출은 멱등(메타만 갱신).
        """
        r = await self.get_report(db, report_id, organization_id)
        if r.status == "draft":
            raise BadRequestError(
                "This report has not been submitted yet. Ask the author to submit it first."
            )
        try:
            r.status = "reviewed"
            r.reviewed_by_id = reviewer_id
            r.reviewed_at = datetime.now(timezone.utc)
            comment: ReportComment | None = None
            if feedback and feedback.strip():
                comment = ReportComment(
                    report_id=r.id, user_id=reviewer_id, content=feedback.strip()
                )
                db.add(comment)
            await db.flush()
            await db.refresh(r)
            await db.commit()
            # 작성자에게 리뷰 알림 (+ feedback excerpt).
            await self._notify_review(
                db, report=r, reviewer_id=reviewer_id, excerpt=feedback
            )
            return r
        except Exception:
            await db.rollback()
            raise

    async def acknowledge_report(
        self,
        db: AsyncSession,
        report_id: UUID,
        organization_id: UUID,
        user_id: UUID,
    ) -> ReportAcknowledgement:
        """리포트 읽음 확인 (P3, reports:acknowledge). 멱등 upsert."""
        r = await self.get_report(db, report_id, organization_id)
        existing = await db.execute(
            select(ReportAcknowledgement).where(
                ReportAcknowledgement.report_id == r.id,
                ReportAcknowledgement.user_id == user_id,
            )
        )
        ack = existing.scalar_one_or_none()
        if ack:
            return ack
        try:
            ack = ReportAcknowledgement(report_id=r.id, user_id=user_id)
            db.add(ack)
            await db.flush()
            await db.refresh(ack)
            await db.commit()
            return ack
        except Exception:
            await db.rollback()
            # 경합으로 UNIQUE 충돌 시 기존 행 반환 (멱등).
            existing = await db.execute(
                select(ReportAcknowledgement).where(
                    ReportAcknowledgement.report_id == report_id,
                    ReportAcknowledgement.user_id == user_id,
                )
            )
            ack = existing.scalar_one_or_none()
            if ack:
                return ack
            raise

    async def _notify_daily_submitted(
        self, db: AsyncSession, *, report: Report, actor_id: UUID
    ) -> None:
        """daily 제출 시 매장 리뷰어(SV+)에게 in-app 알림. 본인 제외. 실패 무시."""
        if not report.store_id:
            return
        try:
            from app.services.alert_service import alert_service

            author_priority = await _author_priority_of(db, report)
            q = (
                select(User.id, User.full_name)
                .join(Role, Role.id == User.role_id)
                .join(UserStore, UserStore.user_id == User.id)
                .where(
                    UserStore.store_id == report.store_id,
                    # 가시성 규칙과 동일 — manager 이면서 상위 직급인 사람만 이 리포트를 연다.
                    UserStore.is_manager.is_(True),
                    Role.priority < author_priority,
                    User.is_active.is_(True),
                    User.deleted_at.is_(None),
                )
                .distinct()
            )
            res = await db.execute(q)
            reviewers = {row.id for row in res}
            reviewers.discard(actor_id)
            if not reviewers:
                return
            author_r = await db.execute(
                select(User.full_name).where(User.id == report.author_id)
            )
            author_name = author_r.scalar() or "A staff member"
            period = (report.payload or {}).get("period", "")
            context_label = f"daily report ({period})" if period else "daily report"
            for uid in reviewers:
                try:
                    await alert_service.create_for_report_submitted(
                        db,
                        organization_id=report.organization_id,
                        recipient_id=uid,
                        author_name=author_name,
                        context_label=context_label,
                        reference_type=f"{report.type}_report",
                        reference_id=report.id,
                    )
                except Exception:
                    pass
            await db.commit()
        except Exception:
            pass

    async def _notify_review(
        self,
        db: AsyncSession,
        *,
        report: Report,
        reviewer_id: UUID,
        excerpt: str | None,
    ) -> None:
        """리뷰 완료 시 작성자에게 알림 + (feedback 있으면) 이메일."""
        recipient_id = report.author_id
        if recipient_id is None or recipient_id == reviewer_id:
            return
        try:
            from app.services.alert_service import alert_service

            reviewer_r = await db.execute(
                select(User.full_name).where(User.id == reviewer_id)
            )
            reviewer_name = reviewer_r.scalar() or "A manager"
            period = (report.payload or {}).get("period", "")
            context_label = f"daily report ({period})" if period else "report"
            await alert_service.create_for_report_reviewed(
                db,
                organization_id=report.organization_id,
                recipient_id=recipient_id,
                reviewer_name=reviewer_name,
                context_label=context_label,
                reference_type=f"{report.type}_report",
                reference_id=report.id,
            )
            await db.commit()
        except Exception:
            pass

    async def delete_report(
        self,
        db: AsyncSession,
        report_id: UUID,
        organization_id: UUID,
        author_id: UUID | None = None,
    ) -> None:
        r = await report_repository.get_with_details(db, report_id, organization_id)
        if not r:
            raise NotFoundError("Report not found")
        if author_id:
            if r.author_id != author_id:
                raise ForbiddenError("Only the author can delete this report")
            if r.status != "draft":
                raise BadRequestError("Only draft reports can be deleted")
        try:
            # soft delete (drafts에서도 동일 처리. 필요시 hard delete로 변경)
            r.deleted_at = datetime.now(timezone.utc)
            await db.flush()
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def add_comment(
        self,
        db: AsyncSession,
        report_id: UUID,
        organization_id: UUID,
        user_id: UUID,
        data: ReportCommentCreate,
    ) -> ReportComment:
        r = await report_repository.get_with_details(db, report_id, organization_id)
        if not r:
            raise NotFoundError("Report not found")
        try:
            c = ReportComment(report_id=r.id, user_id=user_id, content=data.content)
            db.add(c)
            await db.flush()
            await db.refresh(c)
            await db.commit()
            if r.type == "issue":
                await self._notify_issue_event(
                    db, report=r, event="comment", actor_id=user_id, excerpt=data.content
                )
            else:
                await self._notify_reply(db, report=r, author_id=user_id, excerpt=data.content)
            return c
        except Exception:
            await db.rollback()
            raise

    async def _notify_issue_event(
        self,
        db: AsyncSession,
        *,
        report: Report,
        event: str,
        actor_id: UUID,
        excerpt: str | None = None,
    ) -> None:
        """이슈 리포트 이벤트 알림.

        event: "created" | "status:open" | "status:in_progress" | "status:closed" | "comment"
        **알림 대상 = 수신자 하나뿐이다**
        ((그 매장 GM 이상 전원) ∪ (콕 집어 추가한 사람) ∪ (작성자, created 제외)).
        in-app alert 과 email 이 같은 집합을 쓴다. 작성자/액션 actor 도 GM 이상이면 포함된다.
        작성자는 GM 미만이어도 자기 리포트의 후속(댓글/상태 변경)은 받는다 — 이슈를 올린
        사람이 결과를 모르면 리포트 기능 자체가 성립하지 않는다.

        조회권과 알림은 별개다 — **볼 수 있다고 알림을 받지 않는다.**
        조회 범위(visibility_scope)를 넓히는 것은 "필요하면 열어볼 수 있게" 한다는 뜻이지
        "전원에게 통지한다"는 뜻이 아니다. 예전엔 alert 을 조회권자 전원에게 보내서,
        범위를 store_all 로 넓히는 순간 매장 전원에게 알림이 쏟아졌다.

        알림 실패는 리포트 작성을 깨뜨리면 안 되므로 계속 삼키되, **반드시 로그를 남긴다**
        (예전엔 except: pass 로 완전 무음이라 안 간 알림을 아무도 몰랐다).
        """
        try:
            from app.services.alert_service import alert_service
            from app.utils.deep_links import build_cta_url
            from app.utils.email import send_email
            from app.utils.email_templates import build_reply_email
            import asyncio

            recipients = await _resolve_issue_notify_recipients(db, report, event=event)
            # actor 를 빼지 않는다 — 그 매장 GM 이상은 자기가 올린/건드린 이슈여도
            # "그 매장 이슈 알림" 을 받는 게 확정 규칙이다 (2026-08-14).

            # 액션 actor 이름
            actor_r = await db.execute(select(User.full_name).where(User.id == actor_id))
            actor_name = actor_r.scalar() or "Someone"

            severity = (report.payload or {}).get("severity", "")
            category = (report.payload or {}).get("category", "")
            subtitle_parts = []
            if category:
                subtitle_parts.append(category)
            if severity:
                subtitle_parts.append(severity)
            subtitle = " · ".join(subtitle_parts) or "issue"

            # 이벤트별 문구/본문.
            # 예전엔 세 이벤트가 전부 답글 템플릿 기본문구("New reply on your ...")를 타서
            # 신규 등록도 "누가 답글을 달았다"로 나갔고, 인용 블록에 본문 대신 제목이 들어가
            # subtitle 의 제목과 중복되면서 정작 내용은 메일에 없었다.
            description = (report.payload or {}).get("description") or None
            status_label_map = {
                "open": "reopened",
                "in_progress": "marked in progress",
                "closed": "closed",
            }
            title_txt = report.title or "(untitled)"
            if event == "created":
                context_label = "issue report"
                excerpt_text = description
                excerpt_fallback = "(No description provided)"
                email_subject = f"[Issue] New · {title_txt}"
                headline = "New issue report"
                lead = f"<strong>{escape(actor_name)}</strong> reported a new issue:"
            elif event.startswith("status:"):
                new_status = event.split(":", 1)[1]
                context_label = f"issue {new_status}"
                excerpt_text = description
                excerpt_fallback = "(No description provided)"
                status_txt = status_label_map.get(new_status, new_status)
                email_subject = f"[Issue] {new_status.replace('_', ' ').title()} · {title_txt}"
                headline = f"Issue {status_txt}"
                lead = (
                    f"<strong>{escape(actor_name)}</strong> {escape(status_txt)} this issue:"
                )
            else:
                context_label = "issue report"
                excerpt_text = excerpt
                excerpt_fallback = "(Photo or video attachment)"
                email_subject = f"[Issue] Reply · {title_txt}"
                headline = "New reply on an issue report"
                lead = f"<strong>{escape(actor_name)}</strong> left a reply on:"

            # 1) 수신자에게 in-app alert (email 과 같은 집합 — 조회권자 전원이 아니다)
            for uid in recipients:
                try:
                    await alert_service.create_for_reply(
                        db,
                        organization_id=report.organization_id,
                        recipient_id=uid,
                        author_name=actor_name,
                        context_label=context_label,
                        reference_type="issue_report",
                        reference_id=report.id,
                    )
                except Exception:
                    logger.warning(
                        "issue notify: in-app alert failed (report_id=%s event=%s recipient=%s)",
                        report.id, event, uid, exc_info=True,
                    )
            await db.commit()

            # 2) 수신자(자동 후보 − 제외 + 지목 추가) 전원에게 이메일
            for uid in recipients:
                try:
                    recipient = await db.execute(
                        select(User.full_name, User.email, Role.priority)
                        .join(Role, Role.id == User.role_id, isouter=True)
                        .where(User.id == uid)
                    )
                    row = recipient.first()
                    if not row or not row.email:
                        logger.warning(
                            "issue notify: no email for recipient (report_id=%s event=%s recipient=%s)",
                            report.id, event, uid,
                        )
                        continue
                    if not await alert_service.should_send_email(db, uid, "reply"):
                        continue
                    subject, html = build_reply_email(
                        recipient_name=row.full_name or "there",
                        author_name=actor_name,
                        context_label=context_label.title(),
                        context_subtitle=f"{subtitle} · {report.title or ''}".strip(" ·"),
                        excerpt=_email_excerpt(excerpt_text),
                        cta_url=build_cta_url("issue_report", report.id, row.priority),
                        subject=email_subject,
                        headline=headline,
                        lead=lead,
                        cta_label="Open issue",
                        excerpt_fallback=excerpt_fallback,
                    )
                    asyncio.create_task(send_email(to=row.email, subject=subject, html=html))
                except Exception:
                    logger.warning(
                        "issue notify: email dispatch failed (report_id=%s event=%s recipient=%s)",
                        report.id, event, uid, exc_info=True,
                    )
        except Exception:
            logger.exception(
                "issue notify: failed entirely (report_id=%s event=%s actor=%s)",
                getattr(report, "id", None), event, actor_id,
            )

    async def _notify_reply(
        self,
        db: AsyncSession,
        *,
        report: Report,
        author_id: UUID,
        excerpt: str | None,
    ) -> None:
        """리포트에 코멘트가 달렸을 때 작성자에게 알림 + 이메일."""
        recipient_id: UUID | None = report.author_id
        if recipient_id is None or recipient_id == author_id:
            return
        try:
            from app.services.alert_service import alert_service
            from app.utils.deep_links import build_cta_url
            from app.utils.email import send_email
            from app.utils.email_templates import build_reply_email
            import asyncio

            author_r = await db.execute(select(User.full_name).where(User.id == author_id))
            author_name = author_r.scalar() or "Manager"
            recipient_r = await db.execute(
                select(User.full_name, User.email, Role.priority)
                .join(Role, Role.id == User.role_id, isouter=True)
                .where(User.id == recipient_id)
            )
            row = recipient_r.first()
            recipient_name = (row.full_name if row else None) or "there"
            recipient_email = row.email if row else None
            recipient_priority = row.priority if row else None

            # context label/subtitle: type별
            context_label = "report"
            subtitle = ""
            if report.type == "daily":
                period = (report.payload or {}).get("period", "")
                period_label = "Lunch" if period == "lunch" else "Dinner" if period == "dinner" else str(period)
                subtitle = f"{report.report_date} · {period_label}"
                context_label = "daily report"
            else:
                if report.title:
                    subtitle = report.title
                elif report.report_date:
                    subtitle = str(report.report_date)
                context_label = f"{report.type} report"

            await alert_service.create_for_reply(
                db,
                organization_id=report.organization_id,
                recipient_id=recipient_id,
                author_name=author_name,
                context_label=context_label,
                reference_type=f"{report.type}_report",
                reference_id=report.id,
            )
            await db.commit()

            if recipient_email and await alert_service.should_send_email(
                db, recipient_id, "reply"
            ):
                subject, html = build_reply_email(
                    recipient_name=recipient_name,
                    author_name=author_name,
                    context_label=context_label.title(),
                    context_subtitle=subtitle,
                    excerpt=_email_excerpt(excerpt),
                    cta_url=build_cta_url(
                        f"{report.type}_report", report.id, recipient_priority
                    ),
                )
                asyncio.create_task(send_email(to=recipient_email, subject=subject, html=html))
        except Exception:
            pass

    # ── Response builders ──────────────────────────────────

    def _resolve_payload_attachments(self, payload: dict) -> dict:
        """payload.attachments[].key → url 추가."""
        if not isinstance(payload, dict):
            return payload
        attachments = payload.get("attachments")
        if not attachments:
            return payload
        from app.services.storage_service import storage_service
        resolved = []
        for a in attachments:
            item = dict(a) if isinstance(a, dict) else {}
            key = item.get("key")
            if key:
                item["url"] = storage_service.resolve_url(key)
            resolved.append(item)
        return {**payload, "attachments": resolved}

    @staticmethod
    def _compute_late_flags(r: Report) -> tuple[bool, bool]:
        """(is_overdue, is_late) 계산 (display only).

        is_overdue: 마감 지났는데 아직 미제출(draft).
        is_late: 마감 이후에 제출됨.
        """
        if r.deadline_at is None:
            return False, False
        deadline = r.deadline_at
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        submitted = r.submitted_at
        if submitted is not None and submitted.tzinfo is None:
            submitted = submitted.replace(tzinfo=timezone.utc)
        is_late = submitted is not None and submitted > deadline
        is_overdue = submitted is None and r.status == "draft" and now > deadline
        return is_overdue, is_late

    def _to_dict(
        self,
        r: Report,
        author_name: str | None,
        store_name: str | None,
        include_comments: bool = False,
        comment_user_names: dict | None = None,
        reviewer_name: str | None = None,
        ack_user_names: dict | None = None,
    ) -> dict:
        try:
            comment_count = len(r.comments)
        except Exception:
            comment_count = 0
        try:
            acks = list(r.acknowledgements)
        except Exception:
            acks = []
        is_overdue, is_late = self._compute_late_flags(r)
        ack_names = ack_user_names or {}
        resp = {
            "id": str(r.id),
            "type": r.type,
            "organization_id": str(r.organization_id),
            "store_id": str(r.store_id) if r.store_id else None,
            "store_name": store_name,
            "template_id": str(r.template_id) if r.template_id else None,
            "author_id": str(r.author_id) if r.author_id else None,
            "author_name": author_name,
            "title": r.title,
            "status": r.status,
            "report_date": r.report_date,
            "submitted_at": r.submitted_at,
            "deadline_at": r.deadline_at,
            "is_overdue": is_overdue,
            "is_late": is_late,
            "reviewed_by_id": str(r.reviewed_by_id) if r.reviewed_by_id else None,
            "reviewed_by_name": reviewer_name,
            "reviewed_at": r.reviewed_at,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
            "payload": self._resolve_payload_attachments(r.payload or {}),
            "comment_count": comment_count,
            "acknowledgement_count": len(acks),
            "acknowledgements": [
                {
                    "user_id": str(a.user_id),
                    "user_name": ack_names.get(a.user_id) or "Unknown",
                    "acknowledged_at": a.acknowledged_at,
                }
                for a in acks
            ],
        }
        if include_comments:
            names = comment_user_names or {}
            resp["comments"] = [
                {
                    "id": str(c.id),
                    "user_id": str(c.user_id) if c.user_id else None,
                    "user_name": names.get(c.user_id) or "Unknown",
                    "content": c.content,
                    "created_at": c.created_at,
                }
                for c in r.comments
            ]
        else:
            resp["comments"] = []
        return resp

    async def build_response(
        self, db: AsyncSession, report: Report, include_comments: bool = True
    ) -> dict:
        author_name: str | None = None
        if report.author_id:
            u = await db.execute(select(User.full_name).where(User.id == report.author_id))
            author_name = u.scalar()
        store_name: str | None = None
        if report.store_id:
            s = await db.execute(select(Store.name).where(Store.id == report.store_id))
            store_name = s.scalar()

        reviewer_name: str | None = None
        if report.reviewed_by_id:
            ru = await db.execute(
                select(User.full_name).where(User.id == report.reviewed_by_id)
            )
            reviewer_name = ru.scalar()

        ack_user_names: dict | None = None
        try:
            ack_ids = list({a.user_id for a in report.acknowledgements})
        except Exception:
            ack_ids = []
        if ack_ids:
            au = await db.execute(
                select(User.id, User.full_name).where(User.id.in_(ack_ids))
            )
            ack_user_names = {row.id: row.full_name for row in au}

        comment_user_names = None
        if include_comments:
            try:
                ids = list({c.user_id for c in report.comments if c.user_id})
            except Exception:
                ids = []
            if ids:
                cu = await db.execute(
                    select(User.id, User.full_name).where(User.id.in_(ids))
                )
                comment_user_names = {row.id: row.full_name for row in cu}
        return self._to_dict(
            report, author_name, store_name, include_comments, comment_user_names,
            reviewer_name=reviewer_name, ack_user_names=ack_user_names,
        )

    async def build_responses_batch(
        self, db: AsyncSession, reports: list[Report]
    ) -> list[dict]:
        author_ids = list({r.author_id for r in reports if r.author_id})
        reviewer_ids = list({r.reviewed_by_id for r in reports if r.reviewed_by_id})
        store_ids = list({r.store_id for r in reports if r.store_id})
        ack_ids: set[UUID] = set()
        for r in reports:
            try:
                ack_ids.update(a.user_id for a in r.acknowledgements)
            except Exception:
                pass
        user_id_set = set(author_ids) | set(reviewer_ids) | ack_ids
        user_names: dict = {}
        if user_id_set:
            res = await db.execute(
                select(User.id, User.full_name).where(User.id.in_(user_id_set))
            )
            user_names = {row.id: row.full_name for row in res}
        store_names: dict = {}
        if store_ids:
            res = await db.execute(select(Store.id, Store.name).where(Store.id.in_(store_ids)))
            store_names = {row.id: row.name for row in res}
        return [
            self._to_dict(
                r,
                user_names.get(r.author_id) if r.author_id else None,
                store_names.get(r.store_id) if r.store_id else None,
                reviewer_name=user_names.get(r.reviewed_by_id) if r.reviewed_by_id else None,
                ack_user_names=user_names,
            )
            for r in reports
        ]

    def build_template_response(self, template: ReportTemplate) -> dict:
        return {
            "id": str(template.id),
            "type": template.type,
            "organization_id": str(template.organization_id) if template.organization_id else None,
            "store_id": str(template.store_id) if template.store_id else None,
            "name": template.name,
            "is_default": template.is_default,
            "is_active": template.is_active,
            "applicable_types": template.applicable_types,
            "payload": template.payload or {},
            "created_at": template.created_at,
        }


report_service: ReportService = ReportService()
