"""스케줄 일일 리포트 — 수동 트리거 (Owner 전용).

Cron이 자동 발송하지만 미리보기/긴급 발송용 admin endpoint.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.error_codes.common import FORBIDDEN
from app.core.permissions import is_owner
from app.database import get_db
from app.models.user import User
from app.services.schedule_report_service import (
    _resolve_recipients,
    generate_and_send_report,
    resend_last_report,
)

router: APIRouter = APIRouter()


@router.post("/run")
async def trigger_report(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    dry_run: bool = Query(False, description="True면 발송 안 함, HTML 미리보기만"),
    to: str | None = Query(None, description="콤마 구분 수신자 override (비어있으면 .env 사용)"),
) -> dict:
    """Owner 전용 — 보고서 즉시 생성 + (옵션) 발송.

    **이 엔드포인트는 스냅샷(diff 베이스라인)을 절대 쓰지 않는다.**
    베이스라인을 쓰는 주체는 15:00 크론 하나뿐이어야 한다. 예전엔 수동 발송이
    스냅샷을 남겨서, 오너가 오전에 포맷 확인차 한 번 눌러보면 그날 15:00 리포트의
    NEW 배지가 전부 사라졌다 — 그리고 그렇게 됐다는 힌트가 어디에도 없었다.
    """
    if not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Owner only")

    override = [t.strip() for t in to.split(",") if t.strip()] if to else None

    result = await generate_and_send_report(
        db,
        current_user.organization_id,
        save_snapshot=False,
        override_recipients=[] if dry_run else override,
    )
    # html 은 미리보기일 때만 포함
    if not dry_run:
        result.pop("html", None)
    return result


@router.post("/resend")
async def resend_report(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    to: str | None = Query(None, description="콤마 구분 수신자. 비우면 설정된 수신자"),
) -> dict:
    """Owner 전용 — **마지막으로 저장된** 리포트를 그대로 다시 보낸다.

    `/run` 과의 차이가 요점이다. `/run` 은 지금 시점으로 새로 만든다 — today 가 밀려
    있거나 그 사이 스케줄이 바뀌었으면 **다른 문서**가 나온다. 발송만 실패한 상황에서
    필요한 건 "그때 그것" 이므로, 이 경로는 저장된 재료로 같은 문서를 재현해 보낸다.

    스냅샷은 쓰지 않는다 — diff 기준선을 움직이는 주체는 크론뿐이다.
    """
    if not is_owner(current_user):
        raise FORBIDDEN()

    override = [t.strip() for t in to.split(",") if t.strip()] if to else None
    recipients = (
        override
        if override is not None
        else await _resolve_recipients(db, current_user.organization_id)
    )

    return await resend_last_report(
        db, current_user.organization_id, recipients=recipients
    )


@router.get("/preview", response_class=Response)
async def preview_report_html(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Response:
    """Owner 전용 — 이메일 본문 HTML 미리보기 (브라우저에서 직접 확인)."""
    if not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Owner only")

    result = await generate_and_send_report(
        db,
        current_user.organization_id,
        save_snapshot=False,
        override_recipients=[],
    )
    return Response(content=result["html"], media_type="text/html")


# 샘플 미리보기는 API 가 아니라 temp/preview_sections.py 스크립트로 처리.
# 실행: cd server && python temp/preview_sections.py → temp/preview_sections.html
