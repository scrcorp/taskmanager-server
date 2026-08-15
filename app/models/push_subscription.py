"""Push Subscription 모델 — 웹 푸시 구독(기기) 등록.

Web push subscription registry — one row per browser/device that agreed to
receive push notifications.

구독은 "사용자" 가 아니라 "기기 안의 브라우저" 단위다. 한 사용자가 폰·태블릿에
각각 설치하면 행이 2개 생기고, 발송 시 둘 다에 쏜다.

수명 주기 (중요 — 구독은 수시로 끊긴다):
    1. 앱에서 알림 권한 허용 → 브라우저가 endpoint + 키 발급 → POST 로 여기 저장
    2. 발송: endpoint 로 HTTPS POST (본문은 p256dh/auth 로 암호화, VAPID 서명)
    3. 사용자가 홈 화면 아이콘 삭제 / 브라우저 권한 차단 / 기기 교체
       → **서버는 통보받지 못한다.** 두 경로로만 알게 된다:
          a) 앱 시작 시 reconcile — 실제 구독과 대조해 없으면 삭제
          b) 발송 시 404/410 응답 — 죽은 구독이므로 행 삭제
    4. 다시 허용하면 **새 endpoint** 가 발급된다(옛 것은 되살아나지 않는다)

`endpoint` 가 사실상의 기기 식별자다. 같은 브라우저가 재구독하면 endpoint 가
바뀌므로 새 행이 되고, 옛 행은 위 3-a/3-b 로 정리된다.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PushSubscription(Base):
    """웹 푸시 구독 1건 = 기기 1대.

    Attributes:
        id: 구독 고유 식별자 (PK, UUID)
        organization_id: 소속 조직 FK (멀티테넌트 격리)
        user_id: 구독 소유자 FK
        endpoint: 브라우저가 발급한 푸시 중계 URL. 사실상 기기 식별자라 UNIQUE
        p256dh: 페이로드 암호화용 공개키 (base64url)
        auth: 페이로드 암호화용 인증 시크릿 (base64url)
        user_agent: 어느 기기인지 사람이 알아보기 위한 힌트 (진단용)
        failure_count: 연속 발송 실패 횟수 (일시적 오류 누적 — 404/410 은 즉시 삭제)
        created_at: 구독 등록 시각
        last_seen_at: 앱이 마지막으로 이 구독을 확인해 준 시각 (reconcile)
        last_success_at: 마지막으로 발송에 성공한 시각
    """

    __tablename__ = "push_subscriptions"

    # 구독 고유 식별자 — Subscription PK (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — 조직 삭제 시 구독도 삭제
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # 구독 소유자 FK — 사용자 삭제 시 구독도 삭제
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # 푸시 중계 URL (구글/애플/모질라). 길이가 길고 가변적이라 Text.
    # UNIQUE — 같은 endpoint 가 두 번 저장되면 같은 기기에 알림이 두 번 간다.
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # 페이로드 암호화 재료 — 브라우저가 구독 시 함께 발급
    p256dh: Mapped[str] = mapped_column(String(255), nullable=False)
    auth: Mapped[str] = mapped_column(String(255), nullable=False)

    # 어느 기기인지 구분하기 위한 진단용 힌트 (신뢰할 수 있는 값은 아님)
    user_agent: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # 연속 실패 횟수 — 일시적 오류가 계속 쌓이면 정리 대상으로 본다.
    # (404/410 은 확정 사망이라 카운트하지 않고 즉시 행을 지운다)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    # 앱이 시작될 때 "이 구독 아직 살아있다" 고 확인해 준 시각
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    last_success_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # 발송 시 "이 사용자의 모든 기기" 를 훑는 게 가장 잦은 조회다.
    # 인덱스는 모델에 선언해야 alembic autogenerate 가 지우려 들지 않는다.
    __table_args__ = (
        Index("ix_push_subscriptions_user_id", "user_id"),
        Index("ix_push_subscriptions_organization_id", "organization_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 디버깅 편의
        return f"<PushSubscription user={self.user_id} endpoint={self.endpoint[:40]}...>"
