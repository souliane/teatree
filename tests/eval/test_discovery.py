"""Scenario discovery walks ``src/teatree/eval/scenarios/*.yaml``."""

from pathlib import Path
from unittest.mock import patch

from teatree.eval import discovery
from teatree.eval.discovery import discover_specs, find_spec

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
        with patch.object(discovery, "SCENARIOS_DIR", scenarios):
            specs = discover_specs()
        assert [s.name for s in specs] == ["alpha", "mu", "zeta"]

    def test_returns_empty_list_when_directory_is_empty(self, tmp_path: Path) -> None:
        empty = tmp_path / "scenarios"
        empty.mkdir()
        with patch.object(discovery, "SCENARIOS_DIR", empty):
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
        with patch.object(discovery, "SCENARIOS_DIR", scenarios):
            found = find_spec("two")
        assert found is not None
        assert found.name == "two"

    def test_returns_none_when_no_match(self, tmp_path: Path) -> None:
        scenarios = tmp_path / "scenarios"
        _seed_scenarios(scenarios, ["only"])
        with patch.object(discovery, "SCENARIOS_DIR", scenarios):
            assert find_spec("missing") is None
