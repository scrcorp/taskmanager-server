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
    build_report_csv,
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


def test_already_assigned_matches_file_skipped() -> None:
    # DB 사번 == 파일 사번 → assigned(skip)
    u = FakeUser(uuid4(), "Camille Ilar", "c@x.com", employee_no="415")
    res = classify([_row("c@x.com", "415", "Camille Ilar")], {"c@x.com": [u]})
    assert len(res.assigned) == 1 and not res.auto and not res.mismatch


def test_mismatch_db_differs_from_file() -> None:
    # DB 사번 != 파일 사번 → mismatch(충돌, 덮어쓰지 않음)
    u = FakeUser(uuid4(), "Camille Ilar", "c@x.com", employee_no="999")
    res = classify([_row("c@x.com", "415", "Camille Ilar")], {"c@x.com": [u]})
    assert len(res.mismatch) == 1 and not res.assigned and not res.auto
    m = res.mismatch[0]
    assert m.db_emp_id == "999" and m.emp_id_options == ["415"]
    assert "999" in m.note and "415" in m.note


def test_multiple_includes_sources_per_company() -> None:
    u = FakeUser(uuid4(), "Camille Ilar", "c@x.com")
    rows = [_row("c@x.com", "415", "CAMILLE ILAR", company="IL FIORA"),
            _row("c@x.com", "1226", "CAMILLE ILAR", company="M KOREAN BBQ")]
    res = classify(rows, {"c@x.com": [u]})
    src = dict(res.multiple[0].emp_id_sources)
    assert src["415"] == "IL FIORA" and src["1226"] == "M KOREAN BBQ"


def test_deferred_no_db_user() -> None:
    res = classify([_row("ghost@x.com", "415", "Ghost Person")], {})
    assert len(res.deferred) == 1 and res.deferred[0].note == "email present, no DB user"


def test_deferred_shows_similar_name_hint() -> None:
    # 이메일은 매칭 안되지만 이름이 비슷한 DB 유저를 힌트로
    u = FakeUser(uuid4(), "John Doe", "john.real@x.com")
    res = classify(
        [_row("john.typo@x.com", "415", "John Doe")],
        {},  # 이메일 매칭 없음
        users=[u],
    )
    assert len(res.deferred) == 1
    sim = res.deferred[0].similar
    assert any(n == "John Doe" for n, _ in sim)


def test_no_similar_when_names_unrelated() -> None:
    u = FakeUser(uuid4(), "Maria Santos", "maria@x.com")
    res = classify([_row("bob@x.com", "415", "Bob Lee")], {}, users=[u])
    assert res.deferred[0].similar == []


def test_build_report_csv_covers_all_buckets() -> None:
    auto_u = FakeUser(uuid4(), "Auto User", "auto@x.com")
    mis_u = FakeUser(uuid4(), "Mis User", "mis@x.com", employee_no="999")
    rows = [
        _row("auto@x.com", "100", "Auto User"),
        _row("mis@x.com", "200", "Mis User"),
        _row("ghost@x.com", "300", "Ghost Person"),
    ]
    res = classify(rows, {"auto@x.com": [auto_u], "mis@x.com": [mis_u]})
    csv_text = build_report_csv(res)
    assert "bucket,name,email" in csv_text
    assert "auto" in csv_text and "mismatch" in csv_text and "deferred" in csv_text
    assert "100" in csv_text and "999" in csv_text


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


def test_parse_csv_format() -> None:
    csv_text = (
        "COMPANY,CORP_ABR_3,Name,emp_id,Email\n"
        "IL FIORA,IFO,Alice,415,a@x.com\n"
        "PURADAK BP,,Ghost,5021,g@x.com\n"
        "SEED AND WATER,SWC,Bob,07,b@x.com\n"
    )
    rows, excluded = parse_emplist(csv_text.encode("utf-8"), filename="list.csv")
    assert excluded == 1
    emp_ids = {r.emp_id for r in rows}
    assert "415" in emp_ids and "07" in emp_ids  # 선행0 보존
    assert {r.email for r in rows} == {"a@x.com", "b@x.com"}
