"""'t3 codex auth check' runs one OK-turn under the auth-cache lock and names why it failed."""

import base64
import fcntl
import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import Result
from typer.testing import CliRunner

from teatree.agents.codex_auth_cache import CodexAuthCache
from teatree.cli.codex import codex_app
from tests.teatree_agents.test_codex_shared_app_server import _SERVER

_SENTINEL = "sentinel-do-not-print"
_LOGGING_SERVER = "import sys\nLOG = sys.argv[1]\n" + _SERVER.replace(
    "    request = json.loads(raw)\n", "    request = json.loads(raw)\n    open(LOG, 'a').write(raw)\n", 1
)
_ACCOUNT_READ = (
    "    elif method == 'account/read':\n"
    "        response = {'account': {'type': 'chatgpt'}, 'requiresOpenaiAuth': True}"
)
_UNAUTHORIZED = (
    "    elif method == 'account/read':\n"
    "        error = {'code': -32000, 'message': 'do-not-print', 'data': {'codexErrorInfo': 'unauthorized'}}\n"
    "        print(json.dumps({'id': request_id, 'error': error}), flush=True)\n"
    "        continue"
)
_UNAUTHORIZED_SERVER = _LOGGING_SERVER.replace(_ACCOUNT_READ, _UNAUTHORIZED)


class _World:
    def __init__(self, home: Path, log: Path) -> None:
        self.home = home
        self.log = log

    def requests(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []


def _run_canary(tmp_path: Path, server: str, *, lock_wait: float = 60.0) -> tuple[_World, Result]:
    script = tmp_path / "server.py"
    script.write_text(server)
    world = _World(tmp_path / "codex-home", tmp_path / "requests.jsonl")
    login = base64.b64encode(json.dumps({"tokens": {"access_token": _SENTINEL}}).encode()).decode()
    command = (sys.executable, str(script), str(world.log))
    with (
        patch.dict(os.environ, {"T3_CODEX_HOME": str(world.home)}),
        patch("teatree.agents.codex_app_server.codex_command", return_value=command),
        patch("teatree.agents.codex_canary.LOCK_WAIT_SECONDS", lock_wait),
        patch(
            "teatree.agents.codex_canary.CodexAuthCache",
            lambda home: CodexAuthCache(home, pass_read=lambda _entry: login),
        ),
    ):
        return world, CliRunner().invoke(codex_app, ["auth", "check"])


@pytest.fixture
def held_lock(tmp_path: Path) -> Iterator[None]:
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    fd = os.open(home / ".auth.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        yield
    finally:
        os.close(fd)


def test_one_ok_turn_in_an_empty_temp_directory_passes_without_printing_the_login(tmp_path: Path) -> None:
    world, result = _run_canary(tmp_path, _LOGGING_SERVER)

    started = [request for request in world.requests() if request["method"] == "thread/start"]
    cwd = Path(started[0]["params"]["cwd"])
    turns = [request for request in world.requests() if request["method"] == "turn/start"]
    assert result.exit_code == 0
    assert result.output.splitlines() == ["Codex canary: starting", "Codex canary: PASS"]
    assert cwd.name.startswith("t3-codex-canary-")
    assert not cwd.exists()
    assert len(turns) == 1
    assert "single word OK" in json.dumps(turns[0])
    assert started[0]["params"]["sandbox"] == "read-only"
    assert _SENTINEL not in result.output


def test_an_auth_failure_is_a_named_fail_with_exit_one(tmp_path: Path) -> None:
    _world, result = _run_canary(tmp_path, _UNAUTHORIZED_SERVER)

    assert result.exit_code == 1
    assert result.output.splitlines()[0] == "Codex canary: starting"
    assert "FAIL: Codex managed ChatGPT authentication is unavailable" in result.output
    assert _SENTINEL not in result.output
    assert "do-not-print" not in result.output


@pytest.mark.usefixtures("held_lock")
def test_a_lock_held_by_another_run_fails_within_the_lock_wait(tmp_path: Path) -> None:
    started = time.monotonic()

    _world, result = _run_canary(tmp_path, _LOGGING_SERVER, lock_wait=0.3)

    assert time.monotonic() - started < 10
    assert result.exit_code == 1
    assert "FAIL: Codex auth cache is held by another run" in result.output
    assert _SENTINEL not in result.output
