"""Unit tests — 카테고리 기본 필드 시드 / 프리셋 은퇴.

2026-08-15 검증에서 나온 문제: Review 를 고르면 Platform·Rating **필드**가 뜨는 동시에
description 프리셋에도 "Platform:" "Rating:" 줄이 채워져 같은 항목을 두 번 물었다.
항목은 필드로만 묻고 프리셋은 비운다 — 그 규칙을 여기서 고정한다.
"""

from app.core.issue_fields import validate_and_normalize_values
from app.schemas.report import (
    DEFAULT_ISSUE_CATEGORY_DESCRIPTION_TEMPLATES,
    DEFAULT_ISSUE_CATEGORY_FIELDS,
    ISSUE_FIELD_TYPES,
    LEGACY_ISSUE_CATEGORY_DESCRIPTION_TEMPLATES,
)


class TestNoDoubleAsking:
    def test_category_with_fields_has_no_preset(self):
        """필드를 주는 카테고리는 프리셋을 갖지 않는다 — 두 번 묻지 않기 위해."""
        for code in DEFAULT_ISSUE_CATEGORY_FIELDS:
            assert code not in DEFAULT_ISSUE_CATEGORY_DESCRIPTION_TEMPLATES, (
                f"{code}: 필드와 프리셋을 둘 다 주면 같은 항목을 두 번 묻게 된다"
            )

    def test_retired_preset_is_registered_as_legacy(self):
        """승격시킨 옛 프리셋 원문은 LEGACY 에 남아야 startup 보정이 비울 수 있다."""
        for code in DEFAULT_ISSUE_CATEGORY_FIELDS:
            assert LEGACY_ISSUE_CATEGORY_DESCRIPTION_TEMPLATES.get(code), (
                f"{code}: 옛 원문이 LEGACY 에 없으면 기존 환경에 프리셋이 남는다"
            )

    def test_review_legacy_covers_both_old_wordings(self):
        """줄바꿈만 다른 두 판본 모두 은퇴 대상이어야 한다."""
        legacy = LEGACY_ISSUE_CATEGORY_DESCRIPTION_TEMPLATES["review"]
        assert len(legacy) >= 2
        assert any("\n\n" in t for t in legacy), "빈 줄 판본"
        assert any("\n\n" not in t for t in legacy), "붙은 줄 판본"


class TestSeedFieldsAreValid:
    def test_types_are_supported(self):
        for code, fields in DEFAULT_ISSUE_CATEGORY_FIELDS.items():
            for f in fields:
                assert f["type"] in ISSUE_FIELD_TYPES, f"{code}.{f['id']}: {f['type']}"

    def test_ids_are_unique_within_category(self):
        for code, fields in DEFAULT_ISSUE_CATEGORY_FIELDS.items():
            ids = [f["id"] for f in fields]
            assert len(ids) == len(set(ids)), f"{code}: id 중복"

    def test_every_field_has_label(self):
        for code, fields in DEFAULT_ISSUE_CATEGORY_FIELDS.items():
            for f in fields:
                assert f.get("label"), f"{code}.{f['id']}: label 없음"

    def test_choice_fields_have_options(self):
        for code, fields in DEFAULT_ISSUE_CATEGORY_FIELDS.items():
            for f in fields:
                if f["type"].endswith("_choice"):
                    assert f.get("options"), f"{code}.{f['id']}: 선택지 없음"

    def test_seed_fields_pass_their_own_validator(self):
        """시드가 자기 검증기를 통과하지 못하면 신규 조직이 이슈를 못 올린다."""
        for code, fields in DEFAULT_ISSUE_CATEGORY_FIELDS.items():
            answers = {}
            for f in fields:
                if f["type"] == "number":
                    answers[f["id"]] = f.get("min", 1)
                elif f["type"] == "single_choice":
                    answers[f["id"]] = f["options"][0]
                elif f["type"] == "multi_choice":
                    answers[f["id"]] = [f["options"][0]]
                else:
                    answers[f["id"]] = "x"
            out = validate_and_normalize_values(fields, answers)
            assert set(out) == {f["id"] for f in fields}

    def test_seed_fields_are_not_required(self):
        """시드는 아무것도 강제하지 않는다.

        이 필드들은 예전 description 프리셋(안내 텍스트)을 대체하는 것이다.
        프리셋은 비워두고 제출할 수 있었으므로, 시드가 required 를 걸면
        **기존 작성 흐름이 조용히 막힌다**(실제로 기존 테스트가 깨져서 발견).
        필수 여부는 운영자가 콘솔에서 정한다.
        """
        for code, fields in DEFAULT_ISSUE_CATEGORY_FIELDS.items():
            for f in fields:
                assert not f.get("required"), (
                    f"{code}.{f['id']}: 시드 필드는 required 를 걸지 않는다"
                )

    def test_empty_submit_passes_with_seed_fields(self):
        """아무것도 안 채워도 제출은 통과하고, 물어본 필드는 null 로 남는다."""
        fields = DEFAULT_ISSUE_CATEGORY_FIELDS["review"]
        out = validate_and_normalize_values(fields, {})
        assert set(out) == {f["id"] for f in fields}
        assert all(v is None for v in out.values())
