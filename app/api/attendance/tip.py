"""Attendance device tip-entry 라우터 — clock-out 직후 팁 입력 + 분배 후보 조회.

`/api/v1/attendance` 하위에 mount.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_attendance_device
from app.database import get_db
from app.models.attendance import Attendance
from app.models.attendance_device import AttendanceDevice
from app.schemas.tip import TipEntryCreate
from app.services.attendance_device_service import attendance_device_service
from app.services.tip_service import tip_service


router: APIRouter = APIRouter()


@router.post("/tip-entry", status_code=201)
async def device_tip_entry(
    data: dict,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """직원이 attendance device 에서 clock-out 직후 팁 입력.

    device token + 본인 PIN 으로 인증. tip_service.create_entry 호출.
    schedule_id 는 body 로 받거나, 가장 최근 attendance 의 schedule 로 자동 derive.

    body: {
        user_id, pin,
        schedule_id (optional — 없으면 자동 derive),
        card_tips, cash_tips_kept,
        distributions: [{receiver_id, amount, reason}],
    }
    """
    if device.store_id is None:
        raise HTTPException(status_code=400, detail="Device has no store assigned")

    user_id_raw = data.get("user_id")
    pin = data.get("pin")
    if not user_id_raw or not pin:
        raise HTTPException(status_code=400, detail="user_id and pin required")

    user_id = uuid.UUID(str(user_id_raw))
    user = await attendance_device_service.verify_user_pin(
        db, user_id, str(pin), device.organization_id,
    )

    # schedule_id 자동 derive — body 우선, 없으면 user 의 가장 최근 attendance (clock-out
    # 직후 진입을 가정).
    schedule_id_raw = data.get("schedule_id")
    if schedule_id_raw:
        schedule_id = uuid.UUID(str(schedule_id_raw))
    else:
        latest_att = await db.scalar(
            select(Attendance)
            .where(
                Attendance.user_id == user_id,
                Attendance.store_id == device.store_id,
                Attendance.schedule_id.is_not(None),
            )
            .where(Attendance.clock_in.is_not(None)).order_by(Attendance.clock_in.desc())
            .limit(1)
        )
        if latest_att is None or latest_att.schedule_id is None:
            raise HTTPException(
                status_code=400,
                detail="Could not match this clock-out to a schedule. Use the staff app to submit.",
            )
        schedule_id = latest_att.schedule_id

    payload = TipEntryCreate(
        schedule_id=schedule_id,
        card_tips=data.get("card_tips", "0"),
        cash_tips_kept=data.get("cash_tips_kept", "0"),
        source="attendance",
        distributions=data.get("distributions", []),
    )
    entry = await tip_service.create_entry(db, actor=user, payload=payload)
    entry = await tip_service._get_entry_with_dists(db, entry.id)
    return tip_service.build_entry_response(
        entry, schedule=getattr(entry, "_schedule_loaded", None),
    )


@router.post("/tip-entry/eligible-receivers")
async def device_tip_eligible_receivers(
    data: dict,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    """키오스크용 분배 후보 조회 — PIN 인증 후 같은 매장/같은 날/시간 겹친 staff.

    body: { user_id, pin, schedule_id (optional — 가장 최근 attendance 의 schedule 자동 derive) }
    """
    if device.store_id is None:
        raise HTTPException(status_code=400, detail="Device has no store assigned")

    user_id_raw = data.get("user_id")
    pin = data.get("pin")
    if not user_id_raw or not pin:
        raise HTTPException(status_code=400, detail="user_id and pin required")

    user_id = uuid.UUID(str(user_id_raw))
    user = await attendance_device_service.verify_user_pin(
        db, user_id, str(pin), device.organization_id,
    )

    schedule_id_raw = data.get("schedule_id")
    if schedule_id_raw:
        schedule_id = uuid.UUID(str(schedule_id_raw))
    else:
        latest_att = await db.scalar(
            select(Attendance)
            .where(
                Attendance.user_id == user_id,
                Attendance.store_id == device.store_id,
                Attendance.schedule_id.is_not(None),
            )
            .where(Attendance.clock_in.is_not(None)).order_by(Attendance.clock_in.desc())
            .limit(1)
        )
        if latest_att is None or latest_att.schedule_id is None:
            raise HTTPException(
                status_code=400,
                detail="Could not match this clock-out to a schedule.",
            )
        schedule_id = latest_att.schedule_id

    return await tip_service.get_eligible_receivers(
        db,
        schedule_id=schedule_id,
        asking_user_id=user.id,
        organization_id=device.organization_id,
    )


@router.get("/store-employees")
async def device_store_employees(
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    """device 매장의 active 직원 전체 — manual tip receiver 추가용 (L5).

    payload 작아서 한 번에 가져온 후 client-side 검색 필터. 100명 이내 매장이면
    수 KB 수준이라 paging 불필요. 매장 직원이 500+ 인 케이스가 생기면 그때
    server-side search 도입.
    """
    from app.models.user import User

    if device.store_id is None:
        raise HTTPException(status_code=400, detail="Device has no store assigned")

    # 같은 store 에 user_stores 또는 user.primary_store 로 묶인 직원.
    # 단순화: same organization + (user_stores 에 store_id 매핑) + active + non-deleted.
    from app.models.user_store import UserStore

    rows = await db.execute(
        select(User)
        .join(UserStore, UserStore.user_id == User.id)
        .where(
            UserStore.store_id == device.store_id,
            User.organization_id == device.organization_id,
            User.is_active.is_(True),
            User.deleted_at.is_(None),
        )
        .order_by(User.full_name.asc())
        .distinct()
    )
    return [
        {"id": str(u.id), "full_name": u.full_name or u.username}
        for u in rows.scalars().all()
    ]
