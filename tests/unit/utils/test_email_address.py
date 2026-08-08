"""app/utils/email_address — users.email canonical 형태 규칙.

이 헬퍼가 흔들리면 저장값과 조회 입력의 형태가 어긋나 이메일로 사용자를
못 찾게 된다(중복 가입 통과 + 비밀번호 재설정 불가). 규칙을 여기서 고정한다.
"""

import pytest

from app.utils.email_address import normalize_email, normalize_email_optional


class TestNormalizeEmail:
    """필수 이메일 필드용 — 항상 str 반환."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("user@example.com", "user@example.com"),  # 이미 canonical → 그대로
            ("User@Example.com", "user@example.com"),  # 대문자 → 소문자
            ("USER@EXAMPLE.COM", "user@example.com"),  # 전체 대문자
            ("  user@example.com  ", "user@example.com"),  # 앞뒤 공백 제거
            ("\tUser@Example.COM\n", "user@example.com"),  # 탭/개행도 제거
            ("Josefinagarellano21@gmail.com", "josefinagarellano21@gmail.com"),  # 실제 사고 사례
        ],
    )
    def test_normalizes(self, raw: str, expected: str) -> None:
        assert normalize_email(raw) == expected

    def test_idempotent(self) -> None:
        """두 번 적용해도 같은 값 — 마이그레이션 재실행 안전성의 근거."""
        once = normalize_email("  Mixed@Case.COM ")
        assert normalize_email(once) == once

    def test_empty_and_none_become_empty_string(self) -> None:
        """필수 필드용이라 None 을 받아도 터지지 않고 빈 문자열."""
        assert normalize_email("") == ""
        assert normalize_email("   ") == ""
        assert normalize_email(None) == ""  # type: ignore[arg-type]


class TestNormalizeEmailOptional:
    """선택 이메일 필드용 — 빈 값은 None 으로 접는다."""

    def test_none_stays_none(self) -> None:
        assert normalize_email_optional(None) is None

    @pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
    def test_blank_becomes_none(self, raw: str) -> None:
        """빈 문자열을 그대로 저장하면 '이메일 없음'과 구분이 안 된다."""
        assert normalize_email_optional(raw) is None

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("User@Example.com", "user@example.com"),
            ("  USER@EXAMPLE.COM  ", "user@example.com"),
            ("user@example.com", "user@example.com"),
        ],
    )
    def test_normalizes(self, raw: str, expected: str) -> None:
        assert normalize_email_optional(raw) == expected
