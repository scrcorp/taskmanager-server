"""이메일 발송 유틸리티 — Brevo SMTP (aiosmtplib).

SMTP 설정은 config.py의 SMTP_* 환경 변수로 관리.
"""

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosmtplib

from app.config import settings


async def send_email(
    to: str,
    subject: str,
    html: str,
    text: str | None = None,
) -> None:
    """이메일 발송.

    Args:
        to: 수신자 이메일 주소
        subject: 제목
        html: HTML 본문
        text: 플레인텍스트 본문 (없으면 html에서 자동 생략)
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
    msg["To"] = to

    if text:
        msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    await aiosmtplib.send(
        msg,
        hostname=settings.SMTP_HOST,
        port=settings.SMTP_PORT,
        username=settings.SMTP_USER,
        password=settings.SMTP_PASSWORD,
        start_tls=True,
    )
