"""Staff app용 issue_report LinkPicker 데이터 통합 endpoint.

GET /app/my/stores/{store_id}/link-options
    매장 단위로 issue_report 작성 시 선택할 수 있는 5개 카테고리의 옵션을
    한 번에 반환. console과 동등한 표시 정보 포함.
"""

from datetime import date, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.checklist import ChecklistInstance, ChecklistTemplate
from app.models.schedule import Schedule, StoreWorkRole
from app.models.user import User
from app.models.user_store import UserStore
from app.models.work import Position

router: APIRouter = APIRouter()


@router.get("/{store_id}/link-options")
async def get_link_options(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    days_back: int = 14,
) -> dict:
    """매장에 속한 스케줄/체크리스트/포지션/work_role/직원 목록을 한 번에 반환.

    호출자는 해당 매장에 속해 있어야 한다 (UserStore 매핑 검증).
    """
    # 권한: 호출자가 매장 소속이어야 함
    membership = await db.execute(
        select(UserStore.id).where(
            UserStore.user_id == current_user.id,
            UserStore.store_id == store_id,
        ).limit(1)
    )
    if membership.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=403,
            detail="You are not assigned to this store",
        )

    today = date.today()
    date_from = today - timedelta(days=days_back)

    # Schedules: 매장의 최근 N일
    sched_rows = await db.execute(
        select(Schedule).where(
            Schedule.store_id == store_id,
            Schedule.work_date >= date_from,
            Schedule.work_date <= today,
        ).order_by(Schedule.work_date.desc())
    )
    schedules = sched_rows.scalars().all()
    # work_role / position / user 이름은 schedule 모델이 이미 join 되어있지 않을 수 있어 별도 fetch
    role_ids = {s.work_role_id for s in schedules if s.work_role_id}
    user_ids_s = {s.user_id for s in schedules if s.user_id}
    role_name_map: dict[UUID, str | None] = {}
    user_name_map: dict[UUID, str | None] = {}
    if role_ids:
        rows = await db.execute(
            select(StoreWorkRole.id, StoreWorkRole.name).where(StoreWorkRole.id.in_(role_ids))
        )
        role_name_map = {r.id: r.name for r in rows.all()}
    if user_ids_s:
        rows = await db.execute(
            select(User.id, User.full_name, User.username).where(User.id.in_(user_ids_s))
        )
        user_name_map = {r.id: r.full_name or r.username for r in rows.all()}
    schedule_items = [
        {
            "id": str(s.id),
            "work_date": s.work_date.isoformat(),
            "start_time": s.start_time.strftime("%H:%M") if s.start_time else None,
            "end_time": s.end_time.strftime("%H:%M") if s.end_time else None,
            "work_role_name": role_name_map.get(s.work_role_id) if s.work_role_id else None,
            "position_snapshot": s.position_snapshot,
            "user_id": str(s.user_id) if s.user_id else None,
            "user_name": user_name_map.get(s.user_id) if s.user_id else None,
        }
        for s in schedules
    ]

    # Checklist Instances: 매장의 최근 N일
    cl_rows = await db.execute(
        select(ChecklistInstance, ChecklistTemplate.title)
        .outerjoin(ChecklistTemplate, ChecklistTemplate.id == ChecklistInstance.template_id)
        .where(
            ChecklistInstance.store_id == store_id,
            ChecklistInstance.work_date >= date_from,
            ChecklistInstance.work_date <= today,
        )
        .order_by(ChecklistInstance.work_date.desc())
    )
    cl_data = cl_rows.all()
    cl_user_ids = {c.user_id for c, _t in cl_data if c.user_id}
    cl_user_map: dict[UUID, str | None] = {}
    if cl_user_ids:
        rows = await db.execute(
            select(User.id, User.full_name, User.username).where(User.id.in_(cl_user_ids))
        )
        cl_user_map = {r.id: r.full_name or r.username for r in rows.all()}
    checklist_items = [
        {
            "id": str(c.id),
            "work_date": c.work_date.isoformat(),
            "template_title": tpl_title,
            "user_id": str(c.user_id) if c.user_id else None,
            "user_name": cl_user_map.get(c.user_id) if c.user_id else None,
            "total_items": c.total_items,
            "completed_items": c.completed_items,
            "status": c.status,
        }
        for c, tpl_title in cl_data
    ]

    # Positions
    pos_rows = await db.execute(
        select(Position).where(Position.store_id == store_id).order_by(Position.sort_order)
    )
    positions = [
        {"id": str(p.id), "name": p.name, "sort_order": p.sort_order}
        for p in pos_rows.scalars().all()
    ]

    # Work Roles (active만)
    wr_rows = await db.execute(
        select(StoreWorkRole)
        .where(StoreWorkRole.store_id == store_id, StoreWorkRole.is_active.is_(True))
        .order_by(StoreWorkRole.sort_order)
    )
    work_roles_raw = wr_rows.scalars().all()
    # position_name lookup for work_role
    wr_pos_ids = {w.position_id for w in work_roles_raw if w.position_id}
    wr_pos_map: dict[UUID, str] = {}
    if wr_pos_ids:
        rows = await db.execute(
            select(Position.id, Position.name).where(Position.id.in_(wr_pos_ids))
        )
        wr_pos_map = {r.id: r.name for r in rows.all()}
    work_roles = [
        {
            "id": str(w.id),
            "name": w.name,
            "position_id": str(w.position_id) if w.position_id else None,
            "position_name": wr_pos_map.get(w.position_id) if w.position_id else None,
            "shift_id": str(w.shift_id) if w.shift_id else None,
        }
        for w in work_roles_raw
    ]

    # Users in store + primary work_role/position 정보
    us_rows = await db.execute(
        select(UserStore, User)
        .join(User, User.id == UserStore.user_id)
        .where(
            UserStore.store_id == store_id,
            User.is_active.is_(True),
        )
    )
    us_data = us_rows.all()
    # role_name (User.role) lookup — User has role_id
    role_user_ids = {u.role_id for _us, u in us_data if u.role_id}
    user_role_map: dict[UUID, str] = {}
    if role_user_ids:
        from app.models.user import Role
        rows = await db.execute(
            select(Role.id, Role.name).where(Role.id.in_(role_user_ids))
        )
        user_role_map = {r.id: r.name for r in rows.all()}
    # primary_work_role + primary_position name lookup
    primary_role_ids = {us.primary_work_role_id for us, _u in us_data if us.primary_work_role_id}
    primary_pos_ids = {us.primary_position_id for us, _u in us_data if us.primary_position_id}
    primary_role_map: dict[UUID, str | None] = {}
    primary_pos_map: dict[UUID, str] = {}
    if primary_role_ids:
        rows = await db.execute(
            select(StoreWorkRole.id, StoreWorkRole.name).where(
                StoreWorkRole.id.in_(primary_role_ids)
            )
        )
        primary_role_map = {r.id: r.name for r in rows.all()}
    if primary_pos_ids:
        rows = await db.execute(
            select(Position.id, Position.name).where(Position.id.in_(primary_pos_ids))
        )
        primary_pos_map = {r.id: r.name for r in rows.all()}

    users_out = [
        {
            "id": str(u.id),
            "username": u.username,
            "full_name": u.full_name,
            "role_name": user_role_map.get(u.role_id, ""),
            "primary_work_role_name": (
                primary_role_map.get(us.primary_work_role_id)
                if us.primary_work_role_id
                else None
            ),
            "primary_position_name": (
                primary_pos_map.get(us.primary_position_id)
                if us.primary_position_id
                else None
            ),
        }
        for us, u in us_data
    ]

    return {
        "schedules": schedule_items,
        "checklist_instances": checklist_items,
        "positions": positions,
        "work_roles": work_roles,
        "users": users_out,
    }
