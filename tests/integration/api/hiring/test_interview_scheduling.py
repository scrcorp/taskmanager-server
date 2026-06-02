"""인터뷰 스케줄링 (#2 Phase 2) — 슬롯 관리 + 공개 토큰 선호 + 확정/취소.

커버: 슬롯 벌크생성/조회(수요·확정), 삭제 가드, 공개 토큰 GET/선호제출(≤3·stage·확정 가드),
확정(interview_at UTC·슬롯잠금·중복확정 409), 취소, 토큰 회전 무효화.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.models.hiring import Application, Candidate
from app.models.interview import InterviewSlot, InterviewSlotPreference
from app.utils.interview_token import issue_interview_token
from app.utils.password import hash_password

PW_HASH = hash_password("1234")
CONSOLE = "/api/v1/console/hiring"
PUBLIC = "/api/v1/app/interview"


async def _mk_app(store_id: UUID, stage: str = "interview") -> tuple[UUID, UUID, str]:
    """candidate + application(stage) 생성 + 인터뷰 토큰 발급. (app_id, cand_id, token)."""
    nonce = uuid.uuid4().hex[:8]
    email = f"__iv_{nonce}@test.local"
    async with async_session() as db:
        cand = Candidate(
            username=f"__iv_{nonce}", email=email, email_normalized=email.lower(),
            password_hash=PW_HASH, full_name="Ivy Vance",
        )
        db.add(cand)
        await db.flush()
        app_row = Application(candidate_id=cand.id, store_id=store_id, stage=stage)
        db.add(app_row)
        await db.flush()
        token, jti = issue_interview_token(app_row.id)
        app_row.interview_token = jti
        ids = (app_row.id, cand.id, token)
        await db.commit()
    return ids


async def _cleanup(app_id: UUID, cand_id: UUID, store_id: UUID) -> None:
    async with async_session() as db:
        await db.execute(delete(InterviewSlotPreference).where(InterviewSlotPreference.application_id == app_id))
        await db.execute(delete(Application).where(Application.id == app_id))
        await db.execute(delete(Candidate).where(Candidate.id == cand_id))
        await db.execute(delete(InterviewSlot))
        await db.commit()


@pytest_asyncio.fixture
async def iv(test_store_id: UUID):
    app_id, cand_id, token = await _mk_app(test_store_id)
    yield {"store_id": test_store_id, "application_id": app_id, "candidate_id": cand_id, "token": token}
    await _cleanup(app_id, cand_id, test_store_id)


def _future(days: int) -> str:
    # 고정 미래일자 — 'today' 의존 줄이려 충분히 먼 미래
    return (date(2026, 7, 1) + timedelta(days=days)).isoformat()


async def _make_slots(client: AsyncClient, headers, store_id: UUID, n: int = 3) -> list[str]:
    slots = [{"date": _future(i), "start": "10:00", "end": "10:30"} for i in range(n)]
    resp = await client.post(f"{CONSOLE}/interview-slots", headers=headers, json={"slots": slots})
    assert resp.status_code == 200, resp.text
    listed = await client.get(f"{CONSOLE}/interview-slots", headers=headers)
    return [s["id"] for s in listed.json()["items"]]


@pytest.mark.asyncio
async def test_create_and_list_slots(async_client: AsyncClient, admin_headers, iv):
    ids = await _make_slots(async_client, admin_headers, iv["store_id"], 3)
    assert len(ids) == 3
    # 중복 생성은 skip
    resp = await async_client.post(
        f"{CONSOLE}/interview-slots", headers=admin_headers,
        json={"slots": [{"date": _future(0), "start": "10:00", "end": "10:30"}]},
    )
    assert resp.json()["created"] == 0


@pytest.mark.asyncio
async def test_public_pick_flow(async_client: AsyncClient, admin_headers, iv):
    ids = await _make_slots(async_client, admin_headers, iv["store_id"], 3)
    token = iv["token"]
    # GET — pending
    g = await async_client.get(f"{PUBLIC}/{token}")
    assert g.status_code == 200, g.text
    assert g.json()["status"] == "pending"
    assert len(g.json()["slots"]) == 3
    # POST 2 picks
    p = await async_client.post(f"{PUBLIC}/{token}/preferences", json={"slot_ids": ids[:2]})
    assert p.status_code == 200, p.text
    assert p.json()["count"] == 2
    # GET — picked + picked flags
    g2 = await async_client.get(f"{PUBLIC}/{token}")
    assert g2.json()["status"] == "picked"
    assert sum(1 for s in g2.json()["slots"] if s["picked"]) == 2
    # admin sees demand + wanters
    lst = await async_client.get(f"{CONSOLE}/interview-slots", headers=admin_headers)
    demand = {s["id"]: s["demand"] for s in lst.json()["items"]}
    assert demand[ids[0]] == 1 and demand[ids[1]] == 1 and demand[ids[2]] == 0


@pytest.mark.asyncio
async def test_pick_max_three(async_client: AsyncClient, admin_headers, iv):
    ids = await _make_slots(async_client, admin_headers, iv["store_id"], 4)
    r = await async_client.post(f"{PUBLIC}/{iv['token']}/preferences", json={"slot_ids": ids[:4]})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "too_many"


@pytest.mark.asyncio
async def test_pick_requires_interview_stage(async_client: AsyncClient, admin_headers, test_store_id):
    app_id, cand_id, token = await _mk_app(test_store_id, stage="screen")
    try:
        ids = await _make_slots(async_client, admin_headers, test_store_id, 1)
        r = await async_client.post(f"{PUBLIC}/{token}/preferences", json={"slot_ids": ids})
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "not_in_interview"
    finally:
        await _cleanup(app_id, cand_id, test_store_id)


@pytest.mark.asyncio
async def test_confirm_locks_slot(async_client: AsyncClient, admin_headers, iv, test_store_id):
    ids = await _make_slots(async_client, admin_headers, iv["store_id"], 2)
    # confirm slot 0
    c = await async_client.post(
        f"{CONSOLE}/applications/{iv['application_id']}/interview/confirm",
        headers=admin_headers, json={"slot_id": ids[0]},
    )
    assert c.status_code == 200, c.text
    assert c.json()["interview_at"] is not None  # 벽시계 → UTC 변환됨

    # 같은 슬롯을 다른 application 이 확정 시도 → 409
    app2, cand2, _t2 = await _mk_app(test_store_id)
    try:
        c2 = await async_client.post(
            f"{CONSOLE}/applications/{app2}/interview/confirm",
            headers=admin_headers, json={"slot_id": ids[0]},
        )
        assert c2.status_code == 409
        assert c2.json()["detail"]["code"] == "slot_taken"
    finally:
        # 공유 슬롯은 건드리지 않고 app2/candidate2 만 정리 (iv 의 슬롯 보존)
        async with async_session() as db:
            await db.execute(delete(Application).where(Application.id == app2))
            await db.execute(delete(Candidate).where(Candidate.id == cand2))
            await db.commit()

    # 확정된 슬롯 삭제 불가
    d = await async_client.delete(f"{CONSOLE}/interview-slots/{ids[0]}", headers=admin_headers)
    assert d.status_code == 400
    assert d.json()["detail"]["code"] == "slot_confirmed"

    # public POST 차단 (already confirmed)
    pp = await async_client.post(f"{PUBLIC}/{iv['token']}/preferences", json={"slot_ids": [ids[1]]})
    assert pp.status_code == 409
    assert pp.json()["detail"]["code"] == "already_confirmed"


@pytest.mark.asyncio
async def test_cancel_frees_slot(async_client: AsyncClient, admin_headers, iv):
    ids = await _make_slots(async_client, admin_headers, iv["store_id"], 1)
    await async_client.post(
        f"{CONSOLE}/applications/{iv['application_id']}/interview/confirm",
        headers=admin_headers, json={"slot_id": ids[0]},
    )
    cancel = await async_client.post(
        f"{CONSOLE}/applications/{iv['application_id']}/interview/cancel", headers=admin_headers
    )
    assert cancel.status_code == 200
    assert cancel.json()["confirmed_slot_id"] is None
    # 이제 슬롯 삭제 가능
    d = await async_client.delete(f"{CONSOLE}/interview-slots/{ids[0]}", headers=admin_headers)
    assert d.status_code == 200


@pytest.mark.asyncio
async def test_token_rotation_invalidates_old(async_client: AsyncClient, admin_headers, iv):
    old = iv["token"]
    assert (await async_client.get(f"{PUBLIC}/{old}")).status_code == 200
    # 새 토큰 발급 → 기존 토큰 무효
    issued = await async_client.post(
        f"{CONSOLE}/applications/{iv['application_id']}/interview/issue-token", headers=admin_headers
    )
    assert issued.status_code == 200
    new = issued.json()["token"]
    assert (await async_client.get(f"{PUBLIC}/{new}")).status_code == 200
    assert (await async_client.get(f"{PUBLIC}/{old}")).status_code == 400


@pytest.mark.asyncio
async def test_confirm_releases_other_demand(async_client: AsyncClient, admin_headers, iv):
    """확정되면 그 지원자의 다른 희망 슬롯은 demand 에서 해제된다."""
    ids = await _make_slots(async_client, admin_headers, iv["store_id"], 2)
    await async_client.post(f"{PUBLIC}/{iv['token']}/preferences", json={"slot_ids": ids[:2]})
    lst = await async_client.get(f"{CONSOLE}/interview-slots", headers=admin_headers)
    demand = {s["id"]: s["demand"] for s in lst.json()["items"]}
    assert demand[ids[0]] == 1 and demand[ids[1]] == 1
    # 슬롯 0 확정 → 슬롯 1(다른 희망)은 더 이상 demand 아님
    c = await async_client.post(
        f"{CONSOLE}/applications/{iv['application_id']}/interview/confirm",
        headers=admin_headers, json={"slot_id": ids[0]},
    )
    assert c.status_code == 200, c.text
    items2 = {s["id"]: s for s in (await async_client.get(f"{CONSOLE}/interview-slots", headers=admin_headers)).json()["items"]}
    assert items2[ids[1]]["demand"] == 0
    assert items2[ids[0]]["confirmed"] is not None


@pytest.mark.asyncio
async def test_update_interviewer_only(async_client: AsyncClient, admin_headers, iv, test_users):
    """인터뷰어만 변경 — 확정 슬롯/시각은 그대로."""
    ids = await _make_slots(async_client, admin_headers, iv["store_id"], 1)
    c = await async_client.post(
        f"{CONSOLE}/applications/{iv['application_id']}/interview/confirm",
        headers=admin_headers, json={"slot_id": ids[0]},
    )
    assert c.status_code == 200, c.text
    at_before = c.json()["interview_at"]
    iv_user = str(test_users["testadmin"]["id"])
    r = await async_client.patch(
        f"{CONSOLE}/applications/{iv['application_id']}/interview/interviewer",
        headers=admin_headers, json={"interviewer_id": iv_user},
    )
    assert r.status_code == 200, r.text
    assert r.json()["interviewer_id"] == iv_user
    det = (await async_client.get(f"{CONSOLE}/applications/{iv['application_id']}/interview", headers=admin_headers)).json()
    assert det["interviewer_id"] == iv_user
    assert det["interview_at"] == at_before  # 시간 불변


@pytest.mark.asyncio
async def test_confirm_records_history(async_client: AsyncClient, admin_headers, iv):
    """확정은 audit_log(history)에 interview_confirmed 로 기록된다."""
    ids = await _make_slots(async_client, admin_headers, iv["store_id"], 1)
    await async_client.post(
        f"{CONSOLE}/applications/{iv['application_id']}/interview/confirm",
        headers=admin_headers, json={"slot_id": ids[0]},
    )
    det = await async_client.get(f"{CONSOLE}/applications/{iv['application_id']}", headers=admin_headers)
    assert det.status_code == 200, det.text
    actions = [e["action"] for e in det.json().get("audit_log", [])]
    assert "interview_confirmed" in actions


@pytest.mark.asyncio
async def test_complete_interview_moves_to_review(async_client: AsyncClient, admin_headers, iv):
    """인터뷰 완료 = review 단계 이동 (stage 패치 + history 기록)."""
    ids = await _make_slots(async_client, admin_headers, iv["store_id"], 1)
    await async_client.post(
        f"{CONSOLE}/applications/{iv['application_id']}/interview/confirm",
        headers=admin_headers, json={"slot_id": ids[0]},
    )
    r = await async_client.patch(
        f"{CONSOLE}/applications/{iv['application_id']}", headers=admin_headers, json={"stage": "review"},
    )
    assert r.status_code == 200, r.text
    det = (await async_client.get(f"{CONSOLE}/applications/{iv['application_id']}", headers=admin_headers)).json()
    assert det["stage"] == "review"
    # stage 변경이 history 에 기록 (interview → review)
    stage_entries = [(e.get("before"), e.get("after")) for e in det.get("audit_log", []) if e["action"] == "stage"]
    assert ("interview", "review") in stage_entries
