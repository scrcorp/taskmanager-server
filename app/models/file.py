"""범용 파일 레지스트리 + 사용처(usage) SQLAlchemy ORM 모델.

Universal file registry + usage (junction) models.

설계:
    - `files`: **순수 레지스트리**. 1 물리파일 = 1 행 (path UNIQUE). 파일이 "무엇인지"만 안다
      (경로/타입/크기/메타). 누가/어디서 쓰는지는 모른다.
    - `file_usages`: **중앙 usage 테이블**. "이 파일이 어디서 쓰이나"를 한 곳에서 관리.
      한 files 행을 여러 file_usages 가 가리킬 수 있다 = **재사용(복사 없음)**.
      도메인은 `owner_type` 만 바꿔 그대로 쓴다 (checklist='cl_item', 향후 task/issue/profile…).

정책:
    - 삭제: usage 행만 지운다(한 줄 삭제). blob 은 안 건드린다.
    - blob 회수: 비동기 GC 가 "어떤 usage 도 없는 files"(NOT EXISTS)를 쓸어담아 blob+행 삭제.
      → refcount 를 삭제 경로에 인라인으로 두지 않는다. usage 행 자체가 진실이라 카운터 드리프트 없음.
    - path: 상대경로(key)만. 절대 URL 금지(resolve_url 로 런타임 변환) — Decision #7.
    - metadata(Python 속성 file_metadata): tri-state JSONB (NULL=미추출 / {}=빈값 / {...}=존재).
      photo 면 EXIF + captured_at + capture_source + dims 등. 썸네일/파생본은 저장 안 하고
      path 에서 규칙으로 유도(storage_service.thumb_key).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, String, DateTime, ForeignKey, Uuid, Integer, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class File(Base):
    """범용 파일 레지스트리 — 1 물리파일 = 1 행 (path UNIQUE).

    Pure file registry. Knows only what the file IS, not who uses it.

    Attributes:
        id: 고유 식별자 (Unique identifier)
        organization_id: 바이트 소유 조직 FK, 개인 파일이면 NULL (owner org of the bytes, nullable/portable)
        store_id: 매장 FK, nullable
        path: 상대경로(key), **UNIQUE** (relative storage key, one row per blob)
        file_type: 거친 분류 photo/video/document
        mime_type: 정확한 포맷 (e.g. image/webp)
        original_filename: 업로드 원본 파일명
        size_bytes: 바이트 크기 (display/accounting/integrity)
        status: active/deleted (GC 대상 표시)
        uploaded_by: 업로더 FK (SET NULL on user delete)
        file_metadata: metadata JSONB tri-state (NULL=미추출 / {}=빈값 / {...}=존재)
        created_at: 서버 수신 시각 — 신뢰 앵커 (server received timestamp, trust anchor)
        updated_at: 갱신 시각
    """

    __tablename__ = "files"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True
    )
    store_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="SET NULL"), nullable=True
    )

    path: Mapped[str] = mapped_column(String(500), nullable=False)
    file_type: Mapped[str] = mapped_column(String(20), nullable=False, default="photo")
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")

    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # 컬럼명은 metadata, Python 속성은 file_metadata (SQLAlchemy 예약어 회피).
    file_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_files_path", "path", unique=True),          # 1 blob = 1 행
        Index("ix_files_org_store", "organization_id", "store_id"),
        Index("ix_files_status", "status"),
        Index("ix_files_uploaded_by", "uploaded_by"),
    )


class FileUsage(Base):
    """파일 사용처 — "이 파일이 어디서 쓰이나"의 중앙 기록 (junction).

    One row per (file, owner) usage. Multiple usages may point at one `files` row
    (= reuse without copying the blob).

    Attributes:
        id: 고유 식별자
        file_id: 가리키는 files 행 FK (delete file → usages cascade)
        owner_type: 사용 주체 종류 — 'cl_item' (향후 task/issue/profile…)
        owner_id: 사용 주체 id (polymorphic, FK 없음 — checklist 면 cl_instance_items.id)
        context: 도메인 하위맥락 — checklist: submission/review/chat (nullable)
        context_id: 하위맥락 대상 id — submission/review_log/message id (nullable)
        sort_order: 표시 순서
        created_at: usage 부착 시각
    """

    __tablename__ = "file_usages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("files.id", ondelete="CASCADE"), nullable=False
    )
    owner_type: Mapped[str] = mapped_column(String(40), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    context: Mapped[str | None] = mapped_column(String(20), nullable=True)  # submission | review | chat
    context_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("ix_file_usages_owner", "owner_type", "owner_id"),
        Index("ix_file_usages_file_id", "file_id"),
        Index("ix_file_usages_context", "context", "context_id"),
    )

    # async 안전 + N+1 회피 위해 selectin.
    file = relationship("File", lazy="selectin")
