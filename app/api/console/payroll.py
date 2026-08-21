"""콘솔 Payroll API — pay periods / preview / confirm / entries / events (Phase 3).

엔드포인트:
    GET  /api/v1/console/payroll/periods/                (payroll:read)
    GET  /api/v1/console/payroll/periods/{id}/preview    (payroll:read)
    POST /api/v1/console/payroll/periods/{id}/confirm    (payroll:confirm)
    GET  /api/v1/console/payroll/periods/{id}/entries    (payroll:read)
    GET  /api/v1/console/payroll/periods/{id}/export     (payroll:export)
    GET  /api/v1/console/payroll/events/                 (schedules:update)
    POST /api/v1/console/payroll/events/{id}/tag         (schedules:update)

권한 설계:
    - payroll:* 는 기본 Owner 전용 (설계방향 L1 — permission 코드로 게이트,
      is_owner 하드체크 금지). preview/entries 는 cost 데이터지만 payroll:read
      자체가 Owner 게이트라 별도 scrub 불필요.
    - events 는 금액 없는 운영 페이로드(귀책 태깅, E1) — 스케줄 운영 권한
      (schedules:update)으로 GM/SV 도 태깅 가능.

period 는 시스템 생성 전용 (스펙 §4 — 수동 생성 API 없음): 목록 조회가 요청
범위의 반월 기간을 ensure_period 로 자동 구체화한다 (미래 기간은 만들지 않음).
"""

from __future__ import annotations

from collections import Counter
from datetime import date as DateType, datetime, timedelta, timezone
from io import BytesIO
from typing import Annotated, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import check_store_access, require_permission
from app.core.error_codes.common import GROUP_NOT_FOUND
from app.database import get_db
from app.models.organization import Organization, Store, StoreGroup
from app.models.payroll import PayPeriod, PayrollEntry, PayrollEvent
from app.models.user import User
from app.schemas.payroll import (
    DraftPayStubResponse,
    EventTagRequest,
    PayPeriodResponse,
    PayrollEntryResponse,
    PayrollEventResponse,
    PayStubResponse,
    PeriodConfirmResponse,
    PeriodEntriesResponse,
    PeriodPreviewResponse,
    ValidationSummaryItem,
)
from app.services.pay_stub_service import pay_stub_service
from app.services.payroll_calc_service import (
    parse_frozen_breakdown,
    payroll_calc_service,
)
from app.services.payroll_confirm_service import payroll_confirm_service
from app.services.payroll_event_service import payroll_event_service
from app.services.payroll_cfs_export_service import (
    export_filename as cfs_export_filename,
    payroll_cfs_export_service,
    workbook_bytes as cfs_workbook_bytes,
)
from app.services.payroll_export_service import (
    export_filename,
    payroll_export_service,
    workbook_bytes,
)
from app.services.payroll_period_service import payroll_period_service
from app.utils.download import content_disposition
from app.utils.exceptions import BadRequestError, DuplicateError, NotFoundError
from app.utils.names import display_name

router: APIRouter = APIRouter()

# 기간 목록 기본/최대 조회 범위 (일) — 반월 기간 auto-ensure 폭주 방지
_DEFAULT_RANGE_DAYS = 75  # 약 5개 반월 기간
_MAX_RANGE_DAYS = 400  # 약 13개월

_VALID_ATTRIBUTION_FILTERS = {"staff", "management", "untagged"}


def _group_today(stores: list[Store]) -> DateType:
    """그룹 타임존 기준 오늘 날짜 — '현재 기간' 판정용 (반월 경계는 날짜 기준).

    그룹 매장들의 tz 는 단일이어야 한다 (D5 — 불일치는 period service 가 400).
    """
    try:
        tz = ZoneInfo(payroll_period_service.group_timezone(stores))
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date()


async def _check_group_access(
    db: AsyncSession, user, group_id: UUID
) -> StoreGroup:
    """그룹 접근 검사 — 타 org 는 404 (존재 비노출).

    payroll 은 그룹(법인) 전체를 다루므로, 비-Owner 는 그룹 내 **모든** 매장에
    접근 가능해야 한다 (일부 매장만 배정된 매니저에게 법인 급여를 열지 않는다).
    """
    group = await db.get(StoreGroup, group_id)
    if group is None or group.organization_id != user.organization_id:
        raise GROUP_NOT_FOUND()
    stores = (
        (await db.execute(select(Store).where(Store.group_id == group_id)))
        .scalars()
        .all()
    )
    for store in stores:
        await check_store_access(db, user, store.id)
    return group


