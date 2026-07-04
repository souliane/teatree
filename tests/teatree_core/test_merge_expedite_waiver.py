"""Human-authorized, SHA-bound PENDING-checks expedite waiver (FIX-EXPEDITE PART A).

The keystone waives a live PENDING required-checks status ONLY when the CLEAR was
issued as an expedite waiver — a linked ticket flagged ``expedited``, a recorded
human authoriser, and a ``local_ci_green_sha`` attestation bound to the exact
reviewed tree — AND that authoriser is re-presented at merge time. A FAILED
required check is NEVER waivable: it is refused at issuance, at the CLEAR guard, at
``ReviewVerdict.record``, and at the live merge-time check (four independent pins).

Only the unstoppable external — the ``gh`` subprocess — is stubbed; every teatree
model / FSM / DB write is real.
"""

import json
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.merge import MergeOutcome, MergePreconditionError, merge_ticket_pr
from teatree.core.models import (
    ClearIssuanceError,
    ClearRequest,
    MergeAudit,
    MergeClear,
    ReviewVerdict,
    ReviewVerdictError,
    Ticket,
)

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SHA = "e" * 40
_OTHER_SHA = "d" * 40
_GREEN = '[{"status": "COMPLETED", "conclusion": "SUCCESS"}]'
_PENDING = '[{"name": "lint", "status": "IN_PROGRESS"}]'
_FAILED = '[{"name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"}]'


@pytest.fixture(autouse=True)
def _skip_author_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # The #1773 public-repo author gate is orthogonal to the expedite waiver.
    monkeypatch.setattr("teatree.core.merge.execution.assert_public_repo_author_trusted", lambda **_: None)


def _branch_protection_probe(joined: str, *, required: list[str]) -> tuple[int, str, str] | None:
    """Answer the base-branch + branch-protection required-context probes, else ``None``."""
    if "baseRefName" in joined:
        return (0, "main", "")
    if "required_status_checks" in joined:
        return (0, json.dumps({"contexts": required}), "")
    return None


class _GhStub:
    """Scripted ``gh`` responses; ``checks`` + ``required`` drive the live rollup verdict."""

    def __init__(
        self,
        *,
        head: str = _SHA,
        draft: str = "false",
        checks: str = _GREEN,
        required: list[str] | None = None,
    ) -> None:
        self.head = head
        self.draft = draft
        self.checks = checks
        self.required = required or []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        joined = " ".join(argv)
        if (meta := _branch_protection_probe(joined, required=self.required)) is not None:
            return meta
        if "headRefOid" in joined:
            return (0, self.head, "")
        if "isDraft" in joined:
            return (0, self.draft, "")
        if "statusCheckRollup" in joined:
            return (0, self.checks, "")
        if "pulls" in joined and "merge" in joined:
            return (0, '{"sha": "expedited0merged"}', "")
        return (0, "", "")


def _pending_stub() -> _GhStub:
    return _GhStub(checks=_PENDING, required=["lint"])


def _failed_stub() -> _GhStub:
    return _GhStub(checks=_FAILED, required=["lint"])


def _expedited_ticket() -> Ticket:
    return Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW, expedited=True)


def _issue_expedite_clear(
    ticket: Ticket,
    *,
    pr_id: int,
    authorizer: str = "owner-x",
    attestation_sha: str = _SHA,
    gh_verify_result: str = "pending",
) -> MergeClear:
    """Issue a real expedite CLEAR + its sibling PENDING merge_safe verdict."""
    clear = MergeClear.issue(
        ClearRequest(
            pr_id=pr_id,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=gh_verify_result,
            blast_class="docs",
            ticket=ticket,
            expedite_authorizer=authorizer,
            local_ci_green_sha=attestation_sha,
        )
    )
    ReviewVerdict.record(
        pr_id=clear.pr_id,
        slug=clear.slug,
        reviewed_sha=clear.reviewed_sha,
        verdict=ReviewVerdict.Verdict.MERGE_SAFE,
        reviewer_identity="cold-reviewer",
        gh_verify_result=gh_verify_result,
        expedited=True,
    )
    return clear


