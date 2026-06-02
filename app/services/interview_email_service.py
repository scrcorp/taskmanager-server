"""인터뷰 스케줄링 이메일 — 요청(초대) + 확정. 모두 best-effort (실패해도 흐름 안 깨짐).

발송은 호출 측 트랜잭션과 분리: invite 는 토큰 jti 를 application 에 세팅하므로 호출 측이 commit 한다.
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.hiring import Application, Candidate
from app.models.interview import InterviewSlot
from app.models.organization import Store
from app.models.user import User
from app.utils.email import send_email
from app.utils.email_templates import (
    build_interview_confirmation_email,
    build_interview_invite_email,
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


async def send_confirmation(db: AsyncSession, app_obj: Application, slot: InterviewSlot) -> None:
    """확정 메일 — 확정된 슬롯 시각(store-local) + 인터뷰어. best-effort."""
    try:
        candidate = (
            await db.execute(select(Candidate).where(Candidate.id == app_obj.candidate_id))
        ).scalar_one()
        store = (await db.execute(select(Store).where(Store.id == app_obj.store_id))).scalar_one()
        tz = await get_store_timezone(db, app_obj.store_id)
        first = candidate.full_name.split(" ")[0] if candidate.full_name else "there"
        local = datetime.combine(slot.slot_date, slot.start_time, tzinfo=ZoneInfo(tz))
        when_label = local.strftime("%a, %b %-d · %-I:%M %p ") + tz
        interviewer_name = None
        if app_obj.interviewer_id:
            iv = (await db.execute(select(User).where(User.id == app_obj.interviewer_id))).scalar_one_or_none()
            interviewer_name = iv.full_name if iv else None
        subject, html = build_interview_confirmation_email(first, store.name, when_label, interviewer_name)
        await send_email(to=candidate.email, subject=subject, html=html)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[interview_confirmation] send failed for application {app_obj.id}: {e}")
