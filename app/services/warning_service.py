"""경고 서비스 — Warning v1 비즈니스 로직.

Warning Service — create/update/resolve/delete, 방향 검증, subject-store 검증,
seq 발급, 소유권(Owner 전체 / GM 본인) 수정·삭제, 직원별 카운트, 응답 빌드.

핵심 규칙:
    - org-scope: 모든 조회는 organization_id 로 격리.
    - 방향 검증: app.core.permissions.can_warn (발행자보다 엄격히 낮은 권한만 대상).
    - subject-store: 경고의 store 는 대상 직원이 배정된 매장(user_stores) 이어야 한다.
    - 사람 ID: seq = org당 max+1. UNIQUE(org, seq) 충돌 시 재시도.
    - 수정/삭제 소유권: Owner 는 조직 전체, 그 외(GM)는 본인 발행건만.
    - soft delete: deleted_at. 읽기는 항상 deleted_at IS NULL.
"""

import re
from datetime import date, datetime, timezone
from typing import Sequence
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.permissions import can_warn, is_owner, role_priority
from app.models.organization import Store
from app.models.user import User
from app.models.user_store import UserStore
from app.models.warning import Warning
from app.repositories.warning_repository import warning_repository
from app.repositories.warning_category_repository import warning_category_repository
from app.services.alert_service import alert_service
from app.services.storage_service import storage_service
from app.services.warning_category_service import warning_category_service
from app.utils.exceptions import BadRequestError, ForbiddenError, NotFoundError


def _ref_no(seq: int) -> str:
    """seq → 사람용 ID "W-00046"."""
    return f"W-{seq:05d}"


