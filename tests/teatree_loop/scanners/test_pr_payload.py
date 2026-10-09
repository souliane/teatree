"""Unit tests for the shared dual-forge PR payload extractors (#7 / SIG-2)."""

from teatree.loop.scanners.pr_payload import head_sha


class TestHeadSha:
    def test_gitlab_top_level_sha(self) -> None:
        # GitLab MR list endpoints expose the head commit as top-level ``sha``.
        assert head_sha({"sha": "abc123"}) == "abc123"

    def test_github_nested_head_sha(self) -> None:
        # GitHub PRs nest the head commit under ``head.sha``.
        assert head_sha({"head": {"sha": "deadbeef"}}) == "deadbeef"

    def test_gitlab_diff_refs_fallback(self) -> None:
        # GitLab MR detail endpoints carry it under ``diff_refs.head_sha``.
        assert head_sha({"diff_refs": {"head_sha": "feedface"}}) == "feedface"

    def test_top_level_sha_wins_over_nested(self) -> None:
        assert head_sha({"sha": "top", "head": {"sha": "nested"}}) == "top"

    def test_missing_everywhere_is_blank(self) -> None:
        assert head_sha({}) == ""

    def test_wrong_types_are_blank(self) -> None:
        assert head_sha({"sha": 123}) == ""
        assert head_sha({"head": {"sha": 99}}) == ""
        assert head_sha({"diff_refs": {"head_sha": True}}) == ""
        assert head_sha({"head": "not-a-dict"}) == ""