def _run(clear: MergeClear, stub: _GhStub, *, expedite_authorized: str = "") -> MergeOutcome:
    with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=stub):
        return merge_ticket_pr(
            clear=clear,
            executing_loop_identity="merge-loop",
            expedite_authorized=expedite_authorized,
        )


class TestExpediteIssuance(TestCase):
    """``MergeClear.issue`` — the first of the four FAILED pins + the waiver contract."""

    def test_issue_refuses_failed_snapshot_even_with_expedite(self) -> None:
        # ANTI-VACUITY (issuance): a FAILED snapshot is refused even with full expedite fields.
        ticket = _expedited_ticket()
        with pytest.raises(ClearIssuanceError, match="failed"):
            MergeClear.issue(
                ClearRequest(
                    pr_id=100,
                    slug="souliane/teatree",
                    reviewed_sha=_SHA,
                    reviewer_identity="cold-reviewer",
                    gh_verify_result="failed",
                    ticket=ticket,
                    expedite_authorizer="owner-x",
                    local_ci_green_sha=_SHA,
                )
            )
        assert MergeClear.objects.count() == 0

    def test_issue_accepts_pending_snapshot_with_full_expedite_waiver(self) -> None:
        ticket = _expedited_ticket()
        clear = _issue_expedite_clear(ticket, pr_id=101)
        assert clear.gh_verify_result == MergeClear.VerifyResult.PENDING
        assert clear.expedite_authorizer == "owner-x"
        assert clear.local_ci_green_sha == _SHA

    def test_issue_refuses_pending_snapshot_without_expedite(self) -> None:
        ticket = _expedited_ticket()
        with pytest.raises(ClearIssuanceError, match="expedite waiver"):
            MergeClear.issue(
                ClearRequest(
                    pr_id=102,
                    slug="souliane/teatree",
                    reviewed_sha=_SHA,
                    reviewer_identity="cold-reviewer",
                    gh_verify_result="pending",
                    ticket=ticket,
                )
            )

    def test_expedite_fields_refused_on_unflagged_ticket(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW, expedited=False)
        with pytest.raises(ClearIssuanceError, match="flagged expedited"):
            MergeClear.issue(
                ClearRequest(
                    pr_id=103,
                    slug="souliane/teatree",
                    reviewed_sha=_SHA,
                    reviewer_identity="cold-reviewer",
                    gh_verify_result="pending",
                    ticket=ticket,
                    expedite_authorizer="owner-x",
                    local_ci_green_sha=_SHA,
                )
            )

    def test_expedite_fields_refused_on_ticketless_clear(self) -> None:
        with pytest.raises(ClearIssuanceError, match="flagged expedited"):
            MergeClear.issue(
                ClearRequest(
                    pr_id=104,
                    slug="souliane/teatree",
                    reviewed_sha=_SHA,
                    reviewer_identity="cold-reviewer",
                    gh_verify_result="pending",
                    expedite_authorizer="owner-x",
                    local_ci_green_sha=_SHA,
                )
            )

    def test_expedite_refused_with_mismatched_attestation_sha(self) -> None:
        ticket = _expedited_ticket()
        with pytest.raises(ClearIssuanceError, match="EQUAL to reviewed_sha"):
            MergeClear.issue(
                ClearRequest(
                    pr_id=105,
                    slug="souliane/teatree",
                    reviewed_sha=_SHA,
                    reviewer_identity="cold-reviewer",
                    gh_verify_result="pending",
                    ticket=ticket,
                    expedite_authorizer="owner-x",
                    local_ci_green_sha=_OTHER_SHA,
                )
            )

    def test_expedite_refused_with_truncated_attestation_sha(self) -> None:
        ticket = _expedited_ticket()
        with pytest.raises(ClearIssuanceError, match="full 40-char"):
            MergeClear.issue(
                ClearRequest(
                    pr_id=106,
                    slug="souliane/teatree",
                    reviewed_sha=_SHA,
                    reviewer_identity="cold-reviewer",
                    gh_verify_result="pending",
                    ticket=ticket,
                    expedite_authorizer="owner-x",
                    local_ci_green_sha=_SHA[:12],
                )
            )

    def test_local_ci_sha_without_authorizer_refused(self) -> None:
        ticket = _expedited_ticket()
        with pytest.raises(ClearIssuanceError, match="without expedite_authorizer"):
            MergeClear.issue(
                ClearRequest(
                    pr_id=107,
                    slug="souliane/teatree",
                    reviewed_sha=_SHA,
                    reviewer_identity="cold-reviewer",
                    gh_verify_result="pending",
                    ticket=ticket,
                    local_ci_green_sha=_SHA,
                )
            )

    def test_expedite_fields_on_green_clear_pre_authorize(self) -> None:
        # Expedite fields on a GREEN CLEAR are allowed — pre-authorises a later queue-flip.
        ticket = _expedited_ticket()
        clear = MergeClear.issue(
            ClearRequest(
                pr_id=108,
                slug="souliane/teatree",
                reviewed_sha=_SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                ticket=ticket,
                expedite_authorizer="owner-x",
                local_ci_green_sha=_SHA,
            )
        )
        assert clear.gh_verify_result == MergeClear.VerifyResult.GREEN
        assert clear.expedite_pending_waived_by("owner-x") is True


