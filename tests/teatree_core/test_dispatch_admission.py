"""Governor gating of the INTERACTIVE dispatch chokepoint (#4107).

``decide_admission`` had exactly two callers, both factory lanes
(``core.agent_admission``, ``loop.admission``), so an orchestrator session
dispatching through the ``Agent``/``Task`` tool was admitted with no ceiling and
no load brake — the two agent populations summed unchecked (measured: load 58 on
8 cores while ``issue_implementer_max_concurrent = 3`` was in force). These cover
the core-side seam the dispatch gate consults.
"""

import datetime as dt
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core import dispatch_admission as gate_mod
from teatree.core.admission_governor import BRAKE_LOAD_PER_CORE, AdmissionDecision, MachineSignal, QuotaSignal
from teatree.core.dispatch_admission import dispatch_admission_denied_reason, release_interactive_dispatch
from teatree.core.models import SEAT_WINDOW, InteractiveDispatch, Session, Task, Ticket

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_WEEK = 7 * 24 * 3600
_CORES = 8
_OVER_THE_WATERMARK = BRAKE_LOAD_PER_CORE * _CORES + 1


def _healthy_quota() -> QuotaSignal:
    return QuotaSignal(
        fresh=True,
        all_accounts_exhausted=False,
        weekly_utilization=0.1,
        short_utilization=0.1,
        seconds_to_weekly_reset=_WEEK * 0.5,
    )


def _unknown_quota() -> QuotaSignal:
    return QuotaSignal(
        fresh=False,
        all_accounts_exhausted=False,
        weekly_utilization=0.0,
        short_utilization=0.0,
        seconds_to_weekly_reset=None,
    )


def _admits(*, ceiling: int) -> AdmissionDecision:
    """A healthy verdict at an exact *ceiling* — the seat arithmetic under test, not the ceiling's."""
    return AdmissionDecision(admit=True, reason="", ceiling=ceiling, braked=False)


@contextmanager
def _signals(*, load1: float = 1.0, quota: QuotaSignal | None = None) -> Iterator[None]:
    machine = MachineSignal(cores=_CORES, load1=load1, ram_available_gb=20.0)
    with ExitStack() as stack:
        stack.enter_context(patch.object(gate_mod, "governor_enabled", return_value=True))
        stack.enter_context(patch.object(gate_mod, "read_quota_signal", return_value=quota or _healthy_quota()))
        stack.enter_context(patch.object(gate_mod, "read_machine_signal", return_value=machine))
        yield


class TestDispatchAdmissionDeniedReason(TestCase):
    def test_kill_switch_off_admits(self) -> None:
        # Acceptance #3: admission_governor_enabled = false ⇒ exactly today's behaviour.
        with patch.object(gate_mod, "governor_enabled", return_value=False):
            assert dispatch_admission_denied_reason() is None

    def test_kill_switch_off_never_probes(self) -> None:
        with (
            patch.object(gate_mod, "governor_enabled", return_value=False),
            patch.object(gate_mod, "decide_admission") as decide,
        ):
            dispatch_admission_denied_reason()
        decide.assert_not_called()

    def test_the_dispatch_path_calls_decide_admission(self) -> None:
        # Acceptance #2: the governor's caller count is no longer "factory lanes only".
        with _signals(), patch.object(gate_mod, "decide_admission", wraps=gate_mod.decide_admission) as decide:
            dispatch_admission_denied_reason()
        decide.assert_called_once()

    def test_load_over_the_watermark_denies_naming_the_brake(self) -> None:
        # Acceptance #1: over the load brake watermark ⇒ denied, message names the brake.
        with _signals(load1=_OVER_THE_WATERMARK):
            reason = dispatch_admission_denied_reason()
        assert reason is not None
        assert "load" in reason
        assert "watermark" in reason

    def test_healthy_signals_below_ceiling_admit(self) -> None:
        with _signals():
            assert dispatch_admission_denied_reason() is None

    def test_a_signal_read_failure_admits_fail_open(self) -> None:
        with (
            patch.object(gate_mod, "governor_enabled", return_value=True),
            patch.object(gate_mod, "read_quota_signal", side_effect=RuntimeError("probe down")),
        ):
            assert dispatch_admission_denied_reason() is None

    def test_live_agent_count_at_ceiling_denies(self) -> None:
        with _signals(), patch.object(gate_mod, "_claimed_agent_count", return_value=999):
            reason = dispatch_admission_denied_reason()
        assert reason is not None
        assert "at/over governor ceiling" in reason

    def test_ceiling_is_skipped_when_not_applied(self) -> None:
        # An already-admitted caller (a sub-agent whose own lane admitted it) must not be
        # re-clamped by the same ceiling — that would deadlock it against its own claim.
        with _signals(), patch.object(gate_mod, "_claimed_agent_count", return_value=999):
            assert dispatch_admission_denied_reason(apply_ceiling=False) is None

    def test_brakes_still_apply_when_the_ceiling_is_skipped(self) -> None:
        with _signals(load1=_OVER_THE_WATERMARK):
            assert dispatch_admission_denied_reason(apply_ceiling=False) is not None

    def test_an_unknown_quota_still_bounds_the_lane(self) -> None:
        # #4097: an unknown budget is the CONSERVATIVE case, never the unbounded one — the
        # ceiling falls back to the machine signal the governor DID read, so this lane is
        # gated on a real count rather than waved through.
        with _signals(quota=_unknown_quota()), patch.object(gate_mod, "_claimed_agent_count", return_value=999):
            reason = dispatch_admission_denied_reason()
        assert reason is not None
        assert "at/over governor ceiling" in reason

    def test_an_unknown_quota_admits_under_the_machine_ceiling(self) -> None:
        with _signals(quota=_unknown_quota()), patch.object(gate_mod, "_claimed_agent_count", return_value=0):
            assert dispatch_admission_denied_reason() is None


