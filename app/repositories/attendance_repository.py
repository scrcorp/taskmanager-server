"""근태 관리 레포지토리 — 근태 및 QR 코드 관련 DB 쿼리 담당.

Attendance Repository — Handles all attendance and QR code related database queries.
Provides methods for attendance CRUD, QR code management, and correction tracking.
"""

from datetime import date
from typing import Sequence
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import Attendance, AttendanceCorrection, QRCode
from app.repositories.base import BaseRepository


class AttendanceRepository(BaseRepository[Attendance]):
    """근태 기록 레포지토리.

    Attendance record repository with filtering, user-specific queries,
    and correction management.

    Extends:
        BaseRepository[Attendance]
    """

    def __init__(self) -> None:
        """레포지토리를 초기화합니다.

        Initialize the attendance repository with Attendance model.
        """
        super().__init__(Attendance)

    async def get_by_filters(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        user_id: UUID | None = None,
        work_date: date | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        status: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Attendance], int]:
        """필터 조건에 맞는 근태 기록을 페이지네이션하여 조회합니다.

        Retrieve paginated attendance records matching the given filters.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            store_id: 매장 UUID 필터, 선택 (Optional store UUID filter)
            user_id: 사용자 UUID 필터, 선택 (Optional user UUID filter)
            work_date: 근무일 필터, 선택 (Optional work date filter)
            date_from: 시작일 필터, 선택 (Optional date range start)
            date_to: 종료일 필터, 선택 (Optional date range end)
            status: 상태 필터, 선택 (Optional status filter)
            page: 페이지 번호, 1부터 시작 (Page number, 1-based)
            per_page: 페이지당 항목 수 (Items per page)

        Returns:
            tuple[Sequence[Attendance], int]: (근태 목록, 전체 개수)
                                               (List of attendances, total count)
        """
        query: Select = (
            select(Attendance)
            .where(Attendance.organization_id == organization_id)
        )

        if store_id is not None:
            query = query.where(Attendance.store_id == store_id)
        if user_id is not None:
            query = query.where(Attendance.user_id == user_id)
        if work_date is not None:
            query = query.where(Attendance.work_date == work_date)
        if date_from is not None:
            query = query.where(Attendance.work_date >= date_from)
        if date_to is not None:
            query = query.where(Attendance.work_date <= date_to)
        if status is not None:
            query = query.where(Attendance.status == status)

        query = query.order_by(Attendance.work_date.desc(), Attendance.created_at.desc())

        return await self.get_paginated(db, query, page, per_page)

    async def get_by_id_with_org(
        self,
        db: AsyncSession,
        attendance_id: UUID,
        organization_id: UUID,
    ) -> Attendance | None:
        """조직 범위 내에서 근태 기록 단건을 조회합니다.

        Retrieve a single attendance record within organization scope.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            attendance_id: 근태 UUID (Attendance UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            Attendance | None: 근태 기록 또는 None (Attendance record or None)
        """
        return await self.get_by_id(db, attendance_id, organization_id)

    async def get_user_today(
        self,
        db: AsyncSession,
        user_id: UUID,
        work_date: date,
    ) -> Attendance | None:
        """특정 사용자의 오늘 근태 기록을 조회합니다.

        Retrieve today's attendance record for a specific user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 UUID (User UUID)
            work_date: 근무일 (Work date, usually today)

        Returns:
            Attendance | None: 오늘 근태 기록 또는 None (Today's attendance or None)
        """
        query: Select = (
            select(Attendance)
            .where(Attendance.user_id == user_id)
            .where(Attendance.work_date == work_date)
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def get_user_attendances(
        self,
        db: AsyncSession,
        user_id: UUID,
        work_date: date | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Attendance], int]:
        """특정 사용자의 근태 기록 목록을 조회합니다.

        Retrieve paginated attendance records for a specific user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 UUID (User UUID)
            work_date: 근무일 필터, 선택 (Optional work date filter)
            page: 페이지 번호 (Page number)
            per_page: 페이지당 항목 수 (Items per page)

        Returns:
            tuple[Sequence[Attendance], int]: (근태 목록, 전체 개수)
        """
        query: Select = (
            select(Attendance)
            .where(Attendance.user_id == user_id)
        )

        if work_date is not None:
            query = query.where(Attendance.work_date == work_date)

        query = query.order_by(Attendance.work_date.desc(), Attendance.created_at.desc())

        return await self.get_paginated(db, query, page, per_page)

    async def create_correction(
        self,
        db: AsyncSession,
        data: dict,
    ) -> AttendanceCorrection:
        """근태 수정 이력을 생성합니다.

        Create an attendance correction record.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            data: 수정 이력 데이터 딕셔너리 (Correction data dictionary)

        Returns:
            AttendanceCorrection: 생성된 수정 이력 (Created correction record)
        """
        correction: AttendanceCorrection = AttendanceCorrection(**data)
        db.add(correction)
        await db.flush()
        await db.refresh(correction)
        return correction

    async def get_corrections(
        self,
        db: AsyncSession,
        attendance_id: UUID,
    ) -> Sequence[AttendanceCorrection]:
        """특정 근태 기록의 수정 이력을 조회합니다.

        Retrieve all correction records for a specific attendance.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            attendance_id: 근태 UUID (Attendance UUID)

        Returns:
            Sequence[AttendanceCorrection]: 수정 이력 목록 (List of corrections)
        """
        query: Select = (
            select(AttendanceCorrection)
            .where(AttendanceCorrection.attendance_id == attendance_id)
            .order_by(AttendanceCorrection.created_at.desc())
        )
        result = await db.execute(query)
        return result.scalars().all()


