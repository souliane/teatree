from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from teatree.core.backend_protocols import PrOpenState
from teatree.core.forge_content import ContentEvidence, ContentPresence
from teatree.core.gates.merge_evidence_gate import has_merge_evidence, record_confirmed_forge_merge
from teatree.core.models import (
    MergeAudit,
    MergeClear,
    PullRequest,
    ReviewEvidence,
    ReviewVerdict,
    Rubric,
    RubricCriterion,
    Ticket,
)
from teatree.dash.selectors import pr_chip
from teatree.loop.scanners.board_reconcile import reconcile_board
from teatree.mcp.serializers import serialize_pull_request, serialize_ticket_detail

_HEAD = "a" * 40
_MERGED = "b" * 40


class TestVoidMergeCommand(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/acme/widget/issues/5",
            state=Ticket.State.REVIEW_REQUESTED,
            extra={"anti_vacuity_attestations": {_HEAD: {"result": "passed"}}, "rubric_grades_by_head": {_HEAD: [1]}},
        )
        self.pr = PullRequest.objects.create(
            ticket=self.ticket,
            overlay="t3-teatree",
            url="https://github.com/acme/widget/pull/7",
            repo="acme/widget",
            iid="7",
            state=PullRequest.State.APPROVED,
        )
        self.pr.mark_merged()
        self.pr.save()
        clear = MergeClear.objects.create(
            ticket=self.ticket,
            pr_id=7,
            slug="acme/widget",
            reviewed_sha=_HEAD,
            reviewer_identity="reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
        )
        MergeAudit.objects.create(clear=clear, merged_sha=_MERGED, required_checks_status="green")
        ReviewVerdict.objects.create(
            ticket=self.ticket,
            pr_id=7,
            slug="acme/widget",
            reviewed_sha=_HEAD,
            verdict=ReviewVerdict.Verdict.MERGE_SAFE,
            reviewer_identity="reviewer",
            blast_class=MergeClear.BlastClass.LOGIC,
            gh_verify_result=MergeClear.VerifyResult.GREEN,
        )
        ReviewEvidence.objects.create(
            ticket=self.ticket,
            kind=ReviewEvidence.Kind.COLD_REVIEW,
            reviewer_identity="reviewer",
            verdict="pass",
            head_sha=_HEAD,
            repos=["acme/widget"],
        )
        Rubric.populate(self.ticket, ["A void restores both merged states and records its evidence"])
        with patch("teatree.core.models.ticket.get_gate", return_value=lambda _ticket: None):
            self.ticket.mark_merged()
            self.ticket.save()

    def _run(
        self,
        *,
        reason: str = "history rewrite removed the change",
        presence: str = "absent",
        prior_pr_state: str = "",
        prior_ticket_state: str = "",
    ) -> object:
        evidence = ContentEvidence(
            ContentPresence(presence),
            head_sha=_HEAD,
            default_sha=_MERGED,
            head_tree="c" * 40,
            default_tree="d" * 40,
            checked_paths=("a.py",),
            reason="checked",
        )
        with patch("teatree.core.merge.void_merge.CodeHostQuery.for_ref") as query:
            query.return_value.content_evidence.return_value = evidence
            args = ["pr", "void-merge", str(self.pr.pk), "--actor", "operator", "--reason", reason]
            if prior_pr_state:
                args.extend(["--prior-pr-state", prior_pr_state])
            if prior_ticket_state:
                args.extend(["--prior-ticket-state", prior_ticket_state])
            return call_command(*args)

    def test_void_restores_both_states_and_preserves_ledgers(self) -> None:
        before = (
            MergeClear.objects.count(),
            MergeAudit.objects.count(),
            ReviewVerdict.objects.count(),
            ReviewEvidence.objects.count(),
            Rubric.objects.count(),
            RubricCriterion.objects.count(),
        )
        extra = self.ticket.extra

        self._run()

        self.ticket.refresh_from_db()
        self.pr.refresh_from_db()
        assert self.ticket.state == Ticket.State.REVIEW_REQUESTED
        assert self.pr.state == PullRequest.State.APPROVED
        assert (
            MergeClear.objects.count(),
            MergeAudit.objects.count(),
            ReviewVerdict.objects.count(),
            ReviewEvidence.objects.count(),
            Rubric.objects.count(),
            RubricCriterion.objects.count(),
        ) == before
        assert self.ticket.extra["anti_vacuity_attestations"] == extra["anti_vacuity_attestations"]
        assert self.ticket.extra["rubric_grades_by_head"] == extra["rubric_grades_by_head"]
        assert self.ticket.merge_voids.get().actor == "operator"
        assert self.ticket.merge_voids.get().reason == "history rewrite removed the change"
        assert self.ticket.merge_voids.get().voided_at is not None

    def test_present_or_unreadable_content_refuses_without_any_write(self) -> None:
        for presence in ("present", "unknown"):
            with pytest.raises(CommandError, match="content is present or unverified"):
                self._run(presence=presence)
            self.ticket.refresh_from_db()
            self.pr.refresh_from_db()
            assert self.ticket.state == Ticket.State.MERGED
            assert self.pr.state == PullRequest.State.MERGED
            assert not self.ticket.merge_voids.exists()

    def test_missing_reason_refuses(self) -> None:
        with pytest.raises(CommandError, match="reason is required"):
            self._run(reason="")
        assert not self.ticket.merge_voids.exists()

    def test_voided_evidence_cannot_remerge_ticket_or_pr(self) -> None:
        self.ticket.extra["forge_merge_evidence"] = {
            "slug": "acme/widget",
            "pr_id": 7,
            "merged_sha": _MERGED,
        }
        self.ticket.save(update_fields=["extra"])
        self._run()
        self.ticket.refresh_from_db()
        self.pr.refresh_from_db()
        assert "forge_merge_evidence" not in self.ticket.extra
        assert self.ticket.merge_voids.get().evidence["presence"] == "absent"

        with patch("teatree.core.gates.merge_evidence_gate.CodeHostQuery.for_ref") as query:
            query.return_value.pr_merge_state.return_value.is_merged = True
            query.return_value.pr_merge_state.return_value.merge_commit_oid = _MERGED
            assert not has_merge_evidence(self.ticket)
            assert not record_confirmed_forge_merge(self.ticket)
            query.assert_not_called()

        assert PullRequest.objects.record_forge_merge(slug="acme/widget", pr_id=7) == 0
        self.pr.refresh_from_db()
        assert self.pr.state == PullRequest.State.APPROVED

    def test_other_pr_merge_audit_remains_usable(self) -> None:
        self._run()
        clear = MergeClear.objects.create(
            ticket=self.ticket,
            pr_id=9,
            slug="acme/widget",
            host_kind="github",
            reviewed_sha=_HEAD,
            reviewer_identity="reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
        )
        MergeAudit.objects.create(clear=clear, merged_sha=_MERGED, required_checks_status="green")
        self.ticket.refresh_from_db()
        assert has_merge_evidence(self.ticket)

    def test_reconciler_does_not_treat_voided_pr_as_new_merge(self) -> None:
        self.ticket.issue_url = self.pr.url
        self.ticket.save(update_fields=["issue_url"])
        self._run()

        with (
            patch("teatree.loop.scanners.board_reconcile.pr_open_state", return_value=PrOpenState.MERGED),
            patch("teatree.loop.scanners.board_reconcile._issue_done_transitions", return_value=[]),
        ):
            report = reconcile_board(overlay="t3-teatree", probe_budget=1)

        assert report.transitions == ()
        self.ticket.refresh_from_db()
        assert self.ticket.state == Ticket.State.REVIEW_REQUESTED

    def test_generic_ticket_transition_refuses_void_merge(self) -> None:
        with pytest.raises(SystemExit):
            call_command("ticket", "transition", str(self.ticket.pk), "void_merge")
        assert not self.ticket.merge_voids.exists()

    def test_void_is_visible_in_operator_and_read_models(self) -> None:
        self._run()
        self.ticket.refresh_from_db()
        self.pr.refresh_from_db()

        shown = call_command("ticket", "show", str(self.ticket.pk))
        assert shown["merge_void"]["actor"] == "operator"
        assert shown["merge_void"]["reason"] == "history rewrite removed the change"
        assert serialize_ticket_detail(self.ticket)["merge_void"]["actor"] == "operator"
        assert serialize_pull_request(self.pr)["merge_void"]["reason"] == "history rewrite removed the change"
        assert "Merge voided" in pr_chip(self.pr).label

    def test_forge_reconcile_persists_the_pr_premerge_state(self) -> None:
        pr = PullRequest.objects.create(
            ticket=self.ticket,
            overlay="t3-teatree",
            url="https://github.com/acme/widget/pull/8",
            repo="acme/widget",
            iid="8",
            state=PullRequest.State.APPROVED,
        )
        assert PullRequest.objects.record_forge_merge(slug="acme/widget", pr_id=8) == 1
        pr.refresh_from_db()
        assert pr.state == PullRequest.State.MERGED
        assert pr.state_before_merge == PullRequest.State.APPROVED

    def test_historical_pr_requires_operator_to_name_unknown_prior_state(self) -> None:
        self.pr.state_before_merge = ""
        self.pr.save(update_fields=["state_before_merge"])

        with pytest.raises(CommandError, match="prior PR state is unknown"):
            self._run()
        assert not self.ticket.merge_voids.exists()

        self._run(prior_pr_state=PullRequest.State.APPROVED)
        self.pr.refresh_from_db()
        assert self.pr.state == PullRequest.State.APPROVED

    def test_missing_ticket_transition_requires_operator_prior_state(self) -> None:
        self.ticket.transitions.filter(to_state=Ticket.State.MERGED).delete()

        with pytest.raises(CommandError, match="prior ticket state is unknown"):
            self._run()
        assert not self.ticket.merge_voids.exists()

        self._run(prior_ticket_state=Ticket.State.REVIEW_REQUESTED)
        self.ticket.refresh_from_db()
        assert self.ticket.state == Ticket.State.REVIEW_REQUESTED

    def test_operator_cannot_override_recorded_prior_states(self) -> None:
        with pytest.raises(CommandError, match="prior ticket state conflicts"):
            self._run(prior_ticket_state=Ticket.State.TESTED)
        with pytest.raises(CommandError, match="prior PR state conflicts"):
            self._run(prior_pr_state=PullRequest.State.OPEN)
        assert not self.ticket.merge_voids.exists()