async def _period_store_ids(db: AsyncSession, period: PayPeriod) -> list[UUID]:
    """기간의 스코프 매장 id 목록 — group 기간은 그룹 전체, 레거시는 1곳."""
    if period.store_group_id is not None:
        rows = await db.scalars(
            select(Store.id).where(Store.group_id == period.store_group_id)
        )
        return list(rows.all())
    return [period.store_id] if period.store_id is not None else []


async def _period_response(db: AsyncSession, period: PayPeriod) -> PayPeriodResponse:
    """PayPeriod → 응답 모델 (+대응 tip_period status — 게이트 ④ 사전 표시)."""
    store_ids = await _period_store_ids(db, period)
    statuses = await payroll_period_service.tip_period_status_for(
        db, store_ids=store_ids, period=period
    )
    return PayPeriodResponse(
        id=period.id,
        organization_id=period.organization_id,
        store_group_id=period.store_group_id,
        store_id=period.store_id,
        start_date=period.start_date,
        end_date=period.end_date,
        status=period.status,
        confirmed_at=period.confirmed_at,
        confirmed_by=period.confirmed_by,
        override_reason=period.override_reason,
        tip_period_status=payroll_period_service.aggregate_tip_status(statuses),
    )


def _entry_response(entry: PayrollEntry) -> PayrollEntryResponse:
    """PayrollEntry → 응답 모델 (breakdown 은 계약 파서 경유 — calc_version 검증)."""
    return PayrollEntryResponse(
        id=entry.id,
        pay_period_id=entry.pay_period_id,
        user_id=entry.user_id,
        org_member_id=entry.org_member_id,
        empid=entry.empid,
        crewid=entry.crewid,
        member_name=entry.member_name,
        revision=entry.revision,
        regular_minutes=entry.regular_minutes,
        ot_minutes=entry.ot_minutes,
        dt_minutes=entry.dt_minutes,
        regular_pay=entry.regular_pay,
        ot_pay=entry.ot_pay,
        dt_pay=entry.dt_pay,
        penalty_pay=entry.penalty_pay,
        card_tips=entry.card_tips,
        gross_pay=entry.gross_pay,
        calc_version=entry.calc_version,
        breakdown=parse_frozen_breakdown(entry.breakdown),
        created_at=entry.created_at,
    )


def _event_response(
    event: PayrollEvent, names: dict[UUID, str]
) -> PayrollEventResponse:
    """PayrollEvent → 금액 없는 응답 모델 (운영 태깅용)."""
    return PayrollEventResponse(
        id=event.id,
        user_id=event.user_id,
        member_name=names.get(event.user_id) if event.user_id else None,
        attendance_id=event.attendance_id,
        work_date=event.work_date,
        kind=event.kind,
        reason=event.reason,
        attribution=event.attribution,
        tagged_by=event.tagged_by,
        tagged_at=event.tagged_at,
        voided_at=event.voided_at,
        pay_period_id=event.pay_period_id,
        frozen=event.pay_period_id is not None,
        created_at=event.created_at,
    )


async def _event_names(
    db: AsyncSession, events: list[PayrollEvent]
) -> dict[UUID, str]:
    """이벤트 사용자 표시 이름 일괄 로드 (display_name 경유)."""
    user_ids = {e.user_id for e in events if e.user_id is not None}
    if not user_ids:
        return {}
    users = (
        (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
    )
    return {u.id: display_name(u) for u in users}


async def _get_period_checked(
    db: AsyncSession, current_user: User, period_id: UUID
) -> PayPeriod:
    """period 로드 + 그룹(또는 레거시 매장) 접근 검사 (타 org 는 404)."""
    period = await db.get(PayPeriod, period_id)
    if period is None or period.organization_id != current_user.organization_id:
        raise NotFoundError("Pay period not found")
    if period.store_group_id is not None:
        await _check_group_access(db, current_user, period.store_group_id)
    elif period.store_id is not None:
        await check_store_access(db, current_user, period.store_id)
    return period


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------


@router.get("/periods/", response_model=list[PayPeriodResponse])
async def list_periods(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("payroll:read"))],
    group_id: Annotated[UUID, Query()],
    start: Annotated[Optional[DateType], Query()] = None,
    end: Annotated[Optional[DateType], Query()] = None,
) -> list[PayPeriodResponse]:
    """법인(그룹)의 pay period 목록 — 요청 범위와 겹치는 반월 기간을 자동 보장.

    범위 기본값: 오늘(그룹 tz)까지 최근 75일. 오늘 이전(포함) 범위의 기간 행이
    없으면 ensure_period 로 만들어 준다 — 시스템 생성 전용 원칙(스펙 §4)의
    유일한 콘솔 진입점. 미래 기간은 만들지 않는다.
    전환 전 확정된 레거시 store 스코프 기간(그룹 소속 매장분)도 목록에 나온다.
    """
    await _check_group_access(db, current_user, group_id)
    stores = await payroll_period_service.group_stores(db, group_id)
    today = _group_today(stores)
    if end is None:
        end = today
    if start is None:
        start = end - timedelta(days=_DEFAULT_RANGE_DAYS)
    if start > end:
        raise BadRequestError(
            "Start date must be on or before end date — check the requested range"
        )
    if (end - start).days > _MAX_RANGE_DAYS:
        raise BadRequestError(
            "Requested range is too large — request at most 13 months of pay periods"
        )

    # 오늘까지의 반월 기간 auto-ensure (겹침은 ensure_period 가 원천 차단)
    cursor = start
    ensure_until = min(end, today)
    while cursor <= ensure_until:
        period = await payroll_period_service.ensure_period(
            db, store_group_id=group_id, date_in_period=cursor
        )
        cursor = period.end_date + timedelta(days=1)
    await db.commit()

    periods = await payroll_period_service.list_periods(
        db, store_group_id=group_id, range_start=start, range_end=end
    )
    return [await _period_response(db, p) for p in periods]


