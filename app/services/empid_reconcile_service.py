"""EMPID 레거시 임포트 — 매칭/버킷 분류 + 확정(commit) 서비스.

Backoffice의 첫 도구. 레거시 직원 마스터(xlsx)의 사번(emp_id)을
우리 `users.employee_no`(org-scope)에 채운다.

SoT: docs/99_inbox/2026-06-24 HTM control-plane(=Backoffice) 운영자콘솔 + EMPID 임포트 설계.md
설계 핵심:
- blind import 금지 → 제안(버킷) → 사람 검토 → 확정(commit).
- 멱등: 이미 employee_no 있으면 skip(=저널). NULL만 채움.
- PURADAK(폐점) 행 제외. 신규 번호 생성은 이 도구 범위 밖.

버킷(설계 §5):
1. auto       — 이메일 1:1 매칭 + 단일 emp_id + 대상 user employee_no NULL → 자동 제안
2. multiple   — 멀티스토어 동일인(이메일 같고 emp_id 여러 개) → 운영자가 canonical 선택
3. placeholder— 더미/공용 이메일(내부 도메인 or 서로 다른 사람들이 공유) → 매칭 제외
4. assigned   — 매칭됐으나 이미 employee_no 있음 → skip 표시
5. deferred   — DB 미매칭(이메일 있으나 user 없음) / 무이메일 → 리포트만
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from uuid import UUID

import openpyxl
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.user_repository import user_repository
from app.schemas.user import _normalize_employee_no

# 더미/내부 이메일 도메인 — 매칭 키로 신뢰 불가 (placeholder 처리)
_PLACEHOLDER_DOMAINS = {"tigersplus.com"}
# 폐점 등 임포트 제외 회사명 (COMPANY 컬럼 부분일치, 대문자 비교)
_EXCLUDED_COMPANIES = ("PURADAK",)


def _norm_email(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip().lower()
    return s or None


def _first_name_token(name: object) -> str:
    """이름에서 별칭 괄호 제거 후 첫 단어(소문자, 알파벳만) — 동일인 판별용."""
    if not name:
        return ""
    s = re.sub(r"\(.*?\)", "", str(name)).strip().lower()
    parts = re.sub(r"[^a-z\s]", "", s).split()
    return parts[0] if parts else ""


@dataclass
class EmpRow:
    """xlsx 한 행 (정규화)."""

    company: str
    corp_abr: str | None
    name: str
    emp_id: str  # 문자열 보존(선행0)
    email: str | None


@dataclass
class Proposal:
    """매칭 제안 1건."""

    email: str | None
    name: str
    user_id: UUID | None  # 매칭된 우리 유저
    user_full_name: str | None
    emp_id: str | None  # 단일 후보(auto)
    emp_id_options: list[str] = field(default_factory=list)  # 멀티(operator 선택)
    note: str = ""


@dataclass
class ReconcileResult:
    """버킷별 분류 결과."""

    auto: list[Proposal] = field(default_factory=list)
    multiple: list[Proposal] = field(default_factory=list)
    placeholder: list[Proposal] = field(default_factory=list)
    assigned: list[Proposal] = field(default_factory=list)
    deferred: list[Proposal] = field(default_factory=list)
    excluded_rows: int = 0  # PURADAK 등 제외 행 수
    total_rows: int = 0

    def counts(self) -> dict[str, int]:
        return {
            "auto": len(self.auto),
            "multiple": len(self.multiple),
            "placeholder": len(self.placeholder),
            "assigned": len(self.assigned),
            "deferred": len(self.deferred),
            "excluded_rows": self.excluded_rows,
            "total_rows": self.total_rows,
        }


# 헤더 별칭 — 컬럼명이 조금 달라도 매핑 (소문자/공백제거 기준)
def _hkey(s: object) -> str:
    return re.sub(r"\s+", "", str(s or "").strip().lower())


_HEADER_ALIASES = {
    "company": "company", "corp_abr_3": "corp_abr", "corpabr3": "corp_abr",
    "name": "name", "emp_id": "emp_id", "empid": "emp_id",
    "email": "email",
}


def _emp_id_str(raw: object) -> str | None:
    """emp_id 문자열 보존 (float면 .0 제거, 선행0 보존)."""
    if raw is None:
        return None
    if isinstance(raw, float) and raw.is_integer():
        return str(int(raw))
    s = str(raw).strip()
    return s or None


def _build_row(cells: dict[str, object]) -> EmpRow | None:
    """정규화 컬럼 dict(company/corp_abr/name/emp_id/email) → EmpRow (emp_id 없으면 None)."""
    emp_id = _emp_id_str(cells.get("emp_id"))
    if emp_id is None:
        return None
    return EmpRow(
        company=str(cells.get("company") or "").strip(),
        corp_abr=(str(cells.get("corp_abr")).strip() if cells.get("corp_abr") else None),
        name=str(cells.get("name") or "").strip(),
        emp_id=emp_id,
        email=_norm_email(cells.get("email")),
    )


def _is_excluded(company: str) -> bool:
    return any(x in company.upper() for x in _EXCLUDED_COMPANIES)


def _rows_from_records(records) -> tuple[list[EmpRow], int]:
    """헤더 매핑된 레코드 iterable(dict 정규화키) → (EmpRow 목록, 제외 수)."""
    out: list[EmpRow] = []
    excluded = 0
    for cells in records:
        company = str(cells.get("company") or "").strip()
        if _is_excluded(company):
            excluded += 1
            continue
        row = _build_row(cells)
        if row is not None:
            out.append(row)
    return out, excluded


def _parse_xlsx(content: bytes) -> tuple[list[EmpRow], int]:
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    ws = wb.worksheets[0]
    rows_iter = ws.iter_rows(values_only=True)
    header = [_HEADER_ALIASES.get(_hkey(c)) for c in next(rows_iter)]

    def records():
        for row in rows_iter:
            if row is None or all(c is None for c in row):
                continue
            yield {key: row[i] for i, key in enumerate(header) if key and i < len(row)}

    return _rows_from_records(records())


def _parse_csv(content: bytes) -> tuple[list[EmpRow], int]:
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    try:
        header = [_HEADER_ALIASES.get(_hkey(c)) for c in next(reader)]
    except StopIteration:
        return [], 0

    def records():
        for row in reader:
            if not row or all(not str(c).strip() for c in row):
                continue
            yield {key: row[i] for i, key in enumerate(header) if key and i < len(row)}

    return _rows_from_records(records())


def parse_emplist(content: bytes, filename: str = "") -> tuple[list[EmpRow], int]:
    """업로드 파일(xlsx/csv) → (제외 후 EmpRow 목록, 제외된 행 수).

    포맷은 확장자로 판별(.csv → CSV, 그 외 → Excel). PURADAK 등 폐점 회사 행 제외.
    """
    if filename.lower().endswith(".csv"):
        return _parse_csv(content)
    return _parse_xlsx(content)


def _is_placeholder_email(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    return domain in _PLACEHOLDER_DOMAINS


def classify(emp_rows: list[EmpRow], users_by_email: dict[str, list]) -> ReconcileResult:
    """순수 분류 로직 — (xlsx 행, 이메일→유저목록) → 버킷.

    users_by_email: normalized email → [User, ...] (해당 org).
    User는 .id, .full_name, .employee_no 속성만 사용.
    """
    result = ReconcileResult()
    result.total_rows = len(emp_rows)

    # 이메일 없는 행 → deferred(no-email)
    no_email = [r for r in emp_rows if not r.email]
    for r in no_email:
        result.deferred.append(Proposal(email=None, name=r.name, user_id=None, user_full_name=None, emp_id=r.emp_id, note="no email"))

    # 이메일별 그룹
    groups: dict[str, list[EmpRow]] = {}
    for r in emp_rows:
        if r.email:
            groups.setdefault(r.email, []).append(r)

    for email, rows in groups.items():
        emp_ids = sorted({r.emp_id for r in rows})
        first_names = {_first_name_token(r.name) for r in rows}
        same_person = len(first_names) == 1
        rep_name = rows[0].name
        db_users = users_by_email.get(email, [])

        # 3. placeholder — 내부 도메인 or 서로 다른 사람이 공유
        if _is_placeholder_email(email) or not same_person:
            result.placeholder.append(
                Proposal(email=email, name=rep_name, user_id=None, user_full_name=None,
                         emp_id=None, emp_id_options=emp_ids,
                         note="internal/shared email — not matchable")
            )
            continue

        # 동일인. DB 매칭?
        if not db_users:
            result.deferred.append(
                Proposal(email=email, name=rep_name, user_id=None, user_full_name=None,
                         emp_id=emp_ids[0] if len(emp_ids) == 1 else None, emp_id_options=emp_ids,
                         note="email present, no DB user")
            )
            continue

        user = db_users[0]  # 동일 이메일 1유저가 일반적

        # 4. 이미 사번 있음 → skip
        if getattr(user, "employee_no", None):
            result.assigned.append(
                Proposal(email=email, name=rep_name, user_id=user.id, user_full_name=user.full_name,
                         emp_id=user.employee_no, note="already assigned")
            )
            continue

        # 1/2. NULL → 자동 or 멀티
        if len(emp_ids) == 1:
            result.auto.append(
                Proposal(email=email, name=rep_name, user_id=user.id, user_full_name=user.full_name, emp_id=emp_ids[0])
            )
        else:
            result.multiple.append(
                Proposal(email=email, name=rep_name, user_id=user.id, user_full_name=user.full_name,
                         emp_id=None, emp_id_options=emp_ids,
                         note="multi-store same person — pick canonical")
            )

    return result


async def reconcile(
    db: AsyncSession, organization_id: UUID, content: bytes, filename: str = ""
) -> ReconcileResult:
    """DB 연동 — 업로드 파일(xlsx/csv) 파싱 + 해당 org 유저 매칭 + 분류."""
    emp_rows, excluded = parse_emplist(content, filename)
    users = await user_repository.get_by_org(db, organization_id)
    by_email: dict[str, list] = {}
    for u in users:
        e = _norm_email(u.email)
        if e:
            by_email.setdefault(e, []).append(u)
    result = classify(emp_rows, by_email)
    result.excluded_rows = excluded
    return result


@dataclass
class CommitResult:
    assigned: list[tuple[str, str]] = field(default_factory=list)  # (full_name, emp_id)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # 이미 있어 skip
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (user_id, reason)


async def commit_assignments(
    db: AsyncSession,
    organization_id: UUID,
    assignments: list[tuple[UUID, str]],
) -> CommitResult:
    """확정 — NULL인 대상에만 employee_no 기록 (멱등).

    - org 불일치 user → reject (IDOR 방지)
    - 이미 employee_no 있음 → skip (저널, 덮어쓰지 않음)
    - 포맷 검증(_normalize_employee_no) + org-uniqueness 위반 → reject
    """
    result = CommitResult()
    for user_id, raw_emp in assignments:
        user = await user_repository.get_by_id(db, user_id, organization_id)
        if user is None:
            result.rejected.append((str(user_id), "not found in org"))
            continue
        if user.employee_no:
            result.skipped.append((user.full_name, user.employee_no))
            continue
        try:
            emp = _normalize_employee_no(raw_emp)
        except ValueError as e:
            result.rejected.append((str(user_id), str(e)))
            continue
        if emp is None:
            result.rejected.append((str(user_id), "empty employee_no"))
            continue
        dup = await user_repository.exists(db, {"organization_id": organization_id, "employee_no": emp})
        if dup:
            result.rejected.append((str(user_id), f"employee_no {emp} already used in org"))
            continue
        user.employee_no = emp
        result.assigned.append((user.full_name, emp))

    if result.assigned:
        await db.commit()
    return result
