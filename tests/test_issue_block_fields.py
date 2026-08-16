"""API integration — 카테고리별 블록 필드 (작성/수정/스냅샷/promote).

계약 SoT: docs/99_inbox/2026-08-15-이슈리포트-description-블록화-검토.md
단위 규칙은 tests/unit/core/test_issue_fields.py 가 덮는다. 여기서는 **실제 API 를 통과했을 때**
payload 에 무엇이 저장되는지를 본다.
"""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.main import app
from app.models.report import Report, ReportTemplate
from app.models.user import User as UserModel
from app.models.user_store import UserStore

RD = "2030-09-02"


async def _login(username: str) -> str:
    from app.utils.jwt import create_access_token

    async with async_session() as db:
        u = (
            await db.execute(select(UserModel).where(UserModel.username == username))
        ).scalar_one()
        return create_access_token({"sub": str(u.id), "org": str(u.organization_id)})


def _h(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def perms(seed_roles: dict[str, UUID]) -> None:
    from app.models.permission import Permission, RolePermission

    async with async_session() as db:
        codes = ["reports:read", "reports:create", "reports:update"]
        pmap: dict[str, UUID] = {}
        for c in codes:
            p = (
                await db.execute(select(Permission).where(Permission.code == c))
            ).scalar_one_or_none()
            if p is None:
                p = Permission(code=c, name=c)
                db.add(p)
                await db.flush()
            pmap[c] = p.id
        for rid in seed_roles.values():
            for c in codes:
                ex = (
                    await db.execute(
                        select(RolePermission).where(
                            RolePermission.role_id == rid,
                            RolePermission.permission_id == pmap[c],
                        )
                    )
                ).scalar_one_or_none()
                if ex is None:
                    db.add(RolePermission(role_id=rid, permission_id=pmap[c]))
        await db.commit()


@pytest_asyncio.fixture
async def author(seed_organization: dict, test_store_id: UUID):
    """testsv 를 대상 매장에 배정(idempotent) + 사후 원복."""
    org_id: UUID = seed_organization["id"]
    async with async_session() as db:
        u = (
            await db.execute(
                select(UserModel).where(
                    UserModel.username == "testsv",
                    UserModel.organization_id == org_id,
                )
            )
        ).scalar_one()
        link = (
            await db.execute(
                select(UserStore).where(
                    UserStore.user_id == u.id, UserStore.store_id == test_store_id
                )
            )
        ).scalar_one_or_none()
        created = link is None
        if created:
            db.add(UserStore(user_id=u.id, store_id=test_store_id, is_manager=True))
            await db.commit()
        uid = u.id
    yield uid
    if created:
        async with async_session() as db:
            await db.execute(
                delete(UserStore).where(
                    UserStore.user_id == uid, UserStore.store_id == test_store_id
                )
            )
            await db.commit()


@pytest_asyncio.fixture
async def block_template(seed_organization: dict, test_store_id: UUID):
    """카테고리별 fields 를 가진 store 템플릿 + field_order. 사후 삭제."""
    payload = {
        "categories": [
            {"code": "review", "label": "Review", "is_active": True, "fields": [
                {"id": "platform", "type": "single_choice", "label": "Platform",
                 "required": True, "options": ["Google", "Yelp"]},
                {"id": "rating", "type": "number", "label": "Rating",
                 "min": 1, "max": 5, "decimals": 0},
                {"id": "followup", "type": "single_choice", "label": "Follow-up needed",
                 "options": ["Yes", "No"]},
            ]},
            {"code": "equipment", "label": "Equipment", "is_active": True, "fields": [
                {"id": "asset", "type": "short_text", "label": "Asset"},
            ]},
        ],
        "custom_fields": [
            {"id": "global_note", "type": "short_text", "label": "Note", "sort_order": 9},
        ],
        "field_order": ["__title", "rating", "platform", "__description", "global_note"],
    }
    async with async_session() as db:
        t = ReportTemplate(
            type="issue",
            organization_id=seed_organization["id"],
            store_id=test_store_id,
            name="Block Test Form",
            is_default=True,
            is_active=True,
            payload=payload,
        )
        db.add(t)
        await db.commit()
        await db.refresh(t)
        tid = t.id
    yield tid
    async with async_session() as db:
        await db.execute(delete(ReportTemplate).where(ReportTemplate.id == tid))
        await db.commit()


async def _create(client, token, store_id, payload, title="Block issue"):
    r = await client.post(
        "/api/v1/app/my/reports",
        headers=_h(token),
        json={"type": "issue", "store_id": str(store_id), "report_date": RD,
              "title": title, "payload": payload},
    )
    return r.status_code, (r.json() if r.content else {})


async def _payload_of(report_id: str) -> dict:
    async with async_session() as db:
        r = await db.get(Report, UUID(report_id))
        return dict(r.payload or {})


@pytest.mark.asyncio
async def test_asked_fields_all_get_keys(client, perms, author, block_template, test_store_id):
    """물어본 필드는 전부 키가 생기고, 미응답은 null 로 남는다."""
    tok = await _login("testsv")
    code, body = await _create(client, tok, test_store_id, {
        "category": "review", "severity": "high",
        "custom_field_values": {"platform": "Google", "rating": 2},
    })
    assert code == 201, body
    p = await _payload_of(body["id"])
    cfv = p["custom_field_values"]
    assert cfv["platform"] == "Google"
    assert cfv["rating"] == 2
    assert "followup" in cfv and cfv["followup"] is None, "미응답도 키가 있어야 한다"
    assert "global_note" in cfv and cfv["global_note"] is None, "전역 필드도 표시 대상"
    assert "asset" not in cfv, "다른 카테고리 필드는 안 물어봤으므로 키가 없다"


@pytest.mark.asyncio
async def test_server_writes_fields_snapshot(client, perms, author, block_template, test_store_id):
    """스냅샷은 서버가 만든다 — 클라가 보낸 값은 무시."""
    tok = await _login("testsv")
    code, body = await _create(client, tok, test_store_id, {
        "category": "review", "severity": "low",
        "custom_field_values": {"platform": "Yelp"},
        "fields_snapshot": [{"id": "spoofed", "type": "short_text", "label": "Hacked"}],
    })
    assert code == 201, body
    snap = (await _payload_of(body["id"]))["fields_snapshot"]
    ids = {s["id"] for s in snap}
    assert "spoofed" not in ids, "클라가 보낸 스냅샷을 신뢰하면 안 된다"
    assert ids == {"platform", "rating", "followup", "global_note"}
    by_id = {s["id"]: s for s in snap}
    assert by_id["platform"]["options"] == ["Google", "Yelp"]


@pytest.mark.asyncio
async def test_snapshot_survives_template_change(client, perms, author, block_template, test_store_id):
    """템플릿이 바뀌어도 과거 리포트는 그때 물어본 정의를 갖고 있다."""
    tok = await _login("testsv")
    code, body = await _create(client, tok, test_store_id, {
        "category": "review", "severity": "low",
        "custom_field_values": {"platform": "Google"},
    })
    assert code == 201, body

    async with async_session() as db:  # 템플릿에서 followup 제거
        t = await db.get(ReportTemplate, block_template)
        pl = dict(t.payload)
        cats = [dict(c) for c in pl["categories"]]
        for c in cats:
            if c["code"] == "review":
                c["fields"] = [f for f in c["fields"] if f["id"] != "followup"]
        pl["categories"] = cats
        t.payload = pl
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(t, "payload")
        await db.commit()

    snap = (await _payload_of(body["id"]))["fields_snapshot"]
    assert "followup" in {s["id"] for s in snap}, "과거 리포트의 스냅샷은 그대로여야 한다"


@pytest.mark.asyncio
async def test_required_and_range_enforced(client, perms, author, block_template, test_store_id):
    tok = await _login("testsv")
    code, body = await _create(client, tok, test_store_id, {
        "category": "review", "severity": "low", "custom_field_values": {},
    })
    assert code == 400, body

    code, body = await _create(client, tok, test_store_id, {
        "category": "review", "severity": "low",
        "custom_field_values": {"platform": "Google", "rating": 99},
    })
    assert code == 400, body

    code, body = await _create(client, tok, test_store_id, {
        "category": "review", "severity": "low",
        "custom_field_values": {"platform": "Google", "rating": 3.5},
    })
    assert code == 400, body  # decimals=0 → 정수만


@pytest.mark.asyncio
async def test_other_category_isolated(client, perms, author, block_template, test_store_id):
    """equipment 는 review 의 required 필드에 영향받지 않는다."""
    tok = await _login("testsv")
    code, body = await _create(client, tok, test_store_id, {
        "category": "equipment", "severity": "low",
        "custom_field_values": {"asset": "Fryer #2"},
    })
    assert code == 201, body
    cfv = (await _payload_of(body["id"]))["custom_field_values"]
    assert cfv["asset"] == "Fryer #2"
    assert "platform" not in cfv


@pytest.mark.asyncio
async def test_update_revalidates(client, perms, author, block_template, test_store_id):
    """수정 경로도 같은 규칙으로 검증한다 (예전엔 payload 통째 교체라 무검증)."""
    tok = await _login("testsv")
    code, body = await _create(client, tok, test_store_id, {
        "category": "review", "severity": "low",
        "custom_field_values": {"platform": "Google"},
    })
    assert code == 201, body
    rid = body["id"]

    r = await client.put(
        f"/api/v1/app/my/reports/{rid}",
        headers=_h(tok),
        json={"payload": {"category": "review", "severity": "low",
                          "custom_field_values": {"platform": "Naver"}}},
    )
    assert r.status_code == 400, r.text  # options 밖

    r = await client.put(
        f"/api/v1/app/my/reports/{rid}",
        headers=_h(tok),
        json={"payload": {"category": "review", "severity": "low",
                          "custom_field_values": {"platform": "Yelp", "followup": "No"}}},
    )
    assert r.status_code == 200, r.text
    cfv = (await _payload_of(rid))["custom_field_values"]
    assert cfv["platform"] == "Yelp"
    assert cfv["followup"] == "No"
    assert cfv["rating"] is None


@pytest.mark.asyncio
async def test_description_is_not_overwritten(client, perms, author, block_template, test_store_id):
    """description 은 자유 서술 칸 — 필드값 렌더로 덮어쓰지 않는다."""
    tok = await _login("testsv")
    code, body = await _create(client, tok, test_store_id, {
        "category": "review", "severity": "low",
        "description": "Customer was upset about wait time.",
        "custom_field_values": {"platform": "Google", "rating": 1},
    })
    assert code == 201, body
    p = await _payload_of(body["id"])
    assert p["description"] == "Customer was upset about wait time."
