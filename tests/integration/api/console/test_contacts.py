"""Contacts — 콘솔 API 통합 테스트 (`/api/v1/console/contacts`).

계약: docs/99_inbox/2026-08-14-연락처-API계약.md
설계: docs/99_inbox/2026-08-14-연락처(Contacts)-기능-설계.md (D1~D9)

커버 범위
    A. 권한 매트릭스 — read 없는 사람 차단 / read 만 있는 사람은 조회만 / 쓰기 권한자는 즉시 반영
    B. 가시성 (D1) — 전체공유는 SV 도 보임, 매장 지정은 미배정 SV 에게 안 보임(목록 + 상세 IDOR),
       GM/Owner 는 전 매장
    C. 검색 — 이름/회사/이메일/메모/태그/전화번호, 하이픈 번호를 숫자만으로도
    D. 번호 복수 저장·수정, 대표번호 자동 승격
    E. 태그 자유 입력 + 대소문자 흡수 + 자동완성 usage_count
    F. 신청 흐름 — create/update/delete 신청 → pending → 승인 반영 / 반려 미반영 / 취소 / 재처리 409
    G. 사유 누락 거부 (삭제 / 반려)
    H. 변경 이력 — 행위마다 audit 1행, 행위자·대상 스냅샷·사유

전제: 테스트는 lifespan 을 타지 않으므로 contacts 권한 시드를 fixture 가
idempotent 하게 보장한다 (reports/evaluations 테스트 패턴).
"""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.models.contact import (
    Contact,
    ContactAuditLog,
    ContactChangeRequest,
    ContactPhone,
    ContactTag,
    ContactTagLink,
)
from app.models.permission import Permission, RolePermission
from app.models.user import User
from app.models.user_store import UserStore

BASE = "/api/v1/console/contacts"

CONTACT_CODES = [
    "contacts:read",
    "contacts:create",
    "contacts:update",
    "contacts:delete",
]


# ---------------------------------------------------------------------------
# 로그인 / 헤더
# ---------------------------------------------------------------------------


async def _login(username: str) -> str:
    """username → access token (직접 mint — multi-org 로그인 경로 의존 끊기)."""
    from app.utils.jwt import create_access_token

    async with async_session() as db:
        user = (
            await db.execute(select(User).where(User.username == username))
        ).scalar_one()
        return create_access_token(
            {"sub": str(user.id), "org": str(user.organization_id)}
        )


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def admin_h(contact_perms) -> dict[str, str]:
    """testadmin = super_owner — 모든 permission bypass (쓰기 권한자)."""
    return _h(await _login("testadmin"))


@pytest_asyncio.fixture
async def gm_h(contact_perms) -> dict[str, str]:
    """testgm = general_manager — contacts:read 만 (쓰기는 개인 배정, D3)."""
    return _h(await _login("testgm"))


@pytest_asyncio.fixture
async def sv_h(contact_perms) -> dict[str, str]:
    """testsv = supervisor — contacts:read 만."""
    return _h(await _login("testsv"))


@pytest_asyncio.fixture
async def staff_h(contact_perms) -> dict[str, str]:
    """teststaff = staff — contacts 권한 없음."""
    return _h(await _login("teststaff"))


# ---------------------------------------------------------------------------
# 권한 시드 — DEFAULT_ROLE_PERMISSIONS 와 동일하게 강제 (gm/sv=read, staff=없음)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def contact_perms(seed_roles: dict[str, UUID]) -> None:
    async with async_session() as db:
        perms: dict[str, UUID] = {}
        for code in CONTACT_CODES:
            p = (
                await db.execute(select(Permission).where(Permission.code == code))
            ).scalar_one_or_none()
            if p is None:
                resource, action = code.split(":")
                p = Permission(code=code, resource=resource, action=action)
                db.add(p)
                await db.flush()
            perms[code] = p.id

        # 기존 매핑을 먼저 비우고 기대 상태만 다시 넣는다 (다른 테스트/수동 조작 영향 차단).
        for role_name in ("general_manager", "supervisor", "staff"):
            await db.execute(
                delete(RolePermission).where(
                    RolePermission.role_id == seed_roles[role_name],
                    RolePermission.permission_id.in_(list(perms.values())),
                )
            )
        for role_name in ("general_manager", "supervisor"):
            db.add(
                RolePermission(
                    role_id=seed_roles[role_name],
                    permission_id=perms["contacts:read"],
                )
            )
        await db.commit()


# ---------------------------------------------------------------------------
# 매장 배정 — SV 는 store A 만, GM 은 store A 매니저 (store B 는 아무에게도 배정 안 함)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def contact_stores(
    test_users: dict, test_store_id: UUID, second_store_id: UUID
):
    async with async_session() as db:
        for uname in ("testgm", "testsv", "teststaff"):
            await db.execute(
                delete(UserStore).where(UserStore.user_id == test_users[uname]["id"])
            )
        db.add(
            UserStore(
                user_id=test_users["testgm"]["id"],
                store_id=test_store_id,
                is_manager=True,
            )
        )
        db.add(
            UserStore(
                user_id=test_users["testsv"]["id"],
                store_id=test_store_id,
                is_manager=False,
            )
        )
        await db.commit()
    yield
    async with async_session() as db:
        for uname in ("testgm", "testsv", "teststaff"):
            await db.execute(
                delete(UserStore).where(UserStore.user_id == test_users[uname]["id"])
            )
        await db.commit()


