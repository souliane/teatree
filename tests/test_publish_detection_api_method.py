"""Tests for the effective-HTTP-method `gh`/`glab api` classifier (#1530).

`segment_is_api_write` and its `_api_effective_method` helper mirror the
gh (2.87.x) / glab (1.80.x) last-wins method resolution the merge / review-post
gates already encode (`hook_router._is_raw_review_write`): a read-only `api`
call (effective GET/HEAD) is not a publish surface, a mutating call is.
"""

import pytest

from teatree.hooks._publish_detection import (
    _api_effective_method,
    command_has_token_aware_publish_surface,
    segment_is_api_call,
    segment_is_api_read,
    segment_is_api_write,
)


class TestApiEffectiveMethod:
    @pytest.mark.parametrize(
        ("words", "expected"),
        [
            (["gh", "api", "user"], "GET"),
            (["gh", "api", "repos/o/r/issues", "-X", "GET"], "GET"),
            (["gh", "api", "repos/o/r/issues", "--method=GET"], "GET"),
            (["gh", "api", "repos/o/r/issues", "-XGET"], "GET"),
            (["gh", "api", "repos/o/r/issues", "-f", "body=x"], "POST"),
            (["gh", "api", "repos/o/r/issues", "--field", "body=x"], "POST"),
            (["gh", "api", "repos/o/r/issues", "-X", "POST", "-f", "body=x"], "POST"),
            (["gh", "api", "repos/o/r/issues/1", "--method", "PATCH"], "PATCH"),
            (["gh", "api", "repos/o/r/issues/1", "-X", "DELETE"], "DELETE"),
            (["gh", "api", "repos/o/r/issues", "-X", "GET", "-X", "POST"], "POST"),
            (["gh", "api", "repos/o/r/issues", "-X", "POST", "-X", "GET"], "GET"),
        ],
    )
    def test_effective_method(self, words: list[str], expected: str) -> None:
        assert _api_effective_method(words) == expected


class TestSegmentIsApiWrite:
    @pytest.mark.parametrize(
        "words",
        [
            ["gh", "api", "user"],
            ["gh", "api", "repos/o/r/commits/main"],
            ["gh", "api", "repos/o/r/issues", "--method", "GET"],
            ["glab", "api", "projects/42/merge_requests"],
        ],
    )
    def test_reads_are_not_writes(self, words: list[str]) -> None:
        assert segment_is_api_call(words)
        assert not segment_is_api_write(words)

    @pytest.mark.parametrize(
        "words",
        [
            ["gh", "api", "repos/o/r/issues", "-f", "body=x"],
            ["gh", "api", "repos/o/r/issues", "-X", "POST", "-f", "body=x"],
            ["glab", "api", "projects/42/merge_requests/7/notes", "-X", "PUT", "-f", "body=x"],
            ["gh", "api", "repos/o/r/issues/1", "-X", "DELETE"],
        ],
    )
    def test_writes_are_writes(self, words: list[str]) -> None:
        assert segment_is_api_write(words)

    def test_non_api_segment_is_not_a_write(self) -> None:
        assert not segment_is_api_write(["git", "status"])


class TestSegmentIsApiRead:
    """A read-only ``gh``/``glab api`` call posts NO body and can never leak content."""

    @pytest.mark.parametrize(
        "words",
        [
            ["gh", "api", "user"],
            ["gh", "api", "repos/o/r/commits/main"],
            ["gh", "api", "repos/o/r/issues", "--method", "GET"],
            ["gh", "api", "repos/o/r/issues", "-X", "GET"],
            ["glab", "api", "projects/42/merge_requests"],
            ["glab", "api", "projects/42/issues", "-X", "POST", "-X", "GET"],
        ],
    )
    def test_reads_are_reads(self, words: list[str]) -> None:
        assert segment_is_api_read(words)
        assert not segment_is_api_write(words)

    @pytest.mark.parametrize(
        "words",
        [
            ["gh", "api", "repos/o/r/issues", "-f", "body=x"],
            ["gh", "api", "repos/o/r/issues", "-X", "POST"],
            ["gh", "api", "repos/o/r/issues/1", "-X", "DELETE"],
            ["glab", "api", "projects/42/notes", "-X", "PUT", "-f", "body=x"],
        ],
    )
    def test_writes_are_not_reads(self, words: list[str]) -> None:
        assert not segment_is_api_read(words)

    def test_non_api_segment_is_not_a_read(self) -> None:
        assert not segment_is_api_read(["git", "status"])
        assert not segment_is_api_read(["gh", "pr", "create", "--body", "x"])


class TestTokenAwarePublishSurface:
    def test_read_only_api_is_not_a_publish_surface(self) -> None:
        assert not command_has_token_aware_publish_surface("gh api user")

    def test_write_api_after_interspersed_flag_is_a_publish_surface(self) -> None:
        assert command_has_token_aware_publish_surface("gh --hostname github.com api repos/o/r/issues -f body=x")

    def test_chained_read_then_write_api_is_a_publish_surface(self) -> None:
        assert command_has_token_aware_publish_surface("gh api user && gh api repos/o/r/issues -f body=x")
