"""요청 스키마 진입 시점의 이메일 정규화.

`users.email` 에 값이 들어가는 경로는 대부분 이 스키마들을 통과한다.
여기서 정규화가 빠지면 서비스 계층이 원본 대소문자를 그대로 저장하고,
이후 이메일 조회가 영구히 실패한다(중복 계정 생성 + 비밀번호 재설정 불가).
"""

import pytest

from app.schemas.auth import RegisterRequest
from app.schemas.user import ProfileUpdate, UserCreate, UserUpdate

MIXED = "  New.User@Example.COM "
CANON = "new.user@example.com"


class TestRegisterRequest:
    """가입 — email 필수. 유령계정 인수/직접가입/hiring 확정 모두 이 스키마를 거친다."""

    def _build(self, email: str) -> RegisterRequest:
        return RegisterRequest(
            username="newuser",
            password="password123",
            full_name="New User",
            email=email,
            verification_token="tok",
        )

    def test_normalizes(self) -> None:
        assert self._build(MIXED).email == CANON

    def test_already_canonical_unchanged(self) -> None:
        assert self._build(CANON).email == CANON


class TestUserCreate:
    """관리자가 직원 생성 — email 선택. CSV 일괄 업로드도 이 스키마로 수렴한다."""

    def _build(self, email: str | None) -> UserCreate:
        return UserCreate(
            username="newuser",
            password="password123",
            full_name="New User",
            email=email,
            role_id="00000000-0000-0000-0000-000000000000",
        )

    def test_normalizes(self) -> None:
        assert self._build(MIXED).email == CANON

    def test_none_stays_none(self) -> None:
        assert self._build(None).email is None

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_becomes_none(self, blank: str) -> None:
        assert self._build(blank).email is None


class TestUserUpdate:
    """관리자가 직원 이메일 변경 — 변경 시 email_verified 가 리셋되는 경로."""

    def test_normalizes(self) -> None:
        assert UserUpdate(email=MIXED).email == CANON

    def test_omitted_stays_none(self) -> None:
        """보내지 않은 필드는 None — '변경 없음'과 구분되어야 한다."""
        assert UserUpdate().email is None


class TestProfileUpdate:
    """직원 본인이 앱에서 이메일 변경."""

    def test_normalizes(self) -> None:
        assert ProfileUpdate(email=MIXED).email == CANON

    def test_omitted_stays_none(self) -> None:
        assert ProfileUpdate().email is None
