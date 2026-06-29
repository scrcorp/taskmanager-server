"""Unified daily-report redesign — unit + API integration tests (merge gate).

Covers (build_daily_report_redesign S2/S3):
- B per-person daily duplicate (결정-8): two authors share a slot; same author 409.
- C report_types CRUD + effective resolution (org default + store override/add).
- D template applicable_types selection (type_code match → all-types fallback).
- E deadline_at computation from report_type rule (store-tz) + is_late/is_overdue flags.
- F review (submitted→reviewed, feedback comment) + acknowledge (idempotent upsert).

전제: startup lifespan 이 테스트에서 안 돌므로 reports/report_types 권한을
fixture 에서 idempotent 하게 보장한다 (evaluations 테스트 패턴).
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.main import app
from app.models.permission import Permission, RolePermission
from app.models.report import (
    Report,
    ReportAcknowledgement,
    ReportComment,
    ReportTemplate,
    ReportType,
)
from app.services.report_service import report_service

REPORT_CODES = [
    "reports:read",
    "reports:create",
    "reports:update",
    "reports:delete",
    "reports:review",
    "reports:acknowledge",
    "report_types:manage",
]


async def _login(username: str) -> str:
    """username → access token (직접 mint, multi-org login 의존 끊기)."""
    from app.utils.jwt import create_access_token

    async with async_session() as db:
        from app.models.user import User

        user = (
            await db.execute(select(User).where(User.username == username))
        ).scalar_one()
        return create_access_token(
            {"sub": str(user.id), "org": str(user.organization_id)}
        )


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def report_perms(seed_roles: dict[str, UUID]) -> None:
    """reports:* + report_types:manage 권한을 gm/sv/staff role 에 idempotent 부여.

    super_owner/owner 는 require_permission bypass. GM/SV/Staff 는 명시 부여 필요.
    """
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

        # gm/sv/staff 전부에게 부여 (테스트 단순화; 실제 default 세트와 무관하게 통과 보장).
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
async def clean_reports(seed_organization: dict, test_store_id: UUID):
    """테스트 전후 이 store 의 reports/types/templates(org-scoped daily) 정리."""
    org_id: UUID = seed_organization["id"]

    async def _purge() -> None:
        async with async_session() as db:
            # store 의 reports + 그 ack/comment (cascade)
            report_ids = (
                await db.execute(
                    select(Report.id).where(
                        Report.organization_id == org_id,
                        Report.store_id == test_store_id,
                    )
                )
            ).scalars().all()
            if report_ids:
                await db.execute(
                    delete(ReportAcknowledgement).where(
                        ReportAcknowledgement.report_id.in_(report_ids)
                    )
                )
                await db.execute(
                    delete(ReportComment).where(ReportComment.report_id.in_(report_ids))
                )
                await db.execute(delete(Report).where(Report.id.in_(report_ids)))
            # store-scoped report_types (override/add) hard delete
            await db.execute(
                delete(ReportType).where(ReportType.store_id == test_store_id)
            )
            # org-level report_types 중 seed 3개(lunch/dinner/morning) 외 정리
            await db.execute(
                delete(ReportType).where(
                    ReportType.organization_id == org_id,
                    ReportType.store_id.is_(None),
                    ReportType.code.notin_(["lunch", "dinner", "morning"]),
                )
            )
            # org-scoped daily 템플릿(테스트가 만든 것) 정리 — 시스템 default(org NULL)는 보존
            await db.execute(
                delete(ReportTemplate).where(
                    ReportTemplate.organization_id == org_id,
                    ReportTemplate.type == "daily",
                )
            )
            await db.commit()

    await _purge()
    yield
    await _purge()


@pytest_asyncio.fixture
async def daily_template(seed_organization: dict, clean_reports) -> None:
    """org-level all-types daily 템플릿 보장 (clean_reports purge 이후 생성).

    통합 경로는 org report_templates(type='daily') 가 있어야 생성 가능
    (startup 시드는 레거시 테이블 대상). 테스트용으로 명시 보장.
    """
    from app.schemas.report import ReportTemplateCreate

    org_id: UUID = seed_organization["id"]
    async with async_session() as db:
        await report_service.create_template(
            db,
            org_id,
            ReportTemplateCreate(
                type="daily",
                name="Default Daily",
                payload={"sections": [{"id": "d1", "title": "Summary", "sort_order": 0}]},
            ),
        )


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ===================================================================
# Unit: deadline computation + late flags
# ===================================================================


def test_compute_deadline_utc_store_tz():
    """UTC 매장: report_date 22:00 local == 22:00Z, offset 0."""
    rt = {"default_deadline_local_time": "22:00", "deadline_day_offset": 0}
    dl = report_service._compute_deadline_at(
        db_tz="UTC", report_date=date(2030, 3, 15), report_type=rt
    )
    assert dl == datetime(2030, 3, 15, 22, 0, tzinfo=timezone.utc)


def test_compute_deadline_offset_and_la_tz():
    """offset=+1 이면 다음날; LA(-07/-08) tz 변환 적용."""
    rt = {"default_deadline_local_time": "09:00", "deadline_day_offset": 1}
    dl = report_service._compute_deadline_at(
        db_tz="America/Los_Angeles", report_date=date(2030, 6, 1), report_type=rt
    )
    # 2030-06-02 09:00 PDT(-07:00) → 16:00Z
    assert dl == datetime(2030, 6, 2, 16, 0, tzinfo=timezone.utc)


def test_compute_deadline_none_when_no_time():
    rt = {"default_deadline_local_time": None, "deadline_day_offset": 0}
    assert (
        report_service._compute_deadline_at(
            db_tz="UTC", report_date=date(2030, 3, 15), report_type=rt
        )
        is None
    )


def test_late_flags():
    """is_late: submitted after deadline; is_overdue: past deadline & still draft."""
    past = datetime(2000, 1, 1, tzinfo=timezone.utc)

    r_draft = Report(
        type="daily", organization_id=UUID(int=0), status="draft",
        deadline_at=past, payload={},
    )
    overdue, late = report_service._compute_late_flags(r_draft)
    assert overdue is True and late is False

    r_sub = Report(
        type="daily", organization_id=UUID(int=0), status="submitted",
        deadline_at=past, submitted_at=datetime(2000, 1, 2, tzinfo=timezone.utc),
        payload={},
    )
    overdue, late = report_service._compute_late_flags(r_sub)
    assert overdue is False and late is True

    r_none = Report(type="daily", organization_id=UUID(int=0), status="draft", payload={})
    assert report_service._compute_late_flags(r_none) == (False, False)


# ===================================================================
# Integration: report types CRUD + resolution (C)
# ===================================================================


@pytest.mark.asyncio
async def test_report_types_effective_resolution(
    client, report_perms, clean_reports, test_store_id
):
    admin = await _login("testadmin")

    # 1) effective for store → seeded lunch/dinner active, morning inactive
    r = await client.get(
        f"/api/v1/console/report-types/?effective=true&store_id={test_store_id}",
        headers=_h(admin),
    )
    assert r.status_code == 200, r.text
    by_code = {i["code"]: i for i in r.json()["items"]}
    assert by_code["lunch"]["is_active"] is True
    assert by_code["dinner"]["is_active"] is True
    assert by_code["morning"]["is_active"] is False
    assert by_code["lunch"]["scope"] == "org"

    # 2) store override of 'lunch' (relabel) + store-only 'closing' with deadline
    r = await client.post(
        "/api/v1/console/report-types/",
        headers=_h(admin),
        json={
            "code": "lunch",
            "label": "Lunch Service",
            "store_id": str(test_store_id),
            "is_active": True,
            "sort_order": 1,
        },
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/api/v1/console/report-types/",
        headers=_h(admin),
        json={
            "code": "closing",
            "label": "Closing",
            "store_id": str(test_store_id),
            "is_active": True,
            "sort_order": 9,
            "default_deadline_local_time": "23:30",
        },
    )
    assert r.status_code == 201, r.text

    r = await client.get(
        f"/api/v1/console/report-types/?effective=true&store_id={test_store_id}",
        headers=_h(admin),
    )
    by_code = {i["code"]: i for i in r.json()["items"]}
    assert by_code["lunch"]["label"] == "Lunch Service"
    assert by_code["lunch"]["scope"] == "store"
    assert by_code["closing"]["scope"] == "store"
    assert "closing" in by_code

    # 3) org-default code conflict → 409
    r = await client.post(
        "/api/v1/console/report-types/",
        headers=_h(admin),
        json={"code": "lunch", "label": "Dup"},
    )
    assert r.status_code == 409, r.text


# ===================================================================
# Integration: per-person daily duplicate (B)
# ===================================================================


@pytest.mark.asyncio
async def test_per_person_daily_duplicate(
    client, report_perms, daily_template, test_store_id
):
    sv = await _login("testsv")
    staff = await _login("teststaff")
    rd = "2030-04-10"

    def _body():
        return {
            "type": "daily",
            "store_id": str(test_store_id),
            "report_date": rd,
            "payload": {"period": "lunch"},
        }

    # SV creates lunch report
    r1 = await client.post("/api/v1/app/my/reports", headers=_h(sv), json=_body())
    assert r1.status_code == 201, r1.text

    # Staff creates lunch report for SAME store/date/period → allowed (different author)
    r2 = await client.post("/api/v1/app/my/reports", headers=_h(staff), json=_body())
    assert r2.status_code == 201, r2.text
    assert r2.json()["id"] != r1.json()["id"]

    # SV creates the same slot again → 409 (own duplicate)
    r3 = await client.post("/api/v1/app/my/reports", headers=_h(sv), json=_body())
    assert r3.status_code == 409, r3.text
    assert "existing_report_id" in r3.json()["detail"]


@pytest.mark.asyncio
async def test_daily_period_must_be_enabled(
    client, report_perms, clean_reports, test_store_id
):
    """morning 은 기본 비활성 → 생성 거부(400)."""
    staff = await _login("teststaff")
    r = await client.post(
        "/api/v1/app/my/reports",
        headers=_h(staff),
        json={
            "type": "daily",
            "store_id": str(test_store_id),
            "report_date": "2030-04-11",
            "payload": {"period": "morning"},
        },
    )
    assert r.status_code == 400, r.text


# ===================================================================
# Integration: deadline on creation (E)
# ===================================================================


@pytest.mark.asyncio
async def test_daily_deadline_from_store_type(
    client, report_perms, daily_template, test_store_id
):
    admin = await _login("testadmin")
    staff = await _login("teststaff")

    # store-only type 'closing' with deadline 22:00 (UTC store)
    r = await client.post(
        "/api/v1/console/report-types/",
        headers=_h(admin),
        json={
            "code": "closing",
            "label": "Closing",
            "store_id": str(test_store_id),
            "is_active": True,
            "default_deadline_local_time": "22:00",
            "deadline_day_offset": 0,
        },
    )
    assert r.status_code == 201, r.text

    r = await client.post(
        "/api/v1/app/my/reports",
        headers=_h(staff),
        json={
            "type": "daily",
            "store_id": str(test_store_id),
            "report_date": "2030-03-15",
            "payload": {"period": "closing"},
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["deadline_at"] is not None
    dl = datetime.fromisoformat(body["deadline_at"])
    assert dl == datetime(2030, 3, 15, 22, 0, tzinfo=timezone.utc)


# ===================================================================
# Integration: template applicable_types selection (D)
# ===================================================================


@pytest.mark.asyncio
async def test_template_applicable_types_selection(
    client, report_perms, clean_reports, test_store_id
):
    admin = await _login("testadmin")
    staff = await _login("teststaff")

    # Template A: lunch-only
    ra = await client.post(
        "/api/v1/console/report-templates",
        headers=_h(admin),
        json={
            "type": "daily",
            "name": "Lunch Template",
            "applicable_types": ["lunch"],
            "payload": {"sections": [{"id": "s1", "title": "LUNCH SECTION", "sort_order": 0}]},
        },
    )
    assert ra.status_code in (200, 201), ra.text
    assert ra.json()["applicable_types"] == ["lunch"]

    # Template B: all-types (applicable_types null)
    rb = await client.post(
        "/api/v1/console/report-templates",
        headers=_h(admin),
        json={
            "type": "daily",
            "name": "General Template",
            "payload": {"sections": [{"id": "g1", "title": "GENERAL SECTION", "sort_order": 0}]},
        },
    )
    assert rb.status_code in (200, 201), rb.text

    # daily lunch → picks A
    r1 = await client.post(
        "/api/v1/app/my/reports",
        headers=_h(staff),
        json={
            "type": "daily", "store_id": str(test_store_id),
            "report_date": "2030-05-01", "payload": {"period": "lunch"},
        },
    )
    assert r1.status_code == 201, r1.text
    titles = [s["title"] for s in r1.json()["payload"]["sections"]]
    assert "LUNCH SECTION" in titles

    # daily dinner → no lunch-specific template → all-types B
    r2 = await client.post(
        "/api/v1/app/my/reports",
        headers=_h(staff),
        json={
            "type": "daily", "store_id": str(test_store_id),
            "report_date": "2030-05-01", "payload": {"period": "dinner"},
        },
    )
    assert r2.status_code == 201, r2.text
    titles2 = [s["title"] for s in r2.json()["payload"]["sections"]]
    assert "GENERAL SECTION" in titles2


# ===================================================================
# Integration: review + acknowledge (F)
# ===================================================================


@pytest.mark.asyncio
async def test_review_and_acknowledge_flow(
    client, report_perms, daily_template, test_store_id
):
    staff = await _login("teststaff")
    admin = await _login("testadmin")  # owner → store access bypass

    # create + submit
    r = await client.post(
        "/api/v1/app/my/reports",
        headers=_h(staff),
        json={
            "type": "daily", "store_id": str(test_store_id),
            "report_date": "2030-06-20", "payload": {"period": "lunch"},
        },
    )
    assert r.status_code == 201, r.text
    rid = r.json()["id"]

    # review before submit → 400 (console)
    rev_early = await client.post(
        f"/api/v1/console/reports/{rid}/review",
        headers=_h(admin), json={"feedback": "too early"},
    )
    assert rev_early.status_code == 400, rev_early.text

    sub = await client.post(f"/api/v1/app/my/reports/{rid}/submit", headers=_h(staff))
    assert sub.status_code == 200, sub.text
    assert sub.json()["status"] == "submitted"

    # review with feedback (console manager surface)
    rev = await client.post(
        f"/api/v1/console/reports/{rid}/review",
        headers=_h(admin), json={"feedback": "Looks good, thanks"},
    )
    assert rev.status_code == 200, rev.text
    body = rev.json()
    assert body["status"] == "reviewed"
    assert body["reviewed_by_id"] is not None
    assert body["reviewed_at"] is not None
    assert any(c["content"] == "Looks good, thanks" for c in body["comments"])

    # acknowledge via APP surface (author confirms) → count 1
    ack1 = await client.post(
        f"/api/v1/app/my/reports/{rid}/acknowledge", headers=_h(staff)
    )
    assert ack1.status_code == 200, ack1.text
    assert ack1.json()["acknowledgement_count"] == 1

    # acknowledge via CONSOLE surface (different user) → count 2
    ack2 = await client.post(
        f"/api/v1/console/reports/{rid}/acknowledge", headers=_h(admin)
    )
    assert ack2.status_code == 200, ack2.text
    assert ack2.json()["acknowledgement_count"] == 2

    # idempotent: same console user re-acks → still 2
    ack3 = await client.post(
        f"/api/v1/console/reports/{rid}/acknowledge", headers=_h(admin)
    )
    assert ack3.status_code == 200, ack3.text
    assert ack3.json()["acknowledgement_count"] == 2

    # detail shows reviewed + ack
    det = await client.get(f"/api/v1/console/reports/{rid}", headers=_h(admin))
    assert det.status_code == 200, det.text
    dj = det.json()
    assert dj["status"] == "reviewed"
    assert dj["acknowledgement_count"] == 2
    assert dj["acknowledgements"][0]["user_name"]


@pytest.mark.asyncio
async def test_app_effective_report_types_endpoint(
    client, report_perms, clean_reports, test_store_id
):
    """app report-types selector endpoint: active_only filters morning out."""
    staff = await _login("teststaff")
    r = await client.get(
        f"/api/v1/app/my/reports/report-types?store_id={test_store_id}",
        headers=_h(staff),
    )
    assert r.status_code == 200, r.text
    codes = {i["code"] for i in r.json()["items"]}
    assert "lunch" in codes and "dinner" in codes
    assert "morning" not in codes  # inactive filtered by active_only default


# ===================================================================
# Integration: store override enable/disable (C, 추가 분기)
# ===================================================================


@pytest.mark.asyncio
async def test_store_override_enable_and_disable(
    client, report_perms, daily_template, test_store_id
):
    """store override 로 비활성 morning 을 켜고, 기본 활성 lunch 를 끈다.

    - effective: morning→active(scope store), lunch→inactive(scope store).
    - create: morning 이제 허용(201), lunch 이제 거부(400).
    """
    admin = await _login("testadmin")
    staff = await _login("teststaff")

    # store row: morning 활성화(override of inactive org default)
    r = await client.post(
        "/api/v1/console/report-types/",
        headers=_h(admin),
        json={
            "code": "morning", "label": "Morning Service",
            "store_id": str(test_store_id), "is_active": True, "sort_order": 0,
        },
    )
    assert r.status_code == 201, r.text
    # store row: lunch 비활성화(override of active org default)
    r = await client.post(
        "/api/v1/console/report-types/",
        headers=_h(admin),
        json={
            "code": "lunch", "label": "Lunch (off)",
            "store_id": str(test_store_id), "is_active": False, "sort_order": 1,
        },
    )
    assert r.status_code == 201, r.text

    r = await client.get(
        f"/api/v1/console/report-types/?effective=true&store_id={test_store_id}",
        headers=_h(admin),
    )
    by_code = {i["code"]: i for i in r.json()["items"]}
    assert by_code["morning"]["is_active"] is True
    assert by_code["morning"]["scope"] == "store"
    assert by_code["lunch"]["is_active"] is False
    assert by_code["lunch"]["scope"] == "store"

    # morning 이제 생성 가능
    r1 = await client.post(
        "/api/v1/app/my/reports",
        headers=_h(staff),
        json={
            "type": "daily", "store_id": str(test_store_id),
            "report_date": "2030-07-01", "payload": {"period": "morning"},
        },
    )
    assert r1.status_code == 201, r1.text

    # lunch 이제 거부 (store 에서 비활성)
    r2 = await client.post(
        "/api/v1/app/my/reports",
        headers=_h(staff),
        json={
            "type": "daily", "store_id": str(test_store_id),
            "report_date": "2030-07-01", "payload": {"period": "lunch"},
        },
    )
    assert r2.status_code == 400, r2.text


# ===================================================================
# Integration: permission gating (report_types:manage / reports:review)
# ===================================================================


@pytest_asyncio.fixture
async def limited_reporter_token(report_perms, seed_organization) -> str:
    """reports:read 만 가진 커스텀 role 의 유저 토큰.

    report_types:manage / reports:review 게이트가 실제로 막히는지 검증용.
    (owner/super_owner 는 require_permission bypass 라 게이팅 음성 케이스에 못 씀)
    """
    from app.models.user import Role, User
    from app.utils.jwt import create_access_token
    from app.utils.password import hash_password

    org_id: UUID = seed_organization["id"]
    async with async_session() as db:
        role = (
            await db.execute(
                select(Role).where(
                    Role.organization_id == org_id,
                    Role.name == "limited_reporter",
                )
            )
        ).scalar_one_or_none()
        if role is None:
            role = Role(organization_id=org_id, name="limited_reporter", priority=45)
            db.add(role)
            await db.flush()
        # 정확히 reports:read 하나만 부여 (idempotent)
        read_perm = (
            await db.execute(
                select(Permission).where(Permission.code == "reports:read")
            )
        ).scalar_one()
        has_read = (
            await db.execute(
                select(RolePermission).where(
                    RolePermission.role_id == role.id,
                    RolePermission.permission_id == read_perm.id,
                )
            )
        ).scalar_one_or_none()
        if has_read is None:
            db.add(RolePermission(role_id=role.id, permission_id=read_perm.id))

        user = (
            await db.execute(
                select(User).where(
                    User.username == "testlimited",
                    User.organization_id == org_id,
                )
            )
        ).scalar_one_or_none()
        if user is None:
            user = User(
                organization_id=org_id,
                role_id=role.id,
                username="testlimited",
                full_name="Limited Reporter",
                password_hash=hash_password("1234"),
                is_active=True,
            )
            db.add(user)
        else:
            user.role_id = role.id
            user.is_active = True
        await db.commit()
        await db.refresh(user)
        return create_access_token(
            {"sub": str(user.id), "org": str(user.organization_id)}
        )


@pytest.mark.asyncio
async def test_report_types_manage_permission_gating(
    client, limited_reporter_token, clean_reports, test_store_id
):
    """reports:read 만 가진 유저: report-types GET 가능, 그러나 manage(CUD)는 403."""
    tok = limited_reporter_token

    # read 는 통과 (org-default 목록; store_id 주면 store-access 게이트라 분리)
    r = await client.get(
        "/api/v1/console/report-types/?effective=true",
        headers=_h(tok),
    )
    assert r.status_code == 200, r.text

    # create 는 report_types:manage 없으면 403
    r = await client.post(
        "/api/v1/console/report-types/",
        headers=_h(tok),
        json={"code": "closing", "label": "Closing"},
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_review_permission_gating(client, limited_reporter_token):
    """reports:review 없는 유저는 리뷰 엔드포인트 403 (require_permission 게이트)."""
    tok = limited_reporter_token
    r = await client.post(
        f"/api/v1/console/reports/{uuid4()}/review",
        headers=_h(tok),
        json={"feedback": "nope"},
    )
    assert r.status_code == 403, r.text


# ===================================================================
# Regression: issue reports still work (multi-type 통합 경로)
# ===================================================================


@pytest.mark.asyncio
async def test_issue_report_lifecycle_regression(
    client, report_perms, clean_reports, test_store_id
):
    """issue 타입: 생성(open)→transition(in_progress→closed) + severity 검증.

    daily 재설계가 issue 경로를 깨지 않았는지 확인.
    """
    staff = await _login("teststaff")

    # 생성: 기본 status=open
    r = await client.post(
        "/api/v1/app/my/reports",
        headers=_h(staff),
        json={
            "type": "issue",
            "store_id": str(test_store_id),
            "title": "Walk-in fridge down",
            "payload": {"category": "equipment", "severity": "high"},
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "open"
    rid = body["id"]

    # 잘못된 severity → 400 (검증 분기)
    bad = await client.post(
        "/api/v1/app/my/reports",
        headers=_h(staff),
        json={
            "type": "issue",
            "store_id": str(test_store_id),
            "title": "x",
            "payload": {"category": "equipment", "severity": "nope"},
        },
    )
    assert bad.status_code == 400, bad.text

    # transition open → in_progress → closed
    t1 = await client.post(
        f"/api/v1/app/my/reports/{rid}/transition",
        headers=_h(staff),
        json={"status": "in_progress"},
    )
    assert t1.status_code == 200, t1.text
    assert t1.json()["status"] == "in_progress"

    t2 = await client.post(
        f"/api/v1/app/my/reports/{rid}/transition",
        headers=_h(staff),
        json={"status": "closed"},
    )
    assert t2.status_code == 200, t2.text
    assert t2.json()["status"] == "closed"

    # daily 와 달리 issue 는 deadline 없음
    assert body["deadline_at"] is None
