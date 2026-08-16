"""Unit tests — 이슈 커스텀 필드 해석/검증/스냅샷.

이 모듈이 지켜야 하는 문장 셋:
1. 표시 대상 = 전역 custom_fields + 선택 카테고리의 fields, 순서는 field_order
2. 물어본 필드는 전부 키를 만든다 — 미응답은 null (안 물어봄 = 키 없음)
3. 스냅샷은 서버가 만든다 — 템플릿이 바뀌어도 과거 리포트가 해석돼야 한다
"""

import pytest

from app.core.issue_fields import (
    build_fields_snapshot,
    render_field_values_text,
    resolve_issue_fields,
    validate_and_normalize_values,
)
from app.core.error_codes.reports import (
    ISSUE_FIELD_REQUIRED,
    ISSUE_FIELD_VALUE_INVALID,
    ISSUE_FIELD_VALUES_MALFORMED,
)
from app.utils.exceptions import AppError


def _code(exc_info) -> str:
    """AppError 는 code/params 를 detail dict 에 평탄하게 담는다."""
    return exc_info.value.detail["code"]


def _params(exc_info) -> dict:
    return exc_info.value.detail


def F(fid, **kw):
    return {"id": fid, "type": kw.pop("type", "short_text"),
            "label": kw.pop("label", fid.title()), **kw}


TPL = {
    "custom_fields": [F("global_note", sort_order=5)],
    "categories": [
        {"code": "review", "label": "Review", "fields": [
            F("platform", type="single_choice", options=["Google", "Yelp"], required=True),
            F("rating", type="number", min=1, max=5, decimals=0),
            F("followup", type="single_choice", options=["Yes", "No"]),
        ]},
        {"code": "equipment", "label": "Equipment", "fields": [F("asset")]},
    ],
    "field_order": ["__title", "platform", "rating", "__description", "global_note"],
}


class TestResolve:
    def test_global_plus_category_fields(self):
        ids = [f["id"] for f in resolve_issue_fields(TPL, "review")]
        assert set(ids) == {"global_note", "platform", "rating", "followup"}

    def test_other_category_fields_excluded(self):
        ids = [f["id"] for f in resolve_issue_fields(TPL, "equipment")]
        assert set(ids) == {"global_note", "asset"}
        assert "platform" not in ids

    def test_field_order_is_respected(self):
        ids = [f["id"] for f in resolve_issue_fields(TPL, "review")]
        assert ids[:3] == ["platform", "rating", "global_note"]

    def test_standard_placeholders_are_not_fields(self):
        ids = [f["id"] for f in resolve_issue_fields(TPL, "review")]
        assert not any(i.startswith("__") for i in ids)

    def test_field_missing_from_order_still_appears(self):
        """field_order 갱신을 깜빡해도 필드가 사라지면 안 된다."""
        ids = [f["id"] for f in resolve_issue_fields(TPL, "review")]
        assert "followup" in ids

    def test_no_category_gives_only_global(self):
        ids = [f["id"] for f in resolve_issue_fields(TPL, None)]
        assert ids == ["global_note"]

    def test_empty_template_is_safe(self):
        assert resolve_issue_fields({}, "review") == []
        assert resolve_issue_fields(None, None) == []

    def test_category_field_overrides_global_with_same_id(self):
        tpl = {
            "custom_fields": [F("dup", label="Global")],
            "categories": [{"code": "c", "fields": [F("dup", label="Category")]}],
        }
        got = resolve_issue_fields(tpl, "c")
        assert len(got) == 1 and got[0]["label"] == "Category"


