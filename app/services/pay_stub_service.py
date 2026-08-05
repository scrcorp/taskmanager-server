"""Pay stub 생성/조회 서비스 — Payroll v1 Phase 4 (E4).

동결된 payroll entry 1건 → pay stub PDF 를 만들어 **파일 레지스트리**
(files + file_usages) 에 등록한다. entry 에 pdf_key 컬럼을 두지 않는다
(2026-06-24 파일관리 리빌딩 결정 — 소유 관계는 file_usages 가 단일 원천).

레지스트리 시맨틱 (app/models/file.py):
    - path UNIQUE: 1 물리파일 = 1 files 행. stub 키는 entry 기반 고정 경로
      (payroll/stubs/{period_id}/{entry_id}.pdf) 라 재생성은 같은 files 행
      재사용 + blob 덮어쓰기 = 멱등.
    - usage: owner_type='pay_stub', owner_id=entry.id. 재생성 시 usage 도
      1건 유지 (중복 부착 없음). 삭제가 필요해지면 usage 행만 지운다
      (blob 회수는 file_service.gc_orphan_files).

트랜잭션: 서비스는 flush 까지만 — commit 은 호출자(라우터) 소유.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.file import File, FileUsage
from app.models.organization import Organization, Store
from app.models.payroll import PayPeriod, PayrollEntry
from app.services.storage_service import storage_service
from app.utils.download import safe_filename
from app.utils.exceptions import DuplicateError, NotFoundError
from app.utils.pay_stub_pdf import build_pay_stub_pdf

# file_usages.owner_type — pay stub PDF 의 사용 주체 (owner_id = payroll_entries.id)
STUB_OWNER_TYPE = "pay_stub"


@dataclass
class StubContext:
    """stub 대상 entry 와 주변 컨텍스트 (confirmed 검증 통과 상태)."""

    entry: PayrollEntry
    period: PayPeriod
    store: Store
    org: Organization


class PayStubService:
    """pay stub PDF 생성/다운로드."""

    def stub_key(self, entry: PayrollEntry) -> str:
        """entry 기반 고정 상대경로 key — 재생성 시 같은 경로 덮어쓰기(멱등)."""
        return f"payroll/stubs/{entry.pay_period_id}/{entry.id}.pdf"

    def stub_filename(
        self, entry: PayrollEntry, period: PayPeriod, *, draft: bool = False
    ) -> str:
        """PayStub_{직원명}_{start}~{end}.pdf — 미확정본은 _DRAFT 접미.

        직원명은 유니코드 그대로 (한글 이름 보존) — 전송 규격은 헤더가 맡는다.
        """
        suffix = "_DRAFT" if draft else ""
        return (
            f"PayStub_{safe_filename(entry.member_name)}"
            f"_{period.start_date}~{period.end_date}{suffix}.pdf"
        )

    async def load_context(self, db: AsyncSession, entry_id: UUID) -> StubContext:
        """entry + period/store/org 로드 — 404/409 가드 포함.

        Raises:
            NotFoundError: entry 없음.
            DuplicateError(409): 소속 기간이 confirmed 가 아님 — entry 는
                confirm 시에만 생기지만 방어적으로 가드 (스텁은 동결본 전용).
        """
        entry = await db.get(PayrollEntry, entry_id)
        if entry is None:
            raise NotFoundError("Payroll entry not found")
        period = await db.get(PayPeriod, entry.pay_period_id)
        if period is None or period.status != "confirmed":
            raise DuplicateError(
                "Pay stubs are only available for confirmed pay periods — "
                "confirm the pay period first"
            )
        store = await db.get(Store, entry.store_id)
        org = await db.get(Organization, entry.organization_id)
        if store is None or org is None:
            # RESTRICT/CASCADE 정책상 도달 어려움 — 도달 시 명확히
            raise NotFoundError("Store or organization for this entry no longer exists")
        return StubContext(entry=entry, period=period, store=store, org=org)

    async def generate_stub(
        self, db: AsyncSession, entry_id: UUID, *, actor_id: UUID | None = None
    ) -> File:
        """PDF 생성 + 레지스트리 등록 (files upsert-by-path + usage 1건 유지).

        멱등: 같은 entry 로 재호출하면 blob 을 같은 key 에 덮어쓰고 기존
        files/file_usages 행을 재사용한다 (행 증식 없음).
        """
        ctx = await self.load_context(db, entry_id)
        pdf_bytes = build_pay_stub_pdf(ctx.entry, ctx.period, ctx.store, ctx.org)
        key = self.stub_key(ctx.entry)
        storage_service.put_bytes(pdf_bytes, key=key, content_type="application/pdf")

        # files: path UNIQUE — 있으면 재사용(메타 갱신), 없으면 생성
        file = await db.scalar(select(File).where(File.path == key))
        if file is None:
            file = File(
                organization_id=ctx.entry.organization_id,
                store_id=ctx.entry.store_id,
                path=key,
                file_type="document",
                mime_type="application/pdf",
                original_filename=self.stub_filename(ctx.entry, ctx.period),
                size_bytes=len(pdf_bytes),
                status="active",
                uploaded_by=actor_id,
            )
            db.add(file)
            await db.flush()  # file.id 확보
        else:
            file.size_bytes = len(pdf_bytes)
            file.mime_type = "application/pdf"
            file.original_filename = self.stub_filename(ctx.entry, ctx.period)
            file.status = "active"

        # usage: owner 당 1건 유지. 다른 files 를 가리키는 낡은 usage 는
        # usage-only 삭제 (blob 회수는 GC 몫 — 레지스트리 규칙).
        usages = (
            await db.scalars(
                select(FileUsage).where(
                    FileUsage.owner_type == STUB_OWNER_TYPE,
                    FileUsage.owner_id == ctx.entry.id,
                )
            )
        ).all()
        has_current = False
        for usage in usages:
            if usage.file_id == file.id and not has_current:
                has_current = True
            else:
                await db.delete(usage)
        if not has_current:
            db.add(
                FileUsage(
                    file_id=file.id,
                    owner_type=STUB_OWNER_TYPE,
                    owner_id=ctx.entry.id,
                )
            )
        await db.flush()
        return file

    async def get_stub_file(
        self, db: AsyncSession, entry_id: UUID
    ) -> tuple[StubContext, File] | tuple[StubContext, None]:
        """entry 의 등록된 stub File 행 조회 (없으면 None)."""
        ctx = await self.load_context(db, entry_id)
        file = await db.scalar(
            select(File)
            .join(FileUsage, FileUsage.file_id == File.id)
            .where(
                FileUsage.owner_type == STUB_OWNER_TYPE,
                FileUsage.owner_id == ctx.entry.id,
            )
        )
        return ctx, file

    async def read_stub_bytes(
        self, db: AsyncSession, entry_id: UUID
    ) -> tuple[bytes, str]:
        """다운로드용 (bytes, filename).

        Raises:
            NotFoundError: 미생성(생성 안내) 또는 blob 유실(재생성 안내).
        """
        ctx, file = await self.get_stub_file(db, entry_id)
        if file is None:
            raise NotFoundError(
                "Pay stub has not been generated for this entry yet — "
                "generate it first"
            )
        data = storage_service.read_bytes(file.path)
        if data is None:
            raise NotFoundError(
                "Pay stub file is missing from storage — regenerate the stub"
            )
        return data, self.stub_filename(ctx.entry, ctx.period)

    # ── draft stub (미확정 기간, 비영속) ─────────────────────────────

    async def generate_draft_stub(
        self, db: AsyncSession, period_id: UUID, user_id: UUID
    ) -> tuple[bytes, str]:
        """open 기간 preview 행 기반 draft stub — 저장 없이 즉석 생성.

        숫자가 확정 전이라 계속 바뀌므로 files 레지스트리에 남기지 않는다
        (매 요청 재생성). 확정 기간은 동결 entry stub 이 원천 — 409 로 안내.

        Raises:
            NotFoundError: 기간 없음 / 이 기간에 해당 직원의 payroll 데이터 없음.
            DuplicateError(409): 기간이 이미 confirmed — entry stub 사용.
        """
        from types import SimpleNamespace

        from app.services.payroll_calc_service import payroll_calc_service

        period = await db.get(PayPeriod, period_id)
        if period is None:
            raise NotFoundError("Pay period not found")
        if period.status == "confirmed":
            raise DuplicateError(
                "Period is confirmed — use the pay stub on its frozen entry"
            )
        store = await db.get(Store, period.store_id)
        org = await db.get(Organization, period.organization_id)
        if store is None or org is None:
            raise NotFoundError("Store or organization for this period no longer exists")

        # read-only 계산 — draft 다운로드가 이벤트 라이프사이클을 건드리지 않게
        rows = await payroll_calc_service.preview_period(
            db, period.store_id, period, mutate_events=False
        )
        row = next((r for r in rows if r.user_id == user_id), None)
        if row is None:
            raise NotFoundError(
                "No payroll data for this member in this period"
            )

        entry_like = SimpleNamespace(
            member_name=row.member_name,
            empid=row.empid,
            crewid=row.crewid,
            regular_minutes=row.regular_minutes,
            ot_minutes=row.ot_minutes,
            dt_minutes=row.dt_minutes,
            regular_pay=row.regular_pay,
            ot_pay=row.ot_pay,
            dt_pay=row.dt_pay,
            penalty_pay=row.penalty_pay,
            card_tips=row.card_tips,
            gross_pay=row.gross_pay,
            breakdown=row.breakdown.model_dump(mode="json"),
            calc_version=row.breakdown.calc_version,
            revision=0,
        )
        pdf_bytes = build_pay_stub_pdf(entry_like, period, store, org, draft=True)
        return pdf_bytes, self.stub_filename(entry_like, period, draft=True)


pay_stub_service = PayStubService()
