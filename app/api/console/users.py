"""관리자 사용자 라우터 — 사용자 CRUD 및 매장 배정 엔드포인트.

Admin User Router — CRUD and store assignment endpoints for user management.
Provides user listing with filters, detail retrieval, creation, update,
activation toggle, and user-store association management.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import hide_cost_for, require_permission, scrub_cost_fields
from app.database import get_db
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.rate import RateChangeCreate, RateChangeEntry, RateChangeResult
from app.schemas.user import (
    AbsorbPlanResponse,
    AbsorbRequest,
    ClaimCodeResponse,
    ProvisionalUserBulkCreate,
    ProvisionalUserCreate,
    SyncUserStoresRequest,
    UserBulkUpdate,
    UserBulkUpdateResult,
    UserCreate,
    UserListResponse,
    UserResponse,
    UserStoreResponse,
    UserUpdate,
)
from app.services.user_service import user_service

router: APIRouter = APIRouter()


@router.get("", response_model=list[UserListResponse])
async def list_users(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:read"))],
    store_id: Annotated[UUID | None, Query(description="매장 ID 필터 (단일)")] = None,
    store_ids: Annotated[str | None, Query(description="매장 ID 필터 (복수, 콤마 구분)")] = None,
    role_id: Annotated[UUID | None, Query(description="역할 ID 필터")] = None,
    is_active: Annotated[bool | None, Query(description="활성 상태 필터")] = None,
    include_provisional: Annotated[bool, Query(description="미가입(유령) 계정을 is_active 필터에서 면제")] = False,
    provisional_only: Annotated[bool, Query(description="미가입(유령) 계정만 조회")] = False,
) -> list[UserListResponse]:
    """사용자 목록을 필터 조건으로 조회합니다.

    List users with optional filters (store_id/store_ids, role_id, is_active).
    store_ids는 콤마로 구분된 UUID 문자열 (예: "uuid1,uuid2").
    미가입 계정은 is_active=False 라 is_active 필터가 걸리면 사라진다 —
    include_provisional=true 로 면제하거나 provisional_only=true 로 유령만 조회.
    """
    org_id: UUID = current_user.organization_id
    # store_ids가 있으면 store_id보다 우선
    parsed_store_ids: list[UUID] | None = None
    if store_ids:
        parsed_store_ids = [UUID(s.strip()) for s in store_ids.split(",") if s.strip()]
    elif store_id:
        parsed_store_ids = [store_id]
    filters: dict = {
        "store_ids": parsed_store_ids,
        "role_id": role_id,
        "is_active": is_active,
        "include_provisional": include_provisional,
        "provisional_only": provisional_only,
    }
    users = await user_service.list_users(db, org_id, filters)
    if hide_cost_for(current_user):
        for u in users:
            scrub_cost_fields(u)
    return users


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:read"))],
) -> UserResponse:
    """사용자 상세 정보를 조회합니다.

    Retrieve user detail with role information.
    """
    org_id: UUID = current_user.organization_id
    user = await user_service.get_user(db, user_id, org_id)
    if hide_cost_for(current_user):
        scrub_cost_fields(user)
    return user


@router.post("/provisional", response_model=UserResponse, status_code=201)
async def create_provisional_user(
    data: ProvisionalUserCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:create"))],
) -> UserResponse:
    """미가입(유령) 직원을 생성합니다 — 아직 앱에 가입하지 않은 직원 자리.

    이름·역할·매장만으로 생성되고 username/비밀번호는 자동. 로그인은 불가하며
    (is_active=False) 스케줄 배정·empid 부여는 가능. 응답의 claim_code 를 직원에게
    전달하면 본인이 가입할 때 이 계정을 그대로 인수한다.
    """
    from app.services import provisional_staff_service as prov_svc

    org_id: UUID = current_user.organization_id
    user = await prov_svc.create_provisional_user(
        db,
        org_id,
        full_name=data.full_name,
        role_id=UUID(data.role_id),
        store_ids=[UUID(s) for s in data.store_ids],
        department=data.department,
        hourly_rate=data.hourly_rate,
        caller=current_user,
    )
    detail = await user_service.get_user(db, user.id, org_id)
    if hide_cost_for(current_user):
        scrub_cost_fields(detail)
    return detail


@router.post("/provisional/bulk", response_model=list[UserResponse], status_code=201)
async def create_provisional_users_bulk(
    data: ProvisionalUserBulkCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:create"))],
) -> list[UserResponse]:
    """미가입 직원을 여러 명 한 번에 생성합니다 (단일 트랜잭션)."""
    from app.services import provisional_staff_service as prov_svc

    org_id: UUID = current_user.organization_id
    users = await prov_svc.create_provisional_users_bulk(
        db,
        org_id,
        [
            {
                "full_name": p.full_name,
                "role_id": UUID(p.role_id),
                "store_ids": [UUID(s) for s in p.store_ids],
                "department": p.department,
                "hourly_rate": p.hourly_rate,
            }
            for p in data.people
        ],
        caller=current_user,
    )
    out: list[UserResponse] = []
    for u in users:
        detail = await user_service.get_user(db, u.id, org_id)
        if hide_cost_for(current_user):
            scrub_cost_fields(detail)
        out.append(detail)
    return out


@router.post("/{user_id}/absorb/preview", response_model=AbsorbPlanResponse)
async def preview_absorb_provisional(
    user_id: UUID,
    data: AbsorbRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:update"))],
) -> AbsorbPlanResponse:
    """미가입 계정을 실제 계정으로 흡수하기 전 계획을 확인합니다 (DB 변경 없음).

    정상 경로는 인수 코드(claim)라 데이터 이동이 없다. 이 기능은 직원이 코드를 안 쓰고
    따로 가입해 계정이 2개가 된 경우의 폴백이다.
    """
    from app.services import provisional_absorb_service as absorb_svc

    plan = await absorb_svc.preview_absorb(
        db, current_user.organization_id, user_id, UUID(data.target_user_id)
    )
    return AbsorbPlanResponse(**plan.__dict__)


@router.post("/{user_id}/absorb", response_model=AbsorbPlanResponse)
async def absorb_provisional(
    user_id: UUID,
    data: AbsorbRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:update"))],
) -> AbsorbPlanResponse:
    """미가입 계정의 배정·번호·스케줄을 실제 계정으로 옮기고 미가입 행을 폐기합니다."""
    from app.services import provisional_absorb_service as absorb_svc

    plan = await absorb_svc.absorb(
        db, current_user.organization_id, user_id, UUID(data.target_user_id)
    )
    return AbsorbPlanResponse(**plan.__dict__)


@router.post("/{user_id}/claim-code", response_model=ClaimCodeResponse)
async def regenerate_claim_code(
    user_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:update"))],
) -> ClaimCodeResponse:
    """인수 코드를 재발급합니다 (코드 분실·유출 시). 미가입 계정만 가능."""
    from app.services import provisional_staff_service as prov_svc

    code = await prov_svc.regenerate_claim_code(
        db, current_user.organization_id, user_id
    )
    return ClaimCodeResponse(user_id=str(user_id), claim_code=code)


@router.post("", response_model=UserResponse, status_code=201)
async def create_user(
    data: UserCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:create"))],
) -> UserResponse:
    """새 사용자를 생성합니다.

    Create a new user in the current organization.
    Supervisor can create Staff; GM can create Supervisor+Staff; Owner can create all.
    """
    org_id: UUID = current_user.organization_id
    user = await user_service.create_user(db, org_id, data, caller=current_user)
    if hide_cost_for(current_user):
        scrub_cost_fields(user)
    return user


@router.patch("/bulk", response_model=UserBulkUpdateResult)
async def bulk_update_users(
    data: UserBulkUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:update"))],
) -> UserBulkUpdateResult:
    """여러 직원의 필드를 일괄 변경합니다.

    Bulk-update fields for multiple users. 보낸 필드만 적용
    (department/is_active/hourly_rate). null 명시 시 해제(미지정/상속) 의미.
    """
    org_id: UUID = current_user.organization_id
    # model_fields_set 으로 "보낸 필드"만 추출 (user_ids 제외)
    changes = data.model_dump(include=data.model_fields_set - {"user_ids"})
    count = await user_service.bulk_update_users(
        db, org_id, data.user_ids, changes, caller=current_user
    )
    return UserBulkUpdateResult(updated_count=count)


@router.put("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: UUID,
    data: UserUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:update"))],
) -> UserResponse:
    """사용자 정보를 수정합니다.

    Update an existing user's information.
    """
    org_id: UUID = current_user.organization_id
    user = await user_service.update_user(db, user_id, org_id, data, caller=current_user)
    if hide_cost_for(current_user):
        scrub_cost_fields(user)
    return user


@router.patch("/{user_id}/active", response_model=UserResponse)
async def toggle_user_active(
    user_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:update"))],
) -> UserResponse:
    """사용자 활성/비활성 상태를 토글합니다.

    Toggle a user's active/inactive status.
    """
    org_id: UUID = current_user.organization_id
    user = await user_service.toggle_active(db, user_id, org_id)
    if hide_cost_for(current_user):
        scrub_cost_fields(user)
    return user


@router.delete("/{user_id}", response_model=MessageResponse)
async def delete_user(
    user_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:delete"))],
) -> dict[str, str]:
    """사용자를 삭제합니다 (소프트 삭제: is_active=False 처리).

    Delete a user (soft-delete: sets is_active=False and clears store assignments).
    Only managers and above can delete users.

    Args:
        user_id: 삭제할 사용자 UUID (User UUID to delete)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 관리자 사용자 (Authenticated admin user)

    Returns:
        dict: 삭제 결과 메시지 (Deletion result message)
    """
    org_id: UUID = current_user.organization_id
    await user_service.delete_user(db, user_id, org_id)
    return {"message": "User deleted successfully"}


@router.get("/{user_id}/stores", response_model=list[UserStoreResponse])
async def get_user_stores(
    user_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:read"))],
) -> list[UserStoreResponse]:
    """사용자에게 배정된 매장 목록 (is_manager 포함)."""
    org_id: UUID = current_user.organization_id
    return await user_service.get_user_stores(db, user_id, org_id)


@router.put("/{user_id}/stores", response_model=list[UserStoreResponse])
async def sync_user_stores(
    user_id: UUID,
    data: SyncUserStoresRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:update"))],
) -> list[UserStoreResponse]:
    """매장 배정 일괄 저장 (diff 기반)."""
    org_id: UUID = current_user.organization_id
    assignments = [
        {
            "store_id": UUID(a.store_id),
            "is_manager": a.is_manager,
            "is_work_assignment": a.is_work_assignment,
        }
        for a in data.assignments
    ]
    await user_service.sync_user_stores(db, user_id, org_id, assignments)
    return await user_service.get_user_stores(db, user_id, org_id)


@router.post("/{user_id}/stores/{store_id}", status_code=201)
async def add_user_store(
    user_id: UUID,
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:create"))],
) -> dict[str, str]:
    """사용자에게 매장을 배정합니다 (개별, 하위호환)."""
    org_id: UUID = current_user.organization_id
    await user_service.add_user_store(db, user_id, store_id, org_id, caller=current_user)
    return {"message": "Store assigned successfully"}


@router.delete("/{user_id}/stores/{store_id}", status_code=204)
async def remove_user_store(
    user_id: UUID,
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:delete"))],
) -> None:
    """사용자에게서 매장 배정을 해제합니다 (개별, 하위호환)."""
    org_id: UUID = current_user.organization_id
    await user_service.remove_user_store(db, user_id, store_id, org_id)


@router.post("/{user_id}/reset-password")
async def admin_reset_password(
    user_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:update"))],
) -> dict:
    """관리자 비밀번호 초기화 — 임시 비밀번호 생성 + 이메일 발송."""
    from app.services.password_service import password_service

    temp_password = await password_service.admin_reset_password(
        db, current_user, str(user_id)
    )
    return {
        "temporary_password": temp_password,
        "message": "Password reset successfully",
    }


# ── Rate changes (시급 변경 이력 — Payroll v1 Phase 1) ───────────────────────
# 쓰기 권한 = 기존 hourly_rate 편집 경로(PUT /users/{id})와 동일한 users:update.
# 추가로 cost 가시성(GM+) 게이트 — SV/Staff 는 permission 이 있어도 시급 접근 불가
# (scrub_cost_fields 와 같은 규칙. 목록/상세는 스크럽이지만 여긴 이력 전체가
# cost 데이터라 403 으로 차단).


def _require_cost_visibility(current_user: User) -> None:
    """cost(시급) 가시성 게이트 — GM 미만이면 403 (원인+대상 역할 명시)."""
    if hide_cost_for(current_user):
        from app.utils.exceptions import ForbiddenError

        raise ForbiddenError(
            "Hourly rate information is only available to GM and above"
        )


@router.post("/{user_id}/rate-changes", response_model=RateChangeResult)
async def create_user_rate_change(
    user_id: UUID,
    data: RateChangeCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:update"))],
) -> RateChangeResult:
    """개인 시급 변경 등록 — 이력 + org_members/users dual-write + 스케줄 갱신.

    effective_date 생략 시 즉시(오늘 UTC) 적용, 미래 날짜는 일일 잡이 반영.
    같은 값 재등록은 no-op (recorded=False). 0 이하 시급은 400.
    """
    _require_cost_visibility(current_user)
    return await user_service.create_rate_change(
        db,
        user_id,
        current_user.organization_id,
        new_rate=data.new_rate,
        effective_date=data.effective_date,
        reason=data.reason,
        caller=current_user,
    )


@router.get("/{user_id}/rate-changes", response_model=list[RateChangeEntry])
async def list_user_rate_changes(
    user_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("users:read"))],
) -> list[RateChangeEntry]:
    """개인 시급 변경 이력 목록 (최신 우선 — effective_date DESC, created_at DESC)."""
    _require_cost_visibility(current_user)
    return await user_service.list_rate_changes(
        db, user_id, current_user.organization_id
    )


# ── Clockin PIN (attendance device 용) ───────────────────────────
from sqlalchemy import select as _select  # noqa: E402

from app.models.user import Role  # noqa: E402
from app.schemas.attendance_device import (  # noqa: E402
    ClockinPinDirectoryResponse,
    ClockinPinHolder,
    ClockinPinLookupResponse,
    ClockinPinResponse,
    ClockinPinSuggestResponse,
    ClockinPinUpdateRequest,
)
from app.services.attendance_device_service import (  # noqa: E402
    commit_pin_or_409,
    assert_no_pin_conflict,
    find_pin_conflicts,
    generate_unique_clockin_pin,
    suggest_available_clockin_pin,
)
from app.utils.exceptions import NotFoundError  # noqa: E402


# PIN 도구가 한 번에 돌려주는 최대 인원. 이름 검색이 너무 넓으면 잘라내고
# truncated=True 로 알린다 (콘솔이 "검색어를 좁히세요" 안내).
PIN_DIRECTORY_LIMIT = 50


def _pin_holder(user: User, role_name: str | None, conflict: str | None = None) -> ClockinPinHolder:
    """User 행 → PIN 도구용 응답 항목."""
    return ClockinPinHolder(
        user_id=user.id,
        full_name=user.full_name,
        username=user.username,
        role_name=role_name,
        is_active=user.is_active,
        is_provisional=user.is_provisional,
        clockin_pin=user.clockin_pin,
        conflict=conflict,  # type: ignore[arg-type]
    )


async def _fetch_users_with_roles(
    db: AsyncSession, org_id: UUID, user_ids: list[UUID]
) -> list[tuple[User, str | None]]:
    """user_ids 를 org 스코프로 (User, role_name) 쌍으로 읽어온다. 순서는 입력 순."""
    if not user_ids:
        return []
    rows = (
        await db.execute(
            _select(User, Role.name)
            .join(Role, Role.id == User.role_id, isouter=True)
            .where(User.organization_id == org_id, User.id.in_(user_ids))
        )
    ).all()
    by_id = {row[0].id: (row[0], row[1]) for row in rows}
    return [by_id[uid] for uid in user_ids if uid in by_id]


async def _fetch_org_user(db: AsyncSession, user_id: UUID, org_id: UUID) -> User:
    result = await db.execute(
        _select(User).where(User.id == user_id, User.organization_id == org_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise NotFoundError("User not found")
    return user


# 리터럴 경로는 /{user_id}/... 보다 먼저 선언한다 — "clockin-pin" 이 user_id 로
# 파싱되어 422 가 나는 사고를 막기 위해.
@router.get("/clockin-pin/lookup", response_model=ClockinPinLookupResponse)
async def lookup_clockin_pin(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("clockin_pin:read"))],
    pin: Annotated[str, Query(pattern=r"^\d{4,6}$", description="조회할 PIN (4~6자리)")],
) -> ClockinPinLookupResponse:
    """Staff PIN 도구 — 이 PIN 이 지금 배정 가능한지 + 막고 있는 직원.

    저장 경로(`assert_no_pin_conflict`)와 같은 판정을 쓰므로
    available=true 면 그대로 저장해도 409 가 나지 않는다(동시성 제외).
    """
    org_id: UUID = current_user.organization_id
    conflicts = await find_pin_conflicts(db, org_id, pin)
    if not conflicts:
        return ClockinPinLookupResponse(pin=pin, available=True, reason=None, holders=[])

    # 충돌은 같은 번호를 이미 쓰는 사람뿐이고, org 안에서 unique 라 사실상 한 명이다.
    pairs = await _fetch_users_with_roles(db, org_id, [uid for uid, _ in conflicts])
    holders = [
        _pin_holder(user, role_name, conflict="exact") for user, role_name in pairs
    ]
    return ClockinPinLookupResponse(
        pin=pin, available=False, reason="exact", holders=holders
    )


@router.get("/clockin-pin/directory", response_model=ClockinPinDirectoryResponse)
async def list_clockin_pin_directory(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("clockin_pin:read"))],
    q: Annotated[str | None, Query(description="이름/username 부분 일치, 또는 PIN 앞자리")] = None,
    include_inactive: Annotated[bool, Query(description="비활성 직원 포함")] = False,
) -> ClockinPinDirectoryResponse:
    """Staff PIN 도구 — 이름 또는 PIN 으로 직원 + 현재 PIN 목록 조회.

    `q` 가 숫자면 PIN 앞자리 검색으로도 매칭한다(이름에 숫자를 쓰는 경우가 없어
    둘을 OR 로 붙여도 결과가 흐려지지 않는다). 빈 `q` 는 PIN 이 있는 직원부터
    이름순으로 상위 `PIN_DIRECTORY_LIMIT` 명.
    """
    from sqlalchemy import or_ as _or

    org_id: UUID = current_user.organization_id
    stmt = (
        _select(User, Role.name)
        .join(Role, Role.id == User.role_id, isouter=True)
        .where(User.organization_id == org_id)
    )
    if not include_inactive:
        # 유령(미가입)은 is_active=False 지만 PIN 관리 대상이라 면제.
        stmt = stmt.where(_or(User.is_active.is_(True), User.is_provisional.is_(True)))

    term = (q or "").strip()
    if term:
        conditions = [
            User.full_name.ilike(f"%{term}%"),
            User.username.ilike(f"%{term}%"),
        ]
        if term.isdigit():
            conditions.append(User.clockin_pin.startswith(term))
        stmt = stmt.where(_or(*conditions))

    stmt = stmt.order_by(User.full_name).limit(PIN_DIRECTORY_LIMIT + 1)
    rows = (await db.execute(stmt)).all()
    truncated = len(rows) > PIN_DIRECTORY_LIMIT
    items = [_pin_holder(row[0], row[1]) for row in rows[:PIN_DIRECTORY_LIMIT]]
    return ClockinPinDirectoryResponse(items=items, truncated=truncated)


@router.get("/clockin-pin/suggest", response_model=ClockinPinSuggestResponse)
async def suggest_clockin_pin(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("clockin_pin:read"))],
    length: Annotated[int, Query(ge=4, le=6, description="추천 PIN 자릿수")] = 4,
) -> ClockinPinSuggestResponse:
    """Staff PIN 도구 — 안 쓰이는 PIN 하나 추천. 배정은 하지 않는다.

    자릿수 공간이 전부 막혔으면 pin=null — 콘솔이 "자릿수를 늘리세요" 로 안내한다.
    """
    pin = await suggest_available_clockin_pin(
        db, current_user.organization_id, length=length
    )
    return ClockinPinSuggestResponse(pin=pin, length=length)


@router.get("/{user_id}/clockin-pin", response_model=ClockinPinResponse)
async def get_user_clockin_pin(
    user_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("clockin_pin:read"))],
) -> ClockinPinResponse:
    """Staff detail — attendance device PIN 조회."""
    user = await _fetch_org_user(db, user_id, current_user.organization_id)
    return ClockinPinResponse(user_id=user.id, clockin_pin=user.clockin_pin)


@router.post("/{user_id}/clockin-pin/regenerate", response_model=ClockinPinResponse)
async def regenerate_user_clockin_pin(
    user_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("clockin_pin:update"))],
) -> ClockinPinResponse:
    """Staff detail — attendance device PIN 재발급."""
    user = await _fetch_org_user(db, user_id, current_user.organization_id)
    user.clockin_pin = await generate_unique_clockin_pin(
        db, current_user.organization_id, exclude_user_id=user.id
    )
    await commit_pin_or_409(db)
    return ClockinPinResponse(user_id=user.id, clockin_pin=user.clockin_pin)


@router.put("/{user_id}/clockin-pin", response_model=ClockinPinResponse)
async def update_user_clockin_pin(
    user_id: UUID,
    body: ClockinPinUpdateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("clockin_pin:update"))],
) -> ClockinPinResponse:
    """Staff detail — attendance device PIN 직접 지정 (관리자)."""
    user = await _fetch_org_user(db, user_id, current_user.organization_id)
    await assert_no_pin_conflict(
        db, current_user.organization_id, body.clockin_pin, exclude_user_id=user.id
    )
    user.clockin_pin = body.clockin_pin
    await commit_pin_or_409(db)
    return ClockinPinResponse(user_id=user.id, clockin_pin=user.clockin_pin)


@router.delete("/{user_id}/clockin-pin", response_model=ClockinPinResponse)
async def clear_user_clockin_pin(
    user_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("clockin_pin:update"))],
) -> ClockinPinResponse:
    """Staff PIN 도구 — PIN 제거(번호를 비운다).

    PIN 이 없는 직원은 키오스크에서 PIN 으로 출퇴근할 수 없다 — 퇴사자나
    잘못 들어간 번호를 비워 그 번호를 다시 쓸 수 있게 하는 용도.
    """
    user = await _fetch_org_user(db, user_id, current_user.organization_id)
    user.clockin_pin = None
    await db.commit()
    return ClockinPinResponse(user_id=user.id, clockin_pin=None)
