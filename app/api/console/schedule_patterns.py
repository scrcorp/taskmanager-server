"""고정 근무(Fixed Schedule) 패턴 라우터 — prefix `/schedules/patterns` (계약 §4).

`schedules.py` 보다 **먼저** include 해야 한다 — 안 그러면 `/schedules/{entry_id}` 가 "patterns" 를
UUID 로 파싱하려다 422 를 낸다.

스코프: `check_store_access` / `get_accessible_store_ids` 재사용(schedules.py 와 동일).
권한: schedules:read(조회) / schedules:create(생성·검증) / schedules:update(교체·이동·occurrence) /
      schedules:delete(삭제) — schedules.py 의 대응 엔드포인트와 같은 코드.
알림: create/update/move/delete **작업 1회 = 알림 1건**(D-e) `fixed_schedule_changed`. 건별(날짜별) 알림 없음.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    check_store_access,
    get_accessible_store_ids,
    hide_cost_for,
    require_permission,
    scrub_cost_fields,
)
from app.core.error_codes.fixed_schedule import PATTERN_NOT_FOUND
from app.database import get_db
from app.models.user import User
from app.models.work_pattern import StaffWorkPattern
from app.schemas.schedule import ScheduleResponse
from app.schemas.schedule_pattern import (
    MoveGroupIn,
    OccurrenceActionIn,
    PatternGroupIn,
    PatternGroupOut,
    PatternValidateOut,
)
from app.services.alert_service import alert_service
from app.services.fixed_schedule import patterns as pattern_service

router: APIRouter = APIRouter()

_DOW_LABELS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


# ─── 헬퍼 ──────────────────────────────────────────────────────────


async def _group_subject(db: AsyncSession, organization_id: UUID, group_id: UUID) -> tuple[UUID, UUID]:
    """(user_id, store_id) — 스코프 검증용. 이 org 에 없으면 404 PATTERN_NOT_FOUND(존재 비노출)."""
    row = (await db.execute(
        select(StaffWorkPattern.user_id, StaffWorkPattern.store_id).where(
            StaffWorkPattern.group_id == group_id,
            StaffWorkPattern.organization_id == organization_id,
        ).limit(1)
    )).first()
    if row is None:
        raise PATTERN_NOT_FOUND(group_id=str(group_id))
    return row.user_id, row.store_id


async def _pattern_store(db: AsyncSession, organization_id: UUID, pattern_id: UUID) -> UUID:
    store_id = await db.scalar(
        select(StaffWorkPattern.store_id).where(
            StaffWorkPattern.id == pattern_id,
            StaffWorkPattern.organization_id == organization_id,
        )
    )
    if store_id is None:
        raise PATTERN_NOT_FOUND(pattern_id=str(pattern_id))
    return store_id


def group_summary(group: PatternGroupOut) -> str:
    """알림 본문용 그룹 요약 — 블록별 "Mon, Wed, Fri 09:00-17:00" + 기간. 영어 고정(UI 텍스트 규칙)."""
    parts: list[str] = []
    for b in group.blocks:
        days = ", ".join(_DOW_LABELS[d] for d in sorted(b.byday) if 0 <= d <= 6)
        role = f" ({b.work_role_name})" if b.work_role_name else ""
        parts.append(f"{days} {b.start_time}-{b.end_time}{role}")
    period = f"from {group.start_date.isoformat()}"
    if group.until_date is not None:
        period += f" to {group.until_date.isoformat()}"
    where = f" at {group.store_name}" if group.store_name else ""
    return f"{'; '.join(parts)}{where}, {period}"


async def _notify(db: AsyncSession, *, organization_id: UUID, group: PatternGroupOut, action: str) -> None:
    """작업 1회 = 알림 1건. 실패해도 본 작업은 이미 끝났으므로 삼키지 않고 그대로 올린다(조용한 실패 금지)."""
    verb = {
        "created": "set up",
        "updated": "updated",
        "moved": "moved",
        "deleted": "removed",
    }[action]
    await alert_service.create_for_fixed_schedule_changed(
        db,
        organization_id=organization_id,
        user_id=UUID(group.user_id),
        group_id=UUID(group.group_id),
        message=f"Your fixed schedule was {verb}: {group_summary(group)}",
    )
    await db.commit()


def _scrub(resp: ScheduleResponse, user: User) -> ScheduleResponse:
    if hide_cost_for(user):
        scrub_cost_fields(resp)
    return resp


# ─── 조회 ──────────────────────────────────────────────────────────


@router.get("", response_model=list[PatternGroupOut])
async def list_pattern_groups(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
    user_id: UUID = Query(..., description="Staff whose fixed schedules to list"),
    store_id: UUID | None = None,
    include_ended: bool = False,
) -> list[PatternGroupOut]:
    """직원의 고정 근무 그룹 목록(현재 유효 + 예정). store_id 없으면 접근 가능한 매장 전체."""
    if store_id is not None:
        await check_store_access(db, current_user, store_id)
    groups = await pattern_service.list_for_user(
        db, organization_id=current_user.organization_id,
        user_id=user_id, store_id=store_id, include_ended=include_ended,
    )
    if store_id is None:
        accessible = await get_accessible_store_ids(db, current_user)
        if accessible is not None:
            allowed = {str(s) for s in accessible}
            groups = [g for g in groups if g.store_id in allowed]
    return groups


# ─── 생성 / 검증 ───────────────────────────────────────────────────


@router.post("", response_model=PatternGroupOut, status_code=201)
async def create_pattern_group(
    data: PatternGroupIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:create"))],
) -> PatternGroupOut:
    """블록 N개 → 그룹 1개. 창(today..+N주) 즉시 실체화. gate=move|replace 로 기존 그룹 겹침 처리."""
    await check_store_access(db, current_user, UUID(data.store_id))
    group = await pattern_service.create_group(
        db, organization_id=current_user.organization_id, actor=current_user, data=data,
    )
    await _notify(
        db, organization_id=current_user.organization_id, group=group,
        action="moved" if data.gate == "move" else "created",
    )
    return group


@router.post("/validate", response_model=PatternValidateOut)
async def validate_pattern_group(
    data: PatternGroupIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:create"))],
    exclude_group_id: UUID | None = None,
) -> PatternValidateOut:
    """저장 없이 ①블록 겹침 ④availability → errors, ②기존 그룹 겹침 → overlaps."""
    await check_store_access(db, current_user, UUID(data.store_id))
    return await pattern_service.validate_group(
        db, organization_id=current_user.organization_id, data=data, exclude_group_id=exclude_group_id,
    )


# ─── 그룹 단위 ─────────────────────────────────────────────────────


@router.patch("/groups/{group_id}", response_model=PatternGroupOut)
async def update_pattern_group(
    group_id: UUID,
    data: PatternGroupIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> PatternGroupOut:
    """블록 전체 교체(group_id 유지). 진행 중 그룹은 오늘부터 적용(옛 블록은 어제로 종료)."""
    _, store_id = await _group_subject(db, current_user.organization_id, group_id)
    await check_store_access(db, current_user, store_id)
    await check_store_access(db, current_user, UUID(data.store_id))
    group = await pattern_service.update_group(
        db, organization_id=current_user.organization_id, group_id=group_id, actor=current_user, data=data,
    )
    await _notify(db, organization_id=current_user.organization_id, group=group, action="updated")
    return group


@router.post("/groups/{group_id}/move", response_model=PatternGroupOut)
async def move_pattern_group(
    group_id: UUID,
    data: MoveGroupIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> PatternGroupOut:
    """그룹 기간을 delta_days 만큼 이동(시작 전 그룹만)."""
    _, store_id = await _group_subject(db, current_user.organization_id, group_id)
    await check_store_access(db, current_user, store_id)
    group = await pattern_service.move_group(
        db, organization_id=current_user.organization_id, group_id=group_id,
        actor=current_user, delta_days=data.delta_days,
    )
    await _notify(db, organization_id=current_user.organization_id, group=group, action="moved")
    return group


@router.delete("/groups/{group_id}", status_code=204, response_model=None)
async def delete_pattern_group(
    group_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:delete"))],
) -> None:
    """그룹 삭제. 미래 미손댐 자동생성분은 지우고, 그 외 실 행은 일회성으로 남는다."""
    user_id, store_id = await _group_subject(db, current_user.organization_id, group_id)
    await check_store_access(db, current_user, store_id)
    # 삭제 후엔 요약을 만들 수 없으니 먼저 읽어 둔다
    before = next(
        (g for g in await pattern_service.list_for_user(
            db, organization_id=current_user.organization_id, user_id=user_id,
            store_id=store_id, include_ended=True,
        ) if g.group_id == str(group_id)),
        None,
    )
    await pattern_service.delete_group(
        db, organization_id=current_user.organization_id, group_id=group_id, actor=current_user,
    )
    if before is not None:
        await _notify(db, organization_id=current_user.organization_id, group=before, action="deleted")


# ─── occurrence (virtual 한 칸 → 실 행) ──────────────────────────────


@router.post("/{pattern_id}/occurrences/{occurrence_date}", response_model=ScheduleResponse)
async def act_on_occurrence(
    pattern_id: UUID,
    occurrence_date: date,
    data: OccurrenceActionIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> ScheduleResponse:
    """virtual 한 칸을 실 행으로: edit(패치 적용, overridden) / delete(soft delete, 슬롯 점유)."""
    store_id = await _pattern_store(db, current_user.organization_id, pattern_id)
    await check_store_access(db, current_user, store_id)
    resp = await pattern_service.materialize_occurrence(
        db, organization_id=current_user.organization_id, actor=current_user,
        pattern_id=pattern_id, occurrence_date=occurrence_date,
        action=data.action, patch=data.patch,
    )
    return _scrub(resp, current_user)
