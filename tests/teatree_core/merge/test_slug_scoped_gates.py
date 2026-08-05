"""The repo-slug boundary the merge gates join on, and the parity between the two merge paths.

A PR's repo slug has several resolutions across the subsystem, and every finding
pinned here is one place a join was keyed on the WRONG one — a workstream slug that
matches no PR row, or a case-sensitive match against a case-insensitive forge slug.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.merge.authorization import assert_review_verdict_gate
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.merge.execution import execute_bound_merge
from teatree.core.merge.post_hook import _supersede_siblings
from teatree.core.merge.ticket_gates import assert_ticket_scoped_gates
from teatree.core.models import MergeClear, PullRequest, ReviewVerdict
from teatree.utils.pr_ref import PrRef
from tests.factories import MergeClearFactory, PullRequestFactory, TicketFactory

_SHA = "a" * 40
_REPO = "souliane/teatree"
_WORKSTREAM = "merge-candidate-working-repos"

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class TestReviewVerdictSlugIsCaseInsensitive(TestCase):
    """A verdict recorded under a differently-cased slug still vouches for the same PR."""

    def test_verdict_recorded_under_other_casing_is_found(self) -> None:
        ReviewVerdict.record(
            pr_id=77,
            slug="Souliane/Teatree",
            reviewed_sha=_SHA,
            verdict=ReviewVerdict.Verdict.MERGE_SAFE,
            reviewer_identity="cold-reviewer",
        )
        found = list(ReviewVerdict.objects.for_pr(_REPO, 77))
        assert [verdict.slug for verdict in found] == ["Souliane/Teatree"]

    def test_merge_gate_reads_the_other_cased_verdict(self) -> None:
        ReviewVerdict.record(
            pr_id=78,
            slug="SOULIANE/TEATREE",
            reviewed_sha=_SHA,
            verdict=ReviewVerdict.Verdict.MERGE_SAFE,
            reviewer_identity="cold-reviewer",
        )
        assert_review_verdict_gate(slug=_REPO, pr_id=78, head_sha=_SHA)


class TestClearAdoptsItsTicketByRepoSlug(TestCase):
    """A ticketless CLEAR's workstream slug matches no PR row — the caller supplies the repo."""

    def test_workstream_slug_alone_adopts_nothing(self) -> None:
        ticket = TicketFactory()
        PullRequestFactory(ticket=ticket, repo=_REPO, iid="4242")
        clear = MergeClearFactory(ticket=None, slug=_WORKSTREAM, pr_id=4242)
        assert clear.adopt_owning_ticket() is None

    def test_repo_slug_adopts_the_prs_ticket(self) -> None:
        ticket = TicketFactory()
        PullRequestFactory(ticket=ticket, repo=_REPO, iid="4243")
        clear = MergeClearFactory(ticket=None, slug=_WORKSTREAM, pr_id=4243)
        assert clear.adopt_owning_ticket(_REPO) == ticket
        clear.refresh_from_db()
        assert clear.ticket == ticket

    def test_landed_merge_records_the_pr_under_the_repo_slug(self) -> None:
        ticket = TicketFactory()
        pr = PullRequestFactory(ticket=ticket, repo=_REPO, iid="4244", state=PullRequest.State.OPEN)
        clear = MergeClearFactory(ticket=None, slug=_WORKSTREAM, pr_id=4244)
        assert clear.record_merged_pull_request(_REPO) == ticket
        pr.refresh_from_db()
        assert pr.state == PullRequest.State.MERGED


