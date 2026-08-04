"""관리자 EMPID 임포트 라우터 — 레거시 사번 파일 preview/commit.

Console EMPID Import Router — legacy roster upload for per-store empid.
콘솔 /users/bulk/empid 탭이 호출. org_member_stores.empid 에만 기록하며
users.employee_no(폐기 방향)는 건드리지 않는다.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.empid_import import (
    EmpidImportCommitRequest,
    EmpidImportCommitResponse,
    EmpidImportEntry,
    EmpidImportPerson,
    EmpidImportPreviewResponse,
    EmpidRosterStore,
)
from app.services import empid_import_service as svc
from app.utils.exceptions import BadRequestError

router: APIRouter = APIRouter()

# 업로드 상한 — 직원 명부는 작다. 통짜 read 로 인한 메모리 폭주 방지 (10 MB).
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _person_out(p: svc.PersonRow) -> EmpidImportPerson:
    """서비스 dataclass → 응답 스키마."""
    return EmpidImportPerson(
        email=p.email,
        name=p.name,
        user_id=p.user_id,
        user_full_name=p.user_full_name,
        entries=[EmpidImportEntry(**e.__dict__) for e in p.entries],
        note=p.note,
        similar=p.similar,
        members=p.members,
        similar_users=p.similar_users,
    )


@router.post("/preview", response_model=EmpidImportPreviewResponse)
async def preview_empid_import(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:update"))],
    file: UploadFile = File(...),
) -> EmpidImportPreviewResponse:
    """업로드 파일(xlsx/csv)을 파싱해 사람×매장 버킷을 반환합니다 (DB 기록 없음).

    컬럼: COMPANY, CORP_ABR_3, Name, emp_id, Email. PURADAK 행 제외.
    """
    if not file.filename:
        raise BadRequestError("File required")
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ("csv", "xlsx"):
        raise BadRequestError("CSV or Excel (.xlsx) file required")
    if file.size and file.size > MAX_UPLOAD_BYTES:
        raise BadRequestError("File too large (max 10 MB)")

    # read(size) 로 상한+1 만 읽어 초과 여부 판정 — 초대형 업로드도 메모리에 통째로 안 올라감.
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise BadRequestError("File too large (max 10 MB)")
    result = await svc.preview(db, current_user.organization_id, content, file.filename)
    return EmpidImportPreviewResponse(
        people=[_person_out(p) for p in result.people],
        placeholder=[_person_out(p) for p in result.placeholder],
        deferred=[_person_out(p) for p in result.deferred],
        counts=result.counts(),
        excluded_rows=result.excluded_rows,
        total_rows=result.total_rows,
    )


@router.get("/template")
async def download_empid_template(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:read"))],
    mode: str = "blank",
    stores: str | None = None,
    people: str = "all",
    include_dormant: bool = True,
    include_email: bool = True,
    include_numbers: bool = True,
):
    """임포트용 xlsx 다운로드 — mode=blank(빈 템플릿) | current(현황 export).

    current 필터: stores(콤마구분 UUID, 생략=전체) / people(all|numbered|unnumbered) /
    include_dormant / include_email(끄면 재업로드 매칭 불가) / include_numbers(끄면 작성용 양식).
    """
    from fastapi.responses import Response

    if people not in ("all", "numbered", "unnumbered"):
        raise BadRequestError("people must be all|numbered|unnumbered")
    store_ids: set[UUID] | None = None
    if stores:
        try:
            store_ids = {UUID(s) for s in stores.split(",") if s.strip()}
        except ValueError:
            raise BadRequestError("stores must be comma-separated UUIDs")

    prefill = mode == "current"
    content = await svc.build_template_xlsx(
        db, current_user.organization_id, prefill,
        store_ids=store_ids, people=people,
        include_dormant=include_dormant, include_email=include_email,
        include_numbers=include_numbers,
    )
    filename = "empid_export.xlsx" if prefill else "empid_import_template.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/roster", response_model=list[EmpidRosterStore])
async def get_empid_roster(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:read"))],
) -> list[EmpidRosterStore]:
    """매장별 배정·empid 현황 — 파일 없이 직접 편집하는 bulk 에디터용."""
    rows = await svc.roster(db, current_user.organization_id)
    return [EmpidRosterStore(**r) for r in rows]


@router.post("/commit", response_model=EmpidImportCommitResponse)
async def commit_empid_import(
    data: EmpidImportCommitRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:update"))],
) -> EmpidImportCommitResponse:
    """체크된 (user, store, empid) 목록을 매장 단위 3-phase 로 반영합니다 (멱등).

    empid=null 은 번호 삭제(배정 행 유지). 임포트 탭·bulk 에디터·스태프 상세가 공용.
    """
    assignments: list[tuple[UUID, UUID, int | None]] = []
    for item in data.assignments:
        try:
            assignments.append((UUID(item.user_id), UUID(item.store_id), item.empid))
        except ValueError:
            continue  # 잘못된 UUID 는 무시 (프론트 방어)
    result = await svc.commit(db, current_user.organization_id, assignments)
    return EmpidImportCommitResponse(
        applied=result.applied,
        renumbered=result.renumbered,
        skipped=result.skipped,
        rejected=result.rejected,
    )
