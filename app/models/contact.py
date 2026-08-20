"""연락처(Contacts) 관련 SQLAlchemy ORM 모델 정의.

Contacts v1 — 조직 전화번호부. staff/user 레코드와 FK 로 엮지 않는 독립 도메인.

설계 원칙 (docs/99_inbox/2026-08-14-연락처(Contacts)-기능-설계.md D1~D9,
docs/99_inbox/2026-08-15-연락처-복수매장-가시성-확장.md D1~D7):
    - org-scope: 모든 조회는 organization_id 로 격리.
    - 가시성은 **명시 모드**(visibility) + 대상 목록(contact_visibility_targets) 이다.
      대상이 비었다고 전체 공유가 되지 않는다 — 실수가 공개 방향으로 나지 않게.
      **예외는 Owner 뿐**이다 (V3). GM 의 전 매장 예외는 폐기됐다 — 대상에 없으면 안 보인다.
      작성자는 언제나 자기 연락처를 본다 (V6).
    - soft delete: deleted_at. 읽기는 항상 부모의 deleted_at IS NULL.
      자식(phones/tag_links)은 삭제하지 않는다(복구 대비).
    - 전화번호는 자식 테이블 + 정규화 컬럼(숫자만)으로 부분일치 검색 (D6).
    - 태그는 org 단위 마스터 + (org, key) UNIQUE 로 표기 흔들림 흡수 (D7).
    - 쓰기 권한 없는 열람자는 변경 신청(contact_change_requests)을 낸다 (D4).
    - 모든 상태 변경은 contact_audit_log 에 기록. 조회 API 가 없으므로(v1)
      한 행이 조인 없이 읽히도록 행위자/대상 스냅샷을 함께 저장한다 (D9).

Tables:
    - contacts: 연락처 본체
    - contact_visibility_targets: 연락처 ↔ 공개 대상 (매장/직급/개인, 제외 포함)
    - contact_phones: 번호 복수 (원본 표기 + 정규화)
    - contact_emails: 이메일 복수 (라벨 + 대표) — 전화번호와 같은 모양 (D7)
    - contact_links: 링크 복수 (라벨 + URL 원문)
    - contact_favorites: 사람별 즐겨찾기 (개인 설정 — 감사·신청 대상 아님)
    - contact_tags: org 단위 태그 마스터
    - contact_tag_links: 연락처 ↔ 태그 매핑
    - contact_change_requests: 변경 신청 (create/update/delete)
    - contact_audit_log: 변경 이력 (감사 — FK 최소화, 불변 기록 우선)
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# 가시성 모드 (확장 D1) — 모드마다 "대상 목록을 가지는가"가 정해진다.
#   organization : 조직 전체 공유. 대상 목록은 비어 있어야 한다.
#   restricted   : 지정한 대상들만. 대상이 1건 이상이어야 한다.
#                  대상 0개 + restricted 는 저장을 거부한다 (실수가 공개로 나지 않게).
CONTACT_VISIBILITY_ORGANIZATION = "organization"
CONTACT_VISIBILITY_RESTRICTED = "restricted"
CONTACT_VISIBILITIES: tuple[str, ...] = (
    CONTACT_VISIBILITY_ORGANIZATION,
    CONTACT_VISIBILITY_RESTRICTED,
)

# 공개 대상 축 (다축 가시성 V4). 대상들은 **OR 로 합쳐진다** — 고를수록 넓어진다.
#   store : 그 매장에 배정된 사람
#   role  : 그 직급인 사람 (동적 — 승진하면 따라온다, V5)
#   user  : 그 사람 본인
# position 축은 이 프로젝트에 "사용자의 직책"이 존재하지 않아 만들지 않았다
# (positions 는 매장별 근무 역할이라 사람 집합으로 풀리지 않는다).
CONTACT_TARGET_STORE = "store"
CONTACT_TARGET_ROLE = "role"
CONTACT_TARGET_USER = "user"
CONTACT_TARGET_TYPES: tuple[str, ...] = (
    CONTACT_TARGET_STORE,
    CONTACT_TARGET_ROLE,
    CONTACT_TARGET_USER,
)

# 신청 종류 / 상태 / 이력 action — 서비스·스키마가 이 상수를 단일 원천으로 쓴다.
CONTACT_REQUEST_TYPES: tuple[str, ...] = ("create", "update", "delete")
CONTACT_REQUEST_STATUSES: tuple[str, ...] = (
    "pending",
    "approved",
    "rejected",
    "cancelled",
    "superseded",
)
# 변경 경로 (대량등록/일괄수정 D1) — 이력 한 행만 봐도 "어떻게 바뀐 건지" 알 수 있게.
#   direct  : 사람이 화면에서 한 건 고침
#   batch   : 일괄 수정/대량 등록의 일부 (batch_id 로 묶인다)
#   request : 변경 신청 승인으로 반영
CONTACT_SOURCE_DIRECT = "direct"
CONTACT_SOURCE_BATCH = "batch"
CONTACT_SOURCE_REQUEST = "request"
CONTACT_SOURCES: tuple[str, ...] = (
    CONTACT_SOURCE_DIRECT,
    CONTACT_SOURCE_BATCH,
    CONTACT_SOURCE_REQUEST,
)

# restore 는 엔드포인트 없이 값만 예약 (계약 §4.7 — 스텁 금지 원칙).
CONTACT_AUDIT_ACTIONS: tuple[str, ...] = (
    "create",
    "update",
    "delete",
    "restore",
    "request_create",
    "request_approve",
    "request_reject",
    "request_cancel",
    "request_superseded",
)


class Contact(Base):
    """연락처 본체 — 조직 전화번호부의 한 건.

    Attributes:
        id: 고유 식별자 UUID
        organization_id: 소속 조직 FK (CASCADE)
        visibility: 가시성 모드 (CONTACT_VISIBILITIES). 대상은 contact_visibility_targets
        name: 이름/표시명 (필수, 검색 대상)
        company: 업체명/소속 (검색 대상)
        summary: 한 줄 요약 (검색 대상, 목록 노출)
        notes: 상세 메모 (검색 대상, 상세/펼침 전용)
        created_by: 작성자 FK (SET NULL)
        updated_by: 최종 수정자 FK (SET NULL)
        deleted_at: 소프트 삭제 일시 (NULL = 유효)
        deleted_by: 삭제자 FK (SET NULL)
        delete_reason: 삭제 사유 (D9 — 삭제 시 필수 입력값 보관)
        created_at / updated_at: 생성·수정 일시 UTC

    Indexes:
        ix_contacts_org_deleted: (organization_id, deleted_at) — 상시 조건
        ix_contacts_org_visibility: (organization_id, visibility) — 전체공유 필터/가시성 절
        ix_contacts_org_name_lower: (organization_id, lower(name)) — 기본 정렬(N9)
    """

    __tablename__ = "contacts"

    # 연락처 고유 식별자 — Contact unique identifier (UUID v4)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Organization scope for multi-tenant isolation (CASCADE)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # 가시성 모드 — CONTACT_VISIBILITIES. 대상은 contact_visibility_targets 에
    visibility: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=CONTACT_VISIBILITY_ORGANIZATION,
        server_default=CONTACT_VISIBILITY_ORGANIZATION,
    )
    # 이름/표시명 — Display name (required, searchable)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # 업체명/소속 — Company or affiliation (searchable)
    company: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    # 한 줄 요약 — Short summary shown in the list (D8, 72자 상한은 스키마에서 검증)
    summary: Mapped[Optional[str]] = mapped_column(String(72), nullable=True)
    # 상세 메모 — Long notes, detail/expand only (D8).
    # 컬럼은 Text 다. 상한 300 은 **새로 들어오는 값에만** 스키마에서 건다 —
    # 기존 memo 는 4000 까지 허용됐고, 길다는 이유로 열람·수정이 막히면 안 된다.
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 작성자 FK — Creator (SET NULL on user delete)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 최종 수정자 FK — Last editor (SET NULL)
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 소프트 삭제 일시 — Soft delete timestamp (NULL = active)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 삭제자 FK — Who soft-deleted it (SET NULL)
    deleted_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 삭제 사유 — Required at delete time (D9)
    delete_reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # 생성 일시 — Creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        # 상시 켜지는 org + soft-delete 필터 커버
        Index("ix_contacts_org_deleted", "organization_id", "deleted_at"),
        # 전체공유 필터 / 가시성 절
        Index("ix_contacts_org_visibility", "organization_id", "visibility"),
        # 기본 정렬(이름순, 대소문자 무시) — 표현식 인덱스도 모델에 선언해야 autogenerate 가 안 지운다
        Index("ix_contacts_org_name_lower", "organization_id", text("lower(name)")),
    )


class ContactVisibilityTarget(Base):
    """연락처 ↔ 공개 대상 (다축 가시성 V1~V7).

    `visibility='restricted'` 인 연락처만 행을 가진다. `organization` 이면 비어 있다.
    "행이 없음"이 전체 공유를 뜻하지 **않는다** — 전체 공유는 visibility 값으로만 표현된다.

    **포함(include) 대상은 OR 로 합쳐진다** — 고를수록 보는 사람이 늘어난다 (V4).
    그렇게 모은 후보 명단에서 **개인 단위로 뺄 수 있다**(is_excluded=True).
    제외는 `target_type='user'` 에만 쓴다 — 미리보기가 사람 명단이므로 빼는 것도 사람이다.

    대상(매장/직급/사용자)이 삭제되면 링크도 함께 사라진다(CASCADE). 그 결과 대상이
    0개가 되면 그 연락처는 Owner + 작성자에게만 보인다 — 조용히 전 조직에 공개되는 것보다
    안전한 실패 방향이다.

    Attributes:
        id: 고유 식별자 UUID
        contact_id: 연락처 FK (CASCADE)
        target_type: CONTACT_TARGET_TYPES ('store' | 'role' | 'user')
        target_id: 대상 id. FK 를 걸지 않는다 — 타입마다 참조 테이블이 달라서다.
            대신 org 소속 검증을 서비스가 하고, 대상 삭제 시 정리는 아래 트리거 대신
            서비스/마이그레이션이 책임진다(참조 무결성보다 단순함 우선).
        is_excluded: True 면 **제외** (user 타입에만 의미)
        created_at: 생성 일시 UTC

    Constraints:
        uq_contact_visibility_targets: (contact_id, target_type, target_id) UNIQUE
        ix_cvt_lookup: (target_type, target_id) — 사람 → 보이는 연락처 역방향 조회
    """

    __tablename__ = "contact_visibility_targets"

    # 링크 행 고유 식별자 — Link row identifier
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 연락처 FK — Contact side (CASCADE)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    # 대상 축 — CONTACT_TARGET_TYPES
    target_type: Mapped[str] = mapped_column(String(10), nullable=False)
    # 대상 id — 타입별 테이블(stores/roles/users)의 id. FK 없음(다형 참조)
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # 제외 여부 — True 면 후보에서 뺀다 (user 타입 전용, V4)
    is_excluded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # 생성 일시 — Creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint(
            "contact_id", "target_type", "target_id", name="uq_contact_visibility_targets"
        ),
        Index("ix_cvt_lookup", "target_type", "target_id"),
    )


class ContactPhone(Base):
    """연락처 전화번호 — 한 연락처에 여러 개 (D6).

    원본 표기(number)와 숫자만 남긴 정규화 값(number_normalized)을 함께 저장한다.
    표시는 항상 원본 표기, 검색은 양쪽 모두 대상.

    Attributes:
        id: 고유 식별자 UUID
        contact_id: 부모 연락처 FK (CASCADE — 부모 하드 삭제 시 동반 삭제)
        label: 자유 라벨 (mobile/office/... 서버는 값 검증 안 함)
        number: 사용자가 입력한 원본 표기 (표시용, 필수)
        number_normalized: 숫자만 남긴 값. 숫자가 하나도 없으면 NULL
        is_primary: 대표번호 여부 (연락처당 최대 1개 — 서비스가 보장)
        sort_order: 입력 배열 순서 (0부터)
        created_at: 생성 일시 UTC

    Indexes:
        ix_contact_phones_contact: (contact_id, sort_order)
        ix_contact_phones_normalized: (number_normalized) — 부분일치 검색
    """

    __tablename__ = "contact_phones"

    # 번호 행 고유 식별자 — Phone row identifier (외부에 의미 없음: 수정 시 delete-all + re-insert)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 부모 연락처 FK — Parent contact (CASCADE)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    # 라벨 — Free-form label (mobile / office / home / fax / ...)
    label: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    # 원본 표기 — Number exactly as entered (display source of truth)
    number: Mapped[str] = mapped_column(String(50), nullable=False)
    # 정규화 값 — Digits only (app.utils.phone.normalize_phone). NULL = 숫자 없음
    number_normalized: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # 대표번호 — Primary flag (연락처당 최대 1 true)
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # 정렬 순서 — Order within the contact (0-based, 입력 배열 순서)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # 생성 일시 — Creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("ix_contact_phones_contact", "contact_id", "sort_order"),
        # 선행 와일드카드 LIKE 에는 B-tree 이득이 제한적이나 정확/prefix 매칭에는 유효.
        # 데이터가 늘면 pg_trgm 으로 확장 (v1 비범위).
        Index("ix_contact_phones_normalized", "number_normalized"),
    )


class ContactEmail(Base):
    """연락처 이메일 — 한 연락처에 여러 개 (D7).

    전화번호(`ContactPhone`)와 **의도적으로 같은 모양**이다. 업체 하나에 Orders/Billing 처럼
    용도가 다른 주소가 흔하고, 라벨이 없으면 어느 주소인지 알 수 없다.

    Attributes:
        id: 고유 식별자 UUID
        contact_id: 부모 연락처 FK (CASCADE)
        label: 자유 라벨 (orders/billing/... 서버는 값 검증 안 함)
        address: 이메일 주소 (표시·검색 대상, 필수)
        is_primary: 대표 여부 (연락처당 최대 1개 — 서비스가 보장)
        sort_order: 입력 배열 순서 (0부터)
        created_at: 생성 일시 UTC

    Indexes:
        ix_contact_emails_contact: (contact_id, sort_order)
        ix_contact_emails_address: (address) — 검색
    """

    __tablename__ = "contact_emails"

    # 이메일 행 고유 식별자 — 외부에 의미 없음 (수정 시 delete-all + re-insert)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 부모 연락처 FK — Parent contact (CASCADE)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    # 라벨 — Free-form label (orders / billing / support / ...)
    label: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    # 주소 — Email address as entered
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    # 대표 여부 — Primary flag (연락처당 최대 1 true)
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # 정렬 순서 — Order within the contact (0-based)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # 생성 일시 — Creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("ix_contact_emails_contact", "contact_id", "sort_order"),
        Index("ix_contact_emails_address", "address"),
    )


class ContactLink(Base):
    """연락처 링크 — 한 연락처에 여러 개.

    홈페이지·주문 포털·카탈로그처럼 URL 이 여럿인 경우가 흔하다. 라벨이 있어야 무슨 링크인지 안다.

    **URL 은 입력 원문 그대로 저장한다.** 스킴이 없는 값(`order.sysco.com`)에 `https://` 를
    붙이는 것은 **여는 시점**의 클라이언트 몫이다 — 저장 때 건드리면 사용자가 적은 것과
    저장된 것이 달라지고, 되돌릴 수도 없다.

    Attributes:
        id: 고유 식별자 UUID
        contact_id: 부모 연락처 FK (CASCADE)
        label: 자유 라벨 (order portal / website / ...)
        url: 입력 원문 (표시·검색 대상, 필수)
        is_primary: 대표 여부 — 전화/이메일/링크를 통틀어 연락처당 최대 1건
        sort_order: 입력 배열 순서 (0부터)
        created_at: 생성 일시 UTC

    Indexes:
        ix_contact_links_contact: (contact_id, sort_order)
    """

    __tablename__ = "contact_links"

    # 링크 행 고유 식별자 — 외부에 의미 없음 (수정 시 delete-all + re-insert)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 부모 연락처 FK — Parent contact (CASCADE)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    # 라벨 — Free-form label (order portal / website / catalog / ...)
    label: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    # URL — Exactly as entered (scheme 보정은 여는 쪽에서)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    # 대표 여부 — 메인 연락수단은 **채널을 가로질러 연락처당 하나**다 (서비스가 보장)
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # 정렬 순서 — Order within the contact (0-based)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # 생성 일시 — Creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (Index("ix_contact_links_contact", "contact_id", "sort_order"),)


class ContactFavorite(Base):
    """즐겨찾기 — **사람마다 다른 개인 설정** (D3).

    연락처의 내용이 아니라 보는 사람의 설정이므로:
      - 조직에 공유되지 않는다 (남의 즐겨찾기는 보이지 않는다)
      - **감사 로그를 남기지 않는다** (상태 변경이 아니라 개인 취향이다)
      - 변경신청 대상이 아니다 (권한과 무관하게 누구나 자기 것을 켜고 끈다)

    지금은 연락처 전용이다. 다른 도메인에 별이 필요해지면 `user_favorites`
    (user_id, entity_type, entity_id) 로 옮긴다 — 데이터가 가볍고 참조하는 곳이 없어
    `INSERT ... SELECT` 한 번이면 끝난다.

    Attributes:
        id: 고유 식별자 UUID
        user_id: 즐겨찾기를 한 사람 FK (CASCADE — 탈퇴하면 함께 사라진다)
        contact_id: 대상 연락처 FK (CASCADE)
        created_at: 등록 일시 UTC

    Constraints:
        uq_contact_favorites: (user_id, contact_id) UNIQUE — 토글이 멱등이 되는 근거
        ix_contact_favorites_user: (user_id) — "내 즐겨찾기" 조회
    """

    __tablename__ = "contact_favorites"

    # 즐겨찾기 행 고유 식별자 — Favorite row identifier
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 사용자 FK — Owner of this favorite (CASCADE)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # 연락처 FK — Favorited contact (CASCADE)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    # 등록 일시 — Creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint("user_id", "contact_id", name="uq_contact_favorites"),
        Index("ix_contact_favorites_user", "user_id"),
    )


class ContactTag(Base):
    """태그 마스터 — org 단위로 자동 축적 (D7).

    표기 흔들림(vendor / Vendor)은 소문자 key 로 흡수하고, 표시명(name)은
    **최초 등록 표기를 유지**한다(나중에 다른 대소문자로 들어와도 안 바뀜).

    Attributes:
        id: 고유 식별자 UUID
        organization_id: 소속 조직 FK (CASCADE)
        name: 표시명 (최초 입력 표기 유지)
        key: 정규화 키 = lower(trim(name))
        created_by: 최초 생성자 FK (SET NULL)
        created_at: 생성 일시 UTC

    Constraints:
        uq_contact_tags_org_key: (organization_id, key) UNIQUE
    """

    __tablename__ = "contact_tags"

    # 태그 고유 식별자 — Tag identifier
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Organization scope (CASCADE)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # 표시명 — Display name, kept as first-seen casing
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    # 정규화 키 — lower(trim(name)), org 안에서 유일
    key: Mapped[str] = mapped_column(String(40), nullable=False)
    # 최초 생성자 FK — Creator (SET NULL)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 생성 일시 — Creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "key", name="uq_contact_tags_org_key"),
    )


class ContactTagLink(Base):
    """연락처 ↔ 태그 매핑.

    Attributes:
        id: 고유 식별자 UUID
        contact_id: 연락처 FK (CASCADE)
        tag_id: 태그 FK (CASCADE)
        created_at: 생성 일시 UTC

    Constraints:
        uq_contact_tag_links: (contact_id, tag_id) UNIQUE
        ix_contact_tag_links_tag: (tag_id) — 태그 필터 역방향 조회
    """

    __tablename__ = "contact_tag_links"

    # 링크 고유 식별자 — Link row identifier
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 연락처 FK — Contact side (CASCADE)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    # 태그 FK — Tag side (CASCADE)
    tag_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("contact_tags.id", ondelete="CASCADE"), nullable=False
    )
    # 생성 일시 — Creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint("contact_id", "tag_id", name="uq_contact_tag_links"),
        Index("ix_contact_tag_links_tag", "tag_id"),
    )


class ContactChangeRequest(Base):
    """변경 신청 — 쓰기 권한 없는 열람자가 내는 등록/수정/삭제 요청 (D4).

    payload 는 신청 원문이며 **절대 덮어쓰지 않는다**. 승인 시 실제 반영된 내용은
    applied_payload 에 따로 저장한다(원문 보존 = 추적 근거).

    Attributes:
        id: 고유 식별자 UUID
        organization_id: 소속 조직 FK (CASCADE)
        request_type: 'create' | 'update' | 'delete'
        contact_id: 대상 연락처 FK (SET NULL). NULL = 신규 등록 신청
        contact_name_snapshot: 대상 이름 스냅샷 (create 신청은 payload.name)
        payload: 신청 원문 JSONB (delete 신청은 NULL)
        applied_payload: 승인 시 실제 반영 내용 (원문과 같으면 NULL)
        reason: 신청 사유 (update/delete 신청은 입력 필수)
        status: 'pending' | 'approved' | 'rejected' | 'cancelled' | 'superseded'
        base_updated_at: 신청 시점 대상 updated_at (N5 stale 판정 기준)
        requested_by / requested_by_name / requested_at: 신청자 + 이름 스냅샷 + 시각
        resolved_by / resolved_by_name / resolved_at: 처리자 + 이름 스냅샷 + 시각
        resolution_note: 승인 note / 반려 사유(필수)
        created_at / updated_at: 생성·수정 일시 UTC

    Indexes:
        ix_ccr_org_status: (organization_id, status, requested_at) — 처리 대기 목록
        ix_ccr_requester: (requested_by, requested_at) — 내 신청 목록
        ix_ccr_contact: (contact_id, status) — 연락처별 pending 카운트/superseded 전환
    """

    __tablename__ = "contact_change_requests"

    # 신청 고유 식별자 — Change request identifier
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Organization scope (CASCADE)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # 신청 종류 — 'create' | 'update' | 'delete' (CONTACT_REQUEST_TYPES)
    request_type: Mapped[str] = mapped_column(String(10), nullable=False)
    # 대상 연락처 FK — NULL = 신규 등록 신청 (SET NULL: 하드 삭제돼도 신청 행은 남는다)
    contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )
    # 대상 이름 스냅샷 — 대상이 사라져도 무엇에 대한 신청인지 남게
    contact_name_snapshot: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    # 신청 원문 — Proposed content, never overwritten (delete 신청은 NULL)
    payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # 실제 반영 내용 — Set only when approver edited before applying (else NULL)
    applied_payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # 신청 사유 — Required for update/delete requests (D9)
    reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # 상태 — CONTACT_REQUEST_STATUSES
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, default="pending", server_default="pending"
    )
    # 신청 시점 대상 updated_at — stale 판정 기준 (N5, 차단은 안 함)
    base_updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 신청자 FK — Requester (SET NULL)
    requested_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 신청자 이름 스냅샷 — 사용자 삭제 대비
    requested_by_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    # 신청 일시 — Submitted at (UTC)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    # 처리자 FK — Approver/rejecter (SET NULL)
    resolved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 처리자 이름 스냅샷 — 사용자 삭제 대비
    resolved_by_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    # 처리 일시 — Resolved at (UTC)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 처리 메모 — 승인 note (선택) / 반려 사유 (필수)
    resolution_note: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # 생성 일시 — Creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_ccr_org_status", "organization_id", "status", "requested_at"),
        Index("ix_ccr_requester", "requested_by", "requested_at"),
        Index("ix_ccr_contact", "contact_id", "status"),
    )


class ContactAuditLog(Base):
    """변경 이력 — 상태를 바꾼 모든 행위 (D9).

    v1 은 조회 API 를 만들지 않고 DB 직접 조회로 본다. 따라서 **한 행이 조인 없이
    읽혀야 한다** — 행위자(id/이름/이메일)와 대상(id/이름)을 스냅샷으로 함께 저장한다.

    contact_id / change_request_id / actor_user_id 에 **FK 를 걸지 않는다**:
    FK 가 있으면 ON DELETE SET NULL 로 id 추적이 끊긴다. 감사 테이블은 참조 무결성보다
    불변 기록이 우선. (alembic env.py LEGACY 등록 대상 아님 — 모델에 정식 존재)

    Attributes:
        id: 고유 식별자 UUID
        organization_id: 소속 조직 FK (CASCADE) — 조회 스코프
        action: CONTACT_AUDIT_ACTIONS 중 하나 ('restore' 는 값만 예약)
        contact_id: 대상 연락처 id (FK 없음)
        contact_name: 대상 이름 스냅샷
        change_request_id: 신청 경유 건 연결 id (FK 없음)
        actor_user_id: 행위자 id (FK 없음)
        actor_name: 행위자 이름 스냅샷
        actor_email: 행위자 이메일 스냅샷 (없으면 username)
        reason: 사유 (계약 §7.1 매핑)
        source: 변경 경로 (CONTACT_SOURCES) — 직접/배치/신청 승인 구분 (D1)
        batch_id: 일괄 작업 묶음 id (FK 없음). 같은 배치의 행들이 이 값을 공유한다.
        before / after: 변경 전/후 JSONB — 변경된 필드만
        created_at: 발생 시각 UTC

    Indexes:
        ix_contact_audit_org_created: (organization_id, created_at)
        ix_contact_audit_contact: (contact_id, created_at)
    """

    __tablename__ = "contact_audit_log"

    # 이력 행 고유 식별자 — Audit row identifier
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Organization scope (CASCADE)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # 행위 종류 — CONTACT_AUDIT_ACTIONS
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    # 대상 연락처 id — FK 없음(하드 삭제돼도 값 유지)
    contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, nullable=True)
    # 대상 이름 스냅샷 — Contact name at the time of the action
    contact_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    # 신청 연결 id — FK 없음 (신청 경유 건)
    change_request_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, nullable=True)
    # 행위자 id — FK 없음
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, nullable=True)
    # 행위자 이름 스냅샷 — Actor display name at the time
    actor_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    # 행위자 이메일 스냅샷 — Actor email (fallback: username)
    actor_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # 사유 — Reason (필수/선택은 action 별로 다름, 계약 §7.1)
    reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # 변경 경로 — CONTACT_SOURCES. 행 하나만 봐도 어떻게 바뀐 건지 알 수 있게 (D1)
    source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=CONTACT_SOURCE_DIRECT,
        server_default=CONTACT_SOURCE_DIRECT,
    )
    # 배치 묶음 id — 일괄 작업의 일부임을 표시. FK 없음(감사 테이블 원칙)
    batch_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, nullable=True)
    # 변경 전 — Snapshot of changed fields only (NULL = 없음)
    before: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # 변경 후 — Snapshot of changed fields only (NULL = 없음)
    after: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # 발생 시각 — Event timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("ix_contact_audit_org_created", "organization_id", "created_at"),
        Index("ix_contact_audit_contact", "contact_id", "created_at"),
        # 배치 하나가 무엇을 바꿨는지 되짚기
        Index("ix_contact_audit_batch", "batch_id"),
    )
