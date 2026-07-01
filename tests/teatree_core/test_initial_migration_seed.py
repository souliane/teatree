"""The default-loops seed folded into the squashed ``0001_initial`` (#2652).

The migration squash collapsed all of ``core`` to a single ``0001_initial`` and
folded the default-loops seed (the old ``0078_seed_loops`` intent, in its final
per-loop-script / paused shape) into it as a ``RunPython`` — so a brand-new DB
lands the canonical loop set the same way ``t3 setup`` seeds it.

A migration is frozen history and must not import the evolving
:mod:`teatree.loops.seed` module, so the seed values are INLINED in the
migration. :class:`InlinedSeedMatchesCanonicalSeed` pins those inlined constants
against the canonical seed so the migrate-path and the install-seed cannot
drift, and :class:`FreshMigrateSeedsDefaultLoops` proves a real migrate from
``zero`` lands every default loop PAUSED in its final shape.
"""

import importlib

from django.core.management import call_command
from django.test import TransactionTestCase

from teatree.core.models import Loop
from teatree.loops.seed import ARCH_REVIEW_PROMPT_BODY, DEFAULT_LOOPS

_migration = importlib.import_module("teatree.core.migrations.0001_initial")


class TestInlinedSeedMatchesCanonicalSeed:
    """The migration's inlined seed must not drift from ``teatree.loops.seed``."""

    def test_inlined_loops_match_the_canonical_default_loops(self) -> None:
        # Any add/remove/reorder/cadence change to the canonical DEFAULT_LOOPS
        # must be reflected in the migration's inlined snapshot — otherwise a
        # fresh migrate and ``t3 setup`` would seed different loop sets.
        expected = tuple((spec.name, spec.delay_seconds, spec.daily_at, spec.prompt_body) for spec in DEFAULT_LOOPS)
        assert expected == _migration._DEFAULT_LOOPS

    def test_inlined_arch_review_body_matches_the_canonical_body(self) -> None:
        assert _migration._ARCH_REVIEW_PROMPT_BODY == ARCH_REVIEW_PROMPT_BODY


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

    def test_seeds_exactly_the_default_loops_all_paused(self) -> None:
        loops = Loop.objects.all()
        assert loops.count() == len(DEFAULT_LOOPS)
        # Every default loop lands PAUSED — turning one on is a deliberate
        # operator action, never a side effect of a fresh install.
        assert not loops.filter(enabled=True).exists()

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
