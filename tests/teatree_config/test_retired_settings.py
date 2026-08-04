"""The retirement registry's operator-facing rendering (souliane/teatree#4094).

The registry is the single source every surface renders from, so the notice is
asserted against the registry itself rather than a pinned sentence.
"""

from teatree.config.retired_settings import (
    CLEAR_REMEDY,
    REMOVED_SETTING_KEYS,
    RENAMED_SETTING_KEYS,
    removed_setting,
    retirement_notice,
)


class TestRetirementNotice:
    def test_a_renamed_key_is_told_where_its_value_now_lives(self) -> None:
        notice = retirement_notice("speed")
        assert RENAMED_SETTING_KEYS["speed"] in notice
        assert "renamed" in notice

    def test_a_removed_key_carries_its_reason_and_the_shared_remedy(self) -> None:
        key = "issue_implementer_require_label"
        notice = retirement_notice(key)
        assert removed_setting(key).reason in notice
        assert CLEAR_REMEDY.format(key=key) in notice

    def test_the_two_outcomes_do_not_read_alike(self) -> None:
        # A removal has no answer to give and must say so; a rename has one. Reading
        # "no successor" as "use X" is the hour the reporting issue paid for.
        removed = retirement_notice(next(iter(REMOVED_SETTING_KEYS)))
        assert "no replacement" in removed
        assert "no replacement" not in retirement_notice("speed")

    def test_a_live_key_has_no_retirement_to_report(self) -> None:
        assert retirement_notice("issue_implementer_label") is None

    def test_an_unrecorded_key_has_no_retirement_to_report(self) -> None:
        assert retirement_notice("not_a_real_setting") is None
