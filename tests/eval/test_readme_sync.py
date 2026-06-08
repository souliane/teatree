"""The eval README is the durable single map — pin it to the live structure.

A newcomer reads ``src/teatree/eval/README.md`` to learn the harness. Three
contracts keep that map honest as the code moves:

-   every ``src/teatree/cli/eval/`` path the "Where the parts live" table
    names resolves to a real file (locks the table to the subpackage cutover);
-   every ``t3 eval <subcommand>`` the README mentions is a registered command
    on ``eval_app`` and vice versa (so a new command can't ship undocumented
    and a deleted one can't linger in prose);
-   the scenarios glob the table names resolves to a non-empty directory.
"""

import re
from pathlib import Path

from teatree.cli.eval import eval_app

_REPO_ROOT = Path(__file__).resolve().parents[2]
_README = _REPO_ROOT / "src" / "teatree" / "eval" / "README.md"
_PARTS_TABLE_ROW = "| CLI surface"

_CLI_EVAL_FILE_RE = re.compile(r"`(?:src/teatree/cli/eval/)?([a-z_]+\.py)`")
# A subcommand starts with a letter; a `--flag` (e.g. `t3 eval --free-only`,
# now valid on the bare-`t3 eval` default) is NOT a subcommand and must not be
# captured as one.
_EVAL_COMMAND_RE = re.compile(r"\bt3 eval ([a-z][a-z-]*)\b")


def _readme_text() -> str:
    return _README.read_text(encoding="utf-8")


def _parts_table_cli_row() -> str:
    row = next((line for line in _readme_text().splitlines() if line.startswith(_PARTS_TABLE_ROW)), None)
    assert row is not None, f"README has no '{_PARTS_TABLE_ROW}' parts-table row"
    return row


def _registered_eval_commands() -> set[str]:
    return {command.name for command in eval_app.registered_commands if command.name}


class TestPartsTablePathsResolve:
    def test_cli_eval_files_named_in_parts_table_exist(self) -> None:
        cli_eval_dir = _REPO_ROOT / "src" / "teatree" / "cli" / "eval"
        named = _CLI_EVAL_FILE_RE.findall(_parts_table_cli_row())
        assert named, "parts-table CLI row names no cli/eval/*.py files"
        missing = [name for name in named if not (cli_eval_dir / name).is_file()]
        assert not missing, f"parts table names non-existent cli/eval files: {missing}"

    def test_parts_table_points_at_the_subpackage_not_the_flat_layout(self) -> None:
        row = _parts_table_cli_row()
        assert "src/teatree/cli/eval/" in row
        assert "cli/eval*.py" not in row

    def test_scenarios_glob_resolves_to_non_empty_dir(self) -> None:
        scenarios = _REPO_ROOT / "src" / "teatree" / "eval" / "scenarios"
        assert "src/teatree/eval/scenarios/*.yaml" in _readme_text()
        assert scenarios.is_dir()
        assert list(scenarios.glob("*.yaml")), "no scenario yaml on disk"


class TestEvalCommandSync:
    def test_every_readme_command_is_registered(self) -> None:
        registered = _registered_eval_commands()
        documented = set(_EVAL_COMMAND_RE.findall(_readme_text()))
        unknown = documented - registered
        assert not unknown, f"README names eval commands that are not registered: {sorted(unknown)}"

    def test_every_registered_command_is_documented(self) -> None:
        documented = set(_EVAL_COMMAND_RE.findall(_readme_text()))
        undocumented = _registered_eval_commands() - documented
        assert not undocumented, f"registered eval commands missing from README: {sorted(undocumented)}"
