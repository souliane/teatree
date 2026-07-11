"""merge_evidence FSM gate: MERGED unreachable without real merged-SHA evidence (#4a).

Kills "believe work is done when it's not" at the FSM root. The ungated
``_advance_ticket`` walk (``ticket.py``) marked committed-and-tested-but-unpushed
tickets MERGED because ``mark_merged()`` / ``reconcile_merged()`` carried zero
evidence conditions. The gate refuses both transitions unless a keystone
``MergeAudit`` row with a real ``merged_sha`` exists OR the forge confirms the PR
merged (a fail-closed live probe — the never-wedge fallback). The FSM-integration
block is proven anti-vacuous by ``test_gate_is_load_bearing``: with the gate
neutralised the same evidence-less transition advances to MERGED.
"""

import contextlib
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.backend_protocols import PrMergeState
from teatree.core.gates import merge_evidence_gate
from teatree.core.gates.merge_evidence_gate import (
    NoMergeEvidenceError,
    check_merge_evidence,
    forge_confirms_merged,
    has_merge_audit_evidence,
    has_merge_evidence,
)
from teatree.core.modelkit import gate_registry
from teatree.core.models import MergeAudit, MergeClear, PullRequest, Ticket

_FORTY_HEX = "a" * 40


@contextlib.contextmanager
def _gate(*, required: bool) -> Iterator[None]:
    """Pin the gate's ``require_merge_evidence`` flag (mirrors integration_review_gate tests)."""
    with patch.object(
        merge_evidence_gate,
        "get_effective_settings",
        return_value=UserSettings(require_merge_evidence=required),
    ):
        yield


def _pr_merge_state(*, merged: bool) -> PrMergeState:
    return PrMergeState(state="MERGED" if merged else "OPEN", merge_commit_oid=_FORTY_HEX if merged else "")


def _audit_for(ticket: Ticket, *, merged_sha: str = _FORTY_HEX) -> MergeAudit:
    """A keystone-shaped MergeAudit row linked to *ticket* via its CLEAR."""
    clear = MergeClear.objects.create(
        ticket=ticket,
        pr_id=42,
        slug="souliane/teatree",
        reviewed_sha=_FORTY_HEX,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.LOGIC,
    )
    return MergeAudit.objects.create(clear=clear, merged_sha=merged_sha, required_checks_status="green")


def _pr_for(ticket: Ticket) -> PullRequest:
    return PullRequest.objects.create(
        ticket=ticket,
        url="https://github.com/souliane/teatree/pull/42",
        repo="souliane/teatree",
        iid="42",
        overlay="t3-teatree",
    )


