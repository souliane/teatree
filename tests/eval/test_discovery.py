"""Scenario discovery walks ``src/teatree/eval/scenarios/*.yaml`` and overlays."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from teatree.eval import discovery
from teatree.eval.discovery import _discover_colocated_specs, _discover_overlay_specs, discover_specs, find_spec
from teatree.eval.loader import EvalSpecError

_MINIMAL = (
    "- name: {name}\n"
    "  scenario: example scenario\n"
    "  prompt: do the thing\n"
    "  expect:\n"
    "    - tool_call: bash\n"
    '      args.command: contains "git worktree add"\n'
)


def _seed_scenarios(scenarios_dir: Path, names: list[str]) -> None:
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (scenarios_dir / f"{name}.yaml").write_text(_MINIMAL.format(name=name), encoding="utf-8")


class TestDiscoverSpecs:
    def test_returns_specs_in_sorted_order(self, tmp_path: Path) -> None:
        scenarios = tmp_path / "scenarios"
        _seed_scenarios(scenarios, ["zeta", "alpha", "mu"])
        with (
            patch.object(discovery, "SCENARIOS_DIR", scenarios),
            patch.object(discovery, "_discover_colocated_specs", return_value=[]),
            patch.object(discovery, "_discover_overlay_specs", return_value=[]),
        ):
            specs = discover_specs()
        assert [s.name for s in specs] == ["alpha", "mu", "zeta"]

    def test_returns_empty_list_when_directory_is_empty(self, tmp_path: Path) -> None:
        empty = tmp_path / "scenarios"
        empty.mkdir()
        with (
            patch.object(discovery, "SCENARIOS_DIR", empty),
            patch.object(discovery, "_discover_colocated_specs", return_value=[]),
            patch.object(discovery, "_discover_overlay_specs", return_value=[]),
        ):
            specs = discover_specs()
        assert specs == []

    def test_bundled_scenarios_loadable(self) -> None:
        # The shipped scenarios directory must always load without errors —
        # this guards against malformed YAML in the bundled set.
        specs = discover_specs()
        assert any(s.name == "worktree_first" for s in specs)


class TestFindSpec:
    def test_returns_matching_spec_by_name(self, tmp_path: Path) -> None:
        scenarios = tmp_path / "scenarios"
        _seed_scenarios(scenarios, ["one", "two"])
        with (
            patch.object(discovery, "SCENARIOS_DIR", scenarios),
            patch.object(discovery, "_discover_colocated_specs", return_value=[]),
            patch.object(discovery, "_discover_overlay_specs", return_value=[]),
        ):
            found = find_spec("two")
        assert found is not None
        assert found.name == "two"

    def test_returns_none_when_no_match(self, tmp_path: Path) -> None:
        scenarios = tmp_path / "scenarios"
        _seed_scenarios(scenarios, ["only"])
        with (
            patch.object(discovery, "SCENARIOS_DIR", scenarios),
            patch.object(discovery, "_discover_colocated_specs", return_value=[]),
            patch.object(discovery, "_discover_overlay_specs", return_value=[]),
        ):
            assert find_spec("missing") is None


def _fake_overlay(scenarios_dir: Path | None) -> SimpleNamespace:
    return SimpleNamespace(get_eval_scenarios_dir=lambda: scenarios_dir)


class TestDiscoverOverlaySpecs:
    def test_returns_specs_from_overlay_dir(self, tmp_path: Path) -> None:
        overlay_scenarios = tmp_path / "overlay" / "eval" / "scenarios"
        _seed_scenarios(overlay_scenarios, ["over_one", "over_two"])
        overlay = _fake_overlay(overlay_scenarios)
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value={"t3-fake": overlay}):
            specs = _discover_overlay_specs()
        assert sorted(s.name for s in specs) == ["over_one", "over_two"]

    def test_skips_overlay_without_hook(self) -> None:
        # An overlay that has no ``get_eval_scenarios_dir`` attribute at all
        # must not break discovery — the harness must remain forward-compatible
        # with older overlay classes that predate the hook.
        overlay = SimpleNamespace()
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value={"t3-old": overlay}):
            specs = _discover_overlay_specs()
        assert specs == []

    def test_skips_overlay_returning_none(self) -> None:
        overlay = _fake_overlay(None)
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value={"t3-none": overlay}):
            specs = _discover_overlay_specs()
        assert specs == []

    def test_skips_overlay_pointing_at_missing_dir(self, tmp_path: Path) -> None:
        overlay = _fake_overlay(tmp_path / "does-not-exist")
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value={"t3-missing": overlay}):
            specs = _discover_overlay_specs()
        assert specs == []

    def test_returns_empty_when_overlay_hook_raises(self, tmp_path: Path) -> None:
        def _bad() -> Path:
            msg = "boom"
            raise RuntimeError(msg)

        overlay = SimpleNamespace(get_eval_scenarios_dir=_bad)
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value={"t3-bad": overlay}):
            specs = _discover_overlay_specs()
        assert specs == []

    def test_returns_empty_when_overlay_loader_raises(self) -> None:
        def _explode() -> None:
            msg = "no overlays"
            raise RuntimeError(msg)

        with patch("teatree.core.overlay_loader.get_all_overlays", side_effect=_explode):
            specs = _discover_overlay_specs()
        assert specs == []

    def test_logs_warning_on_malformed_overlay_yaml(self, tmp_path: Path) -> None:
        # A malformed YAML in one overlay must not blow up the whole catalog —
        # the loader logs and continues so other overlays' specs still surface.
        overlay_scenarios = tmp_path / "overlay" / "eval" / "scenarios"
        overlay_scenarios.mkdir(parents=True)
        (overlay_scenarios / "bad.yaml").write_text("not: a: list\n", encoding="utf-8")
        _seed_scenarios(overlay_scenarios, ["good_one"])
        overlay = _fake_overlay(overlay_scenarios)
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value={"t3-mixed": overlay}):
            specs = _discover_overlay_specs()
        assert [s.name for s in specs] == ["good_one"]


def _seed_colocated(skills_dir: Path, skill: str, names: list[str]) -> None:
    skill_dir = skills_dir / skill
    skill_dir.mkdir(parents=True, exist_ok=True)
    body = "".join(_MINIMAL.format(name=n) for n in names)
    (skill_dir / "evals.yaml").write_text(body, encoding="utf-8")


class TestDiscoverColocatedSpecs:
    def test_picks_up_evals_yaml_beside_each_skill(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _seed_colocated(skills, "ship", ["ship_one"])
        _seed_colocated(skills, "review", ["review_one"])
        specs = _discover_colocated_specs(skills_dir=skills)
        assert sorted(s.name for s in specs) == ["review_one", "ship_one"]

    def test_defaults_agent_path_to_owning_skill(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _seed_colocated(skills, "ship", ["ship_one"])
        specs = _discover_colocated_specs(skills_dir=skills)
        assert specs[0].agent_path == "skills/ship/SKILL.md"

    def test_returns_empty_when_no_skills_dir(self, tmp_path: Path) -> None:
        assert _discover_colocated_specs(skills_dir=tmp_path / "missing") == []

    def test_skill_dir_without_evals_yaml_is_skipped(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        (skills / "platforms").mkdir(parents=True)
        (skills / "platforms" / "SKILL.md").write_text("---\nname: platforms\n---\n", encoding="utf-8")
        assert _discover_colocated_specs(skills_dir=skills) == []


class TestDiscoverSpecsCombined:
    def test_concatenates_core_and_overlay_specs(self, tmp_path: Path) -> None:
        core = tmp_path / "core"
        overlay_scenarios = tmp_path / "overlay" / "eval" / "scenarios"
        _seed_scenarios(core, ["core_a"])
        _seed_scenarios(overlay_scenarios, ["over_b"])
        overlay = _fake_overlay(overlay_scenarios)
        with (
            patch.object(discovery, "SCENARIOS_DIR", core),
            patch.object(discovery, "_discover_colocated_specs", return_value=[]),
            patch("teatree.core.overlay_loader.get_all_overlays", return_value={"t3-combo": overlay}),
        ):
            specs = discover_specs()
        assert [s.name for s in specs] == ["core_a", "over_b"]

    def test_includes_colocated_between_core_and_overlay(self, tmp_path: Path) -> None:
        core = tmp_path / "core"
        skills = tmp_path / "skills"
        _seed_scenarios(core, ["core_a"])
        _seed_colocated(skills, "ship", ["ship_co"])
        with (
            patch.object(discovery, "SCENARIOS_DIR", core),
            patch.object(discovery, "DEFAULT_SKILLS_DIR", skills),
            patch.object(discovery, "_discover_overlay_specs", return_value=[]),
        ):
            specs = discover_specs()
        assert [s.name for s in specs] == ["core_a", "ship_co"]

    def test_duplicate_name_across_sources_is_hard_error(self, tmp_path: Path) -> None:
        core = tmp_path / "core"
        skills = tmp_path / "skills"
        _seed_scenarios(core, ["dup"])
        _seed_colocated(skills, "ship", ["dup"])
        with (
            patch.object(discovery, "SCENARIOS_DIR", core),
            patch.object(discovery, "DEFAULT_SKILLS_DIR", skills),
            patch.object(discovery, "_discover_overlay_specs", return_value=[]),
            pytest.raises(EvalSpecError, match="duplicate scenario name"),
        ):
            discover_specs()
