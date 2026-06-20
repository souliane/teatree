"""Idempotent seed of the default loops + prompts (#2513, deferred item).

``t3 setup`` seeds the canonical default :class:`Loop` rows (and the prompts they
reference) so a fresh install — or a squashed-migration install — has the loops
present. The seed is idempotent: re-running it creates nothing new and never
clobbers an operator-edited row. Integration-first against the real DB.
"""

import io

import django.test
from django.core.management import call_command

from teatree.core.models import Loop, Prompt
from teatree.loops.registry import iter_loops
from teatree.loops.seed import DEFAULT_LOOPS, seed_default_loops_and_prompts


def _run() -> str:
    out = io.StringIO()
    call_command("seed_loops", stdout=out)
    return out.getvalue()


@django.test.override_settings(USE_TZ=True)
class TestSeedDefaultLoops(django.test.TestCase):
    def test_seeds_every_default_loop(self) -> None:
        seed_default_loops_and_prompts()
        names = set(Loop.objects.values_list("name", flat=True))
        assert {spec.name for spec in DEFAULT_LOOPS} <= names

    def test_seeded_loop_table_matches_iter_loops(self) -> None:
        # No orphan seed row that the master fan-out / iter_loops can never run
        # (#2584). Every seeded ``Loop`` name must have a registry ``MiniLoop``
        # (so ``build_loop_table_jobs`` can resolve it) and every registry loop
        # must be seeded. ``slack_answer`` used to break this — it was in the
        # seed + migration 0087 but had no registry MiniLoop (it runs only via
        # the piggyback cycle, ``tick_piggyback``).
        seed_default_loops_and_prompts()
        seeded = {spec.name for spec in DEFAULT_LOOPS}
        registry = {loop.name for loop in iter_loops()}
        assert seeded == registry, (
            f"seed/registry mismatch: seed-only={seeded - registry}, registry-only={registry - seeded}"
        )

    def test_seed_is_idempotent_no_duplicate_rows(self) -> None:
        seed_default_loops_and_prompts()
        first = Loop.objects.count()
        seed_default_loops_and_prompts()
        assert Loop.objects.count() == first

    def test_seed_creates_every_loop_paused(self) -> None:
        # The #2513 cutover is plumbing only — a fresh seed must land EVERY
        # default loop disabled (enabled=False). Turning a loop on is a
        # deliberate operator action, never a side effect of install/seed.
        seed_default_loops_and_prompts()
        seeded = Loop.objects.filter(name__in=[s.name for s in DEFAULT_LOOPS])
        assert seeded.count() == len(DEFAULT_LOOPS)
        assert not seeded.filter(enabled=True).exists()

    def test_seed_preserves_operator_edited_enabled_flag(self) -> None:
        seed_default_loops_and_prompts()
        # Operator ENABLES a loop, then setup runs the seed again — the seed
        # must not clobber the operator's choice back to paused.
        Loop.objects.filter(name="inbox").update(enabled=True)
        seed_default_loops_and_prompts()
        assert Loop.objects.get(name="inbox").enabled is True

    def test_default_loops_satisfy_the_prompt_xor_script_constraint(self) -> None:
        # Every seeded row must hold exactly one of prompt-FK / script (the DB
        # CheckConstraint) — a seed that violated it would raise on create.
        seed_default_loops_and_prompts()
        for loop in Loop.objects.filter(name__in=[s.name for s in DEFAULT_LOOPS]):
            has_prompt = loop.prompt_id is not None
            has_script = bool(loop.script)
            assert has_prompt != has_script, loop.name

    def test_management_command_seeds_and_reports(self) -> None:
        out = _run()
        assert Loop.objects.filter(name="dispatch").exists()
        assert "loops" in out.lower()

    def test_management_command_is_idempotent(self) -> None:
        _run()
        count = Loop.objects.count()
        prompts = Prompt.objects.count()
        _run()
        assert Loop.objects.count() == count
        assert Prompt.objects.count() == prompts
