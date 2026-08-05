"""Unit tests for the scoped mutmut runner's pure logic.

The runner's IO-bound half (forking mutmut over a temp tree) is exercised
end-to-end by the kill-proof in ``tests/quality/test_mutation_kill_proof.py``.
This file pins the deterministic pieces: the mutmut config it writes, the result
parser, the ratchet/verdict, and the diff-resolution wiring.
"""

import dataclasses
import inspect
from pathlib import Path

import pytest

from teatree.quality import mutation_run
from teatree.quality.mutation_run import (
    BaselineRatchet,
    MutationOutcome,
    MutationResult,
    MutationSettings,
    MutationToolCrashError,
    build_mutmut_config,
    load_baseline_per_module,
    load_settings,
    module_test_paths,
    parse_results,
    run_scoped,
)
from teatree.utils.run import CommandFailedError, TimeoutExpired


class TestModuleTestPaths:
    def test_mapped_module_gets_its_own_scoped_dirs(self) -> None:
        settings = MutationSettings(
            timeout_seconds=1,
            module_tests={"src/teatree/on_behalf_gate.py": ("tests/teatree_core/",), "default": ("tests/",)},
            baseline_total=0,
        )
        assert module_test_paths("src/teatree/on_behalf_gate.py", settings) == ("tests/teatree_core/",)

    def test_unmapped_module_falls_back_to_the_default_entry(self) -> None:
        settings = MutationSettings(timeout_seconds=1, module_tests={"default": ("tests/",)}, baseline_total=0)
        assert module_test_paths("src/teatree/unmapped.py", settings) == ("tests/",)

    def test_no_default_entry_falls_back_to_the_whole_suite(self) -> None:
        settings = MutationSettings(timeout_seconds=1, module_tests={}, baseline_total=0)
        assert module_test_paths("src/teatree/unmapped.py", settings) == ("tests/",)


class TestBuildMutmutConfig:
    def test_emits_a_mutmut_ini_section(self) -> None:
        cfg = build_mutmut_config(("src/teatree/on_behalf_gate.py",), tests_dir=("tests/",))
        assert cfg.startswith("[mutmut]\n")

    def test_scopes_paths_to_mutate_to_the_subset(self) -> None:
        cfg = build_mutmut_config(("src/teatree/on_behalf_gate.py",), tests_dir=("tests/",))
        assert "paths_to_mutate" in cfg
        assert "src/teatree/on_behalf_gate.py" in cfg

    def test_forces_serial_debug_mode(self) -> None:
        cfg = build_mutmut_config(("src/teatree/x.py",), tests_dir=("tests/",))
        assert "debug = true" in cfg

    def test_includes_tests_dir(self) -> None:
        cfg = build_mutmut_config(("src/teatree/x.py",), tests_dir=("tests/teatree_core/",))
        assert "tests/teatree_core/" in cfg


class TestParseResults:
    def test_classifies_killed_and_survived(self) -> None:
        raw = (
            "    teatree.x.f__mutmut_1: killed\n"
            "    teatree.x.f__mutmut_2: survived\n"
            "    teatree.x.f__mutmut_3: no tests\n"
            "    teatree.x.f__mutmut_4: timeout\n"
        )
        result = parse_results(raw)
        assert result.killed == ("teatree.x.f__mutmut_1",)
        assert set(result.survived) == {
            "teatree.x.f__mutmut_2",
            "teatree.x.f__mutmut_3",
        }

    def test_timeout_and_segfault_are_inconclusive_not_survivors(self) -> None:
        raw = "    a: timeout\n    b: segfault\n    c: killed\n"
        result = parse_results(raw)
        assert result.survived == ()
        assert result.killed == ("c",)
        assert set(result.inconclusive) == {"a", "b"}

    def test_empty_output_is_empty_result(self) -> None:
        result = parse_results("")
        assert result.killed == ()
        assert result.survived == ()

    def test_ignores_non_result_lines(self) -> None:
        raw = "Mutant results\n--------------\n    a: survived\n"
        result = parse_results(raw)
        assert result.survived == ("a",)


class TestBaselineRatchetVerdict:
    def _outcome(self, *, survivors: int, scoped: tuple[str, ...] = ("src/teatree/x.py",)) -> MutationOutcome:
        return MutationOutcome(
            scoped_modules=scoped,
            survived=tuple(f"m{i}" for i in range(survivors)),
            killed=(),
            inconclusive=(),
        )

    def test_fails_when_survivors_exceed_baseline(self) -> None:
        outcome = self._outcome(survivors=5)
        assert BaselineRatchet.verdict(outcome, baseline=0) == 1

    def test_passes_at_or_below_baseline(self) -> None:
        outcome = self._outcome(survivors=2)
        assert BaselineRatchet.verdict(outcome, baseline=2) == 0

    def test_fails_one_above_baseline(self) -> None:
        outcome = self._outcome(survivors=3)
        assert BaselineRatchet.verdict(outcome, baseline=2) == 1

    def test_passes_with_no_survivors(self) -> None:
        outcome = self._outcome(survivors=0)
        assert BaselineRatchet.verdict(outcome, baseline=0) == 0

    def test_no_op_outcome_passes(self) -> None:
        outcome = MutationOutcome(scoped_modules=(), survived=(), killed=(), inconclusive=())
        assert BaselineRatchet.verdict(outcome, baseline=0) == 0


