"""EMPID 임포트 Pydantic 스키마 — preview/commit 요청·응답.

EMPID import request/response schemas for the console bulk tab
(/users/bulk/empid). Writes org_member_stores.empid only —
users.employee_no is deprecated and untouched.
"""

from pydantic import BaseModel, Field


class EmpidImportEntry(BaseModel):
    """사람×매장 1건 — preview 행."""

    store_id: str | None = None       # 매칭된 매장 UUID (null = unmatched)
    store_name: str | None = None     # 매칭된 매장 이름
    company: str                      # 파일 COMPANY 원문
    emp_id_raw: str                   # 파일 emp_id 원문 (선행 0 보존)
    emp_id: int | None = None         # 정수 정규화 값 (null = invalid)
    current_empid: int | None = None  # 현재 org_member_stores.empid
    has_assignment: bool = False      # 매장 배정 행 존재 여부
    action: str                       # same|rebind|new_assignment|unmatched_store|invalid|needs_user
    warning: str | None = None        # 경고 (그룹 스코프 충돌 등 — 블록 아님)
    dormant: bool = False             # 휴면 배정 — 번호만 기록되고 재활성화되지 않음
    person_name: str | None = None    # 파일 행의 인물 이름 (placeholder 행별 picker 라벨)


class EmpidImportPerson(BaseModel):
    """사람 1명 — preview 그룹."""

    email: str | None = None
    name: str
    user_id: str | None = None
    user_full_name: str | None = None
    entries: list[EmpidImportEntry] = []
    note: str = ""
    similar: list[str] = []   # deferred — 이름 유사 DB 유저 힌트 (표시용)
    members: list[str] = []   # placeholder — 파일 내 인물 나열 (표시용)
    similar_users: list[dict] = []  # 유저 picker 프리필 후보 {user_id, full_name, email}
    matched_by: str | None = None   # "crewid" = 파일 CREWID 로 정확 매칭됨


class EmpidImportPreviewResponse(BaseModel):
    """preview 응답 — 버킷별 사람 목록 + 카운트."""

    people: list[EmpidImportPerson] = []       # user 매칭 성공 (액션 가능)
    placeholder: list[EmpidImportPerson] = []  # 더미/공유 이메일 (리포트)
    deferred: list[EmpidImportPerson] = []     # DB 미매칭 (리포트)
    counts: dict[str, int] = {}
    excluded_rows: int = 0
    total_rows: int = 0


class EmpidImportCommitItem(BaseModel):
    """commit 1건 — (user, store) 에 empid 기입. null = 번호 삭제(배정 행 유지)."""

    user_id: str
    store_id: str
    empid: int | None = Field(default=None, ge=1)


class EmpidImportCommitRequest(BaseModel):
    """commit 요청 — 운영자가 체크한 (user, store, empid) 목록."""

    assignments: list[EmpidImportCommitItem]


class EmpidImportCommitResponse(BaseModel):
    """commit 응답 — 반영/재채번/스킵/거절 내역."""

    applied: list[dict] = []      # {user, store, empid(null=삭제), created}
    renumbered: list[dict] = []   # {user, store, old, new} — 번호를 뺏긴 기존 인원 재채번
    skipped: list[dict] = []      # {user, store, empid, reason}
    rejected: list[dict] = []     # {user_id, store_id, reason}


class EmpidRosterMember(BaseModel):
    """roster 1인 — 매장 배정 + 현재 empid + 역할/부서 (export 필터 축)."""

    user_id: str
    full_name: str
    email: str | None = None
    empid: int | None = None
    is_work_assignment: bool = True
    is_manager: bool = False
    crewid: int | None = None          # org 번호 (export crewid 컬럼 — 정확 매칭 키)
    role_name: str | None = None       # 역할 (owner/general_manager/supervisor/staff/커스텀)
    role_priority: int | None = None   # 정렬용 우선순위 (낮을수록 상위)
    department: str | None = None      # FOH/BOH (nullable)


class EmpidExportItem(BaseModel):
    """export 1행 — 콘솔에서 개별 선택된 (user, store)."""

    user_id: str
    store_id: str


class EmpidExportRequest(BaseModel):
    """사람 단위 선택 export 요청 — 필터링은 클라이언트 몫, 서버는 목록을 그대로 굽는다."""

    items: list[EmpidExportItem]
    include_email: bool = True    # false = Email 셀 공란 (재업로드 매칭 불가 — 공유용)
    include_numbers: bool = True  # false = emp_id 셀 공란 (작성용 양식)
    split_by: str = "none"        # none | store | role | band — 시트 구분(1차/2차식 배포)


class EmpidRosterStore(BaseModel):
    """roster 매장 1개 — 배정 인원 목록 (empid 오름차순)."""

    store_id: str
    store_name: str
    group_id: str | None = None
    members: list[EmpidRosterMember] = []