# ---------------------------------------------------------------------------
# 데이터 정리 — 테스트 전후로 이 조직의 연락처 도메인 전부 하드 삭제
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def clean_contacts(seed_organization: dict):
    org_id: UUID = seed_organization["id"]

    async def _purge() -> None:
        async with async_session() as db:
            contact_ids = (
                await db.execute(
                    select(Contact.id).where(Contact.organization_id == org_id)
                )
            ).scalars().all()
            if contact_ids:
                await db.execute(
                    delete(ContactTagLink).where(
                        ContactTagLink.contact_id.in_(contact_ids)
                    )
                )
                await db.execute(
                    delete(ContactPhone).where(ContactPhone.contact_id.in_(contact_ids))
                )
            await db.execute(
                delete(ContactChangeRequest).where(
                    ContactChangeRequest.organization_id == org_id
                )
            )
            await db.execute(
                delete(ContactAuditLog).where(ContactAuditLog.organization_id == org_id)
            )
            await db.execute(delete(Contact).where(Contact.organization_id == org_id))
            await db.execute(
                delete(ContactTag).where(ContactTag.organization_id == org_id)
            )
            await db.commit()

    await _purge()
    yield
    await _purge()


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------


async def _create(
    client: AsyncClient, headers: dict[str, str], **payload
) -> dict:
    body = {"name": "Acme Plumbing"}
    body.update(payload)
    resp = await client.post(f"{BASE}/", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _audit_rows(org_id: UUID, action: str | None = None) -> list[ContactAuditLog]:
    async with async_session() as db:
        stmt = select(ContactAuditLog).where(ContactAuditLog.organization_id == org_id)
        if action:
            stmt = stmt.where(ContactAuditLog.action == action)
        return list(
            (await db.execute(stmt.order_by(ContactAuditLog.created_at))).scalars().all()
        )


def _err(resp) -> str:
    return resp.json()["error"]["code"]


# ===========================================================================
# A. 권한 매트릭스
# ===========================================================================


@pytest.mark.asyncio
async def test_user_without_read_permission_is_blocked(
    async_client: AsyncClient, staff_h: dict, contact_stores
) -> None:
    """contacts:read 가 없으면 목록도 태그도 신청도 볼 수 없다."""
    for path in (f"{BASE}/", f"{BASE}/tags", f"{BASE}/requests", f"{BASE}/requests/mine"):
        resp = await async_client.get(path, headers=staff_h)
        assert resp.status_code == 403, f"{path} → {resp.status_code}"


@pytest.mark.asyncio
async def test_read_only_user_can_list_but_not_write(
    async_client: AsyncClient, admin_h: dict, sv_h: dict, contact_stores
) -> None:
    """read 만 있는 SV: 조회 200, 생성/수정/삭제는 403."""
    contact = await _create(async_client, admin_h, name="Shared Vendor")

    assert (await async_client.get(f"{BASE}/", headers=sv_h)).status_code == 200
    assert (
        await async_client.get(f"{BASE}/{contact['id']}", headers=sv_h)
    ).status_code == 200

    resp = await async_client.post(f"{BASE}/", json={"name": "Nope"}, headers=sv_h)
    assert resp.status_code == 403

    resp = await async_client.put(
        f"{BASE}/{contact['id']}", json={"name": "Nope", "reason": "x"}, headers=sv_h
    )
    assert resp.status_code == 403

    resp = await async_client.request(
        "DELETE", f"{BASE}/{contact['id']}", json={"reason": "x"}, headers=sv_h
    )
    assert resp.status_code == 403

    # 원본은 그대로
    detail = (await async_client.get(f"{BASE}/{contact['id']}", headers=admin_h)).json()
    assert detail["name"] == "Shared Vendor"


@pytest.mark.asyncio
async def test_gm_has_read_but_no_write(
    async_client: AsyncClient, gm_h: dict, contact_stores
) -> None:
    """GM 도 기본은 read 만 (쓰기는 개인 배정, D3)."""
    assert (await async_client.get(f"{BASE}/", headers=gm_h)).status_code == 200
    resp = await async_client.post(f"{BASE}/", json={"name": "Nope"}, headers=gm_h)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_writer_creates_directly(
    async_client: AsyncClient, admin_h: dict, contact_stores
) -> None:
    """쓰기 권한자는 신청 없이 바로 반영된다."""
    body = await _create(
        async_client,
        admin_h,
        name="Acme Plumbing",
        company="Acme",
        email="ops@acme.com",
        memo="24h emergency",
        phones=[{"label": "office", "number": "213-555-0142"}],
        tags=["Vendor"],
    )
    assert body["store_id"] is None  # 미지정 = 전 매장 공유 (D1)
    assert body["phones"][0]["number_normalized"] == "2135550142"
    assert body["phones"][0]["is_primary"] is True  # 첫 번호 자동 승격
    assert [t["key"] for t in body["tags"]] == ["vendor"]

    listing = (await async_client.get(f"{BASE}/", headers=admin_h)).json()
    assert listing["total"] == 1


# ===========================================================================
# B. 가시성 (D1)
# ===========================================================================


@pytest.mark.asyncio
async def test_all_store_contact_is_visible_to_sv(
    async_client: AsyncClient, admin_h: dict, sv_h: dict, contact_stores
) -> None:
    contact = await _create(async_client, admin_h, name="Org Wide")
    listing = (await async_client.get(f"{BASE}/", headers=sv_h)).json()
    assert [c["name"] for c in listing["items"]] == ["Org Wide"]
    assert (
        await async_client.get(f"{BASE}/{contact['id']}", headers=sv_h)
    ).status_code == 200


@pytest.mark.asyncio
async def test_assigned_store_contact_is_visible_to_sv(
    async_client: AsyncClient,
    admin_h: dict,
    sv_h: dict,
    test_store_id: UUID,
    contact_stores,
) -> None:
    contact = await _create(
        async_client, admin_h, name="Store A Vendor", store_id=str(test_store_id)
    )
    listing = (await async_client.get(f"{BASE}/", headers=sv_h)).json()
    assert [c["name"] for c in listing["items"]] == ["Store A Vendor"]
    detail = await async_client.get(f"{BASE}/{contact['id']}", headers=sv_h)
    assert detail.status_code == 200
    assert detail.json()["store_name"] == "__attendance_test_store__"


@pytest.mark.asyncio
async def test_other_store_contact_is_hidden_from_sv_list_and_detail(
    async_client: AsyncClient,
    admin_h: dict,
    sv_h: dict,
    gm_h: dict,
    second_store_id: UUID,
    contact_stores,
) -> None:
    """미배정 매장 연락처는 SV 목록에 없고, 상세 직접 조회도 404 (IDOR)."""
    contact = await _create(
        async_client, admin_h, name="Store B Vendor", store_id=str(second_store_id)
    )

    listing = (await async_client.get(f"{BASE}/", headers=sv_h)).json()
    assert listing["total"] == 0

    resp = await async_client.get(f"{BASE}/{contact['id']}", headers=sv_h)
    assert resp.status_code == 404  # 403 이 아니라 404 — 존재를 숨긴다
    assert _err(resp) == "CONTACT_NOT_FOUND"

    # GM / Owner 는 전 매장
    for headers in (gm_h, admin_h):
        assert (
            await async_client.get(f"{BASE}/{contact['id']}", headers=headers)
        ).status_code == 200
        assert (await async_client.get(f"{BASE}/", headers=headers)).json()["total"] == 1


@pytest.mark.asyncio
async def test_store_filter_none_returns_all_store_contacts(
    async_client: AsyncClient, admin_h: dict, test_store_id: UUID, contact_stores
) -> None:
    await _create(async_client, admin_h, name="Org Wide")
    await _create(async_client, admin_h, name="Store A", store_id=str(test_store_id))

    only_shared = (
        await async_client.get(f"{BASE}/?store_id=none", headers=admin_h)
    ).json()
    assert [c["name"] for c in only_shared["items"]] == ["Org Wide"]

    only_store = (
        await async_client.get(f"{BASE}/?store_id={test_store_id}", headers=admin_h)
    ).json()
    assert [c["name"] for c in only_store["items"]] == ["Store A"]


@pytest.mark.asyncio
async def test_sv_cannot_pin_a_contact_to_a_store_they_cannot_access(
    async_client: AsyncClient, sv_h: dict, second_store_id: UUID, contact_stores
) -> None:
    """접근 불가 매장으로 신청하면 403 (도메인 코드) — 조용히 전체공유로 떨어뜨리지 않는다."""
    resp = await async_client.post(
        f"{BASE}/requests",
        json={
            "request_type": "create",
            "payload": {"name": "Sneaky", "store_id": str(second_store_id)},
        },
        headers=sv_h,
    )
    assert resp.status_code == 403
    assert _err(resp) == "CONTACT_STORE_FORBIDDEN"


# ===========================================================================
# C. 검색
# ===========================================================================


@pytest_asyncio.fixture
async def search_seed(async_client: AsyncClient, admin_h: dict) -> None:
    await _create(
        async_client,
        admin_h,
        name="Acme Plumbing",
        company="Acme Industrial",
        email="ops@acme-ind.com",
        memo="Emergency line after 9pm",
        phones=[{"label": "office", "number": "213-555-0142"}],
        tags=["Vendor", "Plumbing"],
    )
    await _create(
        async_client,
        admin_h,
        name="Bright Linens",
        company="Bright Co",
        email="hello@bright.example",
        memo="Weekly pickup",
        phones=[{"label": "mobile", "number": "(310) 777-8899"}],
        tags=["Laundry"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("acme plumb", "Acme Plumbing"),      # 이름
        ("industrial", "Acme Plumbing"),      # 회사
        ("acme-ind.com", "Acme Plumbing"),    # 이메일
        ("after 9pm", "Acme Plumbing"),       # 메모
        ("laundry", "Bright Linens"),         # 태그
        ("310) 777", "Bright Linens"),        # 번호 원본 표기
    ],
)
async def test_search_matches_each_field(
    async_client: AsyncClient, admin_h: dict, search_seed, q: str, expected: str
) -> None:
    body = (await async_client.get(f"{BASE}/", params={"q": q}, headers=admin_h)).json()
    assert [c["name"] for c in body["items"]] == [expected], q


@pytest.mark.asyncio
async def test_search_by_digits_only_matches_hyphenated_number(
    async_client: AsyncClient, admin_h: dict, search_seed
) -> None:
    """하이픈/괄호가 든 번호를 숫자만으로 검색해도 잡힌다 (D6)."""
    for q in ("5550142", "2135550142", "3107778899"):
        body = (
            await async_client.get(f"{BASE}/", params={"q": q}, headers=admin_h)
        ).json()
        assert body["total"] == 1, q


@pytest.mark.asyncio
async def test_short_digit_run_in_text_does_not_drag_in_every_phone(
    async_client: AsyncClient, admin_h: dict, search_seed
) -> None:
    """'after 9pm' 은 메모 검색이다 — 번호에 9 가 들어간 연락처까지 끌고 오면 안 된다.

    정규화 번호 부분일치는 3자리 이상일 때만 태운다(_MIN_PHONE_SEARCH_DIGITS).
    """
    body = (
        await async_client.get(f"{BASE}/", params={"q": "after 9pm"}, headers=admin_h)
    ).json()
    assert [c["name"] for c in body["items"]] == ["Acme Plumbing"]


@pytest.mark.asyncio
async def test_search_wildcards_are_escaped(
    async_client: AsyncClient, admin_h: dict, search_seed
) -> None:
    """사용자가 친 '%' 는 와일드카드가 아니라 리터럴이다."""
    body = (await async_client.get(f"{BASE}/", params={"q": "%"}, headers=admin_h)).json()
    assert body["total"] == 0


@pytest.mark.asyncio
async def test_tag_filter_is_case_insensitive(
    async_client: AsyncClient, admin_h: dict, search_seed
) -> None:
    body = (
        await async_client.get(f"{BASE}/", params={"tag": "VENDOR"}, headers=admin_h)
    ).json()
    assert [c["name"] for c in body["items"]] == ["Acme Plumbing"]


# ===========================================================================
# D. 전화번호 복수 저장 / 수정
# ===========================================================================


@pytest.mark.asyncio
async def test_multiple_phones_are_stored_in_order_with_one_primary(
    async_client: AsyncClient, admin_h: dict, contact_stores
) -> None:
    body = await _create(
        async_client,
        admin_h,
        phones=[
            {"label": "office", "number": "213-555-0142"},
            {"label": "mobile", "number": "310 777 8899", "is_primary": True},
        ],
    )
    assert [p["sort_order"] for p in body["phones"]] == [0, 1]
    assert [p["is_primary"] for p in body["phones"]] == [False, True]
    assert [p["number_normalized"] for p in body["phones"]] == [
        "2135550142",
        "3107778899",
    ]


@pytest.mark.asyncio
async def test_updating_phones_replaces_the_whole_list(
    async_client: AsyncClient, admin_h: dict, contact_stores
) -> None:
    contact = await _create(
        async_client, admin_h, phones=[{"number": "213-555-0142"}]
    )
    resp = await async_client.put(
        f"{BASE}/{contact['id']}",
        json={
            "reason": "New line installed",
            "phones": [
                {"label": "fax", "number": "213-555-0000"},
                {"label": "mobile", "number": "310-777-8899"},
            ],
        },
        headers=admin_h,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [p["number"] for p in body["phones"]] == ["213-555-0000", "310-777-8899"]
    assert body["phones"][0]["is_primary"] is True  # 대표 미지정 → 첫 번호 승격

    # 빈 배열이면 전부 삭제
    cleared = await async_client.put(
        f"{BASE}/{contact['id']}",
        json={"reason": "Line disconnected", "phones": []},
        headers=admin_h,
    )
    assert cleared.json()["phones"] == []


@pytest.mark.asyncio
async def test_duplicate_number_warns_instead_of_blocking(
    async_client: AsyncClient, admin_h: dict, contact_stores
) -> None:
    """같은 번호는 차단하지 않고 경고만 (N7). 조용한 실패 아님 — 응답에 실린다."""
    first = await _create(
        async_client, admin_h, name="Acme", phones=[{"number": "213-555-0142"}]
    )
    second = await _create(
        async_client, admin_h, name="Acme Two", phones=[{"number": "(213) 555 0142"}]
    )
    warnings = second["duplicate_phone_warnings"]
    assert len(warnings) == 1
    assert warnings[0]["contact_id"] == first["id"]
    assert warnings[0]["number_normalized"] == "2135550142"
    # 목록/상세에는 경고가 실리지 않는다
    detail = (await async_client.get(f"{BASE}/{second['id']}", headers=admin_h)).json()
    assert detail["duplicate_phone_warnings"] == []


# ===========================================================================
# E. 태그
# ===========================================================================


@pytest.mark.asyncio
async def test_tags_are_free_text_and_case_variants_merge(
    async_client: AsyncClient, admin_h: dict, contact_stores
) -> None:
    """'Vendor' 와 'vendor' 는 같은 태그 하나로 흡수되고 표시명은 최초 표기 유지 (D7)."""
    await _create(async_client, admin_h, name="A", tags=["Vendor"])
    await _create(async_client, admin_h, name="B", tags=["vendor", "  VENDOR  "])

    async with async_session() as db:
        rows = (
            await db.execute(select(ContactTag).where(ContactTag.key == "vendor"))
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].name == "Vendor"  # 최초 표기 유지

    tags = (await async_client.get(f"{BASE}/tags", headers=admin_h)).json()
    assert len(tags) == 1
    assert tags[0]["key"] == "vendor"
    assert tags[0]["usage_count"] == 2


@pytest.mark.asyncio
async def test_tag_autocomplete_prefix_and_visibility(
    async_client: AsyncClient,
    admin_h: dict,
    sv_h: dict,
    second_store_id: UUID,
    contact_stores,
) -> None:
    await _create(async_client, admin_h, name="A", tags=["Vendor"])
    await _create(async_client, admin_h, name="B", tags=["Laundry"])
    await _create(
        async_client,
        admin_h,
        name="Hidden",
        store_id=str(second_store_id),
        tags=["Vendor"],
    )

    hits = (await async_client.get(f"{BASE}/tags?q=ven", headers=admin_h)).json()
    assert [t["key"] for t in hits] == ["vendor"]
    assert hits[0]["usage_count"] == 2

    # SV 는 store B 연락처를 못 보므로 usage_count 도 그만큼만 (안 보이는 건수 비노출)
    sv_hits = (await async_client.get(f"{BASE}/tags?q=ven", headers=sv_h)).json()
    assert sv_hits[0]["usage_count"] == 1


@pytest.mark.asyncio
async def test_updating_tags_replaces_links(
    async_client: AsyncClient, admin_h: dict, contact_stores
) -> None:
    contact = await _create(async_client, admin_h, tags=["Vendor", "Plumbing"])
    resp = await async_client.put(
        f"{BASE}/{contact['id']}",
        json={"reason": "Recategorized", "tags": ["Plumbing"]},
        headers=admin_h,
    )
    assert [t["key"] for t in resp.json()["tags"]] == ["plumbing"]


# ===========================================================================
# F. 신청 흐름 (D4)
# ===========================================================================


async def _submit(
    client: AsyncClient, headers: dict[str, str], **body
) -> dict:
    resp = await client.post(f"{BASE}/requests", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_create_request_from_read_only_user_needs_approval(
    async_client: AsyncClient, admin_h: dict, sv_h: dict, contact_stores
) -> None:
    req = await _submit(
        async_client,
        sv_h,
        request_type="create",
        payload={"name": "New Vendor", "phones": [{"number": "213-555-1111"}]},
        reason="Met at the trade show",
    )
    assert req["status"] == "pending"
    assert req["contact_id"] is None

    # 아직 반영 안 됨
    assert (await async_client.get(f"{BASE}/", headers=admin_h)).json()["total"] == 0

    # 신청자 목록 / 처리 대기 목록
    mine = (await async_client.get(f"{BASE}/requests/mine", headers=sv_h)).json()
    assert mine["total"] == 1 and mine["items"][0]["current_contact"] is None
    pending = (await async_client.get(f"{BASE}/requests", headers=admin_h)).json()
    assert [r["id"] for r in pending["items"]] == [req["id"]]

    # 승인 → 실제 반영, 소유자는 신청자
    approve = await async_client.post(
        f"{BASE}/requests/{req['id']}/approve", json={}, headers=admin_h
    )
    assert approve.status_code == 200, approve.text
    body = approve.json()
    assert body["request"]["status"] == "approved"
    assert body["contact"]["name"] == "New Vendor"

    listing = (await async_client.get(f"{BASE}/", headers=admin_h)).json()
    assert listing["total"] == 1
    async with async_session() as db:
        contact = (
            await db.execute(select(Contact).where(Contact.name == "New Vendor"))
        ).scalar_one()
    sv_id = (await _who("testsv"))
    assert contact.created_by == sv_id


async def _who(username: str) -> UUID:
    async with async_session() as db:
        return (
            await db.execute(select(User.id).where(User.username == username))
        ).scalar_one()


@pytest.mark.asyncio
async def test_update_request_applies_only_after_approval(
    async_client: AsyncClient, admin_h: dict, sv_h: dict, contact_stores
) -> None:
    contact = await _create(async_client, admin_h, name="Acme", company="Acme")
    req = await _submit(
        async_client,
        sv_h,
        request_type="update",
        contact_id=contact["id"],
        payload={"name": "Acme", "company": "Acme Industrial"},
        reason="Company renamed",
    )
    assert req["status"] == "pending"

    still = (await async_client.get(f"{BASE}/{contact['id']}", headers=admin_h)).json()
    assert still["company"] == "Acme"
    assert still["pending_request_count"] == 1

    approve = await async_client.post(
        f"{BASE}/requests/{req['id']}/approve", json={}, headers=admin_h
    )
    assert approve.status_code == 200, approve.text
    assert approve.json()["contact"]["company"] == "Acme Industrial"

    after = (await async_client.get(f"{BASE}/{contact['id']}", headers=admin_h)).json()
    assert after["company"] == "Acme Industrial"
    assert after["pending_request_count"] == 0


@pytest.mark.asyncio
async def test_approver_can_edit_before_applying_and_original_is_kept(
    async_client: AsyncClient, admin_h: dict, sv_h: dict, contact_stores
) -> None:
    """승인자가 고쳐서 반영해도 신청 원문(payload)은 보존되고 applied_payload 에 따로 남는다."""
    contact = await _create(async_client, admin_h, name="Acme")
    req = await _submit(
        async_client,
        sv_h,
        request_type="update",
        contact_id=contact["id"],
        payload={"name": "ACME!!!"},
        reason="Fix name",
    )
    approve = await async_client.post(
        f"{BASE}/requests/{req['id']}/approve",
        json={"payload": {"name": "Acme Inc."}, "note": "Cleaned up the casing"},
        headers=admin_h,
    )
    assert approve.status_code == 200, approve.text
    body = approve.json()
    assert body["contact"]["name"] == "Acme Inc."
    assert body["request"]["payload"]["name"] == "ACME!!!"
    assert body["request"]["applied_payload"]["name"] == "Acme Inc."
    assert body["request"]["resolution_note"] == "Cleaned up the casing"


@pytest.mark.asyncio
async def test_delete_request_rejected_leaves_contact_intact(
    async_client: AsyncClient, admin_h: dict, sv_h: dict, contact_stores
) -> None:
    contact = await _create(async_client, admin_h, name="Acme")
    req = await _submit(
        async_client,
        sv_h,
        request_type="delete",
        contact_id=contact["id"],
        reason="Vendor closed",
    )
    resp = await async_client.post(
        f"{BASE}/requests/{req['id']}/reject",
        json={"reason": "They are still operating"},
        headers=admin_h,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"
    assert resp.json()["resolution_note"] == "They are still operating"

    assert (
        await async_client.get(f"{BASE}/{contact['id']}", headers=admin_h)
    ).status_code == 200

    # 신청자는 반려 사유를 볼 수 있다
    mine = (await async_client.get(f"{BASE}/requests/mine", headers=sv_h)).json()
    assert mine["items"][0]["resolution_note"] == "They are still operating"


@pytest.mark.asyncio
async def test_delete_request_approved_soft_deletes_contact(
    async_client: AsyncClient, admin_h: dict, sv_h: dict, contact_stores
) -> None:
    contact = await _create(async_client, admin_h, name="Acme")
    req = await _submit(
        async_client,
        sv_h,
        request_type="delete",
        contact_id=contact["id"],
        reason="Vendor closed",
    )
    approve = await async_client.post(
        f"{BASE}/requests/{req['id']}/approve", json={}, headers=admin_h
    )
    assert approve.status_code == 200, approve.text
    assert (
        await async_client.get(f"{BASE}/{contact['id']}", headers=admin_h)
    ).status_code == 404


@pytest.mark.asyncio
async def test_requester_can_cancel_own_request_only(
    async_client: AsyncClient, admin_h: dict, sv_h: dict, gm_h: dict, contact_stores
) -> None:
    req = await _submit(
        async_client, sv_h, request_type="create", payload={"name": "Temp"}
    )
    # 남의 신청은 취소 못 한다
    other = await async_client.post(
        f"{BASE}/requests/{req['id']}/cancel", headers=gm_h
    )
    assert other.status_code == 403
    assert _err(other) == "CONTACT_NOT_YOUR_REQUEST"

    mine = await async_client.post(f"{BASE}/requests/{req['id']}/cancel", headers=sv_h)
    assert mine.status_code == 200
    assert mine.json()["status"] == "cancelled"

    # 취소된 신청은 다시 처리할 수 없다
    again = await async_client.post(
        f"{BASE}/requests/{req['id']}/approve", json={}, headers=admin_h
    )
    assert again.status_code == 409
    assert _err(again) == "CONTACT_REQUEST_NOT_PENDING"


@pytest.mark.asyncio
async def test_approving_twice_conflicts(
    async_client: AsyncClient, admin_h: dict, sv_h: dict, contact_stores
) -> None:
    req = await _submit(
        async_client, sv_h, request_type="create", payload={"name": "Temp"}
    )
    first = await async_client.post(
        f"{BASE}/requests/{req['id']}/approve", json={}, headers=admin_h
    )
    assert first.status_code == 200
    second = await async_client.post(
        f"{BASE}/requests/{req['id']}/approve", json={}, headers=admin_h
    )
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_read_only_user_cannot_approve_or_reject(
    async_client: AsyncClient, sv_h: dict, gm_h: dict, contact_stores
) -> None:
    req = await _submit(
        async_client, sv_h, request_type="create", payload={"name": "Temp"}
    )
    approve = await async_client.post(
        f"{BASE}/requests/{req['id']}/approve", json={}, headers=gm_h
    )
    assert approve.status_code == 403
    assert _err(approve) == "CONTACT_PERMISSION_DENIED"

    reject = await async_client.post(
        f"{BASE}/requests/{req['id']}/reject", json={"reason": "no"}, headers=gm_h
    )
    assert reject.status_code == 403

    # 처리 권한이 없으면 대기 목록은 403 이 아니라 빈 페이지 (정보 비노출)
    queue = (await async_client.get(f"{BASE}/requests", headers=gm_h)).json()
    assert queue["total"] == 0


@pytest.mark.asyncio
async def test_writer_submitting_a_request_applies_immediately(
    async_client: AsyncClient, admin_h: dict, contact_stores
) -> None:
    """쓰기 권한자가 신청 엔드포인트를 써도 대기열에 쌓이지 않고 바로 반영된다."""
    req = await _submit(
        async_client, admin_h, request_type="create", payload={"name": "Fast Path"}
    )
    assert req["status"] == "approved"
    assert req["contact_id"] is not None
    assert (await async_client.get(f"{BASE}/", headers=admin_h)).json()["total"] == 1
    queue = (await async_client.get(f"{BASE}/requests", headers=admin_h)).json()
    assert queue["total"] == 0  # pending 대기열은 깨끗하다


@pytest.mark.asyncio
async def test_deleting_a_contact_supersedes_its_pending_requests(
    async_client: AsyncClient, admin_h: dict, sv_h: dict, contact_stores
) -> None:
    contact = await _create(async_client, admin_h, name="Acme")
    req = await _submit(
        async_client,
        sv_h,
        request_type="update",
        contact_id=contact["id"],
        payload={"name": "Acme Inc."},
        reason="rename",
    )
    resp = await async_client.request(
        "DELETE",
        f"{BASE}/{contact['id']}",
        json={"reason": "Duplicate entry"},
        headers=admin_h,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["superseded_request_count"] == 1

    mine = (await async_client.get(f"{BASE}/requests/mine", headers=sv_h)).json()
    assert mine["items"][0]["status"] == "superseded"
    assert req["id"] == mine["items"][0]["id"]


@pytest.mark.asyncio
async def test_request_on_invisible_contact_is_404(
    async_client: AsyncClient,
    admin_h: dict,
    sv_h: dict,
    second_store_id: UUID,
    contact_stores,
) -> None:
    contact = await _create(
        async_client, admin_h, name="Store B", store_id=str(second_store_id)
    )
    resp = await async_client.post(
        f"{BASE}/requests",
        json={
            "request_type": "delete",
            "contact_id": contact["id"],
            "reason": "nope",
        },
        headers=sv_h,
    )
    assert resp.status_code == 404
    assert _err(resp) == "CONTACT_NOT_FOUND"


# ===========================================================================
# G. 사유 누락
# ===========================================================================


@pytest.mark.asyncio
async def test_delete_without_reason_is_rejected(
    async_client: AsyncClient, admin_h: dict, contact_stores
) -> None:
    contact = await _create(async_client, admin_h, name="Acme")
    for body in ({}, {"reason": "   "}):
        resp = await async_client.request(
            "DELETE", f"{BASE}/{contact['id']}", json=body, headers=admin_h
        )
        # 계약 §6 — 사유 누락은 400 CONTACT_REASON_REQUIRED ("사유를 입력하라" 안내)
        assert resp.status_code == 400, resp.text
        assert _err(resp) == "CONTACT_REASON_REQUIRED"
    # 삭제되지 않았다
    assert (
        await async_client.get(f"{BASE}/{contact['id']}", headers=admin_h)
    ).status_code == 200


@pytest.mark.asyncio
async def test_update_without_reason_is_rejected(
    async_client: AsyncClient, admin_h: dict, contact_stores
) -> None:
    contact = await _create(async_client, admin_h, name="Acme")
    resp = await async_client.put(
        f"{BASE}/{contact['id']}", json={"name": "Acme Inc."}, headers=admin_h
    )
    assert resp.status_code == 400, resp.text
    assert _err(resp) == "CONTACT_REASON_REQUIRED"
    assert (
        await async_client.get(f"{BASE}/{contact['id']}", headers=admin_h)
    ).json()["name"] == "Acme"


@pytest.mark.asyncio
async def test_reject_without_reason_is_rejected(
    async_client: AsyncClient, admin_h: dict, sv_h: dict, contact_stores
) -> None:
    req = await _submit(
        async_client, sv_h, request_type="create", payload={"name": "Temp"}
    )
    for body in ({}, {"reason": " "}):
        resp = await async_client.post(
            f"{BASE}/requests/{req['id']}/reject", json=body, headers=admin_h
        )
        assert resp.status_code == 400, resp.text
        assert _err(resp) == "CONTACT_REASON_REQUIRED"
    # 신청은 여전히 pending
    mine = (await async_client.get(f"{BASE}/requests/mine", headers=sv_h)).json()
    assert mine["items"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_update_and_delete_requests_require_a_reason(
    async_client: AsyncClient, admin_h: dict, sv_h: dict, contact_stores
) -> None:
    contact = await _create(async_client, admin_h, name="Acme")
    for body in (
        {
            "request_type": "update",
            "contact_id": contact["id"],
            "payload": {"name": "Acme Inc."},
        },
        {"request_type": "delete", "contact_id": contact["id"]},
    ):
        resp = await async_client.post(f"{BASE}/requests", json=body, headers=sv_h)
        # 계약 §6: 이 도메인의 검증 실패는 422 가 아니라 400 + CONTACT_* 코드로 통일한다.
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "CONTACT_REASON_REQUIRED", resp.text


@pytest.mark.asyncio
async def test_malformed_requests_return_400_with_a_domain_code(
    async_client: AsyncClient, admin_h: dict, sv_h: dict, contact_stores
) -> None:
    """종류별 shape 위반도 400 + CONTACT_VALIDATION_ERROR (422 아님)."""
    contact = await _create(async_client, admin_h, name="Shape Co")
    bad_bodies = (
        # create 인데 기존 연락처를 지목
        {
            "request_type": "create",
            "contact_id": contact["id"],
            "payload": {"name": "Nope"},
        },
        # create 인데 내용 없음
        {"request_type": "create"},
        # update 인데 대상 없음
        {"request_type": "update", "payload": {"name": "Nope"}, "reason": "why"},
        # delete 인데 내용을 실어보냄
        {
            "request_type": "delete",
            "contact_id": contact["id"],
            "payload": {"name": "Nope"},
            "reason": "why",
        },
    )
    for body in bad_bodies:
        resp = await async_client.post(f"{BASE}/requests", json=body, headers=sv_h)
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "CONTACT_VALIDATION_ERROR", resp.text


# ===========================================================================
# H. 변경 이력 (D9)
# ===========================================================================


@pytest.mark.asyncio
async def test_audit_records_create_update_delete_with_actor_and_reason(
    async_client: AsyncClient,
    admin_h: dict,
    seed_organization: dict,
    test_users: dict,
    contact_stores,
) -> None:
    org_id: UUID = seed_organization["id"]
    admin_id: UUID = test_users["testadmin"]["id"]
    admin_name: str = test_users["testadmin"]["full_name"]

    contact = await _create(
        async_client,
        admin_h,
        name="Acme",
        company="Acme",
        reason="Added from the trade show",
    )
    await async_client.put(
        f"{BASE}/{contact['id']}",
        json={"company": "Acme Industrial", "reason": "Company renamed"},
        headers=admin_h,
    )
    await async_client.request(
        "DELETE",
        f"{BASE}/{contact['id']}",
        json={"reason": "Duplicate entry"},
        headers=admin_h,
    )

    rows = await _audit_rows(org_id)
    assert [r.action for r in rows] == ["create", "update", "delete"]
    for row in rows:
        assert row.actor_user_id == admin_id
        assert row.actor_name == admin_name
        assert row.actor_email  # username fallback 포함
        assert row.contact_id == UUID(contact["id"])
        assert row.contact_name == "Acme"

    create_row, update_row, delete_row = rows
    assert create_row.reason == "Added from the trade show"
    assert create_row.before is None
    assert create_row.after["name"] == "Acme"

    # update 는 바뀐 키만 남긴다
    assert update_row.reason == "Company renamed"
    assert set(update_row.after) == {"company"}
    assert update_row.before == {"company": "Acme"}
    assert update_row.after == {"company": "Acme Industrial"}

    assert delete_row.reason == "Duplicate entry"
    assert delete_row.after is None
    assert delete_row.before["company"] == "Acme Industrial"


@pytest.mark.asyncio
async def test_noop_update_leaves_no_audit_row(
    async_client: AsyncClient, admin_h: dict, seed_organization: dict, contact_stores
) -> None:
    contact = await _create(async_client, admin_h, name="Acme")
    resp = await async_client.put(
        f"{BASE}/{contact['id']}",
        json={"name": "Acme", "reason": "no actual change"},
        headers=admin_h,
    )
    assert resp.status_code == 200
    assert [r.action for r in await _audit_rows(seed_organization["id"])] == ["create"]


@pytest.mark.asyncio
async def test_audit_records_the_whole_request_lifecycle(
    async_client: AsyncClient,
    admin_h: dict,
    sv_h: dict,
    seed_organization: dict,
    test_users: dict,
    contact_stores,
) -> None:
    org_id: UUID = seed_organization["id"]
    sv_id: UUID = test_users["testsv"]["id"]

    # 1) 신청 → 승인 (이력 2행: request_approve + 실제 create)
    req = await _submit(
        async_client,
        sv_h,
        request_type="create",
        payload={"name": "New Vendor"},
        reason="Met at the trade show",
    )
    created_row = (await _audit_rows(org_id, "request_create"))[0]
    assert created_row.actor_user_id == sv_id
    assert created_row.change_request_id == UUID(req["id"])
    assert created_row.reason == "Met at the trade show"
    assert created_row.after["name"] == "New Vendor"

    await async_client.post(
        f"{BASE}/requests/{req['id']}/approve", json={}, headers=admin_h
    )
    actions = [r.action for r in await _audit_rows(org_id)]
    assert actions == ["request_create", "create", "request_approve"]
    approve_row = (await _audit_rows(org_id, "request_approve"))[0]
    assert approve_row.actor_user_id == test_users["testadmin"]["id"]
    assert approve_row.change_request_id == UUID(req["id"])

    # 2) 반려
    contact_id = (await async_client.get(f"{BASE}/", headers=admin_h)).json()["items"][0]["id"]
    rejected = await _submit(
        async_client,
        sv_h,
        request_type="delete",
        contact_id=contact_id,
        reason="Vendor closed",
    )
    await async_client.post(
        f"{BASE}/requests/{rejected['id']}/reject",
        json={"reason": "They are still operating"},
        headers=admin_h,
    )
    reject_row = (await _audit_rows(org_id, "request_reject"))[0]
    assert reject_row.reason == "They are still operating"
    assert reject_row.before == {"status": "pending"}
    assert reject_row.after == {"status": "rejected"}

    # 3) 취소
    cancelled = await _submit(
        async_client,
        sv_h,
        request_type="delete",
        contact_id=contact_id,
        reason="Second thought",
    )
    await async_client.post(f"{BASE}/requests/{cancelled['id']}/cancel", headers=sv_h)
    cancel_row = (await _audit_rows(org_id, "request_cancel"))[0]
    assert cancel_row.actor_user_id == sv_id
    assert cancel_row.after == {"status": "cancelled"}

    # 4) superseded (삭제로 무효화)
    pending = await _submit(
        async_client,
        sv_h,
        request_type="update",
        contact_id=contact_id,
        payload={"name": "Renamed"},
        reason="rename",
    )
    await async_client.request(
        "DELETE",
        f"{BASE}/{contact_id}",
        json={"reason": "Duplicate entry"},
        headers=admin_h,
    )
    superseded_row = (await _audit_rows(org_id, "request_superseded"))[0]
    assert superseded_row.change_request_id == UUID(pending["id"])
    assert superseded_row.after == {"status": "superseded"}


# ===========================================================================
# I. 신청 처리 경로의 IDOR — 대상이 안 보이면 승인/반려도 못 한다
# ===========================================================================


@pytest_asyncio.fixture
async def sv_can_write(contact_perms, seed_roles: dict[str, UUID]) -> None:
    """SV 에게 개인 배정처럼 쓰기 권한을 준다 (D3: 쓰기는 role 기본 부여가 아님).

    권한이 있어도 **가시 범위 밖 연락처**에는 손댈 수 없어야 한다는 것을 보기 위한 시드다.
    """
    async with async_session() as db:
        for code in ("contacts:update", "contacts:delete"):
            perm_id = (
                await db.execute(select(Permission.id).where(Permission.code == code))
            ).scalar_one()
            db.add(
                RolePermission(
                    role_id=seed_roles["supervisor"], permission_id=perm_id
                )
            )
        await db.commit()


@pytest.mark.asyncio
async def test_approving_a_request_for_an_invisible_contact_is_404(
    async_client: AsyncClient,
    admin_h: dict,
    gm_h: dict,
    sv_h: dict,
    second_store_id: UUID,
    contact_stores,
    sv_can_write,
) -> None:
    """쓰기 권한만으로는 안 보이는 매장의 연락처를 승인 경로로 고칠 수 없다."""
    contact = await _create(
        async_client, admin_h, name="Store B Vendor", store_id=str(second_store_id)
    )
    # GM 은 전 매장이 보이므로(D1 예외) 신청은 만들 수 있다. 쓰기 권한은 없어 pending.
    req = await _submit(
        async_client,
        gm_h,
        request_type="update",
        contact_id=contact["id"],
        payload={"name": "Hijacked"},
        reason="number changed",
    )
    assert req["status"] == "pending"

    approve = await async_client.post(
        f"{BASE}/requests/{req['id']}/approve", json={}, headers=sv_h
    )
    assert approve.status_code == 404, approve.text
    assert _err(approve) == "CONTACT_NOT_FOUND"

    # 대상은 그대로다
    detail = await async_client.get(f"{BASE}/{contact['id']}", headers=admin_h)
    assert detail.json()["name"] == "Store B Vendor"


@pytest.mark.asyncio
async def test_rejecting_a_request_for_an_invisible_contact_is_404(
    async_client: AsyncClient,
    admin_h: dict,
    gm_h: dict,
    sv_h: dict,
    second_store_id: UUID,
    contact_stores,
    sv_can_write,
) -> None:
    contact = await _create(
        async_client, admin_h, name="Store B Vendor", store_id=str(second_store_id)
    )
    req = await _submit(
        async_client,
        gm_h,
        request_type="delete",
        contact_id=contact["id"],
        reason="contract ended",
    )
    reject = await async_client.post(
        f"{BASE}/requests/{req['id']}/reject",
        json={"reason": "not ours to handle"},
        headers=sv_h,
    )
    assert reject.status_code == 404, reject.text
    assert _err(reject) == "CONTACT_NOT_FOUND"

    mine = (await async_client.get(f"{BASE}/requests/mine", headers=gm_h)).json()
    assert mine["items"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_approving_a_request_whose_contact_was_deleted_is_409(
    async_client: AsyncClient, admin_h: dict, sv_h: dict, contact_stores
) -> None:
    """삭제된 대상은 404 가 아니라 409 — 콘솔이 '반려하라'고 안내해야 한다."""
    contact = await _create(async_client, admin_h, name="Acme")
    req = await _submit(
        async_client,
        sv_h,
        request_type="update",
        contact_id=contact["id"],
        payload={"name": "Acme Inc."},
        reason="rename",
    )
    # 삭제는 pending 신청을 superseded 로 바꾸므로, 승인 시점의 상태 충돌을 보려면
    # 신청 상태를 pending 으로 되돌려 놓고(=삭제만 반영된 상황) 승인해 본다.
    await async_client.request(
        "DELETE", f"{BASE}/{contact['id']}", json={"reason": "dup"}, headers=admin_h
    )
    async with async_session() as db:
        await db.execute(
            ContactChangeRequest.__table__.update()
            .where(ContactChangeRequest.id == UUID(req["id"]))
            .values(status="pending", resolved_at=None, resolved_by=None)
        )
        await db.commit()

    approve = await async_client.post(
        f"{BASE}/requests/{req['id']}/approve", json={}, headers=admin_h
    )
    assert approve.status_code == 409, approve.text
    assert _err(approve) == "CONTACT_DELETED"
