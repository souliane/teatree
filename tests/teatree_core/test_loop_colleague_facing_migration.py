"""Seed of ``Loop.colleague_facing`` onto the default colleague-facing loops (#2904).

The canonical colleague-facing set lives on :data:`teatree.loops.seed.DEFAULT_LOOPS`;
migration ``0016_loop_colleague_facing`` adds the column and seeds it on existing
rows (a genuinely new field, so every pre-migration row needs its first value, not
a backfill over an operator choice). A migration is frozen history and must not
import the evolving seed module, so the colleague-facing set is INLINED there;
:class:`TestInlinedSetMatchesCanonicalSeed` pins the inlined set against the
canonical seed so the two cannot drift, and :class:`TestMigratedDbHasColleagueFacing`
proves the full migration chain lands the right value on every default loop row.
"""

import importlib

import django.test

from teatree.core.models import Loop
from teatree.loops.seed import DEFAULT_LOOPS

_migration = importlib.import_module("teatree.core.migrations.0016_loop_colleague_facing")


class TestInlinedSetMatchesCanonicalSeed:
    def test_inlined_colleague_facing_set_matches_the_canonical_default_loops(self) -> None:
        expected = {spec.name for spec in DEFAULT_LOOPS if spec.colleague_facing}
        assert expected == _migration._COLLEAGUE_FACING_LOOPS


@django.test.override_settings(USE_TZ=True)
class TestMigratedDbHasColleagueFacing(django.test.TestCase):
    def test_every_default_loop_row_has_the_right_colleague_facing_value(self) -> None:
        # The migration chain (0001 seeds the row, 0016 adds + seeds the column)
        # lands the canonical value on every default loop row.
        for spec in DEFAULT_LOOPS:
            row = Loop.objects.get(name=spec.name)
            assert row.colleague_facing is spec.colleague_facing, spec.name

    def test_review_and_followup_are_colleague_facing(self) -> None:
        assert Loop.objects.get(name="review").colleague_facing is True
        assert Loop.objects.get(name="followup").colleague_facing is True

    def test_internal_loops_are_not_colleague_facing(self) -> None:
        assert Loop.objects.get(name="inbox").colleague_facing is False
        assert Loop.objects.get(name="dream").colleague_facing is False