class TestExpediteReviewVerdict(TestCase):
    """``ReviewVerdict.record`` — the second FAILED pin (merge_safe must-not-be-FAILED)."""

    def test_record_refuses_merge_safe_on_failed_even_expedited(self) -> None:
        with pytest.raises(ReviewVerdictError, match="never carry gh_verify_result=failed"):
            ReviewVerdict.record(
                pr_id=200,
                slug="souliane/teatree",
                reviewed_sha=_SHA,
                verdict=ReviewVerdict.Verdict.MERGE_SAFE,
                reviewer_identity="cold-reviewer",
                gh_verify_result="failed",
                expedited=True,
            )

    def test_record_refuses_merge_safe_on_pending_without_expedited(self) -> None:
        with pytest.raises(ReviewVerdictError, match="requires the expedite waiver"):
            ReviewVerdict.record(
                pr_id=201,
                slug="souliane/teatree",
                reviewed_sha=_SHA,
                verdict=ReviewVerdict.Verdict.MERGE_SAFE,
                reviewer_identity="cold-reviewer",
                gh_verify_result="pending",
                expedited=False,
            )

    def test_record_accepts_merge_safe_on_pending_when_expedited(self) -> None:
        verdict = ReviewVerdict.record(
            pr_id=202,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict=ReviewVerdict.Verdict.MERGE_SAFE,
            reviewer_identity="cold-reviewer",
            gh_verify_result="pending",
            expedited=True,
        )
        assert verdict.is_merge_safe() is True
        assert verdict.gh_verify_result == MergeClear.VerifyResult.PENDING


