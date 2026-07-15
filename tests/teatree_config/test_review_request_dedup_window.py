"""``review_request_dedup_window_days`` — config-driven live-Slack dedup window (#1084 follow-up)."""

from teatree.config.settings import OVERLAY_OVERRIDABLE_SETTINGS, UserSettings


class TestDedupWindowDefault:
    def test_default_is_thirty_days(self) -> None:
        assert UserSettings().review_request_dedup_window_days == 30


class TestDedupWindowParser:
    """Fail-safe positive int: a non-positive / mistyped value degrades to 30."""

    def test_parser_accepts_a_positive_override(self) -> None:
        parse = OVERLAY_OVERRIDABLE_SETTINGS["review_request_dedup_window_days"]
        assert parse("45") == 45

    def test_parser_degrades_non_positive_to_default(self) -> None:
        parse = OVERLAY_OVERRIDABLE_SETTINGS["review_request_dedup_window_days"]
        assert parse("0") == 30
        assert parse("-5") == 30

    def test_parser_degrades_mistyped_to_default(self) -> None:
        parse = OVERLAY_OVERRIDABLE_SETTINGS["review_request_dedup_window_days"]
        assert parse("not-a-number") == 30
