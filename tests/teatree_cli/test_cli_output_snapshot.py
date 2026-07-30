"""The representative CLI-output fixture renders deterministically and stays in sync.

The loud local gate for ``docs/generated/cli/representative-output.md`` — the CLI
analog of ``test_dashboard_snapshot.py`` for the admin "screenshot". CI also catches
the same drift via ``git diff --exit-code docs/generated`` after regenerating. The
render is a pure command-tree function (no DB), so this gate needs no database.

See: souliane/teatree#12
"""

from pathlib import Path

import pytest
from typer.main import get_command

from teatree.cli import app, register_overlay_commands
from teatree.cli.cli_output_snapshot import render_cli_output_snapshot
from teatree.cli.command_tree import _resolve_command_path, render_help_blocks

_CANONICAL = Path(__file__).resolve().parents[2] / "docs/generated/cli/representative-output.md"


def test_committed_fixture_is_in_sync() -> None:
    expected = _CANONICAL.read_text(encoding="utf-8")
    assert render_cli_output_snapshot() == expected, (
        "docs/generated/cli/representative-output.md is stale — regenerate it:\n"
        "  uv run python scripts/hooks/generate_cli_output_snapshot.py"
    )


def test_render_is_byte_stable() -> None:
    assert render_cli_output_snapshot() == render_cli_output_snapshot()


def test_render_is_width_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    narrow = render_cli_output_snapshot()
    monkeypatch.setenv("COLUMNS", "200")
    wide = render_cli_output_snapshot()
    assert narrow == wide


def test_fixture_captures_the_representative_commands() -> None:
    snapshot = render_cli_output_snapshot()
    assert "## `t3`" in snapshot
    assert "## `t3 loop`" in snapshot
    assert "claim-next" in snapshot  # a representative t3 loop subcommand


def test_committed_fixture_has_no_trailing_whitespace() -> None:
    for line in _CANONICAL.read_text(encoding="utf-8").split("\n"):
        assert line == line.rstrip()


def test_resolve_command_path_rejects_unknown_and_non_group() -> None:
    register_overlay_commands(allowlist={"t3-teatree"})
    click_app = get_command(app)
    with pytest.raises(KeyError):
        _resolve_command_path(click_app, ["nope"], base_name="t3")
    with pytest.raises(KeyError):
        # ``whoami`` is a leaf, so descending into it has no subcommands.
        _resolve_command_path(click_app, ["loop", "whoami", "x"], base_name="t3")


def test_render_help_blocks_renders_each_requested_path() -> None:
    register_overlay_commands(allowlist={"t3-teatree"})
    rendered = render_help_blocks(app, [["loop"]])
    assert rendered.startswith("## `t3 loop`")
    assert "## `t3`" not in rendered
