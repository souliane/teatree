"""The local push gate receives the schema it needs in its isolated database."""

import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli import verify_gates as gates

REPO = Path(__file__).resolve().parents[2]


def test_pre_push_stage_has_a_migrated_private_database() -> None:
    saw_push = False

    def run(command: list[str], *, cwd: Path, env: dict[str, str], check: bool) -> int:
        nonlocal saw_push
        if "manage.py" in command:
            return subprocess.run(command, cwd=cwd, env=env, capture_output=True, check=False, timeout=180).returncode
        if command[-2:] == ["--hook-stage", "pre-push"]:
            database = Path(env["XDG_DATA_HOME"]) / "teatree" / "db.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"teatree_config_setting", "teatree_worktree"} <= tables
            saw_push = True
        return 0

    with (
        patch.object(gates, "_prek_available", return_value=True),
        patch.object(gates, "_timeout_available", return_value=True),
        patch.object(gates, "_resource_refusal", return_value=""),
        patch.object(gates, "run_streamed", side_effect=run),
        patch.object(gates, "write_gate_receipt"),
    ):
        result = CliRunner().invoke(app, ["tool", "verify-gates", "--repo", str(REPO), "--allow-main-clone"])

    assert result.exit_code == 0, result.output
    assert saw_push
