"""기본 CRUD 레포지토리 — 모든 레포지토리의 부모 클래스.

Base CRUD Repository — Parent class for all domain repositories.
Provides generic Create, Read, Update, Delete operations with organization scoping.

Usage:
    class BrandRepository(BaseRepository[Brand]):
        def __init__(self) -> None:
            super().__init__(Brand)
"""

from typing import Any, Generic, Sequence, TypeVar
from uuid import UUID

from sqlalchemy import Select, func, select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import Base

# 제네릭 타입 변수 — SQLAlchemy 모델을 나타냄
# Generic type variable representing a SQLAlchemy model
ModelType = TypeVar("ModelType", bound=Base)


class BaseRepository(Generic[ModelType]):
    """제네릭 CRUD 레포지토리.

    Generic CRUD repository providing common database operations.
    All queries are scoped by organization_id when the model supports it.

    Attributes:
        model: SQLAlchemy 모델 클래스 (The SQLAlchemy model class)
    """

    def __init__(self, model: type[ModelType]) -> None:
        """레포지토리를 초기화합니다.

        Initialize the repository with a model class.

        Args:
            model: 이 레포지토리가 관리할 SQLAlchemy 모델 클래스
                   (SQLAlchemy model class this repository manages)
        """
        self.model: type[ModelType] = model

    async def get_by_id(
        self,
        db: AsyncSession,
        record_id: UUID,
        organization_id: UUID | None = None,
    ) -> ModelType | None:
        """ID로 단일 레코드를 조회합니다.

        Retrieve a single record by its UUID.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            record_id: 조회할 레코드의 UUID (UUID of the record to retrieve)
            organization_id: 조직 범위 필터, None이면 조직 필터 미적용
                             (Organization scope filter; None skips org filtering)

        Returns:
            ModelType | None: 조회된 레코드 또는 None (Found record or None)
        """
        query: Select = select(self.model).where(self.model.id == record_id)

        # 모델에 organization_id 컬럼이 있고, 필터가 제공된 경우 조직 범위 적용
        # Apply org scope if model has organization_id and filter is provided
        if organization_id is not None and hasattr(self.model, "organization_id"):
            query = query.where(self.model.organization_id == organization_id)

        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def get_all(
        self,
        db: AsyncSession,
        organization_id: UUID | None = None,
        filters: dict[str, Any] | None = None,
        order_by: Any | None = None,
    ) -> Sequence[ModelType]:
        """조건에 맞는 모든 레코드를 조회합니다.

        Retrieve all records matching the given filters.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 범위 필터 (Organization scope filter)
            filters: 추가 필터 딕셔너리 {'컬럼명': 값}
                     (Additional filter dict {'column_name': value})
            order_by: 정렬 기준 컬럼 (Column to order by)

        Returns:
            Sequence[ModelType]: 조회된 레코드 목록 (List of matching records)
        """
        query: Select = select(self.model)

        if organization_id is not None and hasattr(self.model, "organization_id"):
            query = query.where(self.model.organization_id == organization_id)

        # 동적 필터 적용 — Dynamic filter application
        if filters:
            for column_name, value in filters.items():
                if hasattr(self.model, column_name) and value is not None:
                    query = query.where(getattr(self.model, column_name) == value)

        if order_by is not None:
            query = query.order_by(order_by)

        result = await db.execute(query)
        return result.scalars().all()

    async def get_paginated(
        self,
        db: AsyncSession,
        query: Select,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[ModelType], int]:
        """페이지네이션이 적용된 레코드 목록을 조회합니다.

        Retrieve a paginated list of records.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            query: 기본 SELECT 쿼리 (Base SELECT query)
            page: 현재 페이지 번호, 1부터 시작 (Current page number, 1-based)
            per_page: 페이지당 레코드 수 (Number of records per page)

        Returns:
            tuple[Sequence[ModelType], int]: (레코드 목록, 전체 개수)
                                             (List of records, total count)
        """
        # 전체 카운트 쿼리 — Total count query
        count_query: Select = select(func.count()).select_from(query.subquery())
        total: int = (await db.execute(count_query)).scalar() or 0

        # 오프셋 계산 및 페이지 적용 — Calculate offset and apply pagination
        offset: int = (page - 1) * per_page
        result = await db.execute(query.offset(offset).limit(per_page))
        items: Sequence[ModelType] = result.scalars().all()

        return items, total

    async def create(
        self,
        db: AsyncSession,
        obj_data: dict[str, Any],
    ) -> ModelType:
        """새 레코드를 생성합니다.

        Create a new record in the database.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            obj_data: 생성할 레코드의 데이터 딕셔너리
                      (Dictionary of data for the new record)

        Returns:
            ModelType: 생성된 레코드 (The created record)
        """
        db_obj: ModelType = self.model(**obj_data)
        db.add(db_obj)
        await db.flush()
        await db.refresh(db_obj)
        return db_obj

    async def update(
        self,
        db: AsyncSession,
        record_id: UUID,
        update_data: dict[str, Any],
        organization_id: UUID | None = None,
    ) -> ModelType | None:
        """기존 레코드를 업데이트합니다.

        Update an existing record by its UUID.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            record_id: 업데이트할 레코드의 UUID (UUID of the record to update)
            update_data: 업데이트할 필드와 값의 딕셔너리
                         (Dictionary of fields and values to update)
            organization_id: 조직 범위 필터 (Organization scope filter)

        Returns:
            ModelType | None: 업데이트된 레코드 또는 None (Updated record or None)
        """
        # 먼저 레코드 존재 여부 확인 — First verify record exists
        db_obj: ModelType | None = await self.get_by_id(db, record_id, organization_id)
        if db_obj is None:
            return None

        # Pydantic exclude_unset으로 전달된 필드만 업데이트 (None 값도 허용)
        # Update all fields passed via exclude_unset (allows setting to None)
        for field, value in update_data.items():
            if hasattr(db_obj, field):
                setattr(db_obj, field, value)

        await db.flush()
        await db.refresh(db_obj)
        return db_obj

    async def delete(
        self,
        db: AsyncSession,
        record_id: UUID,
        organization_id: UUID | None = None,
    ) -> bool:
        """레코드를 삭제합니다.

        Delete a record by its UUID.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            record_id: 삭제할 레코드의 UUID (UUID of the record to delete)
            organization_id: 조직 범위 필터 (Organization scope filter)

        Returns:
            bool: 삭제 성공 여부 (Whether the deletion was successful)
        """
        db_obj: ModelType | None = await self.get_by_id(db, record_id, organization_id)
        if db_obj is None:
            return False

        await db.delete(db_obj)
        await db.flush()
        return True

    async def exists(
        self,
        db: AsyncSession,
        filters: dict[str, Any],
    ) -> bool:
        """주어진 조건에 일치하는 레코드가 존재하는지 확인합니다.

        Check if a record matching the given filters exists.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            filters: 검색 조건 딕셔너리 (Filter criteria dictionary)

        Returns:
            bool: 레코드 존재 여부 (Whether a matching record exists)
        """
        query: Select = select(func.count()).select_from(self.model)
        for column_name, value in filters.items():
            if hasattr(self.model, column_name):
                query = query.where(getattr(self.model, column_name) == value)

        count: int = (await db.execute(query)).scalar() or 0
        return count > 0
