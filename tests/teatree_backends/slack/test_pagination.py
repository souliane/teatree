"""Branch coverage for the shared Slack cursor extractor (#3507)."""

from teatree.backends.slack.pagination import next_cursor


class TestNextCursor:
    def test_returns_the_cursor_when_present(self) -> None:
        assert next_cursor({"response_metadata": {"next_cursor": "abc"}}) == "abc"

    def test_none_when_metadata_absent(self) -> None:
        assert next_cursor({"ok": True}) is None

    def test_none_when_metadata_not_a_dict(self) -> None:
        assert next_cursor({"response_metadata": "nope"}) is None

    def test_none_when_cursor_empty_string(self) -> None:
        assert next_cursor({"response_metadata": {"next_cursor": ""}}) is None

    def test_none_when_cursor_not_a_string(self) -> None:
        assert next_cursor({"response_metadata": {"next_cursor": 7}}) is None
