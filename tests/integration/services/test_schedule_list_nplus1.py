"""Integration — schedule_service 목록 변환의 N+1 제거 + 단건 경로와의 동치성.

대상: schedule_service.list_entries → _list_to_responses (batch prefetch)

배경: 기존 list_entries 는 행마다 _to_response 를 호출해 user/store/work_role/시급
cascade 를 각각 별도 쿼리로 조회했다 (행당 5~8쿼리 → per_page=2000 시 수천 쿼리).
_list_to_responses 가 등장하는 id 를 모아 테이블당 1쿼리로 prefetch 하도록 변경.

[작성됨]
- N+1 회귀 가드: 행 수가 늘어도(서로 다른 user 로 분산) 발생 쿼리 수가 일정
- 동치성: _list_to_responses 출력 == 행별 _to_response 출력 (모든 필드)
- 시급 cascade: user > store > org 출처가 목록 경로에서도 단건과 동일
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import engine
from app.models.organization import Store
from app.models.user import User
from app.repositories.schedule_repository import schedule_repository
from app.services.schedule_service import schedule_service

pytestmark = pytest.mark.asyncio


def _query_counter():
    """engine 에서 실행되는 SQL statement 수를 세는 context helper.

    반환: (start, stop) — start() 후 stop() 하면 그 사이 실행된 쿼리 수 반환.
    """
    state = {"n": 0, "listening": False}

    def _on_exec(conn, cursor, statement, params, context, executemany):
        state["n"] += 1

    def start():
        state["n"] = 0
        event.listen(engine.sync_engine, "before_cursor_execute", _on_exec)
        state["listening"] = True

    def stop() -> int:
        if state["listening"]:
            event.remove(engine.sync_engine, "before_cursor_execute", _on_exec)
            state["listening"] = False
        return state["n"]

    return start, stop


async def _entries_for(db: AsyncSession, org_id, store_id):
    entries, _ = await schedule_repository.get_by_filters(
        db, org_id, store_id=store_id, date_from=date(2000, 1, 1), per_page=500,
    )
    return list(entries)


async def test_list_no_n_plus_one(
    db: AsyncSession, make_schedule, test_users: dict, test_store_id, seed_organization,
    _clean_state,
):
    """행 수가 늘어도 쿼리 수가 (행에 비례해) 증가하지 않는다.

    서로 다른 4명의 user 로 스케줄을 분산 생성해도, prefetch 는 테이블당 1쿼리라
    1행일 때와 4행(4 user)일 때의 발생 쿼리 수가 같아야 한다.
    """
    org_id = seed_organization["id"]
    users = list(test_users.values())

    # 1행
    await make_schedule(users[0], store_id=test_store_id)
    entries_1 = await _entries_for(db, org_id, test_store_id)
    assert len(entries_1) == 1

    start, stop = _query_counter()
    start()
    try:
        await schedule_service.list_entries(
            db, org_id, store_id=test_store_id, date_from=date(2000, 1, 1), per_page=500,
        )
    finally:
        c1 = stop()

    # 3행 추가 (서로 다른 user) → 총 4행
    for u in users[1:4]:
        await make_schedule(u, store_id=test_store_id)
    entries_4 = await _entries_for(db, org_id, test_store_id)
    assert len(entries_4) == 4

    start, stop = _query_counter()
    start()
    try:
        await schedule_service.list_entries(
            db, org_id, store_id=test_store_id, date_from=date(2000, 1, 1), per_page=500,
        )
    finally:
        c4 = stop()

    # 핵심: 행이 1→4 로 늘어도 쿼리 수 동일 (N+1 이면 c4 > c1)
    assert c4 == c1, f"query count grew with rows: {c1} → {c4} (N+1 regression)"
    # prefetch 는 소수의 고정 쿼리 (count + main + user/store/org/workrole)
    assert c4 <= 8, f"unexpectedly many queries: {c4}"


async def test_list_matches_single_to_response(
    db: AsyncSession, make_schedule, test_users: dict, test_store_id, seed_organization,
    _clean_state,
):
    """배치 목록 변환이 행별 _to_response 와 모든 필드에서 동일한 결과를 낸다."""
    org_id = seed_organization["id"]
    for u in list(test_users.values())[:3]:
        await make_schedule(u, store_id=test_store_id)

    entries = await _entries_for(db, org_id, test_store_id)
    assert len(entries) == 3

    batch = await schedule_service._list_to_responses(db, entries)
    single = [await schedule_service._to_response(db, e) for e in entries]

    assert len(batch) == len(single) == 3
    for b, s in zip(batch, single):
        assert b.model_dump() == s.model_dump()


async def test_list_rate_cascade(
    db: AsyncSession, make_schedule, test_users: dict, test_store_id, seed_organization,
    _clean_state,
):
    """시급 출처 cascade(user > store > org)가 목록 경로에서도 단건과 동일."""
    org_id = seed_organization["id"]
    users = list(test_users.values())
    user_with_rate = users[0]
    user_without_rate = users[1]

    # user[0] 에만 개인 시급 부여, store 에 기본 시급 부여
    await db.execute(
        update(User).where(User.id == user_with_rate["id"]).values(hourly_rate=20)
    )
    await db.execute(
        update(Store).where(Store.id == test_store_id).values(default_hourly_rate=15)
    )
    await db.commit()
    try:
        await make_schedule(user_with_rate, store_id=test_store_id)
        await make_schedule(user_without_rate, store_id=test_store_id)
        entries = await _entries_for(db, org_id, test_store_id)

        batch = await schedule_service._list_to_responses(db, entries)
        by_user = {r.user_id: r for r in batch}

        # 개인 시급 있는 user → source "user", rate 20
        ru = by_user[str(user_with_rate["id"])]
        assert ru.effective_rate_source == "user"
        assert ru.effective_rate == 20.0
        # 개인 시급 없는 user → store 기본으로 fallback → source "store", rate 15
        rs = by_user[str(user_without_rate["id"])]
        assert rs.effective_rate_source == "store"
        assert rs.effective_rate == 15.0

        # 단건 경로와도 동일
        single = {
            str(e.user_id): await schedule_service._to_response(db, e) for e in entries
        }
        for uid, r in by_user.items():
            assert single[uid].effective_rate_source == r.effective_rate_source
            assert single[uid].effective_rate == r.effective_rate
    finally:
        await db.execute(
            update(User).where(User.id == user_with_rate["id"]).values(hourly_rate=None)
        )
        await db.execute(
            update(Store).where(Store.id == test_store_id).values(default_hourly_rate=None)
        )
        await db.commit()