class TestTheCeilingSeesItsOwnAdmissions(TestCase):
    """#4129: an ad-hoc interactive dispatch creates no ``Task`` row, so it was uncountable.

    ``live_agent_count`` read ``Task.objects.active_claims()`` — durable, and the right
    shape — but the scenario the module exists for creates no ``Task`` at all, so N rapid
    dispatches each read the same count and every one passed.
    """

    def test_the_second_dispatch_sees_the_first(self) -> None:
        with _signals(), patch.object(gate_mod, "decide_admission", return_value=_admits(ceiling=1)):
            assert dispatch_admission_denied_reason(session_id="s-4129") is None
            reason = dispatch_admission_denied_reason(session_id="s-4129")
        assert reason is not None
        assert "at/over governor ceiling" in reason

    def test_n_rapid_dispatches_admit_only_the_ceilings_worth(self) -> None:
        with _signals(), patch.object(gate_mod, "decide_admission", return_value=_admits(ceiling=2)):
            verdicts = [dispatch_admission_denied_reason(session_id="s-4129") for _ in range(5)]
        assert sum(verdict is None for verdict in verdicts) == 2, verdicts

    def test_a_refused_dispatch_takes_no_seat(self) -> None:
        # A denial that left its row behind would spend the seat it was refused, so the
        # lane would narrow by one on every refusal until nothing could be admitted.
        with _signals(), patch.object(gate_mod, "decide_admission", return_value=_admits(ceiling=1)):
            dispatch_admission_denied_reason(session_id="s-4129")
            dispatch_admission_denied_reason(session_id="s-4129")
        assert InteractiveDispatch.objects.live_seats().count() == 1

    def test_a_dispatch_that_never_materialises_releases_its_seat(self) -> None:
        with _signals(), patch.object(gate_mod, "decide_admission", return_value=_admits(ceiling=1)):
            assert dispatch_admission_denied_reason(session_id="s-4129") is None
            InteractiveDispatch.objects.update(admitted_at=timezone.now() - SEAT_WINDOW - dt.timedelta(seconds=1))
            assert dispatch_admission_denied_reason(session_id="s-4129") is None

    def test_a_braked_dispatch_takes_no_seat(self) -> None:
        with _signals(load1=_OVER_THE_WATERMARK):
            assert dispatch_admission_denied_reason(session_id="s-4129") is not None
        assert InteractiveDispatch.objects.live_seats().count() == 0

    def test_the_ceiling_exempt_arm_is_still_counted(self) -> None:
        # A sub-agent's onward dispatch and the TaskCreated fan-out keep their documented
        # exemption from the CEILING, but they put an agent on the box either way — so the
        # arm that does clamp must be able to see them.
        with _signals(), patch.object(gate_mod, "decide_admission", return_value=_admits(ceiling=1)):
            assert dispatch_admission_denied_reason(apply_ceiling=False, session_id="s-4129") is None
            reason = dispatch_admission_denied_reason(session_id="s-4129")
        assert reason is not None
        assert "at/over governor ceiling" in reason

    def test_a_seat_write_failure_admits_fail_open(self) -> None:
        with (
            _signals(),
            patch.object(InteractiveDispatch.objects, "claim_seat", side_effect=RuntimeError("db gone")),
        ):
            assert dispatch_admission_denied_reason(session_id="s-4129") is None

    def test_the_kill_switch_writes_no_seat(self) -> None:
        with patch.object(gate_mod, "governor_enabled", return_value=False):
            assert dispatch_admission_denied_reason(session_id="s-4129") is None
        assert InteractiveDispatch.objects.count() == 0


