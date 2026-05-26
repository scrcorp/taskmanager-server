"""Attendance kiosk 관리자 모드 세션.

매장 매니저(SV/GM/Owner)가 키오스크 설정에서 PIN 으로 관리자 모드를 활성화하면
짧은 in-memory 세션 토큰을 발급해 후속 manage API 호출 시 그것으로 인증한다.
device token 과 별개로 X-Manage-Session 헤더로 전달.

단일 서버 프로세스를 가정 (현재 EC2 1대 운영). 다중 인스턴스로 확장 시 Redis 등으로 옮긴다.
프로세스 재시작 시 세션은 모두 무효 — kiosk 가 재인증해야 함.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

SESSION_TTL_MINUTES = 30


@dataclass
class AttendanceManageSession:
    token: str
    device_id: UUID
    manager_user_id: UUID
    organization_id: UUID
    store_id: UUID
    expires_at: datetime


_sessions: dict[str, AttendanceManageSession] = {}


def create_session(
    device_id: UUID,
    manager_user_id: UUID,
    organization_id: UUID,
    store_id: UUID,
) -> AttendanceManageSession:
    token = secrets.token_urlsafe(32)
    session = AttendanceManageSession(
        token=token,
        device_id=device_id,
        manager_user_id=manager_user_id,
        organization_id=organization_id,
        store_id=store_id,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=SESSION_TTL_MINUTES),
    )
    _sessions[token] = session
    return session


def get_session(token: str | None) -> AttendanceManageSession | None:
    if not token:
        return None
    session = _sessions.get(token)
    if session is None:
        return None
    if datetime.now(timezone.utc) >= session.expires_at:
        _sessions.pop(token, None)
        return None
    return session


def revoke_session(token: str | None) -> None:
    if not token:
        return
    _sessions.pop(token, None)


def revoke_for_device(device_id: UUID) -> None:
    """device 가 매장 변경/해제될 때 그 기기의 모든 manage session 폐기."""
    to_remove = [t for t, s in _sessions.items() if s.device_id == device_id]
    for t in to_remove:
        _sessions.pop(t, None)


def _clear_all_for_tests() -> None:
    _sessions.clear()
