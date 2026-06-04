"""공개 인터뷰 스케줄링 — 지원자가 이메일 토큰 링크로 희망 시간을 고른다 (로그인 없음).

인증 = URL 의 서명 토큰. application.interview_token(jti) 매칭으로 회전/무효화 검사.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.models.hiring import Application, Candidate
from app.models.interview import InterviewSlot, InterviewSlotPreference
from app.models.organization import Store
from app.utils.interview_token import decode_interview_token
from app.utils.timezone import get_org_timezone

router = APIRouter(prefix="/interview", tags=["Public Interview Scheduling"])

MAX_PICKS = 3


async def _resolve_token(db: AsyncSession, token: str) -> Application:
    """토큰 → application. 서명/만료/purpose + jti 매칭 검증."""
    try:
        application_id, jti = decode_interview_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=410, detail={"code": "token_expired", "message": "This scheduling link has expired. Please contact the store."})
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=400, detail={"code": "invalid_token"})

    app_obj = (
        await db.execute(select(Application).where(Application.id == application_id))
    ).scalar_one_or_none()
    if app_obj is None or app_obj.interview_token != jti:
        # jti 불일치 = 토큰 회전됨(새 링크 발급) 또는 취소
        raise HTTPException(status_code=400, detail={"code": "invalid_token"})
    return app_obj


def _slot_dict(s: InterviewSlot) -> dict:
    return {
        "id": str(s.id),
        "date": s.slot_date.isoformat(),
        "start": s.start_time.strftime("%H:%M"),
        "end": s.end_time.strftime("%H:%M"),
    }


@router.get("/{token}")
async def get_schedule(token: str, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    """토큰으로 본인 인터뷰 스케줄 화면 데이터 — 열린 슬롯/내 선택/확정 상태."""
    app_obj = await _resolve_token(db, token)
    store = (await db.execute(select(Store).where(Store.id == app_obj.store_id))).scalar_one()
    candidate = (await db.execute(select(Candidate).where(Candidate.id == app_obj.candidate_id))).scalar_one()
    org_tz = await get_org_timezone(db, store.organization_id)

    slots = (
        await db.execute(
            select(InterviewSlot)
            .where(InterviewSlot.organization_id == store.organization_id)
            .order_by(InterviewSlot.slot_date, InterviewSlot.start_time)
        )
    ).scalars().all()

    # 다른 지원자에게 확정된 슬롯 = taken (회색 처리)
    taken_rows = (
        await db.execute(
            select(Application.confirmed_slot_id).where(
                Application.confirmed_slot_id.is_not(None),
                Application.id != app_obj.id,
            )
        )
    ).all()
    taken = {r[0] for r in taken_rows}

    my_picks = {
        r[0]
        for r in (
            await db.execute(
                select(InterviewSlotPreference.slot_id).where(
                    InterviewSlotPreference.application_id == app_obj.id
                )
            )
        ).all()
    }

    confirmed = None
    if app_obj.confirmed_slot_id:
        cs = next((s for s in slots if s.id == app_obj.confirmed_slot_id), None)
        if cs:
            confirmed = _slot_dict(cs)

    status = "confirmed" if app_obj.confirmed_slot_id else ("picked" if my_picks else "pending")
    return {
        "store": {"id": str(store.id), "name": store.name, "timezone": org_tz},
        "applicant_first_name": candidate.full_name.split(" ")[0] if candidate.full_name else "",
        "status": status,
        "max_picks": MAX_PICKS,
        "slots": [
            {**_slot_dict(s), "taken": s.id in taken, "picked": s.id in my_picks}
            for s in slots
        ],
        "confirmed": confirmed,
    }


class PickBody(BaseModel):
    slot_ids: list[UUID] = Field(default_factory=list)


@router.post("/{token}/preferences")
async def submit_preferences(
    token: str, body: PickBody, db: Annotated[AsyncSession, Depends(get_db)]
) -> dict:
    """희망 슬롯 제출 (최대 3개, 기존 선택 대체). 확정된 뒤엔 불가."""
    app_obj = await _resolve_token(db, token)

    if app_obj.confirmed_slot_id:
        raise HTTPException(status_code=409, detail={"code": "already_confirmed", "message": "Your interview is already confirmed."})
    if app_obj.stage != "interview":
        raise HTTPException(status_code=409, detail={"code": "not_in_interview", "message": "This application is not in the interview stage."})

    picks = list(dict.fromkeys(body.slot_ids))  # dedupe, preserve order
    if len(picks) == 0:
        raise HTTPException(status_code=400, detail={"code": "no_picks", "message": "Pick at least one time."})
    if len(picks) > MAX_PICKS:
        raise HTTPException(status_code=400, detail={"code": "too_many", "message": f"Pick at most {MAX_PICKS} times."})

    # 슬롯들이 이 org 소속 & 다른 지원자에 확정되지 않았는지
    store = (await db.execute(select(Store).where(Store.id == app_obj.store_id))).scalar_one()
    valid = (
        await db.execute(
            select(InterviewSlot.id).where(
                InterviewSlot.id.in_(picks),
                InterviewSlot.organization_id == store.organization_id,
            )
        )
    ).all()
    valid_ids = {r[0] for r in valid}
    if valid_ids != set(picks):
        raise HTTPException(status_code=400, detail={"code": "invalid_slot", "message": "Some times are no longer available."})

    taken = (
        await db.execute(
            select(Application.confirmed_slot_id).where(
                Application.confirmed_slot_id.in_(picks),
                Application.id != app_obj.id,
            )
        )
    ).all()
    if taken:
        raise HTTPException(status_code=409, detail={"code": "slot_taken", "message": "One of those times was just booked. Please pick again."})

    # 기존 선호 삭제 후 재삽입 (rank = 선택 순서)
    await db.execute(
        delete(InterviewSlotPreference).where(InterviewSlotPreference.application_id == app_obj.id)
    )
    for i, sid in enumerate(picks):
        db.add(InterviewSlotPreference(application_id=app_obj.id, slot_id=sid, rank=i + 1))
    await db.commit()
    return {"status": "picked", "count": len(picks)}
