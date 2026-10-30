import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hooks.scripts import hook_router
from hooks.scripts.bot_push_pretooluse import bot_push_refusal
from hooks.scripts.foreign_branch_push_gate import handle_block_foreign_branch_push
from teatree.hooks.bot_push_gate import BotRepo

_GIT = shutil.which("git") or "/usr/bin/git"


def test_interactive_hook_refuses_raw_push_to_bot_repo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    subprocess.run([_GIT, "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(
        [_GIT, "-C", str(tmp_path), "remote", "add", "origin", "https://github.com/build-bot/engine.git"],
        check=True,
    )
    data = {"tool_name": "Bash", "tool_input": {"command": "git push origin HEAD"}, "cwd": str(tmp_path)}
    bot = BotRepo("github.com/build-bot/engine", "github.com", "github", "build-bot/engine", "build-bot")

    with patch("teatree.hooks.bot_push_gate.bot_repo_for", return_value=bot):
        assert handle_block_foreign_branch_push(data)

    assert "t3 push" in json.loads(capsys.readouterr().out)["permissionDecisionReason"]
    assert handle_block_foreign_branch_push in hook_router._HANDLERS["PreToolUse"]


def test_interactive_hook_allows_non_bot_repo(tmp_path: Path) -> None:
    subprocess.run([_GIT, "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(
        [_GIT, "-C", str(tmp_path), "remote", "add", "origin", "https://github.com/team/engine.git"],
        check=True,
    )
    data = {"tool_name": "Bash", "tool_input": {"command": "git push origin HEAD"}, "cwd": str(tmp_path)}

    with patch("teatree.hooks.bot_push_gate.bot_repo_for", return_value=None):
        assert bot_push_refusal(data) is None


def test_interactive_hook_refuses_direct_bot_url(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    subprocess.run([_GIT, "init", "-q", "-b", "main", str(tmp_path)], check=True)
    remote = "https://github.com/build-bot/engine.git"
    data = {"tool_name": "Bash", "tool_input": {"command": f"git push {remote} HEAD"}, "cwd": str(tmp_path)}
    bot = BotRepo("github.com/build-bot/engine", "github.com", "github", "build-bot/engine", "build-bot")

    with patch("teatree.hooks.bot_push_gate.bot_repo_for", return_value=bot):
        assert handle_block_foreign_branch_push(data)

    assert "t3 push" in json.loads(capsys.readouterr().out)["permissionDecisionReason"]
