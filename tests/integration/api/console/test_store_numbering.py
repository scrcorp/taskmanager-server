"""API integration tests — 채번 커서 API + 매장 소프트 삭제 + 번호대 문맥 거절 (트랙 S3).

계약 SoT: `docs/99_inbox/2026-08-18 empid 채번 API계약·규칙.md`

대상:
    - GET    /api/v1/console/stores            (§3-1 numbering 추가)
    - GET    /api/v1/console/stores/{id}       (§3-1)
    - GET    /api/v1/console/store-groups      (§3-1)
    - PUT    /api/v1/console/stores/{id}/numbering              (§3-2)
    - PUT    /api/v1/console/store-groups/{id}/numbering        (§3-2)
    - POST   /api/v1/console/.../numbering/recalculate          (§3-3)
    - DELETE /api/v1/console/stores/{id}       (§3-7 소프트 삭제로 동작 변경)
    - §4 ERR_REASON_REQUIRED / ERR_CURSOR_INVALID / ERR_RANGE_IGNORED

주의: 매장을 만들면 조직의 Owner 전원이 자동 배정되며 그때 커서가 전진한다.
따라서 커서의 **절대값을 가정하지 않고** 조회한 값 기준의 상대 비교로 검증한다.
"""

from __future__ import annotations

from typing import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.models.empid_change import EMPID_SOURCE_CURSOR, EmpidChange
from app.models.org_member import (
    EMPID_KIND_EXCEPTION,
    EMPID_KIND_SEQUENCE,
    OrgMember,
    OrgMemberStore,
)
from app.models.organization import STORE_STATUS_CLOSED, Store, StoreGroup
from app.models.user import Role, User

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/console"

# 계약 §3-1 의 numbering 객체 키 — 하나라도 빠지면 콘솔이 계산을 되살리게 된다.
NUMBERING_KEYS = {
    "next_empid",
    "recommended",
    "exception_count",
    "sequence_count",
    "mismatch",
    "scope",
    "scope_id",
}


# ---------------------------------------------------------------------------
# 픽스처 — 테스트가 만든 매장/그룹은 끝나고 하드 삭제로 정리 (소프트 삭제는 남는다)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def cleanup() -> AsyncIterator[dict]:
    """생성한 store/group/user id 를 모아 테스트 종료 시 정리."""
    bag: dict = {"stores": [], "groups": [], "users": []}
    try:
        yield bag
    finally:
        async with async_session() as s:
            if bag["stores"]:
                await s.execute(delete(Store).where(Store.id.in_(bag["stores"])))
            if bag["groups"]:
                await s.execute(
                    delete(StoreGroup).where(StoreGroup.id.in_(bag["groups"]))
                )
            if bag["users"]:
                await s.execute(delete(User).where(User.id.in_(bag["users"])))
            await s.commit()


