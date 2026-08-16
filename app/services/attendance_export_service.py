"""근태 export 서비스 — 기간별 attendance 목록을 xlsx 로 변환.

Attendance xlsx export. 콘솔 Attendance 페이지의 Export 버튼이 소비한다.

원칙:
    - 급여/프리미엄/OT 관련 컬럼은 넣지 않는다 (사용자 요구 — 시간 기록만).
    - 시각 표시는 매장 타임존 벽시계 HH:MM (store.timezone → org.timezone → 기본값).
    - EMPID 는 org_member_stores.empid (그 매장 배정 행). 없으면 빈칸.
    - break 분류는 attendance_break 의 PAID/UNPAID 상수 + normalize_break_type
      경유 — 레거시(paid_short/unpaid_long) 값도 dual-read 로 인식.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Font
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import Attendance
from app.models.attendance_break import (
    PAID_BREAK_TYPES,
    AttendanceBreak,
    normalize_break_type,
)
from app.models.org_member import OrgMember, OrgMemberStore
from app.models.organization import Organization, Store
from app.models.user import User
from app.services.attendance_service import summarize_break_sessions
from app.services.payroll_export_service import workbook_bytes
from app.utils.timezone import DEFAULT_TIMEZONE

# 컬럼 스코프 고정 — 급여/프리미엄/OT 컬럼 추가 금지 (사용자 요구).
EXPORT_HEADERS: list[str] = [
    "EMPID",
    "Staff",
    "Store",
    "Date",
    "Clock In",
    "Clock Out",
    "Breaks",
    "Paid Break Min",
    "Unpaid Break Min",
    "Status",
]

_HEADER_FONT = Font(bold=True)
_COLUMN_WIDTHS: dict[str, int] = {
    "A": 8,   # EMPID
    "B": 22,  # Staff
    "C": 22,  # Store
    "D": 12,  # Date
    "E": 10,  # Clock In
    "F": 10,  # Clock Out
    "G": 42,  # Breaks
    "H": 15,  # Paid Break Min
    "I": 17,  # Unpaid Break Min
    "J": 13,  # Status
}


def _safe_zone(tz_name: str | None) -> ZoneInfo:
    """IANA 이름 → ZoneInfo. 잘못된 값이면 UTC fallback (행 하나 때문에 500 금지)."""
    try:
        return ZoneInfo(tz_name or DEFAULT_TIMEZONE)
    except Exception:
        return ZoneInfo("UTC")


def _hhmm(value: datetime | None, tz: ZoneInfo) -> str:
    """TIMESTAMPTZ → 매장 tz 벽시계 "HH:MM". None(진행 중 등)은 빈칸."""
    if value is None:
        return ""
    try:
        return value.astimezone(tz).strftime("%H:%M")
    except Exception:
        return ""


def _break_label(break_type: str) -> str:
    """break 세션 라벨 — paid 계열은 'paid 10m', unpaid 계열은 'meal'."""
    normalized = normalize_break_type(break_type)
    return "paid 10m" if normalized in PAID_BREAK_TYPES else "meal"


def format_break_sessions(breaks: list[AttendanceBreak], tz: ZoneInfo) -> str:
    """break 세션 전부를 "HH:MM–HH:MM paid 10m; HH:MM–HH:MM meal" 로 조인.

    진행 중(ended_at NULL) 세션은 종료를 "--:--" 로 표기해 존재는 남긴다.
    """
    parts: list[str] = []
    for br in breaks:
        start = _hhmm(br.started_at, tz) or "--:--"
        end = _hhmm(br.ended_at, tz) or "--:--"
        parts.append(f"{start}–{end} {_break_label(br.break_type)}")
    return "; ".join(parts)


class AttendanceExportService:
    """근태 xlsx export — 목록 엔드포인트와 동일한 스코프 규칙으로 조회해 변환."""

    async def build_export(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        date_from: date,
        date_to: date,
        store_id: UUID | None = None,
        store_ids: list[UUID] | None = None,
    ) -> bytes:
        """기간/매장 필터로 attendance 를 조회해 xlsx bytes 를 만든다.

        Args:
            db: 비동기 데이터베이스 세션
            organization_id: 조직 스코프 (필수 — cross-org 차단)
            date_from/date_to: work_date 범위 (inclusive)
            store_id: 특정 매장 필터 (선택)
            store_ids: 접근 가능 매장 목록 (None = owner 전체, [] = 결과 없음)
                — 목록 엔드포인트의 get_accessible_store_ids 결과를 그대로 받는다

        Returns:
            bytes: xlsx 파일 바이트
        """
        attendances = await self._fetch_attendances(
            db,
            organization_id=organization_id,
            date_from=date_from,
            date_to=date_to,
            store_id=store_id,
            store_ids=store_ids,
        )
        rows = await self._build_rows(db, organization_id, attendances)
        return workbook_bytes(self._assemble_workbook(rows))

    # ── 내부: 조회 ────────────────────────────────────────────────────

    async def _fetch_attendances(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        date_from: date,
        date_to: date,
        store_id: UUID | None,
        store_ids: list[UUID] | None,
    ) -> list[Attendance]:
        """목록 엔드포인트(get_by_filters)와 동일한 필터 — 페이지네이션만 없음."""
        query = select(Attendance).where(
            Attendance.organization_id == organization_id,
            Attendance.work_date >= date_from,
            Attendance.work_date <= date_to,
        )
        if store_ids is not None:
            # 접근 가능 매장 스코프 (None=owner 전체). 빈 리스트면 결과 없음.
            query = query.where(Attendance.store_id.in_(store_ids))
        if store_id is not None:
            query = query.where(Attendance.store_id == store_id)
        result = await db.execute(query)
        return list(result.scalars().all())

    async def _build_rows(
        self,
        db: AsyncSession,
        organization_id: UUID,
        attendances: list[Attendance],
    ) -> list[list]:
        """attendance → export row 리스트. Date → Store → Staff 정렬."""
        if not attendances:
            return []

        attendance_ids = [a.id for a in attendances]
        user_ids = list({a.user_id for a in attendances if a.user_id is not None})
        related_store_ids = list({a.store_id for a in attendances if a.store_id is not None})

        # breaks 일괄 로드 (N+1 방지) — started_at 오름차순
        breaks_map: dict[UUID, list[AttendanceBreak]] = {}
        br_result = await db.execute(
            select(AttendanceBreak)
            .where(AttendanceBreak.attendance_id.in_(attendance_ids))
            .order_by(AttendanceBreak.started_at.asc())
        )
        for br in br_result.scalars().all():
            breaks_map.setdefault(br.attendance_id, []).append(br)

        # 매장 이름/타임존 + org 타임존 fallback
        org_tz_result = await db.execute(
            select(Organization.timezone).where(Organization.id == organization_id)
        )
        org_tz: str | None = org_tz_result.scalar_one_or_none()
        store_info: dict[UUID, tuple[str, ZoneInfo]] = {}
        if related_store_ids:
            store_rows = await db.execute(
                select(Store.id, Store.name, Store.timezone).where(
                    Store.id.in_(related_store_ids)
                )
            )
            for sid, name, tz_name in store_rows.all():
                store_info[sid] = (name or "", _safe_zone(tz_name or org_tz))
        fallback_tz = _safe_zone(org_tz)

        # 사용자 이름
        user_names: dict[UUID, str] = {}
        if user_ids:
            user_rows = await db.execute(
                select(User.id, User.full_name).where(User.id.in_(user_ids))
            )
            user_names = {uid: (name or "") for uid, name in user_rows.all()}

        # EMPID — 그 매장 배정 행(org_member_stores.empid). 없으면 빈칸.
        empid_map: dict[tuple[UUID, UUID], int] = {}
        if user_ids and related_store_ids:
            empid_rows = await db.execute(
                select(OrgMember.user_id, OrgMemberStore.store_id, OrgMemberStore.empid)
                .join(OrgMemberStore, OrgMemberStore.org_member_id == OrgMember.id)
                .where(
                    OrgMember.organization_id == organization_id,
                    OrgMember.user_id.in_(user_ids),
                    OrgMemberStore.store_id.in_(related_store_ids),
                    OrgMemberStore.empid.is_not(None),
                )
            )
            empid_map = {(r.user_id, r.store_id): r.empid for r in empid_rows.all()}

        rows: list[list] = []
        for a in attendances:
            store_name, tz = store_info.get(a.store_id, ("", fallback_tz))
            staff_name = user_names.get(a.user_id, "") if a.user_id else ""
            breaks = breaks_map.get(a.id, [])
            paid_min, unpaid_min, _overage, _items = summarize_break_sessions(breaks)
            empid = (
                empid_map.get((a.user_id, a.store_id))
                if a.user_id and a.store_id
                else None
            )
            rows.append([
                empid if empid is not None else "",
                staff_name,
                store_name,
                a.work_date.isoformat(),
                _hhmm(a.clock_in, tz),
                _hhmm(a.clock_out, tz),
                format_break_sessions(breaks, tz),
                paid_min,
                unpaid_min,
                a.status,
            ])

        # Date → Store → Staff 정렬 (컬럼 인덱스: 3=Date, 2=Store, 1=Staff)
        rows.sort(key=lambda r: (r[3], r[2], r[1]))
        return rows

    # ── 내부: 워크북 조립 ─────────────────────────────────────────────

    def _assemble_workbook(self, rows: list[list]) -> Workbook:
        wb = Workbook()
        ws = wb.active
        ws.title = "Attendance"
        ws.append(EXPORT_HEADERS)
        for cell in ws[1]:
            cell.font = _HEADER_FONT
        for col, width in _COLUMN_WIDTHS.items():
            ws.column_dimensions[col].width = width
        for row in rows:
            ws.append(row)
        ws.freeze_panes = "A2"
        return wb


attendance_export_service = AttendanceExportService()