class WarningService:
    """경고 서비스 — 대상 picker + 경고 CRUD + 카운트 + 응답 빌드."""

    # ====================================================================
    # 경고 대상 직원 (picker)
    # ====================================================================

    async def list_warnable_users(
        self,
        db: AsyncSession,
        current_user: User,
        *,
        store_id: UUID | None = None,
        q: str | None = None,
        page: int = 1,
        limit: int = 30,
    ) -> dict:
        """방향 필터된 경고 대상 직원 목록 (paginated envelope) + stores[].

        roles.priority > current_user priority (엄격히 더 낮은 권한), org-scope,
        활성, 자기 제외. 매장 접근 검증은 라우터에서 선행한다.
        N+1 제거: repository 가 role + user_stores→store 를 eager load.
        """
        page = max(1, page)
        limit = max(1, min(limit, 100))
        q_clean = q.strip() if q else None

        users, total = await warning_repository.list_warnable_users(
            db,
            current_user.organization_id,
            min_priority_exclusive=role_priority(current_user),
            exclude_user_id=current_user.id,
            store_id=store_id,
            q=q_clean,
            limit=limit,
            offset=(page - 1) * limit,
        )

        org_id = current_user.organization_id
        items: list[dict] = []
        for u in users:
            assigned = [
                us
                for us in u.user_stores
                if us.store is not None and us.store.organization_id == org_id
            ]
            assigned.sort(key=lambda us: us.created_at)
            stores = [{"id": str(us.store_id), "name": us.store.name} for us in assigned]
            primary = assigned[0] if assigned else None

            items.append(
                {
                    "id": str(u.id),
                    "full_name": u.full_name,
                    "employee_no": u.employee_no,
                    "role_name": u.role.name if u.role else "",
                    "role_priority": role_priority(u),
                    "store_id": str(primary.store_id) if primary else None,
                    "store_name": primary.store.name if primary else None,
                    "stores": stores,
                }
            )

        return {
            "items": items,
            "total": total,
            "page": page,
            "limit": limit,
            "has_more": (page * limit) < total,
        }

    # ====================================================================
    # 경고 조회 / 카운트
    # ====================================================================

    async def list_warnings(
        self,
        db: AsyncSession,
        organization_id: UUID,
        *,
        store_ids: list[UUID] | None = None,
        status: str | None = None,
        category: str | None = None,
        subject_user_id: UUID | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Warning], int]:
        return await warning_repository.list_active(
            db,
            organization_id,
            store_ids=store_ids,
            status=status,
            category=category,
            subject_user_id=subject_user_id,
            page=page,
            per_page=per_page,
        )

    async def get_warning(
        self, db: AsyncSession, warning_id: UUID, organization_id: UUID
    ) -> Warning:
        """경고 단건 조회 (org-scope + soft-delete 제외). 부재 시 404."""
        warning = await warning_repository.get_active(db, warning_id, organization_id)
        if warning is None:
            raise NotFoundError("Warning not found")
        return warning

    # ====================================================================
    # 직원 본인(app) — 내 경고 조회 / 확인(acknowledge) / 미서명 카운트
    # ====================================================================

    async def list_my_warnings(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        subject_user_id: UUID,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Warning], int]:
        """직원 본인의 active 경고 목록 (app). withdrawn/삭제 제외."""
        return await warning_repository.list_my_active(
            db, organization_id, subject_user_id, page=page, per_page=per_page
        )

    async def get_my_warning(
        self,
        db: AsyncSession,
        *,
        warning_id: UUID,
        organization_id: UUID,
        subject_user_id: UUID,
    ) -> Warning:
        """직원 본인의 단일 경고 (app). 본인 소유 아니거나 부재면 404.

        존재 누설 방지를 위해 '내 것이 아님'과 '없음'을 동일하게 404 처리한다.
        """
        warning = await warning_repository.get_my_active(
            db, warning_id, organization_id, subject_user_id
        )
        if warning is None:
            raise NotFoundError("Warning not found")
        return warning

    async def acknowledge_warning(
        self, db: AsyncSession, warning: Warning
    ) -> Warning:
        """경고를 확인(읽음) 처리 — acknowledged_at 을 최초 1회 stamp (idempotent).

        이미 확인된 경고는 no-op (기존 시각 유지). 확인 != 서명.
        """
        if warning.acknowledged_at is None:
            try:
                warning.acknowledged_at = datetime.now(timezone.utc)
                await db.flush()
                await db.refresh(warning)
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return warning

    async def count_my_unsigned(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        subject_user_id: UUID,
    ) -> int:
        """본인의 active 경고 중 employee 서명이 없는 갯수 (badge)."""
        return await warning_repository.count_my_unsigned(
            db, organization_id, subject_user_id
        )

    async def get_counts(
        self,
        db: AsyncSession,
        organization_id: UUID,
        *,
        store_ids: list[UUID] | None = None,
    ) -> list[dict]:
        """직원별 (total, active) 경고 갯수 — Staff 목록 칼럼용."""
        counts = await warning_repository.counts_by_subject(
            db, organization_id, store_ids=store_ids
        )
        return [
            {"user_id": str(uid), "total": total, "active": active}
            for uid, (total, active) in counts.items()
        ]

    # ====================================================================
    # 생성 / 수정 / 삭제
    # ====================================================================

    async def _load_subject(
        self, db: AsyncSession, subject_id: UUID, organization_id: UUID
    ) -> User:
        """org-scope 로 대상 직원 로드 (role eager). 부재 시 404."""
        result = await db.execute(
            select(User)
            .options(selectinload(User.role))
            .where(User.id == subject_id, User.organization_id == organization_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise NotFoundError("Subject employee not found")
        return user

    async def _validate_subject_store(
        self,
        db: AsyncSession,
        subject_id: UUID,
        store_id: UUID,
        organization_id: UUID,
    ) -> None:
        """store 가 caller org 에 속하고, 대상 직원이 그 매장에 배정됐는지 검증.

        org 불일치/부재 → 404 (cross-org 누설 방지). 배정 안 됨 → 400.
        Owner 는 check_store_access 가 no-op 이므로 org 격리는 여기서 강제.
        """
        store = await db.get(Store, store_id)
        if store is None or store.organization_id != organization_id:
            raise NotFoundError("Store not found")
        result = await db.execute(
            select(UserStore.user_id).where(
                UserStore.user_id == subject_id, UserStore.store_id == store_id
            )
        )
        if result.scalar_one_or_none() is None:
            raise BadRequestError("Store is not assigned to this employee")

    def _assert_can_modify(self, current_user: User, warning: Warning) -> None:
        """수정/철회 권한 — Owner 는 전체, 그 외(GM)는 본인 발행건만. 아니면 403.

        (삭제는 별도 — Owner 전용. delete_warning 참조.)
        """
        if not is_owner(current_user) and warning.issued_by_id != current_user.id:
            raise HTTPException(
                status_code=403,
                detail="You can only modify warnings you issued",
            )

    async def create_warning(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        issuer: User,
        data,
    ) -> Warning:
        """새 경고 발행. 방향 검증(상위→하위) + subject-store 검증 + seq 발급.

        매장 접근 검증(GM 관리매장)은 라우터에서 선행. 여기서는 org 격리 +
        subject 가 그 매장 배정인지 + 방향 검증. seq 충돌 시 재시도.
        """
        subject_id = UUID(data.subject_user_id)
        store_id = UUID(data.store_id)

        subject = await self._load_subject(db, subject_id, organization_id)

        # 발행자(매니저) 결정 — 기본 = 작성자 본인. Owner 만 다른 매니저로 override 가능.
        # 방향 검증은 '실제 발행자(선택된 매니저)' 기준으로 한다.
        issued_by_id = issuer.id
        direction_issuer = issuer
        override_id = getattr(data, "issued_by_id", None)
        if override_id and UUID(override_id) != issuer.id:
            if not is_owner(issuer):
                raise HTTPException(
                    status_code=403,
                    detail="Only an Owner can issue a warning on behalf of another manager",
                )
            selected = await self._load_subject(db, UUID(override_id), organization_id)
            issued_by_id = selected.id
            direction_issuer = selected

        # 방향 검증 — 발행자보다 엄격히 낮은 권한만.
        if not can_warn(direction_issuer, subject):
            raise HTTPException(
                status_code=403,
                detail="You can only warn users with lower authority",
            )

        # 사유 카테고리 검증 — org 의 비삭제 카테고리여야 함.
        await warning_category_service.validate_codes(
            db, organization_id, list(data.categories)
        )

        # store org 격리 + subject 배정 매장 검증.
        await self._validate_subject_store(db, subject_id, store_id, organization_id)

        # seq + 차수(ordinal_snapshot) 발급 + insert.
        # UNIQUE(org, seq) 또는 (org, subject, ordinal) 충돌 시 재시도 — 동시 발행 직렬화.
        last_exc: Exception | None = None
        for _ in range(5):
            seq = await warning_repository.next_seq(db, organization_id)
            ordinal_snapshot = await warning_repository.next_ordinal(
                db, organization_id, subject_id
            )
            warning = Warning(
                organization_id=organization_id,
                seq=seq,
                ordinal_snapshot=ordinal_snapshot,
                issued_by_id=issued_by_id,
                subject_user_id=subject_id,
                store_id=store_id,
                title=data.title,
                categories=list(data.categories),
                details=data.details,
                corrective_action=data.corrective_action,
                other_text=data.other_text,
                deadline=data.deadline,
                follow_up_date=data.follow_up_date,
                follow_up_time=data.follow_up_time,
                status="active",
                warning_date=data.warning_date,
                signature_method=getattr(data, "signature_method", "digital") or "digital",
            )
            db.add(warning)
            try:
                await db.flush()
                await db.refresh(warning)
                # 대상 직원에게 in-app 알림 (선호 비활성 시 내부에서 skip).
                # flush 후 동일 트랜잭션에서 생성 — commit 으로 함께 영속.
                await alert_service.create_for_warning(
                    db,
                    organization_id=organization_id,
                    subject_user_id=subject_id,
                    warning_id=warning.id,
                    title=warning.title,
                )
                await db.commit()
                return warning
            except IntegrityError as exc:
                await db.rollback()
                last_exc = exc
        # 5회 모두 실패 (희박) — 원래 예외 재발생.
        assert last_exc is not None
        raise last_exc

    async def update_warning(
        self,
        db: AsyncSession,
        *,
        warning_id: UUID,
        organization_id: UUID,
        current_user: User,
        data,
        check_store_access,
    ) -> Warning:
        """경고 수정 (partial). 소유권 검증 후 제공된 필드만 반영.

        store 변경 시 org 격리 + 접근(check_store_access) + subject 배정 재검증.
        status 'resolved'↔'active' 토글 시 resolved_at stamp/clear.
        subject(대상 직원)는 변경 불가.
        check_store_access: 라우터가 주입하는 async (store_id) → None | raise 403.
        """
        warning = await self.get_warning(db, warning_id, organization_id)
        self._assert_can_modify(current_user, warning)
        fields = data.model_dump(exclude_unset=True)

        try:
            if "store_id" in fields and fields["store_id"] is not None:
                new_store_id = UUID(fields["store_id"])
                await check_store_access(new_store_id)
                # 대상 직원이 새 매장에 배정됐는지 + org 격리.
                if warning.subject_user_id is not None:
                    await self._validate_subject_store(
                        db, warning.subject_user_id, new_store_id, organization_id
                    )
                warning.store_id = new_store_id

            if "title" in fields and fields["title"] is not None:
                warning.title = fields["title"]
            if "categories" in fields and fields["categories"] is not None:
                # 수정 시엔 그 경고가 이미 가진 코드(legacy=삭제된 카테고리)도 허용.
                await warning_category_service.validate_codes(
                    db,
                    organization_id,
                    fields["categories"],
                    existing_codes=list(warning.categories or []),
                )
                warning.categories = list(fields["categories"])
            if "details" in fields:
                warning.details = fields["details"]
            if "corrective_action" in fields:
                warning.corrective_action = fields["corrective_action"]
            if "other_text" in fields:
                warning.other_text = fields["other_text"]
            if "deadline" in fields:
                warning.deadline = fields["deadline"]
            if "follow_up_date" in fields:
                warning.follow_up_date = fields["follow_up_date"]
            if "follow_up_time" in fields:
                warning.follow_up_time = fields["follow_up_time"]
            if "warning_date" in fields and fields["warning_date"] is not None:
                warning.warning_date = fields["warning_date"]

            # 발행자(매니저) 변경 — Owner only + 방향 검증(새 발행자가 대상보다 상위).
            if fields.get("issued_by_id") is not None:
                new_issuer_id = UUID(fields["issued_by_id"])
                if new_issuer_id != warning.issued_by_id:
                    if not is_owner(current_user):
                        raise HTTPException(
                            status_code=403,
                            detail="Only an Owner can change the issuing manager",
                        )
                    selected = await self._load_subject(db, new_issuer_id, organization_id)
                    if warning.subject_user_id is not None:
                        subj = await self._load_subject(
                            db, warning.subject_user_id, organization_id
                        )
                        if not can_warn(selected, subj):
                            raise HTTPException(
                                status_code=403,
                                detail="Selected manager does not outrank the subject",
                            )
                    warning.issued_by_id = new_issuer_id

            if "status" in fields and fields["status"] is not None:
                new_status = fields["status"]
                if new_status == "withdrawn" and warning.status != "withdrawn":
                    warning.withdrawn_at = datetime.now(timezone.utc)
                elif new_status == "active":
                    warning.withdrawn_at = None
                warning.status = new_status

            warning.updated_at = datetime.now(timezone.utc)
            await db.flush()
            await db.refresh(warning)
            await db.commit()
            return warning
        except Exception:
            await db.rollback()
            raise

    async def delete_warning(
        self,
        db: AsyncSession,
        *,
        warning_id: UUID,
        organization_id: UUID,
        current_user: User,
    ) -> None:
        """소프트 삭제 — Owner 전용. 이미 삭제/부재면 404 (idempotent-safe).

        삭제는 기록을 감사에서까지 지우므로 Owner 만 가능하다. GM 은 잘못 발행한
        경고를 '철회(withdrawn)'만 할 수 있고(기록은 남음), 삭제는 못 한다.
        """
        warning = await self.get_warning(db, warning_id, organization_id)
        if not is_owner(current_user):
            raise HTTPException(
                status_code=403,
                detail="Only an Owner can delete warnings",
            )
        try:
            warning.deleted_at = datetime.now(timezone.utc)
            await db.flush()
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    # ====================================================================
    # 응답 빌드
    # ====================================================================

    # ====================================================================
    # Wet 서명 (출력→실물 서명→PDF 업로드) + 방식 전환
    # ====================================================================

    @staticmethod
    def _sanitize_token(value: str | None, *, fallback: str = "NA") -> str:
        """파일명 토큰 정규화 — 영숫자만, 공백/특수문자→'_', 비ASCII 제거."""
        if not value:
            return fallback
        cleaned = re.sub(r"[^A-Za-z0-9]+", "_", value.strip()).strip("_")
        return cleaned or fallback

    def build_warning_filename(
        self,
        warning: Warning,
        *,
        subject_name: str | None,
        employee_no: str | None,
        store_code: str | None,
        category_labels: dict[str, str] | None = None,
    ) -> str:
        """다운로드 표시용 파일명.

        {YYYY.MM.DD}-{STORECODE}-{EMPID}-{CATEGORIES}-{N}-{First_Last}.pdf
        결손(코드/사번/이름)은 placeholder. 카테고리는 전부 '_' 로 연결.
        N = ordinal_snapshot(통산 차수). 날짜 = wet_signed_on 우선, 없으면 warning_date.
        """
        d: date = warning.wet_signed_on or warning.warning_date
        date_str = d.strftime("%Y.%m.%d")
        store = self._sanitize_token(store_code, fallback="NA")
        emp = self._sanitize_token(
            employee_no, fallback=str(warning.id).replace("-", "")[:8]
        )
        cats = list(warning.categories or [])
        if category_labels:
            cat_tokens = [self._sanitize_token(category_labels.get(c, c), fallback="") for c in cats]
        else:
            cat_tokens = [self._sanitize_token(c, fallback="") for c in cats]
        cat_tokens = [t for t in cat_tokens if t]
        cat_str = "_".join(cat_tokens) if cat_tokens else "NA"
        n = warning.ordinal_snapshot if warning.ordinal_snapshot is not None else "NA"
        name = self._sanitize_token(subject_name, fallback="NA")
        return f"{date_str}-{store}-{emp}-{cat_str}-{n}-{name}.pdf"

    async def upload_wet_pdf(
        self,
        db: AsyncSession,
        *,
        warning_id: UUID,
        organization_id: UUID,
        uploader: User,
        can_upload_others: bool,
        pdf_bytes: bytes,
        filename: str,
        signed_on: date | None,
        check_store_access,
    ) -> Warning:
        """wet 서명 PDF 업로드 = 서명완료. 교체 시 기존 key 삭제 후 재저장.

        Gate: method=='wet' + active + 비삭제. 발행자 본인 OR can_upload_others(오너/upload권한).
        """
        warning = await self.get_warning(db, warning_id, organization_id)
        await check_store_access(warning.store_id)
        if warning.signature_method != "wet":
            raise BadRequestError("Warning is not in wet-signature mode")
        if warning.status != "active":
            raise BadRequestError("Only active warnings can be signed")
        if warning.issued_by_id != uploader.id and not can_upload_others:
            raise ForbiddenError(
                "You can only upload signed PDFs for warnings you issued"
            )
        if not pdf_bytes.startswith(b"%PDF-"):
            raise BadRequestError("Uploaded file is not a valid PDF")

        # 교체 — 기존 key 삭제 (orphan 방지, best-effort).
        if warning.signed_pdf_key:
            try:
                storage_service.delete_file(warning.signed_pdf_key)
            except Exception:
                pass

        key = storage_service.upload_bytes(
            pdf_bytes, filename or "signed.pdf", "warnings", content_type="application/pdf"
        )
        now = datetime.now(timezone.utc)
        try:
            warning.signed_pdf_key = key
            warning.wet_signed_on = signed_on
            warning.wet_uploaded_by_id = uploader.id
            warning.wet_uploaded_at = now
            await db.flush()
            await db.refresh(warning)
            await db.commit()
            return warning
        except Exception:
            await db.rollback()
            raise

    async def switch_method(
        self,
        db: AsyncSession,
        *,
        warning_id: UUID,
        organization_id: UUID,
        new_method: str,
        check_store_access,
    ) -> Warning:
        """서명 방식 전환 (digital↔wet). 기존 서명/PDF 무효화 + 재서명 알림.

        전환 != 철회 (status active 유지). 무효화: 벡터 서명행 삭제 + PDF key 클리어
        (S3 파일은 보존 — 법적 기록). wet→digital 만 직원에게 재서명 알림(앱 행동 필요).
        digital→wet 은 직원이 앱에서 할 게 없어 알림 생략.
        """
        from app.services.warning_signature_service import warning_signature_service

        if new_method not in ("digital", "wet"):
            raise BadRequestError("Invalid signature method")
        warning = await self.get_warning(db, warning_id, organization_id)
        await check_store_access(warning.store_id)
        if warning.status != "active":
            raise BadRequestError("Only active warnings can change signature method")
        if warning.signature_method == new_method:
            return warning  # no-op

        had_vector = await warning_signature_service.delete_all(db, warning.id)
        had_wet = warning.signed_pdf_key is not None
        warning.signed_pdf_key = None
        warning.wet_signed_on = None
        warning.wet_uploaded_by_id = None
        warning.wet_uploaded_at = None
        warning.signature_method = new_method

        try:
            await db.flush()
            if (
                new_method == "digital"
                and (had_vector or had_wet)
                and warning.subject_user_id
            ):
                await alert_service.create_for_warning(
                    db,
                    organization_id=organization_id,
                    subject_user_id=warning.subject_user_id,
                    warning_id=warning.id,
                    title=warning.title,
                    alert_type="warning_resign",
                )
            await db.refresh(warning)
            await db.commit()
            return warning
        except Exception:
            await db.rollback()
            raise

    async def build_warning_response(
        self,
        db: AsyncSession,
        warning: Warning,
        *,
        include_ordinal: bool = False,
        include_signatures: bool = True,
        category_labels: dict[str, str] | None = None,
    ) -> dict:
        """Warning → WarningResponse dict (joined names + ref_no + category labels).

        include_ordinal=True 면 그 직원의 경고 순번(First/Second/Other 표시용)을
        계산해 담는다. 목록에선 N+1 을 피하려 생략(None), 상세에서만 True.
        include_signatures=True 면 acknowledged_at + party 별 서명을 채운다
        (employee/manager). category_labels: org 의 code→label 맵(주입 시 list N+1
        회피). None 이면 조회.
        """
        from app.services.warning_signature_service import warning_signature_service
        subject_name: str | None = None
        employee_no: str | None = None
        if warning.subject_user_id:
            subject = await db.get(User, warning.subject_user_id)
            if subject:
                subject_name = subject.full_name
                employee_no = subject.employee_no

        # 차수 = 발행 시점 스냅샷(불변). 철회/복구로 변하지 않는다.
        # backfill 전 legacy 행은 NULL → live 계산으로 폴백(점진 이행 안전).
        ordinal: int | None = None
        if include_ordinal and warning.subject_user_id:
            ordinal = warning.ordinal_snapshot
            if ordinal is None:
                ordinal = await warning_repository.subject_warning_ordinal(
                    db, warning.organization_id, warning.subject_user_id, warning.created_at
                )

        issued_by_name: str | None = None
        if warning.issued_by_id:
            issuer = await db.get(User, warning.issued_by_id)
            if issuer:
                issued_by_name = issuer.full_name

        store_name: str | None = None
        store_code: str | None = None
        if warning.store_id:
            store = await db.get(Store, warning.store_id)
            if store:
                store_name = store.name
                store_code = store.code

        # 카테고리 라벨 live resolve (삭제된 legacy 코드 포함). 미주입 시 조회.
        if category_labels is None:
            category_labels = await warning_category_repository.labels_by_code(
                db, warning.organization_id
            )
        codes = list(warning.categories or [])
        labels = {c: category_labels.get(c, c) for c in codes}

        signatures: dict[str, dict | None] = {"employee": None, "manager": None}
        if include_signatures:
            signatures = await warning_signature_service.get_signatures(db, warning.id)

        # 서명완료 파생 bool — 단일 원천. wet 은 PDF 가 양쪽 갈음, digital 은 party 행 유무.
        is_wet = warning.signature_method == "wet"
        has_wet_pdf = warning.signed_pdf_key is not None
        if is_wet:
            employee_signed = has_wet_pdf
            manager_signed = has_wet_pdf
        else:
            employee_signed = signatures.get("employee") is not None
            manager_signed = signatures.get("manager") is not None

        return {
            "id": str(warning.id),
            "ref_no": _ref_no(warning.seq),
            "status": warning.status,
            "subject_user_id": str(warning.subject_user_id) if warning.subject_user_id else None,
            "subject_name": subject_name,
            "employee_no": employee_no,
            "issued_by_id": str(warning.issued_by_id) if warning.issued_by_id else None,
            "issued_by_name": issued_by_name,
            "store_id": str(warning.store_id) if warning.store_id else None,
            "store_name": store_name,
            "title": warning.title,
            "categories": codes,
            "category_labels": labels,
            "details": warning.details,
            "corrective_action": warning.corrective_action,
            "other_text": warning.other_text,
            "deadline": warning.deadline,
            "follow_up_date": warning.follow_up_date,
            "follow_up_time": warning.follow_up_time,
            "warning_date": warning.warning_date,
            "ordinal": ordinal,
            "withdrawn_at": warning.withdrawn_at,
            "acknowledged_at": warning.acknowledged_at,
            "signatures": signatures,
            "signature_method": warning.signature_method,
            "store_code": store_code,
            "signed_pdf_present": has_wet_pdf,
            "wet_signed_on": warning.wet_signed_on,
            "wet_uploaded_at": warning.wet_uploaded_at,
            "employee_signed": employee_signed,
            "manager_signed": manager_signed,
            "created_at": warning.created_at,
            "updated_at": warning.updated_at,
        }


# 싱글턴 인스턴스
warning_service: WarningService = WarningService()
