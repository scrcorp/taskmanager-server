"""Console 인터뷰 스케줄링 라우터 — org 통합 가용 슬롯 관리 + 확정/취소 + 토큰 발급.

슬롯은 org-local 벽시계 (2026-06-01: 매장별 → org 통합). 확정 시 org timezone 으로 UTC 변환.
권한: hiring:update. 슬롯은 org 스코프(현재 user 의 organization_id), 확정/취소는 check_store_access.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Annotated, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import check_store_access, get_db, require_permission
from app.models.hiring import Application, Candidate
from app.models.interview import InterviewSlot, InterviewSlotPreference
from app.models.user import User
from app.utils.interview_token import issue_interview_token
from app.utils.timezone import get_org_timezone

router = APIRouter(prefix="/hiring", tags=["Admin Hiring Interviews"])


def _hhmm(t: time) -> str:
    return t.strftime("%H:%M")


def _parse_hhmm(s: str) -> time:
    try:
        h, m = s.split(":")
        return time(int(h), int(m))
    except Exception:
        raise HTTPException(status_code=400, detail={"code": "invalid_time", "message": f"Bad time: {s}"})


async def wallclock_to_utc(db: AsyncSession, org_id: UUID, d: date, t: time) -> datetime:
    """org-local 벽시계 → UTC. org timezone 으로 해석."""
    tz = await get_org_timezone(db, org_id)
    local = datetime.combine(d, t, tzinfo=ZoneInfo(tz))
    return local.astimezone(timezone.utc)


# ────────────────────────────────────────────────────────────────
# Slots (org 통합)
# ────────────────────────────────────────────────────────────────
class SlotIn(BaseModel):
    date: str  # YYYY-MM-DD
    start: str  # HH:MM
    end: str  # HH:MM


class BulkSlotsBody(BaseModel):
    slots: list[SlotIn] = Field(default_factory=list)


@router.get("/interview-slots")
async def list_slots(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:read"))],
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> dict:
    """org 슬롯 목록 + 슬롯별 수요(누가 원하는지) + 확정 여부. start/end(YYYY-MM-DD)로 범위 필터."""
    stmt = select(InterviewSlot).where(InterviewSlot.organization_id == current_user.organization_id)
    if start:
        stmt = stmt.where(InterviewSlot.slot_date >= date.fromisoformat(start))
    if end:
        stmt = stmt.where(InterviewSlot.slot_date <= date.fromisoformat(end))
    stmt = stmt.order_by(InterviewSlot.slot_date, InterviewSlot.start_time)
    slots = (await db.execute(stmt)).scalars().all()
    slot_ids = [s.id for s in slots]

    # 슬롯별 선호자 (application + candidate name)
    wanters: dict[UUID, list[dict]] = {sid: [] for sid in slot_ids}
    if slot_ids:
        pref_rows = await db.execute(
            select(InterviewSlotPreference.slot_id, Application.id, Candidate.full_name)
            .join(Application, Application.id == InterviewSlotPreference.application_id)
            .join(Candidate, Candidate.id == Application.candidate_id)
            .where(
                InterviewSlotPreference.slot_id.in_(slot_ids),
                # 이미 확정된 지원자는 다른 희망 슬롯에서 해제 (booked → 다른 픽은 demand 아님)
                Application.confirmed_slot_id.is_(None),
            )
        )
        for sid, app_id, name in pref_rows.all():
            wanters[sid].append({"application_id": str(app_id), "candidate_name": name})

    # 확정된 슬롯 → application
    booked: dict[UUID, dict] = {}
    if slot_ids:
        book_rows = await db.execute(
            select(Application.confirmed_slot_id, Application.id, Candidate.full_name, User.full_name)
            .join(Candidate, Candidate.id == Application.candidate_id)
            .outerjoin(User, User.id == Application.interviewer_id)
            .where(Application.confirmed_slot_id.in_(slot_ids))
        )
        for sid, app_id, name, iv_name in book_rows.all():
            booked[sid] = {"application_id": str(app_id), "candidate_name": name, "interviewer_name": iv_name}

    return {
        "items": [
            {
                "id": str(s.id),
                "date": s.slot_date.isoformat(),
                "start": _hhmm(s.start_time),
                "end": _hhmm(s.end_time),
                "demand": len(wanters.get(s.id, [])),
                "wanters": wanters.get(s.id, []),
                "confirmed": booked.get(s.id),
            }
            for s in slots
        ]
    }


@router.post("/interview-slots")
async def create_slots(
    body: BulkSlotsBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:update"))],
) -> dict:
    """슬롯 벌크 생성 (upsert — 이미 있는 시각은 skip). 요일×시간 그리드/주 단위 세팅용. org 통합."""
    org_id = current_user.organization_id
    existing = {
        (s.slot_date, s.start_time)
        for s in (
            await db.execute(select(InterviewSlot).where(InterviewSlot.organization_id == org_id))
        ).scalars().all()
    }
    created = 0
    for sl in body.slots:
        d = date.fromisoformat(sl.date)
        st = _parse_hhmm(sl.start)
        et = _parse_hhmm(sl.end)
        if (d, st) in existing:
            continue
        db.add(InterviewSlot(
            organization_id=org_id, slot_date=d, start_time=st, end_time=et,
            created_by_user_id=current_user.id,
        ))
        existing.add((d, st))
        created += 1
    await db.commit()
    return {"created": created}


@router.delete("/interview-slots/{slot_id}")
async def delete_slot(
    slot_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:update"))],
) -> dict:
    """슬롯 삭제. 확정된 슬롯은 삭제 불가 (먼저 확정 취소)."""
    slot = (await db.execute(select(InterviewSlot).where(InterviewSlot.id == slot_id))).scalar_one_or_none()
    if slot is None:
        raise HTTPException(status_code=404, detail={"code": "slot_not_found"})
    if slot.organization_id != current_user.organization_id:
        raise HTTPException(status_code=404, detail={"code": "slot_not_found"})
    booked = (
        await db.execute(select(Application.id).where(Application.confirmed_slot_id == slot_id))
    ).first()
    if booked is not None:
        raise HTTPException(
            status_code=400,
            detail={"code": "slot_confirmed", "message": "Cancel the confirmed interview before deleting this slot."},
        )
    await db.execute(delete(InterviewSlot).where(InterviewSlot.id == slot_id))
    await db.commit()
    return {"deleted": True}


# ────────────────────────────────────────────────────────────────
# Application interview detail / confirm / cancel / token
# ────────────────────────────────────────────────────────────────
async def _load_app(db: AsyncSession, application_id: UUID) -> Application:
    app_obj = (
        await db.execute(select(Application).where(Application.id == application_id))
    ).scalar_one_or_none()
    if app_obj is None:
        raise HTTPException(status_code=404, detail={"code": "application_not_found"})
    return app_obj


@router.get("/applications/{application_id}/interview")
async def get_interview(
    application_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:read"))],
) -> dict:
    """지원자의 인터뷰 상태 — 희망 슬롯, 확정 슬롯, 인터뷰어."""
    app_obj = await _load_app(db, application_id)
    await check_store_access(db, current_user, app_obj.store_id)

    prefs = (
        await db.execute(
            select(InterviewSlot)
            .join(InterviewSlotPreference, InterviewSlotPreference.slot_id == InterviewSlot.id)
            .where(InterviewSlotPreference.application_id == application_id)
            .order_by(InterviewSlot.slot_date, InterviewSlot.start_time)
        )
    ).scalars().all()

    def slot_dict(s: InterviewSlot) -> dict:
        return {"id": str(s.id), "date": s.slot_date.isoformat(), "start": _hhmm(s.start_time), "end": _hhmm(s.end_time)}

    confirmed = None
    if app_obj.confirmed_slot_id:
        cs = (await db.execute(select(InterviewSlot).where(InterviewSlot.id == app_obj.confirmed_slot_id))).scalar_one_or_none()
        if cs:
            confirmed = slot_dict(cs)

    status = "confirmed" if app_obj.confirmed_slot_id else ("picked" if prefs else "pending")
    return {
        "application_id": str(application_id),
        "status": status,
        "preferences": [slot_dict(s) for s in prefs],
        "confirmed": confirmed,
        "interviewer_id": str(app_obj.interviewer_id) if app_obj.interviewer_id else None,
        "interview_at": app_obj.interview_at.isoformat() if app_obj.interview_at else None,
        "has_token": bool(app_obj.interview_token),
    }


class ConfirmBody(BaseModel):
    slot_id: UUID
    interviewer_id: Optional[UUID] = None


class InterviewerBody(BaseModel):
    interviewer_id: Optional[UUID] = None


def _append_interview_history(app_obj: Application, user: User, action: str, **fields) -> None:
    """interview 관련 audit row를 applications.history에 append (hiring.py와 동일 포맷)."""
    from app.api.console.hiring import _append_history, _now_iso
    _append_history(app_obj, {
        "action": action,
        **{k: v for k, v in fields.items() if v is not None},
        "by_user_id": str(user.id),
        "by_username": user.username,
        "by_full_name": user.full_name,
        "at": _now_iso(),
    })


@router.post("/applications/{application_id}/interview/confirm")
async def confirm_interview(
    application_id: UUID,
    body: ConfirmBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:update"))],
) -> dict:
    """슬롯 확정 + 인터뷰어 배정. interview_at(UTC) 세팅. 슬롯이 다른 지원자에 이미 확정됐으면 거부."""
    app_obj = await _load_app(db, application_id)
    await check_store_access(db, current_user, app_obj.store_id)

    slot = (await db.execute(select(InterviewSlot).where(InterviewSlot.id == body.slot_id))).scalar_one_or_none()
    if slot is None or slot.organization_id != current_user.organization_id:
        raise HTTPException(status_code=400, detail={"code": "invalid_slot"})

    # 다른 application 이 이미 이 슬롯을 확정했는지
    other = (
        await db.execute(
            select(Application.id).where(
                Application.confirmed_slot_id == body.slot_id,
                Application.id != application_id,
            )
        )
    ).first()
    if other is not None:
        raise HTTPException(status_code=409, detail={"code": "slot_taken", "message": "This slot is already confirmed for another applicant."})

    # 인터뷰어 검증 (선택) — 해당 매장 접근 가능한 user 여야
    if body.interviewer_id is not None:
        iv = (await db.execute(select(User).where(User.id == body.interviewer_id))).scalar_one_or_none()
        if iv is None or iv.organization_id != current_user.organization_id:
            raise HTTPException(status_code=400, detail={"code": "invalid_interviewer"})

    prev_slot_id = app_obj.confirmed_slot_id
    prev_interviewer_id = app_obj.interviewer_id

    app_obj.confirmed_slot_id = body.slot_id
    app_obj.interviewer_id = body.interviewer_id
    app_obj.interview_at = await wallclock_to_utc(db, current_user.organization_id, slot.slot_date, slot.start_time)

    # audit log (confirm vs reschedule)
    new_when = f"{slot.slot_date.isoformat()} {_hhmm(slot.start_time)}"
    iv_name = None
    if body.interviewer_id is not None:
        iv_obj = (await db.execute(select(User).where(User.id == body.interviewer_id))).scalar_one_or_none()
        iv_name = iv_obj.full_name if iv_obj else None
    is_reschedule = bool(prev_slot_id and prev_slot_id != body.slot_id)
    if is_reschedule:
        prev_slot = (await db.execute(select(InterviewSlot).where(InterviewSlot.id == prev_slot_id))).scalar_one_or_none()
        before_when = f"{prev_slot.slot_date.isoformat()} {_hhmm(prev_slot.start_time)}" if prev_slot else None
        _append_interview_history(app_obj, current_user, "interview_rescheduled", before=before_when, after=new_when, interviewer=iv_name)
    else:
        _append_interview_history(app_obj, current_user, "interview_confirmed", after=new_when, interviewer=iv_name)

    await db.commit()

    # 메일 (best-effort) — 시간 변경이면 "변경됨", 신규 확정이면 "확정" 메일
    from app.services.interview_email_service import send_confirmation, send_reschedule
    if is_reschedule:
        await send_reschedule(db, app_obj, slot)
    else:
        await send_confirmation(db, app_obj, slot)

    return {
        "application_id": str(application_id),
        "confirmed_slot_id": str(body.slot_id),
        "interview_at": app_obj.interview_at.isoformat(),
        "interviewer_id": str(body.interviewer_id) if body.interviewer_id else None,
    }


@router.post("/applications/{application_id}/interview/cancel")
async def cancel_interview(
    application_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:update"))],
) -> dict:
    """확정 취소 — 슬롯 해제. 지원자는 다시 '선택 완료' 상태로 (희망 슬롯은 유지)."""
    app_obj = await _load_app(db, application_id)
    await check_store_access(db, current_user, app_obj.store_id)
    prev_when = None
    prev_slot = None
    if app_obj.confirmed_slot_id:
        prev_slot = (await db.execute(select(InterviewSlot).where(InterviewSlot.id == app_obj.confirmed_slot_id))).scalar_one_or_none()
        prev_when = f"{prev_slot.slot_date.isoformat()} {_hhmm(prev_slot.start_time)}" if prev_slot else None
    app_obj.confirmed_slot_id = None
    app_obj.interviewer_id = None
    app_obj.interview_at = None
    _append_interview_history(app_obj, current_user, "interview_cancelled", before=prev_when)
    await db.commit()

    # 취소 메일 (best-effort) — 확정됐던 인터뷰가 있을 때만
    if prev_slot is not None:
        from app.services.interview_email_service import send_cancellation
        await send_cancellation(db, app_obj, prev_slot)

    return {"application_id": str(application_id), "confirmed_slot_id": None}


@router.patch("/applications/{application_id}/interview/interviewer")
async def update_interviewer(
    application_id: UUID,
    body: InterviewerBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:update"))],
) -> dict:
    """인터뷰어만 변경 (시간/확정은 그대로). 새 면접관이 배정되면 지원자에게 변경 메일 발송."""
    app_obj = await _load_app(db, application_id)
    await check_store_access(db, current_user, app_obj.store_id)
    prev_id = app_obj.interviewer_id
    new_name = None
    if body.interviewer_id is not None:
        iv = (await db.execute(select(User).where(User.id == body.interviewer_id))).scalar_one_or_none()
        if iv is None or iv.organization_id != current_user.organization_id:
            raise HTTPException(status_code=400, detail={"code": "invalid_interviewer"})
        new_name = iv.full_name
    prev_name = None
    if prev_id is not None:
        pv = (await db.execute(select(User).where(User.id == prev_id))).scalar_one_or_none()
        prev_name = pv.full_name if pv else None
    app_obj.interviewer_id = body.interviewer_id
    _append_interview_history(app_obj, current_user, "interviewer", before=prev_name, after=new_name)
    await db.commit()

    # 인터뷰어 변경 메일 (best-effort) — 실제로 새 면접관이 배정됐고, 값이 바뀐 경우만
    changed = body.interviewer_id is not None and body.interviewer_id != prev_id
    if changed:
        slot = None
        if app_obj.confirmed_slot_id:
            slot = (await db.execute(select(InterviewSlot).where(InterviewSlot.id == app_obj.confirmed_slot_id))).scalar_one_or_none()
        from app.services.interview_email_service import send_interviewer_update
        await send_interviewer_update(db, app_obj, slot)

    return {"application_id": str(application_id), "interviewer_id": str(body.interviewer_id) if body.interviewer_id else None}


@router.post("/applications/{application_id}/interview/issue-token")
async def issue_token(
    application_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:update"))],
) -> dict:
    """지원자용 인터뷰 링크 토큰 발급(회전). Phase 3 이메일 발송에서 사용. 기존 토큰은 무효화됨."""
    app_obj = await _load_app(db, application_id)
    await check_store_access(db, current_user, app_obj.store_id)
    token, jti = issue_interview_token(application_id)
    app_obj.interview_token = jti
    await db.commit()
    return {"token": token}