class TestNoInertModeParameter:
    """The verdict is a pure function of ``(outcome, baseline)`` — no discarded knob.

    ``verdict`` used to accept and validate a ``mode`` that both values resolved
    identically through, and ``MutationSettings`` carried the field only to feed
    it. A parameter validated and then discarded reads as a live control it is
    not, so neither survives.
    """

    def test_verdict_accepts_no_mode_argument(self) -> None:
        assert "mode" not in inspect.signature(BaselineRatchet.verdict).parameters

    def test_settings_carry_no_mode_field(self) -> None:
        assert "mode" not in {field.name for field in dataclasses.fields(MutationSettings)}

    def test_a_survivor_at_baseline_passes(self) -> None:
        outcome = MutationOutcome(
            scoped_modules=("src/teatree/x.py",),
            survived=("m0",),
            killed=(),
            inconclusive=(),
        )
        assert BaselineRatchet.verdict(outcome, baseline=1) == 0


class TestSurvivingExceedsBaseline:
    """The ratchet: more survivors than the recorded baseline fails."""

    def _outcome(self, *, survivors: int) -> MutationOutcome:
        return MutationOutcome(
            scoped_modules=("src/teatree/x.py",),
            survived=tuple(f"m{i}" for i in range(survivors)),
            killed=(),
            inconclusive=(),
        )

    def test_more_survivors_than_baseline_exceeds(self) -> None:
        assert BaselineRatchet.exceeds_baseline(self._outcome(survivors=8), baseline=7) is True

    def test_equal_to_baseline_does_not_exceed(self) -> None:
        assert BaselineRatchet.exceeds_baseline(self._outcome(survivors=7), baseline=7) is False

    def test_fewer_than_baseline_does_not_exceed(self) -> None:
        assert BaselineRatchet.exceeds_baseline(self._outcome(survivors=3), baseline=7) is False

    def test_no_op_outcome_never_exceeds(self) -> None:
        outcome = MutationOutcome(scoped_modules=(), survived=(), killed=(), inconclusive=())
        assert BaselineRatchet.exceeds_baseline(outcome, baseline=0) is False


class TestModuleDottedPrefix:
    def test_strips_src_and_suffix_and_dots_the_path(self) -> None:
        assert BaselineRatchet.module_dotted_prefix("src/teatree/on_behalf_gate.py") == "teatree.on_behalf_gate"

    def test_handles_nested_package_path(self) -> None:
        assert (
            BaselineRatchet.module_dotted_prefix("src/teatree/core/merge/execution.py")
            == "teatree.core.merge.execution"
        )


class TestSurvivorsPerModule:
    def test_attributes_each_survivor_to_its_module_by_dotted_prefix(self) -> None:
        outcome = MutationOutcome(
            scoped_modules=("src/teatree/a.py", "src/teatree/b.py"),
            survived=("teatree.a.f__mutmut_1", "teatree.a.g__mutmut_2", "teatree.b.h__mutmut_1"),
            killed=(),
            inconclusive=(),
        )
        assert BaselineRatchet.survivors_per_module(outcome) == {"src/teatree/a.py": 2, "src/teatree/b.py": 1}

    def test_longest_prefix_wins_over_a_shorter_sibling(self) -> None:
        outcome = MutationOutcome(
            scoped_modules=("src/teatree/core.py", "src/teatree/core/merge.py"),
            survived=("teatree.core.merge.x__mutmut_1", "teatree.core.y__mutmut_1"),
            killed=(),
            inconclusive=(),
        )
        assert BaselineRatchet.survivors_per_module(outcome) == {
            "src/teatree/core.py": 1,
            "src/teatree/core/merge.py": 1,
        }

    def test_no_survivors_is_all_zero(self) -> None:
        outcome = MutationOutcome(scoped_modules=("src/teatree/a.py",), survived=(), killed=(), inconclusive=())
        assert BaselineRatchet.survivors_per_module(outcome) == {"src/teatree/a.py": 0}

    def test_survivor_matching_no_scoped_module_is_not_attributed(self) -> None:
        outcome = MutationOutcome(
            scoped_modules=("src/teatree/a.py",),
            survived=("teatree.unrelated.f__mutmut_1",),
            killed=(),
            inconclusive=(),
        )
        assert BaselineRatchet.survivors_per_module(outcome) == {"src/teatree/a.py": 0}


