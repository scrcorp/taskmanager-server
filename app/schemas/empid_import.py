"""EMPID 임포트 Pydantic 스키마 — preview/commit 요청·응답.

EMPID import request/response schemas for the console bulk tab
(/users/bulk/empid). Writes org_member_stores.empid only —
users.employee_no is deprecated and untouched.
"""

from typing import Literal

from pydantic import BaseModel, Field

from app.models.org_member import EMPID_KIND_SEQUENCE


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
    corp_abr: str | None = None       # 파일 매장 코드 원문 (unmatched 매핑 키·표시용)
    # 그룹 스코프 매칭 — needs_store/needs_user 의 매장 picker 옵션
    group_id: str | None = None
    group_name: str | None = None
    group_stores: list[dict] | None = None  # [{store_id, store_name}]
    # 매장→그룹 승격 행이면 파일 corp 가 지목했던 매장 — picker 프리필
    hint_store_id: str | None = None


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
    matched_by: str | None = None   # "crewid" = CREWID 정확 매칭 / "name" = 이름 자동 매칭(검토 대상)


class EmpidReconChange(BaseModel):
    """매칭된 사람의 매장별 번호 전이."""

    store_id: str | None = None    # needs_store(매장 미정)면 null
    store_name: str | None = None
    current: int | None = None     # 현재 번호 (없으면 null)
    new: int                       # 파일 번호 (커밋 시 이 값)
    pending_store: bool = False    # 매장 선택 대기(needs_store) 여부


class EmpidReconMatched(BaseModel):
    """파일↔HTM 매칭된 사람 1명."""

    user_id: str
    name: str
    changes: list[EmpidReconChange] = []


class EmpidReconHtmPerson(BaseModel):
    """HTM 에 등록돼 있는데 파일이 못 덮은 사람 — 번호 직접 지정 대상."""

    user_id: str
    name: str
    store_id: str
    store_name: str
    current_empid: int | None = None  # 번호 없는 배정자도 포함 (지정 필요 대상)


class EmpidReconFileRow(BaseModel):
    """파일에만 있는 행 — deferred/placeholder 에서 사람을 골라 해소."""

    empid: int
    name: str


class EmpidReconciliationScope(BaseModel):
    """스코프(그룹/매장)별 사람 단위 대조."""

    scope: str          # "group" | "store"
    id: str
    name: str
    matched: list[EmpidReconMatched] = []
    htm_unmatched: list[EmpidReconHtmPerson] = []
    file_unmatched: list[EmpidReconFileRow] = []


class EmpidSavedAlias(BaseModel):
    """자동 적용된 저장 별칭 1건 — "저장된 매핑" 표시·수정 UI 재료."""

    key: str                 # 정규화 라벨
    target_id: str           # 저장된 대상 (매장 또는 그룹 id)
    store_id: str | None = None  # 매장으로 확정된 경우만
    store_name: str          # 매장명 또는 그룹명


class EmpidUnmatchedStore(BaseModel):
    """매장 미매칭 원문 1건 — 콘솔 매핑 UI 재료.

    key 를 preview 요청의 store_overrides 키로 그대로 되돌려 보내면 된다.
    """

    key: str                    # 정규화 키 (오버라이드 키)
    company: str                # 파일 COMPANY 원문
    corp_abr: str | None = None  # 파일 매장 코드 원문 (있으면 이게 키의 출처)
    rows: int = 0               # 해당 키의 행 수


class EmpidBandCount(BaseModel):
    """업로드 번호의 백 단위 분포 1칸 — 대역 밖 번호를 눈에 띄게 하는 재료."""

    band: str   # "1000-1099"
    lo: int
    hi: int
    count: int


class EmpidImportPreviewResponse(BaseModel):
    """preview 응답 — 버킷별 사람 목록 + 카운트."""

    people: list[EmpidImportPerson] = []       # user 매칭 성공 (액션 가능)
    placeholder: list[EmpidImportPerson] = []  # 더미/공유 이메일 (리포트)
    deferred: list[EmpidImportPerson] = []     # DB 미매칭 (리포트)
    counts: dict[str, int] = {}
    excluded_rows: int = 0
    total_rows: int = 0
    # 매장 미매칭 원문 집계 — 운영자가 매장에 수동 매핑해 재-preview 하는 용도
    unmatched_stores: list[EmpidUnmatchedStore] = []
    # 자동 적용된 저장 별칭 — 한 번 매핑한 라벨은 org 에 남아 다음 업로드부터 자동
    saved_aliases: list[EmpidSavedAlias] = []
    # 스코프별 양측 대조 — HTM 에만 / 파일에만 있는 번호
    reconciliation: list[EmpidReconciliationScope] = []
    # 백 단위 번호대 분포 (export split_by="band" 와 같은 규칙). 자동 예외 추천은 없다.
    distribution: list[EmpidBandCount] = []


class EmpidImportCommitItem(BaseModel):
    """commit 1건 — (user, store) 에 empid 기입. null = 번호 삭제(배정 행 유지)."""

    user_id: str
    store_id: str
    empid: int | None = Field(default=None, ge=1)
    # 번호 구분 — 생략 시 sequence. 화면/경로로 추론하지 않는다 (INV-6).
    empid_kind: Literal["sequence", "exception"] = EMPID_KIND_SEQUENCE
    reason: str | None = Field(default=None, max_length=500)  # 변경 사유 (선택)


class EmpidImportCommitRequest(BaseModel):
    """commit 요청 — 운영자가 체크한 (user, store, empid) 목록."""

    assignments: list[EmpidImportCommitItem]


class EmpidImportCommitResponse(BaseModel):
    """commit 응답 — 반영/재채번/스킵/거절 내역."""

    applied: list[dict] = []      # {user, store, empid(null=삭제), created}
    renumbered: list[dict] = []   # {user, store, old, new} — 번호를 뺏긴 기존 인원 재채번
    skipped: list[dict] = []      # {user, store, empid, reason}
    rejected: list[dict] = []     # {user_id, store_id, reason}
    exception_count: int = 0      # 이번 커밋이 예외로 기입한 건수
    cursor_after: dict[str, int] = {}  # {scope_id: 커밋 후 커서} — 커밋은 커서를 밀지 않는다


class EmpidRosterMember(BaseModel):
    """roster 1인 — 매장 배정 + 현재 empid + 역할/부서 (export 필터 축)."""

    user_id: str
    full_name: str
    email: str | None = None
    empid: int | None = None
    empid_kind: str = EMPID_KIND_SEQUENCE  # sequence | exception (empid 없으면 무의미)
    is_work_assignment: bool = True
    is_manager: bool = False
    is_active: bool = True             # 계정 활성 여부 — 비활성 계정 export 제외 필터 축
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