class TestHasMergeAuditEvidence(TestCase):
    def test_true_with_a_real_merged_sha(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _audit_for(ticket)
        assert has_merge_audit_evidence(ticket) is True

    def test_false_without_any_audit_row(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        assert has_merge_audit_evidence(ticket) is False

    def test_false_for_a_blank_merged_sha(self) -> None:
        """A row whose ``merged_sha`` is blank/whitespace is NOT real evidence."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _audit_for(ticket, merged_sha="   ")
        assert has_merge_audit_evidence(ticket) is False

    def test_false_for_another_tickets_audit(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        other = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _audit_for(other)
        assert has_merge_audit_evidence(ticket) is False


class TestForgeConfirmsMerged(TestCase):
    def test_true_when_probe_reports_merged(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr_for(ticket)
        with patch(
            "teatree.core.merge.ci_rollup.CodeHostQuery.pr_merge_state", return_value=_pr_merge_state(merged=True)
        ):
            assert forge_confirms_merged(ticket) is True

    def test_false_when_probe_reports_not_merged(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr_for(ticket)
        with patch(
            "teatree.core.merge.ci_rollup.CodeHostQuery.pr_merge_state", return_value=_pr_merge_state(merged=False)
        ):
            assert forge_confirms_merged(ticket) is False

    def test_fail_closed_when_probe_raises(self) -> None:
        """An unreachable / erroring probe is inconclusive → no evidence (never mistaken for merged)."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr_for(ticket)
        with patch("teatree.core.merge.ci_rollup.CodeHostQuery.pr_merge_state", side_effect=RuntimeError("forge down")):
            assert forge_confirms_merged(ticket) is False

    def test_false_without_any_pr_rows(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        assert forge_confirms_merged(ticket) is False

    def test_gitlab_pr_url_probes_gitlab_host(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        PullRequest.objects.create(
            ticket=ticket,
            url="https://gitlab.com/acme/app/-/merge_requests/7",
            repo="acme/app",
            iid="7",
            overlay="t3-teatree",
        )
        with patch(
            "teatree.core.merge.ci_rollup.CodeHostQuery.pr_merge_state",
            autospec=True,
            return_value=_pr_merge_state(merged=True),
        ) as probe:
            forge_confirms_merged(ticket)
        # The query was bound to the GitLab transport (the ``self`` the method ran on).
        assert probe.call_args.args[0].ref.host_kind == "gitlab"


class TestCheckMergeEvidence(TestCase):
    def test_gate_off_passes_without_any_evidence(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        with _gate(required=False):
            check_merge_evidence(ticket)  # no raise

    def test_gate_on_refuses_without_evidence(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        with _gate(required=True), pytest.raises(NoMergeEvidenceError) as exc:
            check_merge_evidence(ticket)
        assert "no merged-SHA evidence" in str(exc.value)
        assert "require_merge_evidence false" in str(exc.value)  # names the never-wedge escape

    def test_gate_on_passes_with_a_keystone_merge_audit(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _audit_for(ticket)
        with _gate(required=True):
            check_merge_evidence(ticket)  # no raise

    def test_gate_on_passes_when_the_forge_confirms_merged(self) -> None:
        """Never-wedge: a genuinely-merged PR with no MergeAudit row still passes."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr_for(ticket)
        with (
            _gate(required=True),
            patch(
                "teatree.core.merge.ci_rollup.CodeHostQuery.pr_merge_state", return_value=_pr_merge_state(merged=True)
            ),
        ):
            check_merge_evidence(ticket)  # no raise

    def test_gate_on_fail_closed_when_probe_raises_and_no_audit(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr_for(ticket)
        with (
            _gate(required=True),
            patch("teatree.core.merge.ci_rollup.CodeHostQuery.pr_merge_state", side_effect=RuntimeError("forge down")),
            pytest.raises(NoMergeEvidenceError),
        ):
            check_merge_evidence(ticket)

    def test_has_merge_evidence_is_audit_or_forge(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        assert has_merge_evidence(ticket) is False
        _audit_for(ticket)
        assert has_merge_evidence(ticket) is True


class TestMergeEvidenceFsmGate(TestCase):
    """The anti-vacuity core: a terminal transition without a MergeAudit MUST refuse."""

    def test_mark_merged_refused_without_evidence(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        with _gate(required=True), pytest.raises(NoMergeEvidenceError):
            ticket.mark_merged()
        assert ticket.state == Ticket.State.IN_REVIEW  # the transition did NOT advance

    def test_reconcile_merged_refused_without_evidence(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.STARTED)
        with _gate(required=True), pytest.raises(NoMergeEvidenceError):
            ticket.reconcile_merged()
        assert ticket.state == Ticket.State.STARTED

    def test_reconcile_merged_allowed_with_a_merge_audit(self) -> None:
        """The keystone shape: a MergeAudit written before reconcile → MERGED reached."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.STARTED)
        _audit_for(ticket)
        with _gate(required=True), self.captureOnCommitCallbacks(execute=False):
            ticket.reconcile_merged()
            ticket.save()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_mark_merged_allowed_when_forge_confirms_merged(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _pr_for(ticket)
        with (
            _gate(required=True),
            patch(
                "teatree.core.merge.ci_rollup.CodeHostQuery.pr_merge_state", return_value=_pr_merge_state(merged=True)
            ),
            self.captureOnCommitCallbacks(execute=False),
        ):
            ticket.mark_merged()
            ticket.save()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_gate_is_load_bearing(self) -> None:
        """Anti-vacuity: with the gate neutralised, the same evidence-less MERGED advances.

        If this passes while ``test_mark_merged_refused_without_evidence`` also
        passes, the gate is genuinely the thing blocking believe-done-not-done.
        """
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        neutralised = {**gate_registry._REGISTRY, ("gate", "merge_evidence"): lambda _ticket: None}
        with (
            _gate(required=True),
            patch.object(gate_registry, "_REGISTRY", neutralised),
            self.captureOnCommitCallbacks(execute=False),
        ):
            ticket.mark_merged()
            ticket.save()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED


class TestRegistration(TestCase):
    def test_merge_evidence_gate_is_registered(self) -> None:
        assert gate_registry.get_gate("merge_evidence") is check_merge_evidence
