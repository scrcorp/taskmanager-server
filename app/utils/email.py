"""이메일 발송 유틸리티 — Brevo SMTP (aiosmtplib).

SMTP 설정은 config.py의 SMTP_* 환경 변수로 관리.

**모든 발송이 이 함수 하나를 지난다.** 그래서 비-prod 안전장치(수신자 리다이렉트/
차단)도 여기서 딱 한 번 적용한다 — 호출부마다 환경을 따지게 하면 새 기능이 추가될
때마다 빠뜨린다. 판단 로직 자체는 `email_guard.resolve_email_route` (pure).
"""

import logging
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosmtplib

from app.config import settings
from app.utils.email_guard import resolve_email_route

logger = logging.getLogger(__name__)


async def send_email(
    to: str,
    subject: str,
    html: str,
    text: str | None = None,
    attachments: list[tuple[str, bytes]] | None = None,
) -> bool:
    """이메일 발송.

    Returns:
        실제로 SMTP 로 내보냈으면 True, 가드가 막았으면 False.
        **반환값을 무시하지 말 것** — 가드는 조용히 return 하므로, 호출부가 예외만
        보고 성공을 판단하면 "보냈다고 기록됐는데 실제로는 안 나간" 상태가 된다.
        (실제로 schedule 일일 보고서에서 그 일이 있었다.)

    Args:
        to: 수신자 이메일 주소
        subject: 제목
        html: HTML 본문
        text: 플레인텍스트 본문 (없으면 html에서 자동 생략)
        attachments: 첨부파일 목록 [(filename, data), ...]

    비-prod 에서는 수신자가 EMAIL_REDIRECT_TO 로 바뀌거나(제목에 원래 수신자 표기),
    그 값이 없으면 아예 발송하지 않는다. 자세한 규칙은 email_guard 참조.
    """
    route = resolve_email_route(
        to=to,
        subject=subject,
        app_env=settings.APP_ENV,
        redirect_to=settings.EMAIL_REDIRECT_TO,
        send_real=settings.EMAIL_SEND_REAL,
    )
    if not route.send:
        # 조용한 실패 금지 — 왜 안 나갔는지 로그로 분명히 남긴다.
        if route.reason == "blocked_no_redirect":
            logger.warning(
                "email blocked (APP_ENV=%s is not production and EMAIL_REDIRECT_TO is "
                "empty): would have sent %r to %s. Set EMAIL_REDIRECT_TO to receive it, "
                "or EMAIL_SEND_REAL=true to send to real recipients.",
                settings.APP_ENV,
                subject,
                to,
            )
        else:
            logger.warning("email not sent (%s): %r", route.reason, subject)
        return False

    if route.redirected:
        logger.info("email redirected to %s (original: %s)", route.to, to)

    msg = MIMEMultipart("mixed")
    msg["Subject"] = route.subject
    msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
    msg["To"] = route.to

    body = MIMEMultipart("alternative")
    if text:
        body.attach(MIMEText(text, "plain", "utf-8"))
    body.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(body)

    for filename, data in (attachments or []):
        part = MIMEApplication(data, Name=filename)
        part["Content-Disposition"] = f'attachment; filename="{filename}"'
        msg.attach(part)

    await aiosmtplib.send(
        msg,
        hostname=settings.SMTP_HOST,
        port=settings.SMTP_PORT,
        username=settings.SMTP_USER,
        password=settings.SMTP_PASSWORD,
        start_tls=True,
    )
    return True