class TestMissingAnswers:
    """D4 — 미응답 vs 안 물어봄."""

    def test_asked_but_unanswered_becomes_null(self):
        fields = resolve_issue_fields(TPL, "review")
        out = validate_and_normalize_values(fields, {"platform": "Google"})
        assert out["followup"] is None, "물어본 필드는 키가 있어야 한다"
        assert out["rating"] is None

    def test_unasked_field_has_no_key(self):
        fields = resolve_issue_fields(TPL, "equipment")
        out = validate_and_normalize_values(fields, {})
        assert "platform" not in out, "안 물어본 필드는 키 자체가 없어야 한다"

    def test_explicit_no_is_distinct_from_null(self):
        fields = resolve_issue_fields(TPL, "review")
        answered = validate_and_normalize_values(
            fields, {"platform": "Google", "followup": "No"})
        skipped = validate_and_normalize_values(fields, {"platform": "Google"})
        assert answered["followup"] == "No"
        assert skipped["followup"] is None
        assert answered["followup"] != skipped["followup"]

    def test_blank_string_is_treated_as_unanswered(self):
        fields = resolve_issue_fields(TPL, "equipment")
        out = validate_and_normalize_values(fields, {"asset": ""})
        assert out["asset"] is None

    def test_unknown_keys_are_preserved(self):
        """구버전 클라가 보낸 정의 밖 키를 조용히 지우지 않는다."""
        fields = resolve_issue_fields(TPL, "equipment")
        out = validate_and_normalize_values(fields, {"asset": "Fryer", "legacy": "x"})
        assert out["legacy"] == "x"


class TestValidation:
    def test_required_blank_rejected(self):
        fields = resolve_issue_fields(TPL, "review")
        with pytest.raises(AppError) as ei:
            validate_and_normalize_values(fields, {})
        assert _code(ei) == ISSUE_FIELD_REQUIRED.code
        assert _params(ei).get("field") == "Platform", "어느 항목인지 알려줘야 한다"

    def test_single_choice_must_match_options(self):
        fields = resolve_issue_fields(TPL, "review")
        with pytest.raises(AppError) as ei:
            validate_and_normalize_values(fields, {"platform": "Naver"})
        assert _code(ei) == ISSUE_FIELD_VALUE_INVALID.code
        assert _params(ei).get("options") == ["Google", "Yelp"]

    def test_multi_choice_all_must_match(self):
        fields = [F("tags", type="multi_choice", options=["a", "b"])]
        assert validate_and_normalize_values(fields, {"tags": ["a", "b"]})["tags"] == ["a", "b"]
        with pytest.raises(AppError):
            validate_and_normalize_values(fields, {"tags": ["a", "z"]})

    @pytest.mark.parametrize("bad", ["abc", None if False else "x1"])
    def test_number_rejects_non_numeric(self, bad):
        fields = [F("n", type="number")]
        with pytest.raises(AppError):
            validate_and_normalize_values(fields, {"n": bad})

    def test_number_range(self):
        fields = resolve_issue_fields(TPL, "review")
        base = {"platform": "Google"}
        assert validate_and_normalize_values(fields, {**base, "rating": 3})["rating"] == 3
        with pytest.raises(AppError) as lo:
            validate_and_normalize_values(fields, {**base, "rating": 0})
        assert _params(lo).get("reason") == "min"
        with pytest.raises(AppError) as hi:
            validate_and_normalize_values(fields, {**base, "rating": 9})
        assert _params(hi).get("reason") == "max"

    def test_decimals_zero_requires_whole_number(self):
        fields = [F("n", type="number", decimals=0)]
        assert validate_and_normalize_values(fields, {"n": 4})["n"] == 4
        with pytest.raises(AppError):
            validate_and_normalize_values(fields, {"n": 4.5})

    def test_decimals_two_allows_and_rounds(self):
        fields = [F("n", type="number", decimals=2)]
        assert validate_and_normalize_values(fields, {"n": 4.567})["n"] == 4.57

    def test_decimals_missing_defaults_to_integer(self):
        fields = [F("n", type="number")]
        with pytest.raises(AppError):
            validate_and_normalize_values(fields, {"n": 1.5})

    def test_max_length_enforced(self):
        fields = [F("s", max_length=3)]
        assert validate_and_normalize_values(fields, {"s": "abc"})["s"] == "abc"
        with pytest.raises(AppError):
            validate_and_normalize_values(fields, {"s": "abcd"})

    def test_values_must_be_object(self):
        with pytest.raises(AppError) as ei:
            validate_and_normalize_values([], ["nope"])
        assert _code(ei) == ISSUE_FIELD_VALUES_MALFORMED.code


