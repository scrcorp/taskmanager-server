"""인터뷰 이메일 템플릿 (#2 Phase 3) — pure builder 검증."""

from app.utils.email_templates import (
    build_interview_cancellation_email,
    build_interview_confirmation_email,
    build_interview_interviewer_email,
    build_interview_invite_email,
    build_interview_reschedule_email,
)


def test_invite_email():
    subject, html = build_interview_invite_email(
        "Ivy", "Bean & Brew", "https://console.example.com/interview/tok123", "PDT"
    )
    assert "Bean & Brew" in subject
    assert "/interview/tok123" in html
    assert "Ivy" in html
    assert "PDT" in html


def test_confirmation_email():
    subject, html = build_interview_confirmation_email(
        "Ivy", "Bean & Brew", "Mon, Jul 6 · 10:00 AM PDT", "Mina Park"
    )
    assert "Bean & Brew" in subject
    assert "Mon, Jul 6" in html
    assert "Mina Park" in html


def test_confirmation_email_no_interviewer():
    subject, html = build_interview_confirmation_email("Ivy", "Bean & Brew", "Mon, Jul 6 · 10:00 AM PDT")
    assert "confirmed" in subject.lower()
    assert "Mon, Jul 6" in html


def test_reschedule_email():
    subject, html = build_interview_reschedule_email(
        "Ivy", "Bean & Brew", "Tue, Jul 7 · 2:00 PM PDT", "Mina Park"
    )
    # 확정이 아니라 "변경됨" 메일이어야 함
    assert "changed" in subject.lower()
    assert "confirmed" not in subject.lower()
    assert "Tue, Jul 7" in html
    assert "Mina Park" in html
    assert "New time" in html


def test_cancellation_email_with_time():
    subject, html = build_interview_cancellation_email("Ivy", "Bean & Brew", "Mon, Jul 6 · 10:00 AM PDT")
    assert "cancelled" in subject.lower()
    assert "Bean & Brew" in subject
    assert "Mon, Jul 6" in html


def test_cancellation_email_without_time():
    subject, html = build_interview_cancellation_email("Ivy", "Bean & Brew")
    assert "cancelled" in subject.lower()
    assert "Ivy" in html


def test_interviewer_email_includes_role():
    subject, html = build_interview_interviewer_email(
        "Ivy", "Bean & Brew", "Mina Park (GM)", "Mon, Jul 6 · 10:00 AM PDT"
    )
    assert "interviewer" in subject.lower()
    assert "Mina Park (GM)" in html
    assert "Mon, Jul 6" in html


def test_interviewer_email_without_time():
    subject, html = build_interview_interviewer_email("Ivy", "Bean & Brew", "Sam Lee (Owner)")
    assert "Sam Lee (Owner)" in html
