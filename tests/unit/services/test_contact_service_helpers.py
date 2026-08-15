"""Contacts — 순수 로직 유닛 테스트 (DB/HTTP 없음).

커버 범위
    - `normalize_phone`: 숫자만 남기기 / 국가코드 보존 / 숫자 없으면 None
    - `_escape_like`: 사용자가 친 LIKE 와일드카드가 와일드카드로 새지 않는지
    - `_parse_uuid`: 잘못된 식별자는 500 이 아니라 400 도메인 에러
    - `_visibility_clause` (D1): Owner/GM 은 store 조건 없음, SV 는 조건 있음
    - `diff_snapshots` / `contact_snapshot`: 변경된 키만, 배열은 통째로
    - Pydantic 스키마 검증: 사유 필수 / 태그 대소문자 흡수 / 대표번호 1개 / 신청 모양
    - `contact_audit_service.record`: 오타 action 은 조용히 넘기지 않고 즉시 ValueError
"""

from __future__ import annotations

import uuid

import pytest

from app.models.contact import Contact
from app.models.user import Role, User
from app.schemas.contact import (
    ContactChangeRequestCreate,
    ContactCreate,
    ContactDeleteRequest,
    ContactPayload,
    ContactPhoneInput,
    ContactRequestReject,
    ContactUpdate,
)
from app.services.contact_audit_service import (
    contact_audit_service,
    contact_snapshot,
    diff_snapshots,
)
from app.services.contact_service import _escape_like, _parse_uuid, contact_service
from app.utils.exceptions import AppError
from app.utils.phone import normalize_phone


# ---------------------------------------------------------------------------
# 전화번호 정규화
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("213-555-0142", "2135550142"),
        ("(213) 555 0142", "2135550142"),
        ("+1 213.555.0142 ext 7", "121355501427"),
        ("1-213-555-0142", "12135550142"),  # 선행 '1' 은 제거하지 않는다
        ("", None),
        (None, None),
        ("no digits here", None),
    ],
)
def test_normalize_phone(raw: str | None, expected: str | None) -> None:
    assert normalize_phone(raw) == expected


# ---------------------------------------------------------------------------
# LIKE 이스케이프 / UUID 파싱
# ---------------------------------------------------------------------------


def test_escape_like_neutralizes_wildcards() -> None:
    assert _escape_like("100%") == "100\\%"
    assert _escape_like("a_b") == "a\\_b"
    assert _escape_like("back\\slash") == "back\\\\slash"
    assert _escape_like("plain") == "plain"


def test_parse_uuid_rejects_garbage_with_400() -> None:
    ok = uuid.uuid4()
    assert _parse_uuid(str(ok), "store_id") == ok

    with pytest.raises(AppError) as exc:
        _parse_uuid("not-a-uuid", "store_id")
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "CONTACT_VALIDATION_ERROR"
    assert "store_id" in exc.value.detail["message"]


# ---------------------------------------------------------------------------
# 가시성 절 (D1)
# ---------------------------------------------------------------------------


def _user(priority: int) -> User:
    """role priority 만 필요한 임시(비영속) User."""
    u = User(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        username="tmp",
        full_name="Tmp",
    )
    u.role = Role(name="tmp", priority=priority)
    return u


def test_visibility_owner_has_no_store_clause() -> None:
    # Owner 는 accessible=None (전 매장)
    assert contact_service._visibility_clause(_user(10), None) is None


def test_visibility_gm_sees_every_store_even_with_limited_accessible() -> None:
    # GM 은 관리 매장만 accessible 로 받지만 연락처 가시성은 전 매장 (D1 예외)
    gm = _user(20)
    assert contact_service._visibility_clause(gm, [uuid.uuid4()]) is None


def test_visibility_sv_is_restricted_to_all_store_or_assigned() -> None:
    sv = _user(30)
    store_id = uuid.uuid4()
    clause = contact_service._visibility_clause(sv, [store_id])
    assert clause is not None
    sql = str(clause)
    assert "IS NULL" in sql and "IN " in sql


def test_visibility_sv_without_stores_sees_only_all_store_contacts() -> None:
    sv = _user(30)
    clause = contact_service._visibility_clause(sv, [])
    assert clause is not None
    # Contact.store_id IS NULL 단일 조건
    assert str(clause) == str(Contact.store_id.is_(None))


# ---------------------------------------------------------------------------
# 이력 스냅샷 / diff
# ---------------------------------------------------------------------------


def _snap(**over) -> dict:
    base = dict(
        name="Acme Plumbing",
        company="Acme",
        email="a@acme.com",
        memo="24h",
        store_id=None,
        store_name=None,
        phones=[{"label": "office", "number": "213-555-0142", "is_primary": True}],
        tags=["vendor"],
    )
    base.update(over)
    return contact_snapshot(**base)


def test_diff_snapshots_returns_only_changed_keys() -> None:
    before = _snap()
    after = _snap(company="Acme Inc.")
    changed_before, changed_after = diff_snapshots(before, after)
    assert set(changed_after) == {"company"}
    assert changed_before == {"company": "Acme"}
    assert changed_after == {"company": "Acme Inc."}


def test_diff_snapshots_treats_arrays_as_a_whole() -> None:
    before = _snap()
    after = _snap(
        phones=[
            {"label": "office", "number": "213-555-0142", "is_primary": True},
            {"label": "mobile", "number": "213-555-9999", "is_primary": False},
        ]
    )
    changed_before, changed_after = diff_snapshots(before, after)
    assert set(changed_after) == {"phones"}
    assert len(changed_after["phones"]) == 2
    assert len(changed_before["phones"]) == 1


