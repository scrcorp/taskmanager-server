"""웹 푸시 관련 요청/응답 스키마."""

from typing import Optional

from pydantic import BaseModel, Field


class PushConfigResponse(BaseModel):
    """앱이 구독을 만들기 전에 받아가는 설정.

    공개키를 빌드에 박지 않고 서버가 내려주는 이유:
    구독은 "구독할 때 쓴 공개키" 에 묶인다. 짝이 안 맞는 개인키로 발송하면
    에러 없이 조용히 배달되지 않는다. 앱이 항상 자기가 대화하는 서버의 키를
    받도록 하면 이 불일치가 구조적으로 불가능해진다.
    """

    enabled: bool = Field(description="서버에 VAPID 키가 설정되어 푸시를 쓸 수 있는지")
    vapid_public_key: str = Field(description="applicationServerKey (base64url). 비활성이면 빈 문자열")


class PushSubscriptionKeys(BaseModel):
    """브라우저가 발급한 페이로드 암호화 재료."""

    p256dh: str = Field(min_length=1, max_length=255)
    auth: str = Field(min_length=1, max_length=255)


class PushSubscribeRequest(BaseModel):
    """구독 등록/갱신 — 브라우저 PushSubscription 을 그대로 올린다."""

    endpoint: str = Field(min_length=1, description="푸시 중계 URL. 기기 식별자 역할")
    keys: PushSubscriptionKeys
    user_agent: Optional[str] = Field(default=None, max_length=500)


class PushUnsubscribeRequest(BaseModel):
    """구독 해지 — endpoint 로 지목한다."""

    endpoint: str = Field(min_length=1)


class PushSubscriptionResponse(BaseModel):
    """등록 결과."""

    subscribed: bool
    device_count: int = Field(description="이 사용자가 현재 등록해 둔 기기 수")


class PushTestRequest(BaseModel):
    """개발용 테스트 발송."""

    title: str = Field(default="HTM test", max_length=100)
    body: str = Field(default="Push is working.", max_length=300)


class PushTestResponse(BaseModel):
    """테스트 발송 집계."""

    attempted: int
    sent: int
    failed: int
    removed: int
    errors: list[str]
