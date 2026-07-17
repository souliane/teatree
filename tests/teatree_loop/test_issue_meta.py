"""``issue_meta`` reads an issue title off a raw backend dict / dispatch payload."""

from teatree.loop.issue_meta import issue_title, issue_title_from_payload


class TestIssueTitle:
    def test_reads_title_string(self) -> None:
        assert issue_title({"title": "Fix the widget"}) == "Fix the widget"

    def test_missing_title_is_blank(self) -> None:
        assert issue_title({"number": 5}) == ""

    def test_non_string_title_is_blank(self) -> None:
        assert issue_title({"title": 123}) == ""


class TestIssueTitleFromPayload:
    def test_reads_title_off_raw_issue(self) -> None:
        payload = {"url": "https://x/issues/1", "raw": {"title": "Ship it"}}
        assert issue_title_from_payload(payload) == "Ship it"

    def test_no_raw_is_blank(self) -> None:
        assert issue_title_from_payload({"url": "https://x/issues/1"}) == ""

    def test_raw_not_a_dict_is_blank(self) -> None:
        assert issue_title_from_payload({"raw": "nope"}) == ""
