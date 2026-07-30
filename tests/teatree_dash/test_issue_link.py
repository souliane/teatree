"""``issue_link`` maps a ticket ``issue_url`` to a clickable ``(href, ref)``."""

from teatree.dash.issue_link import issue_link


class TestIssueLink:
    def test_github_issue_url_yields_hash_ref(self) -> None:
        href, ref = issue_link("https://github.com/souliane/teatree/issues/3205")
        assert href == "https://github.com/souliane/teatree/issues/3205"
        assert ref == "#3205"

    def test_github_pull_url_yields_bang_ref(self) -> None:
        href, ref = issue_link("https://github.com/souliane/teatree/pull/3230")
        assert href == "https://github.com/souliane/teatree/pull/3230"
        assert ref == "!3230"

    def test_gitlab_merge_request_url_yields_bang_ref(self) -> None:
        href, ref = issue_link("https://gitlab.com/group/proj/-/merge_requests/42")
        assert href == "https://gitlab.com/group/proj/-/merge_requests/42"
        assert ref == "!42"

    def test_scanning_news_sentinel_is_not_a_link(self) -> None:
        assert issue_link("scanning-news://t3-teatree") == ("", "")

    def test_eval_local_sentinel_is_not_a_link(self) -> None:
        assert issue_link("eval-local://t3-teatree") == ("", "")

    def test_dogfood_smoke_sentinel_is_not_a_link(self) -> None:
        assert issue_link("dogfood-smoke://t3-teatree") == ("", "")

    def test_blank_url_is_not_a_link(self) -> None:
        assert issue_link("") == ("", "")

    def test_http_url_without_number_falls_back_to_last_segment(self) -> None:
        href, ref = issue_link("https://example.com/board/overview")
        assert href == "https://example.com/board/overview"
        assert ref == "overview"
