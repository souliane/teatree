"""F8.4 — the keystone merge RPC runner bounds every subprocess with a timeout.

An unbounded ``gh`` merge call was the one hole left on the KEYSTONE merge path:
a stalled TLS handshake wedged the single-threaded loop indefinitely. The runner
must thread :data:`_FORGE_MERGE_TIMEOUT_SECONDS` into every
``run_allowed_to_fail`` call. GitLab has no runner to bound since #4007 — it
speaks httpx, bounded by ``GitLabHTTPClient._timeout``.
"""

import json
import subprocess
from unittest.mock import patch

import pytest

from teatree.backends import forge_merge_rpc as rpc
from teatree.backends.forge_merge_rpc import _FORGE_MERGE_TIMEOUT_SECONDS, GhMergeRpc, _gh_conflict_state, gh_runner
from teatree.core.backend_protocols import HEAD_SHA_UNREADABLE, MergeConflictState


def _completed() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, "", "")


def test_gh_runner_threads_the_merge_timeout() -> None:
    with patch.object(rpc, "run_allowed_to_fail", return_value=_completed()) as mock_run:
        gh_runner("tok")(["pr", "view", "9"])
    assert mock_run.call_args.kwargs["timeout"] == _FORGE_MERGE_TIMEOUT_SECONDS


def test_gh_runner_passes_token_via_env_and_returns_tuple() -> None:
    with patch.object(rpc, "run_allowed_to_fail", return_value=subprocess.CompletedProcess([], 3, "out", "err")):
        rc, out, err = gh_runner("secret-tok")(["pr", "view", "1"])
    assert (rc, out, err) == (3, "out", "err")


def test_merge_timeout_is_positive_and_finite() -> None:
    assert _FORGE_MERGE_TIMEOUT_SECONDS > 0


class TestGhConflictState:
    """The PRIMARY forge's conflict axis, mirroring the GitLab twin's table (#4193).

    The GitLab mapper shipped with a seven-case table; the ``gh`` one shipped with
    none, on the forge this repo actually merges through. Laundering every PR to
    CLEAN passed the whole suite, which is the definition of an untested mapper:
    a real conflict would have read as mergeable and the keystone would have
    walked into it.

    The third value is load-bearing. GitHub answers ``UNKNOWN`` while it
    recomputes mergeability after a push, so a two-valued mapper has to guess,
    and the safe guess (CLEAN) is the one that hides a conflict.
    """

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"mergeable": "CONFLICTING"}, MergeConflictState.CONFLICTED),
            ({"mergeable": "MERGEABLE"}, MergeConflictState.CLEAN),
            # GitHub's own "still computing" answer — a fact, not a failure.
            ({"mergeable": "UNKNOWN"}, MergeConflictState.UNKNOWN),
            # An older API that omits the field must not read as clean.
            ({}, MergeConflictState.UNKNOWN),
            ({"mergeable": None}, MergeConflictState.UNKNOWN),
            ({"mergeable": ""}, MergeConflictState.UNKNOWN),
            # The `.upper()` path — the enum's case is not guaranteed by the CLI.
            ({"mergeable": "mergeable"}, MergeConflictState.CLEAN),
        ],
    )
    def test_maps_the_github_enum_onto_the_conflict_axis(
        self, payload: dict[str, object], expected: MergeConflictState
    ) -> None:
        assert _gh_conflict_state(payload) is expected

    def test_fetch_pr_merge_state_carries_the_conflict_through(self) -> None:
        # End to end through the wiring, so a mapper that is correct but never
        # called (the GitLab regression, exactly) still fails.
        body = json.dumps({"state": "OPEN", "mergeCommit": None, "mergeable": "CONFLICTING"})
        state = GhMergeRpc(lambda _argv: (0, body, "")).fetch_pr_merge_state(slug="o/r", pr_id=7)
        assert state.conflict is MergeConflictState.CONFLICTED
        assert state.state == "OPEN"

    def test_an_unreadable_pr_stays_unknown(self) -> None:
        state = GhMergeRpc(lambda _argv: (1, "", "boom")).fetch_pr_merge_state(slug="o/r", pr_id=7)
        assert state.conflict is MergeConflictState.UNKNOWN


class TestFetchLiveHeadSha:
    """A failed read and a PR with no head are DIFFERENT facts (the twin of the GitLab case).

    Collapsing both to ``""`` made ``review status`` report an untouched
    ``merge_safe`` PR as ``stale — re-review needed`` for the length of a GitHub
    503: an empty head compares unequal to every reviewed SHA, so the staleness
    test confidently answered a question nobody could answer.
    """

    @staticmethod
    def _rpc(result: tuple[int, str, str]) -> GhMergeRpc:
        return GhMergeRpc(lambda _argv: result)

    def test_a_readable_head_is_returned_verbatim(self) -> None:
        assert self._rpc((0, "  " + "a" * 40 + "\n", "")).fetch_live_head_sha(slug="o/r", pr_id=1) == "a" * 40

    def test_a_non_zero_rc_is_the_unreadable_sentinel(self) -> None:
        failed = (1, "", "HTTP 503: Service Unavailable")
        assert self._rpc(failed).fetch_live_head_sha(slug="o/r", pr_id=1) == HEAD_SHA_UNREADABLE

    def test_a_successful_call_with_an_empty_body_is_still_empty(self) -> None:
        # The forge DID answer, just degradedly — not the same as an unreadable read,
        # and every caller already fails closed on a falsy sha.
        assert self._rpc((0, "", "")).fetch_live_head_sha(slug="o/r", pr_id=1) == ""
