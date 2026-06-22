"""경고 서명 서비스 — Warning confirm+sign 비즈니스 로직.

Warning Signature Service — party 별 서명 적용(upsert) + 조회 + 저장 서명 갱신.

핵심 규칙 (security-critical):
    - 신원 바인딩(identity binding):
        · 앱 셀프사인(allow_on_behalf=False): 서명은 본인만.
          employee → signer == subject_user_id, manager → signer == issued_by_id.
          아니면 403 (Owner/super-owner 라도 예외 없음).
        · 콘솔 온-디바이스 캡처(allow_on_behalf=True, warnings:sign 권한): 한 기기에서
          party 본인이 그 자리에 직접 서명. 일치 검사는 면제하되 서명 명의
          (signer_user_id)는 항상 party 본인으로 박제하고, 실제 조작 계정은
          captured_by_user_id 에 따로 남긴다(감사). required signer 미지정은 거부.
    - 스냅샷: signature_strokes 는 적용 순간 박제. users.signature_strokes 를
      나중에 바꿔도 과거 warning_signatures 행은 불변 (징계 기록 보존).
    - 상태: status=='active' + 비삭제 경고만 서명 가능. 아니면 400.
    - party 당 1개. 재서명은 같은 (warning, party) 행 upsert (strokes/시각/method 갱신).
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.warning import Warning
from app.models.warning_signature import WarningSignature
from app.utils.exceptions import BadRequestError, ForbiddenError

# party 상수
PARTY_EMPLOYEE = "employee"
PARTY_MANAGER = "manager"


class WarningSignatureService:
    """경고 서명 서비스 — sign(upsert) + get_signatures + 저장 서명 get/set."""

    def _required_signer_id(self, warning: Warning, party: str) -> UUID | None:
        """party 별 유일하게 허용되는 서명자 user_id (대리 금지의 핵심).

        employee → subject_user_id, manager → issued_by_id. 그 외 party 는 None.
        """
        if party == PARTY_EMPLOYEE:
            return warning.subject_user_id
        if party == PARTY_MANAGER:
            return warning.issued_by_id
        return None

    async def sign(
        self,
        db: AsyncSession,
        *,
        warning: Warning,
        party: str,
        signer: User,
        strokes_payload: dict,
        method: str = "drawn",
        save_as_default: bool = False,
        allow_on_behalf: bool = False,
    ) -> WarningSignature:
        """party 서명 적용(upsert). 신원/상태 검증 후 스냅샷 박제.

        Identity gate:
            기본(allow_on_behalf=False, 앱 셀프사인)은 signer.id 가 party 의 required
            signer (employee=subject, manager=issuer)와 정확히 일치해야 한다 — 대리 금지.
            allow_on_behalf=True (콘솔 온-디바이스 캡처)면 일치 검사를 건너뛴다: party
            본인이 다른 사람의 로그인 세션(기기)으로 그 자리에서 서명하는 경우. 이때도
            서명 명의(signer_user_id)는 항상 party 본인(required_id)으로 박제하고,
            실제 조작 계정은 captured_by_user_id 에 따로 기록한다. required_id 미지정
            (None — 발행자/대상 없음)은 양쪽 모두 거부.

        State gate:
            status=='active' + deleted_at IS NULL 만. 아니면 400.

        save_as_default 는 signer 가 곧 party 본인일 때만 적용 (남의 서명을 조작 계정의
        기본 서명으로 저장하지 않는다). 콘솔 온-디바이스 캡처(타인)는 draw-only.
        """
        if party not in (PARTY_EMPLOYEE, PARTY_MANAGER):
            raise BadRequestError("Invalid signature party")

        # 상태 게이트 — active + 비삭제만.
        if warning.deleted_at is not None or warning.status != "active":
            raise BadRequestError("Only active warnings can be signed")

        # 신원 게이트. required_id 미지정(발행자/대상 없음)은 항상 거부.
        required_id = self._required_signer_id(warning, party)
        if required_id is None:
            raise ForbiddenError(
                "This warning has no designated "
                f"{party} to sign for"
            )
        # 앱 셀프사인 경로는 본인 강제(대리 금지). 콘솔 온-디바이스는 게이트 면제.
        if not allow_on_behalf and signer.id != required_id:
            raise ForbiddenError(
                "You are not authorized to sign this warning as "
                f"the {party}"
            )

        # 서명 명의는 party 본인, 캡처 계정은 실제 조작자. 셀프사인이면 둘이 동일.
        is_self = signer.id == required_id

        # upsert — 같은 (warning, party) 행이 있으면 갱신, 없으면 생성.
        from datetime import datetime, timezone

        existing = (
            await db.execute(
                select(WarningSignature).where(
                    WarningSignature.warning_id == warning.id,
                    WarningSignature.party == party,
                )
            )
        ).scalar_one_or_none()

        now = datetime.now(timezone.utc)
        try:
            if existing is None:
                sig = WarningSignature(
                    warning_id=warning.id,
                    party=party,
                    signer_user_id=required_id,
                    captured_by_user_id=signer.id,
                    signed_at=now,
                    method=method,
                    signature_strokes=strokes_payload,
                )
                db.add(sig)
            else:
                existing.signer_user_id = required_id
                existing.captured_by_user_id = signer.id
                existing.signed_at = now
                existing.method = method
                existing.signature_strokes = strokes_payload
                sig = existing

            # 저장 서명 갱신 (옵션) — 본인 셀프사인일 때만. 스냅샷과 독립적인 별개 기록.
            if save_as_default and is_self:
                signer.signature_strokes = strokes_payload

            await db.flush()
            await db.refresh(sig)
            await db.commit()
            return sig
        except Exception:
            await db.rollback()
            raise

    async def get_signatures(
        self, db: AsyncSession, warning_id: UUID
    ) -> dict[str, dict | None]:
        """경고의 party 별 서명 묶음 — {"employee": ...|None, "manager": ...|None}.

        각 값은 signer_name resolve 포함한 dict (SignatureInfo 형태) 또는 None.
        """
        rows = (
            await db.execute(
                select(WarningSignature).where(
                    WarningSignature.warning_id == warning_id
                )
            )
        ).scalars().all()

        out: dict[str, dict | None] = {PARTY_EMPLOYEE: None, PARTY_MANAGER: None}
        for sig in rows:
            signer_name: str | None = None
            if sig.signer_user_id:
                signer = await db.get(User, sig.signer_user_id)
                if signer:
                    signer_name = signer.full_name
            # 캡처 계정 — 명의(signer)와 다를 때만 의미 있음(온-디바이스). 셀프사인이면 동일.
            captured_by_name: str | None = None
            captured_by_id = sig.captured_by_user_id
            if captured_by_id and captured_by_id != sig.signer_user_id:
                captured_by = await db.get(User, captured_by_id)
                if captured_by:
                    captured_by_name = captured_by.full_name
            out[sig.party] = {
                "signer_user_id": str(sig.signer_user_id) if sig.signer_user_id else None,
                "signer_name": signer_name,
                "captured_by_user_id": str(captured_by_id) if captured_by_id else None,
                "captured_by_name": captured_by_name,
                "signed_at": sig.signed_at,
                "method": sig.method,
                "signature_strokes": sig.signature_strokes,
            }
        return out

    async def delete_all(self, db: AsyncSession, warning_id: UUID) -> int:
        """경고의 모든 party 벡터 서명 행 삭제 (방식 전환 시 무효화).

        커밋은 호출자(전환 트랜잭션)가 한다. 삭제된 행 수 반환.
        PDF/파일은 여기서 손대지 않는다(보존 — 법적 기록).
        """
        from sqlalchemy import delete as sa_delete

        result = await db.execute(
            sa_delete(WarningSignature).where(
                WarningSignature.warning_id == warning_id
            )
        )
        return result.rowcount or 0

    async def has_employee_signature(
        self, db: AsyncSession, warning_id: UUID
    ) -> bool:
        """그 경고에 employee party 서명 행이 있는지 (unsigned-count 용)."""
        row = (
            await db.execute(
                select(WarningSignature.id).where(
                    WarningSignature.warning_id == warning_id,
                    WarningSignature.party == PARTY_EMPLOYEE,
                )
            )
        ).scalar_one_or_none()
        return row is not None

    # ====================================================================
    # 저장 서명 (users.signature_strokes) — employee + manager 공용 재사용 템플릿
    # ====================================================================

    def get_saved_signature(self, user: User) -> dict | None:
        """유저의 저장 서명 ({strokes, aspect}) 또는 None."""
        return user.signature_strokes

    async def set_saved_signature(
        self, db: AsyncSession, user: User, strokes_payload: dict
    ) -> dict:
        """유저의 저장 서명을 설정/갱신하고 저장된 값을 반환."""
        try:
            user.signature_strokes = strokes_payload
            await db.flush()
            await db.commit()
            return strokes_payload
        except Exception:
            await db.rollback()
            raise


# 싱글턴 인스턴스
warning_signature_service: WarningSignatureService = WarningSignatureService()
