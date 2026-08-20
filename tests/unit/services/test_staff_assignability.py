"""배정 가능 판정 — 고용 상태 축 (2026-08-19).

이 판정이 갈리면 "화면에선 눌리는데 저장이 안 되는" 상태가 된다. 규칙은
`staff_assignment_service` 한 곳이 소유하고, 검증·API 가 그걸 함께 읽는다.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

from app.core import schedule_codes as codes
from app.services.staff_assignment_service import (
    Assignability,
    _judge,
    blocking_issue,
    blocking_message,
)

TODAY = date(2026, 8, 19)
LEFT_ON = date(2026, 8, 10)


def _j(**over):
    base = dict(
        user_id=uuid4(),
        is_active=True,
        is_provisional=False,
        member_status="active",
        termination_date=None,
    )
    base.update(over)
    return _judge(**base)


class TestJudge:
    def test_active_user_has_no_limit(self):
        a = _j()
        assert a.employed and a.assignable_until is None
        assert a.allows(TODAY)

    def test_provisional_is_assignable_even_though_inactive(self):
        """유령(미가입)은 is_active=False 지만 '앞으로 일할 사람' 이다."""
        a = _j(is_active=False, is_provisional=True)
        assert a.employed and a.assignable_until is None

    def test_terminated_allows_up_to_and_including_last_working_day(self):
        a = _j(is_active=False, member_status="terminated", termination_date=LEFT_ON)
        assert a.assignable_until == LEFT_ON
        assert a.allows(LEFT_ON - timedelta(days=1))
        assert a.allows(LEFT_ON)                       # 퇴사일 당일까지는 근무로 본다
        assert not a.allows(LEFT_ON + timedelta(days=1))

    def test_terminated_member_still_active_account_uses_the_narrower_rule(self):
        """한쪽만 바뀐 데이터가 있어도 좁은 쪽(퇴사일)을 택한다 — fail-closed."""
        a = _j(is_active=True, member_status="terminated", termination_date=LEFT_ON)
        assert a.assignable_until == LEFT_ON
        assert not a.allows(TODAY)

    def test_inactive_without_termination_date_is_blocked_everywhere(self):
        """일반 삭제/토글로 비활성된 사람 — 판정 기준이 없으므로 전 날짜 차단 (D1-a)."""
        a = _j(is_active=False)
        assert not a.employed
        assert not a.allows(LEFT_ON - timedelta(days=365))
        assert not a.allows(TODAY)

    def test_no_membership_row_falls_back_to_account_flag(self):
        assert _j(member_status=None).allows(TODAY)
        assert not _j(is_active=False, member_status=None).allows(TODAY)


class TestBlockingIssue:
    def test_passes_through_when_allowed(self):
        assert blocking_issue(_j(), TODAY) is None

    def test_terminated_reports_both_dates(self):
        a = _j(is_active=False, member_status="terminated", termination_date=LEFT_ON)
        issue = blocking_issue(a, TODAY)
        assert issue["code"] == codes.USER_TERMINATED_BEFORE_DATE
        assert issue["params"]["termination_date"] == LEFT_ON.isoformat()
        assert issue["params"]["operating_day"] == TODAY.isoformat()

    def test_not_employed_reports_plain_code(self):
        issue = blocking_issue(_j(is_active=False), TODAY)
        assert issue["code"] == codes.USER_NOT_EMPLOYED

    def test_codes_are_errors_not_warnings(self):
        """확인(force)으로 뚫리면 안 된다 — 에러 집합에 있어야 한다."""
        assert codes.USER_NOT_EMPLOYED in codes.ERROR_CODES
        assert codes.USER_TERMINATED_BEFORE_DATE in codes.ERROR_CODES
        assert codes.USER_NOT_EMPLOYED not in codes.WARNING_CODES
        assert codes.USER_TERMINATED_BEFORE_DATE not in codes.WARNING_CODES

    def test_unknown_user_is_blocked(self):
        a = Assignability(user_id=uuid4(), employed=False, assignable_until=None)
        assert blocking_issue(a, TODAY)["code"] == codes.USER_NOT_EMPLOYED


class TestBlockingMessage:
    """스케줄 코드 체계를 안 쓰는 도메인(팁 등)용 문장 — 문구를 도메인마다 새로 쓰지 않는다."""

    def test_none_while_allowed(self):
        assert blocking_message(_j(), TODAY) is None

    def test_names_the_last_working_day(self):
        a = _j(is_active=False, member_status="terminated", termination_date=LEFT_ON)
        msg = blocking_message(a, TODAY)
        assert LEFT_ON.isoformat() in msg and TODAY.isoformat() in msg

    def test_plain_sentence_without_a_date(self):
        assert "no longer active" in blocking_message(_j(is_active=False), TODAY)
