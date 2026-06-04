"""Unit tests for the scoped mutmut runner's pure logic.

The runner's IO-bound half (forking mutmut over a temp tree) is exercised
end-to-end by the kill-proof in ``tests/quality/test_mutation_kill_proof.py``.
This file pins the deterministic pieces: the mutmut config it writes, the result
parser, the ratchet/verdict, and the diff-resolution wiring.
"""

from pathlib import Path

import pytest

from teatree.quality import mutation_run
from teatree.quality.mutation_run import (
    MutationOutcome,
    MutationResult,
    MutationSettings,
    build_mutmut_config,
    decide_verdict,
    load_settings,
    parse_results,
    run_scoped,
)


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


class TestDecideVerdict:
    def _outcome(self, *, survivors: int, scoped: tuple[str, ...] = ("src/teatree/x.py",)) -> MutationOutcome:
        return MutationOutcome(
            scoped_modules=scoped,
            survived=tuple(f"m{i}" for i in range(survivors)),
            killed=(),
            inconclusive=(),
        )

    def test_warn_mode_never_fails_even_with_survivors(self) -> None:
        outcome = self._outcome(survivors=5)
        assert decide_verdict(outcome, mode="warn", baseline=0) == 0

    def test_block_mode_fails_above_baseline(self) -> None:
        outcome = self._outcome(survivors=3)
        assert decide_verdict(outcome, mode="block", baseline=2) == 1

    def test_block_mode_passes_at_or_below_baseline(self) -> None:
        outcome = self._outcome(survivors=2)
        assert decide_verdict(outcome, mode="block", baseline=2) == 0

    def test_block_mode_passes_with_no_survivors(self) -> None:
        outcome = self._outcome(survivors=0)
        assert decide_verdict(outcome, mode="block", baseline=0) == 0

    def test_no_op_outcome_passes_in_any_mode(self) -> None:
        outcome = MutationOutcome(scoped_modules=(), survived=(), killed=(), inconclusive=())
        assert decide_verdict(outcome, mode="block", baseline=0) == 0


class TestRunScopedWiring:
    _REGISTRY = ("src/teatree/a.py", "src/teatree/b.py")
    _SETTINGS = MutationSettings(
        mode="warn",
        timeout_seconds=10,
        module_tests={"default": ("tests/",)},
        baseline_total=0,
    )

    def _spy(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
        calls: list[tuple[str, ...]] = []

        def fake(modules, **_kwargs):
            calls.append(tuple(modules))
            return MutationResult(killed=(), survived=(), inconclusive=())

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
        assert settings.mode == "warn"
        assert settings.baseline_total == 0

    def test_reads_mode_and_baseline(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "[tool.teatree.mutation]\n"
            'high_value_modules = [ "src/teatree/x.py" ]\n'
            'mode = "block"\n'
            'baseline_surviving = [ { path = "src/teatree/x.py", count = 4 } ]\n',
        )
        settings = load_settings(path)
        assert settings.mode == "block"
        assert settings.baseline_total == 4

    def test_rejects_unknown_mode(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            '[tool.teatree.mutation]\nhigh_value_modules = [ "src/teatree/x.py" ]\nmode = "explode"\n',
        )
        with pytest.raises(ValueError, match="mode"):
            load_settings(path)
