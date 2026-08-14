"""``t3 eval changed-scenarios`` selects exactly the scenarios a PR's diff touched.

The reusable overlay-facing CLI reads a PR's changed file paths from STDIN and
prints the ``name`` of each discovered scenario whose source YAML the PR touched.
A PR that edits no scenario file resolves to nothing (exit ``--skip-code``), so a
caller's eval job runs only when scenarios actually changed. These exercise the
command through the real typer CLI; the catalog discovery is real (no mock), so
the parity with the host's ``scripts/eval/scenarios_for_changed.py`` shim is
exercised end to end.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.eval import quarantine
from teatree.eval.changed_scenarios import MAX_SELECTIVE_PR_SCENARIOS, specs_under
from teatree.eval.discovery import SCENARIOS_DIR, discover_specs

_REPO_ROOT = SCENARIOS_DIR.parents[1]


class TestChangedScenarios:
    def test_changed_scenario_file_prints_its_names_and_exits_zero(self) -> None:
        catalog_file = min(SCENARIOS_DIR.glob("*.yaml"))
        rel = catalog_file.relative_to(_REPO_ROOT).as_posix()
        expected = sorted(s.name for s in discover_specs() if s.source_path == catalog_file)
        assert expected, "the chosen catalog file must define at least one scenario"
        result = CliRunner().invoke(app, ["eval", "changed-scenarios"], input=f"{rel}\n")
        assert result.exit_code == 0, result.output
        assert [line for line in result.output.splitlines() if line] == expected

    def test_no_scenario_changed_exits_skip_code_and_prints_nothing(self) -> None:
        result = CliRunner().invoke(
            app, ["eval", "changed-scenarios", "--skip-code", "3"], input="src/teatree/cli/eval/app.py\n"
        )
        assert result.exit_code == 3
        assert result.output.strip() == ""

    def test_empty_stdin_exits_default_skip_code(self) -> None:
        result = CliRunner().invoke(app, ["eval", "changed-scenarios"], input="")
        assert result.exit_code == 1
        assert result.output.strip() == ""

    def test_blank_lines_are_ignored(self) -> None:
        result = CliRunner().invoke(app, ["eval", "changed-scenarios"], input="\n   \nsrc/teatree/x.py\n")
        assert result.exit_code == 1

    def test_corpus_wide_change_surfaces_truncation_on_stderr(self) -> None:
        # Every real scenario file changed → the selection exceeds the cap; the deferral
        # note must appear on stderr (#2737) while stdout still prints only the capped run
        # set — a corpus-wide PR's truncated coverage is visible, not silently dropped.
        # Scope to THIS repo's own scenarios before making paths repo-relative.
        # ``discover_specs()`` returns the union catalog — core plus every installed
        # overlay's scenarios — and an overlay installed from outside this repo (the
        # vendored-fork topology) has ``source_path``s that are not under
        # ``_REPO_ROOT`` at all, so mapping the union through ``relative_to`` raises.
        # ``specs_under`` is the per-consumer filter that exists for exactly this.
        core_specs = specs_under(discover_specs(), SCENARIOS_DIR)
        all_files = sorted({s.source_path.relative_to(_REPO_ROOT).as_posix() for s in core_specs})
        result = CliRunner().invoke(app, ["eval", "changed-scenarios"], input="\n".join(all_files) + "\n")
        assert result.exit_code == 0, result.output
        assert "capped to" in result.stderr
        assert "weekly sharded lane" in result.stderr
        assert len([line for line in result.stdout.splitlines() if line]) == MAX_SELECTIVE_PR_SCENARIOS


class TestChangedScenariosOverlayFacingFlags:
    """``--repo-root`` / ``--scenarios-dir`` / ``--require-specs`` make the CLI reusable (#3337).

    A consuming overlay's diff paths are relative to its own root and it owns only its own
    scenarios; these flags surface the parameters the shared core already accepted so the CLI
    core advertises as reusable is reachable, and the empty-catalog quiet-skip closes.
    """

    def _real_scenario_rel(self) -> str:
        return min(SCENARIOS_DIR.glob("*.yaml")).relative_to(_REPO_ROOT).as_posix()

    def test_explicit_teatree_root_matches_like_the_default(self) -> None:
        rel = self._real_scenario_rel()
        result = CliRunner().invoke(
            app, ["eval", "changed-scenarios", "--repo-root", str(_REPO_ROOT)], input=f"{rel}\n"
        )
        assert result.exit_code == 0, result.output
        assert [line for line in result.output.splitlines() if line]

    def test_wrong_repo_root_relativizes_elsewhere_and_skips(self, tmp_path: Path) -> None:
        # The path is a real teatree scenario, but relative to a DIFFERENT root it no longer
        # matches the catalog's source paths, so the lane cleanly skips (exit --skip-code).
        rel = self._real_scenario_rel()
        result = CliRunner().invoke(app, ["eval", "changed-scenarios", "--repo-root", str(tmp_path)], input=f"{rel}\n")
        assert result.exit_code == 1
        assert result.output.strip() == ""

    def test_scenarios_dir_filter_excludes_the_real_catalog(self, tmp_path: Path) -> None:
        # A real scenario changed, but the filtered catalog (an unrelated dir) holds none of
        # teatree's specs, so nothing is selected — the per-consumer catalog scope in action.
        rel = self._real_scenario_rel()
        result = CliRunner().invoke(
            app, ["eval", "changed-scenarios", "--scenarios-dir", str(tmp_path)], input=f"{rel}\n"
        )
        assert result.exit_code == 1
        assert result.output.strip() == ""

    def test_scenarios_dir_pointing_at_the_catalog_still_matches(self) -> None:
        rel = self._real_scenario_rel()
        result = CliRunner().invoke(
            app, ["eval", "changed-scenarios", "--scenarios-dir", str(SCENARIOS_DIR)], input=f"{rel}\n"
        )
        assert result.exit_code == 0, result.output
        assert [line for line in result.output.splitlines() if line]

    def test_require_specs_fails_loud_on_empty_catalog(self, tmp_path: Path) -> None:
        # "empty catalog" and "nothing changed" both skip today; --require-specs separates them.
        rel = self._real_scenario_rel()
        result = CliRunner().invoke(
            app,
            ["eval", "changed-scenarios", "--scenarios-dir", str(tmp_path), "--require-specs"],
            input=f"{rel}\n",
        )
        assert result.exit_code == 2
        assert "filtered catalog is empty" in result.stderr

    def test_empty_catalog_without_require_specs_still_skips(self, tmp_path: Path) -> None:
        rel = self._real_scenario_rel()
        result = CliRunner().invoke(
            app, ["eval", "changed-scenarios", "--scenarios-dir", str(tmp_path)], input=f"{rel}\n"
        )
        assert result.exit_code == 1


RULES_SKILL = "skills/rules/SKILL.md"
GRADED_SECTION = "Background Long Operations (Non-Negotiable)"
UNGRADED_SECTION = "Temp File Safety"


def _hunk_inside(section: str) -> str:
    """A ``-U0`` hunk touching one line of *section* in the REAL rules skill."""
    lines = (_REPO_ROOT / RULES_SKILL).read_text(encoding="utf-8").splitlines()
    heading = next(n for n, line in enumerate(lines, start=1) if line.startswith("## ") and line[3:].strip() == section)
    body = heading + 1
    return f"--- a/{RULES_SKILL}\n+++ b/{RULES_SKILL}\n@@ -{body} +{body} @@\n-was\n+now\n"


def _graded_by(section: str) -> list[str]:
    return sorted(s.name for s in discover_specs() if s.agent_path == RULES_SKILL and section in s.agent_sections)


class TestDiffFileNarrowsProseSelection:
    """``--diff-file`` gives the section granularity a path list cannot carry (#3944)."""

    def _run(self, diff_file: Path | None) -> list[str]:
        argv = ["eval", "changed-scenarios"]
        if diff_file is not None:
            argv += ["--diff-file", str(diff_file)]
        result = CliRunner().invoke(app, argv, input=f"{RULES_SKILL}\n")
        assert result.exit_code == 0, result.output
        return [line for line in result.stdout.splitlines() if line]

    def test_graded_section_edit_selects_its_scenarios_first(self, tmp_path: Path) -> None:
        diff = tmp_path / "changed.diff"
        diff.write_text(_hunk_inside(GRADED_SECTION), encoding="utf-8")
        graded = _graded_by(GRADED_SECTION)
        assert graded, "the motivating section must still be graded by at least one scenario"
        assert self._run(diff)[: len(graded)] == graded

    def test_ungraded_section_edit_selects_no_scenario_that_grades_elsewhere(self, tmp_path: Path) -> None:
        diff = tmp_path / "changed.diff"
        diff.write_text(_hunk_inside(UNGRADED_SECTION), encoding="utf-8")
        selected = set(self._run(diff))
        # Only whole-file scenarios may appear; a scenario pinned to a DIFFERENT section
        # must not be dragged in by an edit that never touched what it grades.
        assert not selected & set(_graded_by(GRADED_SECTION))

    def test_without_the_diff_every_scenario_grading_the_file_is_eligible(self) -> None:
        # The unchanged STDIN-only contract stays fail-safe: unknown granularity selects
        # more, never the pre-#3944 zero.
        assert self._run(None)


class TestQuarantineSuppression:
    """A scenario in the known-red registry is dropped from the lane and named (#4173)."""

    def test_a_quarantined_scenario_is_dropped_and_named_on_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog_file = min(SCENARIOS_DIR.glob("*.yaml"))
        rel = catalog_file.relative_to(_REPO_ROOT).as_posix()
        names = sorted(s.name for s in discover_specs() if s.source_path == catalog_file)
        registry = tmp_path / "quarantine.yaml"
        registry.write_text(
            f"scenarios:\n  {names[0]}:\n"
            f"    issue: https://github.com/souliane/teatree/issues/4172\n"
            f"    until: 2999-01-01\n    reason: tracked known red\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(quarantine, "QUARANTINE_PATH", registry)
        result = CliRunner().invoke(app, ["eval", "changed-scenarios", "--skip-code", "3"], input=f"{rel}\n")
        assert names[0] not in [line for line in result.output.splitlines() if line in names]
        assert "suppressed 1 quarantined known-red scenario(s)" in result.output
        assert names[0] in result.output

    def test_the_registry_sits_beside_the_scenarios_dir(self, tmp_path: Path) -> None:
        # The convention teatree's own layout defines, so a consumer passing its own
        # --scenarios-dir reaches its own registry with no extra flag.
        assert quarantine.quarantine_path_for(None) == quarantine.QUARANTINE_PATH
        assert quarantine.quarantine_path_for(tmp_path / "eval" / "scenarios") == (
            tmp_path / "eval" / "quarantine.yaml"
        )
