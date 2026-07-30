"""The ``debt_delta_gate`` core wrapper — scan a diff, honour manifest waivers, refuse (PR-3).

``check_debt_delta`` scans a ship diff for net-new tech-debt suppressions and
refuses unless every one is covered by an ``approved_debt`` waiver on the
ticket's latest plan manifest. Anti-vacuity: a net-new ``# noqa`` is refused; a
clean diff passes; a diff whose only debt is a REMOVED suppression passes (delta,
not absolute); a genuinely-justified suppression passes once the plan records the
audited waiver; a blank-reason waiver never covers.
"""

import pytest
from django.test import TestCase

from teatree.core.gates.debt_delta_gate import DebtDeltaExceededError, check_debt_delta, waivers_for_ticket
from teatree.core.models import PlanArtifact, Ticket
from teatree.quality.debt_delta import DebtWaiver

_NEW_NOQA = (
    "diff --git a/src/teatree/m.py b/src/teatree/m.py\n"
    "--- a/src/teatree/m.py\n"
    "+++ b/src/teatree/m.py\n"
    "@@ -1,2 +1,3 @@\n"
    " unchanged = 1\n"
    "+risky = frobnicate()  # noqa: F821\n"
)
_REMOVED_NOQA = (
    "diff --git a/src/teatree/m.py b/src/teatree/m.py\n"
    "--- a/src/teatree/m.py\n"
    "+++ b/src/teatree/m.py\n"
    "@@ -1,2 +1,1 @@\n"
    "-risky = frobnicate()  # noqa: F821\n"
)
_CLEAN = (
    "diff --git a/src/teatree/m.py b/src/teatree/m.py\n"
    "--- a/src/teatree/m.py\n"
    "+++ b/src/teatree/m.py\n"
    "@@ -1,1 +1,2 @@\n"
    " unchanged = 1\n"
    "+clean = compute()\n"
)


def _plan(ticket: Ticket, *, adequacy: dict) -> PlanArtifact:
    return PlanArtifact.objects.create(
        ticket=ticket,
        plan_text="plan",
        recorded_by="tester",
        adequacy=adequacy,
    )


class TestCheckDebtDeltaWithExplicitWaivers(TestCase):
    """The pure gate path — waivers passed in, no DB read."""

    def _ticket(self) -> Ticket:
        return Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)

    def test_refuses_net_new_noqa_with_no_waiver(self) -> None:
        with pytest.raises(DebtDeltaExceededError) as excinfo:
            check_debt_delta(self._ticket(), _NEW_NOQA, waivers=())
        message = str(excinfo.value)
        assert "noqa" in message
        assert "require_debt_delta" in message  # names the operator escape

    def test_passes_a_clean_diff(self) -> None:
        check_debt_delta(self._ticket(), _CLEAN, waivers=())  # no raise

    def test_passes_when_only_debt_is_removed(self) -> None:
        # Delta, not absolute: deleting a suppression is a shrink, never refused.
        check_debt_delta(self._ticket(), _REMOVED_NOQA, waivers=())

    def test_passes_when_a_waiver_covers_the_introduction(self) -> None:
        waiver = DebtWaiver(pattern="noqa: F821", reason="upstream stub gap, tracked separately")
        check_debt_delta(self._ticket(), _NEW_NOQA, waivers=(waiver,))  # no raise


class TestCheckDebtDeltaReadsPlanManifest(TestCase):
    """The DB path — waivers resolved from the ticket's latest plan manifest."""

    def _ticket(self) -> Ticket:
        return Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)

    def test_refuses_when_manifest_has_no_matching_waiver(self) -> None:
        ticket = self._ticket()
        _plan(ticket, adequacy={"design": {"content": "d"}})
        with pytest.raises(DebtDeltaExceededError):
            check_debt_delta(ticket, _NEW_NOQA)

    def test_passes_when_manifest_waives_the_introduction(self) -> None:
        ticket = self._ticket()
        _plan(ticket, adequacy={"approved_debt": [{"pattern": "noqa: F821", "reason": "stub gap"}]})
        check_debt_delta(ticket, _NEW_NOQA)  # no raise

    def test_blank_reason_manifest_waiver_does_not_cover(self) -> None:
        ticket = self._ticket()
        _plan(ticket, adequacy={"approved_debt": [{"pattern": "noqa", "reason": ""}]})
        with pytest.raises(DebtDeltaExceededError):
            check_debt_delta(ticket, _NEW_NOQA)

    def test_uses_the_latest_plan_artifact(self) -> None:
        ticket = self._ticket()
        _plan(ticket, adequacy={})  # older, no waiver
        _plan(ticket, adequacy={"approved_debt": [{"pattern": "noqa", "reason": "stub gap"}]})
        check_debt_delta(ticket, _NEW_NOQA)  # latest waiver wins -> no raise

    def test_waivers_for_ticket_empty_without_a_plan(self) -> None:
        assert waivers_for_ticket(self._ticket()) == ()
