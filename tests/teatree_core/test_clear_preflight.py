"""The CLEAR-side E2E gate classifies the INVOKING worktree's diff, and only a diff it read (#776, #1967, #4929).

The §17.4 CLEAR-side E2E gate must classify the diff of the branch the CLEAR is
acting on — not the ticket's earliest (often already-merged) worktree row. A
reused ticket spanning N workstreams records the invoking branch on
``extra['ship_invoking_branch']``; the resolver must share the canonical
:func:`resolve_ship_worktree` so the CLEAR side and the ship side classify the
same tree. A diff it could not read — an ambiguous worktree, no worktree, a
failing git — refuses as DID NOT RUN instead of classifying an empty list.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.evidence.customer_display_impact import classify_paths
from teatree.core.management.commands._clear_preflight import clear_preflight_refusal, resolve_clear_changed_files
from teatree.core.modelkit.gate_verdict import EvidenceUnavailableError
from teatree.core.models import E2EBypassApproval, Ticket, Worktree
from teatree.core.overlay import OverlayReview
from teatree.utils.run import CommandFailedError

_SHA = "c" * 40


class _NothingImpactsReview(OverlayReview):
    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        _ = changed_files
        return False


class _NothingImpactsOverlay:
    review = _NothingImpactsReview()


class _AllowlistReview(OverlayReview):
    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        return classify_paths(changed_files, ("tests/*",))


class _AllowlistOverlay:
    review = _AllowlistReview()


def _worktree(ticket: Ticket, path: str, branch: str) -> Worktree:
    return Worktree.objects.create(
        ticket=ticket, overlay="t3-teatree", repo_path=path, branch=branch, extra={"worktree_path": path}
    )


class TestResolveClearChangedFiles(TestCase):
    def test_uses_invoking_worktree_not_earliest(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/i/76", overlay="t3-teatree")
        _worktree(ticket, "/tmp/stale", "ac/old-workstream")
        _worktree(ticket, "/tmp/current", "ac/current-workstream")
        ticket.extra = {"ship_invoking_branch": "ac/current-workstream"}
        ticket.save(update_fields=["extra"])

        with patch("teatree.visual_qa.changed_files", side_effect=lambda repo: [repo]) as changed:
            result = resolve_clear_changed_files(ticket)

        changed.assert_called_once_with(repo="/tmp/current")
        assert result == ["/tmp/current"]

    def test_no_worktree_is_unreadable_never_the_cwd(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/i/77", overlay="t3-teatree")

        with (
            patch("teatree.visual_qa.changed_files") as changed,
            pytest.raises(EvidenceUnavailableError, match="no worktree"),
        ):
            resolve_clear_changed_files(ticket)

        changed.assert_not_called()


class TestClearPreflightRefusesAnUnreadDiff(TestCase):
    """Even an overlay that classifies every path non-impacting cannot pass a diff nobody read."""

    def _refusal(self, ticket: Ticket, overlay: object = None) -> str | None:
        with patch(
            "teatree.core.gates.e2e_mandatory_gate.get_overlay", return_value=overlay or _NothingImpactsOverlay()
        ):
            return clear_preflight_refusal(_SHA, ticket)

    def test_an_ambiguous_worktree_did_not_run(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/i/80", overlay="t3-teatree")
        _worktree(ticket, "/tmp/repo-a", "feat-a")
        _worktree(ticket, "/tmp/repo-b", "feat-b")

        refusal = self._refusal(ticket)

        assert refusal is not None
        assert refusal.startswith("[gate:e2e_mandatory] DID NOT RUN")
        assert "ambiguous" in refusal
        assert "e2e-bypass" in refusal

    def test_a_ticket_without_a_worktree_did_not_run(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/i/81", overlay="t3-teatree")

        refusal = self._refusal(ticket)

        assert refusal is not None
        assert refusal.startswith("[gate:e2e_mandatory] DID NOT RUN")
        assert "no worktree" in refusal

    def test_a_failing_diff_did_not_run(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/i/82", overlay="t3-teatree")
        _worktree(ticket, "/tmp/repo-c", "feat-c")
        failure = CommandFailedError(["git", "diff"], 128, "", "fatal: bad revision")

        with patch("teatree.visual_qa.changed_files", side_effect=failure):
            refusal = self._refusal(ticket)

        assert refusal is not None
        assert refusal.startswith("[gate:e2e_mandatory] DID NOT RUN")
        assert "bad revision" in refusal

    def test_a_user_bypass_covers_a_diff_nobody_could_read(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/i/83", overlay="t3-teatree")
        E2EBypassApproval.record(ticket=ticket, head_sha=_SHA, approver_id="souliane")

        assert self._refusal(ticket) is None

    def test_a_verified_empty_diff_is_not_display_impacting(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/i/84", overlay="t3-teatree")
        _worktree(ticket, "/tmp/repo-d", "feat-d")

        with patch("teatree.visual_qa.changed_files", return_value=[]):
            assert self._refusal(ticket, _AllowlistOverlay()) is None

    def test_an_out_of_fsm_clear_is_not_gated(self) -> None:
        assert clear_preflight_refusal(_SHA, None) is None
