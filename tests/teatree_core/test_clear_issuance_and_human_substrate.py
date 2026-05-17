"""The CLEAR→MergeClear contract: issuance seam + sanctioned human-substrate merge.

#863 added the *consume* side (``t3 <overlay> ticket merge <clear_id>``) and the
prohibition guard, but left two gaps in the orchestrator-decides /
loop-executes topology (BLUEPRINT §17.4):

Gap 1 — No issuance seam. There was no ``t3`` command for an orchestrator to
record a per-diff CLEAR as a durable ``MergeClear`` row the loop can act on by
id. ``ticket clear`` is that seam (§17.4.2 — the orchestrator's only merge
output is a ``MergeClear`` row; §17.8 clause 3 — it must be independently
reviewed, so the issuer cannot equal the executing loop and a maker/loop role
cannot issue it).

Gap 2 — No sanctioned human-substrate merge. ``assert_merge_preconditions``
refuses ``blast_class == substrate`` unconditionally — correct for the loop,
but BLUEPRINT invariant 8 says even a human/owner merge must go through a
sanctioned ``t3`` path, never raw ``gh``. ``human_authorizer`` + ``ticket
merge --human-authorized`` is that path: a substrate CLEAR a human explicitly
authorised still merges through the same SHA-bound, audited transition, just
with the human decision recorded durably.

Only the unstoppable external — the ``gh`` subprocess — is stubbed; every
teatree model / FSM / DB write is real.
"""

from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.merge_execution import MergePreconditionError, merge_ticket_pr
from teatree.core.models import MergeAudit, MergeClear, Ticket

pytestmark = pytest.mark.django_db

_SHA = "c" * 40
_GREEN = '[{"status": "COMPLETED", "conclusion": "SUCCESS"}]'


def _gh_stub(argv: list[str]) -> tuple[int, str, str]:
    joined = " ".join(argv)
    if "headRefOid" in joined:
        return (0, _SHA, "")
    if "isDraft" in joined:
        return (0, "false", "")
    if "statusCheckRollup" in joined:
        return (0, _GREEN, "")
    if "pulls" in joined and "merge" in joined:
        return (0, '{"sha": "landed00deadbeef"}', "")
    return (0, "", "")


