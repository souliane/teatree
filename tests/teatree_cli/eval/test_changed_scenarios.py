"""``t3 eval changed-scenarios`` selects exactly the scenarios a PR's diff touched.

The reusable overlay-facing CLI reads a PR's changed file paths from STDIN and
prints the ``name`` of each discovered scenario whose source YAML the PR touched.
A PR that edits no scenario file resolves to nothing (exit ``--skip-code``), so a
caller's eval job runs only when scenarios actually changed. These exercise the
command through the real typer CLI; the catalog discovery is real (no mock), so
the parity with the host's ``scripts/eval/scenarios_for_changed.py`` shim is
exercised end to end.
"""

from typer.testing import CliRunner

from teatree.cli import app
from teatree.eval.changed_scenarios import MAX_SELECTIVE_PR_SCENARIOS
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
        all_files = sorted({s.source_path.relative_to(_REPO_ROOT).as_posix() for s in discover_specs()})
        result = CliRunner().invoke(app, ["eval", "changed-scenarios"], input="\n".join(all_files) + "\n")
        assert result.exit_code == 0, result.output
        assert "capped to" in result.stderr
        assert "weekly sharded lane" in result.stderr
        assert len([line for line in result.stdout.splitlines() if line]) == MAX_SELECTIVE_PR_SCENARIOS
