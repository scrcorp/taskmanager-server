"""비-prod 이메일 라우팅 결정 — pure helper.

배경: 이 시스템의 이메일 수신자는 대부분 DB(`User.email`)에서 나온다. 그런데
worktree/로컬 DB 는 dev 복사본이라 **실제 사람들의 주소**가 들어있고 `.env` 의
SMTP 자격증명은 진짜다. 그래서 로컬에서 기능 하나만 눌러도 (조기 출근 알림처럼
권한 보유자 전원에게 fan-out 하는 경우엔 특히) 진짜 받은편지함으로 나간다.

2026-03 첫 이메일 기능(일일보고)은 수신자가 `REPORT_NOTIFICATION_EMAIL` env 하나
뿐이라 로컬에서는 자연히 내 주소로만 갔다. 2026-05 부터 수신자를 DB 에서 읽는
메일이 늘면서 그 성질이 조용히 사라졌다 — 이 모듈이 그 자리를 대신한다.

**기본이 차단이다.** 설정을 깜빡한 새 worktree 에서도 사고가 나지 않아야 하므로,
비-prod 인데 EMAIL_REDIRECT_TO 가 비어 있으면 보내지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

# APP_ENV 가 이 값들 중 하나면 운영 — 가드를 적용하지 않는다.
PRODUCTION_ENVS = frozenset({"production", "prod"})


@dataclass(frozen=True)
class EmailRoute:
    """이 메일을 실제로 어디로 보낼지에 대한 결정."""

    send: bool
    to: str | None
    subject: str
    # 로그/테스트용 사유 코드. 사람이 읽는 문장이 아니라 분기 이름이다.
    reason: str

    @property
    def redirected(self) -> bool:
        return self.reason == "redirected"


def resolve_email_route(
    *,
    to: str,
    subject: str,
    app_env: str,
    redirect_to: str,
    send_real: bool,
) -> EmailRoute:
    """수신자/제목을 환경 정책에 맞게 바꾼 결과를 돌려준다.

    분기:
      - production            → 그대로 발송 ("production")
      - send_real=True        → 그대로 발송 ("send_real_override")
      - redirect_to 설정됨    → 그 주소로 발송 + 제목에 원래 수신자 표기 ("redirected")
      - 그 외                 → 발송 안 함 ("blocked_no_redirect")
      - 수신자 없음           → 발송 안 함 ("no_recipient")
    """
    recipient = (to or "").strip()
    if not recipient:
        return EmailRoute(send=False, to=None, subject=subject, reason="no_recipient")

    if (app_env or "").strip().lower() in PRODUCTION_ENVS:
        return EmailRoute(send=True, to=recipient, subject=subject, reason="production")

    if send_real:
        return EmailRoute(
            send=True, to=recipient, subject=subject, reason="send_real_override"
        )

    target = (redirect_to or "").strip()
    if target:
        # 원래 수신자를 제목 앞에 남긴다 — 여러 명에게 갈 메일이 한 받은편지함에
        # 모이므로, 누구에게 갈 메일이었는지 제목만 보고 구분돼야 한다.
        return EmailRoute(
            send=True,
            to=target,
            subject=f"[to: {recipient}] {subject}",
            reason="redirected",
        )

    return EmailRoute(
        send=False, to=None, subject=subject, reason="blocked_no_redirect"
    )