@router.get("/periods/{period_id}/preview", response_model=PeriodPreviewResponse)
async def preview_period(
    period_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("payroll:read"))],
) -> PeriodPreviewResponse:
    """기간 미리보기 — 직원별 계산 행 + validation 요약 (동결 없음, 멱등).

    부수효과로 payroll_events 가 최신 상태로 upsert 된다 (open 기간 재계산
    라이프사이클). confirmed 기간은 409 — 동결본은 entries 엔드포인트가 원천.
    """
    period = await _get_period_checked(db, current_user, period_id)
    if period.status == "confirmed":
        raise DuplicateError(
            "This pay period is already confirmed — use its frozen entries "
            "instead of the live preview"
        )
    rows = await payroll_calc_service.preview_period(db, period)
    await db.commit()  # 이벤트 upsert 영속화 (계산 자체는 저장 안 됨)

    counter = Counter(v.code for row in rows for v in row.validations)
    return PeriodPreviewResponse(
        period=await _period_response(db, period),
        rows=rows,
        validations_summary=[
            ValidationSummaryItem(code=code, count=count)
            for code, count in sorted(counter.items())
        ],
    )


@router.post("/periods/{period_id}/confirm", response_model=PeriodConfirmResponse)
async def confirm_period(
    period_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("payroll:confirm"))],
) -> PeriodConfirmResponse:
    """기간 확정 (L5 마감 게이트 + 동결) — 서비스가 트랜잭션 소유.

    실패 응답 (409, detail 구조화):
        - detail.code == "pay_period_already_confirmed": 이미 확정됨
        - detail.code == "payroll_close_gates_failed": detail.gates[] 에
          게이트별 {gate, message, items[{user_id, member_name, dates, message}]}
    """
    await _get_period_checked(db, current_user, period_id)
    result = await payroll_confirm_service.confirm_period(
        db, period_id=period_id, actor=current_user
    )
    return PeriodConfirmResponse(
        period=await _period_response(db, result.period),
        entries=[_entry_response(e) for e in result.entries],
        events_frozen=result.events_frozen,
    )


@router.get("/periods/{period_id}/entries", response_model=PeriodEntriesResponse)
async def list_period_entries(
    period_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("payroll:read"))],
) -> PeriodEntriesResponse:
    """동결된 entries 조회 — confirm 전이면 빈 목록 (open 기간은 preview 가 원천)."""
    period = await _get_period_checked(db, current_user, period_id)
    entries = (
        (
            await db.execute(
                select(PayrollEntry)
                .where(PayrollEntry.pay_period_id == period.id)
                .order_by(PayrollEntry.member_name.asc(), PayrollEntry.revision.asc())
            )
        )
        .scalars()
        .all()
    )
    return PeriodEntriesResponse(
        period=await _period_response(db, period),
        entries=[_entry_response(e) for e in entries],
    )


