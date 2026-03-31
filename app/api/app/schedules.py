"""직원 스케줄 조회/체크리스트 라우터 — 내 스케줄 + 체크리스트 완료 API.

App Schedule Router — API endpoints for user's own schedules with checklist operations.
Replaces the old /my/work-assignments endpoints with schedule-based equivalents.
"""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.repositories.checklist_instance_repository import checklist_instance_repository
from app.schemas.common import ChecklistItemComplete, ChecklistItemRespond
from app.services.checklist_instance_service import checklist_instance_service
from app.services.schedule_service import schedule_service
from app.utils.exceptions import ForbiddenError, NotFoundError
from app.utils.timezone import get_store_timezone, get_work_date, resolve_timezone

router: APIRouter = APIRouter()


@router.get("/schedules")
async def list_my_schedules(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    work_date: Annotated[date | None, Query()] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    past: Annotated[bool, Query()] = False,
    status: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 20,
    sort: Annotated[str | None, Query()] = None,
) -> dict:
    """내 확정 스케줄 목록을 조회합니다 (체크리스트 진행 정보 포함).

    List my confirmed schedules with checklist progress info.
    Default: only 'confirmed' status. Returns schedules with cl_instance data.
    Returns paginated response: { items, total, page, per_page }

    Special: work_date=today → 사용자 소속 매장별 timezone+day_start_time 기준으로 "오늘" 판단.
    """
    effective_status = status or "confirmed"

    # 날짜 필터 결정
    effective_date_from = date_from
    effective_date_to = date_to

    # 소속 매장별 timezone 기준으로 "오늘" 계산 (today/past 모드 공용)
    from app.utils.timezone import get_store_day_config
    from app.models.user_store import UserStore
    from sqlalchemy import select
    from datetime import timedelta

    store_today_dates: set[date] | None = None

    async def _get_store_today_dates() -> set[date]:
        store_rows = await db.execute(
            select(UserStore.store_id).where(UserStore.user_id == current_user.id)
        )
        user_store_ids = [row[0] for row in store_rows.all()]
        dates: set[date] = set()
        for sid in user_store_ids:
            tz, day_start = await get_store_day_config(db, sid)
            dates.add(get_work_date(tz, day_start))
        return dates

    if past:
        # Past 모드: 넉넉한 범위로 조회 후 매장별 today 기준으로 필터
        store_today_dates = await _get_store_today_dates()
        if store_today_dates:
            latest_today = max(store_today_dates)
            effective_date_to = latest_today - timedelta(days=1)
            effective_date_from = effective_date_to - timedelta(days=29)
    elif work_date is None and date_from is None and date_to is None:
        # Today 모드: 매장별 timezone 기준 오늘
        store_today_dates = await _get_store_today_dates()
        if store_today_dates:
            effective_date_from = min(store_today_dates)
            effective_date_to = max(store_today_dates)
    elif work_date and not date_from and not date_to:
        effective_date_from = work_date
        effective_date_to = work_date

    sort_desc = sort == "desc"

    entries, total = await schedule_service.list_entries(
        db,
        current_user.organization_id,
        user_id=current_user.id,
        date_from=effective_date_from,
        date_to=effective_date_to,
        status=effective_status,
        page=page,
        per_page=per_page,
        sort_desc=sort_desc,
    )

    # 매장별 정밀 필터: today/past 모드에서 각 entry의 store today 기준으로 판단
    if store_today_dates is not None and len(store_today_dates) > 1:
        filtered_entries = []
        for entry in entries:
            if entry.store_id:
                from uuid import UUID as UUIDType
                sid = UUIDType(entry.store_id) if isinstance(entry.store_id, str) else entry.store_id
                tz, day_start = await get_store_day_config(db, sid)
                entry_store_today = get_work_date(tz, day_start)
                if past:
                    # Past: entry의 work_date가 해당 매장 today 이전이면 포함
                    if entry.work_date < entry_store_today:
                        filtered_entries.append(entry)
                else:
                    # Today: entry의 work_date가 해당 매장 today와 같으면 포함
                    if entry.work_date == entry_store_today:
                        filtered_entries.append(entry)
            else:
                filtered_entries.append(entry)
        entries = filtered_entries
        total = len(entries)

    # 각 스케줄에 cl_instance 진행 정보 병합
    items: list[dict] = []
    for entry in entries:
        schedule_id = UUID(entry.id)
        cl_instance = await checklist_instance_repository.get_by_schedule_id(
            db, schedule_id
        )

        item: dict = {
            "id": entry.id,
            "store_id": entry.store_id,
            "store_name": entry.store_name,
            "work_role_id": entry.work_role_id,
            "work_role_name": entry.work_role_name,
            "user_id": entry.user_id,
            "user_name": entry.user_name,
            "work_date": entry.work_date,
            "start_time": entry.start_time,
            "end_time": entry.end_time,
            "break_start_time": entry.break_start_time,
            "break_end_time": entry.break_end_time,
            "net_work_minutes": entry.net_work_minutes,
            "status": entry.status,
            "request_id": str(entry.request_id) if entry.request_id else None,
            "note": entry.note,
            "created_at": entry.created_at,
            # 체크리스트 진행 정보
            "checklist_instance_id": str(cl_instance.id) if cl_instance else None,
            "total_items": cl_instance.total_items if cl_instance else 0,
            "completed_items": cl_instance.completed_items if cl_instance else 0,
            "checklist_status": cl_instance.status if cl_instance else None,
            "reported_at": cl_instance.reported_at if cl_instance else None,
        }
        # 아이템 리뷰 상태 요약 (목록에서 rejected/pending 표시용)
        if cl_instance and cl_instance.items:
            has_rejected = any(
                it.review_result == "fail" for it in cl_instance.items
            )
            has_pending_re_review = any(
                it.review_result == "pending_re_review" for it in cl_instance.items
            )
            all_passed = all(
                it.review_result == "pass" for it in cl_instance.items
            ) if cl_instance.items else False
            item["has_rejected"] = has_rejected
            item["has_pending_re_review"] = has_pending_re_review
            item["all_passed"] = all_passed
        else:
            item["has_rejected"] = False
            item["has_pending_re_review"] = False
            item["all_passed"] = False
        items.append(item)

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/schedules/{schedule_id}")
async def get_my_schedule(
    schedule_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """내 스케줄 상세를 조회합니다 (체크리스트 스냅샷 + 완료 기록 포함).

    Get my schedule detail with full checklist snapshot merged with completions.
    """
    from app.repositories.schedule_repository import schedule_repository

    entry = await schedule_repository.get_by_id(
        db, schedule_id, current_user.organization_id
    )
    if entry is None:
        raise NotFoundError("Schedule not found")
    if entry.user_id != current_user.id:
        raise ForbiddenError("Can only view your own schedule")

    # 스케줄 기본 응답 생성
    response_entry = await schedule_service._to_response(db, entry)

    result: dict = {
        "id": response_entry.id,
        "store_id": response_entry.store_id,
        "store_name": response_entry.store_name,
        "work_role_id": response_entry.work_role_id,
        "work_role_name": response_entry.work_role_name,
        "user_id": response_entry.user_id,
        "user_name": response_entry.user_name,
        "work_date": response_entry.work_date,
        "start_time": response_entry.start_time,
        "end_time": response_entry.end_time,
        "break_start_time": response_entry.break_start_time,
        "break_end_time": response_entry.break_end_time,
        "net_work_minutes": response_entry.net_work_minutes,
        "status": response_entry.status,
        "note": response_entry.note,
        "created_at": response_entry.created_at,
    }

    # cl_instance 상세 병합
    cl_instance = await checklist_instance_repository.get_by_schedule_id(
        db, schedule_id
    )
    result["checklist_instance_id"] = str(cl_instance.id) if cl_instance else None
    result["total_items"] = cl_instance.total_items if cl_instance else 0
    result["completed_items"] = cl_instance.completed_items if cl_instance else 0
    result["checklist_status"] = cl_instance.status if cl_instance else None

    if cl_instance:
        detail = await checklist_instance_service.build_detail_response(db, cl_instance)
        result["checklist_snapshot"] = detail.get("items")
        result["reported_at"] = cl_instance.reported_at
    else:
        result["checklist_snapshot"] = None
        result["reported_at"] = None

    return result


@router.patch("/schedules/{schedule_id}/checklist/{item_index}")
async def complete_checklist_item(
    schedule_id: UUID,
    item_index: int,
    data: ChecklistItemComplete,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """스케줄 체크리스트 항목을 완료/미완료 처리합니다.

    Complete or uncomplete a checklist item for a schedule.
    """
    cl_instance = await checklist_instance_repository.get_by_schedule_id(
        db, schedule_id
    )
    if cl_instance is None:
        raise NotFoundError("Checklist not found for this schedule")
    if cl_instance.user_id != current_user.id:
        raise ForbiddenError("Can only modify your own checklist")

    # 매장 타임존 해석
    store_tz = await get_store_timezone(db, cl_instance.store_id)
    effective_tz = resolve_timezone(data.timezone, store_tz)

    if data.is_completed:
        # photo_urls 우선, 없으면 photo_url 하위 호환
        photo_urls = data.photo_urls
        if not photo_urls and data.photo_url:
            photo_urls = [data.photo_url]
        # 완료 처리
        await checklist_instance_service.complete_item(
            db,
            instance_id=cl_instance.id,
            item_index=item_index,
            user_id=current_user.id,
            photo_urls=photo_urls,
            note=data.note,
            client_timezone=effective_tz,
        )
    else:
        # 미완료 처리 (완료 취소)
        await checklist_instance_service.uncomplete_item(
            db,
            instance_id=cl_instance.id,
            item_index=item_index,
            user_id=current_user.id,
        )

    # 업데이트된 스케줄 상세 반환
    return await get_my_schedule(schedule_id, db, current_user)


@router.patch("/schedules/{schedule_id}/checklist/{item_index}/respond")
async def respond_to_rejection(
    schedule_id: UUID,
    item_index: int,
    data: ChecklistItemRespond,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """거절된 체크리스트 항목에 대해 재제출합니다.

    Respond to a rejected checklist item by resubmitting with new evidence.
    """
    cl_instance = await checklist_instance_repository.get_by_schedule_id(
        db, schedule_id
    )
    if cl_instance is None:
        raise NotFoundError("Checklist not found for this schedule")
    if cl_instance.user_id != current_user.id:
        raise ForbiddenError("Can only resubmit your own checklist")

    # 매장 타임존 해석
    store_tz = await get_store_timezone(db, cl_instance.store_id)
    effective_tz = resolve_timezone(data.timezone, store_tz)

    photo_urls = data.photo_urls
    if not photo_urls and data.photo_url:
        photo_urls = [data.photo_url]
    await checklist_instance_service.resubmit_completion(
        db,
        instance_id=cl_instance.id,
        item_index=item_index,
        user_id=current_user.id,
        photo_urls=photo_urls,
        note=data.response_comment,
        client_timezone=effective_tz,
    )

    # 업데이트된 스케줄 상세 반환
    return await get_my_schedule(schedule_id, db, current_user)