class TestSeatRelease(TestCase):
    """A terminating sub-agent hands its seat back — the window is only the backstop."""

    def test_a_released_seat_re_opens_the_lane(self) -> None:
        with _signals(), patch.object(gate_mod, "decide_admission", return_value=_admits(ceiling=1)):
            assert dispatch_admission_denied_reason(session_id="s-4129") is None
            assert release_interactive_dispatch(session_id="s-4129", agent_id="a-1") is True
            assert dispatch_admission_denied_reason(session_id="s-4129") is None

    def test_a_re_fired_stop_releases_at_most_one_seat(self) -> None:
        with _signals(), patch.object(gate_mod, "decide_admission", return_value=_admits(ceiling=2)):
            dispatch_admission_denied_reason(session_id="s-4129")
            dispatch_admission_denied_reason(session_id="s-4129")
        assert release_interactive_dispatch(session_id="s-4129", agent_id="a-1") is True
        assert release_interactive_dispatch(session_id="s-4129", agent_id="a-1") is False
        assert InteractiveDispatch.objects.live_seats().count() == 1

    def test_a_release_with_no_seat_to_give_back_is_a_no_op(self) -> None:
        assert release_interactive_dispatch(session_id="s-4129", agent_id="a-1") is False


class TestLiveAgentCount(TestCase):
    """The count the ceiling is compared against — the TOTAL live agent population."""

    def _claimed(self) -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        return Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.CLAIMED,
            lease_expires_at=timezone.now() + dt.timedelta(minutes=10),
            phase="architectural_review",
        )

    def test_counts_every_live_claim(self) -> None:
        self._claimed()
        self._claimed()
        assert gate_mod.live_agent_count() == 2

    def test_an_expired_lease_is_not_live(self) -> None:
        task = self._claimed()
        task.lease_expires_at = timezone.now() - dt.timedelta(minutes=1)
        task.save(update_fields=["lease_expires_at"])
        assert gate_mod.live_agent_count() == 0


