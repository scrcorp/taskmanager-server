"""인터뷰 이메일 템플릿 (#2 Phase 3) — pure builder 검증."""

from app.utils.email_templates import (
    build_interview_confirmation_email,
    build_interview_invite_email,
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
