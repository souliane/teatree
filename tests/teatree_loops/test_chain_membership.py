"""teatree.loops.chain_membership — which loops carry a chain, and which are starved (#4185).

``Loop.enabled`` is the LOWEST-precedence input to the enable verdict (hold > forced >
preset > column), so an active preset holding an opinion decides the loop and the column
is never reached. Membership built from that column alone left preset-admitted loops with
no timer row of any status, ever — admitted, reported healthy, and with nothing to drive
them. Integration-first against the real DB + ``django_tasks_db`` backend.
"""

import django.test
from django.utils import timezone
from django_tasks.base import TaskResultStatus
from django_tasks_db.models import DBTaskResult

from teatree.core.models import Loop, Mode, ModeOverride
from teatree.loops import chain_membership, timer_chains, timer_reconciler
from teatree.loops.preset_status import effective_verdicts
from teatree.loops.registry import iter_loops

_DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops"]}}
#: A preset of the test's own, so nothing here depends on the seeded production modes.
_PRESET = "forced-on-4185"


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestChainMembership(django.test.TestCase):
    """``inbox``: a registered live-tick loop, column OFF, forced ON by the active preset."""

    def setUp(self) -> None:
        Loop.objects.all().delete()
        DBTaskResult.objects.all().delete()
        Loop.objects.create(
            name="inbox",
            script="src/teatree/loops/inbox/loop.py",
            delay_seconds=60,
            enabled=False,
        )
        Mode.objects.create(name=_PRESET, entries={"inbox": True})
        ModeOverride.objects.set_override(_PRESET)

    def test_a_preset_forced_on_column_disabled_loop_is_a_member(self) -> None:
        assert "inbox" in chain_membership.timer_chain_loop_names()

    def test_membership_is_the_registered_live_tick_set_intersected_with_the_verdict(self) -> None:
        # The drift pin: a future edit cannot reintroduce a tier-subset read of the
        # enable planes without this equality failing.
        registered = {loop.name for loop in iter_loops() if not loop.off_live_tick}
        admitted = {verdict.name for verdict in effective_verdicts() if verdict.admitted}
        assert chain_membership.timer_chain_loop_names() == registered & admitted


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestStarvedLoopNames(django.test.TestCase):
    """Admitted with no driver — the state the eight starved loops should have rendered."""

    def setUp(self) -> None:
        Loop.objects.all().delete()
        DBTaskResult.objects.all().delete()
        Loop.objects.create(
            name="inbox",
            script="src/teatree/loops/inbox/loop.py",
            delay_seconds=60,
            enabled=False,
        )
        Mode.objects.create(name=_PRESET, entries={"inbox": True})
        ModeOverride.objects.set_override(_PRESET)

    def test_an_admitted_loop_with_no_timer_row_is_starved(self) -> None:
        assert chain_membership.starved_loop_names() == {"inbox"}

    def test_the_reconciler_clears_starvation(self) -> None:
        timer_reconciler.ensure_loop_timers()
        assert chain_membership.starved_loop_names() == set()

    def test_a_running_timer_also_counts_as_a_driver(self) -> None:
        timer_chains.enqueue_loop_timer("inbox", run_after=timezone.now())
        [timer] = timer_chains.pending_loop_timers("inbox")
        DBTaskResult.objects.filter(id=timer.id).update(status=TaskResultStatus.RUNNING, started_at=timezone.now())
        assert chain_membership.starved_loop_names() == set()

    def test_a_masked_off_loop_is_not_starved(self) -> None:
        # Starvation is "admitted with no driver"; a loop the preset masks OFF is not
        # admitted, so having no chain is exactly what was asked for, not a fault.
        Mode.objects.filter(name=_PRESET).update(entries={"inbox": False})
        assert chain_membership.starved_loop_names() == set()
