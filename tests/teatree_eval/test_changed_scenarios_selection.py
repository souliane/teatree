"""``select_changed_scenarios`` is parameterized by ``repo_root`` and the catalog (#3337).

The inner matcher (:func:`selection_for_changed`) already took both parameters; the entry
point the CLI exposes hardwired teatree's own root and the full union catalog, so a consuming
overlay could not reach it. These tests pin that the entry point now honors both keyword
arguments while every default preserves teatree's own lane behavior exactly.
"""

from pathlib import Path

import pytest

from teatree.eval import changed_scenarios as cs
from teatree.eval.models import EvalSpec


def _spec(name: str, source_path: Path) -> EvalSpec:
    return EvalSpec(name=name, scenario="", agent_path="", prompt="", matchers=(), source_path=source_path)


class TestSpecsUnder:
    def test_keeps_specs_under_dir_and_drops_the_rest(self, tmp_path: Path) -> None:
        catalog = tmp_path / "catalog"
        specs = [
            _spec("mine", catalog / "a.yaml"),
            _spec("mine_nested", catalog / "sub" / "b.yaml"),
            _spec("theirs", tmp_path / "other" / "c.yaml"),
        ]
        kept = cs.specs_under(specs, catalog)
        assert sorted(s.name for s in kept) == ["mine", "mine_nested"]

    def test_empty_when_dir_holds_no_specs(self, tmp_path: Path) -> None:
        specs = [_spec("theirs", tmp_path / "core" / "a.yaml")]
        assert cs.specs_under(specs, tmp_path / "empty") == []


class TestSelectChangedScenariosParameters:
    def test_repo_root_and_specs_are_both_honored(self, tmp_path: Path) -> None:
        spec = _spec("only", tmp_path / "evals" / "scenarios" / "x.yaml")
        selection = cs.select_changed_scenarios(["evals/scenarios/x.yaml"], repo_root=tmp_path, specs=[spec])
        assert selection.names == ["only"]

    def test_wrong_repo_root_matches_nothing(self, tmp_path: Path) -> None:
        # The diff path is relative to tmp_path, but the caller declares a different root,
        # so the same path normalizes elsewhere and selects no scenario (the quiet-skip a
        # consuming repo hits when it forgets to pass its own root).
        spec = _spec("only", tmp_path / "evals" / "scenarios" / "x.yaml")
        selection = cs.select_changed_scenarios(
            ["evals/scenarios/x.yaml"], repo_root=tmp_path / "elsewhere", specs=[spec]
        )
        assert selection.names == []

    def test_defaults_fall_back_to_discovery_and_teatree_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No keyword arguments == today's behavior: discover_specs() over teatree's own root.
        sentinel = [_spec("discovered", cs.REPO_ROOT / "evals" / "scenarios" / "disc.yaml")]
        monkeypatch.setattr(cs, "discover_specs", lambda: sentinel)
        selection = cs.select_changed_scenarios(["evals/scenarios/disc.yaml"])
        assert selection.names == ["discovered"]
