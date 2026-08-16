"""Contacts — 순수 로직 유닛 테스트 (DB/HTTP 없음).

커버 범위
    - `normalize_phone`: 숫자만 남기기 / 국가코드 보존 / 숫자 없으면 None
    - `_escape_like`: 사용자가 친 LIKE 와일드카드가 와일드카드로 새지 않는지
    - `_parse_uuid`: 잘못된 식별자는 500 이 아니라 400 도메인 에러
    - `_visibility_clause` (D1): Owner/GM 은 store 조건 없음, SV 는 조건 있음
    - `_validate_visibility_state` (확장 D1): 모드 ↔ 대상 목록 정합성
    - `_normalize_request_payload` (계약 개정 §0-A): 구형식 신청 폴백
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
from app.services.contact_service import (
    _escape_like,
    _normalize_request_payload,
    _parse_uuid,
    contact_service,
)
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


def test_visibility_owner_has_no_clause() -> None:
    # Owner 만 예외 — 조건 자체가 안 붙는다 (V1/V3)
    assert contact_service._visibility_clause(_user(10), None) is None


def test_visibility_gm_no_longer_bypasses() -> None:
    """GM 의 전 매장 예외는 폐기됐다 (V3).

    이게 되살아나면 '개인 지정' 연락처가 GM 에게 뚫린다 — 이 트랙의 핵심 회귀 지점.
    """
    gm = _user(20)
    clause = contact_service._visibility_clause(gm, [uuid.uuid4()])
    assert clause is not None


def test_visibility_sv_clause_covers_org_wide_creator_and_targets() -> None:
    sv = _user(30)
    clause = contact_service._visibility_clause(sv, [uuid.uuid4()])
    assert clause is not None
    sql = str(clause)
    # 전체 공유 OR 작성자 본인 OR 대상 매칭(EXISTS)
    assert "visibility" in sql
    assert "created_by" in sql
    assert "EXISTS" in sql and "contact_visibility_targets" in sql


def test_visibility_without_stores_still_matches_role_and_user_targets() -> None:
    """배정 매장이 0개여도 직급·개인 지정으로는 보여야 한다.

    예전엔 매장이 유일한 축이라 '매장 없으면 전체공유만'이었다. 3축이 된 뒤로는
    그 지름길이 틀렸다 — 매장 없는 사람도 직급/개인 지정으로 볼 수 있다.
    """
    sv = _user(30)
    clause = contact_service._visibility_clause(sv, [])
    assert clause is not None
    sql = str(clause)
    assert "contact_visibility_targets" in sql


# ---------------------------------------------------------------------------
# 가시성 모드 ↔ 대상 목록 정합성 (확장 D1)
# ---------------------------------------------------------------------------


def test_visibility_state_organization_with_no_stores_is_valid() -> None:
    contact_service._validate_visibility_state("organization", [])


def test_visibility_state_restricted_with_targets_is_valid() -> None:
    contact_service._validate_visibility_state("restricted", [("store", uuid.uuid4())])


def test_visibility_state_restricted_without_targets_is_rejected() -> None:
    """대상 0개인데 전체 공유도 아니면 거부 — 이게 이 결정의 핵심이다 (V1)."""
    with pytest.raises(AppError) as exc:
        contact_service._validate_visibility_state("restricted", [])
    assert exc.value.detail["code"] == "CONTACT_VISIBILITY_REQUIRED"


def test_visibility_state_organization_with_targets_is_rejected() -> None:
    with pytest.raises(AppError) as exc:
        contact_service._validate_visibility_state("organization", [("store", uuid.uuid4())])
    assert exc.value.detail["code"] == "CONTACT_VISIBILITY_CONFLICT"


def test_visibility_state_unknown_mode_is_rejected() -> None:
    with pytest.raises(AppError) as exc:
        contact_service._validate_visibility_state("nonsense", [])
    assert exc.value.detail["code"] == "CONTACT_VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 구형식 신청 payload 폴백 (계약 개정 §0-A)
# ---------------------------------------------------------------------------


def test_normalize_payload_passes_through_new_format() -> None:
    raw = {"name": "A", "visibility": "restricted", "targets": [{"type": "store", "id": "x"}]}
    assert _normalize_request_payload(raw) is raw


def test_normalize_payload_legacy_null_store_becomes_organization() -> None:
    out = _normalize_request_payload({"name": "A", "store_id": None})
    assert out == {
        "name": "A",
        "visibility": "organization",
        "targets": [],
        "excluded_user_ids": [],
    }


def test_normalize_payload_legacy_store_becomes_stores_mode() -> None:
    """폴백이 없으면 승인 순간 가시성이 조용히 전 조직으로 넓어진다 — 그걸 막는 테스트."""
    store_id = str(uuid.uuid4())
    out = _normalize_request_payload({"name": "A", "store_id": store_id})
    assert out["visibility"] == "restricted"
    assert out["targets"] == [{"type": "store", "id": store_id}]
    assert "store_id" not in out


def test_normalize_payload_handles_none() -> None:
    assert _normalize_request_payload(None) is None


def test_normalize_payload_leaves_payload_without_either_key_alone() -> None:
    raw = {"name": "A"}
    assert _normalize_request_payload(raw) is raw


# ---------------------------------------------------------------------------
# 이력 스냅샷 / diff
# ---------------------------------------------------------------------------


def _snap(**over) -> dict:
    base = dict(
        name="Acme Plumbing",
        company="Acme",
        email="a@acme.com",
        memo="24h",
        visibility="organization",
        targets=[],
        excluded_users=[],
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
