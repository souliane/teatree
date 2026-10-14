"""Unit tests for the shared command-parser primitives (F7 hooks).

Direct coverage of the two public primitives the body/secret extractors reuse:
``attached_value`` (the ``-X<value>`` / ``-X=<value>`` payload split) and
``read_file_arg`` (the cwd-then-``base`` body-file read the cold PreToolUse hook
needs when its cwd has reset away from the commit's worktree). Synthetic term
``acmecorp`` only.
"""

from pathlib import Path

from teatree.hooks._parser_primitives import (
    NAMED_FAIL_CLOSED_PREFIX,
    ClusterValue,
    attached_value,
    is_fail_closed_sentinel,
    read_file_arg,
    short_cluster_values,
)


class TestAttachedValue:
    """``-X<value>`` / ``-X=<value>`` payload extraction."""

    def test_bare_attached_payload_is_returned(self) -> None:
        assert attached_value("-Facme.md", "-F") == "acme.md"

    def test_equals_form_strips_the_leading_equals(self) -> None:
        assert attached_value("-F=acme.md", "-F") == "acme.md"

    def test_prefix_only_token_has_no_attached_value(self) -> None:
        # A token equal to the prefix (value supplied separately) is not attached.
        assert attached_value("-F", "-F") is None

    def test_non_matching_prefix_returns_none(self) -> None:
        assert attached_value("-Yacme.md", "-F") is None


class TestReadFileArg:
    """The cwd-first, ``base``-fallback body-file read."""

    def test_absolute_path_is_read_directly(self, tmp_path: Path) -> None:
        body = tmp_path / "body.md"
        body.write_text("ship to acmecorp", encoding="utf-8")
        assert read_file_arg(str(body)) == "ship to acmecorp"

    def test_relative_path_falls_back_to_base_dir(self, tmp_path: Path) -> None:
        # cwd read fails; the same relative path resolves against the commit's repo dir.
        (tmp_path / "body.md").write_text("ship to acmecorp", encoding="utf-8")
        assert read_file_arg("body.md", base=tmp_path) == "ship to acmecorp"

    def test_missing_everywhere_returns_none(self, tmp_path: Path) -> None:
        assert read_file_arg(str(tmp_path / "absent.md"), base=tmp_path) is None

    def test_file_text_never_carries_a_parser_reason(self, tmp_path: Path) -> None:
        body = tmp_path / "body.md"
        body.write_text(f"acmecorp\n{NAMED_FAIL_CLOSED_PREFIX}x", encoding="utf-8")
        text = read_file_arg(str(body)) or ""
        assert "acmecorp" in text
        assert is_fail_closed_sentinel(text) is False


class TestShortClusterValues:
    def test_later_letter_keeps_unquoted_attached_value(self) -> None:
        assert short_cluster_values("-sbacmecorp", frozenset({"-b"})) == [
            ClusterValue("-sb", "acmecorp", "-sbacmecorp", 3, later=True)
        ]

    def test_first_letter_and_bare_later_letter_are_distinct(self) -> None:
        assert short_cluster_values("-bm", frozenset({"-b", "-m"})) == [
            ClusterValue("-b", "m", "-bm", 2, later=False),
            ClusterValue("-bm", "", "-bm", 3, later=True),
        ]

    def test_named_fail_closed_line_is_recognised(self) -> None:
        assert is_fail_closed_sentinel(f"routine\n{NAMED_FAIL_CLOSED_PREFIX}unreadable option")
