"""``model@effort`` variant parsing for the matrix / benchmark lanes."""

import pytest

from teatree.eval.model_variant import (
    EFFORT_LEVELS,
    ModelVariant,
    ModelVariantError,
    parse_model_variant,
    parse_model_variants,
)


class TestParseModelVariant:
    def test_plain_model_has_no_effort(self) -> None:
        variant = parse_model_variant("claude-fable-5")
        assert variant == ModelVariant(model="claude-fable-5", effort=None)

    def test_model_at_effort_parses_both_parts(self) -> None:
        variant = parse_model_variant("claude-opus-4-8@xhigh")
        assert variant.model == "claude-opus-4-8"
        assert variant.effort == "xhigh"

    def test_surrounding_whitespace_is_stripped(self) -> None:
        variant = parse_model_variant("  claude-fable-5 @ medium ")
        assert variant == ModelVariant(model="claude-fable-5", effort="medium")

    def test_every_known_effort_level_is_accepted(self) -> None:
        for level in EFFORT_LEVELS:
            assert parse_model_variant(f"m@{level}").effort == level

    def test_unknown_effort_raises_with_known_levels_listed(self) -> None:
        with pytest.raises(ModelVariantError, match="unknown effort 'turbo'") as excinfo:
            parse_model_variant("claude-opus-4-8@turbo")
        for level in EFFORT_LEVELS:
            assert level in str(excinfo.value)

    def test_empty_model_raises(self) -> None:
        with pytest.raises(ModelVariantError, match="empty model"):
            parse_model_variant("@xhigh")

    def test_empty_effort_after_at_raises(self) -> None:
        with pytest.raises(ModelVariantError, match="unknown effort ''"):
            parse_model_variant("claude-fable-5@")


class TestVariantTag:
    def test_tag_round_trips_model_at_effort(self) -> None:
        tag = "claude-opus-4-8@xhigh"
        assert parse_model_variant(tag).tag == tag

    def test_tag_of_plain_model_is_the_model(self) -> None:
        assert parse_model_variant("claude-fable-5").tag == "claude-fable-5"


class TestParseModelVariants:
    def test_csv_parses_each_entry(self) -> None:
        variants = parse_model_variants("claude-opus-4-8@xhigh, claude-fable-5@medium, haiku")
        assert [v.tag for v in variants] == [
            "claude-opus-4-8@xhigh",
            "claude-fable-5@medium",
            "haiku",
        ]

    def test_blank_entries_are_dropped(self) -> None:
        assert parse_model_variants(" , ") == []

    def test_one_bad_entry_fails_the_whole_list(self) -> None:
        with pytest.raises(ModelVariantError):
            parse_model_variants("haiku,opus@warp")