@router.get("/cfs-export")
async def export_cfs(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("payroll:export"))],
    start: Annotated[DateType, Query()],
    end: Annotated[DateType, Query()],
) -> StreamingResponse:
    """회계사(CFS) 급여 입력 파일 — 조직 전체를 그룹별 시트로 내보냅니다.

    시트 1장 = store group. 확정 전 기간이 섞이면 해당 시트에 DRAFT 배너가 붙고
    파일명에도 _DRAFT 가 붙습니다.
    """
    workbook, is_draft = await payroll_cfs_export_service.build_org_export(
        db,
        organization_id=current_user.organization_id,
        start_date=start,
        end_date=end,
    )
    await db.commit()  # 미확정 기간 preview 경로의 이벤트 upsert 영속화
    # 파일명에 조직명을 담는다 — 회계사는 여러 고객사 파일을 한 폴더에서 받는다.
    org = await db.get(Organization, current_user.organization_id)
    filename = cfs_export_filename(
        org.name if org else "ALL", start, end, draft=is_draft
    )
    return StreamingResponse(
        BytesIO(cfs_workbook_bytes(workbook)),
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": content_disposition(filename)},
    )


@router.get("/periods/{period_id}/export")
async def export_period(
    period_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("payroll:export"))],
    include_idle_members: Annotated[bool, Query()] = False,
) -> StreamingResponse:
    """기간 xlsx 다운로드 (DUMMY 포맷 v1, E3).

    include_idle_members=true 면 기간에 급여 활동이 없는 재직 직원도 0 행으로
    덧붙인다 (파일 전용 — 화면 로스터는 바뀌지 않는다).

    회계사 양식 확정 전 선개발 — 실제 양식 스왑은 payroll_export_service 의
    EXPORT_COLUMNS / export_row 만 교체 (스펙 참조).

    확정 기간: 동결 entries → payroll_{store}_{start}_{end}.xlsx
    미확정 기간: live preview 계산 → DRAFT (배너 행 + payroll_draft_… 파일명).
        확인용 내보내기이므로 차단하지 않되, 확정본과 혼동되지 않게 표시한다.
        preview 와 같은 경로라 payroll_events upsert 부수효과가 있다.

    404: 없는 기간 / 타 org 기간 (존재 비노출)
    """
    period = await _get_period_checked(db, current_user, period_id)
    result = await payroll_export_service.build_period_export(
        db, period, include_idle_members=include_idle_members
    )
    if result.is_draft:
        await db.commit()  # preview 경로의 이벤트 upsert 영속화 (계산은 저장 안 됨)
    if period.store_group_id is not None:
        group = await db.get(StoreGroup, period.store_group_id)
        scope_name = (group.code or group.name) if group else "group"
    else:
        store = await db.get(Store, period.store_id)
        scope_name = store.name if store else "store"
    filename = export_filename(
        scope_name,
        period.start_date,
        period.end_date,
        draft=result.is_draft,
    )
    return StreamingResponse(
        BytesIO(workbook_bytes(result.workbook)),
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": content_disposition(filename)},
    )


# ---------------------------------------------------------------------------
# Pay stub PDF (E4) — 동결 entry 기반 명세서
# ---------------------------------------------------------------------------


async def _get_entry_checked(
    db: AsyncSession, current_user: User, entry_id: UUID
) -> PayrollEntry:
    """entry 로드 + 소속 기간 스코프 접근 검사 (타 org 는 404 — 존재 비노출).

    group 스코프 entry 는 store_id 가 없다 — 접근 판정은 소속 기간이 원천.
    """
    entry = await db.get(PayrollEntry, entry_id)
    if entry is None or entry.organization_id != current_user.organization_id:
        raise NotFoundError("Payroll entry not found")
    await _get_period_checked(db, current_user, entry.pay_period_id)
    return entry


def _stub_response(entry: PayrollEntry, file) -> PayStubResponse:
    return PayStubResponse(
        entry_id=entry.id,
        pay_period_id=entry.pay_period_id,
        file_id=file.id,
        filename=file.original_filename or f"PayStub_{entry.id}.pdf",
        size_bytes=file.size_bytes,
        generated_at=file.updated_at,
    )


@router.post("/entries/{entry_id}/stub", response_model=PayStubResponse)
async def generate_pay_stub(
    entry_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("payroll:export"))],
) -> PayStubResponse:
    """entry 1건의 pay stub PDF 생성 (멱등 — 재호출 시 같은 파일 덮어쓰기).

    파일은 files/file_usages 레지스트리에 owner_type='pay_stub' 로 등록되고
    상대경로 key 만 저장된다. 응답에 경로/URL 미노출 — 다운로드는 GET .../stub.

    404: 없는 entry / 타 org entry (존재 비노출)
    409: 소속 기간 미확정 (방어 가드 — entry 는 confirm 시에만 생성됨)
    """
    entry = await _get_entry_checked(db, current_user, entry_id)
    file = await pay_stub_service.generate_stub(
        db, entry.id, actor_id=current_user.id
    )
    await db.commit()
    return _stub_response(entry, file)


