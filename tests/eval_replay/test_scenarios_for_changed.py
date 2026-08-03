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

from teatree.eval.changed_scenarios import MAX_SELECTIVE_PR_SCENARIOS, selection_for_changed
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


class TestTruncationSurfaced:
    """#2737: when the cap bites the selector reports the deferral, never silence."""

    def _many_specs(self, count: int) -> list[EvalSpec]:
        return [_spec(f"s{n:04d}", SCENARIOS_DIR / f"f{n:04d}.yaml") for n in range(count)]

    def test_selection_below_cap_has_no_truncation_note(self) -> None:
        specs = self._many_specs(MAX_SELECTIVE_PR_SCENARIOS)
        changed = [s.source_path.relative_to(_REPO_ROOT).as_posix() for s in specs]
        selection = selection_for_changed(changed, specs, _REPO_ROOT)
        assert not selection.truncated
        assert selection.truncation_note() is None

    def test_selection_above_cap_reports_total_deferred_and_note(self) -> None:
        total = MAX_SELECTIVE_PR_SCENARIOS + 50
        specs = self._many_specs(total)
        changed = [s.source_path.relative_to(_REPO_ROOT).as_posix() for s in specs]
        selection = selection_for_changed(changed, specs, _REPO_ROOT)
        assert selection.truncated
        assert selection.total_matched == total
        assert selection.deferred == 50
        assert len(selection.names) == MAX_SELECTIVE_PR_SCENARIOS
        note = selection.truncation_note()
        assert note is not None
        assert f"selected {total} changed scenarios" in note
        assert f"capped to {MAX_SELECTIVE_PR_SCENARIOS}" in note
        assert "deferred 50 to the weekly sharded lane" in note


class TestMain:
    def test_corpus_wide_change_surfaces_truncation_on_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Every real scenario file changed → the selection exceeds the cap; the deferral
        # note must appear on stderr (not silently drop the deferred scenarios), while
        # stdout still prints exactly the capped run set.
        all_files = sorted({s.source_path.relative_to(_REPO_ROOT).as_posix() for s in discover_specs()})
        monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(all_files) + "\n"))
        code = main([])
        captured = capsys.readouterr()
        assert code == 0
        assert "capped to" in captured.err
        assert "weekly sharded lane" in captured.err
        assert len([line for line in captured.out.splitlines() if line]) == MAX_SELECTIVE_PR_SCENARIOS

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


RULES_SKILL = "skills/rules/SKILL.md"
GRADED_SECTION = "Background Long Operations (Non-Negotiable)"


def _hunk_inside(section: str) -> str:
    """A ``-U0`` hunk touching one line of *section* in the REAL rules skill."""
    lines = (_REPO_ROOT / RULES_SKILL).read_text(encoding="utf-8").splitlines()
    heading = next(n for n, line in enumerate(lines, start=1) if line.startswith("## ") and line[3:].strip() == section)
    body = heading + 1
    return f"--- a/{RULES_SKILL}\n+++ b/{RULES_SKILL}\n@@ -{body} +{body} @@\n-was\n+now\n"


class TestDiffFileNarrowsProseSelection:
    """A prose-only edit selects the scenarios that GRADE it (#3944).

    #3911 edited this exact section — the graded prompt for ``headless_one_shot_envelope``
    and its ``background_long_operations_*`` siblings — and the lane selected zero, then
    reported PASS. These drive the real catalog and the real skill file, so the regression
    is pinned end to end rather than against a fixture that could drift from either.
    """

    def _selected(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
    ) -> list[str]:
        monkeypatch.setattr("sys.stdin", io.StringIO(f"{RULES_SKILL}\n"))
        assert main(argv) == 0
        return [line for line in capsys.readouterr().out.splitlines() if line]

    def _graded_by(self, section: str) -> list[str]:
        return sorted(s.name for s in discover_specs() if s.agent_path == RULES_SKILL and section in s.agent_sections)

    def test_editing_a_graded_section_selects_every_scenario_grading_it(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        diff = tmp_path / "changed.diff"
        diff.write_text(_hunk_inside(GRADED_SECTION), encoding="utf-8")
        selected = self._selected(monkeypatch, capsys, ["--diff-file", str(diff)])
        graded = self._graded_by(GRADED_SECTION)
        assert graded, "the motivating section must still be graded by at least one scenario"
        # Every scenario naming the section survives the cap — the band ranks them first, so a
        # broader whole-file match can never displace the tightest evidence.
        assert set(graded) <= set(selected)
        assert selected[: len(graded)] == graded

    def test_a_skill_path_alone_still_selects_without_a_diff(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The unchanged STDIN-only contract: granularity unknown → fail-safe to every
        # scenario grading the file, never the pre-#3944 zero.
        assert self._selected(monkeypatch, capsys, [])
