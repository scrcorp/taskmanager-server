"""스케줄 일일 리포트 — 수동 트리거 (Owner 전용).

Cron이 자동 발송하지만 미리보기/긴급 발송용 admin endpoint.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.permissions import is_owner
from app.database import get_db
from app.models.user import User
from app.services.schedule_report_service import generate_and_send_report

router: APIRouter = APIRouter()


@router.post("/run")
async def trigger_report(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    dry_run: bool = Query(False, description="True면 발송/스냅샷 저장 안 함, HTML 미리보기만"),
    to: str | None = Query(None, description="콤마 구분 수신자 override (비어있으면 .env 사용)"),
) -> dict:
    """Owner 전용 — 보고서 즉시 생성 + (옵션) 발송."""
    if not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Owner only")

    override = [t.strip() for t in to.split(",") if t.strip()] if to else None

    result = await generate_and_send_report(
        db,
        current_user.organization_id,
        save_snapshot=not dry_run,
        override_recipients=[] if dry_run else override,
    )
    # html 은 미리보기일 때만 포함
    if not dry_run:
        result.pop("html", None)
    return result


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