@router.get("/entries/{entry_id}/stub")
async def download_pay_stub(
    entry_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("payroll:read"))],
) -> Response:
    """pay stub PDF 다운로드 — 인증 엔드포인트가 바이트 직접 서빙 (IDOR 방지).

    404: 없는 entry / 미생성 stub (생성 안내) / blob 유실 (재생성 안내)
    409: 소속 기간 미확정
    """
    entry = await _get_entry_checked(db, current_user, entry_id)
    data, filename = await pay_stub_service.read_stub_bytes(db, entry.id)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": content_disposition(filename)},
    )


@router.post(
    "/periods/{period_id}/users/{user_id}/stub",
    response_model=DraftPayStubResponse,
)
async def generate_draft_pay_stub(
    period_id: UUID,
    user_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("payroll:export"))],
) -> DraftPayStubResponse:
    """미확정(open) 기간의 draft stub — preview 기반 즉석 생성, 비영속.

    확정 전이라 숫자가 계속 바뀌므로 저장하지 않는다 (콘솔은 POST 로 메타
    확인 후 GET 으로 바이트를 받는다 — 두 번 다 재생성).

    404: 없는 기간 / 타 org / 이 기간에 해당 직원 payroll 데이터 없음
    409: 이미 confirmed — entry stub 사용 안내
    """
    period = await _get_period_checked(db, current_user, period_id)
    data, filename = await pay_stub_service.generate_draft_stub(
        db, period.id, user_id
    )
    return DraftPayStubResponse(
        pay_period_id=period.id,
        user_id=user_id,
        filename=filename,
        size_bytes=len(data),
        generated_at=datetime.now(timezone.utc),
    )


@router.get("/periods/{period_id}/users/{user_id}/stub")
async def download_draft_pay_stub(
    period_id: UUID,
    user_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("payroll:read"))],
) -> Response:
    """draft stub PDF 바이트 — DRAFT 배너 포함, _DRAFT 파일명."""
    period = await _get_period_checked(db, current_user, period_id)
    data, filename = await pay_stub_service.generate_draft_stub(
        db, period.id, user_id
    )
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": content_disposition(filename)},
    )


# ---------------------------------------------------------------------------
# Events (금액 없는 운영 페이로드 — 귀책 태깅 E1)
# ---------------------------------------------------------------------------


@router.get("/events/", response_model=list[PayrollEventResponse])
async def list_events(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
    store_id: Annotated[UUID, Query()],
    start: Annotated[DateType, Query()],
    end: Annotated[DateType, Query()],
    attribution: Annotated[
        Optional[str], Query(description="staff | management | untagged")
    ] = None,
    include_voided: Annotated[bool, Query()] = False,
) -> list[PayrollEventResponse]:
    """store + 기간의 payroll 이벤트 목록 — attribution/voided 필터 지원."""
    await check_store_access(db, current_user, store_id)
    if attribution is not None and attribution not in _VALID_ATTRIBUTION_FILTERS:
        raise BadRequestError(
            f"Invalid attribution filter: {attribution}. "
            "Use staff, management or untagged"
        )
    if start > end:
        raise BadRequestError(
            "Start date must be on or before end date — check the requested range"
        )
    events = await payroll_event_service.list_events(
        db, [store_id], start, end, include_voided=include_voided
    )
    if attribution == "untagged":
        events = [e for e in events if e.attribution is None]
    elif attribution is not None:
        events = [e for e in events if e.attribution == attribution]
    names = await _event_names(db, events)
    return [_event_response(e, names) for e in events]


@router.post("/events/{event_id}/tag", response_model=PayrollEventResponse)
async def tag_event(
    event_id: UUID,
    payload: EventTagRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> PayrollEventResponse:
    """이벤트 귀책 태그 (E1) — staff | management.

    400: attribution 값이 잘못됐거나, confirm 으로 동결된 이벤트일 때
    (동결 후 변경은 amendment 전용 — 서비스가 메시지로 안내).
    """
    event = await db.get(PayrollEvent, event_id)
    if event is None:
        raise NotFoundError("Payroll event not found")
    await check_store_access(db, current_user, event.store_id)
    tagged = await payroll_event_service.tag_event(
        db, event_id, payload.attribution, current_user.id
    )
    names = await _event_names(db, [tagged])
    return _event_response(tagged, names)
