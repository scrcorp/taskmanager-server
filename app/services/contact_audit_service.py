"""연락처 변경 이력 — **유일한 기록 진입점** (D9).

계약 §7 (docs/99_inbox/2026-08-14-연락처-API계약.md):
    - 라우터/다른 서비스에서 `ContactAuditLog` 를 직접 INSERT 하지 않는다.
      상태를 바꾼 모든 행위는 `contact_audit_service.record(...)` 를 거친다
      (attendance_timeline 모듈 관례와 동일).
    - v1 은 이력 **조회 API 를 만들지 않는다** — DB 직접 조회로 본다.
      따라서 한 행이 조인 없이 읽혀야 한다: 행위자(id/이름/이메일)와
      대상(id/이름)을 그 시점 스냅샷으로 함께 저장한다.
    - before/after 는 **변경된 필드만** 담는다.

commit 은 하지 않는다 — 호출하는 서비스 메서드가 같은 트랜잭션에서 커밋한다
(승인 1건이 audit 2행 + 실제 반영을 원자적으로 남겨야 하기 때문).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import (
    CONTACT_AUDIT_ACTIONS,
    CONTACT_SOURCE_DIRECT,
    CONTACT_SOURCES,
    ContactAuditLog,
)
from app.models.user import User


def contact_snapshot(
    *,
    name: str | None,
    company: str | None,
    summary: str | None,
    notes: str | None,
    visibility: str,
    targets: list[dict[str, Any]],
    excluded_users: list[dict[str, Any]],
    phones: list[dict[str, Any]],
    emails: list[dict[str, Any]],
    links: list[dict[str, Any]],
    tags: list[str],
) -> dict[str, Any]:
    """이력 before/after 에 쓰는 연락처 스냅샷 (계약 §7.2 + 개정 §0-A 형태).

    targets 에 **이름까지** 넣는 이유 — 대상이 삭제·개명돼도 이력이 그대로 읽혀야
    한다. 이력 조회 UI 가 없어 DB 에서 한 행만 읽으므로 id 목록만 남기면 못 읽는다.
    """
    return {
        "name": name,
        "company": company,
        "summary": summary,
        "notes": notes,
        "visibility": visibility,
        "targets": targets,
        "excluded_users": excluded_users,
        "phones": phones,
        "emails": emails,
        "links": links,
        "tags": tags,
    }


def diff_snapshots(
    before: dict[str, Any], after: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """두 스냅샷에서 **달라진 키만** 뽑아 (before, after) 쌍으로 돌려준다.

    phones / tags 는 배열 전체가 하나의 키로 취급된다 — 하나라도 다르면 전체 배열이
    양쪽에 들어간다(계약 §7.2).
    """
    changed_before: dict[str, Any] = {}
    changed_after: dict[str, Any] = {}
    for key in after:
        if before.get(key) != after.get(key):
            changed_before[key] = before.get(key)
            changed_after[key] = after.get(key)
    return changed_before, changed_after


class ContactAuditService:
    """연락처 이력 기록 서비스 — record() 하나만 공개한다."""

    async def record(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        action: str,
        actor: User | None,
        contact_id: UUID | None = None,
        contact_name: str | None = None,
        change_request_id: UUID | None = None,
        reason: str | None = None,
        source: str = CONTACT_SOURCE_DIRECT,
        batch_id: UUID | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
    ) -> ContactAuditLog:
        """이력 1행을 남긴다 (commit 은 호출자 몫).

        Args:
            organization_id: 조회 스코프 org.
            action: CONTACT_AUDIT_ACTIONS 중 하나. 벗어나면 ValueError
                (오타를 런타임 조용한 실패가 아니라 즉시 터뜨린다).
            actor: 행위자. 이름/이메일을 스냅샷으로 복사한다(없으면 username).
            contact_id / contact_name: 대상 연락처 id + 이름 스냅샷.
            change_request_id: 신청 경유 건 연결 id.
            reason: 사유 (계약 §7.1 매핑 — 필수/선택은 호출부에서 이미 검증됨).
            source: 변경 경로 (direct/batch/request). 오타는 즉시 ValueError.
            batch_id: 일괄 작업 묶음 id. 같은 배치의 행들이 공유한다.
            before / after: 변경 전/후 (변경된 필드만).

        Returns:
            생성된 ContactAuditLog (flush 까지 완료).
        """
        if action not in CONTACT_AUDIT_ACTIONS:
            raise ValueError(f"Unknown contact audit action: {action!r}")
        if source not in CONTACT_SOURCES:
            raise ValueError(f"Unknown contact audit source: {source!r}")

        row = ContactAuditLog(
            organization_id=organization_id,
            action=action,
            contact_id=contact_id,
            contact_name=contact_name,
            change_request_id=change_request_id,
            actor_user_id=actor.id if actor else None,
            actor_name=actor.full_name if actor else None,
            # 이메일이 없는 계정이 있어 username 으로 대체 (계약 §7.2)
            actor_email=(actor.email or actor.username) if actor else None,
            reason=reason,
            source=source,
            batch_id=batch_id,
            before=before,
            after=after,
        )
        db.add(row)
        await db.flush()
        return row


contact_audit_service = ContactAuditService()
