"""Lifecycle phase-coverage gate at the ``merge_safe`` verdict chokepoint (#3762).

The failure this reconstructs: an out-of-band change ships a
file the resolver never read — one phase of a multi-phase plan silently skipped
— and its ticket's ledger showed ``tasks: 1`` at phase ``reviewing`` with
``visited_phases: ['reviewing']``. No ``coding``, no ``testing``, ever. The
implementation happened entirely out of band and teatree was handed a finished
PR to review, so every gate keyed to the coding/testing lifecycle was
structurally absent. Across the whole ledger this is the anomaly: 304 of 306
tickets carry a task or a phase visit for the work itself.

The tests below pin both directions: the reconstructed #338 shape BLOCKS at
``ReviewVerdict.record(verdict="merge_safe")``, and a normally-governed ticket
(coding + testing coverage) records clean.
"""

from unittest import mock

import pytest
from django.db import DatabaseError
from django.test import TestCase

from teatree.core.models import ConfigSetting, PullRequest, ReviewVerdictError, Session, Task, Ticket
from teatree.core.models.out_of_band_approval import OutOfBandWorkApproval, OutOfBandWorkAudit
from teatree.core.models.phase_coverage_gate import PhaseCoverageError, check_phase_coverage, lifecycle_coverage

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SHA = "a" * 40
_SLUG = "souliane/teatree"
_PR_ID = 3710


def _ticket(*, overlay: str = "t3-teatree") -> Ticket:
    return Ticket.objects.create(overlay=overlay, state=Ticket.State.IN_REVIEW)


def _session(ticket: Ticket, phases: list[str]) -> Session:
    return Session.objects.create(ticket=ticket, overlay=ticket.overlay, visited_phases=list(phases))


def _task(ticket: Ticket, session: Session, phase: str) -> Task:
    return Task.objects.create(ticket=ticket, session=session, phase=phase, subject=f"{phase} work")


def _ticket_338() -> Ticket:
    """The reconstructed shape of ticket #338 — the out-of-band lifecycle ledger.

    One session whose only visited phase is ``reviewing``; one task, also at
    ``reviewing``. Nothing else was ever recorded for the work.
    """
    ticket = _ticket()
    session = _session(ticket, ["reviewing"])
    _task(ticket, session, "reviewing")
    return ticket


def _governed_ticket() -> Ticket:
    """A normally-governed ticket: coding + testing visits, then reviewing."""
    ticket = _ticket()
    session = _session(ticket, ["planning", "coding", "testing", "reviewing"])
    _task(ticket, session, "coding")
    _task(ticket, session, "testing")
    return ticket


class TestLifecycleCoverage(TestCase):
    def test_reconstructed_338_shape_has_records_but_no_coding_or_testing(self) -> None:
        coverage = lifecycle_coverage(_ticket_338())
        assert coverage.has_lifecycle_record
        assert not coverage.covered
        assert coverage.visited_phases == ["reviewing"]
        assert coverage.task_phases == ["reviewing"]

    def test_a_coding_phase_visit_alone_is_coverage(self) -> None:
        ticket = _ticket()
        _session(ticket, ["coding", "reviewing"])
        assert lifecycle_coverage(ticket).covered

    def test_a_testing_task_alone_is_coverage(self) -> None:
        ticket = _ticket()
        session = _session(ticket, ["reviewing"])
        _task(ticket, session, "testing")
        assert lifecycle_coverage(ticket).covered

    def test_short_verb_phase_spellings_count_as_coverage(self) -> None:
        ticket = _ticket()
        session = _session(ticket, ["review"])
        _task(ticket, session, "code")
        assert lifecycle_coverage(ticket).covered

    def test_an_e2e_task_is_coverage_for_the_external_delivery_loop(self) -> None:
        # `ReviewLoop.start_external_loop` records `e2e` tasks, not `coding`: a
        # hand delivery teatree genuinely verified end-to-end is exercised work.
        ticket = _ticket()
        session = _session(ticket, ["reviewing"])
        _task(ticket, session, "e2e")
        assert lifecycle_coverage(ticket).covered

    def test_phases_about_the_change_are_not_coverage(self) -> None:
        # Planning, shipping and retro happen AROUND the change; none of them
        # means anyone exercised it.
        ticket = _ticket()
        _session(ticket, ["planning", "reviewing", "shipping", "retro"])
        assert not lifecycle_coverage(ticket).covered

    def test_a_ticket_with_no_ledger_at_all_never_entered_the_lifecycle(self) -> None:
        coverage = lifecycle_coverage(_ticket())
        assert not coverage.has_lifecycle_record
        assert not coverage.covered


