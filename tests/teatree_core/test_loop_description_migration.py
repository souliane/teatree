"""Backfill of ``Loop.description`` onto existing rows via data migration.

The canonical descriptions live in :data:`teatree.loops.seed.DEFAULT_LOOPS`; the
install-seed populates fresh rows, but rows seeded by an earlier install carry a
blank ``description``. Migration ``0009_seed_loop_descriptions`` backfills those.
A migration is frozen history and must not import the evolving seed module, so the
descriptions are INLINED there; :class:`TestInlinedDescriptionsMatchCanonicalSeed`
pins the inlined map against the canonical seed so the two cannot drift, and
:class:`TestMigratedDbHasDescriptions` proves the full migration chain lands a real
description on every default loop row.
"""

import importlib

import django.test

from teatree.core.models import Loop
from teatree.loops.seed import DEFAULT_LOOPS

_migration = importlib.import_module("teatree.core.migrations.0009_seed_loop_descriptions")


class TestInlinedDescriptionsMatchCanonicalSeed:
    def test_inlined_descriptions_match_the_canonical_default_loops(self) -> None:
        expected = {spec.name: spec.description for spec in DEFAULT_LOOPS}
        assert expected == _migration._LOOP_DESCRIPTIONS


@django.test.override_settings(USE_TZ=True)
class TestMigratedDbHasDescriptions(django.test.TestCase):
    def test_every_default_loop_row_has_a_real_description(self) -> None:
        # The migration chain (0001 seeds blank, 0009 backfills) lands a real,
        # non-placeholder description on every default loop row.
        for spec in DEFAULT_LOOPS:
            row = Loop.objects.get(name=spec.name)
            assert row.description.strip(), spec.name
            assert "Default loop prompt for" not in row.description, spec.name
