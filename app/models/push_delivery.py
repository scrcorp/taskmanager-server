"""Push Delivery 모델 — 푸시 발송 시도 기록.

왜 필요한가:
    alert_preference_audits 가 "당신이 껐다" 를 증명한다면, 이 테이블은
    **"우리는 보냈다"** 를 증명한다. 둘이 있어야 "그 알림 왜 확인 안 했냐"
    분쟁에서 양쪽 사실이 다 남는다.

    지금까지 발송 결과는 로그에만 남아서, 나중에 "그날 그 사람에게 알림이
    갔었나?" 를 조회할 수 없었다.

무엇을 기록하고 무엇을 기록하지 않는가:
    기록하는 것 — 우리가 중계 서버(FCM/APNs)에 요청을 보냈고 그쪽이 수락했는지.
    기록하지 **않는** 것 — 사용자가 실제로 봤는지. 알 방법이 없다.
    중계 서버가 201 을 줘도 폰에서 OS 알림을 꺼놨으면 화면에 안 뜬다.
    그 차이를 오해하지 않도록 상태값 이름을 'accepted' 로 둔다("delivered" 아님).

발송 1건 = **구독(기기) 1대에 대한 시도** 1행. 한 사용자에게 폰+태블릿이
있으면 알림 하나에 2행이 남는다 — 어느 기기가 실패했는지 알아야 하기 때문이다.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# 발송 결과 상태.
#   accepted : 중계 서버가 요청을 수락했다. **표시됐다는 뜻은 아니다.**
#   gone     : 404/410 — 구독이 죽어 있었다. 해당 구독 행은 삭제된다.
#   failed   : 그 외 오류(네트워크, 5xx 등). 일시적일 수 있다.
#   skipped  : 보내지 않았다. 사유는 skip_reason 에 남는다.
DELIVERY_ACCEPTED = "accepted"
DELIVERY_GONE = "gone"
DELIVERY_FAILED = "failed"
DELIVERY_SKIPPED = "skipped"

# skipped 사유
SKIP_PREFERENCE_OFF = "preference_off"   # 사용자가 그 카테고리 푸시를 꺼둠
SKIP_NO_SUBSCRIPTION = "no_subscription"  # 등록된 기기가 없음
SKIP_PUSH_DISABLED = "push_disabled"      # 서버에 VAPID 키가 없음


class PushDelivery(Base):
    """푸시 발송 시도 1건 (기기 단위).

    Attributes:
        id: 기록 고유 식별자 (PK, UUID)
        organization_id: 소속 조직 FK (멀티테넌트 격리)
        user_id: 수신자
        alert_id: 이 발송을 유발한 알림. 테스트 발송 등은 null.
            알림이 지워져도 발송 기록은 남아야 하므로 SET NULL.
        alert_type: 알림 종류 스냅샷. alert 이 지워져도 무엇이었는지 남는다.
        subscription_endpoint: 어느 기기로 보냈는지. 구독 행이 지워져도 남도록
            FK 가 아니라 값으로 스냅샷한다(짧게 자름).
        status: accepted / gone / failed / skipped
        skip_reason: status=skipped 일 때의 사유
        http_status: 중계 서버 응답 코드 (있을 때)
        error: 실패 요약 (진단용)
        title/body: 실제로 보낸 내용 스냅샷 — "무슨 알림이었나" 를 답하려면 필요
        created_at: 시도 시각
    """

    __tablename__ = "push_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # 알림이 삭제돼도 발송 사실은 남는다.
    alert_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("alerts.id", ondelete="SET NULL"), nullable=True
    )
    alert_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # 구독 행이 사라져도(죽은 구독 정리) 어느 기기였는지 남아야 한다 → 값 스냅샷.
    subscription_endpoint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False)
    skip_reason: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    title: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    body: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    # 핵심 조회: "사용자 U 에게 기간 T 동안 무엇이 나갔나"
    __table_args__ = (
        Index("ix_push_deliveries_user_created", "user_id", "created_at"),
        Index("ix_push_deliveries_alert_id", "alert_id"),
        Index("ix_push_deliveries_organization_id", "organization_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 디버깅 편의
        return f"<PushDelivery {self.status} user={self.user_id} type={self.alert_type}>"