class TestNoDoubleCountForALoopClaimedTask(TestCase):
    """Review of #4285/#4129: one Task claim + its own FIRST dispatch is one agent.

    A ``t3 loop claim-next`` claim and the SAME unit's own ``Agent``-tool dispatch
    name one live sub-agent. Before the fix, ``other_agents`` for ``claim_seat`` was
    ``_claimed_agent_count()`` taken with no regard for the calling session's own
    already-CLAIMED ``Task`` — so a loop tick's single dispatch was refused by the
    very claim it exists to service, and ``live_agent_count()`` kept double-counting
    for the sub-agent's whole run once seated.
    """

    def _claimed_interactive(self, *, session_id: str) -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        return Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.CLAIMED,
            claimed_by_session=session_id,
            lease_expires_at=timezone.now() + dt.timedelta(minutes=10),
            phase="architectural_review",
        )

    def test_the_claiming_sessions_own_dispatch_is_not_refused_by_its_own_claim(self) -> None:
        # The Task claim IS the population unit this dispatch represents — not a
        # second, distinct agent already occupying the one-seat ceiling.
        self._claimed_interactive(session_id="s-loop")
        with _signals(), patch.object(gate_mod, "decide_admission", return_value=_admits(ceiling=1)):
            assert dispatch_admission_denied_reason(session_id="s-loop") is None

    def test_live_agent_count_does_not_double_count_once_seated(self) -> None:
        self._claimed_interactive(session_id="s-loop")
        with _signals(), patch.object(gate_mod, "decide_admission", return_value=_admits(ceiling=1)):
            assert dispatch_admission_denied_reason(session_id="s-loop") is None
        assert gate_mod.live_agent_count() == 1

    def test_a_different_sessions_claim_still_counts(self) -> None:
        # Only the CALLING session's own claim is exempt — a distinct session's
        # claimed-but-not-yet-dispatched unit is real population and still counts.
        self._claimed_interactive(session_id="s-other")
        with _signals(), patch.object(gate_mod, "decide_admission", return_value=_admits(ceiling=1)):
            reason = dispatch_admission_denied_reason(session_id="s-loop")
        assert reason is not None

    def test_four_claims_and_one_seat_report_four_not_five(self) -> None:
        # A session that raced ahead and claimed 4 units before dispatching any of
        # them is 4 live agents once one is seated — the seat is one of the four
        # claims made concrete, never a fifth agent stacked on top of them.
        for _ in range(4):
            self._claimed_interactive(session_id="s-loop")
        assert gate_mod.live_agent_count() == 4
        with _signals(), patch.object(gate_mod, "decide_admission", return_value=_admits(ceiling=99)):
            assert dispatch_admission_denied_reason(session_id="s-loop") is None
        assert gate_mod.live_agent_count() == 4

    def test_only_the_first_of_several_claims_exempts_a_dispatch(self) -> None:
        # The exemption is ONE-SHOT per session: a second dispatch gets no credit
        # from the session's OTHER still-unseated claims, or a burst of cheap
        # claims could buy a burst of dispatches past the ceiling.
        for _ in range(4):
            self._claimed_interactive(session_id="s-loop")
        with _signals(), patch.object(gate_mod, "decide_admission", return_value=_admits(ceiling=4)):
            first = dispatch_admission_denied_reason(session_id="s-loop")
            second = dispatch_admission_denied_reason(session_id="s-loop")
        assert first is None
        assert second is not None

    def test_a_burst_of_ten_dispatches_admits_exactly_the_first_seats_worth(self) -> None:
        # #4285 review finding 2: ceiling=4 with 4 claims refuses the second dispatch
        # either way the one-shot exemption is coded, so it does not discriminate
        # 71adc657 from the blanket per-session exemption it replaced. One claim,
        # ceiling 5, 10 dispatches for the SAME session: a blanket exemption would
        # drop this session from `other_agents` on every call and admit 5 (rank
        # alone bounds it); the one-shot exemption spends its credit on the first
        # call only, so `other_agents` reverts to 1 from the second call on and
        # admission tracks the growing seat rank — exactly 4, not 5.
        self._claimed_interactive(session_id="s-burst")
        with _signals(), patch.object(gate_mod, "decide_admission", return_value=_admits(ceiling=5)):
            verdicts = [dispatch_admission_denied_reason(session_id="s-burst") for _ in range(10)]
        assert sum(verdict is None for verdict in verdicts) == 4, verdicts

    def test_the_denial_message_never_understates_its_own_ceiling(self) -> None:
        # #4285 review finding 1: the un-deduped CHECK can refuse a dispatch while
        # the deduped REPORT (live_agent_count) is still under the ceiling — an
        # accepted, conservative trade-off (finding 3). But the denial message
        # must never claim a population "at/over" a ceiling it is actually under.
        self._claimed_interactive(session_id="s-other")
        with _signals(), patch.object(gate_mod, "decide_admission", return_value=_admits(ceiling=2)):
            assert dispatch_admission_denied_reason(session_id="s-other") is None
            reason = dispatch_admission_denied_reason(session_id="s-adhoc")
        assert gate_mod.live_agent_count() == 1  # genuinely one live agent by the deduped report
        assert reason is not None  # still refused under the conservative check (finding 3, accepted)
        reported = int(reason.split()[2])
        assert reported >= 2, f"message claims {reported} at/over ceiling 2, which is false: {reason}"
