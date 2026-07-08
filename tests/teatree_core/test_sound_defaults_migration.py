"""``0043`` flips the sound operational-default loops ON (policy reversal of #2513).

``0043_enable_sound_default_loops`` reverses the "seeded paused" cutover for the
local/read-only operational core: it sets ``Loop.enabled`` True on the eight
sound-default loops, and ONLY on rows still carrying the untouched seed default
(``enabled=False`` with no ``paused``/``disabled`` ``LoopState`` hold) so an
operator's explicit disable/pause is never clobbered. The inlined ON-set is
pinned against the canonical ``teatree.loops.seed`` ``default_enabled`` specs, and
a full re-migrate from ``zero`` proves the fresh-DB landing lands exactly the 8 ON
and the other 15 OFF.
"""

import importlib

import django.test
import pytest
from django.apps import apps
from django.core.management import call_command
from django.test import TransactionTestCase

from teatree.core.models import Loop, LoopState
from teatree.loops.seed import DEFAULT_LOOPS

_migration = importlib.import_module("teatree.core.migrations.0043_enable_sound_default_loops")

_SOUND_ON = frozenset(s.name for s in DEFAULT_LOOPS if s.default_enabled)


class TestInlinedOnSetMatchesCanonicalSeed:
    """The migration's inlined ON-set must not drift from the canonical seed."""

    def test_inlined_on_set_matches_canonical_seed_default_enabled(self) -> None:
        # The migration is frozen history and inlines its own ON-set; this pins
        # it against the canonical ``default_enabled`` specs so the migrate-path
        # and the install-seed enable the same 8 loops. Set semantics: the
        # migration only uses the names in ``filter(name__in=...)``.
        assert set(_migration._SOUND_DEFAULT_ON) == _SOUND_ON
        assert len(_migration._SOUND_DEFAULT_ON) == len(set(_migration._SOUND_DEFAULT_ON)) == 8


@django.test.override_settings(USE_TZ=True)
class TestForwardRespectsOperatorIntent(django.test.TestCase):
    """The forward flip only touches rows still at the untouched seed default."""

    def test_forward_respects_an_explicit_operator_disable(self) -> None:
        Loop.objects.filter(name="dispatch").update(enabled=False)
        LoopState.objects.update_or_create(name="dispatch", defaults={"status": "disabled"})
        _migration._enable_sound_default_loops(apps, None)
        assert Loop.objects.get(name="dispatch").enabled is False

    def test_forward_respects_a_pause_hold(self) -> None:
        Loop.objects.filter(name="tickets").update(enabled=False)
        LoopState.objects.update_or_create(name="tickets", defaults={"status": "paused"})
        _migration._enable_sound_default_loops(apps, None)
        assert Loop.objects.get(name="tickets").enabled is False

    def test_forward_enables_an_untouched_seed_default(self) -> None:
        Loop.objects.filter(name="inbox").update(enabled=False)
        LoopState.objects.filter(name="inbox").delete()
        _migration._enable_sound_default_loops(apps, None)
        assert Loop.objects.get(name="inbox").enabled is True

    def test_forward_is_idempotent(self) -> None:
        Loop.objects.filter(name__in=_SOUND_ON).update(enabled=False)
        _migration._enable_sound_default_loops(apps, None)
        first = dict(Loop.objects.filter(name__in=_SOUND_ON).values_list("name", "enabled"))
        _migration._enable_sound_default_loops(apps, None)
        second = dict(Loop.objects.filter(name__in=_SOUND_ON).values_list("name", "enabled"))
        assert first == second
        assert {k for k, v in second.items() if v} == _SOUND_ON

    def test_reverse_disables_the_on_set_but_keeps_an_explicit_enable(self) -> None:
        Loop.objects.filter(name__in=_SOUND_ON).update(enabled=True)
        LoopState.objects.update_or_create(name="inbox", defaults={"status": "enabled"})
        _migration._disable_sound_default_loops(apps, None)
        assert Loop.objects.get(name="inbox").enabled is True
        assert not Loop.objects.filter(name__in=_SOUND_ON - {"inbox"}, enabled=True).exists()


# ``setUp`` reverse-migrates ``core`` to ``zero`` then re-applies the full graph
# on the shared ``default`` connection — several seconds single-core that
# exceeds the global 60s ``pytest-timeout`` under maximum parallel contention.
# Scoped 240s bump mirroring ``FreshMigrateSeedsDefaultLoops`` (#1189).
@pytest.mark.timeout(240)
class FreshMigrateEnablesSoundDefaults(TransactionTestCase):
    """A migrate from ``zero`` re-runs 0001's seed then 0043's flip, landing the split."""

    def setUp(self) -> None:
        call_command("migrate", "core", "zero", "--no-input", verbosity=0)
        self.addCleanup(call_command, "migrate", "core", "--no-input", verbosity=0)
        call_command("migrate", "core", "--no-input", verbosity=0)

    def test_fresh_migrate_enables_exactly_the_sound_on_set(self) -> None:
        loops = Loop.objects.all()
        assert loops.count() == len(DEFAULT_LOOPS)
        enabled = set(loops.filter(enabled=True).values_list("name", flat=True))
        assert enabled == _SOUND_ON
        assert loops.filter(enabled=False).count() == len(DEFAULT_LOOPS) - len(_SOUND_ON)
