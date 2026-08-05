"""An inconclusive mutation run is not a measurement, and must not move the ratchet.

``survived`` is empty in two opposite situations: a module whose tests kill every
mutant, and a module whose every mutant segfaulted on a loaded runner. Reading the
second as the first records "0 survivors" for code that was never exercised — and
``--update-baseline`` then commits that zero, so the next honest run reds against a
baseline derived from a run that measured nothing.
"""

import pytest

from teatree.quality import mutation_run
from teatree.quality.mutation_run import (
    BaselineRatchet,
    MutationOutcome,
    MutationResult,
    MutationSettings,
    MutationToolCrashError,
    run_scoped,
)

_SETTINGS = MutationSettings(timeout_seconds=10, module_tests={"default": ("tests/",)}, baseline_total=0)
_REGISTRY = ("src/teatree/a.py", "src/teatree/b.py")


class TestMeasuredModules:
    def test_a_module_with_only_inconclusive_mutants_is_unmeasured(self) -> None:
        outcome = MutationOutcome(
            scoped_modules=_REGISTRY,
            survived=("teatree.a.f__mutmut_1",),
            killed=("teatree.a.g__mutmut_2",),
            inconclusive=("teatree.b.h__mutmut_1", "teatree.b.h__mutmut_2"),
        )
        assert BaselineRatchet.measured_modules(outcome) == {"src/teatree/a.py"}

    def test_a_killed_mutant_alone_counts_as_measured(self) -> None:
        # Zero survivors is a real result when something was actually graded.
        outcome = MutationOutcome(
            scoped_modules=("src/teatree/a.py",),
            survived=(),
            killed=("teatree.a.f__mutmut_1",),
            inconclusive=(),
        )
        assert BaselineRatchet.measured_modules(outcome) == {"src/teatree/a.py"}


class TestUnmeasuredModuleKeepsItsBaseline:
    def test_an_all_segfault_module_is_not_ratcheted_to_zero(self) -> None:
        # The recorded case: mutmut segfaults on every mutant of one module while
        # another module grades cleanly. Committing 0 for the segfaulting module
        # reds the next honest run against a number nothing measured.
        outcome = MutationOutcome(
            scoped_modules=_REGISTRY,
            survived=(),
            killed=("teatree.a.f__mutmut_1",),
            inconclusive=("teatree.b.g__mutmut_1",),
        )

        new_baseline, loosens = BaselineRatchet.per_module(
            outcome, committed={"src/teatree/a.py": 3, "src/teatree/b.py": 4}
        )

        assert new_baseline["src/teatree/b.py"] == 4, "an unmeasured module keeps its committed count"
        assert new_baseline["src/teatree/a.py"] == 0, "a measured module still auto-tightens"
        assert loosens is False

    def test_an_unmeasured_module_can_never_flag_a_loosen(self) -> None:
        outcome = MutationOutcome(
            scoped_modules=("src/teatree/b.py",),
            survived=(),
            killed=(),
            inconclusive=("teatree.b.g__mutmut_1",),
        )
        _, loosens = BaselineRatchet.per_module(outcome, committed={})
        assert loosens is False


class TestRunScopedRefusesAnUnmeasuredRun:
    def _run(self, monkeypatch: pytest.MonkeyPatch, result: MutationResult) -> MutationOutcome:
        monkeypatch.setattr(mutation_run, "_run_mutmut", lambda modules, **_kw: result)
        return run_scoped(all_modules=True, settings=_SETTINGS, registry=_REGISTRY)

    def test_an_all_inconclusive_run_is_a_tool_crash_not_a_clean_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The warn-first CLI treats this as inconclusive (exit 0 with a WARNING) and
        # — crucially — never reaches `--update-baseline`, so nothing is written back.
        result = MutationResult(killed=(), survived=(), inconclusive=("teatree.a.f__mutmut_1",))
        with pytest.raises(MutationToolCrashError, match="measured nothing"):
            self._run(monkeypatch, result)

    def test_one_conclusive_mutant_keeps_the_run_a_measurement(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The anti-vacuity control: a partially-inconclusive run still measured
        # something, so it stays a real outcome rather than a crash.
        result = MutationResult(killed=("teatree.a.f__mutmut_1",), survived=(), inconclusive=("teatree.b.g__mutmut_1",))
        outcome = self._run(monkeypatch, result)
        assert outcome.is_unmeasured is False
        assert outcome.killed == ("teatree.a.f__mutmut_1",)