class TestRatchetPerModuleBaseline:
    def _outcome(self, survived: tuple[str, ...]) -> MutationOutcome:
        return MutationOutcome(
            scoped_modules=("src/teatree/a.py", "src/teatree/b.py"),
            survived=survived,
            killed=(),
            inconclusive=(),
        )

    def test_fewer_survivors_tightens_that_module_and_does_not_loosen(self) -> None:
        outcome = self._outcome(("teatree.a.f__mutmut_1",))
        new_baseline, loosens = BaselineRatchet.per_module(
            outcome, committed={"src/teatree/a.py": 4, "src/teatree/b.py": 0}
        )
        assert new_baseline == {"src/teatree/a.py": 1, "src/teatree/b.py": 0}
        assert loosens is False

    def test_more_survivors_flags_loosen_and_holds_the_lower_count(self) -> None:
        outcome = self._outcome(("teatree.a.f__mutmut_1", "teatree.a.g__mutmut_2"))
        new_baseline, loosens = BaselineRatchet.per_module(
            outcome, committed={"src/teatree/a.py": 1, "src/teatree/b.py": 0}
        )
        assert new_baseline == {"src/teatree/a.py": 1, "src/teatree/b.py": 0}
        assert loosens is True

    def test_modules_not_in_this_run_carry_through_unchanged(self) -> None:
        outcome = self._outcome(())
        new_baseline, loosens = BaselineRatchet.per_module(
            outcome, committed={"src/teatree/a.py": 0, "src/teatree/b.py": 0, "src/teatree/other.py": 5}
        )
        assert new_baseline["src/teatree/other.py"] == 5
        assert loosens is False


