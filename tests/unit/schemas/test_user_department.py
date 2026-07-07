"""Unit tests — department (FOH/BOH) Pydantic 검증.

대상: app/schemas/user.py 의 UserCreate / UserUpdate / UserResponse / UserListResponse.
값은 대문자 약어 "FOH" / "BOH" 고정 (대소문자 구분).

[작성됨]
- UserCreate: department 생략 시 None (default)
- UserCreate: "FOH"/"BOH" 허용
- UserCreate: 잘못된 값(엉뚱한 값/소문자/빈문자) → ValidationError
- UserUpdate: "FOH"/"BOH"/None 허용, 잘못된 값 → ValidationError
- UserUpdate: 생략 시 model_dump(exclude_unset)에서 빠짐, 명시 null 은 포함
- UserResponse/UserListResponse: department 직렬화 + default None
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.user import UserCreate, UserListResponse, UserResponse, UserUpdate


def _base_create_kwargs(**extra) -> dict:
    return {"username": "user1", "password": "p", "full_name": "U", "role_id": "r", **extra}


def test_user_create_department_defaults_none():
    data = UserCreate(**_base_create_kwargs())
    assert data.department is None


@pytest.mark.parametrize("value", ["FOH", "BOH"])
def test_user_create_accepts_valid_department(value):
    data = UserCreate(**_base_create_kwargs(department=value))
    assert data.department == value


@pytest.mark.parametrize("value", ["kitchen", "foh", "Front", ""])
def test_user_create_rejects_invalid_department(value):
    """엉뚱한 값/소문자(대소문자 구분)/빈문자 모두 거부."""
    with pytest.raises(ValidationError):
        UserCreate(**_base_create_kwargs(department=value))


@pytest.mark.parametrize("value", ["FOH", "BOH", None])
def test_user_update_accepts_valid_department(value):
    data = UserUpdate(department=value)
    assert data.department == value


def test_user_update_omitted_is_unset():
    """department 미지정 시 model_dump(exclude_unset)에서 빠져야 함 (부분 업데이트 보장)."""
    data = UserUpdate(full_name="x")
    assert "department" not in data.model_dump(exclude_unset=True)


def test_user_update_explicit_null_is_set():
    """null 을 명시하면 set 으로 간주 → 미지정으로 해제하는 의도가 전달돼야 함."""
    dumped = UserUpdate(department=None).model_dump(exclude_unset=True)
    assert "department" in dumped and dumped["department"] is None


def test_user_update_rejects_invalid_department():
    with pytest.raises(ValidationError):
        UserUpdate(department="server")


def test_user_response_serializes_department():
    resp = UserResponse(
        id="x", username="u", full_name="U", email=None, email_verified=False,
        role_name="staff", role_priority=40, department="FOH",
        is_active=True, created_at=datetime.now(timezone.utc),
    )
    assert resp.department == "FOH"


def test_user_list_response_department_defaults_none():
    resp = UserListResponse(
        id="x", username="u", full_name="U", role_name="staff", role_priority=40,
        is_active=True, created_at=datetime.now(timezone.utc),
    )
    assert resp.department is None
