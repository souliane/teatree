"""The live per-loop tick is cut over to the DB ``Loop`` table (#2513, D1).

After the #1796 cutover the live tick (``loops_tick`` management command) selects
its scanner jobs from the ``Loop`` table via ``build_loop_table_jobs`` — NOT from a
code-cadence ledger. LOOP-PR-A then DELETED that retired ledger + gate entirely.
These tests pin that the builder routes through the Loop table and that the
retired code-cadence modules are gone (so they can never be consulted again). The
gate is the same whether ``build_loop_table_jobs`` scans every enabled+due row or
is scoped to one via ``only`` (#2650) — a per-loop tick honours it symmetrically.
"""

import datetime as dt
import importlib

import django.test
import pytest
from django.utils import timezone

from teatree.core.models import Loop, Prompt
from teatree.loops.base import MiniLoop
from teatree.loops.master import build_loop_table_jobs


def _mini(name: str) -> MiniLoop:
    return MiniLoop(name=name, default_cadence_seconds=60, build_jobs=lambda n=name, **_: [f"job-{n}"])


def _prompt() -> Prompt:
    prompt, _ = Prompt.objects.get_or_create(name="demo-cutover", defaults={"body": "x"})
    return prompt


@django.test.override_settings(USE_TZ=True)
class TestLiveTickReadsLoopTable(django.test.TestCase):
    def test_builder_selects_enabled_due_rows_from_the_loop_table(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="ct-on", delay_seconds=60, prompt=_prompt())  # never run -> due
        Loop.objects.create(name="ct-off", delay_seconds=60, prompt=_prompt(), enabled=False)
        from unittest.mock import patch  # noqa: PLC0415

        with patch("teatree.loops.master.iter_loops", return_value=(_mini("ct-on"), _mini("ct-off"))):
            jobs = build_loop_table_jobs({}, now=now)
        assert "job-ct-on" in jobs
        assert "job-ct-off" not in jobs

    def test_per_loop_scope_admits_only_the_named_row(self) -> None:
        # The per-loop tick (#2650) scopes the SAME gate to one row via ``only``.
        now = timezone.now()
        Loop.objects.create(name="ct-a", delay_seconds=60, prompt=_prompt())
        Loop.objects.create(name="ct-b", delay_seconds=60, prompt=_prompt())
        from unittest.mock import patch  # noqa: PLC0415

        with patch("teatree.loops.master.iter_loops", return_value=(_mini("ct-a"), _mini("ct-b"))):
            jobs = build_loop_table_jobs({}, now=now, only="ct-a")
        assert jobs == ["job-ct-a"]

    def test_legacy_code_cadence_modules_are_deleted(self) -> None:
        # LOOP-PR-A removed the retired code-cadence ledger + its gate + the
        # duplicate tick engine. They no longer exist, so the live path can never
        # consult them again — the Loop row is the single source of truth.
        for mod in ("teatree.loops.gating", "teatree.loops.cadence_ledger", "teatree.loops.orchestrator"):
            with pytest.raises(ModuleNotFoundError):
                importlib.import_module(mod)

    def test_builder_bumps_last_run_for_dispatched_row(self) -> None:
        now = timezone.now()
        Loop.objects.create(name="ct-bump", delay_seconds=60, prompt=_prompt())
        from unittest.mock import patch  # noqa: PLC0415

        with patch("teatree.loops.master.iter_loops", return_value=(_mini("ct-bump"),)):
            build_loop_table_jobs({}, now=now)
        assert Loop.objects.get(name="ct-bump").last_run_at == now

    def test_cooling_row_is_skipped_by_its_own_cadence(self) -> None:
        now = timezone.now()
        Loop.objects.create(
            name="ct-cool", delay_seconds=600, prompt=_prompt(), last_run_at=now - dt.timedelta(seconds=10)
        )
        from unittest.mock import patch  # noqa: PLC0415

        with patch("teatree.loops.master.iter_loops", return_value=(_mini("ct-cool"),)):
            jobs = build_loop_table_jobs({}, now=now)
        assert jobs == []


@django.test.override_settings(USE_TZ=True)
class TestTickPreservesOperationalPause(django.test.TestCase):
    """The unified gate (#2584) preserves the migration-0087 operational pause.

    Migration 0087 disabled every default ``Loop`` row (``enabled=False``) for the
    #2513 cutover. The unification must NOT un-pause anything: with the rows
    disabled the builder produces zero jobs, exactly as today. This pins the pause
    through ``build_loop_table_jobs`` so a future change to the unified verdict
    cannot silently re-arm the loops. No live tick is run here — only the pure
    job-builder is invoked.
    """

    def test_builder_emits_zero_jobs_when_all_rows_disabled(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        now = timezone.now()
        # Simulate the migration-0087 state: enabled+due registry loops, but
        # every Loop row disabled. Zero jobs must fan out.
        for name in ("p-a", "p-b", "p-c"):
            Loop.objects.create(name=name, delay_seconds=60, prompt=_prompt(), enabled=False)
        registry = (_mini("p-a"), _mini("p-b"), _mini("p-c"))
        with patch("teatree.loops.master.iter_loops", return_value=registry):
            jobs = build_loop_table_jobs({}, now=now)
        assert jobs == []
        for name in ("p-a", "p-b", "p-c"):
            assert Loop.objects.get(name=name).last_run_at is None
