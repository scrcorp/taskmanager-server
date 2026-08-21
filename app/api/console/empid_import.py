"""관리자 EMPID 임포트 라우터 — 레거시 사번 파일 preview/commit.

Console EMPID Import Router — legacy roster upload for per-store empid.
콘솔 /users/bulk/empid 탭이 호출. org_member_stores.empid 에만 기록하며
users.employee_no(폐기 방향)는 건드리지 않는다.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.empid_import import (
    EmpidBandCount,
    EmpidExportRequest,
    EmpidReconciliationScope,
    EmpidSavedAlias,
    EmpidUnmatchedStore,
    EmpidImportCommitRequest,
    EmpidImportCommitResponse,
    EmpidImportEntry,
    EmpidImportPerson,
    EmpidImportPreviewResponse,
    EmpidRosterStore,
)
from app.services import empid_import_service as svc
from app.services.export_naming_service import resolve_store_scope
from app.utils.download import content_disposition, export_filename
from app.core.error_codes.common import INVALID_STORE_OVERRIDES
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
        matched_by=p.matched_by,
    )


@router.post("/preview", response_model=EmpidImportPreviewResponse)
async def preview_empid_import(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:update"))],
    file: UploadFile = File(...),
    store_overrides: Annotated[str | None, Form()] = None,
) -> EmpidImportPreviewResponse:
    """업로드 파일(xlsx/csv)을 파싱해 사람×매장 버킷을 반환합니다 (DB 기록 없음).

    컬럼: COMPANY, CORP_ABR_3(또는 WORKNOTE), Name(또는 FIRST/LAST), emp_id,
    Email(선택), CREWID(선택). PURADAK 행 제외. Email/CREWID 없는 행은 이름으로
    자동 매칭을 시도한다(유일 후보만 — 동명이인은 운영자 선택).

    store_overrides: JSON 오브젝트 문자열 {정규화키: store UUID}. 응답
    unmatched_stores[].key 를 그대로 키로 쓰면 된다 (오탈자 매장 코드 수동 매핑).
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
    overrides: dict[str, str] | None = None
    if store_overrides:
        import json

        try:
            parsed = json.loads(store_overrides)
        except ValueError:
            raise INVALID_STORE_OVERRIDES()
        if not isinstance(parsed, dict):
            raise INVALID_STORE_OVERRIDES()
        overrides = {str(k): str(v) for k, v in parsed.items() if k and v}
    result = await svc.preview(
        db, current_user.organization_id, content, file.filename,
        store_overrides=overrides, actor_id=current_user.id,
    )
    return EmpidImportPreviewResponse(
        people=[_person_out(p) for p in result.people],
        placeholder=[_person_out(p) for p in result.placeholder],
        deferred=[_person_out(p) for p in result.deferred],
        counts=result.counts(),
        excluded_rows=result.excluded_rows,
        total_rows=result.total_rows,
        unmatched_stores=[EmpidUnmatchedStore(**u) for u in result.unmatched_stores],
        saved_aliases=[EmpidSavedAlias(**a) for a in result.saved_aliases],
        reconciliation=[
            EmpidReconciliationScope(**r) for r in result.reconciliation
        ],
        distribution=[EmpidBandCount(**d) for d in result.distribution],
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
    # blank 는 내용이 항상 같은 빈 양식이라 고정 이름이 맞다. current 는 필터에
    # 따라 내용이 달라지므로 매장/사람 필터·옵션을 파일명에 남긴다.
    if prefill:
        scope = await resolve_store_scope(
            db, current_user.organization_id, store_ids
        )
        extra = []
        if people != "all":
            extra.append(people)  # numbered / unnumbered
        if not include_dormant:
            extra.append("ActiveOnly")
        if not include_numbers:
            extra.append("BlankIDs")  # 작성용 양식 — 번호 칸이 비어 있다
        if not include_email:
            extra.append("NoEmail")  # 재업로드 매칭 불가
        filename = export_filename("EmpID_Export", scope=scope, extra=extra)
    else:
        filename = "EmpID_ImportTemplate.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(filename)},
    )


@router.post("/export")
async def export_selected_empids(
    data: EmpidExportRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:read"))],
):
    """사람 단위 선택 export — 콘솔 필터·개별 선택 결과를 임포트 형식 xlsx 로.

    split_by(store|role|band)로 시트 구분 가능. 재업로드는 첫 시트만 읽힌다.
    """
    from fastapi.responses import Response

    if data.split_by not in ("none", "store", "role", "band"):
        raise BadRequestError("split_by must be none|store|role|band")
    selected: list[tuple[UUID, UUID]] = []
    for item in data.items:
        try:
            selected.append((UUID(item.user_id), UUID(item.store_id)))
        except ValueError:
            continue  # 잘못된 UUID 는 무시 (프론트 방어)
    content = await svc.build_selected_export_xlsx(
        db, current_user.organization_id, selected,
        include_email=data.include_email, include_numbers=data.include_numbers,
        split_by=data.split_by,
    )
    # 선택 export 는 "누구를 골랐는지" 가 내용이다 — 인원수와 시트 분할 축을
    # 파일명에 남겨야 두 번 받은 파일이 구분된다.
    store_ids = {sid for _, sid in selected}
    scope = await resolve_store_scope(db, current_user.organization_id, store_ids)
    extra = [f"{len(selected)}rows"]
    if data.split_by != "none":
        extra.append(f"by{data.split_by}")
    if not data.include_numbers:
        extra.append("BlankIDs")
    if not data.include_email:
        extra.append("NoEmail")
    filename = export_filename("EmpID_Export", scope=scope, extra=extra)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(filename)},
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
    empid_kind 는 생략하면 sequence — 어느 화면에서 왔는지로 추론하지 않는다.
    응답의 cursor_after 는 커밋 후 커서(커밋이 커서를 밀지는 않는다).
    """
    assignments: list[svc.CommitAssignment] = []
    for item in data.assignments:
        try:
            assignments.append(svc.CommitAssignment(
                user_id=UUID(item.user_id),
                store_id=UUID(item.store_id),
                empid=item.empid,
                empid_kind=item.empid_kind,
                reason=item.reason,
            ))
        except ValueError:
            continue  # 잘못된 UUID 는 무시 (프론트 방어)
    result = await svc.commit(
        db, current_user.organization_id, assignments, actor_id=current_user.id,
    )
    return EmpidImportCommitResponse(
        applied=result.applied,
        renumbered=result.renumbered,
        skipped=result.skipped,
        rejected=result.rejected,
        exception_count=result.exception_count,
        cursor_after=result.cursor_after,
    )
