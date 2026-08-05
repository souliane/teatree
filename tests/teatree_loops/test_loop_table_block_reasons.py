"""A loop the loop-table refuses states WHY — it never reports as a quiet run (#3843).

``build_loop_table_jobs`` returned a bare job list, so every refusal — a durable
``LoopState`` hold, an emergency force-OFF, a not-due cadence, a preset mask, a
colleague-facing row under an away mode — collapsed into the same empty list the
caller then rendered as ``ran loop 'X' — 0 signal(s), 0 action(s)``. That is how
the ``review`` loop sat force-OFF for hours while every tick printed success, and
why the outage was first diagnosed as a missing own-PR review arm rather than as
a control-plane hold.

:func:`teatree.loops.loop_table.dispatch_loop_table` is the reasoned form: one
:class:`LoopDispatch` per considered loop, carrying either its jobs or a
one-line, operator-actionable ``blocked_reason``.
"""

import datetime as dt
from unittest.mock import patch

import django.test
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from teatree.core.mode_resolution import ResolvedMode
from teatree.core.models import Loop, LoopState, Mode, Prompt
from teatree.loops.base import MiniLoop
from teatree.loops.loop_table import build_loop_table_jobs, dispatch_loop_table

_MODE_SEAM = "teatree.loops.enable_verdict.resolve_active_mode"


def _resolved(*, defers: bool = False, name: str = "engaged") -> ResolvedMode:
    """A ResolvedMode carrying only the availability posture (no loop-mask opinion)."""
    return ResolvedMode(mode=Mode(name=name, entries={}, defers_questions=defers), source="override", until=None)


def _mini(name: str) -> MiniLoop:
    return MiniLoop(name=name, default_cadence_seconds=60, build_jobs=lambda n=name, **_: [f"job-{n}"])


def _prompt(name: str = "block-reason-prompt") -> Prompt:
    prompt, _ = Prompt.objects.get_or_create(name=name, defaults={"body": "do x"})
    return prompt


def _reason(name: str, *, now: dt.datetime, resolved: ResolvedMode | None = None) -> str:
    """The ``blocked_reason`` a single-loop-scoped pass records for *name*."""
    with (
        patch("teatree.loops.loop_table.iter_loops", return_value=(_mini(name),)),
        patch(_MODE_SEAM, return_value=resolved or _resolved()),
    ):
        outcomes = dispatch_loop_table({}, now=now, only=name)
    assert len(outcomes) == 1, outcomes
    return outcomes[0].blocked_reason


