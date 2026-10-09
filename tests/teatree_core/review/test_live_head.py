"""The live-head probe answers UNREADABLE for every failure, never an empty SHA (#4737).

The distinction is the whole point of the probe. An empty head compares unequal to every
reviewed tree, so a forge hiccup flattened into one would read as a branch that moved —
spending a review claim the reviewer could still have satisfied.
"""

from typing import TYPE_CHECKING
from unittest.mock import patch

from teatree.core.modelkit.forge_readability import HEAD_SHA_UNREADABLE, LiveHeadRead
from teatree.core.review.live_head import live_head_at

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from unittest.mock import MagicMock


_SLUG = "souliane/teatree"
_PR_ID = 4716
_HEAD = "21023d20d7f4b6c1590ae83f2d47c168b95a0e34"


def _query(read: LiveHeadRead | Exception) -> "AbstractContextManager[MagicMock]":
    """A ``CodeHostQuery`` stand-in whose ``live_head_read`` answers or raises."""

    class _Query:
        @staticmethod
        def live_head_read() -> LiveHeadRead:
            if isinstance(read, Exception):
                raise read
            return read

    return patch("teatree.core.merge.ci_rollup.CodeHostQuery.for_ref", return_value=_Query())


class TestTheProbeReportsWhatTheForgeSaid:
    def test_a_readable_head_is_returned_verbatim(self) -> None:
        with _query(LiveHeadRead(sha=_HEAD, unreadable=False)):
            read = live_head_at(slug=_SLUG, pr_id=_PR_ID, host_kind="github")

        assert read.sha == _HEAD
        assert not read.unreadable

    def test_the_forges_own_unreadable_answer_is_passed_through(self) -> None:
        with _query(LiveHeadRead(sha="", unreadable=True)):
            read = live_head_at(slug=_SLUG, pr_id=_PR_ID)

        assert read.unreadable
        assert read.sha == ""


class TestAFailedReadIsNeverASilentEmpty:
    def test_a_raising_backend_answers_unreadable(self) -> None:
        with _query(RuntimeError("no backend provider is configured")):
            read = live_head_at(slug=_SLUG, pr_id=_PR_ID)

        assert read.unreadable
        assert read.sha == ""

    def test_the_probe_never_propagates_the_failure_to_its_caller(self) -> None:
        # The recorder runs inside a task-completion path; a raise here would fail the
        # task on a forge hiccup instead of refusing the one verdict and retrying.
        with _query(OSError("connection reset by peer")):
            assert live_head_at(slug=_SLUG, pr_id=_PR_ID).unreadable


class TestTheUnreadableSentinelNeverEscapesAsAHead:
    """A forge that declined names no head, so the sentinel must never reach a comparison.

    It is a truthy string, and the recorder binds a verdict to whatever head it is handed
    — so a sentinel that survived the read would bind a verdict to a tree that is not one.
    """

    @staticmethod
    def _backend(raw_sha: str) -> "AbstractContextManager[MagicMock]":
        class _Backend:
            @staticmethod
            def fetch_live_head_sha(*, slug: str, pr_id: int) -> str:
                del slug, pr_id
                return raw_sha

        return patch("teatree.core.merge.ci_rollup._code_host_for", return_value=_Backend())

    def test_a_declining_backend_answers_unreadable_with_no_sha(self) -> None:
        with self._backend(HEAD_SHA_UNREADABLE):
            read = live_head_at(slug=_SLUG, pr_id=_PR_ID)

        assert read.sha == ""
        assert read.unreadable

    def test_a_backend_that_named_a_head_is_not_reported_unreadable(self) -> None:
        with self._backend(_HEAD):
            read = live_head_at(slug=_SLUG, pr_id=_PR_ID)

        assert (read.sha, read.unreadable) == (_HEAD, False)