class TestLoadBaselinePerModule:
    def test_reads_the_per_module_counts(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text(
            "[tool.teatree.mutation]\n"
            'high_value_modules = [ "src/teatree/x.py" ]\n'
            'baseline_surviving = [ { path = "src/teatree/x.py", count = 7 }, '
            '{ path = "src/teatree/y.py", count = 2 } ]\n',
            encoding="utf-8",
        )
        assert load_baseline_per_module(path) == {"src/teatree/x.py": 7, "src/teatree/y.py": 2}

    def test_absent_array_is_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text('[tool.teatree.mutation]\nhigh_value_modules = [ "src/teatree/x.py" ]\n', encoding="utf-8")
        assert load_baseline_per_module(path) == {}


class TestRunScopedWiring:
    _REGISTRY = ("src/teatree/a.py", "src/teatree/b.py")
    _SETTINGS = MutationSettings(
        timeout_seconds=10,
        module_tests={"default": ("tests/",)},
        baseline_total=0,
    )

    def _spy(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
        calls: list[tuple[str, ...]] = []

        def fake(modules, **_kwargs):
            calls.append(tuple(modules))
            # A real run always classifies at least one mutant; return one killed
            # so the zero-mutant fail-loud guard (#7) doesn't trip the wiring tests.
            return MutationResult(killed=("teatree.b.f__mutmut_1",), survived=(), inconclusive=())

        monkeypatch.setattr(mutation_run, "_run_mutmut", fake)
        return calls

    def test_no_op_skips_mutmut_when_diff_touches_no_safety_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._spy(monkeypatch)
        outcome = run_scoped(
            changed_files=("README.md",),
            settings=self._SETTINGS,
            registry=self._REGISTRY,
        )
        assert outcome.is_no_op
        assert calls == []

    def test_diff_scopes_to_touched_modules(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._spy(monkeypatch)
        outcome = run_scoped(
            changed_files=("src/teatree/b.py", "README.md"),
            settings=self._SETTINGS,
            registry=self._REGISTRY,
        )
        assert outcome.scoped_modules == ("src/teatree/b.py",)
        assert calls == [("src/teatree/b.py",)]

    def test_all_modules_mutates_whole_registry_without_diffing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._spy(monkeypatch)
        outcome = run_scoped(all_modules=True, settings=self._SETTINGS, registry=self._REGISTRY)
        assert outcome.scoped_modules == self._REGISTRY
        assert calls == [self._REGISTRY]


class TestZeroMutantsFailsLoud:
    """Fix #7: a scoped run that produces zero mutants is a FAILURE, not a pass.

    When a safety module is touched (scoped non-empty) but mutmut classifies zero
    mutants — a crash, an empty results DB, a silent invocation failure — the old
    gate returned an all-empty outcome whose surviving count (0) was at-or-below
    baseline, so it exited 0 having tested nothing (fake-green). run_scoped now
    raises so the gate fails loud.
    """

    _REGISTRY = ("src/teatree/a.py",)
    _SETTINGS = MutationSettings(
        timeout_seconds=10,
        module_tests={"default": ("tests/",)},
        baseline_total=0,
    )

    def test_scoped_run_with_zero_mutants_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            mutation_run,
            "_run_mutmut",
            lambda _modules, **_kwargs: MutationResult(killed=(), survived=(), inconclusive=()),
        )
        with pytest.raises(mutation_run.ZeroMutantsError, match="zero mutants"):
            run_scoped(changed_files=("src/teatree/a.py",), settings=self._SETTINGS, registry=self._REGISTRY)

    def test_scoped_run_with_only_inconclusive_mutants_is_a_tool_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An inconclusive-only run DID execute mutmut — that is not the zero-mutant
        # case — but it graded nothing, so its "0 survivors" is the absence of a
        # measurement. Reported as the same whole-run environment artifact a tool
        # timeout is, which the warn-first CLI passes without touching the baseline.
        monkeypatch.setattr(
            mutation_run,
            "_run_mutmut",
            lambda _modules, **_kwargs: MutationResult(killed=(), survived=(), inconclusive=("a: timeout",)),
        )
        with pytest.raises(mutation_run.MutationToolCrashError, match="measured nothing"):
            run_scoped(changed_files=("src/teatree/a.py",), settings=self._SETTINGS, registry=self._REGISTRY)

    def test_no_op_run_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No safety module in scope → no_op, run_scoped returns before _run_mutmut,
        # so the zero-mutant guard never fires (a no-op is a legitimate pass).
        monkeypatch.setattr(
            mutation_run,
            "_run_mutmut",
            lambda _modules, **_kwargs: MutationResult(killed=(), survived=(), inconclusive=()),
        )
        outcome = run_scoped(changed_files=("README.md",), settings=self._SETTINGS, registry=self._REGISTRY)
        assert outcome.is_no_op


class TestMutmutToolCrashIsWarnFirst:
    """A mutmut tool crash surfaces as :class:`MutationToolCrashError`.

    A wall-clock timeout / process failure is an environment artifact, not a test
    gap — distinct from a genuine survivors-over-baseline regression and from the
    fake-green :class:`ZeroMutantsError` guard, both of which stay failures. The
    warn-first CLI catches this type and exits 0.
    """

    def _cfg(self, monkeypatch: pytest.MonkeyPatch, raises: Exception) -> None:
        def _raise(*_args: object, **_kwargs: object) -> object:
            raise raises

        monkeypatch.setattr(mutation_run, "run_allowed_to_fail", _raise)

    def test_subprocess_timeout_raises_tool_crash(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._cfg(monkeypatch, TimeoutExpired(cmd=["mutmut", "run"], timeout=540))
        with pytest.raises(MutationToolCrashError, match="timed out"):
            mutation_run._run_mutmut(("src/teatree/a.py",), tests_dir=("tests/",), repo=str(tmp_path), timeout=540)

    def test_subprocess_command_failure_raises_tool_crash(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._cfg(monkeypatch, CommandFailedError(["mutmut", "results"], 2, "", "boom"))
        with pytest.raises(MutationToolCrashError):
            mutation_run._run_mutmut(("src/teatree/a.py",), tests_dir=("tests/",), repo=str(tmp_path), timeout=540)

    def test_tool_crash_cleans_up_the_scoped_config(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._cfg(monkeypatch, TimeoutExpired(cmd=["mutmut", "run"], timeout=540))
        with pytest.raises(MutationToolCrashError):
            mutation_run._run_mutmut(("src/teatree/a.py",), tests_dir=("tests/",), repo=str(tmp_path), timeout=540)
        # The generated setup.cfg / pytest.ini must not survive a crash — a stale
        # config would make the next run refuse with FileExistsError.
        assert not (tmp_path / "setup.cfg").exists()
        assert not (tmp_path / "pytest.ini").exists()


class TestLoadSettings:
    def _write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "pyproject.toml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_defaults_when_only_modules_present(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            '[tool.teatree.mutation]\nhigh_value_modules = [ "src/teatree/x.py" ]\n',
        )
        settings = load_settings(path)
        assert settings.baseline_total == 0
        assert settings.timeout_seconds == 540

    def test_reads_timeout_and_baseline(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "[tool.teatree.mutation]\n"
            'high_value_modules = [ "src/teatree/x.py" ]\n'
            "timeout_seconds = 60\n"
            'baseline_surviving = [ { path = "src/teatree/x.py", count = 4 } ]\n',
        )
        settings = load_settings(path)
        assert settings.timeout_seconds == 60
        assert settings.baseline_total == 4