def test_diff_snapshots_noop_is_empty() -> None:
    assert diff_snapshots(_snap(), _snap()) == ({}, {})


# ---------------------------------------------------------------------------
# 스키마 검증
# ---------------------------------------------------------------------------


def test_payload_trims_and_nulls_blank_optional_fields() -> None:
    p = ContactPayload(name="  Acme  ", company="   ", memo="")
    assert p.name == "Acme"
    assert p.company is None
    assert p.memo is None


def test_payload_rejects_blank_name_and_bad_email() -> None:
    with pytest.raises(ValueError):
        ContactPayload(name="   ")
    with pytest.raises(ValueError):
        ContactPayload(name="Acme", email="not-an-email")


def test_tags_are_deduped_case_insensitively_and_keep_first_casing() -> None:
    p = ContactPayload(name="Acme", tags=["Vendor", " vendor ", "VENDOR", "  ", "Food"])
    assert p.tags == ["Vendor", "Food"]


def test_tag_and_phone_limits() -> None:
    with pytest.raises(ValueError):
        ContactPayload(name="Acme", tags=[f"t{i}" for i in range(21)])
    with pytest.raises(ValueError):
        ContactPayload(
            name="Acme",
            phones=[ContactPhoneInput(number=f"213555{i:04d}") for i in range(11)],
        )


def test_only_one_primary_phone_allowed() -> None:
    with pytest.raises(ValueError):
        ContactPayload(
            name="Acme",
            phones=[
                ContactPhoneInput(number="213-555-0142", is_primary=True),
                ContactPhoneInput(number="213-555-9999", is_primary=True),
            ],
        )


def test_update_requires_reason_and_cannot_blank_the_name() -> None:
    # 사유 누락/공백은 **스키마가 아니라 서비스**가 거른다 — 계약 §6 의 400
    # CONTACT_REASON_REQUIRED 로 내리기 위해서다(Pydantic 필수로 두면 422 가 된다).
    # 400 응답은 API 통합 테스트가 고정한다.
    assert ContactUpdate(name="Acme").reason is None
    assert ContactUpdate(reason="   ").reason is None
    with pytest.raises(ValueError):
        ContactUpdate(name=None, reason="fix")
    ok = ContactUpdate(name=" Acme ", reason=" typo fix ")
    assert ok.name == "Acme" and ok.reason == "typo fix"
    # 보내지 않은 키는 exclude_unset 으로 걸러진다 (PUT 부분 반영 규약)
    assert set(ok.model_dump(exclude_unset=True)) == {"name", "reason"}


def test_delete_and_reject_reasons_are_trimmed_and_checked_by_the_service() -> None:
    # 빈 사유는 스키마를 통과하고 서비스가 400 으로 막는다(위와 같은 이유).
    assert ContactDeleteRequest(reason="  ").reason is None
    assert ContactRequestReject(reason="").reason is None
    assert ContactDeleteRequest(reason=" moved out ").reason == "moved out"
    # 길이 상한은 여전히 스키마가 본다
    with pytest.raises(ValueError):
        ContactDeleteRequest(reason="x" * 501)


def test_create_keeps_reason_optional() -> None:
    assert ContactCreate(name="Acme").reason is None


@pytest.mark.parametrize(
    "kwargs",
    [
        # create 신청은 기존 연락처를 가리킬 수 없다
        dict(request_type="create", contact_id=str(uuid.uuid4()),
             payload=ContactPayload(name="Acme")),
        # create 신청은 내용이 필요하다
        dict(request_type="create"),
        # update 신청은 대상 + 내용 + 사유가 모두 필요하다
        dict(request_type="update", payload=ContactPayload(name="Acme"), reason="r"),
        dict(request_type="update", contact_id=str(uuid.uuid4()), reason="r"),
        dict(request_type="update", contact_id=str(uuid.uuid4()),
             payload=ContactPayload(name="Acme")),
        # delete 신청은 내용을 실을 수 없고 사유가 필요하다
        dict(request_type="delete", contact_id=str(uuid.uuid4()),
             payload=ContactPayload(name="Acme"), reason="r"),
        dict(request_type="delete", contact_id=str(uuid.uuid4())),
    ],
)
def test_change_request_shape_rules(kwargs: dict) -> None:
    """shape/사유 검증은 스키마가 아니라 서비스가 본다 (계약 §6: 422 아닌 400).

    스키마 생성 자체는 통과하고, 서비스 검증에서 CONTACT_* 도메인 에러가 나야 한다.
    """
    data = ContactChangeRequestCreate(**kwargs)
    with pytest.raises(AppError) as exc:
        contact_service._validate_request_shape(data)
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] in {
        "CONTACT_VALIDATION_ERROR",
        "CONTACT_REASON_REQUIRED",
    }


def test_change_request_valid_shapes() -> None:
    cid = str(uuid.uuid4())
    assert ContactChangeRequestCreate(
        request_type="create", payload=ContactPayload(name="Acme")
    ).contact_id is None
    assert ContactChangeRequestCreate(
        request_type="update", contact_id=cid,
        payload=ContactPayload(name="Acme"), reason="fix",
    ).reason == "fix"
    assert ContactChangeRequestCreate(
        request_type="delete", contact_id=cid, reason="closed",
    ).payload is None


# ---------------------------------------------------------------------------
# 이력 기록 진입점
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_record_rejects_unknown_action() -> None:
    """오타 action 은 조용히 기록 누락되지 않고 즉시 터진다."""
    with pytest.raises(ValueError):
        await contact_audit_service.record(
            None,  # type: ignore[arg-type]  # 검증에서 먼저 막히므로 db 는 안 쓰인다
            organization_id=uuid.uuid4(),
            action="creat",
            actor=None,
        )
