"""Integration test — 스케줄 수정 사유가 History 에 남는지.

`ScheduleUpdate.change_reason` 이전에는 스케줄을 왜 바꿨는지 알 방법이 없었다.
attendance correction 은 reason 이 NOT NULL 필수인데 스케줄 수정만 diff 만 남아서,
같은 화면에서 두 기록의 신뢰도가 갈렸다. 그 격차를 메운 것이라 회귀 시 조용히
사유가 사라진다 — 여기서 지킨다.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import delete, select

from app.database import async_session
from app.models.schedule import Schedule, ScheduleAuditLog
from app.models.user_store import UserStore
from app.schemas.schedule import ScheduleUpdate
from app.services.schedule_service import schedule_service

pytestmark = pytest.mark.asyncio


# 이 모듈이 직접 만든 근무 배정. conftest 의 _purge_test_data 는 user_stores 를 건드리지
# 않으므로, 남겨두면 "이 유저는 이 매장 소속이 아니다" 를 전제로 하는 IDOR 테스트가
# 조용히 통과해 버린다 (실제로 그랬다). 만든 것은 여기서 되돌린다.
_created_assignments: list[tuple[UUID, UUID]] = []


@pytest.fixture(autouse=True)
async def _cleanup_assignments():
    yield
    if not _created_assignments:
        return
    async with async_session() as db:
        for user_id, store_id in _created_assignments:
            await db.execute(
                delete(UserStore).where(
                    UserStore.user_id == user_id,
                    UserStore.store_id == store_id,
                )
            )
        await db.commit()
    _created_assignments.clear()


async def _make_schedule(test_user: dict, store_id: UUID) -> UUID:
    """스케줄 1건 + 근무 배정 보장.

    update_entry 는 "이 직원이 이 매장에 배정돼 있는가" 를 검증하므로, 다른 테스트가
    남긴 배정 상태에 기대지 않고 여기서 직접 보장한다 (전체 스위트 실행 시 순서 의존 제거).
    """
    day = date.today() + timedelta(days=3)
    async with async_session() as db:
        assigned = await db.scalar(
            select(UserStore).where(
                UserStore.user_id == test_user["id"],
                UserStore.store_id == store_id,
            )
        )
        if assigned is None:
            db.add(
                UserStore(
                    user_id=test_user["id"],
                    store_id=store_id,
                    is_work_assignment=True,
                )
            )
            _created_assignments.append((test_user["id"], store_id))
        elif not assigned.is_work_assignment:
            assigned.is_work_assignment = True
        await db.flush()

        sched = Schedule(
            organization_id=test_user["organization_id"],
            user_id=test_user["id"],
            store_id=store_id,
            operating_day=day,
            start_at=datetime.combine(day, time(9, 0)),
            end_at=datetime.combine(day, time(17, 0)),
            status="confirmed",
        )
        db.add(sched)
        await db.commit()
        return sched.id


async def _modified_logs(schedule_id: UUID) -> list[ScheduleAuditLog]:
    async with async_session() as db:
        result = await db.execute(
            select(ScheduleAuditLog)
            .where(
                ScheduleAuditLog.schedule_id == schedule_id,
                ScheduleAuditLog.event_type == "modified",
            )
            .order_by(ScheduleAuditLog.timestamp)
        )
        return list(result.scalars().all())


class _Actor:
    """schedule_service 가 actor 에서 읽는 최소 속성만."""

    def __init__(self, user: dict) -> None:
        self.id = user["id"]
        self.organization_id = user["organization_id"]
        self.role = None


async def test_change_reason_is_stored_on_modified_audit_log(
    test_user: dict, test_store_id: UUID, _clean_state: None,
) -> None:
    """수정 사유를 보내면 modified 감사 로그의 reason 에 그대로 들어간다."""
    schedule_id = await _make_schedule(test_user, test_store_id)

    async with async_session() as db:
        await schedule_service.update_entry(
            db,
            schedule_id,
            test_user["organization_id"],
            ScheduleUpdate(note="Covering the patio", change_reason="Staffing change"),
            actor=_Actor(test_user),
        )

    logs = await _modified_logs(schedule_id)
    assert logs, "modified audit log should exist"
    assert logs[-1].reason == "Staffing change"


async def test_change_reason_omitted_keeps_reason_null(
    test_user: dict, test_store_id: UUID, _clean_state: None,
) -> None:
    """사유를 안 보내는 기존 호출자는 그대로 동작한다 (선택 입력)."""
    schedule_id = await _make_schedule(test_user, test_store_id)

    async with async_session() as db:
        await schedule_service.update_entry(
            db,
            schedule_id,
            test_user["organization_id"],
            ScheduleUpdate(note="No reason given"),
            actor=_Actor(test_user),
        )

    logs = await _modified_logs(schedule_id)
    assert logs
    assert logs[-1].reason is None


async def test_blank_change_reason_is_normalized_to_null(
    test_user: dict, test_store_id: UUID, _clean_state: None,
) -> None:
    """공백만 보낸 사유는 저장하지 않는다 — 빈 문자열이 History 에 남으면 소음."""
    schedule_id = await _make_schedule(test_user, test_store_id)

    async with async_session() as db:
        await schedule_service.update_entry(
            db,
            schedule_id,
            test_user["organization_id"],
            ScheduleUpdate(note="Whitespace reason", change_reason="   "),
            actor=_Actor(test_user),
        )

    logs = await _modified_logs(schedule_id)
    assert logs
    assert logs[-1].reason is None
