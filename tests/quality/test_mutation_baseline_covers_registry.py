"""The committed baseline must cover every module the ``--all`` run mutates.

Regression for souliane/teatree#2142. The ``mutation-full`` CI job runs
``t3 mutation run --all``, which mutates EVERY module in
``[tool.teatree.mutation].high_value_modules``. The verdict compares the
whole-run surviving-mutant total against the SUM of ``baseline_surviving``
counts. When only a subset of the registry has a baseline entry, the ``--all``
total always exceeds the partial baseline sum and the gate is permanently RED
(738 measured vs 131 baselined → the first PR of every ISO week cannot pass).

The fix records the real current per-module survivor counts for ALL eight
modules so the baseline sum equals the measured total — an honest floor, not a
disarm: the gate is now a shrink-only ratchet that goes RED on any NEW survivor.
These tests lock three properties. Coverage: every ``high_value_module`` has a
``baseline_surviving`` entry. Green-at-current-state: a ``--all`` run measuring
exactly the recorded counts passes (``verdict == 0``). Ratchet-bites
(anti-vacuous): one survivor ABOVE the recorded total turns the gate RED
(``verdict == 1``) — a floor was set, the check was not removed.
"""

from teatree.quality.mutation import load_high_value_modules
from teatree.quality.mutation_run import BaselineRatchet, MutationOutcome, load_baseline_per_module, load_settings


class TestBaselineCoversWholeRegistry:
    """#2142: the ``--all`` run mutates all 8 modules; the baseline must too."""

    def test_every_high_value_module_has_a_baseline_entry(self) -> None:
        registry = load_high_value_modules()
        baseline = load_baseline_per_module()
        missing = [module for module in registry if module not in baseline]
        assert not missing, (
            "mutation-full runs `--all` over every high_value_module, but these have no "
            f"baseline_surviving entry, so the run's total always exceeds the partial baseline "
            f"sum and the gate is permanently red (#2142): {missing}"
        )

    def test_baseline_records_no_module_outside_the_registry(self) -> None:
        registry = set(load_high_value_modules())
        baseline = load_baseline_per_module()
        stray = [module for module in baseline if module not in registry]
        assert not stray, f"baseline_surviving records modules not in high_value_modules: {stray}"


class TestGreenAtCurrentState:
    """A ``--all`` run measuring exactly the recorded per-module counts passes."""

    def _all_modules_outcome(self, per_module: dict[str, int]) -> MutationOutcome:
        """Build a ``--all`` outcome whose survivors match the recorded baseline.

        Each module gets ``count`` survivors named with its dotted prefix so the
        per-module attribution (and the whole-run total) mirror a real run that
        reproduced the committed floor exactly.
        """
        survived: list[str] = []
        for module, count in per_module.items():
            prefix = BaselineRatchet.module_dotted_prefix(module)
            survived.extend(f"{prefix}.f__mutmut_{i}" for i in range(count))
        return MutationOutcome(
            scoped_modules=tuple(per_module),
            survived=tuple(survived),
            killed=(),
            inconclusive=(),
        )

    def test_full_run_at_recorded_counts_passes(self) -> None:
        settings = load_settings()
        per_module = load_baseline_per_module()
        outcome = self._all_modules_outcome(per_module)
        assert outcome.total_mutants == settings.baseline_total
        assert BaselineRatchet.verdict(outcome, mode=settings.mode, baseline=settings.baseline_total) == 0

    def test_attribution_reproduces_the_committed_per_module_floor(self) -> None:
        per_module = load_baseline_per_module()
        outcome = self._all_modules_outcome(per_module)
        # Every recorded module re-derives its exact committed count from the
        # synthetic survivor names — the per-module ratchet would not loosen.
        measured = BaselineRatchet.survivors_per_module(outcome)
        assert measured == per_module
        _new_baseline, loosens = BaselineRatchet.per_module(outcome, committed=per_module)
        assert loosens is False


class TestRatchetStillBites:
    """Anti-vacuous: one survivor ABOVE the recorded total turns the gate RED."""

    def _all_modules_outcome(self, per_module: dict[str, int], *, extra: int = 0) -> MutationOutcome:
        survived: list[str] = []
        for module, count in per_module.items():
            prefix = BaselineRatchet.module_dotted_prefix(module)
            survived.extend(f"{prefix}.f__mutmut_{i}" for i in range(count))
        if extra:
            # A brand-new survivor on a safety module the suite stopped catching.
            first = next(iter(per_module))
            prefix = BaselineRatchet.module_dotted_prefix(first)
            survived.extend(f"{prefix}.g__regression_{i}" for i in range(extra))
        return MutationOutcome(
            scoped_modules=tuple(per_module),
            survived=tuple(survived),
            killed=(),
            inconclusive=(),
        )

    def test_one_new_survivor_above_baseline_fails(self) -> None:
        settings = load_settings()
        per_module = load_baseline_per_module()
        outcome = self._all_modules_outcome(per_module, extra=1)
        assert BaselineRatchet.exceeds_baseline(outcome, baseline=settings.baseline_total) is True
        assert BaselineRatchet.verdict(outcome, mode=settings.mode, baseline=settings.baseline_total) == 1

    def test_per_module_update_refuses_to_loosen_on_a_new_survivor(self) -> None:
        per_module = load_baseline_per_module()
        outcome = self._all_modules_outcome(per_module, extra=1)
        _new_baseline, loosens = BaselineRatchet.per_module(outcome, committed=per_module)
        assert loosens is True
