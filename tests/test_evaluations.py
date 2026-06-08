"""Evaluation v1 — unit + API integration tests (merge gate).

Covers: seed/idempotency, direction validation (POST/PUT/picker), submit gate,
snapshot isolation + job_title, org scoping + soft delete, average computation,
dashboard compat, employee_no uniqueness.

전제: startup lifespan 이 테스트에서 안 돌므로 evaluation 권한/템플릿을
fixture 에서 idempotent 하게 보장한다 (hiring 테스트 패턴).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.evaluation import BASIC_CRITERIA, BASIC_SCALE, build_default_config
from app.core.permissions import DEFAULT_ROLE_PERMISSIONS
from app.database import async_session
from app.main import app
from app.models.evaluation import EvalTemplate, Evaluation
from app.models.organization import Organization, Store
from app.models.permission import Permission, RolePermission
from app.models.user import Role, User
from app.models.user_store import UserStore
from app.models.work import Position
from app.services.evaluation_service import evaluation_service
from app.utils.password import hash_password

PW_HASH = hash_password("1234")


# ===================================================================
# Fixtures
# ===================================================================


async def _login(username: str) -> str:
    """username → access token. HTTP login 대신 직접 mint.

    다른 테스트가 임시 org 를 만들면 company_code 없는 login 이 400 이 되므로
    (multi-org) JWT 를 직접 발급해 그 의존성을 끊는다. get_current_user 는
    sub/type 만 검증.
    """
    from app.utils.jwt import create_access_token

    async with async_session() as db:
        user = (
            await db.execute(select(User).where(User.username == username))
        ).scalar_one()
        return create_access_token(
            {
                "sub": str(user.id),
                "org": str(user.organization_id),
            }
        )


@pytest_asyncio.fixture
async def eval_perms(seed_roles: dict[str, UUID]) -> None:
    """evaluations:* permission 을 DB + role_permissions 에 idempotent 보장.

    super_owner/owner 는 require_permission bypass 이지만, GM/SV 는 명시적 부여 필요.
    """
    codes = [
        "evaluations:read",
        "evaluations:create",
        "evaluations:update",
        "evaluations:delete",
    ]
    async with async_session() as db:
        perms: dict[str, UUID] = {}
        for code in codes:
            p = (
                await db.execute(select(Permission).where(Permission.code == code))
            ).scalar_one_or_none()
            if p is None:
                resource, action = code.split(":")
                p = Permission(code=code, resource=resource, action=action)
                db.add(p)
                await db.flush()
            perms[code] = p.id

        # GM 은 4개 전부, SV 도 4개 전부 (contract: SV→Staff 작성 가능).
        grants = {
            "general_manager": codes,
            "supervisor": codes,
        }
        for role_name, role_codes in grants.items():
            role_id = seed_roles[role_name]
            for code in role_codes:
                exists = (
                    await db.execute(
                        select(RolePermission).where(
                            RolePermission.role_id == role_id,
                            RolePermission.permission_id == perms[code],
                        )
                    )
                ).scalar_one_or_none()
                if exists is None:
                    db.add(
                        RolePermission(role_id=role_id, permission_id=perms[code])
                    )
        await db.commit()


@pytest_asyncio.fixture
async def normalize_staff_role(test_users: dict, seed_roles: dict[str, UUID]):
    """teststaff 가 실제 'staff' role(priority 40)을 가리키도록 보장 (idempotent).

    이 worktree 의 시드 DB 에서 teststaff 가 supervisor role 에 매핑된 과거 흔적이
    있어 방향 검증 테스트가 깨진다. 비파괴적으로 staff role 로 정규화한다.
    """
    staff_role_id = seed_roles["staff"]
    staff_uid: UUID = test_users["teststaff"]["id"]
    async with async_session() as db:
        u = (await db.execute(select(User).where(User.id == staff_uid))).scalar_one()
        if u.role_id != staff_role_id:
            u.role_id = staff_role_id
            await db.commit()


@pytest_asyncio.fixture
async def basic_template(seed_organization: dict, eval_perms: None) -> EvalTemplate:
    """조직 Basic 템플릿 보장 (없으면 시드)."""
    org_id: UUID = seed_organization["id"]
    async with async_session() as db:
        template = await evaluation_service.ensure_basic_template(db, org_id)
        await db.commit()
        await db.refresh(template)
        return template


@pytest_asyncio.fixture
async def cleanup_evaluations(seed_organization: dict):
    """테스트 전후 이 조직의 evaluations 전부 삭제 (hard)."""
    org_id: UUID = seed_organization["id"]
    async with async_session() as db:
        await db.execute(delete(Evaluation).where(Evaluation.organization_id == org_id))
        await db.commit()
    yield
    async with async_session() as db:
        await db.execute(delete(Evaluation).where(Evaluation.organization_id == org_id))
        await db.commit()


@pytest_asyncio.fixture
async def test_position(test_store_id: UUID) -> UUID:
    """test_store 에 position 1개 보장."""
    async with async_session() as db:
        pos = (
            await db.execute(
                select(Position).where(
                    Position.store_id == test_store_id,
                    Position.name == "__eval_test_position__",
                )
            )
        ).scalar_one_or_none()
        if pos is None:
            pos = Position(store_id=test_store_id, name="__eval_test_position__")
            db.add(pos)
            await db.commit()
            await db.refresh(pos)
        return pos.id


@pytest_asyncio.fixture
async def assign_stores(test_users: dict, test_store_id: UUID, normalize_staff_role):
    """gm/sv/staff 를 test_store 에 배정 (gm=manager). picker/store-access 용."""
    async with async_session() as db:
        for uname, is_manager in (
            ("testgm", True),
            ("testsv", False),
            ("teststaff", False),
        ):
            uid = test_users[uname]["id"]
            us = (
                await db.execute(
                    select(UserStore).where(
                        UserStore.user_id == uid,
                        UserStore.store_id == test_store_id,
                    )
                )
            ).scalar_one_or_none()
            if us is None:
                db.add(
                    UserStore(
                        user_id=uid, store_id=test_store_id, is_manager=is_manager
                    )
                )
            else:
                us.is_manager = is_manager
        await db.commit()
    yield
    async with async_session() as db:
        for uname in ("testgm", "testsv", "teststaff"):
            uid = test_users[uname]["id"]
            await db.execute(
                delete(UserStore).where(
                    UserStore.user_id == uid, UserStore.store_id == test_store_id
                )
            )
        await db.commit()


def _full_responses() -> dict[str, int]:
    """9개 criteria 전부 채운 valid responses."""
    return {c["code"]: 4 for c in BASIC_CRITERIA}


def _payload(
    evaluatee_id: UUID,
    store_id: UUID,
    *,
    responses: dict[str, int] | None = None,
    status: str = "draft",
    position_id: UUID | None = None,
) -> dict:
    return {
        "evaluatee_id": str(evaluatee_id),
        "store_id": str(store_id),
        "position_id": str(position_id) if position_id else None,
        "period_start": "2026-01-01",
        "period_end": "2026-03-31",
        "responses": responses if responses is not None else {},
        "status": status,
    }


# ===================================================================
# 1. Seed
# ===================================================================


@pytest.mark.asyncio
async def test_basic_config_is_nine_criteria_and_five_scale():
    """build_default_config = 9 criteria(sort_order 1..9) + 5pt scale, deep-copy 격리."""
    cfg = build_default_config()
    assert len(cfg["criteria"]) == 9
    assert [c["sort_order"] for c in cfg["criteria"]] == list(range(1, 10))
    assert all(c["max_score"] == 5 for c in cfg["criteria"])
    assert len(cfg["scale"]) == 5
    assert [s["value"] for s in cfg["scale"]] == [1, 2, 3, 4, 5]
    # em-dash U+2014 verbatim in criterion 1
    assert "—" in cfg["criteria"][0]["description"]
    # deep-copy 격리 — 반환값 수정이 모듈 상수를 오염시키지 않음
    cfg["criteria"][0]["label"] = "MUTATED"
    assert BASIC_CRITERIA[0]["label"] != "MUTATED"


@pytest.mark.asyncio
async def test_seed_idempotent(seed_organization: dict, eval_perms: None):
    """ensure_basic_template 두 번 호출해도 default 는 정확히 1개."""
    org_id: UUID = seed_organization["id"]
    async with async_session() as db:
        await evaluation_service.ensure_basic_template(db, org_id)
        await db.commit()
        await evaluation_service.ensure_basic_template(db, org_id)
        await db.commit()
        rows = (
            await db.execute(
                select(EvalTemplate).where(
                    EvalTemplate.organization_id == org_id,
                    EvalTemplate.is_default.is_(True),
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].name == "Basic Performance Evaluation"
    assert rows[0].version == 1
    assert rows[0].status == "published"
    assert len(rows[0].config["criteria"]) == 9


@pytest.mark.asyncio
async def test_new_org_backfilled_via_ensure():
    """새 조직 생성 → ensure_basic_template 로 default 1개 생김."""
    async with async_session() as db:
        org = Organization(name=f"__eval_org_{uuid.uuid4().hex[:8]}")
        db.add(org)
        await db.flush()
        org_id = org.id
        await evaluation_service.ensure_basic_template(db, org_id)
        await db.commit()
        rows = (
            await db.execute(
                select(EvalTemplate).where(EvalTemplate.organization_id == org_id)
            )
        ).scalars().all()
        assert len(rows) == 1
        # cleanup
        await db.execute(delete(EvalTemplate).where(EvalTemplate.organization_id == org_id))
        await db.execute(delete(Organization).where(Organization.id == org_id))
        await db.commit()


@pytest.mark.asyncio
async def test_list_templates_endpoint(
    async_client: AsyncClient, basic_template: EvalTemplate
):
    """GET /templates → 조직 Basic 1개, config 9 criteria."""
    token = await _login("testadmin")
    resp = await async_client.get(
        "/api/v1/console/evaluations/templates",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, list) and len(body) >= 1
    basic = next(t for t in body if t["is_default"])
    assert basic["status"] == "published"
    assert basic["version"] == 1
    assert len(basic["config"]["criteria"]) == 9
    assert len(basic["config"]["scale"]) == 5


# ===================================================================
# 2. Direction validation
# ===================================================================


@pytest.mark.asyncio
async def test_create_direction_owner_to_staff_ok(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """Owner(admin super_owner)→Staff OK."""
    token = await _login("testadmin")
    resp = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(test_users["teststaff"]["id"], test_store_id),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "draft"


@pytest.mark.asyncio
async def test_create_direction_gm_to_owner_403(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """GM→Owner(testadmin) 403 (상위 평가 금지)."""
    token = await _login("testgm")
    resp = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(test_users["testadmin"]["id"], test_store_id),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_create_direction_gm_to_gm_403(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    seed_organization: dict,
    seed_roles: dict,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """GM→동급 GM 403. 두 번째 GM 임시 생성."""
    org_id: UUID = seed_organization["id"]
    async with async_session() as db:
        gm2 = User(
            organization_id=org_id,
            role_id=seed_roles["general_manager"],
            username=f"__eval_gm2_{uuid.uuid4().hex[:6]}",
            full_name="GM Two",
            password_hash=PW_HASH,
            is_active=True,
        )
        db.add(gm2)
        await db.commit()
        gm2_id = gm2.id
    try:
        token = await _login("testgm")
        resp = await async_client.post(
            "/api/v1/console/evaluations/",
            headers={"Authorization": f"Bearer {token}"},
            json=_payload(gm2_id, test_store_id),
        )
        assert resp.status_code == 403, resp.text
    finally:
        async with async_session() as db:
            await db.execute(delete(User).where(User.id == gm2_id))
            await db.commit()


@pytest.mark.asyncio
async def test_create_direction_self_403(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """자기 평가 403 (동급)."""
    token = await _login("testgm")
    resp = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(test_users["testgm"]["id"], test_store_id),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_create_direction_sv_to_staff_ok(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """SV→Staff OK (권한 + 방향 모두 통과)."""
    token = await _login("testsv")
    resp = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(test_users["teststaff"]["id"], test_store_id),
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_create_direction_sv_to_gm_403(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """SV→GM 403 (상위 평가 금지)."""
    token = await _login("testsv")
    resp = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(test_users["testgm"]["id"], test_store_id),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_evaluatable_users_strictly_lower(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
):
    """evaluatable-users — GM 호출 시 SV/Staff 만, 자기/상위 제외."""
    token = await _login("testgm")
    resp = await async_client.get(
        "/api/v1/console/evaluations/evaluatable-users",
        headers={"Authorization": f"Bearer {token}"},
        params={"store_id": str(test_store_id)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = body["items"]
    ids = {u["id"] for u in items}
    assert str(test_users["teststaff"]["id"]) in ids
    assert str(test_users["testsv"]["id"]) in ids
    # 자기(GM) / 상위(admin) 제외
    assert str(test_users["testgm"]["id"]) not in ids
    assert str(test_users["testadmin"]["id"]) not in ids
    # 모두 GM 보다 낮은 priority
    gm_priority = 20
    assert all(u["role_priority"] > gm_priority for u in items)
    # 각 후보는 stores[] 를 포함 (§M1) — store_id 필터 시 그 store 가 들어있어야.
    for u in items:
        assert "stores" in u and isinstance(u["stores"], list)
    # 페이지 envelope 형태
    assert body["page"] == 1
    assert body["limit"] >= 1
    assert "total" in body and "has_more" in body


@pytest.mark.asyncio
async def test_update_direction_revalidated(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """PUT 으로 evaluatee 를 상위로 바꾸면 403."""
    token = await _login("testgm")
    create = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(test_users["teststaff"]["id"], test_store_id),
    )
    assert create.status_code == 201, create.text
    eval_id = create.json()["id"]

    resp = await async_client.put(
        f"/api/v1/console/evaluations/{eval_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"evaluatee_id": str(test_users["testadmin"]["id"])},
    )
    assert resp.status_code == 403, resp.text


# ===================================================================
# 3. Submit gate
# ===================================================================


@pytest.mark.asyncio
async def test_draft_partial_ok(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """draft 는 부분 responses 허용."""
    token = await _login("testadmin")
    resp = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(
            test_users["teststaff"]["id"],
            test_store_id,
            responses={"communication": 3},
            status="draft",
        ),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "draft"


@pytest.mark.asyncio
async def test_submit_missing_criteria_400(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """submit 인데 9개 미만 → 400."""
    token = await _login("testadmin")
    resp = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(
            test_users["teststaff"]["id"],
            test_store_id,
            responses={"communication": 3},
            status="submitted",
        ),
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_submit_value_out_of_range_422(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """값 0 또는 6 → 422 (schema 검증, status 무관)."""
    token = await _login("testadmin")
    bad = _full_responses()
    bad["communication"] = 6
    resp = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(
            test_users["teststaff"]["id"],
            test_store_id,
            responses=bad,
            status="submitted",
        ),
    )
    assert resp.status_code == 422, resp.text

    bad2 = _full_responses()
    bad2["communication"] = 0
    resp2 = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(
            test_users["teststaff"]["id"], test_store_id, responses=bad2, status="draft"
        ),
    )
    assert resp2.status_code == 422, resp2.text


@pytest.mark.asyncio
async def test_valid_submit_stamps_submitted_at(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """valid submit → submitted_at stamp, status submitted."""
    token = await _login("testadmin")
    resp = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(
            test_users["teststaff"]["id"],
            test_store_id,
            responses=_full_responses(),
            status="submitted",
        ),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "submitted"
    assert body["submitted_at"] is not None


@pytest.mark.asyncio
async def test_unknown_criterion_code_400(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """snapshot 에 없는 criterion code → 400 (service)."""
    token = await _login("testadmin")
    resp = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(
            test_users["teststaff"]["id"],
            test_store_id,
            responses={"not_a_real_code": 3},
        ),
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_period_end_before_start_422(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """period_end < period_start → 422."""
    token = await _login("testadmin")
    payload = _payload(test_users["teststaff"]["id"], test_store_id)
    payload["period_start"] = "2026-03-31"
    payload["period_end"] = "2026-01-01"
    resp = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    assert resp.status_code == 422, resp.text


# ===================================================================
# 4. Snapshot + job_title
# ===================================================================


@pytest.mark.asyncio
async def test_snapshot_equals_template_and_isolated(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    seed_organization: dict,
    test_users: dict,
    test_store_id: UUID,
    test_position: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """생성된 평가 snapshot == 템플릿 config; 이후 템플릿 변경해도 snapshot 불변; job_title=position name."""
    token = await _login("testadmin")
    resp = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(
            test_users["teststaff"]["id"], test_store_id, position_id=test_position
        ),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    eval_id = body["id"]
    assert len(body["template_snapshot"]["criteria"]) == 9
    assert body["job_title"] == "__eval_test_position__"

    # 템플릿 config 를 변경(시뮬레이션) — 기존 평가 snapshot 은 그대로여야 함.
    org_id: UUID = seed_organization["id"]
    async with async_session() as db:
        tmpl = await evaluation_service.ensure_basic_template(db, org_id)
        new_cfg = build_default_config()
        new_cfg["criteria"] = new_cfg["criteria"][:1]
        tmpl.config = new_cfg
        await db.commit()

    detail = await async_client.get(
        f"/api/v1/console/evaluations/{eval_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert detail.status_code == 200, detail.text
    assert len(detail.json()["template_snapshot"]["criteria"]) == 9

    # 템플릿 원복
    async with async_session() as db:
        tmpl = await evaluation_service.ensure_basic_template(db, org_id)
        tmpl.config = build_default_config()
        await db.commit()


@pytest.mark.asyncio
async def test_job_title_refreshes_on_position_change_snapshot_stable(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    test_position: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """PUT 으로 position 변경 시 job_title 갱신, template_snapshot 은 불변."""
    token = await _login("testadmin")
    create = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(test_users["teststaff"]["id"], test_store_id),
    )
    assert create.status_code == 201, create.text
    eval_id = create.json()["id"]
    snapshot_before = create.json()["template_snapshot"]

    resp = await async_client.put(
        f"/api/v1/console/evaluations/{eval_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"position_id": str(test_position)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_title"] == "__eval_test_position__"
    assert body["template_snapshot"] == snapshot_before


# ===================================================================
# 5. Org scoping + soft delete
# ===================================================================


@pytest_asyncio.fixture
async def other_org_evaluation(eval_perms: None):
    """다른 조직 + 평가 1건 생성. (org_id, eval_id) 반환, teardown 으로 삭제."""
    async with async_session() as db:
        org = Organization(name=f"__eval_other_{uuid.uuid4().hex[:8]}")
        db.add(org)
        await db.flush()
        role = Role(organization_id=org.id, name="staff", priority=40)
        db.add(role)
        await db.flush()
        u = User(
            organization_id=org.id,
            role_id=role.id,
            username=f"__eval_other_u_{uuid.uuid4().hex[:6]}",
            full_name="Other Staff",
            password_hash=PW_HASH,
            is_active=True,
        )
        db.add(u)
        await db.flush()
        tmpl = await evaluation_service.ensure_basic_template(db, org.id)
        ev = Evaluation(
            organization_id=org.id,
            evaluator_id=None,
            evaluatee_id=u.id,
            template_id=tmpl.id,
            template_snapshot=build_default_config(),
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            responses={},
            status="draft",
        )
        db.add(ev)
        await db.commit()
        result = {"org_id": org.id, "eval_id": ev.id}
    yield result
    async with async_session() as db:
        await db.execute(delete(Evaluation).where(Evaluation.organization_id == result["org_id"]))
        await db.execute(delete(EvalTemplate).where(EvalTemplate.organization_id == result["org_id"]))
        await db.execute(delete(User).where(User.organization_id == result["org_id"]))
        await db.execute(delete(Role).where(Role.organization_id == result["org_id"]))
        await db.execute(delete(Organization).where(Organization.id == result["org_id"]))
        await db.commit()


@pytest.mark.asyncio
async def test_cross_org_get_404(
    async_client: AsyncClient, basic_template: EvalTemplate, other_org_evaluation
):
    """다른 조직 평가 GET → 404."""
    token = await _login("testadmin")
    resp = await async_client.get(
        f"/api/v1/console/evaluations/{other_org_evaluation['eval_id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_cross_org_delete_404(
    async_client: AsyncClient, basic_template: EvalTemplate, other_org_evaluation
):
    """다른 조직 평가 DELETE → 404."""
    token = await _login("testadmin")
    resp = await async_client.delete(
        f"/api/v1/console/evaluations/{other_org_evaluation['eval_id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_soft_delete_excluded_and_idempotent(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """DELETE → soft delete; 목록/상세 제외; 두 번째 DELETE → 404."""
    token = await _login("testadmin")
    create = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(test_users["teststaff"]["id"], test_store_id),
    )
    assert create.status_code == 201, create.text
    eval_id = create.json()["id"]

    d1 = await async_client.delete(
        f"/api/v1/console/evaluations/{eval_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert d1.status_code == 200, d1.text

    # 상세 404
    detail = await async_client.get(
        f"/api/v1/console/evaluations/{eval_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert detail.status_code == 404

    # 목록에서 제외
    lst = await async_client.get(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert lst.status_code == 200
    assert all(item["id"] != eval_id for item in lst.json()["items"])

    # 두 번째 DELETE 404
    d2 = await async_client.delete(
        f"/api/v1/console/evaluations/{eval_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert d2.status_code == 404


# ===================================================================
# 6. average
# ===================================================================


@pytest.mark.asyncio
async def test_average_unit():
    """compute_average — rated 평균 1-dp, 빈 dict 면 None."""
    assert evaluation_service.compute_average({}) is None
    assert evaluation_service.compute_average({"a": 4}) == 4.0
    # (4+3+5)/3 = 4.0
    assert evaluation_service.compute_average({"a": 4, "b": 3, "c": 5}) == 4.0
    # (4+3)/2 = 3.5
    assert evaluation_service.compute_average({"a": 4, "b": 3}) == 3.5
    # (1+1+2)/3 = 1.333 → 1.3
    assert evaluation_service.compute_average({"a": 1, "b": 1, "c": 2}) == 1.3


@pytest.mark.asyncio
async def test_average_in_response(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """draft 응답에도 average 계산; 빈 응답이면 None."""
    token = await _login("testadmin")
    # 빈 응답 → None
    empty = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(test_users["teststaff"]["id"], test_store_id),
    )
    assert empty.status_code == 201
    assert empty.json()["average"] is None

    # 일부 응답 → 평균
    partial = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(
            test_users["teststaff"]["id"],
            test_store_id,
            responses={"communication": 4, "work_quality": 2},
        ),
    )
    assert partial.status_code == 201
    assert partial.json()["average"] == 3.0


# ===================================================================
# 7. Dashboard compat
# ===================================================================


@pytest.mark.asyncio
async def test_dashboard_summary_excludes_soft_deleted(
    basic_template: EvalTemplate,
    seed_organization: dict,
    test_users: dict,
    test_store_id: UUID,
    cleanup_evaluations,
):
    """get_evaluation_summary 카운트가 정확하고 soft-deleted 제외."""
    from app.services.dashboard_service import dashboard_service

    org_id: UUID = seed_organization["id"]
    async with async_session() as db:
        tmpl = await evaluation_service.ensure_basic_template(db, org_id)
        # draft 2, submitted 1, soft-deleted 1
        for status in ("draft", "draft", "submitted"):
            db.add(
                Evaluation(
                    organization_id=org_id,
                    evaluatee_id=test_users["teststaff"]["id"],
                    template_id=tmpl.id,
                    template_snapshot=build_default_config(),
                    period_start=date(2026, 1, 1),
                    period_end=date(2026, 1, 31),
                    responses={},
                    status=status,
                    submitted_at=datetime.now(timezone.utc) if status == "submitted" else None,
                )
            )
        deleted = Evaluation(
            organization_id=org_id,
            evaluatee_id=test_users["teststaff"]["id"],
            template_id=tmpl.id,
            template_snapshot=build_default_config(),
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            responses={},
            status="draft",
            deleted_at=datetime.now(timezone.utc),
        )
        db.add(deleted)
        await db.commit()

        summary = await dashboard_service.get_evaluation_summary(db, org_id)
    assert summary["total_evaluations"] == 3
    assert summary["draft"] == 2
    assert summary["submitted"] == 1


# ===================================================================
# 8. employee_no
# ===================================================================


@pytest.mark.asyncio
async def test_employee_no_multiple_nulls_ok(
    seed_organization: dict, seed_roles: dict
):
    """같은 org 에 employee_no NULL 인 user 둘 공존 가능 (partial unique)."""
    org_id: UUID = seed_organization["id"]
    async with async_session() as db:
        u1 = User(
            organization_id=org_id,
            role_id=seed_roles["staff"],
            username=f"__eval_no_null1_{uuid.uuid4().hex[:6]}",
            full_name="Null One",
            password_hash=PW_HASH,
            employee_no=None,
        )
        u2 = User(
            organization_id=org_id,
            role_id=seed_roles["staff"],
            username=f"__eval_no_null2_{uuid.uuid4().hex[:6]}",
            full_name="Null Two",
            password_hash=PW_HASH,
            employee_no=None,
        )
        db.add_all([u1, u2])
        await db.commit()
        ids = [u1.id, u2.id]
        await db.execute(delete(User).where(User.id.in_(ids)))
        await db.commit()


@pytest.mark.asyncio
async def test_employee_no_duplicate_non_null_integrity_error(
    seed_organization: dict, seed_roles: dict
):
    """같은 org 에 같은 non-null employee_no 둘 → IntegrityError."""
    from sqlalchemy.exc import IntegrityError

    org_id: UUID = seed_organization["id"]
    shared = f"EMP-{uuid.uuid4().hex[:6]}"
    created_ids: list[UUID] = []
    async with async_session() as db:
        u1 = User(
            organization_id=org_id,
            role_id=seed_roles["staff"],
            username=f"__eval_no_dup1_{uuid.uuid4().hex[:6]}",
            full_name="Dup One",
            password_hash=PW_HASH,
            employee_no=shared,
        )
        db.add(u1)
        await db.commit()
        created_ids.append(u1.id)

    with pytest.raises(IntegrityError):
        async with async_session() as db:
            u2 = User(
                organization_id=org_id,
                role_id=seed_roles["staff"],
                username=f"__eval_no_dup2_{uuid.uuid4().hex[:6]}",
                full_name="Dup Two",
                password_hash=PW_HASH,
                employee_no=shared,
            )
            db.add(u2)
            await db.commit()

    async with async_session() as db:
        await db.execute(delete(User).where(User.id.in_(created_ids)))
        await db.commit()


@pytest.mark.asyncio
async def test_employee_no_passthrough_in_response(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    seed_organization: dict,
    seed_roles: dict,
    test_store_id: UUID,
    cleanup_evaluations,
):
    """평가 응답의 employee_no 가 user 값 그대로 (None passthrough)."""
    org_id: UUID = seed_organization["id"]
    async with async_session() as db:
        staffx = User(
            organization_id=org_id,
            role_id=seed_roles["staff"],
            username=f"__eval_empno_{uuid.uuid4().hex[:6]}",
            full_name="Emp No Staff",
            password_hash=PW_HASH,
            employee_no="E-12345",
            is_active=True,
        )
        db.add(staffx)
        await db.flush()
        db.add(UserStore(user_id=staffx.id, store_id=test_store_id, is_manager=False))
        await db.commit()
        staffx_id = staffx.id

    try:
        token = await _login("testadmin")
        resp = await async_client.post(
            "/api/v1/console/evaluations/",
            headers={"Authorization": f"Bearer {token}"},
            json=_payload(staffx_id, test_store_id),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["employee_no"] == "E-12345"
    finally:
        async with async_session() as db:
            await db.execute(
                delete(Evaluation).where(Evaluation.evaluatee_id == staffx_id)
            )
            await db.execute(delete(UserStore).where(UserStore.user_id == staffx_id))
            await db.execute(delete(User).where(User.id == staffx_id))
            await db.commit()


# ===================================================================
# 9. PUT / update — submit transitions & content
# ===================================================================


@pytest.mark.asyncio
async def test_put_draft_to_submitted_stamps_submitted_at(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """PUT 으로 draft→submitted 전환: 9개 채우고 status=submitted 면 submitted_at stamp."""
    token = await _login("testadmin")
    create = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(test_users["teststaff"]["id"], test_store_id),
    )
    assert create.status_code == 201, create.text
    eval_id = create.json()["id"]
    assert create.json()["submitted_at"] is None

    resp = await async_client.put(
        f"/api/v1/console/evaluations/{eval_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"responses": _full_responses(), "status": "submitted"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "submitted"
    assert body["submitted_at"] is not None
    first_submitted_at = body["submitted_at"]

    # submitted→submitted 재수정: 코멘트만 바꿔도 submitted_at 은 유지(불변).
    resp2 = await async_client.put(
        f"/api/v1/console/evaluations/{eval_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"improvement": "Keep improving", "status": "submitted"},
    )
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert body2["status"] == "submitted"
    assert body2["submitted_at"] == first_submitted_at
    assert body2["improvement"] == "Keep improving"


@pytest.mark.asyncio
async def test_put_submit_gate_missing_criteria_400(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """PUT draft→submitted 인데 9개 미만 → 400 (submit-gate)."""
    token = await _login("testadmin")
    create = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(test_users["teststaff"]["id"], test_store_id),
    )
    assert create.status_code == 201, create.text
    eval_id = create.json()["id"]

    resp = await async_client.put(
        f"/api/v1/console/evaluations/{eval_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"responses": {"communication": 4}, "status": "submitted"},
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_put_updates_responses_and_comments(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """PUT 으로 responses/comments 부분 갱신; average 재계산."""
    token = await _login("testadmin")
    create = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(test_users["teststaff"]["id"], test_store_id),
    )
    assert create.status_code == 201, create.text
    eval_id = create.json()["id"]

    resp = await async_client.put(
        f"/api/v1/console/evaluations/{eval_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "responses": {"communication": 5, "work_quality": 4},
            "good_examples": "Helped the team",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["responses"] == {"communication": 5, "work_quality": 4}
    assert body["good_examples"] == "Helped the team"
    assert body["average"] == 4.5
    assert body["status"] == "draft"


@pytest.mark.asyncio
async def test_put_unknown_code_400(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """PUT responses 에 snapshot 밖 code → 400 (service)."""
    token = await _login("testadmin")
    create = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(test_users["teststaff"]["id"], test_store_id),
    )
    assert create.status_code == 201, create.text
    eval_id = create.json()["id"]

    resp = await async_client.put(
        f"/api/v1/console/evaluations/{eval_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"responses": {"nope": 3}},
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_cross_org_put_404(
    async_client: AsyncClient, basic_template: EvalTemplate, other_org_evaluation
):
    """다른 조직 평가 PUT → 404 (cross-org 누설 방지)."""
    token = await _login("testadmin")
    resp = await async_client.put(
        f"/api/v1/console/evaluations/{other_org_evaluation['eval_id']}",
        headers={"Authorization": f"Bearer {token}"},
        json={"improvement": "x"},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_put_nonexistent_404(
    async_client: AsyncClient, basic_template: EvalTemplate
):
    """존재하지 않는 평가 PUT → 404."""
    token = await _login("testadmin")
    resp = await async_client.put(
        f"/api/v1/console/evaluations/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {token}"},
        json={"improvement": "x"},
    )
    assert resp.status_code == 404, resp.text


# ===================================================================
# 10. List filters
# ===================================================================


@pytest.mark.asyncio
async def test_list_filters_status_and_evaluatee(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    second_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """GET / — status / evaluatee_id 필터, created_at DESC, pagination."""
    token = await _login("testadmin")
    # 2 draft (staff), 1 submitted (staff)
    for _ in range(2):
        r = await async_client.post(
            "/api/v1/console/evaluations/",
            headers={"Authorization": f"Bearer {token}"},
            json=_payload(test_users["teststaff"]["id"], test_store_id),
        )
        assert r.status_code == 201, r.text
    r = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(
            test_users["teststaff"]["id"],
            test_store_id,
            responses=_full_responses(),
            status="submitted",
        ),
    )
    assert r.status_code == 201, r.text

    # 전체
    all_resp = await async_client.get(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert all_resp.status_code == 200
    assert all_resp.json()["total"] == 3

    # status=submitted → 1건
    sub = await async_client.get(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        params={"status": "submitted"},
    )
    assert sub.status_code == 200
    assert sub.json()["total"] == 1
    assert all(i["status"] == "submitted" for i in sub.json()["items"])

    # status=draft → 2건
    drafts = await async_client.get(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        params={"status": "draft"},
    )
    assert drafts.status_code == 200
    assert drafts.json()["total"] == 2

    # evaluatee_id 필터
    by_evaluatee = await async_client.get(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        params={"evaluatee_id": str(test_users["teststaff"]["id"])},
    )
    assert by_evaluatee.status_code == 200
    assert by_evaluatee.json()["total"] == 3

    # store_id 필터(접근 가능 매장) → 3건; 다른 store 면 0건
    by_store = await async_client.get(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        params={"store_id": str(test_store_id)},
    )
    assert by_store.status_code == 200
    assert by_store.json()["total"] == 3

    other_store = await async_client.get(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        params={"store_id": str(second_store_id)},
    )
    assert other_store.status_code == 200
    assert other_store.json()["total"] == 0

    # pagination
    paged = await async_client.get(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        params={"per_page": 2, "page": 1},
    )
    assert paged.status_code == 200
    body = paged.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert body["per_page"] == 2


# ===================================================================
# 11. Store-access scoping
# ===================================================================


@pytest.mark.asyncio
async def test_post_inaccessible_store_403(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    second_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """SV 가 배정되지 않은 매장으로 POST → 403 (check_store_access)."""
    # assign_stores 는 test_store_id 에만 배정 — second_store_id 는 SV 접근 불가.
    token = await _login("testsv")
    resp = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(test_users["teststaff"]["id"], second_store_id),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_list_store_filter_inaccessible_empty_page(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    second_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """SV 가 접근 불가한 store_id 로 목록 필터 → 403 아님, 빈 페이지."""
    # Owner 가 test_store 평가 1건 만든다(SV 는 그 store 접근 가능).
    admin = await _login("testadmin")
    created = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {admin}"},
        json=_payload(test_users["teststaff"]["id"], test_store_id),
    )
    assert created.status_code == 201, created.text

    sv = await _login("testsv")
    # 접근 가능한 store → 1건
    ok = await async_client.get(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {sv}"},
        params={"store_id": str(test_store_id)},
    )
    assert ok.status_code == 200
    assert ok.json()["total"] == 1

    # 접근 불가 store → 빈 페이지(403 아님)
    empty = await async_client.get(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {sv}"},
        params={"store_id": str(second_store_id)},
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["total"] == 0
    assert empty.json()["items"] == []


@pytest.mark.asyncio
async def test_detail_cross_store_404_for_non_owner(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    seed_organization: dict,
    seed_roles: dict,
    test_users: dict,
    second_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """SV 가 자기 접근 밖 store 의 평가(자기가 evaluator 도 아님) 상세 → 404."""
    # admin 이 second_store 평가 생성(SV 는 second_store 접근 불가, evaluator 도 admin).
    admin = await _login("testadmin")
    created = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {admin}"},
        json=_payload(test_users["teststaff"]["id"], second_store_id),
    )
    assert created.status_code == 201, created.text
    eval_id = created.json()["id"]

    # admin(Owner)은 본다
    admin_detail = await async_client.get(
        f"/api/v1/console/evaluations/{eval_id}",
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert admin_detail.status_code == 200

    # SV 는 접근 불가 store + evaluator 아님 → 404
    sv = await _login("testsv")
    sv_detail = await async_client.get(
        f"/api/v1/console/evaluations/{eval_id}",
        headers={"Authorization": f"Bearer {sv}"},
    )
    assert sv_detail.status_code == 404, sv_detail.text


@pytest.mark.asyncio
async def test_evaluatable_users_inaccessible_store_403(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    second_store_id: UUID,
    assign_stores,
):
    """SV 가 접근 불가 store_id 로 evaluatable-users → 403 (check_store_access 선행)."""
    sv = await _login("testsv")
    resp = await async_client.get(
        "/api/v1/console/evaluations/evaluatable-users",
        headers={"Authorization": f"Bearer {sv}"},
        params={"store_id": str(second_store_id)},
    )
    assert resp.status_code == 403, resp.text


# ===================================================================
# 12. Template detail + position validation
# ===================================================================


@pytest.mark.asyncio
async def test_get_template_by_id_and_404(
    async_client: AsyncClient, basic_template: EvalTemplate
):
    """GET /templates/{id} 200; 없는 id → 404."""
    token = await _login("testadmin")
    ok = await async_client.get(
        f"/api/v1/console/evaluations/templates/{basic_template.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["id"] == str(basic_template.id)
    assert len(ok.json()["config"]["criteria"]) == 9

    missing = await async_client.get(
        f"/api/v1/console/evaluations/templates/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert missing.status_code == 404, missing.text


@pytest.mark.asyncio
async def test_position_not_in_store_400(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    second_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """position 이 선택 store 에 속하지 않으면 → 400."""
    # second_store 에 position 생성 후, test_store 로 평가하면서 그 position 지정.
    async with async_session() as db:
        pos = Position(store_id=second_store_id, name="__eval_wrong_store_pos__")
        db.add(pos)
        await db.commit()
        await db.refresh(pos)
        wrong_pos_id = pos.id

    try:
        token = await _login("testadmin")
        resp = await async_client.post(
            "/api/v1/console/evaluations/",
            headers={"Authorization": f"Bearer {token}"},
            json=_payload(
                test_users["teststaff"]["id"],
                test_store_id,
                position_id=wrong_pos_id,
            ),
        )
        assert resp.status_code == 400, resp.text
    finally:
        async with async_session() as db:
            await db.execute(delete(Position).where(Position.id == wrong_pos_id))
            await db.commit()


# ===================================================================
# 13. Cross-org store/position IDOR (Owner bypass of check_store_access)
# ===================================================================


@pytest_asyncio.fixture
async def foreign_org_store_position():
    """다른 조직 + store + position 생성. (store_id, position_id) 반환, teardown.

    Owner 의 check_store_access 는 no-op 이므로, Owner 가 이 foreign store/position
    을 POST/PUT 에 넣어도 org 격리가 service 에서 강제되는지 검증하기 위한 fixture.
    """
    async with async_session() as db:
        org = Organization(name=f"__eval_foreign_{uuid.uuid4().hex[:8]}")
        db.add(org)
        await db.flush()
        store = Store(organization_id=org.id, name="__eval_foreign_store__")
        db.add(store)
        await db.flush()
        pos = Position(store_id=store.id, name="__eval_foreign_pos__")
        db.add(pos)
        await db.commit()
        result = {"org_id": org.id, "store_id": store.id, "position_id": pos.id}
    yield result
    async with async_session() as db:
        await db.execute(delete(Position).where(Position.store_id == result["store_id"]))
        await db.execute(delete(Store).where(Store.id == result["store_id"]))
        await db.execute(delete(Organization).where(Organization.id == result["org_id"]))
        await db.commit()


@pytest.mark.asyncio
async def test_create_foreign_org_store_404(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    foreign_org_store_position,
    cleanup_evaluations,
):
    """Owner 가 다른 조직 store_id 로 POST → 404 (org 격리, check_store_access no-op 우회)."""
    token = await _login("testadmin")
    resp = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(
            test_users["teststaff"]["id"], foreign_org_store_position["store_id"]
        ),
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_create_foreign_org_position_rejected(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    foreign_org_store_position,
    assign_stores,
    cleanup_evaluations,
):
    """Owner 가 자기 store + 다른 조직 position_id 로 POST → 거부 (400, store 불일치)."""
    token = await _login("testadmin")
    resp = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(
            test_users["teststaff"]["id"],
            test_store_id,
            position_id=foreign_org_store_position["position_id"],
        ),
    )
    # position 의 store 가 caller store 와 다르므로 400 (org 격리도 함께 차단).
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_update_foreign_org_store_404(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    foreign_org_store_position,
    assign_stores,
    cleanup_evaluations,
):
    """Owner 가 PUT 으로 store_id 를 다른 조직 store 로 바꾸면 → 404 (org 격리)."""
    token = await _login("testadmin")
    create = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json=_payload(test_users["teststaff"]["id"], test_store_id),
    )
    assert create.status_code == 201, create.text
    eval_id = create.json()["id"]

    resp = await async_client.put(
        f"/api/v1/console/evaluations/{eval_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"store_id": str(foreign_org_store_position["store_id"])},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_unauthenticated_rejected(async_client: AsyncClient):
    """인증 없이 호출 → 거부.

    - Authorization 헤더 전혀 없음 → HTTPBearer 가 403 ("Not authenticated").
    - 유효하지 않은 토큰 → get_current_user 가 401.
    """
    # 헤더 없음 → 403 (FastAPI HTTPBearer 기본 동작)
    no_header = await async_client.get("/api/v1/console/evaluations/")
    assert no_header.status_code == 403, no_header.text

    # 잘못된 토큰 → 401
    bad = await async_client.get(
        "/api/v1/console/evaluations/",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert bad.status_code == 401, bad.text


# ===================================================================
# 14. Partial draft (M6) — store/period optional, only evaluatee required
# ===================================================================


@pytest.mark.asyncio
async def test_draft_create_no_store_no_period_ok(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    assign_stores,
    cleanup_evaluations,
):
    """draft 는 store/period 없이 evaluatee_id 만으로 생성 가능 (§M6)."""
    token = await _login("testadmin")
    resp = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json={"evaluatee_id": str(test_users["teststaff"]["id"]), "status": "draft"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "draft"
    assert body["store_id"] is None
    assert body["period_start"] is None
    assert body["period_end"] is None
    assert body["position_id"] is None


@pytest.mark.asyncio
async def test_draft_create_period_only_start_ok(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    assign_stores,
    cleanup_evaluations,
):
    """draft 는 period 한쪽만 있어도 통과 (start<=end 검증은 둘 다 있을 때만)."""
    token = await _login("testadmin")
    resp = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "evaluatee_id": str(test_users["teststaff"]["id"]),
            "period_start": "2026-01-01",
            "status": "draft",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["period_start"] == "2026-01-01"
    assert body["period_end"] is None


@pytest.mark.asyncio
async def test_submit_requires_store_400(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    assign_stores,
    cleanup_evaluations,
):
    """submit 인데 store 없으면 400 (§M6 — submit 은 store 필수)."""
    token = await _login("testadmin")
    resp = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "evaluatee_id": str(test_users["teststaff"]["id"]),
            "period_start": "2026-01-01",
            "period_end": "2026-03-31",
            "responses": _full_responses(),
            "status": "submitted",
        },
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_submit_requires_period_400(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """submit 인데 period 없으면 400 (§M5/M6 — submit 은 기간 필수)."""
    token = await _login("testadmin")
    resp = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "evaluatee_id": str(test_users["teststaff"]["id"]),
            "store_id": str(test_store_id),
            "responses": _full_responses(),
            "status": "submitted",
        },
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_draft_then_put_to_submit_full(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """부분 draft → PUT 으로 store/period/responses 채워 submit 성공 (§M6)."""
    token = await _login("testadmin")
    create = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json={"evaluatee_id": str(test_users["teststaff"]["id"]), "status": "draft"},
    )
    assert create.status_code == 201, create.text
    eval_id = create.json()["id"]

    # store 없이 submit 시도 → 400 (PUT 경로 게이트)
    bad = await async_client.put(
        f"/api/v1/console/evaluations/{eval_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"responses": _full_responses(), "status": "submitted"},
    )
    assert bad.status_code == 400, bad.text

    # store + period + responses 채워 submit → 200
    ok = await async_client.put(
        f"/api/v1/console/evaluations/{eval_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "store_id": str(test_store_id),
            "period_start": "2026-01-01",
            "period_end": "2026-03-31",
            "responses": _full_responses(),
            "status": "submitted",
        },
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["status"] == "submitted"
    assert body["submitted_at"] is not None


# ===================================================================
# 15. Future period rejection (M5)
# ===================================================================


@pytest.mark.asyncio
async def test_period_future_rejected_422_create(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """미래 period_end → 422 (§M5, schema). draft 라도 거부."""
    from datetime import timedelta

    token = await _login("testadmin")
    future = (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat()
    resp = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "evaluatee_id": str(test_users["teststaff"]["id"]),
            "store_id": str(test_store_id),
            "period_start": "2026-01-01",
            "period_end": future,
            "status": "draft",
        },
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_period_future_start_rejected_422_create(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """미래 period_start → 422 (§M5)."""
    from datetime import timedelta

    token = await _login("testadmin")
    fut_start = (datetime.now(timezone.utc).date() + timedelta(days=5)).isoformat()
    fut_end = (datetime.now(timezone.utc).date() + timedelta(days=10)).isoformat()
    resp = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "evaluatee_id": str(test_users["teststaff"]["id"]),
            "store_id": str(test_store_id),
            "period_start": fut_start,
            "period_end": fut_end,
            "status": "draft",
        },
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_period_future_rejected_422_put(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_users: dict,
    test_store_id: UUID,
    assign_stores,
    cleanup_evaluations,
):
    """PUT 으로 미래 period 설정 → 422 (§M5)."""
    from datetime import timedelta

    token = await _login("testadmin")
    create = await async_client.post(
        "/api/v1/console/evaluations/",
        headers={"Authorization": f"Bearer {token}"},
        json={"evaluatee_id": str(test_users["teststaff"]["id"]), "status": "draft"},
    )
    assert create.status_code == 201, create.text
    eval_id = create.json()["id"]

    future = (datetime.now(timezone.utc).date() + timedelta(days=15)).isoformat()
    resp = await async_client.put(
        f"/api/v1/console/evaluations/{eval_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"period_start": "2026-01-01", "period_end": future},
    )
    assert resp.status_code == 422, resp.text


# ===================================================================
# 16. evaluatable-users — pagination + search + stores[] (P1/M1)
# ===================================================================


@pytest_asyncio.fixture
async def many_staff(seed_organization: dict, seed_roles: dict, test_store_id: UUID):
    """검색/페이지 테스트용 staff 35명 생성 (test_store 배정). teardown 으로 삭제."""
    org_id: UUID = seed_organization["id"]
    created: list[UUID] = []
    async with async_session() as db:
        for i in range(35):
            u = User(
                organization_id=org_id,
                role_id=seed_roles["staff"],
                username=f"__eval_page_{i:02d}_{uuid.uuid4().hex[:6]}",
                full_name=f"PageStaff {i:02d}",
                employee_no=f"PG-{i:04d}",
                password_hash=PW_HASH,
                is_active=True,
            )
            db.add(u)
            await db.flush()
            db.add(UserStore(user_id=u.id, store_id=test_store_id, is_manager=False))
            created.append(u.id)
        await db.commit()
    yield created
    async with async_session() as db:
        await db.execute(delete(UserStore).where(UserStore.user_id.in_(created)))
        await db.execute(delete(User).where(User.id.in_(created)))
        await db.commit()


@pytest.mark.asyncio
async def test_evaluatable_users_pagination_envelope_and_has_more(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_store_id: UUID,
    assign_stores,
    many_staff,
):
    """페이지 envelope: limit 적용, has_more 토글, page 2 가 page 1 과 다름 (§P1)."""
    token = await _login("testadmin")
    p1 = await async_client.get(
        "/api/v1/console/evaluations/evaluatable-users",
        headers={"Authorization": f"Bearer {token}"},
        params={"store_id": str(test_store_id), "page": 1, "limit": 10},
    )
    assert p1.status_code == 200, p1.text
    b1 = p1.json()
    assert len(b1["items"]) == 10
    assert b1["page"] == 1
    assert b1["limit"] == 10
    assert b1["total"] >= 35
    assert b1["has_more"] is True

    p2 = await async_client.get(
        "/api/v1/console/evaluations/evaluatable-users",
        headers={"Authorization": f"Bearer {token}"},
        params={"store_id": str(test_store_id), "page": 2, "limit": 10},
    )
    assert p2.status_code == 200, p2.text
    b2 = p2.json()
    assert b2["page"] == 2
    # 서로 다른 페이지 → id 겹치지 않음
    ids1 = {u["id"] for u in b1["items"]}
    ids2 = {u["id"] for u in b2["items"]}
    assert ids1.isdisjoint(ids2)

    # 마지막 페이지 → has_more False
    last_page = (b1["total"] + 9) // 10
    plast = await async_client.get(
        "/api/v1/console/evaluations/evaluatable-users",
        headers={"Authorization": f"Bearer {token}"},
        params={"store_id": str(test_store_id), "page": last_page, "limit": 10},
    )
    assert plast.status_code == 200, plast.text
    assert plast.json()["has_more"] is False


@pytest.mark.asyncio
async def test_evaluatable_users_search_by_name_and_employee_no(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    test_store_id: UUID,
    assign_stores,
    many_staff,
):
    """q 로 full_name(부분/대소문자무시) + employee_no 매치 (§P1)."""
    token = await _login("testadmin")
    # 이름 부분일치 (대소문자 무시)
    by_name = await async_client.get(
        "/api/v1/console/evaluations/evaluatable-users",
        headers={"Authorization": f"Bearer {token}"},
        params={"store_id": str(test_store_id), "q": "pagestaff 1"},
    )
    assert by_name.status_code == 200, by_name.text
    nb = by_name.json()
    # "PageStaff 10".."19" = 10명
    assert nb["total"] == 10
    assert all("PageStaff 1" in u["full_name"] for u in nb["items"])

    # employee_no 매치 — "PG-0005" → 1명
    by_no = await async_client.get(
        "/api/v1/console/evaluations/evaluatable-users",
        headers={"Authorization": f"Bearer {token}"},
        params={"store_id": str(test_store_id), "q": "PG-0005"},
    )
    assert by_no.status_code == 200, by_no.text
    nbno = by_no.json()
    assert nbno["total"] == 1
    assert nbno["items"][0]["employee_no"] == "PG-0005"

    # 매치 없음 → 0
    none = await async_client.get(
        "/api/v1/console/evaluations/evaluatable-users",
        headers={"Authorization": f"Bearer {token}"},
        params={"store_id": str(test_store_id), "q": "zzzznomatch"},
    )
    assert none.status_code == 200, none.text
    assert none.json()["total"] == 0
    assert none.json()["items"] == []


@pytest.mark.asyncio
async def test_evaluatable_users_returns_all_stores_and_primary(
    async_client: AsyncClient,
    basic_template: EvalTemplate,
    seed_organization: dict,
    seed_roles: dict,
    test_store_id: UUID,
    second_store_id: UUID,
    cleanup_evaluations,
):
    """후보 stores[] = 배정된 모든 매장, primary(store_id) = 가장 먼저 배정된 매장 (§M1/M2)."""
    org_id: UUID = seed_organization["id"]
    async with async_session() as db:
        u = User(
            organization_id=org_id,
            role_id=seed_roles["staff"],
            username=f"__eval_multistore_{uuid.uuid4().hex[:6]}",
            full_name="MultiStore Staff",
            employee_no="MS-0001",
            password_hash=PW_HASH,
            is_active=True,
        )
        db.add(u)
        await db.flush()
        uid = u.id
        # test_store 먼저 배정(primary), second_store 나중.
        first = UserStore(user_id=uid, store_id=test_store_id, is_manager=False)
        db.add(first)
        await db.flush()
        second = UserStore(user_id=uid, store_id=second_store_id, is_manager=False)
        db.add(second)
        await db.commit()

    try:
        token = await _login("testadmin")
        resp = await async_client.get(
            "/api/v1/console/evaluations/evaluatable-users",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": "MS-0001"},
        )
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert len(items) == 1
        u_resp = items[0]
        store_ids_in_resp = {s["id"] for s in u_resp["stores"]}
        assert str(test_store_id) in store_ids_in_resp
        assert str(second_store_id) in store_ids_in_resp
        assert len(u_resp["stores"]) == 2
        # primary = 가장 먼저 배정된 test_store
        assert u_resp["store_id"] == str(test_store_id)
    finally:
        async with async_session() as db:
            await db.execute(delete(UserStore).where(UserStore.user_id == uid))
            await db.execute(delete(User).where(User.id == uid))
            await db.commit()


@pytest.mark.asyncio
async def test_evaluatable_users_no_n_plus_one(
    seed_organization: dict,
    seed_roles: dict,
    test_store_id: UUID,
    eval_perms: None,
    basic_template: EvalTemplate,
    many_staff,
    monkeypatch,
):
    """N+1 제거 검증: stores/primary 를 만드는 데 후보당 추가 쿼리가 없어야 한다 (§P1).

    이전 구현은 후보당 get_primary_store(1) + db.get(Store)(1) = 2N 쿼리를 냈다.
    eager-load 로 전환됐으므로 service 가 후보 루프에서 get_primary_store 를
    전혀 호출하지 않아야 한다(0회). 또한 30명 모두 stores[] 가 채워져야 한다.
    """
    from app.repositories.evaluation_repository import evaluation_repository as repo

    org_id: UUID = seed_organization["id"]

    primary_calls = {"n": 0}
    original = repo.get_primary_store

    async def _counting_primary(db, user_id):
        primary_calls["n"] += 1
        return await original(db, user_id)

    monkeypatch.setattr(repo, "get_primary_store", _counting_primary)

    from sqlalchemy.orm import selectinload

    async with async_session() as db:
        admin = (
            await db.execute(
                select(User)
                .options(selectinload(User.role))
                .where(User.username == "testadmin")
            )
        ).scalar_one()
        result = await evaluation_service.list_evaluatable_users(
            db, admin, store_id=test_store_id, page=1, limit=30
        )

    assert len(result["items"]) == 30
    # 후보 루프에서 per-user get_primary_store 가 사라졌다 (N+1 제거).
    assert primary_calls["n"] == 0, (
        f"get_primary_store called {primary_calls['n']} times — N+1 regression"
    )
    # eager-loaded stores[] 가 후보마다 채워졌다 (test_store 1개 이상).
    assert all(len(u["stores"]) >= 1 for u in result["items"])
    assert all(
        any(s["id"] == str(test_store_id) for s in u["stores"])
        for u in result["items"]
    )