@django.test.override_settings(USE_TZ=True)
class TestBlockedLoopsStateTheirReason(django.test.TestCase):
    def test_a_force_off_loop_says_it_is_forced_off(self) -> None:
        """The exact shape that hid the review outage: enabled, due — and force-skipped."""
        now = timezone.now()
        Loop.objects.create(name="br-forced", delay_seconds=60, prompt=_prompt())
        LoopState.objects.override("br-forced", on=False, reason="holding until the variable is corrected")

        reason = _reason("br-forced", now=now)

        assert "forced" in reason.lower(), reason
        assert "br-forced" in reason, reason

    def test_a_held_loop_says_it_is_held(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="br-held", delay_seconds=60, prompt=_prompt())
        LoopState.objects.pause("br-held")

        reason = _reason("br-held", now=now)

        assert "held" in reason.lower(), reason

    def test_a_not_due_loop_says_it_is_not_due(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="br-cooling", delay_seconds=60, prompt=_prompt(), last_run_at=now)

        assert "not due" in _reason("br-cooling", now=now).lower()

    def test_a_disabled_loop_says_it_is_disabled(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="br-off", delay_seconds=60, prompt=_prompt(), enabled=False)

        assert "disabled" in _reason("br-off", now=now).lower()

    def test_a_colleague_facing_loop_under_an_away_mode_says_so(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="br-colleague", delay_seconds=60, prompt=_prompt(), colleague_facing=True)

        reason = _reason("br-colleague", now=now, resolved=_resolved(defers=True, name="autonomous_away"))

        assert "colleague" in reason.lower(), reason

    def test_a_loop_with_no_row_says_its_config_was_never_seeded(self) -> None:
        assert "no Loop row" in _reason("br-orphan", now=timezone.now())

    def test_a_dispatched_loop_carries_its_jobs_and_no_reason(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="br-ok", delay_seconds=60, prompt=_prompt())
        with (
            patch("teatree.loops.loop_table.iter_loops", return_value=(_mini("br-ok"),)),
            patch(_MODE_SEAM, return_value=_resolved()),
        ):
            outcomes = dispatch_loop_table({}, now=now, only="br-ok")

        assert outcomes[0].blocked_reason == ""
        assert outcomes[0].dispatched
        assert outcomes[0].jobs == ("job-br-ok",)

    def test_blocked_and_quiet_are_distinguishable(self) -> None:
        """The whole point: an empty SIGNAL list alone cannot tell the two apart."""
        now = timezone.now()
        Loop.objects.create(name="br-quiet", delay_seconds=60, prompt=_prompt())
        Loop.objects.create(name="br-blocked", delay_seconds=60, prompt=_prompt())
        LoopState.objects.override("br-blocked", on=False, reason="emergency")

        with (
            patch("teatree.loops.loop_table.iter_loops", return_value=(_mini("br-quiet"), _mini("br-blocked"))),
            patch(_MODE_SEAM, return_value=_resolved()),
        ):
            outcomes = {outcome.name: outcome for outcome in dispatch_loop_table({}, now=now)}

        assert outcomes["br-blocked"].jobs == ()
        assert outcomes["br-quiet"].dispatched, "a loop whose scanners ran and found nothing is NOT blocked"
        assert not outcomes["br-blocked"].dispatched, "a force-skipped loop must not read as a quiet run"

    def test_a_loop_that_built_no_scanner_says_so(self) -> None:
        """The gate one level BELOW the loop gate: admitted, anchor claimed, zero scanners.

        Every scanner factory declining (an off feature flag, an absent backend) yields
        the same empty job list a healthy quiet tick yields, so the loop reported ``ran
        … 0 signal(s)`` while scanning nothing at all — and ``last_run_at`` advanced, so
        every staleness surface read green. That is a refusal too, and it must say so.
        """
        now = timezone.now()
        Loop.objects.create(name="br-inert", delay_seconds=60, prompt=_prompt())
        inert = MiniLoop(name="br-inert", default_cadence_seconds=60, build_jobs=lambda **_: [])

        with (
            patch("teatree.loops.loop_table.iter_loops", return_value=(inert,)),
            patch(_MODE_SEAM, return_value=_resolved()),
        ):
            outcomes = dispatch_loop_table({}, now=now, only="br-inert")

        assert not outcomes[0].dispatched, "a loop that built no scanner must not read as a quiet run"
        assert "no scanner" in outcomes[0].blocked_reason, outcomes[0].blocked_reason


@django.test.override_settings(USE_TZ=True)
class TestReasonedPassKeepsTheExistingContracts(django.test.TestCase):
    """The reasoned pass is the same fan-out — job list, cadence CAS and one bulk read."""

    def test_build_loop_table_jobs_still_returns_the_flat_job_list(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="bc-flat", delay_seconds=60, prompt=_prompt())
        with (
            patch("teatree.loops.loop_table.iter_loops", return_value=(_mini("bc-flat"),)),
            patch(_MODE_SEAM, return_value=_resolved()),
        ):
            jobs = build_loop_table_jobs({}, now=now, only="bc-flat")

        assert jobs == ["job-bc-flat"]

    def test_a_blocked_loop_keeps_its_cadence_anchor(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="bc-anchor", delay_seconds=60, prompt=_prompt())
        LoopState.objects.override("bc-anchor", on=False, reason="emergency")

        _reason("bc-anchor", now=now)

        assert Loop.objects.get(name="bc-anchor").last_run_at is None

    def test_the_reason_costs_no_extra_loop_state_query(self) -> None:
        """#2584's single bulk control-plane read survives — the reason is pure over it."""
        now = timezone.now()
        registry = tuple(_mini(f"bc-n{i}") for i in range(5))
        for loop in registry:
            Loop.objects.create(name=loop.name, delay_seconds=60, prompt=_prompt())
        LoopState.objects.override("bc-n0", on=False, reason="emergency")
        LoopState.objects.pause("bc-n1")

        with (
            patch("teatree.loops.loop_table.iter_loops", return_value=registry),
            patch(_MODE_SEAM, return_value=_resolved()),
            CaptureQueriesContext(connection) as ctx,
        ):
            dispatch_loop_table({}, now=now)

        assert sum("teatree_loop_state" in query["sql"] for query in ctx.captured_queries) <= 1
