"""Issue notification recipients — console/app 라우터가 공유하는 조회 로직.

두 라우터가 완전히 같은 body 를 내려야 하므로(front_contract) 한 곳에 둔다.
"""
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import check_store_access
from app.core.error_codes.reports import (
    ISSUE_RECIPIENTS_STORE_MISMATCH,
    ISSUE_RECIPIENTS_TARGET_REQUIRED,
    ISSUE_RECIPIENTS_UNAVAILABLE,
)
from app.models.user import User
from app.services.report_service import report_service


async def resolve_issue_recipients(
    db: AsyncSession,
    current_user: User,
    *,
    store_id: UUID | None,
    report_id: UUID | None,
) -> dict:
    """store_id(작성 화면) 또는 report_id(상세 화면) 기준 수신자 목록.

    - 둘 다 없으면 400
    - report_id 가 오면 조회권 검사(assert_can_view) 후 그 리포트의 매장을 쓴다
    - store_id 와 report.store_id 가 다르면 400 (조용히 한쪽을 고르지 않는다)
    """
    if store_id is None and report_id is None:
        raise ISSUE_RECIPIENTS_TARGET_REQUIRED()

    report = None
    if report_id is not None:
        report = await report_service.get_report(
            db, report_id, current_user.organization_id
        )
        if report.type != "issue" or report.store_id is None:
            raise ISSUE_RECIPIENTS_UNAVAILABLE(report_type=report.type)
        if store_id is not None and report.store_id != store_id:
            raise ISSUE_RECIPIENTS_STORE_MISMATCH(
                report_store_id=str(report.store_id)
            )
        store_id = report.store_id

    await check_store_access(db, current_user, store_id)
    if report is not None:
        await report_service.assert_can_view(db, current_user, report)

    items = await report_service.list_issue_recipients(
        db, viewer=current_user, store_id=store_id, report=report
    )
    return {
        "store_id": str(store_id),
        "report_id": str(report_id) if report_id else None,
        "items": items,
    }


async def resolve_issue_expected_viewers(
    db: AsyncSession,
    current_user: User,
    *,
    store_id: UUID | None,
    report_id: UUID | None,
    scope: str,
    extra_user_ids: list[UUID] | None,
) -> dict:
    """선택한 조회 범위(scope)에서 실제로 볼 수 있게 되는 사람 미리보기.

    타깃 해석(store_id / report_id / 조회권 검사)은 수신자 목록과 완전히 같은 규칙이다.
    """
    if store_id is None and report_id is None:
        raise ISSUE_RECIPIENTS_TARGET_REQUIRED()

    report = None
    if report_id is not None:
        report = await report_service.get_report(
            db, report_id, current_user.organization_id
        )
        if report.type != "issue" or report.store_id is None:
            raise ISSUE_RECIPIENTS_UNAVAILABLE(report_type=report.type)
        if store_id is not None and report.store_id != store_id:
            raise ISSUE_RECIPIENTS_STORE_MISMATCH(
                report_store_id=str(report.store_id)
            )
        store_id = report.store_id

    await check_store_access(db, current_user, store_id)
    if report is not None:
        await report_service.assert_can_view(db, current_user, report)

    return await report_service.list_issue_expected_viewers(
        db,
        viewer=current_user,
        store_id=store_id,
        scope=scope,
        report=report,
        extra_user_ids=extra_user_ids,
    )