class TestDeprecatedType:
    """D5 — checkbox 폐기. 남아 있어도 작성을 막지 않는다."""

    def test_unknown_type_does_not_block_submission(self):
        fields = [F("legacy_cb", type="checkbox")]
        out = validate_and_normalize_values(fields, {"legacy_cb": True})
        assert out["legacy_cb"] is True

    def test_unknown_type_still_honors_required(self):
        fields = [F("legacy_cb", type="checkbox", required=True)]
        with pytest.raises(AppError) as ei:
            validate_and_normalize_values(fields, {})
        assert _code(ei) == ISSUE_FIELD_REQUIRED.code


class TestSnapshot:
    def test_snapshot_keeps_interpretation_keys(self):
        snap = build_fields_snapshot(resolve_issue_fields(TPL, "review"))
        by_id = {s["id"]: s for s in snap}
        assert by_id["platform"]["options"] == ["Google", "Yelp"]
        assert by_id["platform"]["required"] is True
        assert by_id["rating"]["type"] == "number"
        assert by_id["rating"]["min"] == 1

    def test_snapshot_covers_every_asked_field(self):
        fields = resolve_issue_fields(TPL, "review")
        snap = build_fields_snapshot(fields)
        assert {s["id"] for s in snap} == {f["id"] for f in fields}

    def test_snapshot_skips_fields_without_id(self):
        assert build_fields_snapshot([{"type": "short_text", "label": "x"}]) == []


class TestRenderText:
    def test_renders_answered_only(self):
        fields = resolve_issue_fields(TPL, "review")
        vals = validate_and_normalize_values(
            fields, {"platform": "Google", "rating": 2})
        text = render_field_values_text(build_fields_snapshot(fields), vals)
        assert "Platform: Google" in text
        assert "Rating: 2" in text
        assert "Followup" not in text, "미응답은 줄을 만들지 않는다"

    def test_multi_choice_joined(self):
        fields = [F("tags", type="multi_choice", options=["a", "b"], label="Tags")]
        text = render_field_values_text(fields, {"tags": ["a", "b"]})
        assert text == "Tags: a, b"

    def test_empty_values_render_empty(self):
        assert render_field_values_text([], None) == ""


class TestErrorMessagesNameTheField:
    """검증 실패 메시지는 **어느 항목이 왜 막혔는지**를 담아야 한다.

    2026-08-15 검증에서 나온 문제 — 코드는 맞게 나가는데 문구가 "허용되지 않는 값" 뿐이라
    필드가 여러 개인 폼에서 작성자가 어느 칸을 고쳐야 하는지 알 수 없었다.
    서버 문구를 콘솔·앱이 그대로 쓰므로 여기서 고치면 3-repo 가 함께 좋아진다.
    """

    def _raise(self, fields, values):
        with pytest.raises(AppError) as ei:
            validate_and_normalize_values(fields, values)
        return ei.value.detail

    def test_required_message_names_the_field(self):
        d = self._raise([F("rating", type="number", label="Rating", required=True)], {})
        assert "Rating" in d["message"], "메시지에 필드 라벨이 들어가야 한다"
        assert d["hint"]

    def test_range_hint_states_the_bounds(self):
        f = [F("rating", type="number", label="Rating", min=1, max=5)]
        d = self._raise(f, {"rating": 9})
        assert "Rating" in d["message"]
        assert "1" in d["hint"] and "5" in d["hint"], "허용 범위를 알려줘야 한다"

    def test_whole_number_hint_explains_why(self):
        d = self._raise([F("rating", type="number", label="Rating", decimals=0)],
                        {"rating": 3.5})
        assert "whole number" in d["hint"].lower()

    def test_options_hint_lists_choices(self):
        f = [F("p", type="single_choice", label="Platform", options=["Google", "Yelp"])]
        d = self._raise(f, {"p": "Naver"})
        assert "Platform" in d["message"]
        assert "Google" in d["hint"] and "Yelp" in d["hint"]

    def test_max_length_hint_states_the_limit(self):
        d = self._raise([F("s", label="Plan", max_length=3)], {"s": "abcd"})
        assert "Plan" in d["message"]
        assert "3" in d["hint"]

    def test_label_missing_falls_back_to_id(self):
        """라벨을 안 채운 필드라도 식별자는 나와야 한다 — 빈 따옴표는 안 된다."""
        d = self._raise([{"id": "f_123", "type": "short_text", "required": True}], {})
        assert "f_123" in d["message"]
