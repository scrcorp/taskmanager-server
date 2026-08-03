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


@dataclass
class PersonRow:
    """사람 1명 — preview 그룹."""

    email: str | None
    name: str
    user_id: str | None
    user_full_name: str | None
    entries: list[ImportEntry] = field(default_factory=list)
    note: str = ""
    similar: list[str] = field(default_factory=list)  # deferred 이름 유사 힌트
    members: list[str] = field(default_factory=list)  # placeholder — 파일 내 인물 나열


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
        return {
            "people": len(self.people),
            "same": entry_actions.count(ACTION_SAME),
            "rebind": entry_actions.count(ACTION_REBIND),
            "new_assignment": entry_actions.count(ACTION_NEW_ASSIGNMENT),
            "unmatched_store": entry_actions.count(ACTION_UNMATCHED_STORE),
            "invalid": entry_actions.count(ACTION_INVALID),
            "placeholder": len(self.placeholder),
            "deferred": len(self.deferred),
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

    def _similar(name: str) -> list[str]:
        toks = _name_tokens(name)
        if not toks:
            return []
        out = []
        for u_toks, u in name_index:
            if _name_similar(toks, u_toks):
                out.append(f"{getattr(u, 'full_name', '')} <{getattr(u, 'email', None) or '-'}>")
        return out[:5]

    # 이메일 없는 행 → deferred
    for r in (row for row in emp_rows if not row.email):
        result.deferred.append(PersonRow(
            email=None, name=r.name, user_id=None, user_full_name=None,
            note="no email", similar=_similar(r.name),
            entries=[_build_entry(r, store_index, None, None, {})],
        ))

    groups: dict[str, list[EmpRow]] = {}
    for r in emp_rows:
        if r.email:
            groups.setdefault(r.email, []).append(r)

    for email, rows in groups.items():
        first_names = {_first_name_token(r.name) for r in rows}
        same_person = len(first_names) == 1
        rep_name = rows[0].name
        db_users = users_by_email.get(email, [])

        # 더미/공유 이메일 → 리포트만 (파일 내 인물+번호 나열)
        if _is_placeholder_email(email) or not same_person:
            reason = ("internal email — shared placeholder" if _is_placeholder_email(email)
                      else "shared email — multiple people")
            members = list(dict.fromkeys(f"{r.name} = {r.emp_id} ({r.company})" for r in rows))
            result.placeholder.append(PersonRow(
                email=email, name=rep_name, user_id=None, user_full_name=None,
                note=reason, members=members,
            ))
            continue

        if not db_users:
            result.deferred.append(PersonRow(
                email=email, name=rep_name, user_id=None, user_full_name=None,
                note="email present, no DB user", similar=_similar(rep_name),
                entries=[_build_entry(r, store_index, None, None, {}) for r in rows],
            ))
            continue

        user = db_users[0]
        member_id = member_by_user.get(user.id)
        person = PersonRow(
            email=email, name=rep_name,
            user_id=str(user.id), user_full_name=user.full_name,
        )
        if member_id is None:
            person.note = "no org membership (legacy account) — cannot assign"
            person.entries = [_build_entry(r, store_index, None, None, {}) for r in rows]
            for e in person.entries:
                e.action = ACTION_INVALID
                e.warning = "no org_member row"
            result.people.append(person)
            continue

        # 같은 (매장, 사람) 중복 행 제거 후 매장별 엔트리 구성
        seen_pairs: set[tuple[str, str]] = set()
        for r in rows:
            pair = (r.company, r.emp_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            entry = _build_entry(r, store_index, member_id, user, empid_map, work_map)
            # 그룹 공유 스코프 내 타 매장 번호와 충돌 경고 (막지 않음 — D2 결정)
            if entry.store_id and entry.emp_id is not None:
                others = await _scope_other_empids(UUID(entry.store_id))
                if entry.emp_id in others:
                    entry.warning = "same number already used by another store in this group"
            person.entries.append(entry)

        # 같은 매장에 서로 다른 두 값이 오는 경우 — 뒤 값에 경고
        by_store: dict[str, int] = {}
        for e in person.entries:
            if e.store_id and e.emp_id is not None:
                if e.store_id in by_store and by_store[e.store_id] != e.emp_id:
                    e.action = ACTION_INVALID
                    e.warning = "conflicting numbers for the same store in file"
                else:
                    by_store[e.store_id] = e.emp_id
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
            warning="no matching store in this org",
        )
    if emp_int is None:
        return ImportEntry(
            store_id=str(store.id), store_name=store.name, company=r.company,
            emp_id_raw=r.emp_id, emp_id=None, current_empid=None,
            has_assignment=False, action=ACTION_INVALID,
            warning="emp_id is not a positive integer",
        )
    if member_id is None:
        return ImportEntry(
            store_id=str(store.id), store_name=store.name, company=r.company,
            emp_id_raw=r.emp_id, emp_id=emp_int, current_empid=None,
            has_assignment=False, action=ACTION_INVALID,
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
    assignments: list[tuple[UUID, UUID, int]],  # (user_id, store_id, empid)
) -> CommitResult:
    """확정 — 매장 단위 3-phase 로 empid 재기입. 단일 트랜잭션, 멱등.

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

    # 매장별 mapping 구성 — {store_id: {org_member_id: empid}}.
    # claims 는 매장별 역맵(empid→member) — 같은 매장에 같은 번호를 두 사람이 요청하면
    # 현재 보유자를 우선하고 나머지는 거절한다 (전체 롤백/무단 재채번 방지).
    per_store: dict[UUID, dict[UUID, int]] = {}
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
        if empid < 1:
            result.rejected.append({"user_id": str(user_id), "store_id": str(store_id),
                                    "reason": "empid must be >= 1"})
            continue
        mapping = per_store.setdefault(store_id, {})
        store_claims = claims.setdefault(store_id, {})
        if member_id in mapping and mapping[member_id] != empid:
            result.rejected.append({"user_id": str(user_id), "store_id": str(store_id),
                                    "reason": "conflicting values for the same store in request"})
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

    def _label(member_id: UUID, store_id: UUID) -> tuple[str, str]:
        user_id = member_user.get(member_id)
        return (
            user_names.get(user_id, str(user_id)) if user_id else str(member_id),
            store_by_id[store_id].name,
        )

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
            wanted_values = set(mapping.values())

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
