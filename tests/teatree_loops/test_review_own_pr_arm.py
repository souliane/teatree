"""The review loop's OWN-PR arm survives every per-loop tick request shape (#3843).

The review intake is the single board for self-authored AND colleague PRs
(#3569), and the self arm is the one that produces the independent cold-review
``merge_safe`` verdict the merge gate needs. On a solo repo it is also the ONLY
arm that can ever fire: assigning yourself as a reviewer is forbidden
(``handle_block_self_reviewer_assign``), so a colleague-only intake makes no PR
reviewable at all — two individually-correct rules that are jointly fatal.

``t3 loops tick --loop review --overlay <name>`` used to hand the mini-loop a
bare ``host`` instead of the overlay's backend, and the ``host`` arm builds only
the colleague ``ReviewerPrsScanner`` — silently dropping the own-PR arm for the
whole tick. These tests pin the arm at both ends: the request the command builds,
and the job set the mini-loop returns from it.
"""
# test-path: cross-cutting — the contract under test is that the command's request
# shape and the review mini-loop's job set AGREE, so observing it requires importing
# both packages; mirroring either one alone would hide the seam the bug lived in.

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import django.test
import pytest

from teatree.core.management.commands.loops_tick import Command
from teatree.loops.review.loop import MINI_LOOP as REVIEW_LOOP

if TYPE_CHECKING:
    from teatree.core.backend_factory import OverlayBackends

_ITER_BACKENDS = "teatree.core.management.commands.loops_tick.iter_overlay_backends"


def _backend(name: str) -> "OverlayBackends":
    """A stub overlay backend whose review domain yields the self-PR arm."""
    backend = MagicMock()
    backend.name = name
    backend.overlay = MagicMock()
    backend.overlay.metadata.get_followup_repos.return_value = ["souliane/teatree"]
    backend.overlay.config.get_github_token.return_value = ""
    backend.hosts = ()
    backend.messaging = None
    return backend


def _scanner_names(jobs: list[Any]) -> set[str]:
    return {job.scanner.name for job in jobs}


class TestOverlayScopedTickKeepsTheOwnPrArm(django.test.TestCase):
    def test_the_overlay_scoped_request_carries_the_backend_not_a_bare_host(self) -> None:
        """A bare host cannot build the own-PR arm — it needs the overlay object."""
        backend = _backend("t3-teatree")
        with patch(_ITER_BACKENDS, return_value=[backend]):
            request = Command()._build_request("t3-teatree")

        assert request.backends == [backend]
        assert request.host is None

    def test_an_unregistered_overlay_name_fails_loud(self) -> None:
        """Never silently fall back to a partial scan — an unknown name is an error."""
        with patch(_ITER_BACKENDS, return_value=[_backend("t3-teatree")]), pytest.raises(SystemExit):
            Command()._build_request("no-such-overlay")

    def test_the_review_job_set_built_from_that_request_includes_the_own_pr_arm(self) -> None:
        backend = _backend("t3-teatree")
        with patch(_ITER_BACKENDS, return_value=[backend]):
            request = Command()._build_request("t3-teatree")
        with patch("teatree.loop.scanner_factories._admit_colleague_prs_to_board", return_value=False):
            jobs = REVIEW_LOOP.build_jobs(backends=request.backends, host=request.host)

        assert "self_pr_review" in _scanner_names(jobs), _scanner_names(jobs)