class TestExpediteMergeTime(TestCase):
    """The merge keystone — the anti-vacuity FAILED pin + the sanctioned pending merge."""

    def test_expedite_never_waives_failed_checks(self) -> None:
        # ANTI-VACUITY (merge): a fully-authorized expedite CLEAR with live FAILED
        # required checks is STILL refused — no MergeAudit, FSM stays IN_REVIEW.
        ticket = _expedited_ticket()
        clear = _issue_expedite_clear(ticket, pr_id=300)
        with pytest.raises(MergePreconditionError, match="FAILED required check"):
            _run(clear, _failed_stub(), expedite_authorized="owner-x")
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW
        assert not MergeAudit.objects.filter(clear=clear).exists()
        assert clear.consumed_at is None

    def test_expedited_pending_merge_with_authorizer_merges(self) -> None:
        ticket = _expedited_ticket()
        clear = _issue_expedite_clear(ticket, pr_id=301)
        outcome = _run(clear, _pending_stub(), expedite_authorized="owner-x")
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert outcome.ticket_state == Ticket.State.MERGED
        assert ticket.state == Ticket.State.MERGED
        assert clear.consumed_at is not None
        audit = MergeAudit.objects.get(clear=clear)
        assert audit.required_checks_status == "pending"
        assert audit.expedited_by == "owner-x"

    def test_pending_merge_without_presented_authorizer_escalates(self) -> None:
        # The loop never auto-expedites: a pending expedite CLEAR with NO presented
        # authorizer is refused at the keystone (FSM untouched).
        ticket = _expedited_ticket()
        clear = _issue_expedite_clear(ticket, pr_id=302)
        with pytest.raises(MergePreconditionError, match="expedite waiver"):
            _run(clear, _pending_stub(), expedite_authorized="")
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW
        assert not MergeAudit.objects.filter(clear=clear).exists()

    def test_expedite_waiver_requires_attestation_bound_to_reviewed_sha(self) -> None:
        # Merge-time double-guard: a raw-ORM row whose attestation SHA != reviewed_sha
        # is refused even with a matching presented authorizer.
        ticket = _expedited_ticket()
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=303,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.PENDING,
            blast_class=MergeClear.BlastClass.DOCS,
            expedite_authorizer="owner-x",
            local_ci_green_sha=_OTHER_SHA,
        )
        with pytest.raises(MergePreconditionError, match="not green"):
            _run(clear, _pending_stub(), expedite_authorized="owner-x")
        clear.refresh_from_db()
        assert clear.consumed_at is None

    def test_substrate_key_does_not_unlock_pending_waiver(self) -> None:
        # A presented --human-authorized (substrate key) never satisfies the pending
        # waiver: the two keys are orthogonal. A raw-ORM pending row carrying only a
        # human_authorizer is refused even when that authorizer is re-presented.
        ticket = _expedited_ticket()
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=304,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.PENDING,
            blast_class=MergeClear.BlastClass.SUBSTRATE,
            human_authorizer="owner-x",
        )
        with (
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_pending_stub()),
            pytest.raises(MergePreconditionError, match="not green"),
        ):
            merge_ticket_pr(
                clear=clear,
                executing_loop_identity="merge-loop",
                human_authorized="owner-x",
            )
        clear.refresh_from_db()
        assert clear.consumed_at is None


class TestExpediteCliRoundTrip(TestCase):
    """The ``ticket clear --expedite-authorize`` → ``ticket merge --expedite-authorized`` seam."""

    def test_cli_clear_and_merge_waives_pending(self) -> None:
        ticket = _expedited_ticket()
        issued = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "400",
                "souliane/teatree",
                reviewed_sha=_SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result="pending",
                blast_class="docs",
                ticket_id=int(ticket.pk),
                expedite_authorize="owner-x",
                local_ci_green_sha=_SHA,
            ),
        )
        assert issued["issued"]
        with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_pending_stub()):
            merged = cast(
                "dict[str, object]",
                call_command(
                    "ticket",
                    "merge",
                    str(issued["clear_id"]),
                    expedite_authorized="owner-x",
                ),
            )
        assert merged["merged"] is True
        assert MergeAudit.objects.get(clear_id=issued["clear_id"]).expedited_by == "owner-x"

    def test_cli_merge_without_expedite_flag_escalates(self) -> None:
        # The loop path (no --expedite-authorized) refuses and returns an escalated result.
        ticket = _expedited_ticket()
        issued = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "401",
                "souliane/teatree",
                reviewed_sha=_SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result="pending",
                blast_class="docs",
                ticket_id=int(ticket.pk),
                expedite_authorize="owner-x",
                local_ci_green_sha=_SHA,
            ),
        )
        with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_pending_stub()):
            result = cast(
                "dict[str, object]",
                call_command("ticket", "merge", str(issued["clear_id"])),
            )
        assert result["merged"] is False
        assert result["escalated"] is True
