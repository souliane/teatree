"""Upgrade-path seed of the ``directive_loop`` Loop row (north-star PR-7).

A fresh migrate seeds ``directive_loop`` via ``0001_initial._seed_default_loops`` (the
inlined-snapshot parity tests pin that), but an install that already ran ``0001`` before
the loop existed has no row. ``0035_seed_directive_loop`` idempotently ``get_or_create``s
it PAUSED. :class:`TestUpgradePathSeedsDirectiveLoop` proves the RunPython recreates a
missing row disabled (anti-vacuous: the row is deleted first), and
:class:`TestMigratedDbHasDirectiveLoop` proves the full chain lands it on the migrated DB.
"""

import importlib

import django.test
from django.apps import apps

from teatree.core.models import Loop

_migration = importlib.import_module("teatree.core.migrations.0035_seed_directive_loop")


@django.test.override_settings(USE_TZ=True)
class TestMigratedDbHasDirectiveLoop(django.test.TestCase):
    def test_directive_loop_row_exists_paused_and_script_backed(self) -> None:
        row = Loop.objects.get(name="directive_loop")
        assert row.enabled is False  # QUADRUPLE-OFF layer 2
        assert row.colleague_facing is False
        assert row.description.strip()
        assert row.script == "src/teatree/loops/directive_loop/loop.py"
        assert row.prompt_id is None


@django.test.override_settings(USE_TZ=True)
class TestUpgradePathSeedsDirectiveLoop(django.test.TestCase):
    def test_run_python_recreates_a_missing_row_disabled(self) -> None:
        # Anti-vacuous: delete the row (simulate a DB migrated before the loop
        # existed), re-run the seed, and confirm it is recreated PAUSED.
        Loop.objects.filter(name="directive_loop").delete()
        _migration._seed_directive_loop(apps, None)
        row = Loop.objects.get(name="directive_loop")
        assert row.enabled is False

    def test_run_python_is_idempotent_and_non_clobbering(self) -> None:
        Loop.objects.filter(name="directive_loop").update(enabled=True)
        _migration._seed_directive_loop(apps, None)
        # get_or_create never clobbers an operator-enabled row.
        assert Loop.objects.get(name="directive_loop").enabled is True
