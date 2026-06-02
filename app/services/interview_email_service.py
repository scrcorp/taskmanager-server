"""인터뷰 스케줄링 이메일 — 요청(초대) + 확정. 모두 best-effort (실패해도 흐름 안 깨짐).

발송은 호출 측 트랜잭션과 분리: invite 는 토큰 jti 를 application 에 세팅하므로 호출 측이 commit 한다.
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.hiring import Application, Candidate
from app.models.interview import InterviewSlot
from app.models.organization import Store
from app.models.user import User
from app.utils.email import send_email
from app.utils.email_templates import (
    build_interview_cancellation_email,
    build_interview_confirmation_email,
    build_interview_interviewer_email,
    build_interview_invite_email,
    build_interview_reschedule_email,
)
from app.utils.interview_token import issue_interview_token
from app.utils.timezone import get_store_timezone

logger = logging.getLogger("uvicorn.error")


async def issue_and_send_invite(db: AsyncSession, app_obj: Application) -> None:
    """인터뷰 토큰 발급(회전) + app_obj.interview_token 세팅 + 초대 메일 발송.

    호출 측이 이후 commit 해야 토큰이 저장된다. 메일 발송은 best-effort.
    """
    token, jti = issue_interview_token(app_obj.id)
    app_obj.interview_token = jti  # caller commits

    try:
        candidate = (
            await db.execute(select(Candidate).where(Candidate.id == app_obj.candidate_id))
        ).scalar_one()
        store = (await db.execute(select(Store).where(Store.id == app_obj.store_id))).scalar_one()
        tz = await get_store_timezone(db, app_obj.store_id)
        first = candidate.full_name.split(" ")[0] if candidate.full_name else "there"
        link = f"{settings.ADMIN_BASE_URL.rstrip('/')}/interview/{token}"
        subject, html = build_interview_invite_email(first, store.name, link, tz)
        await send_email(to=candidate.email, subject=subject, html=html)
    except Exception as e:  # noqa: BLE001 — 메일 실패가 단계 전환을 막지 않음
        logger.warning(f"[interview_invite] send failed for application {app_obj.id}: {e}")


async def _slot_when_label(db: AsyncSession, store_id: UUID, slot: InterviewSlot) -> str:
    """슬롯 시각을 store-local 라벨로 ("Mon, Jul 6 · 10:00 AM PDT")."""
    tz = await get_store_timezone(db, store_id)
    local = datetime.combine(slot.slot_date, slot.start_time, tzinfo=ZoneInfo(tz))
    return local.strftime("%a, %b %-d · %-I:%M %p ") + tz


async def _interviewer_label(db: AsyncSession, app_obj: Application) -> str | None:
    """인터뷰어 표시 라벨 — "Mina Park (GM)". 역할은 selectinload 로 안전 로드(lazy 회피)."""
    if not app_obj.interviewer_id:
        return None
    iv = (
        await db.execute(
            select(User).options(selectinload(User.role)).where(User.id == app_obj.interviewer_id)
        )
    ).scalar_one_or_none()
    if iv is None:
        return None
    role_name = iv.role.name if iv.role else None
    return f"{iv.full_name} ({role_name})" if role_name else iv.full_name


async def send_confirmation(db: AsyncSession, app_obj: Application, slot: InterviewSlot) -> None:
    """확정 메일 — 확정된 슬롯 시각(store-local) + 인터뷰어. best-effort."""
    try:
        candidate = (
            await db.execute(select(Candidate).where(Candidate.id == app_obj.candidate_id))
        ).scalar_one()
        store = (await db.execute(select(Store).where(Store.id == app_obj.store_id))).scalar_one()
        first = candidate.full_name.split(" ")[0] if candidate.full_name else "there"
        when_label = await _slot_when_label(db, app_obj.store_id, slot)
        subject, html = build_interview_confirmation_email(
            first, store.name, when_label, await _interviewer_label(db, app_obj)
        )
        await send_email(to=candidate.email, subject=subject, html=html)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[interview_confirmation] send failed for application {app_obj.id}: {e}")


async def send_reschedule(db: AsyncSession, app_obj: Application, slot: InterviewSlot) -> None:
    """일정 변경 메일 — 확정 시간이 다른 슬롯으로 바뀌었을 때. best-effort."""
    try:
        candidate = (
            await db.execute(select(Candidate).where(Candidate.id == app_obj.candidate_id))
        ).scalar_one()
        store = (await db.execute(select(Store).where(Store.id == app_obj.store_id))).scalar_one()
        first = candidate.full_name.split(" ")[0] if candidate.full_name else "there"
        when_label = await _slot_when_label(db, app_obj.store_id, slot)
        subject, html = build_interview_reschedule_email(
            first, store.name, when_label, await _interviewer_label(db, app_obj)
        )
        await send_email(to=candidate.email, subject=subject, html=html)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[interview_reschedule] send failed for application {app_obj.id}: {e}")


async def send_interviewer_update(
    db: AsyncSession, app_obj: Application, slot: InterviewSlot | None = None
) -> None:
    """인터뷰어 변경 메일 — 시간 그대로, 면접관만 바뀜. best-effort.

    slot 은 현재 확정 슬롯(시간 라벨용, 있으면). 인터뷰어가 배정돼 있을 때만 호출할 것.
    """
    try:
        label = await _interviewer_label(db, app_obj)
        if not label:
            return  # 면접관이 없으면(해제) 변경 메일 의미 없음
        candidate = (
            await db.execute(select(Candidate).where(Candidate.id == app_obj.candidate_id))
        ).scalar_one()
        store = (await db.execute(select(Store).where(Store.id == app_obj.store_id))).scalar_one()
        first = candidate.full_name.split(" ")[0] if candidate.full_name else "there"
        when_label = await _slot_when_label(db, app_obj.store_id, slot) if slot else None
        subject, html = build_interview_interviewer_email(first, store.name, label, when_label)
        await send_email(to=candidate.email, subject=subject, html=html)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[interview_interviewer] send failed for application {app_obj.id}: {e}")


async def send_cancellation(
    db: AsyncSession, app_obj: Application, prev_slot: InterviewSlot | None = None
) -> None:
    """취소 메일 — 확정됐던 인터뷰가 취소됐을 때. best-effort.

    prev_slot 은 취소된(확정 해제된) 슬롯. 메일에 취소된 시각을 보여주기 위함.
    """
    try:
        candidate = (
            await db.execute(select(Candidate).where(Candidate.id == app_obj.candidate_id))
        ).scalar_one()
        store = (await db.execute(select(Store).where(Store.id == app_obj.store_id))).scalar_one()
        first = candidate.full_name.split(" ")[0] if candidate.full_name else "there"
        when_label = await _slot_when_label(db, app_obj.store_id, prev_slot) if prev_slot else None
        subject, html = build_interview_cancellation_email(first, store.name, when_label)
        await send_email(to=candidate.email, subject=subject, html=html)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[interview_cancellation] send failed for application {app_obj.id}: {e}")
