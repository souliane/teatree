"""The selective-PR eval picks exactly the scenarios a PR's changed files define.

``scenarios_for_changed.py`` is the PR-path selector: it reads changed file
paths from STDIN, discovers every spec, and prints the ``name`` of each spec
whose ``source_path`` (made repo-relative) equals one of the changed paths. A PR
that edits no scenario file resolves to nothing (exit ``--skip-code``), so the
metered ``eval-pr`` workflow runs only when scenarios actually changed.
"""

import importlib.util
import io
from pathlib import Path

import pytest

from teatree.eval.changed_scenarios import MAX_SELECTIVE_PR_SCENARIOS
from teatree.eval.changed_scenarios import names_for_changed as _core_names_for_changed
from teatree.eval.discovery import SCENARIOS_DIR, discover_specs
from teatree.eval.models import EvalSpec

_SPEC = importlib.util.spec_from_file_location(
    "scenarios_for_changed",
    Path(__file__).parents[2] / "scripts" / "eval" / "scenarios_for_changed.py",
)
assert _SPEC is not None
assert _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

names_for_changed = _MOD.names_for_changed
main = _MOD.main

_REPO_ROOT = SCENARIOS_DIR.parents[1]


def _spec(name: str, source_path: Path) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario="s",
        agent_path="skills/code/SKILL.md",
        prompt="p",
        matchers=(),
        source_path=source_path,
    )


class TestNamesForChanged:
    def test_changed_scenario_file_yields_its_scenario_names(self) -> None:
        catalog_file = min(SCENARIOS_DIR.glob("*.yaml"))
        rel = catalog_file.relative_to(_REPO_ROOT).as_posix()
        expected = sorted(s.name for s in discover_specs() if s.source_path == catalog_file)
        assert names_for_changed([rel], discover_specs(), _REPO_ROOT) == expected
        assert expected, "the chosen catalog file must define at least one scenario"

    def test_absolute_source_path_under_src_resolves(self) -> None:
        src_yaml = _REPO_ROOT / "src" / "teatree" / "contrib" / "x" / "eval" / "scenarios" / "demo.yaml"
        specs = [_spec("alpha", src_yaml), _spec("beta", SCENARIOS_DIR / "other.yaml")]
        rel = src_yaml.relative_to(_REPO_ROOT).as_posix()
        assert names_for_changed([rel], specs, _REPO_ROOT) == ["alpha"]

    def test_no_match_returns_empty(self) -> None:
        specs = [_spec("alpha", SCENARIOS_DIR / "a.yaml")]
        assert names_for_changed(["src/teatree/cli/eval/app.py"], specs, _REPO_ROOT) == []

    def test_blank_and_whitespace_paths_are_ignored(self) -> None:
        specs = [_spec("alpha", SCENARIOS_DIR / "a.yaml")]
        assert names_for_changed(["", "   ", "src/teatree/x.py"], specs, _REPO_ROOT) == []

    def test_two_specs_one_file_both_names_deduped_and_sorted(self) -> None:
        shared = SCENARIOS_DIR / "pair.yaml"
        specs = [_spec("zeta", shared), _spec("alpha", shared), _spec("alpha", shared)]
        rel = shared.relative_to(_REPO_ROOT).as_posix()
        assert names_for_changed([rel], specs, _REPO_ROOT) == ["alpha", "zeta"]

    def test_one_changed_path_among_unrelated_resolves_only_its_scenarios(self) -> None:
        a = SCENARIOS_DIR / "a.yaml"
        b = SCENARIOS_DIR / "b.yaml"
        specs = [_spec("a1", a), _spec("b1", b)]
        assert names_for_changed([b.relative_to(_REPO_ROOT).as_posix()], specs, _REPO_ROOT) == ["b1"]


class TestSelectivePrCap:
    """A corpus-wide mechanical edit must not blow the bounded single-job PR lane.

    The selective-PR lane runs the selected scenarios SEQUENTIALLY in ONE job
    (`eval-pr.yml`), so a PR that touches every scenario file (a `model:`→`tier:`
    backfill, a mass rename) would select the whole catalog and exceed the 80-min
    step cap — the cancellation that reddened PR #2726's eval job. The selector
    caps the selection at :data:`MAX_SELECTIVE_PR_SCENARIOS`; full coverage of a
    corpus-wide change is the weekly sharded lane's job, not the PR lane's.
    """

    def _many_specs(self, count: int) -> list[EvalSpec]:
        # Distinct source files so each is independently "changed", names sortable.
        return [_spec(f"s{n:04d}", SCENARIOS_DIR / f"f{n:04d}.yaml") for n in range(count)]

    def test_selection_at_or_below_cap_is_unbounded(self) -> None:
        specs = self._many_specs(MAX_SELECTIVE_PR_SCENARIOS)
        changed = [s.source_path.relative_to(_REPO_ROOT).as_posix() for s in specs]
        out = names_for_changed(changed, specs, _REPO_ROOT)
        assert len(out) == MAX_SELECTIVE_PR_SCENARIOS
        assert out == sorted(s.name for s in specs)

    def test_selection_above_cap_is_truncated_deterministically(self) -> None:
        specs = self._many_specs(MAX_SELECTIVE_PR_SCENARIOS + 50)
        changed = [s.source_path.relative_to(_REPO_ROOT).as_posix() for s in specs]
        out = names_for_changed(changed, specs, _REPO_ROOT)
        # Bounded to the cap, and a deterministic sorted-name prefix (stable across runs).
        assert len(out) == MAX_SELECTIVE_PR_SCENARIOS
        assert out == sorted(s.name for s in specs)[:MAX_SELECTIVE_PR_SCENARIOS]

    def test_whole_real_catalog_selection_is_capped(self) -> None:
        # Every real scenario file changed → the selector caps rather than returning
        # the full ~210-scenario catalog the PR lane cannot run in one job.
        all_files = sorted({s.source_path.relative_to(_REPO_ROOT).as_posix() for s in discover_specs()})
        out = _core_names_for_changed(all_files, discover_specs(), _REPO_ROOT)
        assert len(out) <= MAX_SELECTIVE_PR_SCENARIOS


class TestMain:
    def test_real_catalog_file_prints_names_and_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        catalog_file = min(SCENARIOS_DIR.glob("*.yaml"))
        rel = catalog_file.relative_to(_REPO_ROOT).as_posix()
        expected = sorted(s.name for s in discover_specs() if s.source_path == catalog_file)
        monkeypatch.setattr("sys.stdin", io.StringIO(f"{rel}\n"))
        code = main([])
        printed = [line for line in capsys.readouterr().out.splitlines() if line]
        assert code == 0
        assert printed == expected

    def test_no_match_exits_skip_code_and_prints_nothing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO("src/teatree/cli/eval/app.py\n"))
        code = main(["--skip-code", "3"])
        assert code == 3
        assert capsys.readouterr().out.strip() == ""

    def test_empty_stdin_exits_skip_code(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        code = main([])
        assert code == 1
        assert capsys.readouterr().out.strip() == ""
