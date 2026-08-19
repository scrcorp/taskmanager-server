"""API integration — 시작 달력일 방어선 (2026-08 "1439분 조기출근" 오염의 회귀 고정).

배경: 시프트의 시작 달력일은 입력이 아니라 파생값이다.

    시작 날짜 = 영업일 + (시작 시각 < day_start(영업일+1) ? 1 : 0)

경계가 11:00 인 매장(MSK)에서는 오전 시프트가 **정상적으로** +1일에 앉는다. 그래서
"+1일" 자체는 오염 신호가 아니고, **자동 판정과 다른 날짜가 표시 없이 들어오는 것**이
신호다. 실제 사고에서는 09:00(+1d) 시프트의 시각만 17:00 으로 바뀌면서 +1d 가 남아
`영업일+1 17:00` 으로 저장됐고, 검증이 경고뿐이라 벌크의 force 에 삼켜져 24건이 쌓였다.

여기서 고정하는 계약:
  - 표시 없는 불일치        → 400 START_DATE_MISMATCH (force 로도 못 넘는다)
  - `date_override:true`    → 409 경고 후 force 로 통과 (사람이 고른 것)
  - 시각 변경 시            → 날짜 재파생 (옛 오프셋을 붙들지 않는다)
  - 24h 초과 구간           → 400 SHIFT_SPAN_TOO_LONG
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update

from app.database import async_session
from app.models.organization import Store
from app.models.schedule import Schedule
from app.models.user_store import UserStore

pytestmark = pytest.mark.asyncio

CREATE_URL = "/api/v1/console/schedules"
OD = date(2026, 12, 4)          # 영업일 라벨
NEXT = OD + timedelta(days=1)   # 영업일 + 1일


@pytest_asyncio.fixture
async def late_boundary_store(test_user, test_store_id: UUID) -> AsyncIterator[dict]:
    """경계 11:00 매장 + 직원 배정. MSK 와 같은 조건을 만든다. teardown 에서 복원."""
    async with async_session() as db:
        store = (await db.execute(select(Store).where(Store.id == test_store_id))).scalar_one()
        orig = store.day_start_time
        await db.execute(update(Store).where(Store.id == test_store_id)
                         .values(day_start_time={"all": "11:00"}))
        await db.execute(delete(UserStore).where(
            UserStore.user_id == test_user["id"], UserStore.store_id == test_store_id,
        ))
        db.add(UserStore(user_id=test_user["id"], store_id=test_store_id,
                         is_work_assignment=True))
        await db.commit()
    try:
        yield {**test_user, "store_id": test_store_id}
    finally:
        async with async_session() as db:
            await db.execute(delete(Schedule).where(
                Schedule.user_id == test_user["id"], Schedule.operating_day >= OD,
            ))
            await db.execute(update(Store).where(Store.id == test_store_id)
                             .values(day_start_time=orig))
            await db.commit()


def _payload(s, **over) -> dict:
    body = {
        "user_id": str(s["id"]), "store_id": str(s["store_id"]),
        "operating_day": OD.isoformat(),
        "status": "confirmed", "force": True,
    }
    body.update(over)
    return body


# ── 정상 경로 ────────────────────────────────────────────────

async def test_morning_shift_lands_on_next_day(async_client, admin_headers, late_boundary_store):
    """경계 11:00 매장의 09:00 시프트는 **자동으로** 영업일+1일에 앉는다 (정상, 경고 없음)."""
    resp = await async_client.post(CREATE_URL, headers=admin_headers, json=_payload(
        late_boundary_store,
        start_at=f"{NEXT.isoformat()}T09:00", end_at=f"{NEXT.isoformat()}T14:30",
    ))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["operating_day"] == OD.isoformat()
    assert body["start_at"] == f"{NEXT.isoformat()}T09:00"


async def test_evening_shift_lands_on_operating_day(async_client, admin_headers, late_boundary_store):
    """17:00 시프트는 영업일 당일이다."""
    resp = await async_client.post(CREATE_URL, headers=admin_headers, json=_payload(
        late_boundary_store,
        start_at=f"{OD.isoformat()}T17:00", end_at=f"{NEXT.isoformat()}T00:00",
    ))
    assert resp.status_code == 201, resp.text
    assert resp.json()["start_at"] == f"{OD.isoformat()}T17:00"


# ── 차단 (이번 사고의 형태) ──────────────────────────────────

async def test_evening_shift_on_next_day_is_rejected(async_client, admin_headers, late_boundary_store):
    """**오염 그 자체** — 17:00 인데 영업일+1일. 표시가 없으면 force 로도 못 넘는다."""
    resp = await async_client.post(CREATE_URL, headers=admin_headers, json=_payload(
        late_boundary_store,
        start_at=f"{NEXT.isoformat()}T17:00", end_at=f"{NEXT.isoformat() and (NEXT + timedelta(days=1)).isoformat()}T00:00",
    ))
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "SCHEDULE_INVALID"
    codes_ = [e["code"] for e in detail["errors"]]
    assert "START_DATE_MISMATCH" in codes_, detail
    # 사용자가 읽을 수 있어야 한다 — 원인(시각)·정답(자동 날짜)·경계가 params 에 다 있다.
    params = next(e["params"] for e in detail["errors"] if e["code"] == "START_DATE_MISMATCH")
    assert params["auto"] == OD.isoformat()
    assert params["chosen"] == NEXT.isoformat()
    assert params["boundary"] == "11:00"
    assert params["start_time"] == "17:00"


async def test_bulk_create_rejects_contaminated_row(async_client, admin_headers, late_boundary_store):
    """벌크 경로도 뚫리지 않는다 — 다건은 force 를 자동으로 붙이지만 에러는 못 넘는다.

    실제 오염 24건이 정확히 이 경로(주 단위 복사 → 벌크 생성)로 들어왔다.
    """
    resp = await async_client.post(f"{CREATE_URL}/bulk", headers=admin_headers, json={"entries": [
        _payload(late_boundary_store,
                 start_at=f"{OD.isoformat()}T17:00", end_at=f"{NEXT.isoformat()}T00:00"),
        _payload(late_boundary_store,
                 start_at=f"{NEXT.isoformat()}T17:00",
                 end_at=f"{(NEXT + timedelta(days=1)).isoformat()}T00:00"),
    ]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] == 1, body      # 정상 1건만 저장
    assert body["failed"] + body["skipped"] == 1, body
    assert any("START_DATE_MISMATCH" in e for e in body["errors"]), body["errors"]


async def test_span_over_24h_is_rejected(async_client, admin_headers, late_boundary_store):
    """종료 달력일이 한 칸 더 밀리면 24h 를 넘는다 — 긴 근무가 아니라 조립 오류다."""
    resp = await async_client.post(CREATE_URL, headers=admin_headers, json=_payload(
        late_boundary_store,
        start_at=f"{OD.isoformat()}T17:00",
        end_at=f"{(OD + timedelta(days=2)).isoformat()}T00:00",
    ))
    assert resp.status_code == 400, resp.text
    codes_ = [e["code"] for e in resp.json()["detail"]["errors"]]
    assert "SHIFT_SPAN_TOO_LONG" in codes_, resp.text


# ── 사람이 고른 경우 ─────────────────────────────────────────

async def test_explicit_override_is_also_rejected(async_client, admin_headers, late_boundary_store):
    """확인(`date_override`)으로도 넘길 수 없다.

    자동값과 다른 시작 달력일은 **예외 없이 자기 영업일 구간 밖**이다 — 구간이
    `[경계, 다음 경계)` 반열림이라 두 후보 중 하나만 안에 든다. 구간 밖 행은 저장돼도
    현장에서 못 쓴다(출근 시각의 영업일과 라벨이 달라 후보 조회에 안 잡히고, 경계가
    지난 뒤엔 이미 끝난 근무다). 그래서 통로를 아예 닫는다.

    의도가 "그 달력일에 일한다" 라면 바꿀 것은 시작 날짜가 아니라 **영업일**이고,
    응답이 그 값을 `suggested_operating_day` 로 알려준다.
    """
    payload = _payload(
        late_boundary_store,
        start_at=f"{NEXT.isoformat()}T17:00",
        end_at=f"{(NEXT + timedelta(days=1)).isoformat()}T00:00",
        date_override=True, force=True,      # 확인 + 강제 둘 다 줘도 막힌다
    )
    resp = await async_client.post(CREATE_URL, headers=admin_headers, json=payload)
    assert resp.status_code == 400, resp.text
    err = next(
        e for e in resp.json()["detail"]["errors"] if e["code"] == "START_DATE_MISMATCH"
    )
    # 사람이 다음에 뭘 해야 하는지가 params 에 있어야 한다.
    assert err["params"]["suggested_operating_day"] == NEXT.isoformat()
    assert err["params"]["operating_day"] == OD.isoformat()


async def test_moving_the_operating_day_is_the_way_to_do_it(async_client, admin_headers, late_boundary_store):
    """위 에러가 알려준 대로 **영업일을 옮기면** 같은 근무가 정상 저장된다.

    "그날만 특별히 일찍/늦게" 는 이렇게 표현한다 — 시작 달력일을 비틀지 않는다.
    """
    resp = await async_client.post(CREATE_URL, headers=admin_headers, json={
        "user_id": str(late_boundary_store["id"]),
        "store_id": str(late_boundary_store["store_id"]),
        "operating_day": NEXT.isoformat(),                     # 영업일을 하루 옮겼다
        "start_at": f"{NEXT.isoformat()}T17:00",
        "end_at": f"{(NEXT + timedelta(days=1)).isoformat()}T00:00",
        "status": "confirmed", "force": True,
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["operating_day"] == NEXT.isoformat()
    assert body["start_at"] == f"{NEXT.isoformat()}T17:00"


# ── 시각 변경 시 재파생 (오염 생성 경로) ─────────────────────

async def test_time_change_redirives_start_date(async_client, admin_headers, late_boundary_store):
    """**사고의 생성 경로** — 09:00(+1d) 시프트의 시각만 17:00 으로 바꾸면 날짜가 따라온다.

    예전에는 +1d 오프셋이 보존돼 `영업일+1 17:00` 이 저장됐다. 이제는 재파생되어
    영업일 당일로 돌아오고, 날짜가 움직이므로 확인(force)을 받는다.
    """
    create = await async_client.post(CREATE_URL, headers=admin_headers, json=_payload(
        late_boundary_store,
        start_at=f"{NEXT.isoformat()}T09:00", end_at=f"{NEXT.isoformat()}T14:30",
    ))
    assert create.status_code == 201, create.text
    sid = create.json()["id"]

    patch = await async_client.patch(f"{CREATE_URL}/{sid}", headers=admin_headers, json={
        "start_time": "17:00", "end_time": "00:00", "force": True,
    })
    assert patch.status_code == 200, patch.text
    assert patch.json()["start_at"] == f"{OD.isoformat()}T17:00", patch.text


async def test_time_change_reverse_direction(async_client, admin_headers, late_boundary_store):
    """반대 방향도 같다 — 17:00(당일) 시프트를 09:00 으로 바꾸면 영업일+1일로 간다."""
    create = await async_client.post(CREATE_URL, headers=admin_headers, json=_payload(
        late_boundary_store,
        start_at=f"{OD.isoformat()}T17:00", end_at=f"{NEXT.isoformat()}T00:00",
    ))
    assert create.status_code == 201, create.text
    sid = create.json()["id"]

    patch = await async_client.patch(f"{CREATE_URL}/{sid}", headers=admin_headers, json={
        "start_time": "09:00", "end_time": "14:30", "force": True,
    })
    assert patch.status_code == 200, patch.text
    assert patch.json()["start_at"] == f"{NEXT.isoformat()}T09:00", patch.text


# ── 종료가 다음 영업일 경계를 넘는 경우 ──────────────────────

async def test_end_past_next_day_start_warns(async_client, admin_headers, late_boundary_store):
    """경계 11:00 매장에서 17:00 → (다음날) 12:00 근무.

    종료 12:00 은 이미 다음 영업일(Aug 19) 창이라 근무의 뒷부분이 그쪽에 속하는데,
    라벨은 Aug 18 하나뿐이라 급여·리포트가 전부 Aug 18 로 귀속된다.
    막지는 않되(실제로 가능한 근무) 확인은 받는다.
    """
    payload = _payload(
        late_boundary_store,
        start_at=f"{OD.isoformat()}T17:00", end_at=f"{NEXT.isoformat()}T12:00",
        force=False,
    )
    unconfirmed = await async_client.post(CREATE_URL, headers=admin_headers, json=payload)
    assert unconfirmed.status_code == 409, unconfirmed.text
    warns = [w["code"] for w in unconfirmed.json()["detail"]["warnings"]]
    assert "END_AFTER_NEXT_DAY_START" in warns, warns

    confirmed = await async_client.post(
        CREATE_URL, headers=admin_headers, json={**payload, "force": True},
    )
    assert confirmed.status_code == 201, confirmed.text


async def test_end_exactly_at_next_day_start_is_clean(async_client, admin_headers, late_boundary_store):
    """경계 **정각**에 끝나는 근무는 경고 없음 — 창은 [경계, 다음 경계) 반열림이다."""
    resp = await async_client.post(f"{CREATE_URL}/validate", headers=admin_headers, json=_payload(
        late_boundary_store,
        start_at=f"{OD.isoformat()}T17:00", end_at=f"{NEXT.isoformat()}T11:00",
        force=False,
    ))
    assert resp.status_code == 200, resp.text
    warns = [w["code"] for w in resp.json()["warnings"]]
    assert "END_AFTER_NEXT_DAY_START" not in warns, warns


# ── 이미 저장된 이상 행을 화면이 알아볼 수 있는가 ──────────────

async def test_response_flags_a_schedule_whose_start_is_outside_its_window(
    async_client, admin_headers, late_boundary_store,
):
    """저장 단계를 지나간 이상 행은 **응답이 이상하다고 말해야** 한다.

    지금은 API 로 못 만들지만 ① 이 검증 이전에 저장된 행 ② SQL 직접 수정·임포트
    ③ **매장 경계 설정을 나중에 바꾼 경우** 는 검증을 지나가지 않는다. 특히 ③ 은 설정
    한 번으로 멀쩡하던 스케줄이 통째로 구간 밖이 된다.

    그런 행은 현장에서 출근이 안 되므로(후보 조회에 안 잡힌다) 화면이 "에러 스케줄" 로
    드러내 사람이 고칠 수 있어야 한다. 조용히 정상처럼 보이는 것이 가장 나쁘다.
    """
    from app.database import async_session
    from app.models.schedule import Schedule as _Schedule

    # 정상 저장 (17:00 = 경계 11:00 이후 → 영업일 당일)
    created = await async_client.post(CREATE_URL, headers=admin_headers, json=_payload(
        late_boundary_store,
        start_at=f"{OD.isoformat()}T17:00", end_at=f"{NEXT.isoformat()}T00:00",
    ))
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    assert created.json()["start_outside_operating_window"] is False

    # 저장 뒤에 경계가 바뀐 상황을 만든다 (18:00 으로 이동 → 17:00 시작이 구간 밖이 된다)
    async with async_session() as db:
        store = await db.get(Store, late_boundary_store["store_id"])
        store.day_start_time = {"all": "18:00"}
        await db.commit()

    listed = await async_client.get(
        CREATE_URL,
        headers=admin_headers,
        params={
            "store_id": str(late_boundary_store["store_id"]),
            "date_from": OD.isoformat(),
            "date_to": NEXT.isoformat(),
        },
    )
    assert listed.status_code == 200, listed.text
    payload_json = listed.json()
    rows = payload_json["items"] if isinstance(payload_json, dict) else payload_json
    row = next(r for r in rows if r["id"] == sid)
    assert row["start_outside_operating_window"] is True, (
        "경계 설정이 바뀌어 구간 밖이 된 스케줄을 화면이 알아볼 수 없다"
    )

    # 단건 조회도 같은 사실을 말해야 한다 — 목록에서만 보이면 상세로 들어가는 순간 사라진다.
    detail = await async_client.get(f"{CREATE_URL}/{sid}", headers=admin_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["start_outside_operating_window"] is True

    async with async_session() as db:
        sched = await db.get(_Schedule, sid)
        if sched is not None:
            await db.delete(sched)
        await db.commit()