class QRCodeRepository(BaseRepository[QRCode]):
    """QR 코드 레포지토리.

    QR code repository for store attendance scanning.

    Extends:
        BaseRepository[QRCode]
    """

    def __init__(self) -> None:
        """레포지토리를 초기화합니다.

        Initialize the QR code repository with QRCode model.
        """
        super().__init__(QRCode)

    async def get_qr_by_store(
        self,
        db: AsyncSession,
        store_id: UUID,
    ) -> QRCode | None:
        """매장의 활성 QR 코드를 조회합니다.

        Retrieve the active QR code for a store.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            store_id: 매장 UUID (Store UUID)

        Returns:
            QRCode | None: 활성 QR 코드 또는 None (Active QR code or None)
        """
        query: Select = (
            select(QRCode)
            .where(QRCode.store_id == store_id)
            .where(QRCode.is_active == True)
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def get_qr_by_code(
        self,
        db: AsyncSession,
        code: str,
    ) -> QRCode | None:
        """QR 코드 문자열로 QR 코드를 조회합니다.

        Retrieve a QR code by its code string.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            code: QR 코드 문자열 (QR code string)

        Returns:
            QRCode | None: QR 코드 또는 None (QR code or None)
        """
        query: Select = (
            select(QRCode)
            .where(QRCode.code == code)
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def create_qr(
        self,
        db: AsyncSession,
        data: dict,
    ) -> QRCode:
        """새 QR 코드를 생성합니다.

        Create a new QR code.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            data: QR 코드 데이터 딕셔너리 (QR code data dictionary)

        Returns:
            QRCode: 생성된 QR 코드 (Created QR code)
        """
        qr: QRCode = QRCode(**data)
        db.add(qr)
        await db.flush()
        await db.refresh(qr)
        return qr

    async def deactivate_qr(
        self,
        db: AsyncSession,
        qr: QRCode,
    ) -> None:
        """QR 코드를 비활성화합니다.

        Deactivate a QR code (set is_active = false).

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            qr: 비활성화할 QR 코드 (QR code to deactivate)
        """
        qr.is_active = False
        await db.flush()

    async def deactivate_store_qr_codes(
        self,
        db: AsyncSession,
        store_id: UUID,
    ) -> None:
        """매장의 모든 활성 QR 코드를 비활성화합니다.

        Deactivate all active QR codes for a store.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            store_id: 매장 UUID (Store UUID)
        """
        query: Select = (
            select(QRCode)
            .where(QRCode.store_id == store_id)
            .where(QRCode.is_active == True)
        )
        result = await db.execute(query)
        active_qrs: Sequence[QRCode] = result.scalars().all()
        for qr in active_qrs:
            qr.is_active = False
        await db.flush()


# 싱글턴 인스턴스 — Singleton instances
attendance_repository: AttendanceRepository = AttendanceRepository()
qr_code_repository: QRCodeRepository = QRCodeRepository()
