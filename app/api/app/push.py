"""앱 웹 푸시 라우터 — 구독 등록/해지 및 설정 조회.

구독은 사용자가 아니라 **기기(브라우저)** 단위다. 한 사용자가 여러 행을 가진다.

앱은 시작할 때마다 이 API 로 서버 상태를 실제 브라우저 상태에 맞춘다(reconcile).
서버는 구독이 끊긴 걸 통보받지 못하기 때문에, 앱이 알려주는 것 + 발송 실패가
유일한 정보원이다.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.config import settings
from app.core.error_codes.push import PUSH_NOT_CONFIGURED
from app.database import get_db
from app.models.push_subscription import PushSubscription
from app.models.user import User
from app.schemas.push import (
    PushConfigResponse,
    PushSubscribeRequest,
    PushSubscriptionResponse,
    PushTestRequest,
    PushTestResponse,
    PushUnsubscribeRequest,
)
from app.services.push_service import push_service

logger = logging.getLogger(__name__)

router: APIRouter = APIRouter()


async def _device_count(db: AsyncSession, user_id) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(PushSubscription)
        .where(PushSubscription.user_id == user_id)
    )
    return int(result.scalar_one())


@router.get("/config", response_model=PushConfigResponse)
async def get_push_config(
    current_user: Annotated[User, Depends(get_current_user)],
) -> PushConfigResponse:
    """구독 생성에 필요한 공개키를 내려준다.

    공개키는 비밀이 아니다 — 어차피 클라이언트에 노출되는 값이다.
    """
    return PushConfigResponse(
        enabled=settings.push_enabled,
        vapid_public_key=settings.VAPID_PUBLIC_KEY if settings.push_enabled else "",
    )


@router.post("/subscribe", response_model=PushSubscriptionResponse)
async def subscribe(
    payload: PushSubscribeRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PushSubscriptionResponse:
    """구독 등록 — 이미 있는 endpoint 면 소유자/갱신 시각만 업데이트한다.

    같은 endpoint 가 다른 사용자로 넘어오는 경우가 실제로 있다(공용 단말에서
    로그아웃 후 다른 사람이 로그인). endpoint 는 기기를 가리키므로 소유자를
    새 사용자로 옮긴다 — 안 그러면 이전 사용자의 알림이 남의 기기로 간다.
    """
    if not settings.push_enabled:
        raise PUSH_NOT_CONFIGURED()

    existing = await db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == payload.endpoint)
    )
    subscription = existing.scalar_one_or_none()

    if subscription is None:
        subscription = PushSubscription(
            organization_id=current_user.organization_id,
            user_id=current_user.id,
            endpoint=payload.endpoint,
            p256dh=payload.keys.p256dh,
            auth=payload.keys.auth,
            user_agent=payload.user_agent,
        )
        db.add(subscription)
    else:
        # 기기 주인이 바뀌었거나 키가 갱신된 경우를 흡수한다.
        subscription.organization_id = current_user.organization_id
        subscription.user_id = current_user.id
        subscription.p256dh = payload.keys.p256dh
        subscription.auth = payload.keys.auth
        subscription.user_agent = payload.user_agent
        subscription.failure_count = 0
        subscription.last_seen_at = func.now()

    await db.commit()
    return PushSubscriptionResponse(
        subscribed=True, device_count=await _device_count(db, current_user.id)
    )


@router.post("/unsubscribe", response_model=PushSubscriptionResponse)
async def unsubscribe(
    payload: PushUnsubscribeRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PushSubscriptionResponse:
    """구독 해지 — 본인 소유 endpoint 만 지운다.

    없는 endpoint 여도 성공으로 본다(이미 정리된 상태 = 원하는 결과).
    """
    await db.execute(
        delete(PushSubscription)
        .where(PushSubscription.endpoint == payload.endpoint)
        .where(PushSubscription.user_id == current_user.id)
    )
    await db.commit()
    return PushSubscriptionResponse(
        subscribed=False, device_count=await _device_count(db, current_user.id)
    )


async def send_test_push(
    payload: PushTestRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PushTestResponse:
    """본인에게 테스트 푸시를 보낸다 — 개발/진단용.

    prod 에서는 아래에서 라우트 자체를 등록하지 않는다(404 를 던지는 것보다
    엔드포인트가 존재하지 않는 편이 낫다 — 스캐너에 흔적도 남지 않는다).
    """
    result = await push_service.send_to_user(
        db,
        current_user.id,
        title=payload.title,
        body=payload.body,
        url="/",
        tag="test",
    )
    await db.commit()
    return PushTestResponse(
        attempted=result.attempted,
        sent=result.sent,
        failed=result.failed,
        removed=result.removed,
        errors=result.errors,
    )


# 테스트 발송은 개발/진단 도구다. prod 에는 라우트를 만들지 않는다.
if settings.APP_ENV != "production":
    router.add_api_route(
        "/test",
        send_test_push,
        methods=["POST"],
        response_model=PushTestResponse,
        tags=["My Push"],
    )
