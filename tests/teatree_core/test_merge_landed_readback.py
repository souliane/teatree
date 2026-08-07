"""A merge that LANDED is not head drift (#4144).

Observed live: the keystone merged a PR, then reported ``merged: False`` and
re-escalated because the head readback came back ``(unresolved)``. The read can
come back empty for the same reasons #4239 already covers (transient forge error,
missing credential, rate limit) — GitHub keeps answering ``headRefOid`` for a
merged PR even after its source branch is deleted, so branch deletion is not
established as the cause. Whatever emptied it, the keystone diagnosed the empty
read as drift, named a force-push that never happened, skipped
``record_merge_and_advance``, and left the ticket ``tested`` against a merged PR.

These tests pin the ordering the fix establishes: "did it land?" is settled
BEFORE the read is diagnosed as drift, at both sites that raise on a head they
could not read (the #1335 slug reconciliation and §17.4.3 step 2). Everything
that must NOT move is pinned alongside — a head that resolves to a DIFFERENT SHA
is still genuine drift, an unreadable head on an unmerged PR still fails closed,
and the reconcile issues no merge RPC (the forge would 405 a second merge).

Only the ``gh`` subprocess (the network boundary) is stubbed; the candidate
enumeration, reconciliation, preconditions, post hook and FSM are real teatree
code. Repo names are neutral placeholders — core/tests stay overlay-agnostic
(BLUEPRINT § 1).
"""

import json
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.merge import MergePreconditionError, assert_merge_preconditions, merge_ticket_pr, pr_slug_resolution
from teatree.core.models import MergeAudit, MergeClear, Session, Ticket
from teatree.utils.pr_ref import PrRef
from tests._forge_stub import changed_files_stdout

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_REVIEWED_SHA = "a" * 40
_DRIFTED_SHA = "b" * 40
_MERGE_COMMIT = "c" * 40
_PR_ID = 4142

_SLUG = "example-org/example-repo"
_CANDIDATE = "downstream-org/downstream-overlay"

_GREEN = '[{"status": "COMPLETED", "conclusion": "SUCCESS"}]'


@pytest.fixture(autouse=True)
def _skip_author_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # The #1773/#3244 provenance gate has its own suite; these tests assert the
    # landed-vs-drift ordering and would otherwise fail closed on a stub author.
    monkeypatch.setattr("teatree.core.merge.execution.assert_merge_provenance_trusted", lambda **_: None)


class _Gh:
    """A scripted ``gh`` whose head read and merge state are set per test."""

    def __init__(
        self,
        *,
        head: str = "",
        head_rc: int = 1,
        state: str = "MERGED",
        merge_commit: str = _MERGE_COMMIT,
    ) -> None:
        self.head = head
        self.head_rc = head_rc
        self.state = state
        self.merge_commit = merge_commit
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        joined = " ".join(argv)
        for probe, answer in self._answers().items():
            if probe in joined:
                return answer
        if "pulls" in joined and "merge" in joined:
            return (0, json.dumps({"sha": "merged0deadbeef"}), "")
        return (0, changed_files_stdout(joined), "")

    def _answers(self) -> dict[str, tuple[int, str, str]]:
        """Each `gh --json` probe keyed by the field name that identifies it."""
        oid = {"oid": self.merge_commit} if self.merge_commit else None
        return {
            "baseRefName": (0, "main", ""),
            "required_status_checks": (0, json.dumps({"contexts": []}), ""),
            "headRefOid": (self.head_rc, self.head, ""),
            "mergeCommit": (0, json.dumps({"state": self.state, "mergeCommit": oid, "mergeable": "MERGEABLE"}), ""),
            "isDraft": (0, "false", ""),
            "statusCheckRollup": (0, _GREEN, ""),
        }

    @property
    def merge_calls(self) -> list[list[str]]:
        return [argv for argv in self.calls if "pulls" in " ".join(argv) and "merge" in " ".join(argv)]


def _clear(ticket: Ticket | None = None) -> MergeClear:
    from teatree.core.models.review_verdict import ReviewVerdict  # noqa: PLC0415 — deferred: ORM needs the app registry

    clear = MergeClear.objects.create(
        ticket=ticket,
        pr_id=_PR_ID,
        slug=_SLUG,
        reviewed_sha=_REVIEWED_SHA,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.DOCS,
        host_kind="github",
    )
    # What the real `ticket clear` keystone records; the #2829 verdict gate needs it.
    ReviewVerdict.record(
        pr_id=clear.pr_id,
        slug=clear.slug,
        reviewed_sha=clear.reviewed_sha,
        verdict=ReviewVerdict.Verdict.MERGE_SAFE,
        reviewer_identity=clear.reviewer_identity,
        blast_class=clear.blast_class,
        gh_verify_result=clear.gh_verify_result,
        ticket=clear.ticket,
    )
    return clear


