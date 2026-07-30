"""``review_request_dedup_max_pages`` — configurable channel-scan page cap (#3292 part 4)."""

from teatree.config.setting_registries import OVERLAY_OVERRIDABLE_SETTINGS
from teatree.config.settings import UserSettings


class TestDedupMaxPagesDefault:
    def test_default_is_five(self) -> None:
        assert UserSettings().review_request_dedup_max_pages == 5


class TestDedupMaxPagesParser:
    """Fail-safe positive int: a non-positive / mistyped value degrades to 5."""

    def test_parser_accepts_a_positive_override(self) -> None:
        parse = OVERLAY_OVERRIDABLE_SETTINGS["review_request_dedup_max_pages"]
        assert parse("20") == 20

    def test_parser_degrades_non_positive_to_default(self) -> None:
        parse = OVERLAY_OVERRIDABLE_SETTINGS["review_request_dedup_max_pages"]
        assert parse("0") == 5
        assert parse("-3") == 5

    def test_parser_degrades_mistyped_to_default(self) -> None:
        parse = OVERLAY_OVERRIDABLE_SETTINGS["review_request_dedup_max_pages"]
        assert parse("not-a-number") == 5