class TestCheckPhaseCoverage(TestCase):
    def test_blocks_the_reconstructed_338_shape(self) -> None:
        ticket = _ticket_338()
        with pytest.raises(PhaseCoverageError) as excinfo:
            check_phase_coverage(ticket, head_sha=_SHA)
        message = str(excinfo.value)
        assert "only at 'reviewing'" in message
        assert "lifecycle visit-phase" in message
        assert "lifecycle approve-out-of-band" in message

    def test_passes_a_governed_ticket(self) -> None:
        check_phase_coverage(_governed_ticket(), head_sha=_SHA)

    def test_passes_a_ticket_that_never_entered_the_lifecycle(self) -> None:
        # A cold review of work teatree never owned (a stranger's PR) has no
        # lifecycle to be routed around — the gate has nothing to say about it.
        check_phase_coverage(_ticket(), head_sha=_SHA)

    def test_kill_switch_disables_the_gate(self) -> None:
        ConfigSetting.objects.set_value("phase_coverage_gate_enabled", value=False)
        check_phase_coverage(_ticket_338(), head_sha=_SHA)

    def test_per_overlay_kill_switch_beats_the_global_row(self) -> None:
        ConfigSetting.objects.set_value("phase_coverage_gate_enabled", value=True)
        ConfigSetting.objects.set_value("phase_coverage_gate_enabled", value=False, scope="t3-teatree")
        check_phase_coverage(_ticket_338(), head_sha=_SHA)

    def test_an_unreadable_ledger_fails_open(self) -> None:
        # Never-lockout: a gate that cannot read its own evidence must pass, so a
        # DB outage can never strand a reviewed, green, SHA-bound merge.
        ticket = _ticket_338()
        with mock.patch(
            "teatree.core.models.phase_coverage_gate.lifecycle_coverage",
            side_effect=DatabaseError("database is locked"),
        ):
            check_phase_coverage(ticket, head_sha=_SHA)

    def test_a_recorded_override_is_consumed_single_use_and_audited(self) -> None:
        ticket = _ticket_338()
        OutOfBandWorkApproval.record(
            ticket=ticket,
            head_sha=_SHA,
            approver_id="souliane",
            reason="docs typo; no code path touched",
        )
        check_phase_coverage(ticket, head_sha=_SHA)

        audit = OutOfBandWorkAudit.objects.get(ticket=ticket)
        assert (audit.approver_id, audit.head_sha) == ("souliane", _SHA)
        assert audit.reason == "docs typo; no code path touched"

        with pytest.raises(PhaseCoverageError):
            check_phase_coverage(ticket, head_sha=_SHA)

    def test_an_override_for_another_tree_does_not_carry(self) -> None:
        ticket = _ticket_338()
        OutOfBandWorkApproval.record(
            ticket=ticket, head_sha=_SHA, approver_id="souliane", reason="revert of a bad merge"
        )
        with pytest.raises(PhaseCoverageError):
            check_phase_coverage(ticket, head_sha="b" * 40)


class TestVerdictRecordChokepoint(TestCase):
    """The gate fires where the merge door is: recording a ``merge_safe`` verdict."""

    def _record(self, ticket: Ticket | None, *, verdict: str = "merge_safe", sha: str = _SHA) -> None:
        from teatree.core.models import ReviewVerdict  # noqa: PLC0415 — kept local to the chokepoint tests

        ReviewVerdict.record(
            pr_id=_PR_ID,
            slug=_SLUG,
            reviewed_sha=sha,
            verdict=verdict,
            reviewer_identity="cold-reviewer",
            ticket=ticket,
        )

    def test_merge_safe_on_the_reconstructed_338_shape_is_refused(self) -> None:
        # The factory surfaces the refusal as its own contract error, keeping the
        # gate's PhaseCoverageError as the cause, so `review record` reports it
        # through the one error type it already handles.
        with pytest.raises(ReviewVerdictError, match="only at 'reviewing'") as excinfo:
            self._record(_ticket_338())
        assert isinstance(excinfo.value.__cause__, PhaseCoverageError)

    def test_merge_safe_on_a_governed_ticket_records_clean(self) -> None:
        from teatree.core.models import ReviewVerdict  # noqa: PLC0415 — kept local to the chokepoint tests

        self._record(_governed_ticket())
        assert ReviewVerdict.objects.for_pr(_SLUG, _PR_ID).get().is_merge_safe()

    def test_a_hold_verdict_is_never_gated(self) -> None:
        # A reviewer must always be able to record findings; only the merge-
        # authorising verdict is gated.
        self._record(_ticket_338(), verdict="hold")

    def test_omitting_the_ticket_does_not_dodge_the_gate(self) -> None:
        ticket = _ticket_338()
        PullRequest.objects.create(
            ticket=ticket,
            overlay=ticket.overlay,
            url=f"https://github.com/{_SLUG}/pull/{_PR_ID}",
            repo=_SLUG,
            iid=str(_PR_ID),
        )
        with pytest.raises(ReviewVerdictError, match="only at 'reviewing'"):
            self._record(None)

    def test_a_second_verdict_at_a_moved_head_is_not_re_gated(self) -> None:
        # Phase coverage is a ticket-level property: once a PR's first merge_safe
        # verdict has cleared the bar, a re-review at a moved head (force-push,
        # conflict-only rebind carry-forward) is not re-judged on it.
        governed = _governed_ticket()
        self._record(governed)
        self._record(governed, sha="c" * 40)

    def test_an_unresolvable_ticket_is_not_gated(self) -> None:
        from teatree.core.models import ReviewVerdict  # noqa: PLC0415 — kept local to the chokepoint tests

        self._record(None)
        assert ReviewVerdict.objects.for_pr(_SLUG, _PR_ID).get().is_merge_safe()
