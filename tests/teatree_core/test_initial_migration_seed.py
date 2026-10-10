"""The seed rows the squashed core migration carries, pinned against the shipped seed.

The core history was squashed into ONE initial migration (named on disk; read through
``core_initial_migration()`` so no test pins the name). Its last operation is a
``RunPython`` that seeds exactly the rows a fresh history DB used to end with: the
default loops, the ``arch_review`` prompt and the ``off`` preset. They are LITERALS
(a migration is frozen history and imports nothing from teatree), dumped from a fresh
history DB of the pre-squash head, so :class:`TestSeedLiteralsMatchTheShippedSeed`
keeps them in lock-step with :mod:`teatree.loops.seed` (the install-time seed ``t3
setup`` runs), and :class:`FreshMigrateSeedsDefaultLoops` proves a migrate from
``zero`` lands them.
"""

import importlib

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase

from teatree.core.models import Loop, Mode, Prompt
from teatree.loops.seed import ARCH_REVIEW_PROMPT_BODY, DEFAULT_LOOPS, script_entry_point_for
from tests.teatree_core._migration_graph import core_initial_migration

_migration = importlib.import_module(f"teatree.core.migrations.{core_initial_migration()}")
_dispatch_rewrite = importlib.import_module("teatree.core.migrations.0009_dispatch_loop_description")
_LOOP_ROWS = {row["name"]: row for row in _migration._LOOP_ROWS}
_COLLEAGUE_FACING = frozenset(spec.name for spec in DEFAULT_LOOPS if spec.colleague_facing)


class TestSeedLiteralsMatchTheShippedSeed:
    """The migration's literals must not drift from ``teatree.loops.seed``."""

    def test_the_loop_rows_are_the_default_loops_in_order(self) -> None:
        assert [row["name"] for row in _migration._LOOP_ROWS] == [spec.name for spec in DEFAULT_LOOPS]

    def test_each_loop_row_carries_its_shipped_spec(self) -> None:
        for spec in DEFAULT_LOOPS:
            row = _LOOP_ROWS[spec.name]
            as_seeded = _dispatch_rewrite._OLD if spec.description == _dispatch_rewrite._NEW else spec.description
            assert (row["delay_seconds"], row["daily_at"], row["description"], row["colleague_facing"]) == (
                spec.delay_seconds,
                spec.daily_at,
                as_seeded,
                spec.colleague_facing,
            ), spec.name
            if spec.is_prompt_backed:
                assert (row["script"], row["prompt"]) == ("", spec.name)
            else:
                assert (row["script"], row["prompt"]) == (script_entry_point_for(spec.name), None)

    def test_no_loop_row_carries_a_manual_override(self) -> None:
        # ``Loop.enabled`` is the MANUAL override; whether a loop runs is the preset's answer.
        assert {row["enabled"] for row in _migration._LOOP_ROWS} == {None}

    def test_the_prompt_row_is_the_arch_review_body(self) -> None:
        (prompt,) = _migration._PROMPT_ROWS
        assert prompt["name"] == "arch_review"
        assert prompt["body"] == ARCH_REVIEW_PROMPT_BODY
        assert prompt["description"] == _LOOP_ROWS["arch_review"]["description"]

    def test_the_off_preset_switches_every_default_loop_off(self) -> None:
        (off,) = _migration._MODE_ROWS
        assert off["name"] == "off"
        assert off["entries"] == dict.fromkeys(sorted(spec.name for spec in DEFAULT_LOOPS), False)


# ``setUp`` reverse-migrates ``core`` to ``zero`` then re-applies it on the shared
# ``default`` connection; scoped bump over the global 60s hang-detector (#1189).
@pytest.mark.timeout(480)
class FreshMigrateSeedsDefaultLoops(TransactionTestCase):
    """A migrate from ``zero`` runs the seed ``RunPython`` and lands the rows."""

    def setUp(self) -> None:
        call_command("migrate", "core", "zero", "--no-input", verbosity=0)
        self.addCleanup(call_command, "migrate", "core", "--no-input", verbosity=0)
        call_command("migrate", "core", "--no-input", verbosity=0)

    def test_seeds_every_default_loop_once(self) -> None:
        assert sorted(Loop.objects.values_list("name", flat=True)) == sorted(spec.name for spec in DEFAULT_LOOPS)

    def test_a_migrated_box_carries_no_manual_override_on_any_loop(self) -> None:
        assert not Loop.objects.exclude(enabled=None).exists()

    def test_seeds_colleague_facing_on_exactly_the_colleague_loops(self) -> None:
        facing = set(Loop.objects.filter(colleague_facing=True).values_list("name", flat=True))
        assert facing == _COLLEAGUE_FACING

    def test_seeds_each_loop_description_from_the_canonical_seed(self) -> None:
        by_name = dict(Loop.objects.values_list("name", "description"))
        assert by_name == {spec.name: spec.description for spec in DEFAULT_LOOPS}
        assert all(by_name.values())

    def test_slack_answer_is_not_seeded(self) -> None:
        assert not Loop.objects.filter(name="slack_answer").exists()

    def test_each_script_loop_points_at_its_own_module(self) -> None:
        for spec in DEFAULT_LOOPS:
            if spec.is_prompt_backed:
                continue
            row = Loop.objects.get(name=spec.name)
            assert row.script == script_entry_point_for(spec.name)
            assert row.prompt_id is None

    def test_arch_review_is_prompt_backed_with_the_review_skill_body(self) -> None:
        arch = Loop.objects.select_related("prompt").get(name="arch_review")
        assert arch.script == ""
        assert arch.prompt.body == ARCH_REVIEW_PROMPT_BODY
        assert arch.prompt.description == arch.description

    def test_the_off_preset_is_seeded(self) -> None:
        assert list(Mode.objects.values_list("name", flat=True)) == ["off"]

    def test_the_seed_is_idempotent(self) -> None:
        before = (Loop.objects.count(), Prompt.objects.count(), Mode.objects.count())
        _migration._seed_defaults(apps, connection.schema_editor())
        assert (Loop.objects.count(), Prompt.objects.count(), Mode.objects.count()) == before
