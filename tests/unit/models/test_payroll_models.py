"""payroll 모델 메타데이터 단위 테스트 — Payroll v1 Phase 2.

DB 없이 SQLAlchemy Table 메타데이터만 검증한다
(스펙: docs/99_inbox/2026-08-03 payroll-v1-스키마-스펙.md §4/§5/§6):
    - 테이블명 / 컬럼 목록 / nullability
    - money 컬럼 Numeric(10,2), revision Integer server default 0
    - 제약/인덱스 이름 및 partial(where) 조건
    - FK ondelete 선택 (pay_periods.store RESTRICT 등)
"""

from __future__ import annotations

from sqlalchemy import Index, Numeric, UniqueConstraint

from app.models.payroll import PayPeriod, PayrollEntry, PayrollEvent


def _fk(table, column_name: str):
    """단일 FK 컬럼의 ForeignKey 객체 반환."""
    fks = list(table.columns[column_name].foreign_keys)
    assert len(fks) == 1, f"{column_name} must have exactly one FK"
    return fks[0]


def _index(table, name: str) -> Index:
    match = [i for i in table.indexes if i.name == name]
    assert match, f"index {name} missing on {table.name}"
    return match[0]


# ---------------------------------------------------------------------------
# pay_periods (§4)
# ---------------------------------------------------------------------------


def test_pay_period_table_shape() -> None:
    t = PayPeriod.__table__
    assert t.name == "pay_periods"
    expected = {
        "id", "organization_id", "store_id", "start_date", "end_date",
        "status", "confirmed_at", "confirmed_by", "override_reason",
        "created_at", "updated_at",
    }
    assert set(t.columns.keys()) == expected

    # nullability
    for col in ("organization_id", "store_id", "start_date", "end_date", "status"):
        assert not t.columns[col].nullable, col
    for col in ("confirmed_at", "confirmed_by", "override_reason"):
        assert t.columns[col].nullable, col


def test_pay_period_fk_ondelete() -> None:
    t = PayPeriod.__table__
    assert _fk(t, "organization_id").ondelete == "CASCADE"
    # 확정 급여 기록 보존 — 매장 삭제 금지
    assert _fk(t, "store_id").ondelete == "RESTRICT"
    assert _fk(t, "confirmed_by").ondelete == "SET NULL"


def test_pay_period_unique_store_start() -> None:
    t = PayPeriod.__table__
    uqs = {c.name: c for c in t.constraints if isinstance(c, UniqueConstraint)}
    assert "uq_pay_period_store_start" in uqs
    assert [c.name for c in uqs["uq_pay_period_store_start"].columns] == [
        "store_id", "start_date",
    ]


# ---------------------------------------------------------------------------
# payroll_entries (§5)
# ---------------------------------------------------------------------------


def test_payroll_entry_table_shape() -> None:
    t = PayrollEntry.__table__
    assert t.name == "payroll_entries"
    expected = {
        "id", "pay_period_id", "organization_id", "store_id",
        "user_id", "org_member_id", "empid", "crewid", "member_name",
        "revision", "regular_minutes", "ot_minutes", "dt_minutes",
        "regular_pay", "ot_pay", "dt_pay", "penalty_pay", "card_tips", "gross_pay",
        "calc_version", "breakdown", "created_at", "updated_at",
    }
    assert set(t.columns.keys()) == expected

    # NOT NULL
    for col in (
        "pay_period_id", "organization_id", "store_id", "member_name",
        "revision", "regular_minutes", "ot_minutes", "dt_minutes",
        "regular_pay", "ot_pay", "dt_pay", "penalty_pay", "card_tips",
        "gross_pay", "calc_version", "breakdown",
    ):
        assert not t.columns[col].nullable, col
    # NULL 허용 (SET NULL 관례 / export 스냅샷)
    for col in ("user_id", "org_member_id", "empid", "crewid"):
        assert t.columns[col].nullable, col


def test_payroll_entry_money_numeric_10_2() -> None:
    t = PayrollEntry.__table__
    for col in ("regular_pay", "ot_pay", "dt_pay", "penalty_pay", "card_tips", "gross_pay"):
        typ = t.columns[col].type
        assert isinstance(typ, Numeric), col
        assert (typ.precision, typ.scale) == (10, 2), col


def test_payroll_entry_revision_server_default_zero() -> None:
    col = PayrollEntry.__table__.columns["revision"]
    assert col.server_default is not None
    assert str(col.server_default.arg) == "0"
    # calc_version 은 default 없음 — 계산기가 반드시 명시
    assert PayrollEntry.__table__.columns["calc_version"].server_default is None


def test_payroll_entry_fk_ondelete() -> None:
    t = PayrollEntry.__table__
    assert _fk(t, "pay_period_id").ondelete == "CASCADE"
    assert _fk(t, "organization_id").ondelete == "CASCADE"
    assert _fk(t, "store_id").ondelete == "CASCADE"
    assert _fk(t, "user_id").ondelete == "SET NULL"
    assert _fk(t, "org_member_id").ondelete == "SET NULL"


def test_payroll_entry_partial_unique() -> None:
    idx = _index(PayrollEntry.__table__, "uq_payroll_entry_period_user_rev")
    assert idx.unique
    assert [c.name for c in idx.columns] == ["pay_period_id", "user_id", "revision"]
    assert "user_id IS NOT NULL" in str(idx.dialect_options["postgresql"]["where"])


# ---------------------------------------------------------------------------
# payroll_events (§6)
# ---------------------------------------------------------------------------


def test_payroll_event_table_shape() -> None:
    t = PayrollEvent.__table__
    assert t.name == "payroll_events"
    expected = {
        "id", "organization_id", "store_id", "user_id", "attendance_id",
        "work_date", "kind", "reason", "attribution", "tagged_by", "tagged_at",
        "voided_at", "pay_period_id", "created_at", "updated_at",
    }
    assert set(t.columns.keys()) == expected

    for col in ("organization_id", "store_id", "work_date", "kind", "reason"):
        assert not t.columns[col].nullable, col
    # pay_period_id 는 confirm 시 부여 — NULL 허용이 스펙
    for col in (
        "user_id", "attendance_id", "attribution", "tagged_by", "tagged_at",
        "voided_at", "pay_period_id",
    ):
        assert t.columns[col].nullable, col


def test_payroll_event_fk_ondelete() -> None:
    t = PayrollEvent.__table__
    assert _fk(t, "organization_id").ondelete == "CASCADE"
    assert _fk(t, "store_id").ondelete == "CASCADE"
    assert _fk(t, "user_id").ondelete == "SET NULL"
    assert _fk(t, "attendance_id").ondelete == "SET NULL"
    assert _fk(t, "tagged_by").ondelete == "SET NULL"
    # 기간 삭제돼도 이벤트(태깅 기록) 보존
    assert _fk(t, "pay_period_id").ondelete == "SET NULL"


def test_payroll_event_indexes() -> None:
    t = PayrollEvent.__table__
    uq = _index(t, "uq_payroll_event_user_date_kind")
    assert uq.unique
    assert [c.name for c in uq.columns] == [
        "organization_id", "user_id", "work_date", "kind",
    ]
    assert "user_id IS NOT NULL" in str(uq.dialect_options["postgresql"]["where"])

    ix_store = _index(t, "ix_payroll_events_store_date")
    assert not ix_store.unique
    assert [c.name for c in ix_store.columns] == ["store_id", "work_date"]

    ix_user = _index(t, "ix_payroll_events_user_date")
    assert not ix_user.unique
    assert [c.name for c in ix_user.columns] == ["user_id", "work_date"]
