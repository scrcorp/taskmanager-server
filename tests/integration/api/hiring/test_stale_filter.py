"""Pipeline 보드용 stale(오래 방치) 카드 숨김 + rejected_at 유지 테스트.

- rejected_at: rejected/withdrawn 진입 시 기록, 이탈 시 NULL
- GET /console/hiring/applications 의 stale_days / include_stale / stale_hidden
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.core.hiring import set_application_stage
from app.database import async_session
from app.models.hiring import Application, Candidate
from app.utils.password import hash_password

URL = "/api/v1/console/hiring/applications"
PW_HASH = hash_password("1234")


def _mk_candidate(tag: str, full_name: str) -> Candidate:
    nonce = uuid.uuid4().hex[:8]
    email = f"__stale_{tag}_{nonce}@test.local"
    return Candidate(
        username=f"__stale_{tag}_{nonce}",
        email=email,
        email_normalized=email.lower(),
        password_hash=PW_HASH,
        full_name=full_name,
    )


@pytest_asyncio.fixture
async def seeded(test_store_id: UUID):
    """stale/fresh 짝을 stage 별로 시드.

    rejected(오래/최근), withdrawn(오래), pending_form(오래/최근), new(대조군).
    """
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=30)
    recent = now - timedelta(days=2)

    plan = [
        # (tag, stage, rejected_at, updated_at)
        ("rej_old", "rejected", old, old),
        ("rej_new", "rejected", recent, recent),
        ("wd_old", "withdrawn", old, old),
        # rejected_at 이 비어있는 레거시 행 → updated_at 으로 판정되어야 함
        ("rej_legacy", "rejected", None, old),
        # 거절은 오래됐지만 최근에 메모를 고쳐 updated_at 만 갱신된 행 → 여전히 stale
        ("rej_noted", "rejected", old, now),
        ("pf_old", "pending_form", None, old),
        ("pf_new", "pending_form", None, recent),
        ("new_old", "new", None, old),  # 대조군 — 오래돼도 절대 안 숨겨짐
    ]

    ids: dict[str, UUID] = {}
    cand_ids: list[UUID] = []
    async with async_session() as db:
        for tag, stage, rej_at, upd_at in plan:
            cand = _mk_candidate(tag, f"Stale {tag}")
            db.add(cand)
            await db.flush()
            cand_ids.append(cand.id)
            row = Application(
                candidate_id=cand.id,
                store_id=test_store_id,
                stage=stage,
                rejected_at=rej_at,
            )
            db.add(row)
            await db.flush()
            # updated_at 은 onupdate 로 덮이므로 flush 후 강제 지정
            row.updated_at = upd_at
            ids[tag] = row.id
        await db.commit()

    yield ids

    async with async_session() as db:
        await db.execute(delete(Application).where(Application.id.in_(list(ids.values()))))
        await db.execute(delete(Candidate).where(Candidate.id.in_(cand_ids)))
        await db.commit()


async def _ids(client: AsyncClient, headers: dict, **params) -> set[str]:
    resp = await client.get(URL, params={"per_page": 100, **params}, headers=headers)
    assert resp.status_code == 200, resp.text
    return {it["id"] for it in resp.json()["items"]}


# ────────────────────────────────────────────────────────────────
# rejected_at 전이
# ────────────────────────────────────────────────────────────────
def test_set_stage_records_and_clears_rejected_at():
    class _Row:
        stage = "screen"
        rejected_at = None

    row = _Row()
    set_application_stage(row, "rejected")
    assert row.stage == "rejected"
    assert row.rejected_at is not None

    first = row.rejected_at
    # closed → closed 는 최초 종료 시각 유지
    set_application_stage(row, "withdrawn")
    assert row.rejected_at == first

    # 되돌리면 초기화 — 다시 '최근' 카드로 취급
    set_application_stage(row, "screen")
    assert row.rejected_at is None


@pytest.mark.asyncio
async def test_patch_to_rejected_sets_rejected_at(
    async_client: AsyncClient, admin_headers, seeded
):
    app_id = seeded["new_old"]
    resp = await async_client.patch(
        f"{URL}/{app_id}", json={"stage": "rejected"}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["rejected_at"] is not None

    async with async_session() as db:
        row = (
            await db.execute(select(Application).where(Application.id == app_id))
        ).scalar_one()
        assert row.rejected_at is not None

    # 다시 살리면 NULL
    resp = await async_client.patch(
        f"{URL}/{app_id}", json={"stage": "screen"}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["rejected_at"] is None


# ────────────────────────────────────────────────────────────────
# stale_days 필터
# ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_no_param_returns_everything(async_client: AsyncClient, admin_headers, seeded):
    """stale_days 미지정 = 기존 동작 그대로 (아무것도 안 숨김)."""
    got = await _ids(async_client, admin_headers)
    assert set(seeded.values()) <= {UUID(i) for i in got} or all(
        str(v) in got for v in seeded.values()
    )


@pytest.mark.asyncio
async def test_stale_days_hides_old_rejected_and_pending(
    async_client: AsyncClient, admin_headers, seeded
):
    got = await _ids(async_client, admin_headers, stale_days=7)
    hidden = {"rej_old", "wd_old", "rej_legacy", "rej_noted", "pf_old"}
    shown = {"rej_new", "pf_new", "new_old"}
    for tag in hidden:
        assert str(seeded[tag]) not in got, f"{tag} should be hidden"
    for tag in shown:
        assert str(seeded[tag]) in got, f"{tag} should be visible"


@pytest.mark.asyncio
async def test_include_stale_exempts_only_that_bucket(
    async_client: AsyncClient, admin_headers, seeded
):
    """'Show older' 를 Rejected 컬럼에서만 켜면 pending_form 은 계속 접혀 있어야 한다."""
    got = await _ids(async_client, admin_headers, stale_days=7, include_stale="rejected")
    assert str(seeded["rej_old"]) in got
    assert str(seeded["wd_old"]) in got
    assert str(seeded["pf_old"]) not in got

    got = await _ids(async_client, admin_headers, stale_days=7, include_stale="pending_form")
    assert str(seeded["pf_old"]) in got
    assert str(seeded["rej_old"]) not in got

    got = await _ids(
        async_client, admin_headers, stale_days=7, include_stale="rejected,pending_form"
    )
    assert str(seeded["rej_old"]) in got and str(seeded["pf_old"]) in got


@pytest.mark.asyncio
async def test_stale_hidden_counts(async_client: AsyncClient, admin_headers, seeded):
    resp = await async_client.get(
        URL, params={"per_page": 100, "stale_days": 7}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stale_hidden"]["rejected"] >= 4  # rej_old, wd_old, rej_legacy, rej_noted
    assert body["stale_hidden"]["pending_form"] >= 1  # pf_old

    # 면제해도(=펼쳐 봐도) 카운트는 그대로여야 토글 라벨이 안 흔들린다
    resp2 = await async_client.get(
        URL,
        params={"per_page": 100, "stale_days": 7, "include_stale": "rejected,pending_form"},
        headers=admin_headers,
    )
    assert resp2.json()["stale_hidden"] == body["stale_hidden"]


@pytest.mark.asyncio
async def test_stale_days_does_not_touch_counts(
    async_client: AsyncClient, admin_headers, seeded
):
    """상단 summary strip 용 counts 는 stale_days 와 무관한 전체 집계."""
    plain = await async_client.get(URL, params={"per_page": 100}, headers=admin_headers)
    staled = await async_client.get(
        URL, params={"per_page": 100, "stale_days": 7}, headers=admin_headers
    )
    assert plain.json()["counts"] == staled.json()["counts"]