class TestSiblingSupersedeIsRepoScoped(TestCase):
    """A workstream slug is shared across repos, so it can never scope the §15 supersede."""

    _issue = 0

    def _clear_for(self, *, repo: str, pr_id: int) -> "MergeClear":
        """A ticketless-shaped CLEAR whose repo is knowable only through its ticket's issue URL."""
        TestSiblingSupersedeIsRepoScoped._issue += 1
        ticket = TicketFactory(issue_url=f"https://github.com/{repo}/issues/{self._issue}")
        return MergeClearFactory.create(ticket=ticket, slug=_WORKSTREAM, pr_id=pr_id)

    def test_another_repos_live_clear_survives(self) -> None:
        merged = self._clear_for(repo=_REPO, pr_id=42)
        other = self._clear_for(repo="souliane/other-repo", pr_id=42)
        merged.consumed_at = merged.issued_at
        merged.save(update_fields=["consumed_at"])

        _supersede_siblings(merged, repo_slug=_REPO)

        other.refresh_from_db()
        assert other.consumed_at is None

    def test_same_repos_stale_sibling_is_consumed(self) -> None:
        merged = self._clear_for(repo=_REPO, pr_id=43)
        stale = self._clear_for(repo=_REPO, pr_id=43)
        merged.consumed_at = merged.issued_at
        merged.save(update_fields=["consumed_at"])

        _supersede_siblings(merged, repo_slug=_REPO)

        stale.refresh_from_db()
        assert stale.consumed_at == merged.consumed_at

    def test_owner_repo_slug_supersedes_without_a_merge_time_repo(self) -> None:
        merged = MergeClearFactory(slug=_REPO, pr_id=44)
        stale = MergeClearFactory(slug="Souliane/Teatree", pr_id=44)
        merged.consumed_at = merged.issued_at
        merged.save(update_fields=["consumed_at"])

        _supersede_siblings(merged, repo_slug="")

        stale.refresh_from_db()
        assert stale.consumed_at == merged.consumed_at

    def test_unknown_merge_time_repo_supersedes_nothing(self) -> None:
        merged = self._clear_for(repo=_REPO, pr_id=45)
        sibling = self._clear_for(repo=_REPO, pr_id=45)
        merged.consumed_at = merged.issued_at
        merged.save(update_fields=["consumed_at"])

        _supersede_siblings(merged, repo_slug="")

        sibling.refresh_from_db()
        assert sibling.consumed_at is None


class TestTicketScopedGatesAtTheSharedChokepoint(TestCase):
    """Both merge paths cross ``execute_bound_merge``; the ticket-scoped gates must run there."""

    def test_unresolvable_ticket_is_a_no_op_with_both_settings_off(self) -> None:
        assert_ticket_scoped_gates(slug=_REPO, pr_id=9001, head_sha=_SHA)

    def test_unresolvable_ticket_refuses_while_a_setting_is_in_force(self) -> None:
        with (
            patch("teatree.core.gates.anti_vacuity_gate.anti_vacuity_required", return_value=True),
            pytest.raises(MergePreconditionError, match="no owning ticket resolves"),
        ):
            assert_ticket_scoped_gates(slug=_REPO, pr_id=9002, head_sha=_SHA)

    def test_resolved_ticket_without_an_attestation_is_refused(self) -> None:
        ticket = TicketFactory()
        PullRequestFactory(ticket=ticket, repo=_REPO, iid="9003")
        with (
            patch("teatree.core.gates.anti_vacuity_gate.anti_vacuity_required", return_value=True),
            pytest.raises(MergePreconditionError, match="require_anti_vacuity_attestation"),
        ):
            assert_ticket_scoped_gates(slug=_REPO, pr_id=9003, head_sha=_SHA)

    def test_bound_merge_runs_the_ticket_scoped_gates(self) -> None:
        """The solo-overlay bypass reaches the forge with no CLEAR — the gate must still fire.

        The refusal is matched on the ATTESTATION message, not merely on the error type:
        the not-draft and CI floors below also refuse on an unreachable forge, so a
        type-only assertion would pass with this gate deleted.
        """
        ticket = TicketFactory()
        PullRequestFactory(ticket=ticket, repo=_REPO, iid="9004")
        with (
            patch("teatree.core.merge.execution.assert_merge_provenance_trusted"),
            patch("teatree.core.merge.execution.assert_review_verdict_gate"),
            patch("teatree.core.merge.execution.assert_no_active_review_lock"),
            patch("teatree.core.gates.merge_quality_gate.assert_merge_quality_verdict"),
            patch("teatree.core.gates.anti_vacuity_gate.anti_vacuity_required", return_value=True),
            pytest.raises(MergePreconditionError, match="require_anti_vacuity_attestation"),
        ):
            execute_bound_merge(ref=PrRef(slug=_REPO, pr_id=9004), expected_head_oid=_SHA)
