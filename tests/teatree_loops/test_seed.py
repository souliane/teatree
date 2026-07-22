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
    def setUp(self) -> None:
        # Exercise the seed FUNCTION's defaults, NOT the migration's output.
        # Migration 0094 (and friends) seed the same Loop/Prompt rows at
        # migrate-time, and ``seed_default_loops_and_prompts`` uses
        # ``get_or_create(name=...)`` — on the migrated test DB those rows
        # already exist, so the ``defaults`` block never runs and the
        # assertions would silently test the MIGRATION's output instead of the
        # seed function. Clearing the migration-seeded rows first forces every
        # ``get_or_create`` here to create from the seed's OWN ``defaults`` —
        # catching a seed.py regression (e.g. reverting ``script`` to the shared
        # ``run.py``) that the migration's rows would otherwise mask. The
        # TestCase transaction rolls this back, so it is test-local.
        Loop.objects.all().delete()
        Prompt.objects.all().delete()

    def test_seeds_every_default_loop(self) -> None:
        seed_default_loops_and_prompts()
        names = set(Loop.objects.values_list("name", flat=True))
        assert {spec.name for spec in DEFAULT_LOOPS} <= names

    def test_seeded_loop_table_matches_iter_loops(self) -> None:
        # No orphan seed row that the per-loop fan-out / iter_loops can never run
        # (#2584). Every seeded ``Loop`` name must have a registry ``MiniLoop``
        # (so ``build_loop_table_jobs`` can resolve it) and every registry loop
        # must be seeded. The reactive infra loops (``slack_answer`` /
        # ``self_improve`` / ``drain_queue``) are intentionally NOT seeded — they
        # have no registry MiniLoop and each runs as its own dedicated ``/loop``
        # (``t3 loop <slot> run``), never a per-loop tick.
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

    def test_seed_enables_the_operational_core_and_pauses_the_rest(self) -> None:
        # The sound-defaults reversal of the #2513 all-paused cutover: a fresh
        # seed lands the local/read-only operational core enabled and every
        # colleague-facing / heavy loop paused — the enabled set equals the
        # canonical ``default_enabled`` specs.
        seed_default_loops_and_prompts()
        seeded = Loop.objects.filter(name__in=[s.name for s in DEFAULT_LOOPS])
        assert seeded.count() == len(DEFAULT_LOOPS)
        enabled = set(seeded.filter(enabled=True).values_list("name", flat=True))
        assert enabled == {s.name for s in DEFAULT_LOOPS if s.default_enabled}

    def test_seed_preserves_operator_edited_enabled_flag(self) -> None:
        seed_default_loops_and_prompts()
        # Operator ENABLES a paused loop, then setup runs the seed again — the
        # seed must not clobber the operator's choice back to paused.
        Loop.objects.filter(name="arch_review").update(enabled=True)
        seed_default_loops_and_prompts()
        assert Loop.objects.get(name="arch_review").enabled is True

    def test_seed_preserves_an_operator_disabled_default_on_loop(self) -> None:
        seed_default_loops_and_prompts()
        # The mirror never-clobber: an operator DISABLES a default-on loop; a
        # re-seed must not re-enable it (``get_or_create`` never re-applies
        # ``defaults`` to an existing row).
        Loop.objects.filter(name="inbox").update(enabled=False)
        seed_default_loops_and_prompts()
        assert Loop.objects.get(name="inbox").enabled is False

    def test_issue_implementer_seeds_a_thirty_minute_cadence(self) -> None:
        seed_default_loops_and_prompts()
        assert Loop.objects.get(name="issue_implementer").delay_seconds == 1800

    def test_reseed_preserves_an_operator_tuned_cadence(self) -> None:
        seed_default_loops_and_prompts()
        Loop.objects.filter(name="issue_implementer").update(delay_seconds=900)
        seed_default_loops_and_prompts()
        assert Loop.objects.get(name="issue_implementer").delay_seconds == 900

    def test_default_loops_satisfy_the_prompt_xor_script_constraint(self) -> None:
        # Every seeded row must hold exactly one of prompt-FK / script (the DB
        # CheckConstraint) — a seed that violated it would raise on create.
        seed_default_loops_and_prompts()
        for loop in Loop.objects.filter(name__in=[s.name for s in DEFAULT_LOOPS]):
            has_prompt = loop.prompt_id is not None
            has_script = bool(loop.script)
            assert has_prompt != has_script, loop.name

    def test_each_script_loop_points_at_its_own_module(self) -> None:
        # The #2513 regression fix: a script loop's ``script`` is its OWN on-disk
        # module (``src/teatree/loops/<name>/loop.py``), never the retired shared
        # ``run.py``. The DB ``script`` column is now PER-LOOP and load-bearing.
        seed_default_loops_and_prompts()
        for loop in Loop.objects.filter(name__in=[s.name for s in DEFAULT_LOOPS]):
            if loop.script:
                assert loop.script == f"src/teatree/loops/{loop.name}/loop.py", loop.name

    def test_no_loop_points_at_the_retired_shared_runner(self) -> None:
        # No seeded row may carry the retired shared ``run.py`` entry point.
        seed_default_loops_and_prompts()
        assert not Loop.objects.filter(script="src/teatree/loops/run.py").exists()

    def test_no_two_script_loops_share_an_entry_point(self) -> None:
        # The owner's rule: the script is not shared — it is specific to one loop.
        # Every script-backed row must carry a DISTINCT entry point.
        seed_default_loops_and_prompts()
        scripts = list(Loop.objects.exclude(script="").values_list("script", flat=True))
        assert len(scripts) == len(set(scripts)), scripts

    def test_arch_review_is_prompt_backed_and_references_the_review_skill(self) -> None:
        # arch_review stays the single PROMPT-backed default; its body is a real
        # instruction telling the sub-agent to run an architectural review using
        # the ``ac-reviewing-codebase`` skill (owner decision) — not a script.
        seed_default_loops_and_prompts()
        arch = Loop.objects.get(name="arch_review")
        assert arch.script == ""
        assert arch.prompt_id is not None
        assert "ac-reviewing-codebase" in arch.prompt.body

    def test_every_seeded_loop_carries_a_real_description(self) -> None:
        # The owner's requirement: every default loop ships a real, useful
        # one-line description on its ``Loop`` row — never blank, never the old
        # ``Default loop prompt for ...`` placeholder.
        seed_default_loops_and_prompts()
        for loop in Loop.objects.filter(name__in=[s.name for s in DEFAULT_LOOPS]):
            assert loop.description.strip(), loop.name
            assert "Default loop prompt for" not in loop.description, loop.name

    def test_seeded_description_matches_the_spec(self) -> None:
        # The spec is the single source of truth: ``Loop.description`` is the
        # spec's ``description`` verbatim.
        seed_default_loops_and_prompts()
        by_name = {loop.name: loop for loop in Loop.objects.all()}
        for spec in DEFAULT_LOOPS:
            assert by_name[spec.name].description == spec.description, spec.name

    def test_reseed_backfills_a_blank_description_on_an_existing_row(self) -> None:
        # A pre-feature install has a row with a blank description; re-running the
        # seed must backfill it from the spec (the "reseed updates existing row"
        # wiring) rather than leaving the placeholder/blank in place.
        seed_default_loops_and_prompts()
        Loop.objects.filter(name="dispatch").update(description="")
        seed_default_loops_and_prompts()
        dispatch_spec = next(s for s in DEFAULT_LOOPS if s.name == "dispatch")
        assert Loop.objects.get(name="dispatch").description == dispatch_spec.description

    def test_reseed_does_not_clobber_an_operator_edited_description(self) -> None:
        # Mirrors the enabled-flag preservation: an operator who rewrote a
        # description keeps it through a re-seed (only blank rows are backfilled).
        seed_default_loops_and_prompts()
        Loop.objects.filter(name="inbox").update(description="operator note")
        seed_default_loops_and_prompts()
        assert Loop.objects.get(name="inbox").description == "operator note"

    def test_seeded_colleague_facing_matches_the_spec(self) -> None:
        # #2904: followup reaches/reads a colleague, so it seeds
        # colleague_facing=True; #3569 unmasked the review loop (it always runs,
        # colleague admission is gated upstream by admit_colleague_prs_to_board),
        # so review is now colleague_facing=False like every other default loop.
        seed_default_loops_and_prompts()
        by_name = {loop.name: loop for loop in Loop.objects.all()}
        for spec in DEFAULT_LOOPS:
            assert by_name[spec.name].colleague_facing is spec.colleague_facing, spec.name

    def test_review_is_unmasked_and_followup_stays_colleague_facing(self) -> None:
        # #3569: review always runs (self-review must not be masked when away);
        # followup keeps its colleague-facing away-gate.
        seed_default_loops_and_prompts()
        assert Loop.objects.get(name="review").colleague_facing is False
        assert Loop.objects.get(name="followup").colleague_facing is True

    def test_ship_merge_loop_is_not_colleague_facing(self) -> None:
        """#3274: the auto-merge path (`ship`) is NOT colleague_facing.

        A non-colleague-facing loop is NOT deferred under `autonomous_away`. The
        internal merge-verdict + keystone merge sweep lives here (moved off the
        colleague-facing `review` loop in #3244), so a green own-PR still gets its
        verdict and auto-merges while the owner is away.
        """
        seed_default_loops_and_prompts()
        assert Loop.objects.get(name="ship").colleague_facing is False

    def test_triage_assessor_seed_is_paused_and_not_colleague_facing(self) -> None:
        # The needs-triage assessor loop is opt-in (default-off behind
        # triage_assessor_enabled) and reads no colleague surface, so it seeds
        # paused, script-backed, and colleague_facing=False.
        seed_default_loops_and_prompts()
        loop = Loop.objects.get(name="triage_assessor")
        assert loop.enabled is False
        assert loop.colleague_facing is False
        assert loop.script == "src/teatree/loops/triage_assessor/loop.py"
        assert loop.prompt_id is None

    def test_seed_preserves_operator_edited_colleague_facing_flag(self) -> None:
        seed_default_loops_and_prompts()
        # Operator flips inbox (default False) to colleague-facing; re-seeding
        # must not clobber that choice back to the spec default.
        Loop.objects.filter(name="inbox").update(colleague_facing=True)
        seed_default_loops_and_prompts()
        assert Loop.objects.get(name="inbox").colleague_facing is True

    def test_arch_review_prompt_description_is_the_real_description(self) -> None:
        # The single prompt-backed default's ``Prompt.description`` is the loop's
        # real description, not the retired ``Default loop prompt for ...`` placeholder.
        seed_default_loops_and_prompts()
        prompt = Prompt.objects.get(name="arch_review")
        assert prompt.description.strip()
        assert "Default loop prompt for" not in prompt.description

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
