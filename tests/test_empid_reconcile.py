"""EMPID reconcile 서비스 — 순수 분류/파싱 단위 테스트 (DB 불필요).

버킷 분기 전수: auto / multiple / placeholder(도메인·공유) / assigned / deferred / no-email,
그리고 parse_emplist의 PURADAK 제외 + emp_id 문자열 보존.
"""

import io
from dataclasses import dataclass
from uuid import uuid4

import openpyxl

from app.services.empid_reconcile_service import (
    EmpRow,
    classify,
    parse_emplist,
)


@dataclass
class FakeUser:
    id: object
    full_name: str
    email: str
    employee_no: str | None = None


def _row(email, emp_id, name="John Doe", company="IL FIORA", abr="IFO"):
    return EmpRow(company=company, corp_abr=abr, name=name, emp_id=emp_id, email=email)


def test_auto_single_match() -> None:
    u = FakeUser(uuid4(), "Camille Ilar", "c@x.com")
    res = classify([_row("c@x.com", "415", "Camille Ilar")], {"c@x.com": [u]})
    assert len(res.auto) == 1 and res.auto[0].emp_id == "415"
    assert res.auto[0].user_id == u.id


def test_multiple_same_person_multi_store() -> None:
    u = FakeUser(uuid4(), "Camille Ilar", "c@x.com")
    rows = [_row("c@x.com", "415", "CAMILLE ILAR (CAMILLE)"),
            _row("c@x.com", "1226", "CAMILLE ILAR (CAMILLE)", company="M KOREAN BBQ", abr="ODG")]
    res = classify(rows, {"c@x.com": [u]})
    assert len(res.multiple) == 1
    assert sorted(res.multiple[0].emp_id_options) == ["1226", "415"]
    assert not res.auto


def test_placeholder_internal_domain() -> None:
    rows = [_row("lucillaoh@tigersplus.com", "375", "SINDY HERNANDEZ")]
    res = classify(rows, {})
    assert len(res.placeholder) == 1 and not res.deferred


def test_placeholder_shared_different_people() -> None:
    # 같은 이메일, 다른 사람(퍼스트네임 다름) → 공유 이메일로 매칭 제외
    rows = [_row("share@x.com", "1", "ALICE KIM"), _row("share@x.com", "2", "BOB LEE")]
    res = classify(rows, {"share@x.com": [FakeUser(uuid4(), "Alice", "share@x.com")]})
    assert len(res.placeholder) == 1 and not res.auto and not res.multiple


def test_already_assigned_skipped() -> None:
    u = FakeUser(uuid4(), "Camille Ilar", "c@x.com", employee_no="999")
    res = classify([_row("c@x.com", "415", "Camille Ilar")], {"c@x.com": [u]})
    assert len(res.assigned) == 1 and not res.auto


def test_deferred_no_db_user() -> None:
    res = classify([_row("ghost@x.com", "415", "Ghost Person")], {})
    assert len(res.deferred) == 1 and res.deferred[0].note == "email present, no DB user"


def test_deferred_no_email() -> None:
    res = classify([_row(None, "415", "No Email Person")], {})
    assert len(res.deferred) == 1 and res.deferred[0].note == "no email"


def test_parse_excludes_puradak_and_preserves_emp_id_string() -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([" COMPANY", "CORP_ABR_3", "Name", "emp_id", "Email"])
    ws.append(["IL FIORA", "IFO", "Alice", 415, "a@x.com"])         # float→"415"
    ws.append(["PURADAK  BP", None, "Ghost", 5021, "g@x.com"])      # 제외
    ws.append(["SEED AND WATER", "SWC", "Bob", "07", "b@x.com"])    # 선행0 보존
    buf = io.BytesIO()
    wb.save(buf)
    rows, excluded = parse_emplist(buf.getvalue())
    assert excluded == 1
    emp_ids = {r.emp_id for r in rows}
    assert "415" in emp_ids and "07" in emp_ids
    assert all("PURADAK" not in r.company.upper() for r in rows)
