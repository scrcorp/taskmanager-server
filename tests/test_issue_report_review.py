"""Issue report — review category preset, visibility_scope, notification recipients.

Covers (2026-08-14 issue report review 계약):
- visibility_scope default / managers / store_all 조회 가부 (list 필터 + 단건 assert_can_view)
- legacy share_with_store_all=true 가 store_all 과 동일 판정 + 타 매장 인원 차단
- 키가 아예 없는 기존 payload 하위호환
- 알림 수신자 = (그 매장 GM 이상 전원) ∪ (extra_viewers) — 2026-08-14 2차 규칙
  · 자동 수신자는 해제 불가 (notify_excluded_user_ids 는 받기만 하고 무시)
  · 작성자 본인도 GM 이상이면 수신자
- issue 전용 조회권 확장: 그 매장 GM 이상은 동급·상급이 쓴 issue 도 열 수 있다
  (daily 등 다른 타입 가시성은 그대로)
- GET issue-recipients (console/app, store_id-only / report_id 모드, 없는 report → 404)
- GET issue-viewers (scope 별 예상 조회자, store_all 은 summary)
- 잘못된 visibility_scope / 타 org user_id → 400
- ensure_system_issue_template 멱등 (review 중복 append 안 됨)
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.main import app
from app.models.alert import Alert
from app.models.permission import Permission, RolePermission
from app.models.report import Report, ReportAcknowledgement, ReportComment, ReportTemplate
from app.models.user import User as UserModel
from app.models.user_store import UserStore
from app.services.report_service import (
    _resolve_issue_notify_recipients,
    _resolve_issue_viewers,
    ensure_system_issue_template,
    report_service,
)

REPORT_CODES = [
    "reports:read",
    "reports:create",
    "reports:update",
    "reports:delete",
    "reports:review",
    "reports:acknowledge",
]

RD = "2030-09-01"


async def _login(username: str) -> str:
    from app.utils.jwt import create_access_token

    async with async_session() as db:
        user = (
            await db.execute(select(UserModel).where(UserModel.username == username))
        ).scalar_one()
        return create_access_token(
            {"sub": str(user.id), "org": str(user.organization_id)}
        )


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def issue_perms(seed_roles: dict[str, UUID]) -> None:
    """reports:* 를 gm/sv/staff role 에 idempotent 부여."""
    async with async_session() as db:
        perms: dict[str, UUID] = {}
        for code in REPORT_CODES:
            p = (
                await db.execute(select(Permission).where(Permission.code == code))
            ).scalar_one_or_none()
            if p is None:
                resource, action = code.split(":")
                p = Permission(code=code, resource=resource, action=action)
                db.add(p)
                await db.flush()
            perms[code] = p.id
        for role_name in ("general_manager", "supervisor", "staff"):
            role_id = seed_roles[role_name]
            for code in REPORT_CODES:
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
async def issue_people(
    seed_organization: dict,
    seed_roles: dict[str, UUID],
    test_store_id: UUID,
    second_store_id: UUID,
):
    """매장 A 인적 구성 + 타 매장 인원 1명.

    - testgm  : GM, 매장 A manager      → 자동 수신자(GM+)
    - revpeergm : GM, 매장 A **배정만**(is_manager=False)
                  → 자동 수신자이자 issue 조회권자. is_manager 없이 배정만으로 충분한지 확인용
    - testsv  : SV, 매장 A manager      → 작성자
    - revpeersv : SV, 매장 A manager    → 동급이라 default 로는 못 봄 (managers 에서 열림)
    - teststaff : staff, 매장 A 배정만  → store_all 에서만 열림
    - revoutsider : staff, 매장 B 만    → 어떤 scope 로도 매장 A 리포트를 못 봄
    """
    from app.utils.password import hash_password

    org_id: UUID = seed_organization["id"]
    specs = [
        ("revpeersv", "Rev Peer Supervisor", "supervisor"),
        ("revpeergm", "Rev Peer General Manager", "general_manager"),
        ("revoutsider", "Rev Outsider", "staff"),
    ]
    created_users: list[UUID] = []
    created_links: list[tuple[UUID, UUID]] = []
    restored_links: list[tuple[UUID, UUID, bool]] = []

    async with async_session() as db:
        for username, full_name, role_name in specs:
            u = (
                await db.execute(
                    select(UserModel).where(
                        UserModel.username == username,
                        UserModel.organization_id == org_id,
                    )
                )
            ).scalar_one_or_none()
            if u is None:
                u = UserModel(
                    organization_id=org_id,
                    role_id=seed_roles[role_name],
                    username=username,
                    full_name=full_name,
                    password_hash=hash_password("1234"),
                    is_active=True,
                )
                db.add(u)
                await db.commit()
                await db.refresh(u)
                created_users.append(u.id)

        usernames = [
            "testgm", "testsv", "teststaff", "revpeersv", "revpeergm", "revoutsider",
        ]
        users = {
            u.username: u
            for u in (
                await db.execute(
                    select(UserModel).where(
                        UserModel.username.in_(usernames),
                        UserModel.organization_id == org_id,
                    )
                )
            ).scalars().all()
        }

        wanted = {
            "testgm": (test_store_id, True),
            "testsv": (test_store_id, True),
            "revpeersv": (test_store_id, True),
            "revpeergm": (test_store_id, False),
            "teststaff": (test_store_id, False),
            "revoutsider": (second_store_id, False),
        }
        for name, (store_id, is_manager) in wanted.items():
            u = users[name]
            link = (
                await db.execute(
                    select(UserStore).where(
                        UserStore.user_id == u.id, UserStore.store_id == store_id
                    )
                )
            ).scalar_one_or_none()
            if link is None:
                created_links.append((u.id, store_id))
                db.add(
                    UserStore(user_id=u.id, store_id=store_id, is_manager=is_manager)
                )
            elif link.is_manager != is_manager:
                restored_links.append((u.id, store_id, link.is_manager))
                link.is_manager = is_manager
        await db.commit()
        ids = {name: u.id for name, u in users.items()}

    yield ids

    async with async_session() as db:
        # 이 픽스처가 만든 리포트/유저 정리
        rids = (
            await db.execute(
                select(Report.id).where(Report.author_id.in_(list(ids.values())))
            )
        ).scalars().all()
        if rids:
            await db.execute(
                delete(ReportAcknowledgement).where(
                    ReportAcknowledgement.report_id.in_(rids)
                )
            )
            await db.execute(delete(ReportComment).where(ReportComment.report_id.in_(rids)))
            await db.execute(delete(Report).where(Report.id.in_(rids)))
        for uid, sid in created_links:
            await db.execute(
                delete(UserStore).where(
                    UserStore.user_id == uid, UserStore.store_id == sid
                )
            )
        for uid, sid, was in restored_links:
            link = (
                await db.execute(
                    select(UserStore).where(
                        UserStore.user_id == uid, UserStore.store_id == sid
                    )
                )
            ).scalar_one_or_none()
            if link is not None:
                link.is_manager = was
        if created_users:
            await db.execute(delete(UserStore).where(UserStore.user_id.in_(created_users)))
            await db.execute(delete(UserModel).where(UserModel.id.in_(created_users)))
        await db.commit()


@pytest_asyncio.fixture
async def system_issue_template():
    """system default issue 템플릿 보장 (review 카테고리 포함)."""
    async with async_session() as db:
        await ensure_system_issue_template(db)


async def _create_issue(
    client: AsyncClient, token: str, store_id: UUID, payload: dict, title: str = "Issue"
) -> tuple[int, dict]:
    r = await client.post(
        "/api/v1/app/my/reports",
        headers=_h(token),
        json={
            "type": "issue",
            "store_id": str(store_id),
            "report_date": RD,
            "title": title,
            "payload": {"category": "equipment", "severity": "medium", **payload},
        },
    )
    return r.status_code, (r.json() if r.content else {})


async def _can_open(client: AsyncClient, token: str, report_id: str) -> bool:
    r = await client.get(f"/api/v1/app/my/reports/{report_id}", headers=_h(token))
    assert r.status_code in (200, 403), r.text
    return r.status_code == 200


async def _console_visible(client: AsyncClient, token: str) -> set[str]:
    r = await client.get(
        "/api/v1/console/reports",
        headers=_h(token),
        params={"type": "issue", "date_from": RD, "date_to": RD, "per_page": 100},
    )
    assert r.status_code == 200, r.text
    return {i["id"] for i in r.json()["items"]}


# ===================================================================
# visibility_scope
# ===================================================================


@pytest.mark.asyncio
async def test_visibility_scope_matrix(client, issue_perms, issue_people, test_store_id):
    """default / managers / store_all 각각의 조회 가부 (목록 + 단건)."""
    sv = await _login("testsv")          # 작성자
    gm = await _login("testgm")          # 상위 직급 manager → 항상 보임
    peersv = await _login("revpeersv")   # 동급 manager
    staff = await _login("teststaff")    # 배정만 된 인원
    outsider = await _login("revoutsider")

    ids: dict[str, str] = {}
    for scope in ("default", "managers", "store_all"):
        code, body = await _create_issue(
            client, sv, test_store_id, {"visibility_scope": scope}, title=f"scope {scope}"
        )
        assert code == 201, body
        ids[scope] = body["id"]
        assert body["payload"]["visibility_scope"] == scope

    # 단건 (assert_can_view)
    assert await _can_open(client, gm, ids["default"]) is True
    assert await _can_open(client, peersv, ids["default"]) is False
    assert await _can_open(client, staff, ids["default"]) is False

    assert await _can_open(client, peersv, ids["managers"]) is True
    assert await _can_open(client, staff, ids["managers"]) is False

    assert await _can_open(client, peersv, ids["store_all"]) is True  # 배정 인원이기도 함
    assert await _can_open(client, staff, ids["store_all"]) is True

    # 타 매장 인원은 어떤 scope 로도 못 본다
    for scope in ("default", "managers", "store_all"):
        assert await _can_open(client, outsider, ids[scope]) is False

    # 목록 필터도 같은 규칙
    peer_seen = await _console_visible(client, peersv)
    assert ids["default"] not in peer_seen
    assert ids["managers"] in peer_seen
    assert ids["store_all"] in peer_seen

    staff_seen = await _console_visible(client, staff)
    assert ids["default"] not in staff_seen
    assert ids["managers"] not in staff_seen
    assert ids["store_all"] in staff_seen


@pytest.mark.asyncio
async def test_missing_scope_key_is_backward_compatible(
    client, issue_perms, issue_people, test_store_id
):
    """visibility_scope 키가 아예 없는 기존 payload = default 와 동일 동작."""
    sv = await _login("testsv")
    staff = await _login("teststaff")
    gm = await _login("testgm")

    code, body = await _create_issue(client, sv, test_store_id, {}, title="legacy none")
    assert code == 201, body
    rid = body["id"]

    # 저장된 payload 에서 키를 제거해 '옛 데이터' 를 만든다
    async with async_session() as db:
        r = await db.get(Report, UUID(rid))
        payload = dict(r.payload or {})
        payload.pop("visibility_scope", None)
        r.payload = payload
        await db.commit()

    assert await _can_open(client, gm, rid) is True
    assert await _can_open(client, staff, rid) is False
    assert rid not in await _console_visible(client, staff)


@pytest.mark.asyncio
async def test_legacy_share_with_store_all(client, issue_perms, issue_people, test_store_id):
    """legacy share_with_store_all=true = store_all. 단, 타 매장 인원에게는 새지 않는다."""
    sv = await _login("testsv")
    staff = await _login("teststaff")
    outsider = await _login("revoutsider")

    code, body = await _create_issue(client, sv, test_store_id, {}, title="legacy share")
    assert code == 201, body
    rid = body["id"]
    async with async_session() as db:
        r = await db.get(Report, UUID(rid))
        payload = dict(r.payload or {})
        payload.pop("visibility_scope", None)
        payload["share_with_store_all"] = True
        r.payload = payload
        await db.commit()

    assert await _can_open(client, staff, rid) is True
    assert rid in await _console_visible(client, staff)
    assert await _can_open(client, outsider, rid) is False


@pytest.mark.asyncio
async def test_legacy_payload_survives_update_from_old_client(
    client, issue_perms, issue_people, test_store_id
):
    """visibility_scope 를 모르는 구버전 클라가 legacy payload 를 PUT 해도 범위가 안 좁아진다.

    구 콘솔 탭은 `share_with_store_all` 만 알고 `visibility_scope` 는 보내지 않는다.
    이때 서버가 "default" 를 박아버리면 매장 전원 공개가 조용히 사라진다.
    """
    sv = await _login("testsv")
    staff = await _login("teststaff")

    code, body = await _create_issue(client, sv, test_store_id, {}, title="legacy put")
    assert code == 201, body
    rid = body["id"]
    async with async_session() as db:
        r = await db.get(Report, UUID(rid))
        payload = dict(r.payload or {})
        payload.pop("visibility_scope", None)
        payload["share_with_store_all"] = True
        r.payload = payload
        await db.commit()
    assert await _can_open(client, staff, rid) is True

    # 구버전 클라의 PUT: visibility_scope 없음 + legacy 키만
    r = await client.put(
        f"/api/v1/app/my/reports/{rid}",
        headers=_h(sv),
        json={
            "payload": {
                "category": "equipment",
                "severity": "medium",
                "share_with_store_all": True,
            }
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["payload"]["visibility_scope"] == "store_all"

    assert await _can_open(client, staff, rid) is True
    assert rid in await _console_visible(client, staff)


@pytest.mark.asyncio
async def test_scope_wins_over_legacy_key_in_list_and_detail(
    client, issue_perms, issue_people, test_store_id
):
    """두 키가 공존하면 visibility_scope 가 이긴다 — 목록과 단건이 절대 갈리면 안 된다.

    OR 로 두면 scope="default" + legacy=true 인 row 가 목록엔 뜨는데 열면 403 이 되어
    필터가 아니게 되고, 좁힌 의도와 반대로 매장 전원에게 제목이 노출된다.
    """
    sv = await _login("testsv")
    staff = await _login("teststaff")

    code, body = await _create_issue(client, sv, test_store_id, {}, title="scope wins")
    assert code == 201, body
    rid = body["id"]
    async with async_session() as db:
        r = await db.get(Report, UUID(rid))
        payload = dict(r.payload or {})
        payload["visibility_scope"] = "default"
        payload["share_with_store_all"] = True  # 모순된 잔재
        r.payload = payload
        await db.commit()

    assert await _can_open(client, staff, rid) is False
    assert rid not in await _console_visible(client, staff)

    # 반대 방향도: scope=store_all 이면 legacy 키가 false 여도 열린다.
    async with async_session() as db:
        r = await db.get(Report, UUID(rid))
        payload = dict(r.payload or {})
        payload["visibility_scope"] = "store_all"
        payload["share_with_store_all"] = False
        r.payload = payload
        await db.commit()

    assert await _can_open(client, staff, rid) is True
    assert rid in await _console_visible(client, staff)


@pytest.mark.asyncio
async def test_write_drops_legacy_key(client, issue_perms, issue_people, test_store_id):
    """쓰기 경로를 지나면 legacy 키는 사라진다 (모순 상태가 새로 생기지 않게)."""
    sv = await _login("testsv")
    code, body = await _create_issue(
        client,
        sv,
        test_store_id,
        {"visibility_scope": "managers", "share_with_store_all": True},
        title="drop legacy",
    )
    assert code == 201, body
    assert body["payload"]["visibility_scope"] == "managers"
    assert "share_with_store_all" not in body["payload"]


@pytest.mark.asyncio
async def test_invalid_scope_and_foreign_user_rejected(
    client, issue_perms, issue_people, test_store_id
):
    """모르는 scope 값과 조직 밖 user_id 는 400 (조용히 무시하면 조용한 실패)."""
    sv = await _login("testsv")

    code, body = await _create_issue(
        client, sv, test_store_id, {"visibility_scope": "author_only"}
    )
    assert code == 400, body

    code, body = await _create_issue(
        client, sv, test_store_id, {"extra_viewers": {"user_ids": [str(uuid4())]}}
    )
    assert code == 400, body

    # notify_excluded_user_ids 는 2026-08-14 2차 규칙에서 **효과가 사라졌다**.
    # 구버전 클라가 계속 보내므로 400 을 내지 않고 그대로 받아서 무시한다
    # (효과 없는 키를 검증하면 "이 키가 뭔가 한다"는 잘못된 신호가 된다).
    code, body = await _create_issue(
        client, sv, test_store_id, {"notify_excluded_user_ids": [str(uuid4())]}
    )
    assert code == 201, body


# ===================================================================
# 수신자 계산 (제외 / 추가)
# ===================================================================


@pytest.mark.asyncio
async def test_notify_recipients_auto_locked_and_add(
    client, issue_perms, issue_people, test_store_id
):
    """자동 수신자(그 매장 GM 이상)는 해제 불가 + extra_viewers 는 조회권 + 수신자.

    2026-08-14 2차 규칙으로 바뀐 부분:
    - 예전엔 "그 매장 manager 중 작성자보다 상위 직급" 이 자동 후보였고
      notify_excluded_user_ids 로 뺄 수 있었다. 지금은 **그 매장 GM 이상 전원**이고
      **뺄 수 없다** — 매장 이슈는 그 매장 책임자가 무조건 알아야 한다는 결정.
    - is_manager 도 보지 않는다. 배정(user_stores)만 있으면 GM 은 받는다(revpeergm).
    """
    sv = await _login("testsv")
    gm_id = issue_people["testgm"]
    peergm_id = issue_people["revpeergm"]
    staff_id = issue_people["teststaff"]
    staff = await _login("teststaff")

    # 1) 기본: 매장 GM 이상 전원이 수신자 + 조회권자
    code, body = await _create_issue(client, sv, test_store_id, {}, title="notify base")
    assert code == 201, body
    async with async_session() as db:
        r = await db.get(Report, UUID(body["id"]))
        recipients = await _resolve_issue_notify_recipients(db, r)
        viewers = await _resolve_issue_viewers(db, r)
        assert gm_id in recipients
        assert peergm_id in recipients, "manager 플래그 없이 배정만 된 GM 도 받는다"
        assert {gm_id, peergm_id} <= viewers

    # 2) 구버전 클라가 GM 제외를 보내도 무시된다 + staff 추가
    code, body = await _create_issue(
        client,
        sv,
        test_store_id,
        {
            "notify_excluded_user_ids": [str(gm_id)],
            "extra_viewers": {"user_ids": [str(staff_id)], "position_ids": []},
        },
        title="notify tuned",
    )
    assert code == 201, body
    rid = body["id"]
    async with async_session() as db:
        r = await db.get(Report, UUID(rid))
        recipients = await _resolve_issue_notify_recipients(db, r)
        viewers = await _resolve_issue_viewers(db, r)
        assert gm_id in recipients, "자동 수신자는 해제할 수 없다 (제외 키는 무시)"
        assert staff_id in recipients, "지목한 사람은 이메일도 받는다"
        assert gm_id in viewers
        assert staff_id in viewers

    # extra_viewers 지목 인원은 실제로 리포트를 열 수 있다
    assert await _can_open(client, staff, rid) is True


@pytest.mark.asyncio
async def test_author_who_is_gm_is_also_notified(
    client, issue_perms, issue_people, test_store_id
):
    """작성자 본인이 GM 이상이면 본인도 수신자에 들어간다 (작성자 제외 규칙 폐기)."""
    gm = await _login("testgm")
    gm_id = issue_people["testgm"]

    code, body = await _create_issue(client, gm, test_store_id, {}, title="gm writes")
    assert code == 201, body
    async with async_session() as db:
        r = await db.get(Report, UUID(body["id"]))
        assert gm_id in await _resolve_issue_notify_recipients(db, r)


@pytest.mark.asyncio
async def test_peer_gm_can_open_issue_written_by_gm(
    client, issue_perms, issue_people, test_store_id
):
    """동급 GM 이 쓴 issue 를 다른 GM 이 열 수 있다 (알림이 가므로 조회권도 열어야 한다).

    직급 축(동급·상급 차단)만으로는 "알림은 왔는데 누르면 403" 이 된다.
    """
    gm = await _login("testgm")
    peergm = await _login("revpeergm")

    code, body = await _create_issue(client, gm, test_store_id, {}, title="gm issue")
    assert code == 201, body
    rid = body["id"]

    assert await _can_open(client, peergm, rid) is True

    # 반대 방향도 (배정만 된 GM 이 쓴 것을 manager GM 이 연다) — 목록 필터도 같은 규칙.
    # (peergm 쪽 콘솔 목록은 확인하지 않는다. 콘솔 목록은 GM 에게 is_manager 매장만
    #  보여주는 별개의 매장 스코프라 리포트 가시성과 무관하게 비어 있다)
    code, body2 = await _create_issue(
        client, peergm, test_store_id, {}, title="peer gm issue"
    )
    assert code == 201, body2
    assert await _can_open(client, gm, body2["id"]) is True
    assert body2["id"] in await _console_visible(client, gm)


@pytest.mark.asyncio
async def test_daily_visibility_unchanged_for_peer_gm(
    client, issue_perms, issue_people, test_store_id
):
    """issue 전용 확장이다 — daily 는 여전히 동급 GM 에게 안 보인다."""
    gm = await _login("testgm")
    peergm = await _login("revpeergm")

    r = await client.post(
        "/api/v1/app/my/reports",
        headers=_h(gm),
        json={
            "type": "daily",
            "store_id": str(test_store_id),
            "report_date": RD,
            "payload": {"period": "lunch"},
        },
    )
    assert r.status_code == 201, r.text
    rid = r.json()["id"]

    assert await _can_open(client, peergm, rid) is False, (
        "daily 가시성까지 넓히면 안 된다"
    )
    listed = await client.get(
        "/api/v1/console/reports",
        headers=_h(peergm),
        params={"type": "daily", "date_from": RD, "date_to": RD, "per_page": 100},
    )
    assert listed.status_code == 200, listed.text
    assert rid not in {i["id"] for i in listed.json()["items"]}


# ===================================================================
# GET /issue-recipients
# ===================================================================


@pytest.mark.asyncio
async def test_issue_recipients_endpoint(client, issue_perms, issue_people, test_store_id):
    sv = await _login("testsv")
    gm_id = str(issue_people["testgm"])
    staff_id = str(issue_people["teststaff"])

    # store_id-only: 자동 수신자(매장 GM 이상)만, 전원 수신 + 해제 불가
    for base in ("/api/v1/console/reports", "/api/v1/app/my/reports"):
        r = await client.get(
            f"{base}/issue-recipients",
            headers=_h(sv),
            params={"store_id": str(test_store_id)},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["report_id"] is None
        by_id = {i["user_id"]: i for i in body["items"]}
        assert gm_id in by_id
        assert by_id[gm_id]["source"] == "auto"
        assert by_id[gm_id]["is_recipient"] is True
        assert by_id[gm_id]["can_remove"] is False, "자동 수신자는 UI 에서 뺄 수 없다"
        assert by_id[gm_id]["role_label"] == "general_manager"
        # 배정만 된 GM 도 자동 수신자 (is_manager 무관)
        assert str(issue_people["revpeergm"]) in by_id
        # GM 미만은 자동 수신자가 아니다
        assert staff_id not in by_id
        assert str(issue_people["revpeersv"]) not in by_id
        # 정렬: role_priority 오름차순
        priorities = [i["role_priority"] for i in body["items"]]
        assert priorities == sorted(priorities)

    # report_id 모드: 제외/추가 반영
    code, created = await _create_issue(
        client,
        sv,
        test_store_id,
        {
            "notify_excluded_user_ids": [gm_id],
            "extra_viewers": {"user_ids": [staff_id], "position_ids": []},
        },
        title="recipients report",
    )
    assert code == 201, created
    rid = created["id"]

    for base in ("/api/v1/console/reports", "/api/v1/app/my/reports"):
        r = await client.get(
            f"{base}/issue-recipients", headers=_h(sv), params={"report_id": rid}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["report_id"] == rid
        assert body["store_id"] == str(test_store_id)
        by_id = {i["user_id"]: i for i in body["items"]}
        assert by_id[gm_id]["source"] == "auto"
        # 구버전 제외 목록이 실려 있어도 자동 수신자는 계속 받는다
        assert by_id[gm_id]["is_recipient"] is True
        assert by_id[gm_id]["can_remove"] is False
        assert by_id[staff_id]["source"] == "added"
        assert by_id[staff_id]["is_recipient"] is True
        assert by_id[staff_id]["can_remove"] is True

    # store_id 불일치 → 400
    from app.models.organization import Store

    async with async_session() as db:
        other_store = (
            await db.execute(
                select(Store.id).where(
                    Store.organization_id == UUID(str(created["organization_id"])),
                    Store.id != test_store_id,
                )
            )
        ).scalars().first()
    if other_store:
        r = await client.get(
            "/api/v1/console/reports/issue-recipients",
            headers=_h(sv),
            params={"report_id": rid, "store_id": str(other_store)},
        )
        assert r.status_code == 400, r.text

    # 존재하지 않는(=타 org 포함) report_id → 404
    r = await client.get(
        "/api/v1/console/reports/issue-recipients",
        headers=_h(sv),
        params={"report_id": str(uuid4())},
    )
    assert r.status_code == 404, r.text

    # store_id / report_id 둘 다 없음 → 400
    r = await client.get("/api/v1/app/my/reports/issue-recipients", headers=_h(sv))
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_issue_recipients_blocked_for_non_visible_report(
    client, issue_perms, issue_people, test_store_id
):
    """조회권이 없는 리포트의 수신자 목록은 볼 수 없다."""
    sv = await _login("testsv")
    peersv = await _login("revpeersv")
    code, body = await _create_issue(client, sv, test_store_id, {}, title="private")
    assert code == 201, body
    r = await client.get(
        "/api/v1/console/reports/issue-recipients",
        headers=_h(peersv),
        params={"report_id": body["id"]},
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "REPORT_NOT_VISIBLE"


# ===================================================================
# GET /issue-viewers (조회 범위 예상 목록)
# ===================================================================


@pytest.mark.asyncio
async def test_issue_expected_viewers_by_scope(
    client, issue_perms, issue_people, test_store_id
):
    """scope 별로 '실제로 누가 보게 되는지' 목록이 맞아야 한다 (console/app 동일 body)."""
    sv = await _login("testsv")
    sv_id = str(issue_people["testsv"])
    gm_id = str(issue_people["testgm"])
    peergm_id = str(issue_people["revpeergm"])
    peersv_id = str(issue_people["revpeersv"])
    staff_id = str(issue_people["teststaff"])

    for base in ("/api/v1/console/reports", "/api/v1/app/my/reports"):
        # default — 작성자 + 매장 GM 이상. 동급 SV / staff 는 아직 안 보인다.
        r = await client.get(
            f"{base}/issue-viewers",
            headers=_h(sv),
            params={"store_id": str(test_store_id), "scope": "default"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["scope"] == "default"
        assert body["mode"] == "list"
        by_id = {i["user_id"]: i for i in body["items"]}
        assert by_id[sv_id]["reason"] == "author"
        assert by_id[gm_id]["reason"] == "gm_or_above"
        assert by_id[peergm_id]["reason"] == "gm_or_above"
        assert by_id[gm_id]["is_notified"] is True
        assert peersv_id not in by_id
        assert staff_id not in by_id
        assert body["summary"]["count"] == len(body["items"])
        # 정렬: role_priority 오름차순
        priorities = [i["role_priority"] for i in body["items"]]
        assert priorities == sorted(priorities)

        # managers — 매장 manager 전원이 추가된다 (동급 SV 포함). 알림은 안 간다.
        r = await client.get(
            f"{base}/issue-viewers",
            headers=_h(sv),
            params={"store_id": str(test_store_id), "scope": "managers"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        by_id = {i["user_id"]: i for i in body["items"]}
        assert by_id[peersv_id]["reason"] == "store_manager"
        assert by_id[peersv_id]["is_notified"] is False, (
            "볼 수 있게 됐을 뿐인 사람에게 알림이 간다고 표시하면 안 된다"
        )
        assert staff_id not in by_id

        # store_all — 목록 대신 요약만
        r = await client.get(
            f"{base}/issue-viewers",
            headers=_h(sv),
            params={"store_id": str(test_store_id), "scope": "store_all"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mode"] == "summary"
        assert body["items"] == []
        assert body["summary"]["count"] >= 5
        assert body["summary"]["label"] == "Everyone assigned to this store"

    # 아직 저장 전인 추가 지목(extra_user_ids)도 반영된다
    r = await client.get(
        "/api/v1/console/reports/issue-viewers",
        headers=_h(sv),
        params={
            "store_id": str(test_store_id),
            "scope": "default",
            "extra_user_ids": [staff_id],
        },
    )
    assert r.status_code == 200, r.text
    by_id = {i["user_id"]: i for i in r.json()["items"]}
    assert by_id[staff_id]["reason"] == "added"
    assert by_id[staff_id]["is_notified"] is True, "지목한 사람은 알림도 받는다"

    # 모르는 scope → 400
    r = await client.get(
        "/api/v1/console/reports/issue-viewers",
        headers=_h(sv),
        params={"store_id": str(test_store_id), "scope": "author_only"},
    )
    assert r.status_code == 400, r.text

    # store_id / report_id 둘 다 없음 → 400
    r = await client.get(
        "/api/v1/app/my/reports/issue-viewers", headers=_h(sv), params={"scope": "default"}
    )
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_issue_expected_viewers_report_mode(
    client, issue_perms, issue_people, test_store_id
):
    """report_id 모드는 그 리포트의 작성자/추가 지목을 기준으로 계산한다."""
    sv = await _login("testsv")
    staff_id = str(issue_people["teststaff"])

    code, body = await _create_issue(
        client,
        sv,
        test_store_id,
        {
            "visibility_scope": "default",
            "extra_viewers": {"user_ids": [staff_id], "position_ids": []},
        },
        title="viewers report",
    )
    assert code == 201, body
    rid = body["id"]

    r = await client.get(
        "/api/v1/console/reports/issue-viewers",
        headers=_h(sv),
        params={"report_id": rid, "scope": "default"},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["report_id"] == rid
    assert payload["store_id"] == str(test_store_id)
    by_id = {i["user_id"]: i for i in payload["items"]}
    assert by_id[str(issue_people["testsv"])]["reason"] == "author"
    assert by_id[staff_id]["reason"] == "added"

    # 조회권 없는 리포트의 예상 목록은 볼 수 없다
    peersv = await _login("revpeersv")
    code, private = await _create_issue(client, sv, test_store_id, {}, title="private2")
    assert code == 201, private
    r = await client.get(
        "/api/v1/console/reports/issue-viewers",
        headers=_h(peersv),
        params={"report_id": private["id"], "scope": "default"},
    )
    assert r.status_code == 403, r.text


# ===================================================================
# system default template backfill
# ===================================================================


@pytest.mark.asyncio
async def test_ensure_system_issue_template_idempotent(system_issue_template):
    """두 번 돌려도 review 가 중복 append 되지 않고 프리셋이 붙는다."""
    from app.schemas.report import DEFAULT_ISSUE_CATEGORY_DESCRIPTION_TEMPLATES

    async with async_session() as db:
        changed = await ensure_system_issue_template(db)
        assert changed is False, "이미 보정된 상태에서는 다시 쓰지 않는다"
        tpl = (
            await db.execute(
                select(ReportTemplate).where(
                    ReportTemplate.type == "issue",
                    ReportTemplate.organization_id.is_(None),
                    ReportTemplate.store_id.is_(None),
                    ReportTemplate.is_default.is_(True),
                )
            )
        ).scalars().first()
        assert tpl is not None
        cats = tpl.payload["categories"]
        codes = [c["code"] for c in cats]
        assert codes.count("review") == 1
        review = next(c for c in cats if c["code"] == "review")
        assert (
            review["description_template"]
            == DEFAULT_ISSUE_CATEGORY_DESCRIPTION_TEMPLATES["review"]
        )


@pytest.mark.asyncio
async def test_ensure_system_issue_template_keeps_operator_edits(system_issue_template):
    """운영자가 비운(null) 프리셋은 startup 보정이 되살리지 않는다."""
    async with async_session() as db:
        tpl = (
            await db.execute(
                select(ReportTemplate).where(
                    ReportTemplate.type == "issue",
                    ReportTemplate.organization_id.is_(None),
                    ReportTemplate.store_id.is_(None),
                    ReportTemplate.is_default.is_(True),
                )
            )
        ).scalars().first()
        payload = dict(tpl.payload)
        payload["categories"] = [
            {**c, "description_template": None} if c["code"] == "review" else c
            for c in payload["categories"]
        ]
        tpl.payload = payload
        await db.commit()

        assert await ensure_system_issue_template(db) is False
        await db.refresh(tpl)
        review = next(c for c in tpl.payload["categories"] if c["code"] == "review")
        assert review["description_template"] is None

        # 원복 (다른 테스트가 review 프리셋을 기대한다)
        payload = dict(tpl.payload)
        payload["categories"] = [
            {k: v for k, v in c.items() if k != "description_template"}
            if c["code"] == "review" else c
            for c in payload["categories"]
        ]
        tpl.payload = payload
        await db.commit()
        assert await ensure_system_issue_template(db) is True


@pytest.mark.asyncio
async def test_ensure_system_issue_template_upgrades_legacy_preset(system_issue_template):
    """이미 시드된 옛 프리셋 원문은 startup 보정에서 현재 원문(빈 줄 포함)으로 갱신된다."""
    from app.schemas.report import (
        DEFAULT_ISSUE_CATEGORY_DESCRIPTION_TEMPLATES,
        LEGACY_ISSUE_CATEGORY_DESCRIPTION_TEMPLATES,
    )

    # 현재 원문은 항목 사이가 빈 줄로 떨어져 있어야 한다 (읽기 어렵다는 피드백)
    assert DEFAULT_ISSUE_CATEGORY_DESCRIPTION_TEMPLATES["review"] == (
        "Platform:\n\nRating:\n\nWhat was said:\n\n"
        "How we responded:\n\nFollow-up needed:\n\nPlan:"
    )

    async with async_session() as db:
        tpl = (
            await db.execute(
                select(ReportTemplate).where(
                    ReportTemplate.type == "issue",
                    ReportTemplate.organization_id.is_(None),
                    ReportTemplate.store_id.is_(None),
                    ReportTemplate.is_default.is_(True),
                )
            )
        ).scalars().first()

        legacy = LEGACY_ISSUE_CATEGORY_DESCRIPTION_TEMPLATES["review"][0]
        payload = dict(tpl.payload)
        payload["categories"] = [
            {**c, "description_template": legacy} if c["code"] == "review" else c
            for c in payload["categories"]
        ]
        tpl.payload = payload
        await db.commit()

        assert await ensure_system_issue_template(db) is True
        await db.refresh(tpl)
        review = next(c for c in tpl.payload["categories"] if c["code"] == "review")
        assert (
            review["description_template"]
            == DEFAULT_ISSUE_CATEGORY_DESCRIPTION_TEMPLATES["review"]
        )

        # 운영자가 직접 고친 문구는 그대로 둔다 (옛 원문일 때만 갈아끼운다)
        payload = dict(tpl.payload)
        payload["categories"] = [
            {**c, "description_template": "Custom by operator"}
            if c["code"] == "review" else c
            for c in payload["categories"]
        ]
        tpl.payload = payload
        await db.commit()
        assert await ensure_system_issue_template(db) is False
        await db.refresh(tpl)
        review = next(c for c in tpl.payload["categories"] if c["code"] == "review")
        assert review["description_template"] == "Custom by operator"

        # 원복 (다른 테스트가 현재 프리셋을 기대한다)
        payload = dict(tpl.payload)
        payload["categories"] = [
            {**c, "description_template": DEFAULT_ISSUE_CATEGORY_DESCRIPTION_TEMPLATES["review"]}
            if c["code"] == "review" else c
            for c in payload["categories"]
        ]
        tpl.payload = payload
        await db.commit()


@pytest.mark.asyncio
async def test_review_category_accepted(
    client, issue_perms, issue_people, system_issue_template, test_store_id
):
    """review 카테고리로 이슈를 만들 수 있고 description 원문이 그대로 저장된다."""
    sv = await _login("testsv")
    desc = "Platform: Google\nRating: 2/5\nPlan: call back\nhttps://g.page/r/abc/review"
    r = await client.post(
        "/api/v1/app/my/reports",
        headers=_h(sv),
        json={
            "type": "issue",
            "store_id": str(test_store_id),
            "report_date": RD,
            "title": "Google review - 2 stars",
            "payload": {
                "category": "review",
                "severity": "medium",
                "description": desc,
            },
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["payload"]["description"] == desc

    # 템플릿 응답에 프리셋이 실려 온다
    t = await client.get(
        "/api/v1/app/my/reports/template",
        headers=_h(sv),
        params={"type": "issue", "store_id": str(test_store_id)},
    )
    assert t.status_code == 200, t.text
    cats = {c["code"]: c for c in t.json()["payload"]["categories"]}
    assert cats["review"]["description_template"].startswith("Platform:")


@pytest.mark.asyncio
async def test_widening_scope_does_not_widen_notifications(
    client, issue_perms, issue_people, test_store_id
):
    """조회 범위를 넓혀도 알림 대상은 안 늘어난다.

    범위 확대는 '필요하면 열어볼 수 있게' 한다는 뜻이지 '전원에게 통지한다'가 아니다.
    예전엔 in-app alert 을 조회권자 전원에게 보내서, store_all 로 넓히는 순간
    매장 전원에게 알림이 쏟아졌다. 이 테스트가 그걸 막는다.
    """
    sv = await _login("testsv")
    staff_id = issue_people["teststaff"]

    code, base = await _create_issue(
        client, sv, test_store_id, {}, title="scope default"
    )
    assert code == 201, base

    code, wide = await _create_issue(
        client,
        sv,
        test_store_id,
        {"visibility_scope": "store_all"},
        title="scope store_all",
    )
    assert code == 201, wide

    async with async_session() as db:
        r_base = await db.get(Report, UUID(base["id"]))
        r_wide = await db.get(Report, UUID(wide["id"]))

        base_recipients = await _resolve_issue_notify_recipients(db, r_base)
        wide_recipients = await _resolve_issue_notify_recipients(db, r_wide)
        wide_viewers = await _resolve_issue_viewers(db, r_wide)

        # 조회권은 실제로 넓어졌다 (매장 staff 가 열 수 있다)
        assert staff_id in wide_viewers
        # 그런데 알림 대상은 그대로다
        assert wide_recipients == base_recipients, (
            "범위를 넓혔다고 알림 대상이 늘어나면 안 된다"
        )
        assert staff_id not in wide_recipients, (
            "볼 수 있게 됐을 뿐인 사람에게 알림이 가면 안 된다"
        )

        # 계산만 맞고 발송이 틀리면 소용없다 — 실제로 만들어진 alert 을 센다.
        # (예전엔 alert 을 조회권자 전원에게 보내서 매장 staff 에게도 갔다)
        alerted = await db.execute(
            select(Alert.user_id).where(
                Alert.reference_type == "issue_report",
                Alert.reference_id == r_wide.id,
            )
        )
        alerted_ids = {row[0] for row in alerted}
        assert staff_id not in alerted_ids, (
            "범위 확대로 볼 수 있게 된 사람에게 in-app alert 이 가면 안 된다"
        )
        assert alerted_ids <= wide_recipients, (
            "alert 은 수신자 집합을 넘지 않는다"
        )


@pytest.mark.asyncio
async def test_author_below_gm_gets_followup_notifications(
    client, issue_perms, issue_people, test_store_id
):
    """GM 미만 작성자도 자기 이슈의 후속(댓글/상태 변경) 알림을 받는다.

    자동 수신자를 "그 매장 GM 이상" 으로 바꾸면서 작성자가 집합에서 빠졌는데,
    그러면 이슈를 올린 staff 가 "답이 달렸다 / 닫혔다" 를 영영 못 듣는다
    (직전 동작에서는 조회권자 알림으로 받고 있었으므로 명백한 회귀다).
    생성(created) 알림은 작성자 = 액터라 제외한다.
    """
    staff = await _login("teststaff")
    staff_id = issue_people["teststaff"]
    gm = await _login("testgm")

    code, body = await _create_issue(
        client, staff, test_store_id, {}, title="staff writes"
    )
    assert code == 201, body
    rid = body["id"]

    async def _alerted() -> set[UUID]:
        async with async_session() as db:
            rows = await db.execute(
                select(Alert.user_id).where(
                    Alert.reference_type == "issue_report",
                    Alert.reference_id == UUID(rid),
                )
            )
            return {row[0] for row in rows}

    assert staff_id not in await _alerted(), (
        "자기가 방금 쓴 리포트의 created 알림은 받지 않는다"
    )

    r = await client.post(
        f"/api/v1/console/reports/{rid}/comments",
        headers=_h(gm),
        json={"content": "looking into it"},
    )
    assert r.status_code in (200, 201), r.text
    assert staff_id in await _alerted(), "작성자는 자기 이슈의 댓글 알림을 받아야 한다"

    async with async_session() as db:
        r_obj = await db.get(Report, UUID(rid))
        recipients = await _resolve_issue_notify_recipients(db, r_obj, event="comment")
        assert staff_id in recipients
        created_recipients = await _resolve_issue_notify_recipients(
            db, r_obj, event="created"
        )
        assert staff_id not in created_recipients


@pytest.mark.asyncio
async def test_inactive_extra_viewer_not_notified_and_update_not_bricked(
    client, issue_perms, issue_people, test_store_id
):
    """지목했던 사람이 비활성화돼도 (a) 알림이 안 가고 (b) 리포트 수정이 막히지 않는다.

    (a) extra_viewers 는 payload 에 박힌 과거 id 라 필터가 없으면 퇴사자에게 이슈 메일이
        계속 나간다. 자동 수신자 쪽은 is_active 를 걸고 있으므로 지목 쪽만 새는 상태였다.
    (b) 저장된 id 를 매 수정마다 재검증하면 그 리포트는 영영 400 이 되어 벽돌이 된다.
    """
    sv = await _login("testsv")
    staff_id = issue_people["teststaff"]

    code, body = await _create_issue(
        client,
        sv,
        test_store_id,
        {"extra_viewers": {"user_ids": [str(staff_id)], "position_ids": []}},
        title="viewer leaves",
    )
    assert code == 201, body
    rid = body["id"]
    saved_payload = body["payload"]

    async with async_session() as db:
        u = await db.get(UserModel, staff_id)
        u.is_active = False
        await db.commit()
    try:
        async with async_session() as db:
            r_obj = await db.get(Report, UUID(rid))
            recipients = await _resolve_issue_notify_recipients(
                db, r_obj, event="comment"
            )
            assert staff_id not in recipients, "비활성 사용자에게 알림을 보내면 안 된다"

        # 저장돼 있던 id 는 재검증하지 않으므로 수정은 계속 된다
        r = await client.put(
            f"/api/v1/app/my/reports/{rid}",
            headers=_h(sv),
            json={"payload": {**saved_payload, "description": "still editable"}},
        )
        assert r.status_code == 200, r.text
        assert r.json()["payload"]["extra_viewers"]["user_ids"] == [str(staff_id)]

        # 그러나 **새로** 지목하는 비활성 사용자는 그대로 400 (조용한 실패 금지)
        r = await client.put(
            f"/api/v1/app/my/reports/{rid}",
            headers=_h(sv),
            json={
                "payload": {
                    **saved_payload,
                    "extra_viewers": {"user_ids": [], "position_ids": []},
                }
            },
        )
        assert r.status_code == 200, r.text
        r = await client.put(
            f"/api/v1/app/my/reports/{rid}",
            headers=_h(sv),
            json={
                "payload": {
                    **saved_payload,
                    "extra_viewers": {
                        "user_ids": [str(staff_id)],
                        "position_ids": [],
                    },
                }
            },
        )
        assert r.status_code == 400, r.text
        assert r.json()["error"]["code"] == "ISSUE_RECIPIENT_NOT_IN_ORG"
    finally:
        async with async_session() as db:
            u = await db.get(UserModel, staff_id)
            u.is_active = True
            await db.commit()