def _ref() -> PrRef:
    return PrRef(slug=_SLUG, pr_id=_PR_ID, host_kind="github")


def _reconcile_slug(*, candidates: list[str] | None = None) -> str:
    with patch(
        "teatree.core.merge.pr_slug_resolution._iter_candidate_repo_slugs",
        return_value=[_SLUG] if candidates is None else candidates,
    ):
        return pr_slug_resolution._reconcile_slug_against_reviewed_sha(
            initial_slug=_SLUG,
            pr_id=_PR_ID,
            reviewed_sha=_REVIEWED_SHA,
            host_kind="github",
        )


class TestLandedMergeIsReconciledNotEscalated(TestCase):
    """The reported shape: the merge succeeded, the head readback returned nothing."""

    def test_slug_reconciliation_keeps_the_initial_repo(self) -> None:
        with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_Gh()):
            assert _reconcile_slug() == _SLUG

    def test_step_two_returns_a_reconcile_instead_of_refusing(self) -> None:
        clear = _clear(Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW))

        with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_Gh()):
            precheck = assert_merge_preconditions(
                clear=clear,
                executing_loop_identity="merge-loop",
                ref=_ref(),
            )

        assert precheck.needs_reconcile
        assert precheck.already_merged_sha == _MERGE_COMMIT

    def test_keystone_reports_merged_and_advances_the_fsm(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)

        with (
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_Gh()),
            patch("teatree.core.merge.pr_slug_resolution._iter_candidate_repo_slugs", return_value=[_SLUG]),
        ):
            outcome = merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert outcome.merged_sha == _MERGE_COMMIT
        assert ticket.state == Ticket.State.MERGED
        assert clear.consumed_at is not None
        assert MergeAudit.objects.filter(clear=clear).exists()
        session = Session.objects.filter(ticket=ticket).first()
        assert session is not None
        assert "merged" in session.visited_phases

    def test_keystone_issues_no_second_merge_rpc(self) -> None:
        """The forge would 405 a second merge — the reconcile records, never re-merges."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        gh = _Gh()

        with (
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=gh),
            patch("teatree.core.merge.pr_slug_resolution._iter_candidate_repo_slugs", return_value=[_SLUG]),
        ):
            merge_ticket_pr(clear=_clear(ticket), executing_loop_identity="merge-loop")

        assert gh.merge_calls == []


class TestOnlyALandedMergeTakesTheReconcilePath(TestCase):
    """Everything that must stay fail-closed while the landed case is recovered."""

    def test_unreadable_head_on_an_open_pr_is_still_refused(self) -> None:
        with (
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_Gh(state="OPEN", merge_commit="")),
            pytest.raises(MergePreconditionError),
        ):
            _reconcile_slug()

    def test_a_merged_pr_the_forge_names_no_commit_for_is_still_refused(self) -> None:
        """A degraded payload is a non-answer too — it must not unlock the reconcile."""
        with (
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_Gh(merge_commit="")),
            pytest.raises(MergePreconditionError),
        ):
            _reconcile_slug()

    def test_a_head_that_resolves_to_a_different_sha_stays_genuine_drift(self) -> None:
        with (
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_Gh(head=_DRIFTED_SHA, head_rc=0)),
            pytest.raises(MergePreconditionError) as exc,
        ):
            _reconcile_slug()

        assert "PR head moved" in str(exc.value)

    def test_step_two_still_refuses_an_unreadable_head_on_an_open_pr(self) -> None:
        clear = _clear(Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW))

        with (
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_Gh(state="OPEN", merge_commit="")),
            pytest.raises(MergePreconditionError) as exc,
        ):
            assert_merge_preconditions(clear=clear, executing_loop_identity="merge-loop", ref=_ref())

        clear.refresh_from_db()
        assert clear.consumed_at is None
        assert "could not resolve the live head SHA" in str(exc.value)


class TestUnreadableHeadNeverNamesAForcePush(TestCase):
    """Acceptance 2: "could not resolve" and "resolved to a different SHA" read differently."""

    def test_unreadable_initial_head_with_a_readable_candidate_is_not_called_drift(self) -> None:
        def _answers(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "headRefOid" in joined:
                return (0, _DRIFTED_SHA, "") if _CANDIDATE in joined else (1, "", "no such PR")
            if "mergeCommit" in joined:
                return (0, json.dumps({"state": "OPEN", "mergeCommit": None, "mergeable": "MERGEABLE"}), "")
            return (0, changed_files_stdout(joined), "")

        with (
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_answers),
            pytest.raises(MergePreconditionError) as exc,
        ):
            _reconcile_slug(candidates=[_SLUG, _CANDIDATE])

        message = str(exc.value)
        assert "PR head moved" not in message, f"an unreadable head is not a moved head; got: {message}"
        assert "force-push" not in message
        assert "could not read the live head" in message
        assert _CANDIDATE in message
