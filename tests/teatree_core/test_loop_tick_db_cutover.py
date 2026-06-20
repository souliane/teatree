"""The live ``t3 loop tick`` is cut over to the DB ``Loop`` table (#2513, D1).

After the #1796 cutover the LIVE fat tick (``loop_tick`` management command) selects
its scanner jobs from the ``Loop`` table via ``build_loop_table_jobs`` — NOT from the
retired code-cadence path gated by ``LoopsConfig``/``MiniLoopMarker``/
``elapsed_and_enabled``. These tests pin that the live builder routes through the Loop
table and that the retired code-cadence gate no longer suppresses an enabled Loop row.
"""

import datetime as dt
from unittest.mock import patch

import django.test
from django.utils import timezone

from teatree.core.management.commands.loop_tick import _registry_jobs_builder
from teatree.core.models import Loop, Prompt
from teatree.loop.tick import TickRequest
from teatree.loops.base import MiniLoop
from teatree.loops.master import build_loop_table_jobs


def _mini(name: str) -> MiniLoop:
    return MiniLoop(name=name, default_cadence_seconds=60, build_jobs=lambda n=name, **_: [f"job-{n}"])


def _prompt() -> Prompt:
    prompt, _ = Prompt.objects.get_or_create(name="demo-cutover", defaults={"body": "x"})
    return prompt


@django.test.override_settings(USE_TZ=True)
class TestLiveTickReadsLoopTable(django.test.TestCase):
    def test_live_builder_selects_enabled_due_rows_from_the_loop_table(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="ct-on", delay_seconds=60, prompt=_prompt())  # never run -> due
        Loop.objects.create(name="ct-off", delay_seconds=60, prompt=_prompt(), enabled=False)
        request = TickRequest()
        with patch("teatree.loops.master.iter_loops", return_value=(_mini("ct-on"), _mini("ct-off"))):
            jobs = _registry_jobs_builder(request, now)
        assert "job-ct-on" in jobs
        assert "job-ct-off" not in jobs

    def test_live_builder_routes_through_the_loop_table_builder(self) -> None:
        # The cutover routes the live tick through ``build_loop_table_jobs``
        # (the DB master runner), not a code-cadence gate.
        now = timezone.now()
        Loop.objects.create(name="ct-only", delay_seconds=60, prompt=_prompt())
        request = TickRequest()
        with (
            patch("teatree.loops.master.iter_loops", return_value=(_mini("ct-only"),)),
            patch(
                "teatree.loops.master.build_loop_table_jobs",
                wraps=build_loop_table_jobs,
            ) as canonical,
        ):
            jobs = _registry_jobs_builder(request, now)
        canonical.assert_called_once()
        assert "job-ct-only" in jobs

    def test_live_builder_does_not_consult_the_legacy_cadence_gate(self) -> None:
        # The retired ``LoopsConfig``/``MiniLoopMarker``/``elapsed_and_enabled``
        # code-cadence gate must NOT be consulted on the live path — the Loop
        # row is the single source of truth.
        now = timezone.now()
        Loop.objects.create(name="ct-legacy", delay_seconds=60, prompt=_prompt())
        request = TickRequest()
        with (
            patch("teatree.loops.master.iter_loops", return_value=(_mini("ct-legacy"),)),
            patch("teatree.loops.gating.elapsed_and_enabled") as legacy_gate,
            patch("teatree.loops.cadence_ledger.MiniLoopMarker.objects.mark_fired") as legacy_marker,
        ):
            jobs = _registry_jobs_builder(request, now)
        legacy_gate.assert_not_called()
        legacy_marker.assert_not_called()
        assert "job-ct-legacy" in jobs

    def test_live_builder_bumps_last_run_for_dispatched_row(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="ct-bump", delay_seconds=60, prompt=_prompt())
        with patch("teatree.loops.master.iter_loops", return_value=(_mini("ct-bump"),)):
            _registry_jobs_builder(TickRequest(), now)
        assert Loop.objects.get(name="ct-bump").last_run_at == now

    def test_cooling_row_is_skipped_by_its_own_cadence(self) -> None:
        now = timezone.now()
        Loop.objects.create(
            name="ct-cool", delay_seconds=600, prompt=_prompt(), last_run_at=now - dt.timedelta(seconds=10)
        )
        with patch("teatree.loops.master.iter_loops", return_value=(_mini("ct-cool"),)):
            jobs = _registry_jobs_builder(TickRequest(), now)
        assert jobs == []


@django.test.override_settings(USE_TZ=True)
class TestMasterTickPreservesOperationalPause(django.test.TestCase):
    """The unified gate (#2584) preserves the migration-0087 operational pause.

    Migration 0087 disabled every default ``Loop`` row (``enabled=False``) for
    the #2513 cutover. The unification must NOT un-pause anything: with the rows
    disabled the master tick produces zero jobs, exactly as today. This pins the
    pause through the ``loops_tick`` master builder (``_loop_table_jobs_builder``)
    so a future change to the unified verdict cannot silently re-arm the loops.
    No live tick is run here — only the pure job-builder is invoked.
    """

    def test_master_builder_emits_zero_jobs_when_all_rows_disabled(self) -> None:
        from teatree.core.management.commands.loops_tick import _loop_table_jobs_builder  # noqa: PLC0415

        now = timezone.now()
        # Simulate the migration-0087 state: enabled+due registry loops, but
        # every Loop row disabled. Zero jobs must fan out.
        for name in ("p-a", "p-b", "p-c"):
            Loop.objects.create(name=name, delay_seconds=60, prompt=_prompt(), enabled=False)
        registry = (_mini("p-a"), _mini("p-b"), _mini("p-c"))
        with patch("teatree.loops.master.iter_loops", return_value=registry):
            jobs = _loop_table_jobs_builder(TickRequest(), now)
        assert jobs == []
        for name in ("p-a", "p-b", "p-c"):
            assert Loop.objects.get(name=name).last_run_at is None
