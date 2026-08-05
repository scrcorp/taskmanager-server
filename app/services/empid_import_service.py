"""EMPID 임포트 — 레거시 직원 마스터(xlsx/csv)를 매장별 empid 로 등록.

콘솔 /users/bulk/empid 탭의 백엔드. 레거시 도구(empid_reconcile_service — users.employee_no
기입)와 달리 이 서비스는 **org_member_stores.empid** (매장별 정수 순번)에 기록하며
users.employee_no 는 건드리지 않는다 (employee_no 는 폐기 방향 — 2026-08-03 결정).

설계:
- blind import 금지 → preview(사람×매장 버킷) → 운영자 검토(체크박스) → commit.
- 사람 매칭: 이메일 (레거시와 동일 — placeholder/deferred 로직 재사용).
- 매장 매칭: COMPANY/CORP_ABR_3 → org 의 Store name/code 정규화 비교. 미매칭 매장은 생략(리포트만).
- 기존 empid 는 전 매장 자동백필(1..N) 상태 — "이미 있으면 skip" 이 아니라 **rebind**:
  업로드값 == 현재값 → same(스킵), 다르면 rebind(기본 선택 = 업로드값).
- commit 은 매장 단위 3-phase (비우기 → 기입 → 잔여 재채번) 단일 트랜잭션:
  순열 교환(A:5→3, B:3→5)·소번호 재기입이 (store_id, empid) partial unique 에
  걸리지 않게 하고, 파일에 없는 기존 인원이 번호를 뺏기면 next_empid 로 재채번한다.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.org_member import OrgMember, OrgMemberStore
from app.models.organization import Store
from app.repositories.store_repository import store_repository
from app.repositories.user_repository import user_repository
from app.services.empid_reconcile_service import (
    EmpRow,
    _is_placeholder_email,
    _name_tokens,
    _name_similar,
    _norm_email,
    _first_name_token,
    parse_emplist,
)
from app.services.org_numbering import empid_scope_store_ids, lock_empid_scope, next_empid
from app.utils.exceptions import DuplicateError

# 액션 값 — 사람×매장 1건의 처리 분류.
ACTION_SAME = "same"                      # 업로드값 == 현재값 → 할 일 없음
ACTION_REBIND = "rebind"                  # 값이 다름 → 재기입 후보 (기본 선택 = 업로드값)
ACTION_NEW_ASSIGNMENT = "new_assignment"  # 매장 배정 행 없음 → 배정 생성 + 번호 등록
ACTION_UNMATCHED_STORE = "unmatched_store"  # COMPANY 를 org 매장으로 못 찾음 → 생략(리포트)
ACTION_INVALID = "invalid"                # emp_id 정수화 실패 / org_member 없음 등
ACTION_NEEDS_USER = "needs_user"          # 매장·번호 유효하나 유저 미확정 → 운영자가 직접 선택해 등록


def _norm_key(value: str | None) -> str:
    """매장 매칭 키 정규화 — 대문자, 영숫자만."""
    if not value:
        return ""
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _emp_id_int(raw: str) -> int | None:
    """레거시 emp_id 문자열 → 양의 정수 (선행 0 허용: "07"→7). 실패 시 None."""
    s = raw.strip()
    if not s or not s.isdigit():
        return None
    v = int(s)
    return v if v >= 1 else None


def build_store_index(stores: list[Store]) -> dict[str, list[Store]]:
    """정규화 키 → 매장 목록. name/code 둘 다 키로 등록 (중복 키 = 모호 매칭)."""
    index: dict[str, list[Store]] = {}
    for s in stores:
        for key in {_norm_key(s.name), _norm_key(s.code)}:
            if key:
                index.setdefault(key, []).append(s)
    return index


def match_store(index: dict[str, list[Store]], company: str, corp_abr: str | None) -> Store | None:
    """COMPANY/CORP_ABR_3 → Store. 유일 매칭만 인정 (모호하면 None)."""
    for key in (_norm_key(company), _norm_key(corp_abr)):
        if not key:
            continue
        candidates = index.get(key, [])
        # name/code 가 같은 매장을 이중 등록했을 수 있으므로 id 로 유니크화
        unique = {s.id: s for s in candidates}
        if len(unique) == 1:
            return next(iter(unique.values()))
    return None


@dataclass
class ImportEntry:
    """사람×매장 1건 — preview 행."""

    store_id: str | None       # 매칭된 매장 UUID (None = unmatched)
    store_name: str | None     # 매칭된 매장 이름
    company: str               # 파일 COMPANY 원문
    emp_id_raw: str            # 파일 emp_id 원문 (선행 0 보존 표시용)
    emp_id: int | None         # 정수 정규화 값 (None = invalid)
    current_empid: int | None  # 현재 org_member_stores.empid
    has_assignment: bool       # 매장 배정 행 존재 여부
    action: str                # ACTION_* 값
    warning: str | None = None  # 그룹 스코프 충돌 등 경고 (블록 아님)
    dormant: bool = False      # 휴면 배정(is_work_assignment=False) 여부 — 번호만 쓰고 재활성화 안 함
    person_name: str | None = None  # 파일 행의 인물 이름 — placeholder(공유 이메일)에서 행별 picker 라벨


@dataclass
class PersonRow:
    """사람 1명 — preview 그룹."""

    email: str | None
    name: str
    user_id: str | None
    user_full_name: str | None
    entries: list[ImportEntry] = field(default_factory=list)
    note: str = ""
    similar: list[str] = field(default_factory=list)  # deferred 이름 유사 힌트 (표시용)
    members: list[str] = field(default_factory=list)  # placeholder — 파일 내 인물 나열 (표시용)
    # 유저 picker 기본값 후보 — 이름 유사 DB 유저 (구조화, 콘솔 select 프리필용)
    similar_users: list[dict] = field(default_factory=list)  # {user_id, full_name, email}
    matched_by: str | None = None  # "crewid" = 파일의 CREWID 로 정확 매칭 (email/이름 달라도 확정)


@dataclass
class ImportPreview:
    """preview 결과 — 버킷별 사람 목록 + 카운트."""

    people: list[PersonRow] = field(default_factory=list)      # user 매칭 성공 (액션 가능)
    placeholder: list[PersonRow] = field(default_factory=list)  # 더미/공유 이메일 (리포트)
    deferred: list[PersonRow] = field(default_factory=list)     # DB 미매칭 (리포트)
    excluded_rows: int = 0
    total_rows: int = 0

    def counts(self) -> dict[str, int]:
        entry_actions = [e.action for p in self.people for e in p.entries]
        # unmatched/invalid 는 세 버킷 전체 엔트리에서 집계 — 미지 이메일 행(deferred/placeholder
        # 안의 매장 미매칭·비정수)이 타일에서 누락되면 카운터가 운영자를 오도한다.
        all_actions = entry_actions + [
            e.action
            for p in list(self.placeholder) + list(self.deferred)
            for e in p.entries
        ]
        return {
            "people": len(self.people),
            "same": entry_actions.count(ACTION_SAME),
            "rebind": entry_actions.count(ACTION_REBIND),
            "new_assignment": entry_actions.count(ACTION_NEW_ASSIGNMENT),
            "unmatched_store": all_actions.count(ACTION_UNMATCHED_STORE),
            "invalid": all_actions.count(ACTION_INVALID),
            "needs_user": all_actions.count(ACTION_NEEDS_USER),
            "placeholder": len(self.placeholder),
            # deferred = 등록 가능(needs_user) 엔트리가 있는 사람 수 — 리포트 온리 행만 있는
            # 사람은 unmatched/invalid 타일이 이미 설명한다.
            "deferred": sum(
                1 for p in self.deferred
                if any(e.action == ACTION_NEEDS_USER for e in p.entries)
            ),
            "excluded_rows": self.excluded_rows,
            "total_rows": self.total_rows,
        }


async def _current_empid_map(
    db: AsyncSession, organization_id: UUID
) -> tuple[
    dict[UUID, UUID],
    dict[tuple[UUID, UUID], int | None],
    dict[tuple[UUID, UUID], bool],
]:
    """(user_id→org_member_id, (member, store)→empid, (member, store)→is_work_assignment)."""
    member_rows = (
        await db.execute(
            select(OrgMember.id, OrgMember.user_id).where(
                OrgMember.organization_id == organization_id
            )
        )
    ).all()
    member_by_user = {r.user_id: r.id for r in member_rows}
    member_ids = [r.id for r in member_rows]
    empid_map: dict[tuple[UUID, UUID], int | None] = {}
    work_map: dict[tuple[UUID, UUID], bool] = {}
    if member_ids:
        store_rows = (
            await db.execute(
                select(
                    OrgMemberStore.org_member_id,
                    OrgMemberStore.store_id,
                    OrgMemberStore.empid,
                    OrgMemberStore.is_work_assignment,
                ).where(OrgMemberStore.org_member_id.in_(member_ids))
            )
        ).all()
        empid_map = {(r.org_member_id, r.store_id): r.empid for r in store_rows}
        work_map = {(r.org_member_id, r.store_id): r.is_work_assignment for r in store_rows}
    return member_by_user, empid_map, work_map


async def preview(
    db: AsyncSession, organization_id: UUID, content: bytes, filename: str = ""
) -> ImportPreview:
    """업로드 파일 → 사람×매장 버킷 분류 (DB 기록 없음)."""
    emp_rows, excluded = parse_emplist(content, filename)
    result = ImportPreview(excluded_rows=excluded, total_rows=len(emp_rows))

    users = await user_repository.get_by_org(db, organization_id)
    users_by_email: dict[str, list] = {}
    for u in users:
        e = _norm_email(u.email)
        if e:
            users_by_email.setdefault(e, []).append(u)

    stores = await store_repository.get_by_org(db, organization_id, include_closed=True)
    store_index = build_store_index(stores)
    member_by_user, empid_map, work_map = await _current_empid_map(db, organization_id)

    # 그룹 공유 스코프 경고용 — store_id → 스코프 내 사용 중 empid 집합 (자기 매장 제외)
    scope_cache: dict[UUID, set[int]] = {}

    async def _scope_other_empids(store_id: UUID) -> set[int]:
        if store_id not in scope_cache:
            scope_ids = await empid_scope_store_ids(db, store_id)
            used: set[int] = set()
            if len(scope_ids) > 1:
                for (m_id, s_id), emp in empid_map.items():
                    if emp is not None and s_id != store_id and s_id in scope_ids:
                        used.add(emp)
            scope_cache[store_id] = used
        return scope_cache[store_id]

    # 이름 유사도 인덱스 (deferred 힌트)
    name_index: list[tuple[set[str], object]] = []
    for u in users:
        toks = _name_tokens(getattr(u, "full_name", None))
        if toks:
            name_index.append((toks, u))

    def _similar_users(name: str) -> list:
        toks = _name_tokens(name)
        if not toks:
            return []
        return [u for u_toks, u in name_index if _name_similar(toks, u_toks)][:5]

    def _similar(name: str) -> list[str]:
        return [
            f"{getattr(u, 'full_name', '')} <{getattr(u, 'email', None) or '-'}>"
            for u in _similar_users(name)
        ]

    def _similar_structured(name: str) -> list[dict]:
        """유저 picker 프리필용 — {user_id, full_name, email}."""
        return [
            {
                "user_id": str(u.id),
                "full_name": getattr(u, "full_name", ""),
                "email": getattr(u, "email", None),
            }
            for u in _similar_users(name)
        ]

    # ── CREWID 정확 매칭 (optional 컬럼) ────────────────────────────────
    # 개별 가입이라 email/이름이 레거시와 다를 수 있음 — 파일에 CREWID(org 번호)가 있으면
    # 그걸로 확정 매칭한다 (공유 이메일도 구분됨). 없거나 미매칭이면 email 파이프라인 폴백.
    users_by_id = {u.id: u for u in users}
    crew_rows = (
        await db.execute(
            select(OrgMember.user_id, OrgMember.id, OrgMember.crewid).where(
                OrgMember.organization_id == organization_id,
                OrgMember.crewid.isnot(None),
            )
        )
    ).all()
    member_by_crewid: dict[int, tuple[UUID, UUID]] = {
        r.crewid: (r.user_id, r.id) for r in crew_rows
    }

    def _crewid_int(r: EmpRow) -> int | None:
        if not r.crewid or not r.crewid.strip().isdigit():
            return None
        return int(r.crewid)

    def _crewid_warn(r: EmpRow) -> str | None:
        """email 폴백 행에 붙일 CREWID 미해결 경고 (해결된 행은 이 파이프라인에 없음)."""
        if r.crewid is None:
            return None
        v = _crewid_int(r)
        if v is None:
            return f"CREWID '{r.crewid}' is not a number — matched by email instead"
        return f"CREWID {v} not found in this org — matched by email instead"

    crewid_persons: dict[str, PersonRow] = {}   # user_id(str) → PersonRow
    person_seen: dict[str, set[tuple[str, str]]] = {}  # (company, emp_id) 중복 행 제거
    leftover: list[EmpRow] = []
    for r in emp_rows:
        cid = _crewid_int(r)
        resolved = member_by_crewid.get(cid) if cid is not None else None
        user = users_by_id.get(resolved[0]) if resolved else None
        if resolved is None or user is None:
            leftover.append(r)
            continue
        user_id, member_id = resolved
        key = str(user_id)
        person = crewid_persons.get(key)
        if person is None:
            person = PersonRow(
                email=_norm_email(getattr(user, "email", None)), name=r.name,
                user_id=key, user_full_name=user.full_name,
                matched_by="crewid", note=f"matched by CREWID {cid}",
            )
            crewid_persons[key] = person
            person_seen[key] = set()
            result.people.append(person)
        pair = (r.company, r.emp_id)
        if pair in person_seen[key]:
            continue
        person_seen[key].add(pair)
        entry = _build_entry(r, store_index, member_id, user, empid_map, work_map)
        if entry.store_id and entry.emp_id is not None:
            others = await _scope_other_empids(UUID(entry.store_id))
            if entry.emp_id in others:
                entry.warning = "same number already used by another store in this group"
        # 파일 email 이 계정과 다르면 표기 — CREWID 가 이겼음을 명시
        if r.email and r.email != _norm_email(getattr(user, "email", None)):
            note = "file email differs from account — matched by CREWID"
            entry.warning = f"{entry.warning}; {note}" if entry.warning else note
        person.entries.append(entry)

    # 이메일 없는 행 → deferred (유저 선택으로 등록 가능 — needs_user)
    for r in (row for row in leftover if not row.email):
        pr = PersonRow(
            email=None, name=r.name, user_id=None, user_full_name=None,
            note="no email", similar=_similar(r.name),
            similar_users=_similar_structured(r.name),
            entries=[_build_entry(r, store_index, None, None, {})],
        )
        warn = _crewid_warn(r)
        if warn:
            for e in pr.entries:
                e.warning = f"{e.warning}; {warn}" if e.warning else warn
        result.deferred.append(pr)

    groups: dict[str, list[EmpRow]] = {}
    for r in leftover:
        if r.email:
            groups.setdefault(r.email, []).append(r)

    for email, rows in groups.items():
        first_names = {_first_name_token(r.name) for r in rows}
        same_person = len(first_names) == 1
        rep_name = rows[0].name
        db_users = users_by_email.get(email, [])

        # 더미/공유 이메일 → 행별로 유저를 골라 등록 가능 (needs_user). members 는 표시용 유지.
        if _is_placeholder_email(email) or not same_person:
            reason = ("internal email — shared placeholder" if _is_placeholder_email(email)
                      else "shared email — multiple people")
            unique_rows = list({(r.name, r.emp_id, r.company): r for r in rows}.values())
            members = [f"{r.name} = {r.emp_id} ({r.company})" for r in unique_rows]
            ph_entries = []
            for r in unique_rows:
                e = _build_entry(r, store_index, None, None, {})
                warn = _crewid_warn(r)
                if warn:
                    e.warning = f"{e.warning}; {warn}" if e.warning else warn
                ph_entries.append(e)
            result.placeholder.append(PersonRow(
                email=email, name=rep_name, user_id=None, user_full_name=None,
                note=reason, members=members, entries=ph_entries,
            ))
            continue

        if not db_users:
            df_entries = []
            for r in rows:
                e = _build_entry(r, store_index, None, None, {})
                warn = _crewid_warn(r)
                if warn:
                    e.warning = f"{e.warning}; {warn}" if e.warning else warn
                df_entries.append(e)
            result.deferred.append(PersonRow(
                email=email, name=rep_name, user_id=None, user_full_name=None,
                note="email present, no DB user", similar=_similar(rep_name),
                similar_users=_similar_structured(rep_name),
                entries=df_entries,
            ))
            continue

        user = db_users[0]
        member_id = member_by_user.get(user.id)
        # 같은 유저가 CREWID 로 이미 매칭되어 있으면 그 PersonRow 에 병합 (사람 카드 1개 유지)
        merged = crewid_persons.get(str(user.id))
        person = merged if merged is not None else PersonRow(
            email=email, name=rep_name,
            user_id=str(user.id), user_full_name=user.full_name,
        )
        if member_id is None:
            person.note = "no org membership (legacy account) — cannot assign"
            person.entries = [_build_entry(r, store_index, None, None, {}) for r in rows]
            for e in person.entries:
                e.action = ACTION_INVALID
                e.warning = "no org_member row"
            if merged is None:
                result.people.append(person)
            continue

        # 같은 (매장, 사람) 중복 행 제거 후 매장별 엔트리 구성
        # (CREWID 병합 시 이미 추가된 (company, emp_id) 쌍은 person_seen 으로 건너뜀)
        seen_pairs: set[tuple[str, str]] = person_seen.get(str(user.id), set())
        confirm_rows: list[EmpRow] = []  # CREWID 미해결 행 — 자동 등록 금지, 확인 대기
        for r in rows:
            pair = (r.company, r.emp_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            # CREWID 가 주어졌는데 해석 불가(오타 가능성) → email 이 맞아도 자동 매칭하지 않고
            # 확인 대기로 분리 — email 유저를 picker 프리필 후보로 제시, 운영자가 명시 체크.
            if _crewid_warn(r):
                confirm_rows.append(r)
                continue
            entry = _build_entry(r, store_index, member_id, user, empid_map, work_map)
            # 그룹 공유 스코프 내 타 매장 번호와 충돌 경고 (막지 않음 — D2 결정)
            if entry.store_id and entry.emp_id is not None:
                others = await _scope_other_empids(UUID(entry.store_id))
                if entry.emp_id in others:
                    entry.warning = "same number already used by another store in this group"
            person.entries.append(entry)

        if confirm_rows:
            cf_entries = []
            for r in confirm_rows:
                e = _build_entry(r, store_index, None, None, {})  # member 미확정 → needs_user
                warn = _crewid_warn(r)
                if warn:
                    e.warning = f"{e.warning}; {warn}" if e.warning else warn
                cf_entries.append(e)
            result.deferred.append(PersonRow(
                email=email, name=rep_name, user_id=None, user_full_name=None,
                note="CREWID mismatch — email matches an account, confirm before registering",
                similar=[f"{user.full_name} <{email}>"],
                similar_users=[{
                    "user_id": str(user.id),
                    "full_name": user.full_name,
                    "email": email,
                }],
                entries=cf_entries,
            ))

        # 같은 매장에 서로 다른 두 값이 오는 경우 — 뒤 값에 경고
        by_store: dict[str, int] = {}
        for e in person.entries:
            if e.store_id and e.emp_id is not None:
                if e.store_id in by_store and by_store[e.store_id] != e.emp_id:
                    e.action = ACTION_INVALID
                    e.warning = "conflicting numbers for the same store in file"
                else:
                    by_store[e.store_id] = e.emp_id
        # 전 행이 확인 대기로 빠졌으면 빈 카드를 만들지 않는다
        if merged is None and person.entries:
            result.people.append(person)

    return result


def _build_entry(
    r: EmpRow,
    store_index: dict[str, list[Store]],
    member_id: UUID | None,
    user: object | None,
    empid_map: dict[tuple[UUID, UUID], int | None],
    work_map: dict[tuple[UUID, UUID], bool] | None = None,
) -> ImportEntry:
    """EmpRow 1건 → ImportEntry (매장 매칭 + 현재값 비교 + 액션 결정)."""
    store = match_store(store_index, r.company, r.corp_abr)
    emp_int = _emp_id_int(r.emp_id)
    if store is None:
        return ImportEntry(
            store_id=None, store_name=None, company=r.company,
            emp_id_raw=r.emp_id, emp_id=emp_int, current_empid=None,
            has_assignment=False, action=ACTION_UNMATCHED_STORE,
            warning="no matching store in this org", person_name=r.name,
        )
    if emp_int is None:
        return ImportEntry(
            store_id=str(store.id), store_name=store.name, company=r.company,
            emp_id_raw=r.emp_id, emp_id=None, current_empid=None,
            has_assignment=False, action=ACTION_INVALID,
            warning="emp_id is not a positive integer", person_name=r.name,
        )
    if member_id is None:
        # 매장·번호는 유효, 유저만 미확정 — 운영자가 picker 로 유저를 골라 등록 가능.
        return ImportEntry(
            store_id=str(store.id), store_name=store.name, company=r.company,
            emp_id_raw=r.emp_id, emp_id=emp_int, current_empid=None,
            has_assignment=False, action=ACTION_NEEDS_USER, person_name=r.name,
        )
    key = (member_id, store.id)
    has_row = key in empid_map
    current = empid_map.get(key)
    if not has_row:
        action = ACTION_NEW_ASSIGNMENT
    elif current == emp_int:
        action = ACTION_SAME
    else:
        action = ACTION_REBIND
    # 휴면 배정(과거 배정 해제 or 근무배정 제외) — 번호는 기록되지만 재활성화하지 않음을 표시.
    dormant = has_row and not (work_map or {}).get(key, True)
    return ImportEntry(
        store_id=str(store.id), store_name=store.name, company=r.company,
        emp_id_raw=r.emp_id, emp_id=emp_int, current_empid=current,
        has_assignment=has_row, action=action,
        dormant=dormant,
        warning=(
            "assignment is dormant (not in work assignment) — number is written but the person stays inactive"
            if dormant else None
        ),
    )


async def roster(db: AsyncSession, organization_id: UUID) -> list[dict]:
    """매장별 배정·empid 현황 — bulk 에디터·export 필터의 데이터 소스.

    live 매장(sort_order 순)별로 배정 인원과 현재 empid + 역할/부서를 나열한다.
    (역할·부서는 export 필터 축 — SV/Manager 등 role, FOH/BOH department.)
    """
    from app.models.user import Role, User

    stores = await store_repository.get_by_org(db, organization_id, include_closed=False)
    if not stores:
        return []
    store_ids = [s.id for s in stores]
    rows = (
        await db.execute(
            select(
                OrgMemberStore.store_id,
                OrgMemberStore.empid,
                OrgMemberStore.is_work_assignment,
                OrgMemberStore.is_manager,
                OrgMember.user_id,
                OrgMember.crewid,
                OrgMember.department,
                User.full_name,
                User.email,
                Role.name.label("role_name"),
                Role.priority.label("role_priority"),
            )
            .join(OrgMember, OrgMember.id == OrgMemberStore.org_member_id)
            .join(User, User.id == OrgMember.user_id)
            .join(Role, Role.id == OrgMember.role_id)
            .where(
                OrgMemberStore.store_id.in_(store_ids),
                OrgMember.organization_id == organization_id,
            )
        )
    ).all()
    by_store: dict[UUID, list[dict]] = {}
    for r in rows:
        by_store.setdefault(r.store_id, []).append({
            "user_id": str(r.user_id),
            "full_name": r.full_name,
            "email": r.email,
            "empid": r.empid,
            "is_work_assignment": r.is_work_assignment,
            "is_manager": r.is_manager,
            "crewid": r.crewid,
            "role_name": r.role_name,
            "role_priority": r.role_priority,
            "department": r.department,
        })
    out: list[dict] = []
    for s in stores:
        members = by_store.get(s.id, [])
        # empid 오름차순 (없는 사람은 뒤), 그 다음 이름순
        members.sort(key=lambda m: (m["empid"] is None, m["empid"] or 0, m["full_name"] or ""))
        out.append({
            "store_id": str(s.id),
            "store_name": s.name,
            "group_id": str(s.group_id) if s.group_id else None,
            "members": members,
        })
    return out


def _sheet_title(raw: str) -> str:
    """Excel 시트명 제약 대응 — 금지문자 제거 + 31자 제한."""
    cleaned = re.sub(r"[\[\]:*?/\\]", "-", raw).strip() or "Sheet"
    return cleaned[:31]


def _roster_export_sheets(sheets: list[tuple[str, list[list]]]) -> bytes:
    """임포트 형식 xlsx 생성 — 데이터 시트들(구분 export 시 복수) + Instructions.

    parse_emplist 는 첫 번째 시트만 읽으므로 데이터 시트가 반드시 먼저다.
    (시트를 나눈 파일을 재업로드하면 첫 시트만 임포트됨 — Instructions 에 명시.)
    """
    import openpyxl
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    first = True
    used_titles: set[str] = set()
    for title, rows in sheets or [("Roster", [])]:
        base = _sheet_title(title)
        name = base
        n = 2
        while name in used_titles:  # 시트명 중복 방지
            name = _sheet_title(f"{base[:28]}-{n}")
            n += 1
        used_titles.add(name)
        if first:
            ws = wb.active
            ws.title = name
            first = False
        else:
            ws = wb.create_sheet(name)
        ws.append(["COMPANY", "CORP_ABR_3", "Name", "emp_id", "Email", "crewid"])
        for c in ws[1]:
            c.font = Font(bold=True)
        for r in rows:
            ws.append(r)
        for col, width in zip("ABCDEF", (34, 12, 26, 10, 32, 10)):
            ws.column_dimensions[col].width = width
        ws.freeze_panes = "A2"

    info = wb.create_sheet("Instructions")
    for line in (
        ("EMPID import format",),
        (),
        ("COMPANY", "Store name (matched against this org's store names)"),
        ("CORP_ABR_3", "Store code (fallback match, e.g. IFO / SWC)"),
        ("Name", "Person's name (display only — matching uses Email)"),
        ("emp_id", "Number to register for that person at that store. Rows with an empty emp_id are skipped."),
        ("Email", "Person's email — used to match the user in the system"),
        ("crewid", "Optional exact-match key — the person's org number (CREWID). When present, the person is matched by CREWID even if email/name differ (useful when staff signed up with a different email)."),
        (),
        ("Tip", "Export current EMPIDs, edit the emp_id column, then upload the file back on the EMPID Import tab."),
        ("Note", "Import reads the FIRST sheet only — if this file was exported split into multiple sheets, merge rows into the first sheet (or re-export unsplit) before re-uploading."),
    ):
        info.append(list(line))
    info.column_dimensions["A"].width = 14
    info.column_dimensions["B"].width = 90
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def build_template_xlsx(
    db: AsyncSession,
    organization_id: UUID,
    prefill: bool,
    *,
    store_ids: set[UUID] | None = None,
    people: str = "all",  # all | numbered | unnumbered
    include_dormant: bool = True,
    include_email: bool = True,
    include_numbers: bool = True,
) -> bytes:
    """임포트용 템플릿/현황 export xlsx.

    prefill=False: 헤더 + 안내 시트만 (빈 템플릿, 필터 무관).
    prefill=True: 현재 배정·번호 export — 필터로 범위를 좁힐 수 있다:
      store_ids: 매장 부분집합 (None=전체) / people: 번호 유무 필터
      include_dormant: 휴면(근무배정 제외) 포함 여부
      include_email: Email 컬럼 값 포함 여부 — 빼면 재업로드 매칭 불가(공유용)
      include_numbers: emp_id 값 포함 여부 — 빼면 번호 비운 작성용 양식
    헤더 5컬럼은 항상 유지 (임포트 파서 형식 보존).
    """
    rows: list[list] = []
    if prefill:
        stores = await store_repository.get_by_org(db, organization_id, include_closed=False)
        code_by_id = {str(s.id): (s.code or "") for s in stores}
        for st in await roster(db, organization_id):
            if store_ids is not None and UUID(st["store_id"]) not in store_ids:
                continue
            for m in st["members"]:
                if not include_dormant and not m["is_work_assignment"]:
                    continue
                if people == "numbered" and m["empid"] is None:
                    continue
                if people == "unnumbered" and m["empid"] is not None:
                    continue
                rows.append([
                    st["store_name"],
                    code_by_id.get(st["store_id"], ""),
                    m["full_name"],
                    (m["empid"] if m["empid"] is not None else "") if include_numbers else "",
                    (m["email"] or "") if include_email else "",
                    m["crewid"] if m["crewid"] is not None else "",
                ])
    return _roster_export_sheets([("Roster", rows)])


async def build_selected_export_xlsx(
    db: AsyncSession,
    organization_id: UUID,
    selected: list[tuple[UUID, UUID]],  # (user_id, store_id) — 콘솔에서 사람 단위로 확정한 목록
    *,
    include_email: bool = True,
    include_numbers: bool = True,
    split_by: str = "none",  # none | store | role | band(백 단위 번호대)
) -> bytes:
    """사람 단위 선택 export — 콘솔이 필터·개별 넣고빼기로 확정한 (user, store) 목록을 그대로 굽는다.

    필터 축(역할/부서/번호대/매장/개인)은 전부 클라이언트 몫 — 서버는 선택 결과만 받아
    어떤 새 축이 생겨도 변경이 필요 없다. split_by 로 시트를 구분(1차/2차식 배포용):
    store=매장별, role=역할별, band=백 단위 번호대별. 재업로드는 첫 시트만 읽힌다.
    """
    wanted = set(selected)
    if not wanted:
        return _roster_export_sheets([("Roster", [])])
    stores = await store_repository.get_by_org(db, organization_id, include_closed=False)
    code_by_id = {str(s.id): (s.code or "") for s in stores}

    entries: list[dict] = []
    for st in await roster(db, organization_id):
        for m in st["members"]:
            if (UUID(m["user_id"]), UUID(st["store_id"])) not in wanted:
                continue
            entries.append({
                "row": [
                    st["store_name"],
                    code_by_id.get(st["store_id"], ""),
                    m["full_name"],
                    (m["empid"] if m["empid"] is not None else "") if include_numbers else "",
                    (m["email"] or "") if include_email else "",
                    m["crewid"] if m["crewid"] is not None else "",
                ],
                "store_name": st["store_name"],
                "role_name": m["role_name"],
                "empid": m["empid"],
            })

    if split_by == "store":
        keys: list[str] = []
        grouped: dict[str, list[list]] = {}
        for e in entries:
            k = e["store_name"]
            if k not in grouped:
                grouped[k] = []
                keys.append(k)
            grouped[k].append(e["row"])
        sheets = [(k, grouped[k]) for k in keys]
    elif split_by == "role":
        keys = []
        grouped = {}
        for e in entries:
            k = e["role_name"] or "No role"
            if k not in grouped:
                grouped[k] = []
                keys.append(k)
            grouped[k].append(e["row"])
        sheets = [(k, grouped[k]) for k in keys]
    elif split_by == "band":
        # 백 단위 번호대 — 1-99, 100-199, 300-399 … 번호 없는 사람은 "No number" 시트 마지막
        def band_key(empid: int | None) -> tuple[int, str]:
            if empid is None:
                return (10**9, "No number")
            base = (empid // 100) * 100
            lo = base if base > 0 else 1
            return (base, f"{lo}-{base + 99}")

        tagged = sorted(
            ((band_key(e["empid"]), e["row"]) for e in entries), key=lambda t: t[0][0]
        )
        keys = []
        grouped = {}
        for (order, label), row in tagged:
            if label not in grouped:
                grouped[label] = []
                keys.append(label)
            grouped[label].append(row)
        sheets = [(k, grouped[k]) for k in keys]
    else:
        sheets = [("Roster", [e["row"] for e in entries])]
    return _roster_export_sheets(sheets)


@dataclass
class CommitResult:
    """commit 결과 — 반영/재채번/스킵/거절 내역."""

    applied: list[dict] = field(default_factory=list)      # {user, store, empid, created}
    renumbered: list[dict] = field(default_factory=list)   # {user, store, old, new}
    skipped: list[dict] = field(default_factory=list)      # {user, store, empid, reason}
    rejected: list[dict] = field(default_factory=list)     # {user_id, store_id, reason}


async def commit(
    db: AsyncSession,
    organization_id: UUID,
    assignments: list[tuple[UUID, UUID, int | None]],  # (user_id, store_id, empid|None=번호 삭제)
) -> CommitResult:
    """확정 — 매장 단위 3-phase 로 empid 재기입. 단일 트랜잭션, 멱등.

    empid=None 은 번호 삭제(비우기) — 배정 행은 유지, 번호만 해제되어 재사용 가능.
    users.employee_no 는 기록하지 않는다 (폐기 방향).
    """
    result = CommitResult()

    member_by_user, empid_map, _work_map = await _current_empid_map(db, organization_id)
    member_user: dict[UUID, UUID] = {m: u for u, m in member_by_user.items()}

    # org 매장 검증용 (폐점 포함 — 번호는 폐점 매장에도 존재)
    stores = await store_repository.get_by_org(db, organization_id, include_closed=True)
    store_by_id = {s.id: s for s in stores}
    user_names: dict[UUID, str] = {}
    for u in await user_repository.get_by_org(db, organization_id):
        user_names[u.id] = u.full_name

    def _label(member_id: UUID, store_id: UUID) -> tuple[str, str]:
        user_id = member_user.get(member_id)
        return (
            user_names.get(user_id, str(user_id)) if user_id else str(member_id),
            store_by_id[store_id].name,
        )

    # 매장별 mapping 구성 — {store_id: {org_member_id: empid}}.
    # claims 는 매장별 역맵(empid→member) — 같은 매장에 같은 번호를 두 사람이 요청하면
    # 현재 보유자를 우선하고 나머지는 거절한다 (전체 롤백/무단 재채번 방지).
    per_store: dict[UUID, dict[UUID, int | None]] = {}
    claims: dict[UUID, dict[int, UUID]] = {}
    for user_id, store_id, empid in assignments:
        if store_id not in store_by_id:
            result.rejected.append({"user_id": str(user_id), "store_id": str(store_id),
                                    "reason": "store not in this org"})
            continue
        member_id = member_by_user.get(user_id)
        if member_id is None:
            result.rejected.append({"user_id": str(user_id), "store_id": str(store_id),
                                    "reason": "no org membership"})
            continue
        if empid is not None and empid < 1:
            result.rejected.append({"user_id": str(user_id), "store_id": str(store_id),
                                    "reason": "empid must be >= 1"})
            continue
        mapping = per_store.setdefault(store_id, {})
        store_claims = claims.setdefault(store_id, {})
        if member_id in mapping and mapping[member_id] != empid:
            result.rejected.append({"user_id": str(user_id), "store_id": str(store_id),
                                    "reason": "conflicting values for the same store in request"})
            continue
        if empid is None:
            # 번호 삭제 — 값 충돌 검사 대상 아님. 배정 행이 없으면 할 일 없음(스킵).
            if (member_id, store_id) not in empid_map or empid_map[(member_id, store_id)] is None:
                name, store_name = _label(member_id, store_id)
                result.skipped.append({"user": name, "store": store_name,
                                       "empid": None, "reason": "nothing to clear"})
                continue
            mapping[member_id] = None
            continue
        other = store_claims.get(empid)
        if other is not None and other != member_id:
            if empid_map.get((member_id, store_id)) == empid:
                # 이번 요청자가 현재 보유자 — 앞서 등록된 다른 사람을 밀어내고 거절.
                del mapping[other]
                other_user = member_user.get(other)
                result.rejected.append({
                    "user_id": str(other_user) if other_user else str(other),
                    "store_id": str(store_id),
                    "reason": "duplicate empid for this store in request",
                })
            else:
                result.rejected.append({"user_id": str(user_id), "store_id": str(store_id),
                                        "reason": "duplicate empid for this store in request"})
                continue
        store_claims[empid] = member_id
        mapping[member_id] = empid

    try:
        # Pass 1 — 매장별: 멱등 스킵 → 스코프 락 → 비우기 → 기입. 재채번은 미룬다.
        # (그룹 공유 스코프에서 형제 매장의 기입값이 재채번 max 에 반영되도록 2-pass.)
        deferred: list[tuple[UUID, dict[UUID, int], list[OrgMemberStore]]] = []
        for store_id, mapping in per_store.items():
            # 이미 같은 값이면 skip (멱등) — mapping 에서 제거
            for member_id in list(mapping.keys()):
                if empid_map.get((member_id, store_id)) == mapping[member_id]:
                    name, store_name = _label(member_id, store_id)
                    result.skipped.append({"user": name, "store": store_name,
                                           "empid": mapping[member_id],
                                           "reason": "already set"})
                    del mapping[member_id]
            if not mapping:
                continue

            # 그룹 공유 스코프면 advisory lock 선취 — 커밋까지 유지되어 동시 채번과 직렬화.
            await lock_empid_scope(db, store_id)

            rows = (
                await db.execute(
                    select(OrgMemberStore).where(OrgMemberStore.store_id == store_id)
                )
            ).scalars().all()
            by_member = {row.org_member_id: row for row in rows}
            # None(번호 삭제)은 값 충돌 대상 아님
            wanted_values = {v for v in mapping.values() if v is not None}

            # phase 1 — 비우기: 대상 행 + 원하는 값을 점유 중인 행의 empid 를 NULL 로.
            cleared: list[OrgMemberStore] = []
            for row in rows:
                if row.empid is None:
                    continue
                if row.org_member_id in mapping or row.empid in wanted_values:
                    cleared.append(row)
                    row.empid = None
            await db.flush()

            # phase 2 — 기입: 행이 있으면 갱신, 없으면 배정 생성(신규 배정).
            for member_id, value in mapping.items():
                name, store_name = _label(member_id, store_id)
                row = by_member.get(member_id)
                if row is None:
                    db.add(OrgMemberStore(
                        org_member_id=member_id, store_id=store_id,
                        is_manager=False, is_work_assignment=True, empid=value,
                    ))
                    result.applied.append({"user": name, "store": store_name,
                                           "empid": value, "created": True})
                else:
                    row.empid = value
                    result.applied.append({"user": name, "store": store_name,
                                           "empid": value, "created": False})
            await db.flush()
            deferred.append((store_id, mapping, cleared))

        # Pass 2 — 전 매장 기입 완료 후 재채번: 번호를 뺏겼는데 mapping 에 없는 기존 인원.
        # 이 시점의 그룹 max 는 형제 매장의 phase 2 기입값을 포함하므로 임포트가
        # 스스로 그룹 스코프 중복을 만들 수 없다.
        for store_id, mapping, cleared in deferred:
            for row in cleared:
                if row.org_member_id in mapping:
                    continue
                old = empid_map.get((row.org_member_id, store_id))
                row.empid = await next_empid(db, store_id)
                name, store_name = _label(row.org_member_id, store_id)
                result.renumbered.append({"user": name, "store": store_name,
                                          "old": old, "new": row.empid})
            await db.flush()

        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise DuplicateError(
            "empid assignment conflict (concurrent change) — please retry"
        ) from e
    except Exception:
        await db.rollback()
        raise
    return result
