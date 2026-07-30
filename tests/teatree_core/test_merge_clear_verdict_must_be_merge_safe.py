"""A MergeClear may not authorize a merge over a recorded non-green verdict.

The merge ceremony is maker≠checker: the cold reviewer's recorded verdict at
the reviewed tree is authoritative (§17.4.2 / §17.8 clause 3). The merge-time
gate re-checks the forge's *live* CI rollup — so if a CLEAR could be issued
recording a HOLD (``pending``/``failed``) verdict, a later self-flip of CI to
green would let the live re-check stamp green over the reviewer's recorded
HOLD. Green-over-HOLD must be impossible: a non-green verdict can never become
an actionable authorization, and a merge driven by such a row must refuse.

Integration-style: the real guarded factory (``MergeClear.issue`` via the
``ticket clear`` management command), the real merge ceremony
(``merge_ticket_pr``), real ORM rows built with ``MergeClearFactory``. Only the
``gh`` subprocess — the unstoppable external — is stubbed.
"""

from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.merge import MergePreconditionError, merge_ticket_pr
from teatree.core.models import MergeAudit, MergeClear
from tests.factories import _FORTY_HEX, MergeClearFactory, TicketFactory
from tests.teatree_core.conftest import seed_merge_safe_verdict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _skip_author_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # #1773 public-repo author gate — exercised by test_merge_execution_author_gate;
    # these pre-date it and target other concerns, so it is a no-op here.
    monkeypatch.setattr("teatree.core.merge.execution.assert_merge_provenance_trusted", lambda **_: None)


_GREEN = '[{"status": "COMPLETED", "conclusion": "SUCCESS"}]'
_HOLD_VERDICTS = ("pending", "failed")


def _gh_stub_live_green(argv: list[str]) -> tuple[int, str, str]:
    """A forge whose LIVE rollup is green — the worst case for a HOLD CLEAR.

    Models CI having self-flipped to green after the reviewer recorded a HOLD:
    the merge-time live re-check passes, so only a guard on the *recorded*
    verdict can stop the green-over-HOLD merge.
    """
    joined = " ".join(argv)
    if "headRefOid" in joined:
        return (0, _FORTY_HEX, "")
    if "isDraft" in joined:
        return (0, "false", "")
    if "statusCheckRollup" in joined:
        return (0, _GREEN, "")
    if "baseRefName" in joined or "required_status_checks" in joined:
        # Base branch "main"; an empty required-context gate → the live rollup verdict stands.
        return (0, "main" if "baseRefName" in joined else '{"contexts": []}', "")
    if "pulls" in joined and "merge" in joined:
        return (0, '{"sha": "landed00deadbeef"}', "")
    return (0, "", "")


class TestNonGreenVerdictNeverIssuable(TestCase):
    """The guarded factory refuses to record a non-green reviewer verdict."""

    def test_clear_with_non_green_verdict_is_refused_at_issuance(self) -> None:
        for verdict in _HOLD_VERDICTS:
            with self.subTest(verdict=verdict):
                ticket = TicketFactory()
                result = cast(
                    "dict[str, object]",
                    call_command(
                        "ticket",
                        "clear",
                        "1160",
                        "souliane/teatree",
                        reviewed_sha=_FORTY_HEX,
                        reviewer_identity="cold-reviewer",
                        gh_verify_result=verdict,
                        blast_class="docs",
                        ticket_id=int(ticket.pk),
                    ),
                )
                assert not result["issued"]
                # Split messages (FIX-EXPEDITE): a pending snapshot is refused as
                # "issuable only via the expedite waiver"; a failed one as "can never
                # authorize a merge". Both name their verdict class.
                assert verdict in cast("str", result["error"]).lower()
        assert MergeClear.objects.count() == 0

    def test_clear_with_green_verdict_is_issued(self) -> None:
        ticket = TicketFactory()
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "1160",
                "souliane/teatree",
                reviewed_sha=_FORTY_HEX,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="docs",
                ticket_id=int(ticket.pk),
            ),
        )
        assert result["issued"]
        assert MergeClear.objects.get(pk=result["clear_id"]).gh_verify_result == MergeClear.VerifyResult.GREEN


class TestNonGreenVerdictNeverMerges(TestCase):
    """Even a directly-written HOLD row can never drive a green-over-HOLD merge.

    A row written via ``objects.create`` (fixture/migration/non-factory path)
    bypasses the issue-time guard, so the merge ceremony must independently
    refuse a non-green CLEAR rather than rely on the live CI re-check, which is
    green here precisely to model the regression.
    """

    def test_merge_over_recorded_hold_verdict_refuses_and_leaves_ticket_unmerged(self) -> None:
        for trait in _HOLD_VERDICTS:
            with self.subTest(verdict=trait):
                clear = MergeClearFactory(**{trait: True})
                ticket = clear.ticket
                with (
                    patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_stub_live_green),
                    # FIX-EXPEDITE split: pending refuses with "not green" (no waiver
                    # presented), failed refuses with "FAILED required check".
                    pytest.raises(MergePreconditionError, match=r"not green|FAILED required check"),
                ):
                    merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")
                ticket.refresh_from_db()
                assert ticket.state == MergeClear.objects.get(pk=clear.pk).ticket.State.IN_REVIEW
                assert not MergeAudit.objects.filter(clear=clear).exists()
                clear.refresh_from_db()
                assert clear.consumed_at is None

    def test_green_verdict_clear_merges_through_the_keystone(self) -> None:
        clear = MergeClearFactory()
        ticket = clear.ticket
        # Seed the #2829 sibling verdict the real ``clear`` path records.
        seed_merge_safe_verdict(slug=clear.slug, pr_id=clear.pr_id, sha=clear.reviewed_sha)
        with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_stub_live_green):
            merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert ticket.state == ticket.State.MERGED
        assert clear.consumed_at is not None
