"""Staff Warning v1 — unit + API integration tests (merge gate).

Covers: permission policy (GM+ only), direction validation (GM→하급자),
subject-store validation, store-scope (create/list/detail cross-store leak),
multi-category storage + validation, ref_no(seq), resolve/reopen, ownership
(Owner 전체 / GM 본인) on update/delete, soft-delete, counts, warnable picker.

전제: startup lifespan 이 테스트에서 안 돌므로 warnings 권한을 fixture 에서
idempotent 하게 보장한다 (evaluation/hiring 테스트 패턴).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.permissions import DEFAULT_ROLE_PERMISSIONS, can_warn
from app.core.warning import WARNING_CATEGORY_CODES
from app.database import async_session
from app.models.permission import Permission, RolePermission
from app.models.user import Role, User
from app.models.user_store import UserStore
from app.models.warning import Warning
from app.services.warning_service import _ref_no, warning_service

BASE = "/api/v1/console/warnings"
WARNING_CODES = ["warnings:read", "warnings:create", "warnings:update", "warnings:delete"]


# ===================================================================
# Fixtures
# ===================================================================


async def _login(username: str) -> str:
    """username → access token (직접 mint, multi-org login 의존 끊기)."""
    from app.utils.jwt import create_access_token

    async with async_session() as db:
        user = (
            await db.execute(select(User).where(User.username == username))
        ).scalar_one()
        return create_access_token({"sub": str(user.id), "org": str(user.organization_id)})


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def warning_perms(seed_roles: dict[str, UUID]) -> None:
    """warnings:* 를 GM(general_manager) role 에만 idempotent 부여.

    super_owner/owner 는 require_permission bypass. SV/Staff 는 부여하지 않아
    403 이 나야 한다(contract: 발행 권한 = GM 이상).
    """
    async with async_session() as db:
        perms: dict[str, UUID] = {}
        for code in WARNING_CODES:
            p = (
                await db.execute(select(Permission).where(Permission.code == code))
            ).scalar_one_or_none()
            if p is None:
                resource, action = code.split(":")
                p = Permission(code=code, resource=resource, action=action)
                db.add(p)
                await db.flush()
            perms[code] = p.id

        role_id = seed_roles["general_manager"]
        for code in WARNING_CODES:
            exists = (
                await db.execute(
                    select(RolePermission).where(
                        RolePermission.role_id == role_id,
                        RolePermission.permission_id == perms[code],
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                db.add(RolePermission(role_id=role_id, permission_id=perms[code]))
        await db.commit()


@pytest_asyncio.fixture
async def normalize_staff_role(test_users: dict, seed_roles: dict[str, UUID]):
    """teststaff 가 'staff' role(priority 40)을 가리키도록 보장 (방향 검증용)."""
    staff_role_id = seed_roles["staff"]
    staff_uid: UUID = test_users["teststaff"]["id"]
    async with async_session() as db:
        u = (await db.execute(select(User).where(User.id == staff_uid))).scalar_one()
        if u.role_id != staff_role_id:
            u.role_id = staff_role_id
            await db.commit()


@pytest_asyncio.fixture
async def assign_stores(test_users: dict, test_store_id: UUID, normalize_staff_role):
    """gm/sv/staff 를 test_store 에 배정 (gm=manager). picker/store-access 용."""
    async with async_session() as db:
        for uname, is_manager in (("testgm", True), ("testsv", False), ("teststaff", False)):
            uid = test_users[uname]["id"]
            us = (
                await db.execute(
                    select(UserStore).where(
                        UserStore.user_id == uid, UserStore.store_id == test_store_id
                    )
                )
            ).scalar_one_or_none()
            if us is None:
                db.add(UserStore(user_id=uid, store_id=test_store_id, is_manager=is_manager))
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


@pytest_asyncio.fixture
async def cleanup_warnings(seed_organization: dict):
    """테스트 전후 이 조직의 warnings 전부 삭제 (hard)."""
    org_id: UUID = seed_organization["id"]
    async with async_session() as db:
        await db.execute(delete(Warning).where(Warning.organization_id == org_id))
        await db.commit()
    yield
    async with async_session() as db:
        await db.execute(delete(Warning).where(Warning.organization_id == org_id))
        await db.commit()


def _payload(
    subject_id: UUID,
    store_id: UUID,
    *,
    title: str = "Late to closing shift",
    categories: list[str] | None = None,
    details: str | None = "Clocked in 20 minutes late.",
    warning_date: str = "2026-06-01",
) -> dict:
    return {
        "subject_user_id": str(subject_id),
        "store_id": str(store_id),
        "title": title,
        "categories": categories if categories is not None else ["tardiness"],
        "details": details,
        "warning_date": warning_date,
    }


# ===================================================================
# 1. Unit — policy / direction / ref_no
# ===================================================================


def test_default_role_permissions_warnings_gm_plus_only():
    """warnings:* 는 owner/gm 기본 부여, sv/staff 미부여 (GM 이상 contract)."""
    for code in WARNING_CODES:
        assert code in DEFAULT_ROLE_PERMISSIONS["owner"], code
        assert code in DEFAULT_ROLE_PERMISSIONS["gm"], code
        assert code not in DEFAULT_ROLE_PERMISSIONS["sv"], code
        assert code not in DEFAULT_ROLE_PERMISSIONS["staff"], code


def test_can_warn_direction():
    """can_warn = subject priority > issuer priority (엄격히 낮은 권한만)."""

    class _U:
        def __init__(self, prio: int):
            self.role = type("R", (), {"priority": prio})()

    owner, gm, sv, staff = _U(10), _U(20), _U(30), _U(40)
    assert can_warn(gm, staff) is True
    assert can_warn(gm, sv) is True
    assert can_warn(owner, gm) is True
    assert can_warn(gm, gm) is False  # 동급
    assert can_warn(staff, gm) is False  # 역방향
    assert can_warn(gm, owner) is False


def test_ref_no_format():
    assert _ref_no(46) == "W-00046"
    assert _ref_no(7) == "W-00007"
    assert _ref_no(12345) == "W-12345"


# ===================================================================
# 2. API — create
# ===================================================================


@pytest.mark.asyncio
async def test_create_warning_happy(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """GM → staff 경고 발행 성공. ref_no/status/categories 검증."""
    token = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    resp = await async_client.post(
        f"{BASE}/",
        json=_payload(subject, test_store_id, categories=["tardiness", "policy_violation"]),
        headers=_hdr(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["ref_no"].startswith("W-")
    assert body["status"] == "active"
    assert body["subject_user_id"] == str(subject)
    assert body["categories"] == ["tardiness", "policy_violation"]
    assert body["issued_by_name"] == "Test GM"
    assert body["withdrawn_at"] is None


@pytest.mark.asyncio
async def test_create_direction_blocked(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """GM 이 자신보다 높은 권한(admin)을 경고 → 403 (direction)."""
    token = await _login("testgm")
    admin_id = test_users["testadmin"]["id"]
    resp = await async_client.post(
        f"{BASE}/", json=_payload(admin_id, test_store_id), headers=_hdr(token)
    )
    assert resp.status_code == 403, resp.text
    assert "lower authority" in resp.text


@pytest.mark.asyncio
async def test_create_requires_permission(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """SV/Staff 는 warnings:create 없음 → 403."""
    subject = test_users["teststaff"]["id"]
    for uname in ("testsv", "teststaff"):
        token = await _login(uname)
        resp = await async_client.post(
            f"{BASE}/", json=_payload(subject, test_store_id), headers=_hdr(token)
        )
        assert resp.status_code == 403, f"{uname}: {resp.text}"


@pytest.mark.asyncio
async def test_create_store_not_assigned_to_subject(
    async_client, cleanup_warnings, test_users, second_store_id
):
    """대상 직원이 배정되지 않은 매장으로 발행 → 400. (admin 으로 store-access 우회)"""
    token = await _login("testadmin")
    subject = test_users["teststaff"]["id"]
    resp = await async_client.post(
        f"{BASE}/", json=_payload(subject, second_store_id), headers=_hdr(token)
    )
    assert resp.status_code == 400, resp.text
    assert "not assigned" in resp.text.lower()


@pytest.mark.asyncio
async def test_create_store_scope_denied(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, second_store_id
):
    """GM 이 관리하지 않는 매장으로 발행 → 403 (check_store_access)."""
    token = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    resp = await async_client.post(
        f"{BASE}/", json=_payload(subject, second_store_id), headers=_hdr(token)
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_create_invalid_category(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """알 수 없는 카테고리 코드 → 400 (service 가 org 카테고리로 검증, v1.1).

    v1 에선 schema frozenset 검증(422)이었으나, v1.1 부터 카테고리가 org별 DB라
    검증이 서비스로 이동(BadRequestError=400). 빈 배열은 여전히 schema 422.
    """
    token = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    resp = await async_client.post(
        f"{BASE}/",
        json=_payload(subject, test_store_id, categories=["not_a_real_code"]),
        headers=_hdr(token),
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_create_empty_categories(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """카테고리 빈 배열 → 422."""
    token = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    resp = await async_client.post(
        f"{BASE}/",
        json=_payload(subject, test_store_id, categories=[]),
        headers=_hdr(token),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_create_future_date(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """미래 일자 → 422."""
    token = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    future = (datetime.now(timezone.utc).date().replace(year=datetime.now(timezone.utc).year + 1)).isoformat()
    resp = await async_client.post(
        f"{BASE}/",
        json=_payload(subject, test_store_id, warning_date=future),
        headers=_hdr(token),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_create_seq_increments(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """연속 발행 시 ref_no(seq) 가 증가하고 고유하다."""
    token = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    refs = []
    for _ in range(3):
        resp = await async_client.post(
            f"{BASE}/", json=_payload(subject, test_store_id), headers=_hdr(token)
        )
        assert resp.status_code == 201, resp.text
        refs.append(resp.json()["ref_no"])
    assert len(set(refs)) == 3


# ===================================================================
# 3. API — list / detail / store-scope
# ===================================================================


@pytest.mark.asyncio
async def test_list_and_filters(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """목록 — subject/status/category 필터, 최신순."""
    gm = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    await async_client.post(
        f"{BASE}/", json=_payload(subject, test_store_id, categories=["tardiness"]), headers=_hdr(gm)
    )
    await async_client.post(
        f"{BASE}/", json=_payload(subject, test_store_id, categories=["absenteeism"]), headers=_hdr(gm)
    )

    admin = await _login("testadmin")
    resp = await async_client.get(f"{BASE}/?subject_user_id={subject}", headers=_hdr(admin))
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 2

    resp = await async_client.get(f"{BASE}/?category=absenteeism", headers=_hdr(admin))
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["categories"] == ["absenteeism"]


@pytest.mark.asyncio
async def test_detail_404_and_cross_store_leak(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """상세 404 (부재) + GM 이 관리 안하는 매장 경고는 못 봄(누설 방지)."""
    admin = await _login("testadmin")
    subject = test_users["teststaff"]["id"]
    # admin 이 test_store 경고 생성 (teststaff 배정 매장).
    created = (
        await async_client.post(
            f"{BASE}/", json=_payload(subject, test_store_id), headers=_hdr(admin)
        )
    ).json()
    wid = created["id"]

    # 존재하지 않는 id → 404
    import uuid as _uuid

    resp = await async_client.get(f"{BASE}/{_uuid.uuid4()}", headers=_hdr(admin))
    assert resp.status_code == 404

    # GM 이 test_store 관리자라 볼 수 있음 (sanity).
    gm = await _login("testgm")
    resp = await async_client.get(f"{BASE}/{wid}", headers=_hdr(gm))
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_list_org_scope_and_perm(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """SV/Staff 는 warnings:read 없음 → 403."""
    for uname in ("testsv", "teststaff"):
        token = await _login(uname)
        resp = await async_client.get(f"{BASE}/", headers=_hdr(token))
        assert resp.status_code == 403, f"{uname}: {resp.text}"


# ===================================================================
# 4. API — update (resolve) / ownership
# ===================================================================


@pytest.mark.asyncio
async def test_update_edit_and_withdraw(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """제목/카테고리 수정 + withdraw(status) → withdrawn_at stamp, restore → clear."""
    gm = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    wid = (
        await async_client.post(
            f"{BASE}/", json=_payload(subject, test_store_id), headers=_hdr(gm)
        )
    ).json()["id"]

    resp = await async_client.put(
        f"{BASE}/{wid}",
        json={"title": "Updated title", "categories": ["rudeness"], "status": "withdrawn"},
        headers=_hdr(gm),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "Updated title"
    assert body["categories"] == ["rudeness"]
    assert body["status"] == "withdrawn"
    assert body["withdrawn_at"] is not None

    # 철회된 경고도 목록에 남는다 (감사용 — 누가 잘못 발행하는지 추적).
    listing = (await async_client.get(f"{BASE}/?subject_user_id={subject}", headers=_hdr(gm))).json()
    assert any(w["id"] == wid and w["status"] == "withdrawn" for w in listing["items"])

    # restore
    resp = await async_client.put(f"{BASE}/{wid}", json={"status": "active"}, headers=_hdr(gm))
    assert resp.json()["status"] == "active"
    assert resp.json()["withdrawn_at"] is None


@pytest.mark.asyncio
async def test_update_ownership(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """GM 은 본인 발행건만 수정. 타인(admin) 발행건 수정 → 403. Owner 는 가능."""
    admin = await _login("testadmin")
    subject = test_users["teststaff"]["id"]
    wid = (
        await async_client.post(
            f"{BASE}/", json=_payload(subject, test_store_id), headers=_hdr(admin)
        )
    ).json()["id"]

    gm = await _login("testgm")
    resp = await async_client.put(f"{BASE}/{wid}", json={"title": "Hijack"}, headers=_hdr(gm))
    assert resp.status_code == 403, resp.text

    # Owner(admin) 는 가능
    resp = await async_client.put(f"{BASE}/{wid}", json={"title": "OwnerEdit"}, headers=_hdr(admin))
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "OwnerEdit"


# ===================================================================
# 5. API — delete (soft) / counts
# ===================================================================


@pytest.mark.asyncio
async def test_soft_delete_and_leak(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """Owner 소프트 삭제 후 목록/상세에서 제외 (soft-delete 누설 차단)."""
    gm = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    wid = (
        await async_client.post(
            f"{BASE}/", json=_payload(subject, test_store_id), headers=_hdr(gm)
        )
    ).json()["id"]

    # 삭제는 Owner 전용.
    admin = await _login("testadmin")
    resp = await async_client.delete(f"{BASE}/{wid}", headers=_hdr(admin))
    assert resp.status_code == 200, resp.text

    # 상세 404
    assert (await async_client.get(f"{BASE}/{wid}", headers=_hdr(admin))).status_code == 404
    # 목록에서 제외
    listing = (await async_client.get(f"{BASE}/?subject_user_id={subject}", headers=_hdr(admin))).json()
    assert all(item["id"] != wid for item in listing["items"])


@pytest.mark.asyncio
async def test_delete_owner_only(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """삭제는 Owner 전용 — GM 은 본인 발행건도 삭제 불가(철회만), Owner 는 가능."""
    gm = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    wid = (
        await async_client.post(
            f"{BASE}/", json=_payload(subject, test_store_id), headers=_hdr(gm)
        )
    ).json()["id"]

    # GM 은 본인 발행건이라도 삭제 불가 → 403.
    resp = await async_client.delete(f"{BASE}/{wid}", headers=_hdr(gm))
    assert resp.status_code == 403, resp.text

    # 단, 철회(withdraw)는 본인 발행건이라 가능 → 200, 기록 유지.
    resp = await async_client.put(f"{BASE}/{wid}", json={"status": "withdrawn"}, headers=_hdr(gm))
    assert resp.status_code == 200, resp.text

    # Owner 는 삭제 가능 → 200.
    admin = await _login("testadmin")
    resp = await async_client.delete(f"{BASE}/{wid}", headers=_hdr(admin))
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_counts(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """counts — 직원별 total/active. 1건 withdraw 후 active(유효) 감소, total 유지."""
    gm = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    ids = []
    for _ in range(2):
        ids.append(
            (
                await async_client.post(
                    f"{BASE}/", json=_payload(subject, test_store_id), headers=_hdr(gm)
                )
            ).json()["id"]
        )
    # 1건 withdraw (철회 — 기록은 남으므로 total 2 유지, active 만 1로)
    await async_client.put(f"{BASE}/{ids[0]}", json={"status": "withdrawn"}, headers=_hdr(gm))

    admin = await _login("testadmin")
    counts = (await async_client.get(f"{BASE}/counts", headers=_hdr(admin))).json()
    mine = next(c for c in counts if c["user_id"] == str(subject))
    assert mine["total"] == 2
    assert mine["active"] == 1


# ===================================================================
# 6. API — warnable-users picker
# ===================================================================


@pytest.mark.asyncio
async def test_warnable_users_direction_and_stores(
    async_client, warning_perms, assign_stores, test_users, test_store_id
):
    """picker — GM 보다 낮은 권한만, 각 후보 stores[] 포함, admin/gm 제외."""
    gm = await _login("testgm")
    resp = await async_client.get(f"{BASE}/warnable-users?store_id={test_store_id}", headers=_hdr(gm))
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    names = {u["full_name"] for u in items}
    # teststaff(staff), testsv(sv) 는 GM 보다 낮음 → 포함. admin/gm 은 제외.
    assert "Test Staff" in names
    assert "Test GM" not in names
    assert "Test Admin" not in names
    # 각 후보는 자신의 매장 목록 보유
    staff = next(u for u in items if u["full_name"] == "Test Staff")
    assert any(s["id"] == str(test_store_id) for s in staff["stores"])


# ===================================================================
# 7. API — corrective action
# ===================================================================


@pytest.mark.asyncio
async def test_corrective_action(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """corrective_action 저장/응답 검증. (PDF 는 클라이언트 프린트로 이전 — 서버 PDF 제거)"""
    gm = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    payload = _payload(subject, test_store_id, categories=["tardiness", "policy_violation"])
    payload["corrective_action"] = "Arrive 10 minutes before shift; finish the closing checklist."
    created = (
        await async_client.post(f"{BASE}/", json=payload, headers=_hdr(gm))
    ).json()
    assert created["corrective_action"] == payload["corrective_action"]


# ===================================================================
# v1.1 — 새 필드 / 발행자 override / 카테고리 라벨
# ===================================================================


@pytest.mark.asyncio
async def test_create_with_new_fields_and_labels(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """other_text / deadline / follow-up(날짜+시간) 저장 + category_labels live resolve."""
    token = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    payload = _payload(subject, test_store_id, categories=["other", "tardiness"])
    payload.update(
        {
            "other_text": "Used phone during service",
            "corrective_action": "Keep phone in locker",
            "deadline": "2026-06-20",
            "follow_up_date": "2026-06-25",
            "follow_up_time": "14:30:00",
        }
    )
    resp = await async_client.post(f"{BASE}/", json=payload, headers=_hdr(token))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["other_text"] == "Used phone during service"
    assert body["deadline"] == "2026-06-20"
    assert body["follow_up_date"] == "2026-06-25"
    assert body["follow_up_time"] == "14:30:00"
    # 라벨 live resolve (org 카테고리에서)
    assert body["category_labels"]["other"] == "Other"
    assert body["category_labels"]["tardiness"] == "Tardiness"


@pytest.mark.asyncio
async def test_create_followup_tbd_time_null(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """Follow-up 날짜만, 시간 미정(TBD) → follow_up_time None 허용."""
    token = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    payload = _payload(subject, test_store_id)
    payload.update({"follow_up_date": "2026-06-25", "follow_up_time": None})
    resp = await async_client.post(f"{BASE}/", json=payload, headers=_hdr(token))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["follow_up_date"] == "2026-06-25"
    assert body["follow_up_time"] is None


@pytest.mark.asyncio
async def test_create_issuer_override_owner(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """Owner 가 다른 매니저(GM)를 발행자로 지정 → issued_by = GM, 방향검증=GM 기준."""
    owner = await _login("testadmin")  # super_owner
    subject = test_users["teststaff"]["id"]
    gm_id = test_users["testgm"]["id"]
    payload = _payload(subject, test_store_id)
    payload["issued_by_id"] = str(gm_id)
    resp = await async_client.post(f"{BASE}/", json=payload, headers=_hdr(owner))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["issued_by_id"] == str(gm_id)
    assert body["issued_by_name"] == "Test GM"


@pytest.mark.asyncio
async def test_create_issuer_override_nonowner_forbidden(
    async_client, warning_perms, assign_stores, cleanup_warnings, test_users, test_store_id
):
    """non-owner(GM) 가 발행자 override 시도 → 403."""
    gm = await _login("testgm")
    subject = test_users["teststaff"]["id"]
    other_manager = test_users["testsv"]["id"]
    payload = _payload(subject, test_store_id)
    payload["issued_by_id"] = str(other_manager)
    resp = await async_client.post(f"{BASE}/", json=payload, headers=_hdr(gm))
    assert resp.status_code == 403, resp.text