class TestClearIssuanceSeam(TestCase):
    """``t3 ... ticket clear`` records the orchestrator's per-diff CLEAR."""

    def test_clear_creates_actionable_mergeclear_row(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "859",
                "souliane/teatree",
                _SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="docs",
                ticket_id=int(ticket.pk),
            ),
        )
        assert result["issued"]
        clear = MergeClear.objects.get(pk=result["clear_id"])
        assert clear.is_actionable()
        assert clear.reviewer_identity == "cold-reviewer"
        assert clear.reviewed_sha == _SHA
        assert clear.blast_class == MergeClear.BlastClass.DOCS
        assert clear.ticket_id == ticket.pk

    def test_clear_then_merge_round_trip(self) -> None:
        """The seam closes the loop: issue a CLEAR, the loop merges by its id."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        issued = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "861",
                "souliane/teatree",
                _SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="logic",
                ticket_id=int(ticket.pk),
            ),
        )
        with patch("teatree.core.merge_execution._run_gh", side_effect=_gh_stub):
            merged = cast(
                "dict[str, object]",
                call_command("ticket", "merge", str(issued["clear_id"]), loop_identity="merge-loop"),
            )
        ticket.refresh_from_db()
        assert merged["merged"]
        assert ticket.state == Ticket.State.MERGED

    def test_clear_issuer_equal_to_executing_loop_is_refused(self) -> None:
        """§17.8 clause 3: a CLEAR cannot be issued by the loop that will execute it."""
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "862",
                "souliane/teatree",
                _SHA,
                reviewer_identity="merge-loop",
                gh_verify_result="green",
                blast_class="docs",
            ),
        )
        assert not result["issued"]
        assert "merge-loop" in result["error"]
        assert MergeClear.objects.count() == 0

    def test_clear_with_maker_reviewer_is_refused(self) -> None:
        """A maker/coding-agent/loop role cannot self-attest its own review."""
        for maker in ("maker:coding", "coding-agent", "loop"):
            with self.subTest(maker=maker):
                result = cast(
                    "dict[str, object]",
                    call_command(
                        "ticket",
                        "clear",
                        "863",
                        "souliane/teatree",
                        _SHA,
                        reviewer_identity=maker,
                        gh_verify_result="green",
                        blast_class="docs",
                    ),
                )
                assert not result["issued"]
                assert "reviewer" in result["error"].lower()
        assert MergeClear.objects.count() == 0

    def test_clear_rejects_unknown_blast_class(self) -> None:
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "864",
                "souliane/teatree",
                _SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="enormous",
            ),
        )
        assert not result["issued"]
        assert "blast_class" in result["error"]
        assert MergeClear.objects.count() == 0

    def test_clear_with_unknown_ticket_id_is_refused(self) -> None:
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "868",
                "souliane/teatree",
                _SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="docs",
                ticket_id=999999,
            ),
        )
        assert not result["issued"]
        assert "not found" in result["error"]
        assert MergeClear.objects.count() == 0

    def test_clear_rejects_unknown_gh_verify_result(self) -> None:
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "866",
                "souliane/teatree",
                _SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result="maybe",
                blast_class="docs",
            ),
        )
        assert not result["issued"]
        assert "gh_verify_result" in result["error"]
        assert MergeClear.objects.count() == 0

    def test_clear_rejects_empty_reviewer_identity(self) -> None:
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "867",
                "souliane/teatree",
                _SHA,
                reviewer_identity="   ",
                gh_verify_result="green",
                blast_class="docs",
            ),
        )
        assert not result["issued"]
        assert "reviewer_identity is required" in result["error"]
        assert MergeClear.objects.count() == 0

    def test_clear_rejects_branch_ref_instead_of_sha(self) -> None:
        """``reviewed_sha`` binds to an exact tree — a branch ref is not a SHA."""
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "865",
                "souliane/teatree",
                "main",
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="docs",
            ),
        )
        assert not result["issued"]
        assert "sha" in result["error"].lower()
        assert MergeClear.objects.count() == 0


class TestSubstrateStaysHumanMergeOnly(TestCase):
    """The loop never auto-merges substrate; an un-authorised substrate CLEAR holds."""

    def test_substrate_clear_without_human_authorizer_is_held(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=870,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.SUBSTRATE,
        )
        with (
            patch("teatree.core.merge_execution._run_gh", side_effect=_gh_stub),
            pytest.raises(MergePreconditionError, match="substrate"),
        ):
            merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW
        assert not MergeAudit.objects.filter(clear=clear).exists()

    def test_clear_command_can_record_human_authorizer_for_substrate(self) -> None:
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "871",
                "souliane/teatree",
                _SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="substrate",
                human_authorize="owner:adrien",
            ),
        )
        assert result["issued"]
        clear = MergeClear.objects.get(pk=result["clear_id"])
        assert clear.human_authorizer == "owner:adrien"

    def test_human_authorize_rejected_for_non_substrate(self) -> None:
        """``--human-authorize`` is meaningless off the substrate path — reject it loudly."""
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "872",
                "souliane/teatree",
                _SHA,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="logic",
                human_authorize="owner:adrien",
            ),
        )
        assert not result["issued"]
        assert "substrate" in result["error"]
        assert MergeClear.objects.count() == 0


class TestSanctionedHumanSubstrateMerge(TestCase):
    """A human-authorised substrate CLEAR merges through the SAME t3 transition."""

    def test_human_authorized_substrate_merges_and_records_authorizer(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=873,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.SUBSTRATE,
            human_authorizer="owner:adrien",
        )
        with patch("teatree.core.merge_execution._run_gh", side_effect=_gh_stub):
            result = cast(
                "dict[str, object]",
                call_command(
                    "ticket",
                    "merge",
                    str(clear.pk),
                    loop_identity="merge-loop",
                    human_authorized="owner:adrien",
                ),
            )
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert result["merged"]
        assert ticket.state == Ticket.State.MERGED
        assert clear.consumed_at is not None
        audit = MergeAudit.objects.get(clear=clear)
        assert audit.required_checks_status == "green"

    def test_substrate_merge_without_human_authorized_flag_is_held(self) -> None:
        """Even an authorised CLEAR will not auto-merge: the human flag is mandatory at execute time."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=874,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.SUBSTRATE,
            human_authorizer="owner:adrien",
        )
        with patch("teatree.core.merge_execution._run_gh", side_effect=_gh_stub):
            result = cast("dict[str, object]", call_command("ticket", "merge", str(clear.pk)))
        ticket.refresh_from_db()
        assert result["escalated"]
        assert not result["merged"]
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_human_authorized_flag_must_match_recorded_authorizer(self) -> None:
        """The execute-time human flag must match the CLEAR's recorded authoriser."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=875,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.SUBSTRATE,
            human_authorizer="owner:adrien",
        )
        with patch("teatree.core.merge_execution._run_gh", side_effect=_gh_stub):
            result = cast(
                "dict[str, object]",
                call_command(
                    "ticket",
                    "merge",
                    str(clear.pk),
                    human_authorized="someone-else",
                ),
            )
        ticket.refresh_from_db()
        assert result["escalated"]
        assert not result["merged"]
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_human_authorized_flag_on_non_substrate_clear_is_refused(self) -> None:
        """The human-substrate escape hatch must not be usable to bypass loop review of logic PRs."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=876,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
        )
        with patch("teatree.core.merge_execution._run_gh", side_effect=_gh_stub):
            result = cast(
                "dict[str, object]",
                call_command(
                    "ticket",
                    "merge",
                    str(clear.pk),
                    human_authorized="owner:adrien",
                ),
            )
        ticket.refresh_from_db()
        assert result["escalated"]
        assert not result["merged"]
        assert ticket.state == Ticket.State.IN_REVIEW


class TestPrMergeRedirectedToKeystone(TestCase):
    """The old ``t3 ... pr merge`` path is FSM-incoherent post-#863 and must refuse."""

    def test_pr_merge_refuses_and_points_at_keystone(self) -> None:
        result = cast(
            "dict[str, object]",
            call_command("pr", "merge", "859", "souliane/teatree"),
        )
        assert not result["merged"]
        assert "ticket merge" in result["error"]
        assert "ticket clear" in result["error"]
