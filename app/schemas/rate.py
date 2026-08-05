"""시급 변경(rate change) Pydantic 스키마 — Payroll v1 Phase 1.

콘솔 rate-change 엔드포인트(POST/GET /console/users/{id}/rate-changes)용.
쓰기는 rate_service.record_rate_change 단일 경로를 지나며, 응답은
hourly_rate_history 행을 그대로 노출한다 (이력이 canonical).
"""

from datetime import date, datetime

from pydantic import BaseModel, Field


class RateChangeCreate(BaseModel):
    """시급 변경 등록 요청.

    Attributes:
        new_rate: 새 시급 — 0 이하면 400 (DB CHECK new_rate > 0 과 동일 규칙)
        effective_date: 적용 시작일 (생략 시 오늘 UTC). 과거/미래 자유 (스펙 R4)
        reason: 변경 사유 (콘솔 표시용 자유 텍스트)
    """

    new_rate: float  # 새 시급 — 서비스에서 > 0 검증 (400)
    effective_date: date | None = None  # 적용 시작일 (기본: 오늘 UTC)
    reason: str | None = Field(default=None, max_length=1000)  # 변경 사유


class RateChangeEntry(BaseModel):
    """시급 변경 이력 1건 (hourly_rate_history 행)."""

    id: str  # 이력 UUID 문자열
    old_rate: float | None  # 변경 전 시급 (None = 최초 기록)
    new_rate: float  # 변경 후 시급
    effective_date: date  # 적용 시작일
    applied: bool  # 적용일 도래 여부 (effective_date ≤ 오늘 UTC — 콘솔 pending 뱃지용)
    reason: str | None  # 변경 사유
    changed_by: str | None  # 변경한 사람 UUID (None = 계정 삭제됨/시스템)
    changed_by_name: str | None  # 변경한 사람 이름 (조인 값)
    created_at: datetime  # 기록 시각 UTC


class RateChangeResult(BaseModel):
    """시급 변경 등록 결과.

    같은 값 재등록(no-op)은 이력을 만들지 않으므로 recorded=False, entry=None.
    """

    recorded: bool  # 이력이 기록/갱신됐는지 (False = 동일 값 no-op)
    entry: RateChangeEntry | None  # 기록된 이력 (no-op 이면 None)