async def _create_group(
    client: AsyncClient,
    headers: dict,
    cleanup: dict,
    *,
    numbering_mode: str = "group",
    number_range_start: int | None = 7000,
) -> dict:
    resp = await client.post(
        f"{BASE}/store-groups",
        headers=headers,
        json={
            "name": f"__s3grp_{uuid4().hex[:8]}__",
            "numbering_mode": numbering_mode,
            "number_range_start": number_range_start,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    cleanup["groups"].append(UUID(body["id"]))
    return body


async def _create_store(
    client: AsyncClient,
    headers: dict,
    cleanup: dict,
    *,
    group_id: str | None = None,
    number_range_start: int | None = None,
    expect: int = 201,
) -> dict:
    payload: dict = {"name": f"__s3store_{uuid4().hex[:8]}__"}
    if group_id is not None:
        payload["group_id"] = group_id
    if number_range_start is not None:
        payload["number_range_start"] = number_range_start
    resp = await client.post(f"{BASE}/stores", headers=headers, json=payload)
    assert resp.status_code == expect, resp.text
    body = resp.json()
    if resp.status_code == 201:
        cleanup["stores"].append(UUID(body["id"]))
    return body


async def _seed_empid(
    org_id: UUID, store_id: UUID, empid: int, kind: str, cleanup: dict
) -> None:
    """지정 번호/구분의 배정 행을 직접 만든다 (수동 기입 흉내 — 커서는 건드리지 않는다).

    임의의 empid·empid_kind 를 만드는 API 는 임포트 커밋뿐이고 그쪽은 다른 트랙이
    바꾸는 중이라, 재계산 입력값 준비는 DB 로 한다.
    """
    async with async_session() as s:
        role_id = (
            await s.execute(select(Role.id).where(Role.organization_id == org_id).limit(1))
        ).scalar_one()
        user = User(
            organization_id=org_id,
            role_id=role_id,
            username=f"__s3num_{uuid4().hex[:8]}",
            full_name="S3 Numbering",
            password_hash="x",
            is_active=True,
        )
        s.add(user)
        await s.flush()
        member = OrgMember(user_id=user.id, organization_id=org_id, role_id=role_id)
        s.add(member)
        await s.flush()
        s.add(
            OrgMemberStore(
                org_member_id=member.id,
                store_id=store_id,
                empid=empid,
                empid_kind=kind,
            )
        )
        await s.commit()
        cleanup["users"].append(user.id)


# ---------------------------------------------------------------------------
# §3-1 — 조회 응답에 numbering 추가 (기존 필드는 그대로)
# ---------------------------------------------------------------------------


async def test_store_detail_includes_numbering(
    async_client: AsyncClient, admin_headers: dict, test_store_id: UUID
) -> None:
    """매장 상세에 numbering 객체가 붙고 기존 필드는 그대로 남는다."""
    resp = await async_client.get(f"{BASE}/stores/{test_store_id}", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 기존 필드 유지 (추가만 한다는 계약)
    assert "name" in body and "number_range_start" in body and "group_id" in body
    numbering = body["numbering"]
    assert set(numbering) >= NUMBERING_KEYS
    assert numbering["scope"] in ("store", "group")
    assert isinstance(numbering["recommended"], int)
    assert isinstance(numbering["mismatch"], bool)


async def test_store_list_includes_numbering(
    async_client: AsyncClient, admin_headers: dict, test_store_id: UUID
) -> None:
    """매장 목록의 모든 행에 numbering 이 실린다."""
    resp = await async_client.get(f"{BASE}/stores", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    stores = resp.json()
    assert stores
    for store in stores:
        assert store["numbering"] is not None, store["name"]
        assert set(store["numbering"]) >= NUMBERING_KEYS


async def test_group_list_includes_numbering(
    async_client: AsyncClient, admin_headers: dict, cleanup: dict
) -> None:
    """그룹 목록에 numbering 이 실리고 scope 는 그룹 자신이다."""
    group = await _create_group(async_client, admin_headers, cleanup)
    resp = await async_client.get(f"{BASE}/store-groups", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    target = next(g for g in resp.json() if g["id"] == group["id"])
    numbering = target["numbering"]
    assert set(numbering) >= NUMBERING_KEYS
    assert numbering["scope"] == "group"
    assert numbering["scope_id"] == group["id"]
    # 빈 그룹의 커서는 번호대 시작값에서 출발한다
    assert numbering["next_empid"] == 7000
    assert numbering["recommended"] == 7000


async def test_shared_group_store_points_at_group_scope(
    async_client: AsyncClient, admin_headers: dict, cleanup: dict
) -> None:
    """Shared 그룹 소속 매장은 scope="group" + 그룹 id 를 받는다 (콘솔의 수정 대상)."""
    group = await _create_group(async_client, admin_headers, cleanup)
    store = await _create_store(
        async_client, admin_headers, cleanup, group_id=group["id"]
    )
    detail = await async_client.get(
        f"{BASE}/stores/{store['id']}", headers=admin_headers
    )
    numbering = detail.json()["numbering"]
    assert numbering["scope"] == "group"
    assert numbering["scope_id"] == group["id"]


async def test_standalone_store_owns_its_cursor(
    async_client: AsyncClient, admin_headers: dict, cleanup: dict
) -> None:
    """미그룹 매장은 scope="store" + 자기 id. 커서는 번호대에서 출발한다."""
    store = await _create_store(
        async_client, admin_headers, cleanup, number_range_start=4000
    )
    numbering = store["numbering"]
    assert numbering["scope"] == "store"
    assert numbering["scope_id"] == store["id"]
    # 생성 시 Owner 자동 배정으로 커서가 전진하므로 시작값 이상이면 된다
    assert numbering["next_empid"] >= 4000


# ---------------------------------------------------------------------------
# §4 ERR_RANGE_IGNORED — Shared 그룹 매장의 번호대 입력은 거절
# ---------------------------------------------------------------------------


async def test_create_store_in_shared_group_rejects_range_start(
    async_client: AsyncClient, admin_headers: dict, cleanup: dict
) -> None:
    """Shared 그룹 매장에 number_range_start 를 보내면 422 로 거절한다 (조용한 저장 금지)."""
    group = await _create_group(async_client, admin_headers, cleanup)
    resp = await async_client.post(
        f"{BASE}/stores",
        headers=admin_headers,
        json={
            "name": f"__s3store_{uuid4().hex[:8]}__",
            "group_id": group["id"],
            "number_range_start": 5000,
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "ERR_RANGE_IGNORED"


async def test_create_store_in_per_store_group_allows_range_start(
    async_client: AsyncClient, admin_headers: dict, cleanup: dict
) -> None:
    """Per-store 그룹은 매장 번호대가 실제로 쓰이므로 허용한다."""
    group = await _create_group(
        async_client, admin_headers, cleanup, numbering_mode="store"
    )
    store = await _create_store(
        async_client,
        admin_headers,
        cleanup,
        group_id=group["id"],
        number_range_start=5000,
    )
    assert store["number_range_start"] == 5000
    assert store["numbering"]["scope"] == "store"


async def test_update_store_in_shared_group_rejects_range_start(
    async_client: AsyncClient, admin_headers: dict, cleanup: dict
) -> None:
    """이미 Shared 그룹에 속한 매장의 번호대 수정도 같은 코드로 거절한다."""
    group = await _create_group(async_client, admin_headers, cleanup)
    store = await _create_store(
        async_client, admin_headers, cleanup, group_id=group["id"]
    )
    resp = await async_client.put(
        f"{BASE}/stores/{store['id']}",
        headers=admin_headers,
        json={"number_range_start": 6000},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "ERR_RANGE_IGNORED"


async def test_update_store_can_clear_range_start_in_shared_group(
    async_client: AsyncClient, admin_headers: dict, cleanup: dict
) -> None:
    """명시적 null(해제)은 허용 — 지우는 것은 조용한 실패가 아니다."""
    store = await _create_store(
        async_client, admin_headers, cleanup, number_range_start=4000
    )
    group = await _create_group(async_client, admin_headers, cleanup)
    resp = await async_client.put(
        f"{BASE}/stores/{store['id']}",
        headers=admin_headers,
        json={"group_id": group["id"], "number_range_start": None},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["number_range_start"] is None


# ---------------------------------------------------------------------------
# §3-2 — 커서 수동 조정
# ---------------------------------------------------------------------------


async def test_group_numbering_update_requires_reason(
    async_client: AsyncClient, admin_headers: dict, cleanup: dict
) -> None:
    """사유 누락 → ERR_REASON_REQUIRED (422). 공백만 있는 것도 누락이다."""
    group = await _create_group(async_client, admin_headers, cleanup)
    for reason in (None, "   "):
        resp = await async_client.put(
            f"{BASE}/store-groups/{group['id']}/numbering",
            headers=admin_headers,
            json={"next_empid": 7100, "reason": reason},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "ERR_REASON_REQUIRED"


async def test_group_numbering_update_rejects_invalid_cursor(
    async_client: AsyncClient, admin_headers: dict, cleanup: dict
) -> None:
    """0 이하 커서 → ERR_CURSOR_INVALID (422)."""
    group = await _create_group(async_client, admin_headers, cleanup)
    resp = await async_client.put(
        f"{BASE}/store-groups/{group['id']}/numbering",
        headers=admin_headers,
        json={"next_empid": 0, "reason": "why not"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "ERR_CURSOR_INVALID"


async def test_group_numbering_update_applies_and_logs(
    async_client: AsyncClient, admin_headers: dict, cleanup: dict
) -> None:
    """커서를 올리면 저장되고 previous 가 실리며 empid_changes(source='cursor')가 남는다."""
    group = await _create_group(async_client, admin_headers, cleanup)
    resp = await async_client.put(
        f"{BASE}/store-groups/{group['id']}/numbering",
        headers=admin_headers,
        json={"next_empid": 7300, "reason": "임포트 후 커서 정정"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["next_empid"] == 7300
    assert body["previous"] == 7000
    assert body["lowered"] is False
    assert body["scope"] == "group" and body["scope_id"] == group["id"]

    # 조회 응답에도 반영
    listed = await async_client.get(f"{BASE}/store-groups", headers=admin_headers)
    target = next(g for g in listed.json() if g["id"] == group["id"])
    assert target["numbering"]["next_empid"] == 7300

    # 이력 — 커서 값의 old→new, user_id 는 비어 있다
    async with async_session() as s:
        rows = (
            await s.execute(
                select(EmpidChange).where(
                    EmpidChange.source == EMPID_SOURCE_CURSOR,
                    EmpidChange.new_empid == 7300,
                    EmpidChange.old_empid == 7000,
                )
            )
        ).scalars().all()
    assert rows, "커서 변경 이력이 없다"
    assert any(r.user_id is None and r.reason for r in rows)


async def test_group_numbering_update_lowering_is_allowed_and_flagged(
    async_client: AsyncClient, admin_headers: dict, cleanup: dict
) -> None:
    """낮추는 것도 허용하되 lowered=true 로 알린다 (INV-2 의 유일한 예외)."""
    group = await _create_group(async_client, admin_headers, cleanup)
    await async_client.put(
        f"{BASE}/store-groups/{group['id']}/numbering",
        headers=admin_headers,
        json={"next_empid": 7500, "reason": "up"},
    )
    resp = await async_client.put(
        f"{BASE}/store-groups/{group['id']}/numbering",
        headers=admin_headers,
        json={"next_empid": 7100, "reason": "운영자 수동 하향"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["lowered"] is True
    assert body["previous"] == 7500
    assert body["next_empid"] == 7100


async def test_store_numbering_update_rejected_for_shared_group_store(
    async_client: AsyncClient, admin_headers: dict, cleanup: dict
) -> None:
    """Shared 그룹 매장의 커서는 그룹이 갖는다 — 매장 경로는 거절한다."""
    group = await _create_group(async_client, admin_headers, cleanup)
    store = await _create_store(
        async_client, admin_headers, cleanup, group_id=group["id"]
    )
    resp = await async_client.put(
        f"{BASE}/stores/{store['id']}/numbering",
        headers=admin_headers,
        json={"next_empid": 7200, "reason": "nope"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "ERR_RANGE_IGNORED"


async def test_store_numbering_update_on_standalone_store(
    async_client: AsyncClient, admin_headers: dict, cleanup: dict
) -> None:
    """미그룹 매장은 자기 커서를 직접 고칠 수 있다."""
    store = await _create_store(
        async_client, admin_headers, cleanup, number_range_start=4000
    )
    before = store["numbering"]["next_empid"]
    resp = await async_client.put(
        f"{BASE}/stores/{store['id']}/numbering",
        headers=admin_headers,
        json={"next_empid": before + 100, "reason": "매장 커서 정정"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["next_empid"] == before + 100
    assert body["previous"] == before
    assert body["scope"] == "store" and body["scope_id"] == store["id"]


# ---------------------------------------------------------------------------
# §3-3 — 재계산 (RULE-C) · RULE-E 불일치
# ---------------------------------------------------------------------------


async def test_recalculate_preview_does_not_apply(
    async_client: AsyncClient, admin_headers: dict, cleanup: dict, seed_organization: dict
) -> None:
    """apply=false 는 미리보기 — 커서를 건드리지 않고 권장값만 준다."""
    group = await _create_group(async_client, admin_headers, cleanup)
    store = await _create_store(
        async_client, admin_headers, cleanup, group_id=group["id"]
    )
    await _seed_empid(
        seed_organization["id"], UUID(store["id"]), 7050, EMPID_KIND_SEQUENCE, cleanup
    )

    resp = await async_client.post(
        f"{BASE}/store-groups/{group['id']}/numbering/recalculate",
        headers=admin_headers,
        json={"apply": False, "reason": None},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is False
    assert body["recommended"] == 7051
    assert body["next_empid"] == 7000  # 커서는 그대로
    assert body["previous"] == 7000

    listed = await async_client.get(f"{BASE}/store-groups", headers=admin_headers)
    target = next(g for g in listed.json() if g["id"] == group["id"])
    assert target["numbering"]["next_empid"] == 7000
    # RULE-E — 커서가 순번 MAX 를 따라가지 못하므로 경고가 켜진다
    assert target["numbering"]["mismatch"] is True


async def test_recalculate_apply_requires_reason(
    async_client: AsyncClient, admin_headers: dict, cleanup: dict
) -> None:
    """apply=true 인데 사유가 없으면 ERR_REASON_REQUIRED (422)."""
    group = await _create_group(async_client, admin_headers, cleanup)
    resp = await async_client.post(
        f"{BASE}/store-groups/{group['id']}/numbering/recalculate",
        headers=admin_headers,
        json={"apply": True},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "ERR_REASON_REQUIRED"


async def test_recalculate_excludes_exceptions(
    async_client: AsyncClient, admin_headers: dict, cleanup: dict, seed_organization: dict
) -> None:
    """예외 번호는 재계산에서 빠지고 건수만 함께 나간다 (RULE-C) — 이 트랙의 핵심."""
    org_id: UUID = seed_organization["id"]
    group = await _create_group(async_client, admin_headers, cleanup)
    store = await _create_store(
        async_client, admin_headers, cleanup, group_id=group["id"]
    )
    await _seed_empid(org_id, UUID(store["id"]), 7050, EMPID_KIND_SEQUENCE, cleanup)
    # 대역 밖 예외 번호 — 있어도 순번을 끌고 올라가면 안 된다
    await _seed_empid(org_id, UUID(store["id"]), 9900, EMPID_KIND_EXCEPTION, cleanup)

    resp = await async_client.post(
        f"{BASE}/store-groups/{group['id']}/numbering/recalculate",
        headers=admin_headers,
        json={"apply": True, "reason": "임포트 정리 후 재계산"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert body["next_empid"] == 7051  # 9901 이 아니다
    assert body["recommended"] == 7051
    assert body["exception_count"] == 1
    assert body["sequence_count"] >= 1
    assert body["mismatch"] is False

    listed = await async_client.get(f"{BASE}/store-groups", headers=admin_headers)
    target = next(g for g in listed.json() if g["id"] == group["id"])
    assert target["numbering"]["next_empid"] == 7051
    assert target["numbering"]["mismatch"] is False


# ---------------------------------------------------------------------------
# §3-7 — 매장 삭제는 폐점(soft delete)
# ---------------------------------------------------------------------------


async def test_delete_store_is_soft_and_keeps_empids(
    async_client: AsyncClient, admin_headers: dict, cleanup: dict, seed_organization: dict
) -> None:
    """DELETE 는 204 그대로지만 행은 남는다 — status=closed + deleted_at, EMPID 보존."""
    org_id: UUID = seed_organization["id"]
    store = await _create_store(
        async_client, admin_headers, cleanup, number_range_start=4200
    )
    store_id = UUID(store["id"])
    await _seed_empid(org_id, store_id, 4250, EMPID_KIND_SEQUENCE, cleanup)

    resp = await async_client.delete(f"{BASE}/stores/{store_id}", headers=admin_headers)
    assert resp.status_code == 204, resp.text

    async with async_session() as s:
        row = (await s.execute(select(Store).where(Store.id == store_id))).scalar_one_or_none()
        assert row is not None, "하드 삭제됐다 — 번호 점유가 풀린다"
        assert row.status == STORE_STATUS_CLOSED
        assert row.deleted_at is not None
        empids = (
            await s.execute(
                select(OrgMemberStore.empid).where(OrgMemberStore.store_id == store_id)
            )
        ).scalars().all()
    assert 4250 in empids, "폐점 매장의 EMPID 가 사라졌다"

    # 기본 목록에서는 빠지고 include_closed 로는 보인다
    listed = await async_client.get(f"{BASE}/stores", headers=admin_headers)
    assert all(s["id"] != str(store_id) for s in listed.json())
    with_closed = await async_client.get(
        f"{BASE}/stores?include_closed=true", headers=admin_headers
    )
    assert any(s["id"] == str(store_id) for s in with_closed.json())


async def test_delete_store_twice_is_idempotent(
    async_client: AsyncClient, admin_headers: dict, cleanup: dict
) -> None:
    """두 번 눌러도 204 — 두 번째만 404 를 내면 "없는 매장"으로 오인된다."""
    store = await _create_store(async_client, admin_headers, cleanup)
    first = await async_client.delete(f"{BASE}/stores/{store['id']}", headers=admin_headers)
    second = await async_client.delete(f"{BASE}/stores/{store['id']}", headers=admin_headers)
    assert first.status_code == 204, first.text
    assert second.status_code == 204, second.text


async def test_delete_unknown_store_is_404(
    async_client: AsyncClient, admin_headers: dict
) -> None:
    """존재하지 않는 매장은 여전히 404 (소프트 삭제로 바뀌어도 유지)."""
    resp = await async_client.delete(f"{BASE}/stores/{uuid4()}", headers=admin_headers)
    assert resp.status_code == 404, resp.text


async def test_closed_store_releases_its_name(
    async_client: AsyncClient, admin_headers: dict, cleanup: dict
) -> None:
    """폐점 매장은 이름을 놓아준다 — 행이 남는다고 이름이 영구히 잠기면 안 된다.

    코드(code)는 이미 폐점 시 풀리는 규칙이었다. 삭제가 소프트로 바뀐 뒤 이름만
    잠기면 같은 자리에 매장을 다시 열 수 없다.
    """
    name = f"__s3reopen_{uuid4().hex[:8]}__"
    first = await async_client.post(
        f"{BASE}/stores", headers=admin_headers, json={"name": name}
    )
    assert first.status_code == 201, first.text
    cleanup["stores"].append(UUID(first.json()["id"]))

    await async_client.delete(
        f"{BASE}/stores/{first.json()['id']}", headers=admin_headers
    )

    second = await async_client.post(
        f"{BASE}/stores", headers=admin_headers, json={"name": name}
    )
    assert second.status_code == 201, second.text
    cleanup["stores"].append(UUID(second.json()["id"]))
    assert second.json()["id"] != first.json()["id"]
