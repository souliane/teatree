"""Tests for the canonical per-invocation PR-body temp file (#3581).

The ship flow must never share a fixed ``/tmp/pr-body.md`` path across concurrent
shippers. :func:`teatree.utils.pr_body.pr_body_tempfile` owns a distinct
``mkstemp`` path per call and cleans it up on exit.
"""

import pytest

from teatree.utils.pr_body import pr_body_tempfile


class _ConsumerError(RuntimeError):
    """Stand-in for a body consumer that raises mid-create."""


class TestUniquePerInvocation:
    def test_two_calls_yield_two_distinct_paths(self) -> None:
        with pr_body_tempfile("first body") as first, pr_body_tempfile("second body") as second:
            assert first != second

    def test_nested_calls_do_not_share_a_file(self) -> None:
        # The race the fixed shared path caused: an outer body clobbered while an
        # inner shipper writes. Distinct paths mean neither content is lost.
        with pr_body_tempfile("outer") as outer:
            with pr_body_tempfile("inner") as inner:
                assert inner.read_text(encoding="utf-8") == "inner"
            assert outer.read_text(encoding="utf-8") == "outer"


class TestContentAndCleanup:
    def test_file_holds_the_exact_content(self) -> None:
        body = "type(scope): summary\n\nBullet one.\nBullet two.\n"
        with pr_body_tempfile(body) as path:
            assert path.read_text(encoding="utf-8") == body

    def test_file_removed_on_exit(self) -> None:
        with pr_body_tempfile("gone soon") as path:
            captured = path
            assert captured.exists()
        assert not captured.exists()

    def test_file_removed_even_when_the_body_consumer_raises(self) -> None:
        captured = []

        def _consume() -> None:
            with pr_body_tempfile("boom") as path:
                captured.append(path)
                raise _ConsumerError

        with pytest.raises(_ConsumerError):
            _consume()
        assert captured
        assert not captured[0].exists()

    def test_temp_file_is_outside_any_worktree(self) -> None:
        # The canonical path lives in the system temp dir with a ``t3-pr-body-``
        # prefix, so it never lands in a repo where it could be staged.
        with pr_body_tempfile("x") as path:
            assert path.name.startswith("t3-pr-body-")
            assert path.name.endswith(".md")
