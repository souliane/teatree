"""The consolidated default-loops seed folded into the squashed ``0001_initial`` (#2652/#3071).

The migration squash collapsed all of ``core`` to a single ``0001_initial`` and
folded ONE consolidated default-loops seed into it as a ``RunPython`` — so a
brand-new DB lands the canonical loop set the same way ``t3 setup`` seeds it. The
single seed absorbs every fresh-install data effect the old 0001..0043 chain
layered on: the loop rows, their descriptions (old 0009), ``colleague_facing``
(old 0016), the ``directive_loop`` row (old 0035), and the sound-default ON-set
(old 0043) — seeded directly ``enabled=default_enabled`` since a fresh DB carries
no operator ``LoopState`` hold.

A migration is frozen history and must not import the evolving
:mod:`teatree.loops.seed` module, so the seed values are INLINED in the
migration. :class:`InlinedSeedMatchesCanonicalSeed` pins those inlined constants
against the canonical seed so the migrate-path and the install-seed cannot
drift, and :class:`FreshMigrateSeedsDefaultLoops` proves a real migrate from
``zero`` lands every default loop in its final shape — descriptions and
``colleague_facing`` set, and exactly the sound operational-default set enabled.
"""

import importlib

import pytest
from django.core.management import call_command
from django.test import TransactionTestCase

from teatree.core.models import Loop
from teatree.loops.seed import ARCH_REVIEW_PROMPT_BODY, DEFAULT_LOOPS

_migration = importlib.import_module("teatree.core.migrations.0001_initial")

_SOUND_ON = frozenset(spec.name for spec in DEFAULT_LOOPS if spec.default_enabled)
_COLLEAGUE_FACING = frozenset(spec.name for spec in DEFAULT_LOOPS if spec.colleague_facing)


class TestInlinedSeedMatchesCanonicalSeed:
    """The migration's inlined seed must not drift from ``teatree.loops.seed``."""

    def test_inlined_loops_match_the_canonical_default_loops(self) -> None:
        # Any add/remove/reorder/cadence/description/flag change to the canonical
        # DEFAULT_LOOPS must be reflected in the migration's inlined snapshot —
        # otherwise a fresh migrate and ``t3 setup`` would seed different loops.
        expected = tuple(
            (
                spec.name,
                spec.delay_seconds,
                spec.daily_at,
                spec.prompt_body,
                spec.description,
                spec.colleague_facing,
                spec.default_enabled,
            )
            for spec in DEFAULT_LOOPS
        )
        assert expected == _migration._DEFAULT_LOOPS, (
            "Default-loops dataset drifted. The canonical set lives in THREE places that must "
            "stay in lock-step — edit all three:\n"
            "  1. src/teatree/loops/seed.py            (DEFAULT_LOOPS — the canonical source)\n"
            "  2. src/teatree/core/migrations/0001_initial.py  (_DEFAULT_LOOPS — inlined, frozen history)\n"
            "  3. this pin (tests/teatree_core/test_initial_migration_seed.py) + the "
            "registry-parity pin (tests/conformance/test_registry_parity.py)"
        )

    def test_inlined_arch_review_body_matches_the_canonical_body(self) -> None:
        assert _migration._ARCH_REVIEW_PROMPT_BODY == ARCH_REVIEW_PROMPT_BODY

    def test_sound_default_on_set_is_the_eight_local_read_only_loops(self) -> None:
        assert {
            "inbox",
            "dispatch",
            "tickets",
            "housekeeping",
            "idle_stack_reaper",
            "local_stack_queue",
            "resource_pressure",
            "pane_reaper",
        } == _SOUND_ON


# ``setUp`` reverse-migrates ``core`` to ``zero`` then re-applies the full graph
# on the shared ``default`` connection — several seconds single-core that
# exceeds the global 60s ``pytest-timeout`` under maximum ``-n auto --cov
# --doctest-modules`` parallel contention. Scoped 240s bump for the
# genuinely-slow migrations; the global 60s stays the hang-detector (#1189).
@pytest.mark.timeout(240)
class FreshMigrateSeedsDefaultLoops(TransactionTestCase):
    """A migrate from ``zero`` re-runs the seed ``RunPython`` and lands the loops."""

    def setUp(self) -> None:
        # Drop core to ``zero`` then re-apply ``0001_initial`` so the seed
        # RunPython genuinely runs (anti-vacuous: the rows are created from
        # scratch, not asserted against ambient migrate-time state). The
        # cleanup restores the head schema for the rest of the session.
        call_command("migrate", "core", "zero", "--no-input", verbosity=0)
        self.addCleanup(call_command, "migrate", "core", "--no-input", verbosity=0)
        call_command("migrate", "core", "--no-input", verbosity=0)

    def test_seeds_every_default_loop_once(self) -> None:
        assert Loop.objects.count() == len(DEFAULT_LOOPS)

    def test_seeds_the_sound_on_set_enabled_and_the_rest_paused(self) -> None:
        # The single consolidated seed sets ``enabled=default_enabled`` directly;
        # the migrate-path enabled set must equal the canonical seed's
        # ``default_enabled`` specs — colleague-facing / heavy loops stay opt-in.
        enabled = set(Loop.objects.filter(enabled=True).values_list("name", flat=True))
        assert enabled == _SOUND_ON
        assert Loop.objects.filter(enabled=False).count() == len(DEFAULT_LOOPS) - len(_SOUND_ON)

    def test_seeds_colleague_facing_on_exactly_the_colleague_loops(self) -> None:
        facing = set(Loop.objects.filter(colleague_facing=True).values_list("name", flat=True))
        assert facing == _COLLEAGUE_FACING

    def test_seeds_each_loop_description_from_the_canonical_seed(self) -> None:
        by_name = dict(Loop.objects.values_list("name", "description"))
        assert by_name == {spec.name: spec.description for spec in DEFAULT_LOOPS}
        assert all(desc for desc in by_name.values())

    def test_slack_answer_is_not_seeded(self) -> None:
        # ``slack_answer`` has no registry MiniLoop (it runs only via its
        # dedicated ``loop-slack-answer`` ``/loop`` slot); a seeded row would be
        # an orphan the loop-table fan-out can never dispatch.
        assert not Loop.objects.filter(name="slack_answer").exists()

    def test_each_script_loop_points_at_its_own_module(self) -> None:
        for spec in DEFAULT_LOOPS:
            if spec.is_prompt_backed:
                continue
            row = Loop.objects.get(name=spec.name)
            assert row.script == f"src/teatree/loops/{spec.name}/loop.py"
            assert row.prompt_id is None

    def test_arch_review_is_prompt_backed_with_the_review_skill_body(self) -> None:
        arch = Loop.objects.select_related("prompt").get(name="arch_review")
        assert arch.script == ""
        assert arch.prompt_id is not None
        assert "ac-reviewing-codebase" in arch.prompt.body
        assert arch.prompt.description == arch.description
